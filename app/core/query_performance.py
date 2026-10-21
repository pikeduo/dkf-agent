"""显式请求范围内收集既有安全事件耗时；默认关闭，不保存问题、证据或异常原文。"""

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter


_TRACE = ContextVar("query_performance", default=None)
_EVENT_METRICS = {
    "rag.query_encoded": "query_embedding_ms",
    "dense.finished": "dense_retrieval_ms", "dense.empty": "dense_retrieval_ms",
    "bm25.finished": "bm25_ms", "bm25.empty": "bm25_ms",
    "bm25.snapshot_finished": "bm25_snapshot_ms",
    "bm25.index_built": "bm25_index_ms", "bm25.index_reused": "bm25_index_ms",
    "fusion.finished": "fusion_ms", "fusion.empty": "fusion_ms",
    "rerank.finished": "rerank_ms", "rerank.scored": "rerank_model_ms",
    "rag.synthesized": "response_synthesis_ms", "rag.insufficient": "response_synthesis_ms",
    "llm.call_finished": "llm_ms", "llm.call_failed": "llm_ms",
}
_FAILURE_STAGES = {
    "dense.failed": "dense", "bm25.failed": "bm25", "fusion.failed": "retrieval",
    "rerank.score_failed": "rerank", "rerank.failed": "rerank",
    "llm.call_failed": "llm", "rag.failed": "rag", "knowledge.failed": "service",
    "rag.adapter_failed": "embedding_or_adapter",
}


@dataclass
class QueryPerformance:
    """每次调用独享的有界摘要；只保留固定阶段、数值及布尔缓存观测。"""

    timings_ms: dict = field(default_factory=dict)
    failure_stage: str | None = None
    bm25_index_reused: bool | None = None
    sqlstate: str | None = None
    total_ms: float | None = None

    def observe(self, event, fields):
        """从白名单事件聚合墙钟时间，失败优先保留最内层阶段，不保存任意错误字符串。"""
        metric, duration = _EVENT_METRICS.get(event), fields.get("duration_ms")
        if metric and type(duration) in {int, float} and math.isfinite(duration) and duration >= 0:
            self.timings_ms[metric] = self.timings_ms.get(metric, 0.0) + duration
        if event in {"bm25.index_built", "bm25.index_reused"}:
            self.bm25_index_reused = event == "bm25.index_reused"
        if event in _FAILURE_STAGES and self.failure_stage is None:
            self.failure_stage = _FAILURE_STAGES[event]
            state = fields.get("step")
            if isinstance(state, str) and len(state) == 5 and state.isascii() and state.isalnum():
                self.sqlstate = state

    def snapshot(self):
        """返回独立数值快照；父阶段包含子阶段，不能相加声称总耗时。"""
        return {"timings_ms": dict(self.timings_ms), "total_ms": self.total_ms,
                "failure_stage": self.failure_stage, "sqlstate": self.sqlstate,
                "bm25_index_reused": self.bm25_index_reused}


@contextmanager
def capture_query_performance():
    """隔离线程/异步任务及嵌套调用；异常退出也还原上层上下文并记录本次墙钟。"""
    trace, started = QueryPerformance(), perf_counter()
    token = _TRACE.set(trace)
    try:
        yield trace
    finally:
        trace.total_ms = (perf_counter() - started) * 1000
        _TRACE.reset(token)


def observe_query_event(event, fields):
    """只在显式采集期间处理固定事件；遥测故障不得改变业务或日志结果。"""
    trace = _TRACE.get()
    if trace is not None:
        try:
            trace.observe(event, fields)
        except Exception:
            pass
