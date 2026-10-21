"""请求性能摘要的用户手工Mock回归，不连接服务、加载模型或保存正文。"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core import business_logging as logs
from app.core.query_performance import capture_query_performance, observe_query_event


def test_nested_capture_restores_parent_even_when_inner_call_fails():
    """内层异常不能污染或丢失外层计时；未出现的阶段不补成零。"""
    observe_query_event("dense.finished", {"duration_ms": 999})
    with capture_query_performance() as outer:
        logs.log_event("dense.finished", duration_ms=3, query="SECRET_QUERY")
        with pytest.raises(ValueError):
            with capture_query_performance() as inner:
                logs.log_event("bm25.finished", duration_ms=7)
                raise ValueError("SECRET_ERROR")
        logs.log_event("dense.finished", duration_ms=4)
    assert outer.timings_ms == {"dense_retrieval_ms": 7}
    assert inner.timings_ms == {"bm25_ms": 7}
    assert outer.total_ms >= 0 and inner.total_ms >= 0
    assert "SECRET" not in str(outer.snapshot()) + str(inner.snapshot())


def test_threads_and_async_tasks_keep_independent_query_contexts():
    """并发线程和异步任务各自保留耗时，父上下文不累计子请求。"""
    barrier = Barrier(2)

    def thread_request(value):
        """同步交错两个采集范围，避免仅顺序执行造成虚假隔离结论。"""
        with capture_query_performance() as trace:
            barrier.wait(timeout=5)
            logs.log_event("dense.finished", duration_ms=value)
        return trace.timings_ms

    async def async_requests():
        """同一事件循环交错两个独立任务的上下文。"""
        async def request(value):
            """在挂起前后各记一次相同请求的固定阶段。"""
            with capture_query_performance() as trace:
                await asyncio.sleep(0)
                logs.log_event("bm25.finished", duration_ms=value)
            return trace.timings_ms

        return await asyncio.gather(request(5), request(8))

    with capture_query_performance() as parent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            threads = list(pool.map(thread_request, [2, 9]))
        tasks = asyncio.run(async_requests())
    assert threads == [{"dense_retrieval_ms": 2}, {"dense_retrieval_ms": 9}]
    assert tasks == [{"bm25_ms": 5}, {"bm25_ms": 8}]
    assert parent.timings_ms == {}


def test_failed_inner_stage_and_sqlstate_survive_outer_generic_error():
    """只保留最初失败阶段与合法SQLSTATE，丢弃任意正文和异常内容。"""
    with capture_query_performance() as trace:
        logs.log_event("bm25.failed", step="57014", error_code="BM25_DATABASE_QUERY_CANCELED")
        logs.log_event("fusion.failed", step="SECRET_PASSWORD", error="SECRET")
        logs.log_event("bm25.index_reused", duration_ms=2)
    assert trace.failure_stage == "bm25" and trace.sqlstate == "57014"
    assert trace.bm25_index_reused is True and "SECRET" not in str(trace.snapshot())


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "SECRET"])
def test_invalid_or_missing_duration_does_not_become_zero(value):
    """非法值不产生观测，避免把缺失阶段伪装成零延迟。"""
    with capture_query_performance() as trace:
        observe_query_event("rerank.finished", {"duration_ms": value})
        observe_query_event("unknown", {"duration_ms": 20})
    assert trace.timings_ms == {}


def test_disk_logging_failure_does_not_drop_already_observed_timing(monkeypatch):
    """磁盘日志故障安全降级，已取得的性能摘要仍然可用。"""
    def fail(*args, **kwargs):
        """模拟输出设施故障，不执行实际磁盘写入。"""
        raise OSError("SECRET_PATH")

    monkeypatch.setattr(logs.LOGGER, "log", fail)
    with capture_query_performance() as trace:
        logs.log_event("llm.call_failed", level=logging.WARNING, duration_ms=12)
    assert trace.timings_ms == {"llm_ms": 12} and trace.failure_stage == "llm"
