"""阶段34用户执行的纯Mock回归：不运行真实模型、文档解析、数据库或收费API。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import evaluate_stage34 as cli
from scripts import stage34_evaluation as tool
from scripts.stage33_common import digest, read_json, write_json
from scripts.stage34_runtime import MeteredLLM, TimedComponent


def fixture_plan(count=3, *, fallback=False):
    """只创建内存题单及明确请求策略，不生成原件、解析结果或真实验收样本。"""
    return {"version": tool.VERSION, "kb_id": str(UUID(int=1)), "source_scope": "scope",
        "policy": {"fallback": fallback, "llm_calls_per_case_max": 2 if fallback else 1},
        "index_snapshot": {"documents": {"f": {"status": "PARTIAL_READY", "parse_quality": {"content": "PARTIAL"}}}},
        "cases": [{"case_id": digest([i])[:24], "question": "PRIVATE_QUESTION", "scope": "single",
            "doc_ids": [str(UUID(int=2))], "dataset": "ohr", "expected_behavior": "answerable",
            "reference_basis": "official_answer", "purpose": "official_question"} for i in range(count)]}


class RuntimeStub:
    """内存运行桩只模拟付费意图边界，不构造生产QueryEngine或HTTP客户端。"""

    def __init__(self, *, fail=False, interrupt=False, refuse=False):
        """允许分别模拟已发请求失败、中断、正常拒答。"""
        self.fail, self.interrupt, self.refuse = fail, interrupt, refuse
        self.calls, self.stage, self.timings, self.final_evidence = [], "input", {}, []
        self.raw_output, self.snapshot_checks = None, 0

    def validate_snapshot(self, plan):
        """仅计数，不访问数据库。"""
        self.snapshot_checks += 1

    def identity(self):
        """提供固定内存身份，不加载模型或读取密钥。"""
        return {"provider": "custom", "fallback": False}

    def cloud_policy(self):
        """模拟关闭备用的固定策略，供模型加载前核对。"""
        return {"fallback": False, "llm_calls_per_case_max": 1}

    def query(self, kb_id, question, *, doc_ids, before_cloud):
        """范围原样传入；请求前保存具体提供方意图，响应后更新内存状态。"""
        self.calls.append((kb_id, question, doc_ids))
        self.stage = "llm"
        attempt = before_cloud(provider="custom", model="custom-model")
        if self.interrupt:
            raise KeyboardInterrupt
        if self.fail:
            from app.services.llm import LLMError
            attempt.update(status="FAILED", error_code="LLM_NETWORK")
            raise LLMError("LLM_NETWORK", provider="custom")
        attempt["status"] = "COMPLETED"
        return {"answer": {"status": "insufficient_evidence" if self.refuse else "success",
            "insufficient_evidence": self.refuse, "answer": "PRIVATE_ANSWER", "citations": []},
            "final_evidence": [], "source_check": {"status": "NOT_APPLICABLE" if self.refuse else "VERIFIED"},
            "timings_seconds": {"retrieval": .1, "rerank": .2, "synthesis": .3}}


def test_checkpoint_precedes_request_and_resume_only_runs_unstarted_cases(tmp_path):
    """进程中断后保留STARTED并保守占预算；resume只执行尚未开始的下一题。"""
    plan = fixture_plan(2)
    with pytest.raises(KeyboardInterrupt):
        tool.execute(tmp_path, plan, RuntimeStub(interrupt=True), max_cloud_calls=2, limit=1)
    first = read_json(tool.case_path(tmp_path, plan["cases"][0]))
    assert first["status"] == "STARTED" and first["llm_attempted"] is True
    runtime = RuntimeStub()
    records = tool.execute(tmp_path, plan, runtime, max_cloud_calls=2, limit=2)
    assert len(runtime.calls) == 1 and len(records) == 2
    assert records[plan["cases"][0]["case_id"]] == first
    assert runtime.calls[0][1:] == ("PRIVATE_QUESTION", plan["cases"][1]["doc_ids"])
    assert "reference_answer" not in runtime.calls[0]


def test_budget_rejection_precedes_models_and_query(tmp_path):
    """不足以覆盖新题最坏请求数时，不加载身份模型也不执行检索。"""
    runtime = RuntimeStub()
    with pytest.raises(tool.Stage34Error, match="CLOUD_BUDGET"):
        tool.execute(tmp_path, fixture_plan(), runtime, max_cloud_calls=1, limit=2)
    assert runtime.calls == [] and runtime.snapshot_checks == 0
    assert not (tmp_path / "runtime.json").exists()


def test_duplicate_provider_request_rejected_before_second_intent(tmp_path):
    """即使调用方错误地重复主请求，也不得绕过单题上界或追加第二条意图。"""
    class DuplicateRuntime(RuntimeStub):
        """模拟同一题在成功后再次调用主提供方的错误编排。"""

        def query(self, kb_id, question, *, doc_ids, before_cloud):
            """先完成正常一次，再错误地申请重复请求；后者必须在HTTP前拒绝。"""
            super().query(kb_id, question, doc_ids=doc_ids, before_cloud=before_cloud)
            before_cloud(provider="custom", model="custom-model")

    records = tool.execute(tmp_path, fixture_plan(2), DuplicateRuntime(), max_cloud_calls=2, limit=2)
    record = next(iter(records.values()))
    assert len(records) == len(record["llm_attempts"]) == 1
    assert record["error_code"] == "STAGE34_DUPLICATE_CLOUD_CALL"
    assert record["failure_stage"] == "llm" and record["status"] == "FAILED"


def test_paid_network_failure_saved_and_not_retried(tmp_path, capsys):
    """超时可能已收费：失败分类可追踪，本轮停止，后续续跑不重复该题。"""
    plan = fixture_plan()
    runtime = RuntimeStub(fail=True)
    records = tool.execute(tmp_path, plan, runtime, max_cloud_calls=3, limit=3)
    assert len(runtime.calls) == len(records) == 1
    value = next(iter(records.values()))
    assert value["error_code"] == "LLM_NETWORK" and value["failure_stage"] == "llm"
    assert value["llm_attempted"] is True
    runtime = RuntimeStub()
    tool.execute(tmp_path, plan, runtime, max_cloud_calls=3, limit=3)
    assert len(runtime.calls) == 2
    assert "PRIVATE" not in capsys.readouterr().out


def test_plan_or_runtime_change_cannot_mix_existing_answers(tmp_path):
    """题目变更及运行模型身份变更均拒绝复用，保留已有结果。"""
    plan = fixture_plan(2)
    tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=2, limit=1)
    changed = deepcopy(plan)
    changed["cases"][0]["question"] = "CHANGED"
    with pytest.raises(tool.Stage34Error, match="RESULT_IDENTITY"):
        tool.load_records(tmp_path, changed)
    write_json(tmp_path / "runtime.json", {"changed": True})
    with pytest.raises(tool.Stage34Error, match="RUNTIME_CHANGED"):
        tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=2, limit=1)


def test_post_snapshot_failure_preserves_paid_answer_and_stops(tmp_path):
    """后置索引变化保留本次已付费回答，标明原安全原因，不继续收费或重问。"""
    class ChangedRuntime(RuntimeStub):
        """只在第三次快照检查模拟并发文档维护。"""

        def validate_snapshot(self, plan):
            """首轮和题前检查通过，题后检查失败。"""
            self.snapshot_checks += 1
            if self.snapshot_checks == 3:
                raise tool.Stage34Error("INDEX_CHANGED")

    runtime = ChangedRuntime()
    records = tool.execute(tmp_path, fixture_plan(2), runtime, max_cloud_calls=2, limit=2)
    assert len(runtime.calls) == len(records) == 1
    record = next(iter(records.values()))
    assert record["status"] == "COMPLETED" and record["answer"]["answer"] == "PRIVATE_ANSWER"
    assert record["index_check"] == "CHANGED_OR_UNAVAILABLE"
    assert record["index_error_code"] == "STAGE34_INDEX_CHANGED"


def test_unreviewed_answers_do_not_get_automatic_quality_scores(tmp_path):
    """结构成功、来源核对成功和PARTIAL_READY均不能自动变成语义满分。"""
    plan = fixture_plan(1)
    records = tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=1, limit=1)
    review = tool.make_review(plan["cases"][0], next(iter(records.values())))
    result = tool.summarize(plan, records, [review])
    assert result["completed"] == 1 and result["partial_ready_documents"] == 1
    assert result["status"] == "MANUAL_REVIEW_PENDING"
    assert all(m["rate"] is None for m in result["manual_metrics"].values())
    assert result["source_verified_answers"] == 1
    assert result["manual_pending_cases"]["answer_accuracy"] == 1


def test_refusal_counts_for_answer_accuracy_but_not_faithfulness(tmp_path):
    """合理拒答可人工判对，但空事实或空引用不抬高忠实度和引用准确率。"""
    plan = fixture_plan(1)
    records = tool.execute(tmp_path, plan, RuntimeStub(refuse=True), max_cloud_calls=1, limit=1)
    record = next(iter(records.values()))
    review = tool.make_review(plan["cases"][0], record)
    review.update(reviewer="人工", notes="核对受限范围，无法回答", expected_behavior="unanswerable")
    review["answer_accuracy"] = {"passed": 1, "total": 1}
    result = tool.summarize(plan, records, [review])
    assert result["refusals"] == 1 and result["manual_metrics"]["answer_accuracy"]["rate"] == 1
    assert result["manual_metrics"]["faithfulness"]["rate"] is None
    review["faithfulness"] = {"passed": 1, "total": 1}
    with pytest.raises(tool.Stage34Error, match="REFUSAL_NOT_FAITHFULNESS_SAMPLE"):
        tool.summarize(plan, records, [review])


@pytest.mark.parametrize("passed,total", [(True, 1), (1, 0), (2, 1), (-1, 1), (.5, 1)])
def test_manual_scores_reject_invalid_denominators(tmp_path, passed, total):
    """人工分母必须为正整数且分子合法，禁止布尔值、比例或伪造满分。"""
    plan = fixture_plan(1)
    records = tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=1, limit=1)
    review = tool.make_review(plan["cases"][0], next(iter(records.values())))
    review.update(reviewer="人工", notes="对照原文")
    review["faithfulness"] = {"passed": passed, "total": total}
    with pytest.raises(tool.Stage34Error, match="REVIEW_SCORE"):
        tool.summarize(plan, records, [review])


def test_review_staleness_and_partial_review_stay_visible(tmp_path):
    """评分绑定原答案Hash；只评一题不能将全run标成已评完。"""
    plan = fixture_plan(2)
    records = tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=2, limit=2)
    review = tool.make_review(plan["cases"][0], records[plan["cases"][0]["case_id"]])
    review.update(reviewer="人工", notes="原文逐项核对")
    for name in tool.METRICS:
        review[name] = {"passed": 1, "total": 1}
    result = tool.summarize(plan, records, [review])
    assert result["status"] == "MANUAL_REVIEW_PENDING"
    assert result["manual_pending_cases"] == dict.fromkeys(tool.METRICS, 1)
    review["result_sha256"] = "stale"
    with pytest.raises(tool.Stage34Error, match="REVIEW_IDENTITY"):
        tool.summarize(plan, records, [review])


def test_metered_service_persists_intent_before_one_original_call():
    """独立提供方代理不改Prompt；先写意图，再调用原服务一次并记录结果。"""
    calls = []

    def complete(system, user):
        """记录原参数，返回内存响应。"""
        calls.append((system, user))
        return "PRIVATE_RESPONSE"

    def before_cloud(*, provider, model):
        """用调用顺序模拟指定提供方的原子意图回执。"""
        assert calls == []
        assert (provider, model) == ("custom", "custom-model")
        calls.append("intent")
        return {"status": "STARTED"}

    runtime = RuntimeStub()
    proxy = MeteredLLM(SimpleNamespace(provider="custom", model_name="custom-model", complete=complete),
                       runtime, before_cloud)
    assert proxy.complete("system", "user") == "PRIVATE_RESPONSE"
    assert calls == ["intent", ("system", "user")] and runtime.raw_output == "PRIVATE_RESPONSE"


def test_checkpoint_failure_prevents_network_call():
    """保存请求意图失败时，服务不得先发请求再补检查点。"""
    calls = []

    def before_cloud(**metadata):
        """模拟本地磁盘写入失败。"""
        raise OSError("PRIVATE_PATH")

    proxy = MeteredLLM(SimpleNamespace(provider="custom", model_name="custom-model",
        complete=lambda *args: calls.append(args)), RuntimeStub(), before_cloud)
    with pytest.raises(OSError):
        proxy.complete("system", "user")
    assert calls == []


def test_timing_proxy_preserves_scope_and_does_not_retrieve_twice():
    """计时透明传递属性/参数；不使用第二次检索代替同次证据。"""
    calls = []
    component = SimpleNamespace(kb_id="kb", retrieve=lambda q: calls.append(q) or ["node"])
    runtime = RuntimeStub()
    proxy = TimedComponent(component, runtime, "retrieval")
    assert proxy.kb_id == "kb" and proxy.retrieve("PRIVATE") == ["node"]
    assert calls == ["PRIVATE"] and runtime.stage == "retrieval" and runtime.timings["retrieval"] >= 0


def test_cli_requires_cloud_permission_and_budget_before_runtime(tmp_path, monkeypatch, capsys):
    """收费许可和预算在读取模型或建立运行服务之前拒绝。"""
    monkeypatch.setattr(cli, "run_directory", lambda name: tmp_path)
    assert cli.main(["--step", "evaluate", "--execute"]) == 1
    assert "CLOUD_PERMISSION_REQUIRED" in capsys.readouterr().out
    assert cli.main(["--step", "evaluate", "--execute", "--allow-cloud"]) == 1
    assert "CLOUD_BUDGET" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_report_preserves_manual_reviews_and_does_not_call_runtime(tmp_path):
    """离线重新生成报告仅补新题评审项，旧人工评分及其Hash绑定保持。"""
    plan = fixture_plan(2)
    tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=2, limit=1)
    tool.report(tmp_path, plan)
    reviews = read_json(tmp_path / "reviews.json")
    reviews[0].update(reviewer="人工", notes="比较标准答案及同次引用")
    reviews[0]["answer_accuracy"] = {"passed": 0, "total": 1}
    write_json(tmp_path / "reviews.json", reviews)
    tool.execute(tmp_path, plan, RuntimeStub(), max_cloud_calls=2, limit=1)
    result = tool.report(tmp_path, plan)
    updated = read_json(tmp_path / "reviews.json")
    assert len(updated) == 2 and updated[0] == reviews[0]
    assert result["manual_metrics"]["answer_accuracy"]["rate"] == 0
    assert "PRIVATE_QUESTION" in (tmp_path / "review_packet.md").read_text(encoding="utf-8")


def test_legacy_one_request_record_still_reports_without_rewriting_it(tmp_path):
    """旧Boolean单请求记录可离线汇总；缺实际提供方保持未知，不改原结果或评分身份。"""
    plan = fixture_plan(1)
    record = next(iter(tool.execute(tmp_path, plan, RuntimeStub(fail=True), max_cloud_calls=1, limit=1).values()))
    for name in ("llm_attempts", "actual_provider", "actual_model"):
        record.pop(name)
    path = tool.case_path(tmp_path, plan["cases"][0])
    write_json(path, record)
    before = path.read_bytes()
    result = tool.report(tmp_path, plan)
    assert result["llm_attempts_recorded"] == result["llm_budget_reserved"] == 1
    assert result["llm_requests_by_provider"] == result["llm_response_cases_by_provider"] == {}
    assert path.read_bytes() == before


def test_plan_selection_does_not_read_retrieval_results(tmp_path, monkeypatch):
    """固定选题不依赖成绩或主备开关；只冻结调用策略，不读取密钥或联网。"""
    from scripts import stage33_common, stage33_runtime
    documents = [{"file_id": f"f{i}", "sha256": f"sha{i}"} for i in range(14)]
    questions = [{"dataset": "ohr", "qa_id": f"q{i}", "question": f"问题{i}",
        "source_file_ids": [f"f{i}"], "expected_answer": f"答案{i}"} for i in range(12)]
    questions += [{"dataset": "ocb", "qa_id": "ocb1", "question": "比较两份文档", "source_file_ids": ["f12", "f13"],
        "expected_assertions": [{"official": "人工解释"}]}]
    selection = {"per_dataset": 50, "seed": 33}
    data = {"fingerprint": "fingerprint", "documents": documents, "questions": questions, "qa_selection": selection}
    rows = {d["file_id"]: {"doc_id": str(UUID(int=i + 1)), "file_hash": d["sha256"], "status": "READY"}
            for i, d in enumerate(documents)}
    files = {"run.json": {"kb_id": str(UUID(int=100)), "dataset_fingerprint": "fingerprint"},
        "qa_selection.json": selection, "index_snapshot.json": {"kb_id": str(UUID(int=100)),
        "dataset_fingerprint": "fingerprint", "scope": "scope", "documents": rows},
        "evaluation_identity.json": {"backend": {"bm25_corpus_limits": {"chunks": 100000, "bytes": 268435456, "tokens": 32000000}}}}
    reads = []

    def read(path):
        """仅允许读取已有白名单元数据，读取策略成绩会直接失败。"""
        reads.append(path.name)
        return deepcopy(files[path.name])

    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "read_json", read)
    monkeypatch.setattr(stage33_common, "load_dataset", lambda **kwargs: deepcopy(data))
    monkeypatch.setattr(stage33_common, "select_questions", lambda data, **kwargs: data)
    monkeypatch.setattr(stage33_runtime, "check_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(stage33_runtime, "scope_identity", lambda rows: "scope")
    config = SimpleNamespace(llm_fallback_enabled=False)
    plan = tool.make_plan("stage33_run_03", office_count=1, settings=config)
    singles = [c for c in plan["cases"] if c["scope"] == "single" and c["purpose"] == "official_question"]
    assert len(singles) == 10 and len({c["doc_ids"][0] for c in singles}) == 10
    assert sum(c["scope"] == "multi" for c in plan["cases"]) == 1
    assert sum(c["scope"] == "knowledge_base" for c in plan["cases"]) == 3
    controls = [c for c in plan["cases"] if c["purpose"] == "scope_exclusion_control"]
    assert len(controls) == 2 and all(c["expected_behavior"] == "unknown" for c in controls)
    assert all(c["reference_answer"] is None and c["reference_assertions"] is None for c in controls)
    for c in controls:
        assert all(rows[fid]["doc_id"] not in c["doc_ids"] for fid in c["source_file_ids"])
    questions.reverse()
    assert tool.make_plan("stage33_run_03", office_count=1, settings=config) == plan
    config.llm_fallback_enabled = True
    enabled = tool.make_plan("stage33_run_03", office_count=1, settings=config)
    assert enabled["cases"] == plan["cases"]
    assert enabled["policy"]["fallback"] is True and enabled["policy"]["llm_calls_per_case_max"] == 2
    assert tool.plan_id(enabled) != tool.plan_id(plan)
    assert all("retrieval" not in name for name in reads)
