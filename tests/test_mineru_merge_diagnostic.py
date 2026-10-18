"""只读诊断工具Mock：不发HTTP、不连接数据库、不解析PDF或写业务缓存。"""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from scripts import mineru_merge_diagnostic as tool
from app.core.config import Settings
from app.services.mineru import pdf_split as split
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import canonical, recipe
from mineru_samples import result_zip
from test_mineru_merge import merged_case


@pytest.mark.parametrize("outcome", ["pass", "adapter_fail", "guard_fail"])
def test_whole_diagnostic_uses_archived_receipt_and_does_not_mutate_job(context, monkeypatch, outcome):
    """Whole复用原归档；结构增强失败保留可靠基础内容，独立安全Guard拒绝仍显式失败。"""
    document, job, settings, _ = context
    from app.services.mineru import jobs, raw_results
    from app.parsers.routing.contracts import PageSignals, RouteDecision, StructuralSignals
    from app.parsers.routing import guard
    options = jobs.parse_options(settings)
    job.parse_options = options | {"_cache_recipe": recipe(document, settings, "mineru_whole"),
        "_cache_guard": RouteDecision(parse_mode="mineru", file_type="pdf", route_strategy="mineru_whole",
            reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals(page_count=2,
                pages=tuple(PageSignals(page=page, text_chars=10, image_count=0, image_area_ratio=0,
                    drawing_count=0, annotation_count=0, required=True) for page in (1, 2))
                if outcome == "guard_fail" else ())).model_dump(mode="json")}
    job.options_hash = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    job.submission_attempted = True
    values, layout, _ = merged_case()
    if outcome == "adapter_fail":
        layout["pdf_info"][0]["para_blocks"][0]["lines"][1]["spans"][0]["content"] = "无法匹配的源文字"
    archive = result_zip(values, layout=layout)
    monkeypatch.setattr(raw_results, "load_raw_result", lambda *args, **kw: (archive, {
        "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive)}))
    def guard_stub(*args):
        """模拟独立完整性拒绝，不改变Adapter或连接真实服务。"""
        if outcome == "guard_fail":
            raise guard.CompletenessError("缺少必需页面")

    monkeypatch.setattr(guard, "guard_result", guard_stub)
    snapshot = deepcopy(vars(job)), deepcopy(vars(document))
    report = tool.inspect_job(document, job, settings, job.batch_id, None)
    assert report["status"] == ("FAIL" if outcome == "guard_fail" else "ADAPTER_PASS_NOT_RECOVERED")
    assert report["adapter_normalize"] == "PASS" and report["block_count"] == 2
    assert report["completeness_guard"] == ("FAIL" if outcome == "guard_fail" else "PASS")
    if outcome == "adapter_fail":
        assert "MOVED_TEXT" in report["parse_quality"]["warnings"]
        assert report["parse_quality"]["structure"] == "DEGRADED"
    if outcome == "guard_fail":
        assert report["missing_required_original_pages"] == [2]
        assert report["missing_required_page_signals"] == [{"page": 2, "text_chars": 10,
            "image_count": 0, "drawing_count": 0, "annotation_count": 0}]
    assert report["zip_batch_provenance"] == "validated_local_receipt"
    assert not any(report[name] for name in ("database_modified", "cache_published", "cloud_submitted"))
    assert (vars(job), vars(document)) == snapshot


@pytest.fixture
def context(tmp_path, monkeypatch):
    """构造有Hash的字节哨兵与真实字段账本，geometry/Guard仅由Stub替代且不声称原件通过。"""
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    doc_id, job_id, task_id = uuid4(), uuid4(), uuid4()
    original = tmp_path / "original.pdf"
    original.write_bytes(b"mock-original-not-a-pdf")
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    part_hash = hashlib.sha256(b"mock-part-not-a-pdf").hexdigest()
    part_id = uuid5(job_id, f"{split.VERSION}:1:1:2:{part_hash}")
    part = split.PDFPart(ordinal=1, page_start=1, page_end=2, file_hash=part_hash, size_bytes=19,
        data_id=f"dkf-{part_id.hex}", task_id=part_id, batch_id="original-batch", state="failed",
        submission_attempted=True, upload_complete=True, error_code="MERGE_EVIDENCE")
    manifest = split.SplitManifest(doc_id=doc_id, file_hash=original_hash, page_count=2,
                                  policy=split.policy(), parts=[part])
    document = SimpleNamespace(doc_id=doc_id, file_type="pdf", file_hash=original_hash,
        file_path=str(original), file_name="私有文件名.pdf", status="FAILED", processing_task_id=task_id)
    from app.services.mineru.jobs import parse_options
    options = parse_options(settings)
    job = SimpleNamespace(doc_id=doc_id, job_id=job_id, task_id=task_id, file_hash=original_hash,
        provider="mineru_cloud", state="failed", batch_id=part.batch_id, data_id=part.data_id,
        model_version=options["model_version"],
        options_hash=hashlib.sha256(canonical(options | {"pdf_split": split.policy()})).hexdigest(),
        upload_complete=True, parse_options=options | {"_pdf_split": manifest.model_dump(mode="json"),
            "_cache_recipe": recipe(document, settings, "mineru_whole", pdf_split_policy=split.policy())})
    path = split.part_path(tool.ParseCache(settings), doc_id, job_id, 1)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mock-part-not-a-pdf")
    monkeypatch.setattr(tool, "page_geometry", lambda *args: {0: (600, 800), 1: (600, 800)})
    from app.services.mineru import pdf_split_jobs
    monkeypatch.setattr(pdf_split_jobs, "validate_piece", lambda *args: None)
    return document, job, settings, path


def test_missing_zip_is_blocked_and_does_not_revive_failed_job(context):
    """缺ZIP只输出已核对的身份，不根据云done伪造Cache或复活终态。"""
    document, job, settings, _ = context
    snapshot = deepcopy(vars(document)), deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", None)
    assert report["status"] == "BLOCKED" and report["error_code"] == "ZIP_REQUIRED"
    assert report["source_hashes_verified"] and not report["cache_published"]
    assert (vars(document), vars(job)) == snapshot


def test_completed_split_batch_can_be_diagnosed_without_a_pending_cursor(context, monkeypatch):
    """所有片done后仍按指定Batch检查本地ZIP，不再将正常空游标误判身份错误。"""
    from app.services.mineru import raw_results
    document, job, settings, _ = context
    manifest = split.checked_manifest(document, job)
    manifest.parts[0].state, manifest.parts[0].result = "done", {}
    job.parse_options = job.parse_options | {"_pdf_split": manifest.model_dump(mode="json")}
    job.state, document.status = "done", "READY"
    archive = result_zip([{"type": "text", "page_idx": 0, "text": "原始可靠正文"}])
    monkeypatch.setattr(raw_results, "load_raw_result", lambda *args, **kw: (archive, {
        "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive)}))
    snapshot = deepcopy(vars(job)), deepcopy(vars(document))
    report = tool.inspect_job(document, job, settings, job.batch_id, None)
    assert report["adapter_normalize"] == "PASS" and report["all_parts_done"]
    assert report["completed_part_count"] == 1 and report["unsubmitted_part_count"] == 0
    assert report["document_status"] == "READY" and not report["database_modified"]
    assert (vars(job), vars(document)) == snapshot


@pytest.mark.parametrize("damage,check", [("batch", "batch_unique"), ("generation", "generation"),
                                         ("data", "job_data"), ("upload", "part_uploaded")])
def test_identity_failure_names_fixed_check_without_exposing_private_values(context, damage, check):
    """真正的身份错误仍拒绝，并准确报告失败条件而非统一黑盒错误。"""
    document, job, settings, _ = context
    batch = job.batch_id
    if damage == "batch":
        batch = "PRIVATE_WRONG_BATCH"
    elif damage == "generation":
        document.processing_task_id = uuid4()
    elif damage == "data":
        job.data_id = "PRIVATE_WRONG_DATA"
    else:
        job.parse_options["_pdf_split"]["parts"][0]["upload_complete"] = False
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, batch, None)
    assert error.value.code == "DIAGNOSTIC_IDENTITY"
    assert check in error.value.diagnostic["failed_checks"]
    assert "PRIVATE" not in json.dumps(error.value.diagnostic)


def test_existing_zip_reproduces_safe_subreason_without_exporting_text_or_bbox(context, tmp_path):
    """来源多Span按原序保留基础内容并报告增强拒绝；不导出原文、坐标或私有路径。"""
    document, job, settings, _ = context
    values, layout, _ = merged_case(originals=("PRIVATE_BODY", "PRIVATE_BODY_2"))
    layout["pdf_info"][0]["preproc_blocks"][1]["lines"][0]["spans"].append(
        deepcopy(layout["pdf_info"][0]["preproc_blocks"][1]["lines"][0]["spans"][0]))
    archive = tmp_path / "input.zip"
    archive.write_bytes(result_zip(values, layout=layout))
    snapshot = deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", archive)
    assert report["status"] == "ADAPTER_PASS_NOT_RECOVERED" and report["error_code"] is None
    # 相同Span可合法多次出现；本夹具只增加来源项，故在来源/目标序列核对时失败。
    diagnostic = report["enhancement_diagnostic"]
    assert diagnostic["reason"] == "TARGET_SPAN_COUNT"
    assert diagnostic["stage"] == "adapter.restore_merged_text"
    assert (diagnostic["observed"], diagnostic["expected"]) == (2, 3)
    assert diagnostic["page_idx"] == 0
    assert report["parse_quality"]["structure"] == "DEGRADED"
    assert "TARGET_SPAN_COUNT" in report["parse_quality"]["warnings"]
    assert report["zip_size_bytes"] == archive.stat().st_size
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in ("PRIVATE_BODY", "bbox", str(tmp_path), document.file_name))
    assert vars(job) == snapshot and not list(settings.parsed_dir.rglob("ready.json"))


def test_validated_zip_is_not_a_recovery_or_batch_provenance_proof(context, tmp_path):
    """离线通过也只标ADAPTER_PASS，必须保留人工下载来源核对，不发布结果或推进下一片。"""
    document, job, settings, _ = context
    values, layout, _ = merged_case()
    archive = tmp_path / "input.zip"
    archive.write_bytes(result_zip(values, layout=layout))
    snapshot = deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", archive)
    assert report["status"] == "ADAPTER_PASS_NOT_RECOVERED" and report["block_count"] == 2
    assert report["zip_batch_provenance"] == "manual_confirmation_required"
    assert report["original_pages"] == [1] and not report["database_modified"] and not report["cloud_submitted"]
    assert vars(job) == snapshot


@pytest.mark.parametrize("conflict", [False, True])
def test_split_diagnostic_reads_local_receipt_without_zip_argument(context, monkeypatch, tmp_path, conflict):
    """分片也自动读取已有归档；显式另一ZIP不匹配时拒绝，始终不请求云端或修改Job。"""
    document, job, settings, _ = context
    from app.services.mineru import raw_results
    values, layout, _ = merged_case()
    archive = result_zip(values, layout=layout)
    monkeypatch.setattr(raw_results, "load_raw_result", lambda *args, **kwargs: (archive, {
        "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive)}))
    snapshot = deepcopy(vars(job)), deepcopy(vars(document))
    if conflict:
        wrong = tmp_path / "unrelated.zip"
        wrong.write_bytes(b"not-the-original-zip")
        with pytest.raises(MinerUError) as error:
            tool.inspect_job(document, job, settings, "original-batch", wrong)
        assert error.value.code == "DIAGNOSTIC_IDENTITY"
    else:
        report = tool.inspect_job(document, job, settings, "original-batch", None)
        assert report["status"] == "ADAPTER_PASS_NOT_RECOVERED" and report["block_count"] == 2
        assert report["zip_batch_provenance"] == "validated_local_receipt"
        assert not report["cloud_submitted"] and not report["database_modified"] and not report["cache_published"]
    assert (vars(job), vars(document)) == snapshot


@pytest.mark.parametrize("change,code", [("original", "PDF_SPLIT_CHANGED"), ("piece", "PDF_SPLIT_CHANGED"),
    ("batch", "DIAGNOSTIC_IDENTITY"), ("generation", "DIAGNOSTIC_IDENTITY"),
    ("normalizer", "CACHE_CONFIG_CHANGED"), ("part_identity", "PDF_SPLIT_CHECKPOINT"),
    ("options", "DIAGNOSTIC_OPTIONS")])
def test_wrong_source_or_frozen_identity_stops_before_adapter(context, monkeypatch, change, code):
    """核对原Hash、片Hash、Batch、代次、缓存版本与稳定片ID，不能借离线诊断放宽恢复。"""
    document, job, settings, path = context

    def forbidden(*args, **kwargs):
        """任何错误输入进入Adapter均说明验证顺序有误。"""
        pytest.fail("身份校验之前不应处理ZIP")

    monkeypatch.setattr(tool.MinerUResultAdapter, "normalize", forbidden)
    if change == "original":
        document.file_path = path
    elif change == "piece":
        path.write_bytes(b"broken")
    elif change == "batch":
        job.batch_id = "another-batch"
    elif change == "generation":
        document.processing_task_id = uuid4()
    elif change == "normalizer":
        job.parse_options["_cache_recipe"]["normalizer_version"] = "old-version"
    elif change == "options":
        job.parse_options["_cache_recipe"]["language"] = "changed"
    else:
        job.parse_options["_pdf_split"]["parts"][0]["data_id"] = "changed"
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, "original-batch", path)
    assert error.value.code == code


def test_report_is_exclusive_and_io_errors_do_not_leak_or_overwrite(tmp_path, monkeypatch, capsys):
    """同名输出拒绝，驱动/路径异常仅固定安全码，不输出异常原文。"""
    monkeypatch.setattr(tool, "ROOT", tmp_path)

    def unavailable(*args, **kwargs):
        """模拟带敏感URL的驱动错误，不连接数据库。"""
        raise RuntimeError("PRIVATE_TOKEN postgresql://PRIVATE_URL")

    monkeypatch.setattr(tool, "inspect_database", unavailable)
    from app.core import business_logging
    monkeypatch.setattr(business_logging, "configure_logging", lambda *args, **kwargs: None)
    args = ["--job-id", str(uuid4()), "--batch-id", "original-batch", "--output", "results/report.json"]
    assert tool.main(args) == 2
    target = tmp_path / "results/report.json"
    before = target.read_bytes()
    assert json.loads(before)["error_code"] == "DIAGNOSTIC_IO_OR_DB"
    assert tool.main(args) == 2 and target.read_bytes() == before
    assert "PRIVATE" not in capsys.readouterr().out and "PRIVATE" not in before.decode()


def test_old_paid_normalizer_requires_explicit_diagnostic_upgrade_without_changing_job(context):
    """v2冻结Job默认拒绝；显式v4离线比较使用新身份，不修改旧参数或发布缓存。"""
    document, job, settings, _ = context
    job.parse_options["_cache_recipe"]["normalizer_version"] = "mineru-adapter-v2"
    snapshot = deepcopy(vars(job))
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, "original-batch", None)
    assert error.value.code == "CACHE_CONFIG_CHANGED"
    report = tool.inspect_job(document, job, settings, "original-batch", None, allow_normalizer_upgrade=True)
    assert report["normalizer_upgrade_explicit"] and report["normalizer_version"] == "mineru-adapter-v4"
    assert report["frozen_normalizer_version"] == "mineru-adapter-v2"
    assert report["adapter_identity"] != report["frozen_adapter_identity"]
    assert vars(job) == snapshot and not report["cache_published"]


def test_unknown_old_normalizer_cannot_use_upgrade_switch(context):
    """诊断升级许可仅覆盖明确v2/v3到v4，不能绕过任意历史版本或缺失身份。"""
    document, job, settings, _ = context
    job.parse_options["_cache_recipe"]["normalizer_version"] = "mineru-adapter-v1"
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, "original-batch", None, allow_normalizer_upgrade=True)
    assert error.value.code == "CACHE_CONFIG_CHANGED"


def test_production_worker_cannot_silently_upgrade_a_paid_v2_job(context, monkeypatch):
    """生产短任务遇旧付费版本在任何Provider调用前停止，不继承诊断升级权限。"""
    from app.services.mineru import jobs
    document, job, settings, _ = context
    job.parse_options["_cache_recipe"]["normalizer_version"] = "mineru-adapter-v2"
    snapshot = deepcopy(vars(job))
    monkeypatch.setattr(jobs, "restore_cached_result", lambda *args: None)
    monkeypatch.setattr(jobs, "lock_job", lambda *args: (document, job))

    class ForbiddenProvider:
        """不允许产生任何云调用，连原Batch查询也不得在版本检查前发生。"""

        def query(self, *args):
            """一旦触发查询即失败，不发送真实请求。"""
            pytest.fail("旧冻结版本不得自动调用Provider")

    with pytest.raises(MinerUError) as error:
        jobs.check_step(object(), job.job_id, ForbiddenProvider(), settings)
    assert error.value.code == "CACHE_CONFIG_CHANGED" and vars(job) == snapshot


def test_diagnostic_page_and_item_log_fields_only_accept_bounded_integers():
    """新增定位字段不能借字符串、布尔或超限数字输出正文/伪造页码。"""
    from app.core.business_logging import safe_fields
    assert safe_fields({"page": 200, "item_index": 1000}) == {"page": 200, "item_index": 1000}
    for value in (True, -1, 0, 1.5, 10**9, "PRIVATE_BODY"):
        assert safe_fields({"page": value, "item_index": value}) == {}


def test_database_inspection_enforces_read_only_and_rollback_on_error(monkeypatch):
    """仅构造SQL并以Stub记录只读边界，禁止行锁/提交/flush或数据库写入。"""
    from app.db import session as database
    from sqlalchemy import orm
    calls = []

    class ReadOnlySession:
        """只能执行只读查询的测试会话，不建立连接。"""

        def __init__(self, *args, **kwargs):
            """校验禁用autoflush，避免诊断隐式修改记录。"""
            assert kwargs == {"autoflush": False}

        def __enter__(self):
            """返回内存会话。"""
            return self

        def __exit__(self, *args):
            """不隐式提交事务。"""

        def execute(self, statement):
            """记录只读SQL；不存在Job时返回空结果。"""
            calls.append(str(statement))
            return SimpleNamespace(one_or_none=lambda: None)

        def rollback(self):
            """无论异常类型如何均退出只读事务。"""
            calls.append("ROLLBACK")

    monkeypatch.setattr(database, "get_engine", lambda: object())
    monkeypatch.setattr(orm, "Session", ReadOnlySession)
    with pytest.raises(MinerUError) as error:
        tool.inspect_database(uuid4(), object(), "batch", None)
    assert error.value.code == "DIAGNOSTIC_JOB"
    assert calls[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert calls[-1] == "ROLLBACK" and "SELECT" in calls[-2]
    assert all(not any(word in statement.upper() for word in ("FOR UPDATE", "INSERT ", "DELETE ", "COMMIT"))
               for statement in calls)


def coverage_context(context, monkeypatch):
    """在原诊断上下文增加两个物理页摘要；新预检只给元数据Stub，不打开PDF。"""
    from app.parsers.routing import inspector
    from app.parsers.routing.contracts import Inspection, StructuralSignals
    from app.parsers.routing.router import decide_route

    document, job, settings, path = context
    rows = [dict(page=1, text_chars=20, image_count=0, image_area_ratio=0,
                 drawing_count=0, annotation_count=0, required=True),
            dict(page=2, text_chars=0, image_count=0, image_area_ratio=0,
                 drawing_count=4, annotation_count=0, required=True)]
    frozen = decide_route("pdf", "mineru", Inspection(signals=StructuralSignals(page_count=2, pages=rows)))
    job.parse_options["_route_decision"] = frozen.model_dump(mode="json")
    # 用缺省版本字段的旧JSON模拟真实冻结数据，禁止因为新增默认值就改写它。
    job.parse_options["_route_decision"]["signals"].pop("pdf_coverage_version")
    for row in job.parse_options["_route_decision"]["signals"]["pages"]:
        row.pop("coverage_reason")
        row.pop("decorative_drawing_count")
    proposed = deepcopy(rows)
    proposed[0].update(coverage_reason="content_or_uncertain")
    proposed[1].update(required=False, coverage_reason="margin_rule_only", decorative_drawing_count=4)
    inspected = Inspection(signals=StructuralSignals(page_count=2, pages=proposed,
                                                    pdf_coverage_version="pdf-coverage-v2"))

    def inspect_stub(*args):
        """仅返回已验证的结构摘要，不读取或生成文档。"""
        return inspected

    monkeypatch.setattr(inspector, "inspect_pdf", inspect_stub)
    return inspected


def test_coverage_comparison_requires_flag_and_preserves_frozen_failure(context, tmp_path, monkeypatch):
    """冻结缺页记部分覆盖；显式比较仅改内存覆盖摘要，不改块/旧身份或发布缓存。"""
    from app.services.mineru import pdf_split_jobs
    from app.parsers.routing.contracts import RouteDecision
    from app.parsers.routing.guard import guard_result, CompletenessError
    coverage_context(context, monkeypatch)
    document, job, settings, _ = context
    values, layout, _ = merged_case()
    archive = tmp_path / "input.zip"
    archive.write_bytes(result_zip(values, layout=layout))
    calls = []

    def real_guard(doc, task, part, parsed):
        """记录相同正文/ID并调用真实Guard；此测试片从原页1开始，不需额外平移。"""
        calls.append(parsed.model_dump_json())
        try:
            guard_result(parsed, RouteDecision.model_validate(task.parse_options["_route_decision"]))
        except CompletenessError:
            raise MinerUError("COMPLETENESS", "缺少必需页面") from None

    monkeypatch.setattr(pdf_split_jobs, "validate_piece", real_guard)
    before = deepcopy(vars(job)), deepcopy(vars(document))
    default = tool.inspect_job(document, job, settings, "original-batch", archive)
    assert default["status"] == "ADAPTER_PARTIAL_NOT_RECOVERED" and default["error_code"] is None
    assert default["missing_required_original_pages"] == [2]
    report = tool.inspect_job(document, job, settings, "original-batch", archive, allow_coverage_upgrade=True)
    assert report["status"] == "ADAPTER_PASS_NOT_RECOVERED"
    assert report["frozen_completeness_guard"] == "PARTIAL" and report["completeness_guard"] == "PASS"
    assert report["guard_evaluation_scope"] == "proposed_coverage_only"
    assert report["frozen_missing_required_original_pages"] == [2]
    assert report["missing_required_original_pages"] == []
    assert report["coverage_change_pages"] == [{"page": 2, "frozen_required": True,
        "proposed_required": False, "reason": "margin_rule_only", "drawing_count": 4,
        "decorative_drawing_count": 4}]
    assert report["normalizer_version"] == "mineru-adapter-v4"
    assert not report["normalizer_changed_by_coverage_comparison"]
    assert report["frozen_adapter_identity"] == report["adapter_identity"]
    assert all(value == calls[0] for value in calls)
    assert (vars(job), vars(document)) == before
    assert not report["database_modified"] and not report["cloud_submitted"] and not report["cache_published"]
    assert not list(settings.parsed_dir.rglob("ready.json"))


@pytest.mark.parametrize("change", ["missing_page", "count", "page_count", "uncertain",
                                   "required_page", "conflicting_guard", "new_frozen_version"])
def test_coverage_upgrade_rejects_ambiguous_or_unexplained_change(context, monkeypatch, change):
    """来源范围/计数或冻结版本不一致、非装饰覆盖变更均拒绝，不用新预检替换未知旧快照。"""
    from app.parsers.routing import inspector
    inspected = coverage_context(context, monkeypatch)
    document, job, settings, _ = context
    if change == "missing_page":
        signals = inspected.signals.model_copy(update={"pages": inspected.signals.pages[:1]})
        inspected = inspected.model_copy(update={"signals": signals})
    elif change in {"count", "required_page"}:
        first = inspected.signals.pages[0].model_copy(update={"text_chars": 19} if change == "count"
            else {"required": False, "coverage_reason": "empty"})
        inspected = inspected.model_copy(update={"signals": inspected.signals.model_copy(
            update={"pages": (first, inspected.signals.pages[1])})})
    elif change == "page_count":
        inspected = inspected.model_copy(update={"signals": inspected.signals.model_copy(update={"page_count": 3})})
    elif change == "uncertain":
        inspected = inspected.model_copy(update={"uncertain": True})
    elif change == "conflicting_guard":
        job.parse_options["_cache_guard"] = deepcopy(job.parse_options["_route_decision"])
        job.parse_options["_cache_guard"]["parse_mode"] = "auto"
    else:
        job.parse_options["_route_decision"]["signals"] = inspected.signals.model_dump(mode="json")

    def inspect_stub(*args):
        """向比较入口提供故障摘要，不操作真实原件。"""
        return inspected

    monkeypatch.setattr(inspector, "inspect_pdf", inspect_stub)
    before = deepcopy(vars(job))
    manifest = split.checked_manifest(document, job)
    with pytest.raises(MinerUError) as error:
        tool.compare_pdf_coverage(document, job, settings, manifest)
    assert error.value.code == "DIAGNOSTIC_COVERAGE" and vars(job) == before


def test_coverage_upgrade_does_not_authorize_normalizer_upgrade(context, tmp_path, monkeypatch):
    """覆盖许可与Normalizer许可独立，旧v2没有单独许可仍在读取ZIP之前停止。"""
    coverage_context(context, monkeypatch)
    document, job, settings, path = context
    job.parse_options["_cache_recipe"]["normalizer_version"] = "mineru-adapter-v2"
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, "original-batch", path, allow_coverage_upgrade=True)
    assert error.value.code == "CACHE_CONFIG_CHANGED"


def test_comparison_rechecks_source_hash_after_inspection(context, monkeypatch):
    """只读预检前后都核对原件Hash，避免期间原件变化仍解除页面覆盖要求。"""
    from app.parsers.routing import inspector
    inspected = coverage_context(context, monkeypatch)
    document, job, settings, _ = context

    def mutate_source(*args):
        """仅破坏测试临时字节哨兵，模拟其他进程并发替换原件。"""
        from pathlib import Path
        Path(document.file_path).write_bytes(b"changed-source")
        return inspected

    monkeypatch.setattr(inspector, "inspect_pdf", mutate_source)
    with pytest.raises(MinerUError) as error:
        tool.compare_pdf_coverage(document, job, settings, split.checked_manifest(document, job))
    assert error.value.code == "PDF_SPLIT_CHANGED"


def test_cli_passes_two_independent_permissions_without_running_business(tmp_path, monkeypatch):
    """CLI精确传递两个许可；本用例只记录参数及生成临时摘要，不运行预检/Normalizer/数据库。"""
    from app.core import business_logging, config
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    calls = []

    def settings_stub():
        """拒绝读取用户.env，返回无凭据的内存配置。"""
        return Settings(_env_file=None)

    def logging_stub(*args, **kwargs):
        """不建立真实日志文件。"""

    def database_stub(*args, **kwargs):
        """记录许可但不连接数据库，缺ZIP状态不能伪装为验收通过。"""
        calls.append(kwargs)
        return {"status": "BLOCKED", "error_code": "ZIP_REQUIRED"}

    monkeypatch.setattr(config, "get_settings", settings_stub)
    monkeypatch.setattr(business_logging, "configure_logging", logging_stub)
    monkeypatch.setattr(tool, "inspect_database", database_stub)
    assert tool.main(["--job-id", str(uuid4()), "--batch-id", "original-batch", "--output", "results/report.json",
                      "--allow-normalizer-upgrade", "--allow-coverage-upgrade"]) == 2
    assert calls == [{"allow_normalizer_upgrade": True, "allow_coverage_upgrade": True}]
