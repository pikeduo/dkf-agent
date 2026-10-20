"""当前索引检索评测的纯Mock回归；由用户执行，不调用数据库、GPU、Parser或云端。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import evaluate_stage33 as cli
from scripts import stage33_analysis as analysis
from scripts import stage33_common as common
from scripts import stage33_reports as reports
from scripts import stage33_retrieval as retrieval
from scripts import stage33_runtime as runtime


def indexed_inputs(count=2):
    """构造内存身份及两种QA口径，不写原件或产生文档验收样本。"""
    documents = [{"file_id": f"file-{index}", "dataset": "ohr" if index < count // 2 else "ocb",
        "file_type": "pdf" if index < count // 2 else "docx", "sha256": f"{index + 1:064x}",
        "file_path": f"mock/{index}", "page_count": 20} for index in range(count)]
    return {"fingerprint": "mock-indexed-dataset", "file_issues": {}, "documents": documents,
        "questions": [{"dataset": "ohr", "qa_id": "ohr-mock", "question": "PRIVATE_QUERY_A",
            "source_file_ids": [documents[0]["file_id"]], "evidence_page": {"system_1based": [2]},
            "expected_answer": "NEVER_SEND_ANSWER"},
            {"dataset": "ocb", "qa_id": "ocb-mock", "question": "PRIVATE_QUERY_B",
             "source_file_ids": [documents[-1]["file_id"]], "evidence_page": None,
             "expected_assertions": ["NEVER_SEND_ASSERTION"]}]}


def indexed_rows(data, kb, *, partial_count=1):
    """模拟已结束且来源代次有效的索引视图；PARTIAL保持真实质量字段。"""
    return {doc["sha256"]: {"doc_id": str(UUID(int=index + 100)), "kb_id": str(kb),
        "file_hash": doc["sha256"], "file_type": doc["file_type"],
        "status": "PARTIAL_READY" if index < partial_count else "READY",
        "parse_quality": {"content": "PARTIAL" if index < partial_count else "COMPLETE",
            "warnings": ["MISSING_REQUIRED_PAGES"] if index < partial_count else [],
            "missing_pages": [2] if index < partial_count else []},
        "block_count": 2, "chunk_count": 3, "vector_count": 3, "invalid_vector_count": 0,
        "index_current": True, "index_identity": f"generation-{index}",
        "management": {"state": "done"}, "model_identity": {"mock": True}, "parse_jobs": []}
        for index, doc in enumerate(data["documents"])}


def mock_backend(data, calls):
    """返回只记录原始问题的后端工厂，测试不能接触真实框架或模型。"""
    class Backend:
        """四路共享确定性内存候选，不接收答案、真值页或文档过滤条件。"""

        def __init__(self, kb_id):
            """记录构造次数以验证前置拒绝发生在加载模型之前。"""
            calls.append(("construct", str(kb_id)))

        def identity(self):
            """返回与模拟索引一致的模型身份。"""
            return {"embedding": {"mock": True}, "reranker": {"mock": True}}

        def retrieve_dense(self, question):
            """只消费问题，返回可由事后评分识别的内存来源。"""
            calls.append(("dense", question))
            return [{"file_id": data["documents"][0]["file_id"], "source_pages": [2],
                     "page_semantics": "physical_pdf"}]

        def retrieve_bm25(self, question):
            """记录与Dense相同的原问题，不生成新向量。"""
            calls.append(("bm25", question))
            return []

        def fuse(self, dense, bm25):
            """保留同一Dense候选，不在融合时查看QA答案。"""
            return deepcopy(dense)

        def rerank(self, candidates, question):
            """只接收原候选和原问题，返回同一份来源身份。"""
            calls.append(("rerank", question))
            return deepcopy(candidates)

    return Backend


def run_mock_evaluation(data, rows, kb, folder, monkeypatch, calls):
    """执行被测编排的内存替身；仅当用户运行pytest时写临时评测断点。"""
    def reader(kb_id):
        """返回独立快照副本，避免被测代码修改调用方状态。"""
        assert kb_id == kb
        return deepcopy(rows)

    def convert(nodes, documents, kb_id):
        """绕过框架转换；生产来源校验由已有独立Adapter用例覆盖。"""
        return deepcopy(nodes)

    monkeypatch.setattr(retrieval, "hit_rows", convert)
    retrieval.run_evaluation(data, folder, kb, reader, backend_factory=mock_backend(data, calls),
                             index_policy="indexed")


def test_snapshot_observes_all_200_without_overwriting_old_import_intents(tmp_path):
    """118/82当前视图覆盖报告中的旧FAILED，但原导入意图和检查点字节保持不变。"""
    data, kb = indexed_inputs(200), UUID(int=999)
    rows = indexed_rows(data, kb, partial_count=82)
    old = {doc["file_id"]: {"execution_status": "FAILED", "retry_intent": True,
                           "error_code": "OLD_FAILURE"} for doc in data["documents"]}
    checkpoint = tmp_path / "import_checkpoint.json"
    common.write_json(checkpoint, old)
    before = checkpoint.read_bytes()
    runtime.save_index_snapshot(data, tmp_path, kb, rows)
    report = reports.generate(data, tmp_path)
    assert checkpoint.read_bytes() == before
    assert report["indexed"] == 200 and report["ready"] == 118 and report["partial_ready"] == 82
    assert report["state_source"] == "database_snapshot"
    observed = common.read_json(tmp_path / "import_report.json")["documents"]
    assert all(row.get("error_code") is None for row in observed)
    assert all(row["execution_status"] in {"READY", "PARTIAL_READY"} for row in observed)
    assert report["status"] == "INCOMPLETE" and report["metrics_status"] == "NOT_CERTIFIED"


def test_snapshot_missing_document_does_not_inherit_old_success_or_doc_id(tmp_path):
    """当前库缺失的文档仍登记，不能用旧READY身份和向量填补缺口。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb, partial_count=0)
    old = {doc["file_id"]: rows[doc["sha256"]] | {"execution_status": "READY"}
           for doc in data["documents"]}
    common.write_json(tmp_path / "import_checkpoint.json", old)
    rows.pop(data["documents"][0]["sha256"])
    runtime.save_index_snapshot(data, tmp_path, kb, rows)
    report = reports.generate(data, tmp_path)
    documents = common.read_json(tmp_path / "import_report.json")["documents"]
    assert report["fixed_document_count"] == 2 and report["indexed"] == 1
    assert documents[0]["execution_status"] == "NOT_PRESENT" and "doc_id" not in documents[0]


@pytest.mark.parametrize("damage,code", [("model", "INDEX_MODEL_IDENTITY"),
                                       ("missing", "KB_FILE_SET"),
                                       ("source_file", "FROZEN_FILES_INVALID")])
def test_snapshot_readiness_requires_global_scope_and_model_identity(tmp_path, damage, code):
    """逐份数量正确仍不足以宣称全库就绪；一次观察后保留报告并拒绝模型/集合/原件问题。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    if damage == "model":
        rows[data["documents"][0]["sha256"]]["model_identity"] = {"different": True}
    elif damage == "missing":
        rows.pop(data["documents"][0]["sha256"])
    else:
        data["file_issues"] = {data["documents"][0]["file_id"]: "FILE_HASH"}

    def reader(kb_id):
        """仅读取一次内存快照，不启动API或模型。"""
        calls.append(kb_id)
        return deepcopy(rows)

    with pytest.raises(common.Stage33Error, match=code):
        runtime.refresh_index_snapshot(data, tmp_path, kb, reader=reader)
    assert calls == [kb] and len(common.read_json(tmp_path / "index_snapshot.json")["documents"]) == 2
    assert reports.generate(data, tmp_path, fatal_code=code)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("damage", ["missing", "invalid_vector", "generation", "management", "model", "quality"])
def test_indexed_policy_still_blocks_invalid_scope_before_backend(tmp_path, damage):
    """显式容纳PARTIAL只改变质量政策，不能放宽集合、向量、来源、模型或管理状态。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb)
    target = rows[data["documents"][0]["sha256"]]
    if damage == "missing":
        rows.pop(data["documents"][0]["sha256"])
    elif damage == "invalid_vector":
        target["invalid_vector_count"] = 1
    elif damage == "generation":
        target["index_current"] = False
    elif damage == "management":
        target["management"]["state"] = "running"
    elif damage == "model":
        target["model_identity"] = {"different": True}
    else:
        target["parse_quality"]["content"] = "COMPLETE"

    def reader(kb_id):
        """提供损坏的内存快照，禁止接触真实数据库。"""
        return rows

    def forbidden(kb_id):
        """所有前置错误必须在构造模型之前被识别。"""
        pytest.fail("不得构造GPU后端")

    with pytest.raises(common.Stage33Error):
        retrieval.run_evaluation(data, tmp_path, kb, reader, backend_factory=forbidden, index_policy="indexed")
    assert common.read_json(tmp_path / "evaluation_validity.json")["status"] != "VALID"


def test_complete_policy_keeps_partial_blocked_by_default(tmp_path):
    """默认完整验收不静默接纳PARTIAL，须由用户显式选择当前索引评测政策。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb)

    def reader(kb_id):
        """读取含PARTIAL的内存快照。"""
        return rows

    def forbidden(kb_id):
        """未显式授权质量政策时禁止加载模型。"""
        pytest.fail("不得构造GPU后端")

    with pytest.raises(common.Stage33Error, match="ALL_200_READY_REQUIRED"):
        retrieval.run_evaluation(data, tmp_path, kb, reader, backend_factory=forbidden)


def test_indexed_evaluation_reports_full_denominators_and_quality_limits(tmp_path, monkeypatch):
    """所有题完整执行后可报告当前索引成绩，但不能伪装全READY或OCB证据正确率。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    before = deepcopy(rows)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    report = reports.generate(data, tmp_path)
    assert report["status"] == "COMPLETED_WITH_QUALITY_LIMITS" and not report["full_ready_acceptance"]
    assert report["indexed"] == 2 and report["ready"] == report["partial_ready"] == 1
    assert report["metrics_status"] == "VALID_EXECUTION_RECORDS" and rows == before
    assert report["metrics"]["overall"]["dense"]["total_questions"] == 2
    assert report["metrics"]["overall"]["dense"]["scorable_denominator"] == 1
    assert report["metrics"]["ocb"]["dense"]["metrics"]["recall_at_5"] is None
    assert report["metrics"]["ocb"]["dense"]["document_proxy_metrics"]["recall_at_5"] == 0
    for groups in report["metrics_breakdown"].values():
        assert sum(strategies["dense"]["total_questions"] for strategies in groups.values()) == 2
    assert common.read_json(tmp_path / "evaluation_setup.json")["cloud_calls"] == 0
    assert [(name, query) for name, query in calls if name == "dense"] == [
        ("dense", qa["question"]) for qa in data["questions"]]
    assert "NEVER_SEND" not in repr(calls)
    assert len(common.read_json(tmp_path / "retrieval_diagnostics.json")["counts"]) > 0


@pytest.mark.parametrize("partial_count,expected", [(0, "SAMPLE_COMPLETED"),
                                                  (1, "SAMPLE_COMPLETED_WITH_QUALITY_LIMITS")])
def test_completed_sample_is_not_reported_as_full_qa_acceptance(tmp_path, monkeypatch, partial_count, expected):
    """四路样本全部执行成功仍标抽样，含PARTIAL时额外保留质量限制和原分组。"""
    data, kb, calls = common.select_questions(indexed_inputs(), per_dataset=1), UUID(int=999), []
    rows = indexed_rows(data, kb, partial_count=partial_count)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    report = reports.generate(data, tmp_path)
    assert report["status"] == expected and report["qa_scope"] == "sampled"
    assert report["full_qa_evaluation"] is False and report["indexed"] == 2
    assert report["metrics_status"] == "VALID_EXECUTION_RECORDS"


def test_quality_change_invalidates_resume_before_model_construction(tmp_path, monkeypatch):
    """只改质量缺页也不能复用旧成绩；索引数据与模型相同仍需新run。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    before = list(calls)
    rows[data["documents"][0]["sha256"]]["parse_quality"]["missing_pages"] = [2, 3]
    with pytest.raises(common.Stage33Error, match="EVALUATION_IDENTITY_CHANGED"):
        run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    assert calls == before
    assert reports.generate(data, tmp_path)["metrics_status"] == "NOT_CERTIFIED"


def test_snapshot_refresh_revokes_old_overall_and_group_metric_validity(tmp_path, monkeypatch):
    """只读刷新发现索引代次变化后，各层正式指标均变null，旧命中事实仍保留。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    rows[data["documents"][0]["sha256"]]["index_identity"] = "new-generation"
    runtime.save_index_snapshot(data, tmp_path, kb, rows)
    report = reports.generate(data, tmp_path)
    assert report["metrics_status"] == "NOT_CERTIFIED" and report["status"] == "INCOMPLETE"
    metric = report["metrics"]["overall"]["dense"]
    assert metric["metrics"]["recall_at_5"] is None
    assert metric["successful_execution_metrics"]["recall_at_5"] is None
    assert metric["diagnostic_uncertified_metrics"]["recall_at_5"] == 1
    for groups in report["metrics_breakdown"].values():
        assert all(value["metrics"]["recall_at_5"] is None
                   for strategies in groups.values() for value in strategies.values())


def test_current_runner_failure_cannot_publish_old_valid_scores(tmp_path, monkeypatch):
    """本次快照或启动失败时，即使旧断点VALID也只能保留诊断分数，不能发布正式成绩。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    report = reports.generate(data, tmp_path, fatal_code="DATABASE_SNAPSHOT")
    assert report["status"] == "INCOMPLETE" and report["metrics_status"] == "NOT_CERTIFIED"
    assert report["metrics"]["overall"]["dense"]["metrics"]["recall_at_5"] is None


def test_failed_database_refresh_revokes_validity_for_later_report(tmp_path, monkeypatch):
    """数据库观察失败后即使单独重汇总，也不能把旧快照和成绩显示成本次查询成功。"""
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)

    def unavailable(kb_id):
        """模拟安全数据库错误，不连接真实服务或泄漏连接详情。"""
        raise common.Stage33Error("DATABASE_SNAPSHOT")

    with pytest.raises(common.Stage33Error, match="DATABASE_SNAPSHOT"):
        runtime.refresh_index_snapshot(data, tmp_path, kb, reader=unavailable)
    report = reports.generate(data, tmp_path)
    assert report["evaluation_validity"]["status"] == "SNAPSHOT_FAILED"
    assert report["status"] == "INCOMPLETE" and report["metrics_status"] == "NOT_CERTIFIED"


def test_multi_document_format_group_is_mutually_exclusive_and_preserves_missing_pages():
    """混合格式题只进入一个格式组合；缺页只作事后标记，不删除题目或改变真值。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb)
    sources = {doc["file_id"]: doc | rows[doc["sha256"]] | {"execution_status": rows[doc["sha256"]]["status"]}
               for doc in data["documents"]}
    first = analysis.question_context(data["questions"][0], sources)
    assert first["required_evidence_pages_missing"] == {"file-0": [2]}
    assert first["source_quality"] == "INCLUDES_PARTIAL" and first["file_type_group"] == "pdf"
    qa = deepcopy(data["questions"][1])
    qa["source_file_ids"] = [doc["file_id"] for doc in data["documents"]]
    mixed = analysis.question_context(qa, sources)
    assert mixed["file_type_group"] == "docx+pdf" and mixed["required_evidence_pages_missing"] == {}


@pytest.mark.parametrize("hits,category", [
    ([], "NO_HITS"),
    ([{"file_id": "other", "source_pages": [2], "page_semantics": "physical_pdf"}], "TARGET_DOCUMENT_MISS"),
    ([{"file_id": "file-0", "source_pages": [3], "page_semantics": "physical_pdf"}], "TARGET_PAGE_MISS"),
    ([{"file_id": "file-0", "source_pages": [2], "page_semantics": "physical_pdf"}], "HIT_AT_5"),
])
def test_failure_diagnosis_distinguishes_document_page_and_empty_result(hits, category):
    """来源页诊断不靠Chunk大页范围猜命中，也不把未找到页面误报为云解析失败。"""
    qa = indexed_inputs()["questions"][0]
    basis = retrieval.truth(qa)
    value = {"status": "SUCCESS", "hits": hits, "metrics": retrieval.score_units(hits, basis["units"]),
             "document_proxy_metrics": retrieval.score_units(hits, qa["source_file_ids"], document_only=True)}
    result = {"truth": basis, "strategies": {"dense": value}}
    assert analysis.diagnose_retrieval(result, "dense")["category"] == category


def test_ocb_diagnosis_is_explicitly_document_proxy():
    """OCB依赖文档命中不得写成官方页证据命中或断言正确。"""
    qa = indexed_inputs()["questions"][1]
    hits = [{"file_id": qa["source_file_ids"][0], "source_pages": [], "page_semantics": "converted_office"}]
    result = {"truth": retrieval.truth(qa), "strategies": {"dense": {"status": "SUCCESS", "hits": hits,
        "metrics": None, "document_proxy_metrics": retrieval.score_units(hits, qa["source_file_ids"], document_only=True)}}}
    assert analysis.diagnose_retrieval(result, "dense") == {
        "category": "HIT_AT_5", "basis": "document_dependency_proxy", "error_code": None}


@pytest.mark.parametrize('ranks,category', [
    ({'hybrid_reranker': 5}, 'HIT_AT_5'),
    ({'hybrid_reranker': 6}, 'RERANK_LOW_RANK'),
    ({}, 'BRANCH_RECALL_MISS'),
    ({'hybrid': 20}, 'RERANK_OUTPUT_MISS'),
    ({'dense': 20}, 'BRANCH_HIT_FINAL_MISS_UNRESOLVED'),
    ({'bm25': 1}, 'BRANCH_HIT_FINAL_MISS_UNRESOLVED'),
])
def test_ocb_flow_distinguishes_saved_branch_and_rerank_observations(ranks, category):
    """按四路真实保存的排名定位；缺21～30候选时不得把未知归因为融合或重排丢失。"""
    qa = indexed_inputs()['questions'][1]
    file_id = qa['source_file_ids'][0]
    strategies = {}
    for strategy in common.STRATEGIES:
        rank = ranks.get(strategy)
        hits = [{'file_id': 'other'} for _ in range(20)]
        if rank is not None:
            hits[rank - 1] = {'file_id': file_id}
        strategies[strategy] = {'status': 'SUCCESS', 'hits': hits}
    original = deepcopy(strategies)
    diagnostic, = analysis.diagnose_document_flow(qa, {'strategies': strategies})
    assert diagnostic['category'] == category and diagnostic['source_file_id'] == file_id
    assert diagnostic['basis'] == 'document_dependency_proxy' and diagnostic['observation'] == 'saved_top20_only'
    assert all(diagnostic[name + '_rank'] == ranks.get(name) for name in common.STRATEGIES)
    assert strategies == original and 'PRIVATE_QUERY' not in repr(diagnostic)


@pytest.mark.parametrize('status', ['FAILED', 'NOT_EXECUTED'])
def test_ocb_flow_keeps_failed_or_unexecuted_branches_inconclusive(status):
    """失败/未执行分支不能被当作空召回；多依赖逐项记录，不缩小或冒充QA正确率分母。"""
    qa = deepcopy(indexed_inputs()['questions'][1])
    qa['source_file_ids'] = ['target-a', 'target-b']
    strategies = {name: {'status': 'SUCCESS', 'hits': [{'file_id': 'target-a'}]}
                  for name in common.STRATEGIES}
    strategies['bm25'] = {'status': status, 'hits': []}
    rows = analysis.diagnose_document_flow(qa, {'strategies': strategies})
    assert len(rows) == 2 and all(row['category'] == 'INCOMPLETE_EXECUTION' for row in rows)
    assert rows[0]['dense_rank'] == 1 and rows[1]['dense_rank'] is None
    assert all(row['strategy_statuses']['bm25'] == status for row in rows)
    assert analysis.diagnose_document_flow(indexed_inputs()['questions'][0], {'strategies': strategies}) == []


def test_ocb_flow_report_preserves_cases_and_does_not_change_metrics(tmp_path, monkeypatch):
    """本地报告新增逐依赖JSON/CSV，不执行检索或改写逐题断点，不更改已有指标口径。"""
    import csv

    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    before = {path.name: path.read_bytes() for path in (tmp_path / 'cases').glob('*.json')}
    call_count = len(calls)
    report = reports.generate(data, tmp_path)
    assert report['qa_strategy_records'] == 8 and report['metrics_status'] == 'VALID_EXECUTION_RECORDS'
    diagnostic = common.read_json(tmp_path / 'document_recall_diagnostics.json')
    assert diagnostic['metrics_status'] == report['metrics_status']
    assert sum(diagnostic['counts'].values()) == 1
    assert len(diagnostic['cases']) == 1 and diagnostic['cases'][0]['dataset'] == 'ocb'
    assert diagnostic['cases'][0]['source_file_id'] == data['questions'][1]['source_file_ids'][0]
    with (tmp_path / 'document_recall_diagnostics.csv').open(encoding='utf-8-sig', newline='') as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 1 and csv_rows[0]['basis'] == 'document_dependency_proxy'
    assert 'NEVER_SEND' not in repr(diagnostic) and 'PRIVATE_QUERY' not in repr(diagnostic)
    assert len(calls) == call_count
    assert before == {path.name: path.read_bytes() for path in (tmp_path / 'cases').glob('*.json')}


def test_cli_exposes_readonly_snapshot_and_explicit_index_policy():
    """命令行保留严格默认，同时提供无需云许可的只读快照与显式部分索引评测。"""
    parser = cli.build_parser()
    assert parser.parse_args(["--step", "snapshot"]).index_policy == "complete"
    args = parser.parse_args(["--step", "evaluate", "--index-policy", "indexed"])
    assert args.index_policy == "indexed" and not args.allow_cloud


def test_dependency_error_is_safe_without_importing_model_framework():
    """缺依赖直接返回固定错误，不回显携带密钥/正文的原始异常消息。"""
    assert retrieval.safe_service_error(ImportError("SECRET_PATH")) == "RETRIEVAL_DEPENDENCY"
    assert retrieval.safe_service_error(RuntimeError("SECRET_BODY")) == "RETRIEVAL_SERVICE_ERROR"


def test_database_failure_diagnostic_reaches_case_and_report_without_raw_error(tmp_path, monkeypatch):
    """逐题结果与CSV保留安全SQLSTATE；失败四路不冒充成功，原问题/SQL不进入诊断字段。"""
    import csv
    from app.services.rag.dense import DenseRetrievalError
    from test_retrieval_database import driver_error

    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb)
    calls = []
    base = mock_backend(data, calls)

    class FailedBackend(base):
        """仅模拟Dense查询取消，其他组件仍使用内存替身。"""

        def retrieve_dense(self, question):
            """将首次捕获的SQLSTATE传给既有评测错误处理。"""
            raise DenseRetrievalError('DENSE_DATABASE', database_error=driver_error('57014'))

    def reader(kb_id):
        """返回固定索引快照，不访问真实数据库。"""
        return deepcopy(rows)

    def convert(nodes, documents, kb_id):
        """保留原有内存来源契约，不导入模型框架。"""
        return deepcopy(nodes)

    monkeypatch.setattr(retrieval, 'hit_rows', convert)
    retrieval.run_evaluation(data, tmp_path, kb, reader, backend_factory=FailedBackend, index_policy='indexed')
    qa = data['questions'][0]
    case = common.read_json(tmp_path / 'cases' / (common.digest([qa['dataset'], qa['qa_id']]) + '.json'))
    failure = case['strategies']['dense']
    assert failure['error_code'] == 'DENSE_DATABASE_QUERY_CANCELED'
    assert failure['database_diagnostic'] == {'reason': 'QUERY_CANCELED', 'sqlstate': '57014'}
    assert case['strategies']['hybrid']['error_code'] == 'BRANCH_FAILED'
    assert reports.generate(data, tmp_path)['status'] == 'INCOMPLETE'
    for file in ('qa_execution.csv', 'failures_and_unscorable.csv', 'retrieval_diagnostics.csv'):
        with (tmp_path / file).open(encoding='utf-8-sig', newline='') as handle:
            failures = [row for row in csv.DictReader(handle) if row['error_code'] == 'DENSE_DATABASE_QUERY_CANCELED']
        assert len(failures) == 2
        assert all(row['database_reason'] == 'QUERY_CANCELED' and row['sqlstate'] == '57014' for row in failures)
        assert 'PRIVATE' not in repr(failures)


def test_retry_failed_queries_keeps_successful_cases_byte_identical(tmp_path, monkeypatch):
    """显式重跑仅执行含失败分支的题，成功题断点字节不变且沿用原索引与模型身份。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows, calls = indexed_rows(data, kb), []
    run_mock_evaluation(data, rows, kb, tmp_path, monkeypatch, calls)
    failed_qa, successful_qa = data['questions']
    successful_path = tmp_path / 'cases' / (common.digest([successful_qa['dataset'], successful_qa['qa_id']]) + '.json')
    before = successful_path.read_bytes()
    failed_path = tmp_path / 'cases' / (common.digest([failed_qa['dataset'], failed_qa['qa_id']]) + '.json')
    case = common.read_json(failed_path)
    case['strategies']['dense'].update(status='FAILED', error_code='DENSE_DATABASE', hits=[])
    common.write_json(failed_path, case)
    calls.clear()

    def reader(kb_id):
        """原只读索引身份不变，不调用业务数据库。"""
        return deepcopy(rows)

    retrieval.run_evaluation(data, tmp_path, kb, reader, backend_factory=mock_backend(data, calls),
        index_policy='indexed', retry_failed=True)
    queries = [(stage, question) for stage, question in calls if stage != 'construct']
    assert queries == [(stage, failed_qa['question']) for stage in ('dense', 'bm25', 'rerank')]
    assert successful_path.read_bytes() == before
    assert all(value['status'] == 'SUCCESS' for value in common.read_json(failed_path)['strategies'].values())


def test_late_hit_and_partial_source_coverage_are_distinct_diagnostics():
    """第11名晚命中与多页仅召回一页分别记录，不能都当作完全漏召回。"""
    qa = indexed_inputs()["questions"][0]
    target = {"file_id": "file-0", "source_pages": [2], "page_semantics": "physical_pdf"}
    other = {"file_id": "other", "source_pages": [1], "page_semantics": "physical_pdf"}
    late = [deepcopy(other) for _ in range(10)] + [target]
    basis = retrieval.truth(qa)
    value = {"status": "SUCCESS", "hits": late, "metrics": retrieval.score_units(late, basis["units"]),
        "document_proxy_metrics": retrieval.score_units(late, qa["source_file_ids"], document_only=True)}
    assert analysis.diagnose_retrieval({"truth": basis, "strategies": {"dense": value}}, "dense")[
        "category"] == "LOW_RANK"
    qa["evidence_page"]["system_1based"] = [2, 3]
    basis = retrieval.truth(qa)
    value.update(hits=[target], metrics=retrieval.score_units([target], basis["units"]))
    assert analysis.diagnose_retrieval({"truth": basis, "strategies": {"dense": value}}, "dense")[
        "category"] == "PARTIAL_RECALL"


def test_legacy_ready_quality_is_not_invented_as_complete():
    """历史READY缺少质量元数据时单列分组，不能把空字段当COMPLETE。"""
    data = indexed_inputs()
    source = data["documents"][0] | {"execution_status": "READY", "parse_quality": None}
    context = analysis.question_context(data["questions"][0], {"file-0": source})
    assert context["source_quality"] == "READY_WITHOUT_QUALITY_METADATA"


def test_explicit_import_observation_supersedes_older_index_snapshot(tmp_path):
    """后续显式导入沿用原调度语义，不能继续展示旧只读快照或旧评测VALID。"""
    data, kb = indexed_inputs(), UUID(int=999)
    rows = indexed_rows(data, kb, partial_count=0)
    runtime.save_index_snapshot(data, tmp_path, kb, rows)

    def reader(kb_id):
        """返回已结束的当前索引，不创建任何任务。"""
        return deepcopy(rows)

    def forbidden(*args):
        """已READY的全部文件不得再次上传或重投。"""
        pytest.fail("不得发送写请求")

    runtime.import_all(data, tmp_path, kb, SimpleNamespace(upload=forbidden, retry=forbidden), reader=reader)
    assert common.read_json(tmp_path / "index_snapshot.json")["superseded_by_import"] is True
    report = reports.generate(data, tmp_path)
    assert report["state_source"] == "import_checkpoint" and report["ready"] == 2
    assert report["metrics_status"] == "NOT_CERTIFIED"


@pytest.mark.parametrize("quota", [None, 1])
def test_snapshot_cli_does_not_construct_api_or_models(tmp_path, monkeypatch, capsys, quota):
    """全量和抽样snapshot均只读一次；不需要云许可/API/Worker/模型，抽样清单可保存。"""
    from app.core import business_logging, config
    data, kb, calls = indexed_inputs(), UUID(int=999), []
    rows = indexed_rows(data, kb)

    def load(root):
        """提供内存冻结清单，不读取真实原件。"""
        return data

    def reader(kb_id):
        """只观察一次当前索引，不执行任何外部服务操作。"""
        calls.append("read")
        return deepcopy(rows)

    def refresh(data, run, kb_id):
        """把CLI的快照查询显式替换为纯内存reader。"""
        return runtime.refresh_index_snapshot(data, run, kb_id, reader=reader)

    def forbidden(*args, **kwargs):
        """snapshot不得初始化HTTP客户端、模型或进入评测循环。"""
        pytest.fail("不得构造运行时服务")

    def quiet(*args, **kwargs):
        """关闭测试中的真实日志文件写入，保留命令行摘要用于断言。"""

    def settings():
        """返回不会触发真实配置加载的占位对象。"""
        return SimpleNamespace()

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_dataset", load)
    monkeypatch.setattr(cli, "refresh_index_snapshot", refresh)
    monkeypatch.setattr(cli, "API", forbidden)
    monkeypatch.setattr(cli, "Backends", forbidden)
    monkeypatch.setattr(cli, "run_evaluation", forbidden)
    monkeypatch.setattr(business_logging, "configure_logging", quiet)
    monkeypatch.setattr(business_logging, "log_event", quiet)
    monkeypatch.setattr(config, "get_settings", settings)
    args = ["--step", "snapshot", "--run", "stage33_run_02", "--kb-id", str(kb), "--execute"]
    if quota is not None:
        args += ["--qa-per-dataset", str(quota)]
    assert cli.main(args) == 0
    assert calls == ["read"] and "INDEX_SNAPSHOT_READY" in capsys.readouterr().out
    folder = tmp_path / "results/stage33/stage33_run_02"
    assert not (folder / "import_checkpoint.json").exists()
    assert not (folder / "evaluation_identity.json").exists()
    assert common.read_json(folder / "summary.json")["indexed"] == 2
    assert (folder / "qa_selection.json").exists() == (quota is not None)
