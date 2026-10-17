"""阶段33调度/指标/报告Mock；由用户执行，不连接真实服务、GPU或收费API。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import stage33_common as common
from scripts import stage33_runtime as runtime
from scripts import stage33_retrieval as evaluation
from scripts import stage33_reports as reports
from scripts import evaluate_stage33 as cli


def inputs(count=200):
    """提供纯内存文件身份，无PDF/Office正文或真实官方题目。"""
    return {"fingerprint": "mock", "file_issues": {}, "questions": [], "documents": [
        {"file_id": str(uuid4()), "dataset": "ohr" if i < count // 2 else "ocb",
         "file_type": "pdf", "sha256": f"{i:064x}", "file_path": f"mock/{i}.pdf", "page_count": 257}
        for i in range(count)]}


def ready(doc, kb):
    """内存模拟已验证READY视图，不插入数据库或生成向量。"""
    return {"doc_id": str(uuid4()), "kb_id": str(kb), "file_hash": doc["sha256"], "file_type": doc["file_type"],
        "status": "READY", "block_count": 2, "chunk_count": 3, "vector_count": 3, "invalid_vector_count": 0,
        "management": {"state": "done"}, "model_identity": {"mock": True}, "index_identity": "a" * 64,
        "parse_jobs": []}


def test_bounded_scheduler_attempts_all_200_and_resume_never_uploads_ready_again(tmp_path):
    """分批不是缩减：最终全部200份尝试；重复运行沿用同Hash/doc_id、不新增上传。"""
    data, kb, rows, now, calls = inputs(), uuid4(), {}, [0], []

    def reader(kb_id):
        """模拟上一轮任务完成，单轮调度之间只做状态观察。"""
        assert kb_id == kb
        return deepcopy(rows)

    def upload(kb_id, doc):
        """已有业务服务完成模拟，无Parser/队列/收费调用。"""
        calls.append(doc["file_id"])
        rows[doc["sha256"]] = ready(doc, kb)

    def pause(seconds):
        """推进虚拟时间，不阻塞测试。"""
        now[0] += seconds

    def clock():
        """返回确定性虚拟时钟。"""
        return now[0]

    api = SimpleNamespace(upload=upload)
    state = runtime.import_all(data, tmp_path, kb, api, reader=reader, pause=pause, clock=clock,
                               max_inflight=2, timeout=1000, poll_interval=1)
    assert len(calls) == 200 and len(set(calls)) == 200
    assert all(r["execution_status"] == "READY" for r in state.values())
    before = {sha: row["doc_id"] for sha, row in rows.items()}
    runtime.import_all(data, tmp_path, kb, api, reader=reader, pause=pause, clock=clock)
    assert len(calls) == 200 and before == {sha: row["doc_id"] for sha, row in rows.items()}


def test_upload_response_lost_is_adopted_by_hash_not_reposted(tmp_path):
    """确认丢失时只读同库Hash接管，不能盲目再次收费提交。"""
    data, kb, rows, calls, now = inputs(2), uuid4(), {}, [], [0]

    def reader(kb_id):
        """返回真实持久化身份Stub。"""
        return deepcopy(rows)

    def upload(kb_id, doc):
        """模拟服务已保存但HTTP超时；不抛出任何真实上游信息。"""
        calls.append(doc["file_id"])
        rows[doc["sha256"]] = ready(doc, kb)
        raise common.Stage33Error("API_WRITE_UNCONFIRMED")

    def clock():
        """只读虚拟时间。"""
        return now[0]

    def pause(seconds):
        """推进虚拟时间，不请求外部服务。"""
        now[0] += seconds

    state = runtime.import_all(data, tmp_path, kb, SimpleNamespace(upload=upload), reader=reader,
                               pause=pause, clock=clock, timeout=5, poll_interval=1)
    assert len(calls) == 2 and all(r["execution_status"] == "READY" for r in state.values())


def test_uncertain_missing_identity_and_failed_files_stay_in_full_report(tmp_path):
    """没有持久化身份时恢复也不重POST，不删除难题或失败文件。"""
    data, kb, calls = inputs(2), uuid4(), []

    def reader(kb_id):
        """模拟数据库确认找不到文件。"""
        return {}

    def upload(kb_id, doc):
        """模拟不确定传输错误，不接触真实网络。"""
        calls.append(doc["file_id"])
        raise common.Stage33Error("API_WRITE_UNCONFIRMED")

    def pause(seconds):
        """无需真实等待，单轮所有不确定文件已终止调度。"""

    api = SimpleNamespace(upload=upload)
    first = runtime.import_all(data, tmp_path, kb, api, reader=reader, pause=pause)
    second = runtime.import_all(data, tmp_path, kb, api, reader=reader, pause=pause)
    assert len(first) == len(second) == len(calls) == 2
    assert all(r["execution_status"] == "UPLOAD_UNCONFIRMED" for r in second.values())


def question(dataset="ohr", pages=None):
    """仅用于指标逻辑的Mock关联，不冒充官方QA。"""
    return {"dataset": dataset, "qa_id": "mock", "question": "PRIVATE_MOCK_QUERY",
        "source_file_ids": ["file-A"], "evidence_page": {"system_1based": pages or [1]} if dataset == "ohr" else None,
        "expected_assertions": ["PRIVATE_ASSERTION"], "expected_answer": None}


def hit(fid="file-A", page=1):
    """Mock来源行，页码只用于评分，不作为检索输入。"""
    return {"file_id": fid, "source_pages": [page], "page_semantics": "physical_pdf"}


def test_recall_deduplicates_source_units_and_maps_full_physical_pages():
    """200/201页等原页不当作局部页；重复块不得扩大Recall分子。"""
    qa = question(pages=[200, 201])
    result = evaluation.score_units([hit(page=200)] * 10, evaluation.truth(qa)["units"])
    assert result["recall_at_5"] == result["recall_at_10"] == .5
    assert result["mrr_at_20"] == 1
    missing = evaluation.score_units([hit(page=1)], evaluation.truth(qa)["units"])
    assert missing["recall_at_5"] == missing["mrr_at_20"] == 0


def test_ocb_assertions_are_not_relevance_labels_or_plain_answers():
    """OCB正式证据指标不可算；依赖文档代理指标单列，不能伪称答案正确。"""
    qa = question("ocb")
    qa["source_file_ids"].append("file-B")
    assert evaluation.truth(qa) == {"scorable": False, "basis": "no_official_chunk_or_page_relevance",
                                  "units": [], "manual_pending": True}
    proxy = evaluation.score_units([hit()], qa["source_file_ids"], document_only=True)
    assert proxy["recall_at_5"] == .5


def test_failed_cases_remain_in_denominator_and_unexecuted_is_not_a_zero_result():
    """不能靠只统计成功题提高指标；未运行不能编造零分当作实际执行。"""
    measured = evaluation.score_units([hit()], [("file-A", 1)])
    success = {"truth": {"scorable": True, "manual_pending": False}, "strategies": {
        "dense": {"status": "SUCCESS", "metrics": measured, "document_proxy_metrics": measured,
                  "duration_seconds": .1}}}
    failed = deepcopy(success)
    failed["strategies"]["dense"].update(status="FAILED", metrics=None, document_proxy_metrics=None)
    result = evaluation.aggregate([success, failed], "dense")
    assert result["scorable_denominator"] == 2 and result["successfully_scored"] == 1
    assert result["metrics"]["recall_at_5"] == .5 and result["successful_execution_metrics"]["recall_at_5"] == 1
    pending = deepcopy(failed)
    pending["strategies"]["dense"]["status"] = "NOT_EXECUTED"
    assert evaluation.aggregate([success, pending], "dense")["metrics"]["recall_at_5"] is None


def test_four_backends_receive_only_same_original_question(monkeypatch):
    """答案、目标file_id/Chunk/页码不传入模型或策略选择；四路同原问题公平对比。"""
    calls = []

    class Backend:
        """无模型的四路委托Stub。"""

        def retrieve_dense(self, query):
            """只记录问题。"""
            calls.append(("dense", query))
            return [hit()]

        def retrieve_bm25(self, query):
            """只记录同一问题。"""
            calls.append(("bm25", query))
            return [hit()]

        def fuse(self, dense, bm25):
            """只消费既有两路候选。"""
            return dense

        def rerank(self, candidates, query):
            """只消费候选与原问题。"""
            calls.append(("rerank", query))
            return candidates

    def rows(nodes, documents, kb):
        """替代框架转换以验证编排参数，不需要LlamaIndex或模型。"""
        return nodes

    monkeypatch.setattr(evaluation, "hit_rows", rows)
    qa = question()
    result = evaluation.evaluate_question(qa, {}, uuid4(), Backend())
    assert len(result["strategies"]) == 4 and all(v["status"] == "SUCCESS" for v in result["strategies"].values())
    assert calls == [("dense", qa["question"]), ("bm25", qa["question"]), ("rerank", qa["question"])]


def test_incomplete_import_blocks_before_backend_or_gpu(tmp_path):
    """缺一份READY即阻止正式评测，不降到199份，不触发模型工厂。"""
    data, kb = inputs(), uuid4()
    rows = {d["sha256"]: ready(d, kb) for d in data["documents"][:-1]}

    def reader(kb_id):
        """返回不完整范围。"""
        return rows

    def forbidden(kb_id):
        """前置失败不得构造模型。"""
        pytest.fail("不得加载GPU")

    with pytest.raises(common.Stage33Error, match="KB_FILE_SET"):
        evaluation.run_evaluation(data, tmp_path, kb, reader, backend_factory=forbidden)


def test_reports_register_all_4087_without_execution_or_fake_metrics(tmp_path):
    """未执行仍有200文件和4087题状态；正式指标null、没有伪造通过。"""
    data = inputs()
    data["questions"] = [question("ohr" if i < 3588 else "ocb") | {"qa_id": f"mock-{i}"}
                         for i in range(4087)]
    result = reports.generate(data, tmp_path)
    assert result["fixed_document_count"] == 200 and result["fixed_qa_count"] == 4087
    assert result["ready"] == result["import_attempted_or_reused"] == 0
    assert result["status"] == "INCOMPLETE" and result["metrics_status"] == "NOT_CERTIFIED"
    for strategy in common.STRATEGIES:
        assert result["metrics"]["overall"][strategy]["not_executed"] == 4087
        assert result["metrics"]["overall"][strategy]["scorable_denominator"] == 3588
        assert result["metrics"]["ocb"][strategy]["unscorable"] == 499
        assert result["metrics"]["overall"][strategy]["metrics"]["recall_at_5"] is None
    assert sum(1 for _ in (tmp_path / "qa_execution.jsonl").open(encoding="utf-8")) == 4087


def test_formal_cli_has_no_first_n_or_sample_switch():
    """范围不能被count/limit参数缩小，max-inflight只是资源控制。"""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--count", "10"])
    assert parser.parse_args(["--max-inflight", "1"]).max_inflight == 1


def test_evaluation_resume_reuses_only_identical_model_scope_and_qa(tmp_path, monkeypatch):
    """相同QA/索引/模型断点不重复检索；代次变化必须停止，不能混用旧结果。"""
    data, kb, calls = inputs(2), uuid4(), []
    data["questions"] = [question() | {"qa_id": f"mock-{i}", "source_file_ids": [data["documents"][0]["file_id"]]}
                         for i in range(3)]
    rows = {d["sha256"]: ready(d, kb) for d in data["documents"]}

    class Backend:
        """固定身份无模型后端，计数用于断点幂等检查。"""

        def __init__(self, kb_id):
            """记录范围但不访问数据库。"""
            assert kb_id == kb

        def identity(self):
            """匹配内存索引模型身份，不加载BGE。"""
            return {"embedding": {"mock": True}, "reranker": {"mock": True}}

        def retrieve_dense(self, query):
            """计数本地Query调用，不产生文档向量。"""
            calls.append(query)
            return []

        def retrieve_bm25(self, query):
            """空词项结果是正常未命中，不伪造失败或证据。"""
            return []

        def fuse(self, dense, bm25):
            """保留空候选。"""
            return []

        def rerank(self, candidates, query):
            """空候选无需加载模型。"""
            return []

    def reader(kb_id):
        """提供稳定的只读索引快照Stub。"""
        return deepcopy(rows)

    def convert(nodes, documents, kb_id):
        """空结果不调用框架证据转换。"""
        return []

    monkeypatch.setattr(evaluation, "hit_rows", convert)
    evaluation.run_evaluation(data, tmp_path, kb, reader, backend_factory=Backend)
    evaluation.run_evaluation(data, tmp_path, kb, reader, backend_factory=Backend)
    assert len(calls) == 3 and len(list((tmp_path / "cases").glob("*.json"))) == 3
    rows[data["documents"][0]["sha256"]]["index_identity"] = "changed-generation"
    with pytest.raises(common.Stage33Error, match="EVALUATION_IDENTITY_CHANGED"):
        evaluation.run_evaluation(data, tmp_path, kb, reader, backend_factory=Backend)
    assert len(calls) == 3


def test_cross_knowledge_base_and_mixed_models_are_rejected():
    """不能靠结果后过滤掩盖跨库或异模型范围，前置快照即安全停止。"""
    data, kb = inputs(2), uuid4()
    rows = {d["sha256"]: ready(d, kb) for d in data["documents"]}
    rows[data["documents"][0]["sha256"]]["kb_id"] = str(uuid4())
    with pytest.raises(common.Stage33Error, match="TARGET_IDENTITY"):
        runtime.check_scope(data, rows, kb, ready=True)
    rows[data["documents"][0]["sha256"]]["kb_id"] = str(kb)
    rows[data["documents"][0]["sha256"]]["model_identity"] = {"different_model": True}
    with pytest.raises(common.Stage33Error, match="INDEX_MODEL_IDENTITY"):
        runtime.check_scope(data, rows, kb, ready=True)


def test_fee_accounting_deduplicates_batch_data_and_never_guesses_money():
    """重复轮询/历史日志不重复计费；不确定POST单列，真实金额保持null。"""
    unit = {"batch_id": "batch-1", "data_id": "data-1", "submission_attempted": True}
    documents = [{"parse_jobs": [{"job_id": "job-1", "units": [unit, deepcopy(unit),
        {"batch_id": None, "data_id": "data-2", "submission_attempted": True}]}]}]
    result = reports.fee_summary(documents)
    assert result["confirmed_cloud_file_submissions_lifetime"] == 1
    assert result["submission_outcome_unknown"] == 1 and result["actual_cost"] is None


def test_timeout_keeps_all_200_but_does_not_claim_unattempted_files_were_uploaded(tmp_path):
    """运行预算到期保留全范围，资源并发2只提交2份，不能把其余198份计作已导入。"""
    data, kb, now, calls = inputs(), uuid4(), [0], []

    def reader(kb_id):
        """模拟未完成持久化观察，无真实数据库。"""
        return {}

    def upload(kb_id, doc):
        """只记一次在途请求，不声明已经READY。"""
        calls.append(doc["file_id"])

    def clock():
        """返回虚拟时钟。"""
        return now[0]

    def pause(seconds):
        """消耗本地观察窗口，不取消任何后台任务。"""
        now[0] += seconds

    state = runtime.import_all(data, tmp_path, kb, SimpleNamespace(upload=upload), reader=reader,
        clock=clock, pause=pause, max_inflight=2, timeout=1, poll_interval=1)
    assert len(state) == 200 and len(calls) == 2
    assert sum(bool(r.get("upload_intent")) for r in state.values()) == 2
    assert all(r["execution_status"] == "TIMEOUT_PENDING" for r in state.values())


def test_result_export_preserves_original_ids_pages_section_and_source_hash(monkeypatch):
    """评测明细保留原页/来源链，不把第二片201页变成1页，不导出正文。"""
    pytest.importorskip("llama_index.core")
    from app.services.rag.llama_index import node_adapter

    kb, doc, chunk = uuid4(), uuid4(), uuid4()
    span = {"block_id": str(uuid4()), "page": 201, "block_text_hash": "a" * 64,
            "start_char": 0, "end_char": 5, "chunk_start_char": 0, "chunk_end_char": 5}
    value = SimpleNamespace(kb_id=kb, doc_id=doc, chunk_id=chunk, file_name="source.pdf", file_type="pdf",
        text="PRIVATE_BODY", page_start=201, page_end=201, section="section-A", retrieval_score=.5,
        fusion_score=None, rerank_score=None, fusion=None,
        metadata={"source_spans": [span], "page_semantics": "physical_pdf"})

    def convert(hit, **kwargs):
        """提供已通过项目Adapter的证据视图Stub，无模型或数据库。"""
        return value

    monkeypatch.setattr(node_adapter, "node_to_result", convert)
    documents = {str(doc): {"file_id": "file-A", "file_type": "pdf", "sha256": "b" * 64, "page_count": 257}}
    row = evaluation.hit_rows([SimpleNamespace(score=.5)], documents, kb)[0]
    assert row["chunk_id"] == str(chunk) and row["doc_id"] == str(doc)
    assert row["source_pages"] == [201] and row["section"] == "section-A"
    assert row["source_spans"] == [span] and "text" not in row
    with pytest.raises(common.Stage33Error, match="RETRIEVAL_SCOPE_OR_SCORE"):
        evaluation.hit_rows([SimpleNamespace(score=.5)], documents, uuid4())
