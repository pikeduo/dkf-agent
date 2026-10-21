"""阶段36元数据、调度和报告Mock；由用户运行，不使用文档、DB、模型或云。"""

from threading import Barrier, Lock
from uuid import UUID

import pytest

from app.core.business_logging import log_event
from scripts import evaluate_stage36 as cli
from scripts import stage34_evaluation as source_tool
from scripts import stage36_performance as tool
from scripts.stage33_common import digest, read_json, write_json
from scripts.stage33_runtime import scope_identity


def source_fixture():
    """构造索引元数据与题目标识桩，不创建或解析任何原件/验收样本。"""
    kb = str(UUID(int=1000))
    rows = {f"{i:064x}": {"doc_id": str(UUID(int=i)), "kb_id": kb, "file_hash": f"{i:064x}",
        "status": "PARTIAL_READY" if i % 2 else "READY", "block_count": 1,
        "chunk_count": 2, "vector_count": 2, "invalid_vector_count": 0, "index_identity": f"index-{i}",
        "parse_quality": {"content": "PARTIAL" if i % 2 else "COMPLETE"},
        "model_identity": {"model_name": "memory-stub"}, "index_current": True}
        for i in range(1, 201)}
    return {"kb_id": kb, "index_snapshot": {"kb_id": kb, "documents": rows},
        "source_scope": scope_identity(rows), "dataset_fingerprint": "dataset-stub",
        "bm25_corpus_limits": {"chunks": 100000, "bytes": 268435456, "tokens": 32000000},
        "cases": [{"case_id": digest(i)[:24], "dataset": "ohr", "qa_id": f"q{i}",
            "question": f"PRIVATE_QUESTION_{i}", "purpose": "official_question",
            "doc_ids": [str(UUID(int=200))], "reference_answer": "PRIVATE_ANSWER"} for i in range(4)]}


@pytest.fixture
def plan(monkeypatch):
    """只替换源计划读取，保留真实元数据身份核对和确定性选择。"""
    source = source_fixture()
    monkeypatch.setattr(source_tool, "load_plan", lambda run: source)
    return tool.make_plan("stage34_run_03")


class RuntimeStub:
    """可控检索运行桩，只记录并发、快照及失败，不加载真实组件。"""

    def __init__(self, *, fail=False, interrupt=False, barrier=None, changed_on=None):
        """按需模拟请求失败、中断、并发交错和索引变更。"""
        self.fail, self.interrupt, self.barrier, self.changed_on = fail, interrupt, barrier, changed_on
        self.prepared, self.checked, self.calls, self.scopes = 0, 0, 0, []
        self.active, self.peak, self.lock = 0, 0, Lock()
        self.identity, self.setup_ms = {"model": "stub"}, 5

    def prepare(self, plan):
        """只计数身份准备调用，供模型加载前边界断言。"""
        self.prepared += 1

    def validate_snapshot(self, plan):
        """第指定次观察失败，不读数据库也不修改索引。"""
        self.checked += 1
        if self.checked == self.changed_on:
            raise tool.Stage36Error("INDEX_CHANGED")

    def select_scope(self, plan, scope):
        """记录共享实例的范围切换，不建立BM25语料。"""
        self.scopes.append(scope["documents"])

    def search(self, plan, scope, case):
        """模拟固定阶段事件，并用屏障确保并发请求真正交错。"""
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier:
                self.barrier.wait(timeout=5)
            if self.interrupt:
                raise KeyboardInterrupt
            log_event("dense.finished", duration_ms=3)
            if self.fail:
                log_event("bm25.failed", step="57014")
                raise tool.Stage36Error("REQUEST_FAILED")
            return 0
        finally:
            with self.lock:
                self.active -= 1


def test_three_nested_scales_freeze_full_kb_and_ignore_reference_answers(plan, monkeypatch):
    """10/50/200沿用一个题单和预算，全库用None，不借参考答案筛范围。"""
    assert [s["documents"] for s in plan["scopes"]] == [10, 50, 200]
    assert plan["scopes"][-1]["doc_ids"] is None
    assert set(plan["scopes"][0]["doc_ids"]) < set(plan["scopes"][1]["doc_ids"])
    assert sum(plan["scopes"][-1]["status_counts"].values()) == 200
    assert plan["scopes"][-1]["status_counts"]["PARTIAL_READY"] == 100
    assert "reference_answer" not in str(plan) and "doc_ids" not in plan["cases"][0]
    changed = source_fixture()
    for case in changed["cases"]:
        case.update(reference_answer="OTHER", doc_ids=[str(UUID(int=1))])
    monkeypatch.setattr(source_tool, "load_plan", lambda run: changed)
    assert tool.make_plan("stage34_run_03")["scopes"] == plan["scopes"]
    with pytest.raises(tool.Stage36Error, match="SCOPE_LIMIT"):
        tool.make_plan("stage34_run_03", sizes=[150])


def test_plan_hash_and_runtime_changes_cannot_mix_old_results(tmp_path, plan):
    """计划或实际运行身份变化都拒绝续写，旧结果原样保留。"""
    write_json(tmp_path / "plan.json", {"sha256": digest(plan), "plan": plan})
    assert tool.load_plan(tmp_path) == plan
    envelope = read_json(tmp_path / "plan.json")
    envelope["plan"]["repeats"] += 1
    write_json(tmp_path / "plan.json", envelope)
    with pytest.raises(tool.Stage36Error, match="PLAN_CHANGED"):
        tool.load_plan(tmp_path)
    tool.execute(tmp_path, plan, RuntimeStub())
    path = tool.group_paths(tmp_path, plan)[0][2]
    before = path.read_bytes()
    runtime = RuntimeStub()
    runtime.identity = {"model": "changed"}
    with pytest.raises(tool.Stage36Error, match="RUNTIME_CHANGED"):
        tool.execute(tmp_path, plan, runtime, resume=True)
    assert path.read_bytes() == before and runtime.calls == 0


def test_bounded_threads_collect_every_row_and_reuse_each_scope(tmp_path, plan):
    """双并发同进程执行全部测量，逐请求独立，结果不含问题或参考答案。"""
    plan.update(concurrency=[2], warmup=0, repeats=2)
    runtime = RuntimeStub(barrier=Barrier(2))
    tool.execute(tmp_path, plan, runtime, limit_groups=3)
    assert runtime.peak == 2 and runtime.calls == 18 and runtime.scopes == [10, 50, 200]
    for _, _, path in tool.group_paths(tmp_path, plan):
        value = read_json(path)
        assert value["status"] == "COMPLETED" and len(value["rows"]) == 6
        assert all(row["timings_ms"] == {"dense_retrieval_ms": 3} for row in value["rows"])
        assert "PRIVATE" not in str(value)


def test_failure_stops_current_round_and_resume_does_not_retry_failed_group(tmp_path, plan):
    """预热失败停止，续跑跳过旧失败但只执行未开始组，不洗白历史失败。"""
    failed = RuntimeStub(fail=True)
    tool.execute(tmp_path, plan, failed, limit_groups=6)
    path = tool.group_paths(tmp_path, plan)[0][2]
    before = path.read_bytes()
    assert read_json(path)["status"] == "WARMUP_FAILED" and failed.calls == 1
    continued = RuntimeStub()
    tool.execute(tmp_path, plan, continued, resume=True, limit_groups=6)
    assert path.read_bytes() == before and continued.calls == 35
    assert continued.scopes == [10, 50, 200]


def test_measurement_failures_keep_full_attempt_denominator_and_safe_stage(tmp_path, plan, monkeypatch):
    """测量组保留成功与失败分母；真实失败阶段和SQLSTATE不被外层通用码覆盖。"""
    plan.update(warmup=0, concurrency=[1])
    monkeypatch.setattr(tool, "historical_rag_timings", lambda plan: {})
    tool.execute(tmp_path, plan, RuntimeStub(fail=True), limit_groups=3)
    result = tool.report(tmp_path, plan)
    group = result["groups"][0]
    assert group["status"] == "FAILED" and group["planned"] == group["attempted"] == group["failed"] == 6
    assert group["all_attempt_latency_ms"]["count"] == 6 and group["success_latency_ms"]["count"] == 0
    assert group["failure_stages"] == {"bm25": 6}
    rows = read_json(tool.group_paths(tmp_path, plan)[0][2])["rows"]
    assert all(row["sqlstate"] == "57014" and "PRIVATE" not in str(row) for row in rows)
    assert result["groups"][1]["status"] == "NOT_RUN"


def test_interrupt_preserves_started_group_and_complete_resume_loads_no_models(tmp_path, plan):
    """中断组不重发；全部批次已有记录时仅报告，不加载模型或重查数据库。"""
    plan["scopes"], plan["concurrency"] = plan["scopes"][:1], [1]
    with pytest.raises(KeyboardInterrupt):
        tool.execute(tmp_path, plan, RuntimeStub(interrupt=True))
    path = tool.group_paths(tmp_path, plan)[0][2]
    before = path.read_bytes()
    assert read_json(path)["status"] == "STARTED"
    runtime = RuntimeStub()
    tool.execute(tmp_path, plan, runtime, resume=True)
    assert runtime.prepared == runtime.checked == runtime.calls == 0 and path.read_bytes() == before


@pytest.mark.parametrize("changed_on", [1, 2, 3])
def test_invalid_index_is_saved_and_cannot_be_cleared_by_resume(tmp_path, plan, changed_on):
    """启动、批后、下批前观察失败均保留失效状态，后续成功观察不能覆盖。"""
    runtime = RuntimeStub(changed_on=changed_on)
    if changed_on == 2:
        tool.execute(tmp_path, plan, runtime, limit_groups=2)
    else:
        with pytest.raises(tool.Stage36Error, match="INDEX_CHANGED"):
            tool.execute(tmp_path, plan, runtime, limit_groups=2)
    before = (tmp_path / "validity.json").read_bytes()
    assert read_json(tmp_path / "validity.json")["status"] == "CHANGED_OR_UNAVAILABLE"
    fresh = RuntimeStub()
    with pytest.raises(tool.Stage36Error, match="PREVIOUS_INDEX_INVALID"):
        tool.execute(tmp_path, plan, fresh, resume=True)
    assert fresh.prepared == fresh.checked == fresh.calls == 0
    assert (tmp_path / "validity.json").read_bytes() == before


def test_reports_keep_warmup_errors_planned_denominator_and_missing_stages(tmp_path, plan, monkeypatch):
    """失败/未测保留在分母；缺失阶段不报零耗时，预热失败可在摘要定位。"""
    monkeypatch.setattr(tool, "historical_rag_timings", lambda plan: {"historical_only": True})
    tool.execute(tmp_path, plan, RuntimeStub(fail=True))
    result = tool.report(tmp_path, plan)
    group = result["groups"][0]
    assert result["status"] == "INCOMPLETE_OR_INVALID" and result["new_cloud_calls"] == 0
    assert group["planned"] == 6 and group["attempted"] == group["successful"] == 0
    assert group["success_latency_ms"]["p95"] is None and group["stage_timings_ms"] == {}
    assert group["failure_stages"] == {"bm25": 1}
    assert group["error_codes"] == {"STAGE36_REQUEST_FAILED": 1}
    assert len(result["groups"]) == 6 and "PRIVATE" not in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_historical_llm_duration_is_separate_and_never_constructs_live_runtime(tmp_path, plan, monkeypatch):
    """历史主备调用耗时求和，旧失败不消失，不生成任何新LLM请求。"""
    source = source_fixture()
    monkeypatch.setattr(source_tool, "run_directory", lambda name: tmp_path)
    monkeypatch.setattr(source_tool, "load_plan", lambda run: source)
    write_json(tmp_path / "cases/first.json", {"plan_sha256": plan["source_plan_sha256"],
        "status": "COMPLETED", "timings_seconds": {"synthesis": 4, "total": 7},
        "llm_attempts": [{"duration_seconds": 1}, {"duration_seconds": 2}]})
    write_json(tmp_path / "cases/second.json", {"plan_sha256": plan["source_plan_sha256"], "status": "FAILED"})
    result = tool.historical_rag_timings(plan)
    assert result["historical_only"] is True and result["new_cloud_calls"] == 0
    assert result["status_counts"] == {"COMPLETED": 1, "FAILED": 1}
    assert result["timings_ms"]["llm"]["mean"] == 3000
    assert result["timings_ms"]["retrieval"]["count"] == 0


def test_preview_never_constructs_runtime_or_writes_files(tmp_path, plan, monkeypatch, capsys):
    """未指定execute只显示冻结预算，不连服务、不加载模型或创建运行目录。"""
    def forbidden(*args, **kwargs):
        """预览若触发运行组件应直接失败。"""
        pytest.fail("预览不能构造运行组件")

    monkeypatch.setattr(cli, "run_directory", lambda name: tmp_path / "not-created")
    monkeypatch.setattr(cli, "make_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(tool, "Runtime", forbidden)
    assert cli.main([]) == 0 and list(tmp_path.iterdir()) == []
    assert "DRY_RUN" in capsys.readouterr().out


def test_nearest_rank_distribution_discards_invalid_values():
    """最近秩小样本分位数与空值口径稳定，非法数字不参加统计。"""
    result = tool.distribution([1, 2, 3, 4, None, True, -1, float("inf")])
    assert result == {"count": 4, "mean": 2.5, "p50": 2, "p95": 4, "max": 4}
    assert tool.distribution([])["p50"] is None
