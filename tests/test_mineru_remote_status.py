"""原Batch错误定位与安全持久化回归；仅内存Mock，由用户手动执行。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.services import document_management as management
from app.services.mineru import jobs
from app.services.mineru.errors import MinerUError
from scripts import mineru_remote_status as tool
from test_mineru_recovery import context


@pytest.mark.parametrize("payload,expected", [
    ({"err_code": -60003}, "-60003"),
    ({"error_code": "-60007"}, "-60007"),
    ({"err_msg": "解析失败"}, "-60010"),
    ({"err_code": "PRIVATE_TOKEN", "err_msg": "PRIVATE_BODY"}, "REMOTE_FAILED"),
    ({"err_code": True, "err_msg": {"private": "PRIVATE_BODY"}}, "REMOTE_FAILED"),
])
def test_remote_failure_preserves_known_code_without_exporting_raw_message(payload, expected):
    """只保留已有白名单代码或固定原因；未知供应商原文、布尔及对象不进入错误摘要。"""
    error = jobs.result_failure(payload)
    assert error.code == expected and "PRIVATE" not in str(error)


def test_terminal_remote_failure_keeps_cause_and_batch_without_retry(context, monkeypatch):
    """远端临时分类也必须终止原任务；安全原因经失败事务到管理层，绝不新POST或自动重试。"""
    settings, document, job, session = context
    job.parse_options = {"_route_decision": {"existing": True}}
    provider = SimpleNamespace(query=MagicMock(return_value={"state": "failed", "trace_id": "trace-test",
                              "err_code": -60007, "err_msg": "PRIVATE_TOKEN PRIVATE_BODY"}))
    monkeypatch.setattr(jobs, "check_normalizer_recipe", lambda *args: None)
    with pytest.raises(MinerUError) as caught:
        jobs.check_step(session, job.job_id, provider, settings)
    assert caught.value.code == "ROUTED_REMOTE_FAILED" and not caught.value.retryable
    provider.query.assert_called_once_with(job.batch_id, job.data_id)
    assert not jobs.record_error(session, job.job_id, caught.value, settings)
    assert job.parse_options["_remote_failure"] == {
        "cause_code": "-60007", "batch_id": job.batch_id, "data_id": job.data_id, "trace_id": "trace-test"}
    assert job.error_code == "ROUTED_REMOTE_FAILED" and job.is_active is False
    assert document.error_message.startswith("-60007：") and "PRIVATE" not in document.error_message
    document.management_task_id = uuid4()
    monkeypatch.setattr(management, "config_for", lambda doc: {"state": "running", "operation": "retry"})
    monkeypatch.setattr(session, "scalar", lambda statement: document)
    management.mark_failed(session, document.doc_id, document.management_task_id, "MANAGEMENT_STAGE_FAILED")
    assert document.management_config["cause_code"] == "-60007"


@pytest.mark.parametrize("code", ["PRIVATE_TOKEN", "https://private.invalid/?secret=PRIVATE", "-999999"])
def test_management_still_rejects_unknown_remote_cause(code):
    """允许受控云错误码不等于允许任意供应商错误文本或未知数字码。"""
    assert management.safe_cause_code(code) is None


def test_remote_diagnostic_is_local_by_default_and_query_never_publishes(context):
    """只读核对默认零云调用；显式查询只用原身份，过滤URL/原文且不改变Job/文档。"""
    _, document, job, _ = context
    snapshot = deepcopy(job.parse_options), document.status, job.state, job.batch_id, job.task_id
    assert tool.inspect_job(document, job)["cloud_queries"] == 0
    query = MagicMock(return_value={"state": "failed", "trace_id": "trace-test", "err_code": -60003,
                                   "err_msg": "PRIVATE_BODY", "full_zip_url": "https://PRIVATE.invalid/signed"})
    report = tool.inspect_job(document, job, query=query)
    query.assert_called_once_with(job.batch_id, job.data_id)
    assert report["cause_code"] == "-60003" and report["remote_state"] == "failed"
    assert not any(report[key] for key in ("cloud_submitted", "database_modified", "result_downloaded",
                                          "cache_published", "business_recovered"))
    assert "PRIVATE" not in repr(report)
    assert snapshot == (job.parse_options, document.status, job.state, job.batch_id, job.task_id)


@pytest.mark.parametrize("payload,status,field_status", [
    ({}, "MESSAGE_MISSING", "MISSING"),
    ({"err_msg": None}, "MESSAGE_NULL", "NULL"),
    ({"err_msg": ""}, "MESSAGE_EMPTY", "EMPTY"),
    ({"err_msg": " \t\r\n"}, "MESSAGE_EMPTY", "EMPTY"),
    ({"err_msg": False}, "MESSAGE_INVALID_TYPE", "INVALID_TYPE"),
    ({"err_msg": ["PRIVATE_BODY"]}, "MESSAGE_INVALID_TYPE", "INVALID_TYPE"),
    ({"err_msg": {"detail": "PRIVATE_BODY"}}, "MESSAGE_INVALID_TYPE", "INVALID_TYPE"),
    ({"err_msg": "PRIVATE_BODY"}, "MESSAGE_UNRECOGNIZED", "PRESENT"),
])
def test_remote_diagnostic_distinguishes_absent_empty_invalid_and_unrecognized_reason(payload, status, field_status):
    """不再把无原因与本地未识别混为一谈；非法类型不递归导出，未知正文只留指纹。"""
    snapshot = deepcopy(payload)
    report = tool.describe_failure(payload)
    diagnostic = report["failure_diagnostic"]
    assert report["cause_code"] == "REMOTE_FAILED"
    assert diagnostic["reason_status"] == status
    assert diagnostic["source"] is None and diagnostic["category"] is None
    assert diagnostic["fields"]["err_msg"]["status"] == field_status
    assert "PRIVATE" not in json.dumps(report, ensure_ascii=False)
    assert payload == snapshot


@pytest.mark.parametrize("message,category", [
    ("文件转换失败", "FILE_CONVERSION_FAILED"),
    ("  FILE CONVERSION FAILED. \n", "FILE_CONVERSION_FAILED"),
    ("File read failure", "FILE_READ_FAILED"),
    ("File read timeout", "FILE_READ_TIMEOUT"),
    ("Unsupported file format, please upload a valid file type", "UNSUPPORTED_FILE_FORMAT"),
    ("File size exceeds limit", "FILE_SIZE_LIMIT"),
    ("Page count exceeds limit", "PAGE_COUNT_LIMIT"),
    ("Model service temporarily unavailable", "SERVICE_UNAVAILABLE"),
    ("Task submission queue is full", "QUEUE_FULL"),
    ("Extract failed", "PARSING_FAILED"),
    ("parsing failed, please try again later", "PARSING_FAILED"),
    ("File splitting failed", "FILE_SPLITTING_FAILED"),
    ("Failed to read page count", "PAGE_COUNT_READ_FAILED"),
])
def test_remote_diagnostic_classifies_fixed_messages_without_inventing_numeric_code(message, category):
    """固定中英文原因只得到受控类别，不伪造上游数字码，也不改变生产终态/重试策略。"""
    report = tool.describe_failure({"err_msg": message})
    diagnostic = report["failure_diagnostic"]
    assert diagnostic["reason_status"] == "CLASSIFIED_MESSAGE"
    assert diagnostic["source"] == "err_msg" and diagnostic["category"] == category
    assert report["cause_code"] == jobs.result_failure({"err_msg": message}).code
    if category == "FILE_CONVERSION_FAILED":
        assert report["cause_code"] == "REMOTE_FAILED" and "转换失败" in report["message"]


@pytest.mark.parametrize("payload,source,code,category", [
    ({"err_code": -60015}, "err_code", "-60015", "FILE_CONVERSION_FAILED"),
    ({"error_code": "-60016"}, "error_code", "-60016", "FILE_CONVERSION_FAILED"),
    ({"err_code": -60003, "error_code": -60015, "err_msg": "File conversion failed"},
     "err_code", "-60003", "FILE_READ_FAILED"),
    ({"err_code": "PRIVATE_TOKEN", "error_code": -60008, "err_msg": "PRIVATE_BODY"},
     "error_code", "-60008", "FILE_READ_TIMEOUT"),
    ({"err_code": True, "error_code": "A0202"}, "error_code", "A0202", None),
])
def test_remote_diagnostic_prefers_explicit_known_code_over_message(payload, source, code, category):
    """已知错误码保持err_code优先级；布尔/未知码不压过有效错误码，不按短语猜转换子码。"""
    report = tool.describe_failure(payload)
    diagnostic = report["failure_diagnostic"]
    assert report["cause_code"] == code
    assert diagnostic["reason_status"] == "KNOWN_CODE" and diagnostic["source"] == source
    assert diagnostic["category"] == category and diagnostic["fields"][source]["known_code"] == code
    assert "PRIVATE" not in repr(report)


@pytest.mark.parametrize("message", [
    "File conversion failed: PRIVATE_BODY https://private.invalid/?token=PRIVATE_TOKEN",
    "not a failure: File conversion failed",
    "PRIVATE_BODY\nFile conversion failed",
    "PRIVATE_BODY " + "x" * 3000,
    "PRIVATE_BODY\ud800",
])
def test_remote_diagnostic_fingerprints_unknown_message_without_substring_guessing_or_leak(message, caplog):
    """未知长文/带签名URL/换行及异常Unicode仅输出长度与稳定Hash，不能只凭包含关键词分类。"""
    payload = {"err_msg": message, "err_code": "PRIVATE_TOKEN", "error_code": -999999,
               "full_zip_url": "https://PRIVATE.invalid/signed", "PRIVATE_FIELD": "PRIVATE_BODY"}
    snapshot = deepcopy(payload)
    report = tool.describe_failure(payload)
    diagnostic = report["failure_diagnostic"]
    assert diagnostic["reason_status"] == "MESSAGE_UNRECOGNIZED"
    assert diagnostic["fields"]["err_msg"] == {
        "status": "PRESENT", "length": len(message),
        "sha256": hashlib.sha256(message.encode("utf-8", errors="surrogatepass")).hexdigest()}
    assert diagnostic["fields"]["err_code"]["known_code"] is None
    assert diagnostic["fields"]["error_code"] == {"status": "PRESENT", "known_code": None}
    assert tool.describe_failure(payload) == report and payload == snapshot
    assert "PRIVATE" not in json.dumps(report, ensure_ascii=False) + caplog.text


def test_remote_failure_http_fields_reach_diagnostic_with_only_one_mock_get(context, caplog):
    """贯通Provider到诊断的字段保留，用内存HTTP验证只GET原Batch一次，不上传或发布任何结果。"""
    settings, document, job, _ = context
    # 不依赖本地Token；只使用不可用于真实鉴权的值和内存传输。
    settings = settings.model_copy(update={"mineru_api_token": SecretStr("unit-secret-not-real")})
    calls = []
    snapshot = deepcopy(job.parse_options), document.status, job.state, job.batch_id, job.task_id

    def handler(request):
        """仅接受原Batch的GET，返回其他文档及本项原因，避免错误选取同批次的别项。"""
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        assert request.url.path == f"/api/v4/extract-results/batch/{job.batch_id}"
        return httpx.Response(200, json={"code": 0, "trace_id": "trace-test", "data": {
            "batch_id": job.batch_id, "extract_result": [
                {"data_id": "other-doc", "state": "failed", "err_msg": "PRIVATE_OTHER_BODY"},
                {"data_id": job.data_id, "state": "failed", "err_msg": "File conversion failed",
                 "full_zip_url": "https://PRIVATE.invalid/signed"}]}})

    limiter = MagicMock()
    provider = tool.MinerUCloudProvider(settings, limiter=limiter,
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False))
    try:
        report = tool.inspect_job(document, job, query=provider.query)
    finally:
        provider.close()
    assert calls == [("GET", f"/api/v4/extract-results/batch/{job.batch_id}")]
    limiter.reserve.assert_called_once_with("query")
    assert report["failure_diagnostic"]["category"] == "FILE_CONVERSION_FAILED"
    assert report["cloud_queries"] == 1 and report["remote_state"] == "failed"
    assert not any(report[key] for key in ("cloud_submitted", "database_modified", "result_downloaded",
                                          "cache_published", "business_recovered"))
    assert snapshot == (job.parse_options, document.status, job.state, job.batch_id, job.task_id)
    assert "PRIVATE" not in json.dumps(report, ensure_ascii=False) + caplog.text


@pytest.mark.parametrize("damage", ["hash", "task", "provider", "split", "batch", "upload"])
def test_remote_diagnostic_rejects_identity_or_upload_gaps_before_query(context, damage):
    """身份不符、非整份或未确认上传时不访问云端，不靠查询错误任务猜测当前状态。"""
    _, document, job, _ = context
    if damage == "hash":
        job.file_hash = "c" * 64
    elif damage == "task":
        job.task_id = uuid4()
    elif damage == "provider":
        job.provider = "other"
    elif damage == "split":
        job.parse_options = {"_pdf_split": {}}
    elif damage == "batch":
        job.batch_id = None
    else:
        job.upload_complete = False
    query = MagicMock(side_effect=AssertionError("身份未通过不得查询"))
    with pytest.raises(MinerUError):
        tool.inspect_job(document, job, query=query)
    query.assert_not_called()


def test_remote_done_diagnostic_does_not_claim_business_recovery(context):
    """即使原Batch为done，诊断也不下载ZIP、运行Adapter或伪造READY。"""
    _, document, job, _ = context
    report = tool.inspect_job(document, job, query=lambda *args: {"state": "done", "trace_id": None})
    assert report["remote_state"] == "done" and report["business_recovered"] is False
    assert "failure_diagnostic" not in report
    assert document.status == "PARSING" and job.state == "running"


@pytest.mark.parametrize("query_existing", [False, True])
def test_database_diagnostic_uses_read_only_transaction_and_explicit_cloud_permission(context, monkeypatch, query_existing):
    """真实查询构造留在只读事务；默认不构造Provider，显式许可也只GET原批次且始终回滚。"""
    import sqlalchemy.orm
    import app.db.session as database

    settings, document, job, _ = context
    session = MagicMock()
    wrapper = MagicMock()
    wrapper.__enter__.return_value = session
    statements = []

    def execute(statement):
        """记录实际SQL构造并返回原检查点，不连接任何数据库。"""
        statements.append(str(statement))
        return SimpleNamespace(all=lambda: [(document, job)])

    session.execute.side_effect = execute
    monkeypatch.setattr(database, "get_engine", lambda: object())
    monkeypatch.setattr(sqlalchemy.orm, "Session", lambda *args, **kwargs: wrapper)
    provider = MagicMock()
    provider.query.return_value = {"state": "failed", "trace_id": "trace-test", "err_code": -60003}
    factory = MagicMock(return_value=provider)
    monkeypatch.setattr(tool, "MinerUCloudProvider", factory)
    report = tool.inspect_database(document.doc_id, settings, query_existing=query_existing)
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert "document_parse_jobs.task_id = documents.processing_task_id" in statements[-1]
    assert report["cloud_queries"] == int(query_existing)
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    session.flush.assert_not_called()
    if query_existing:
        provider.query.assert_called_once_with(job.batch_id, job.data_id)
        provider.close.assert_called_once()
    else:
        factory.assert_not_called()
    provider.request_upload_urls.assert_not_called()
    provider.upload.assert_not_called()
    provider.download_result.assert_not_called()


def test_private_reason_export_is_explicit_and_never_enters_public_report_or_log(context, monkeypatch, tmp_path, caplog):
    """只把已核对原Job的err_msg写入显式私有文件；普通报告/日志无原文，默认不创建文件。"""
    _, document, job, _ = context
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    reason = "PRIVATE_REASON https://private.invalid/?token=PRIVATE_TOKEN"
    query = MagicMock(return_value={"state": "failed", "trace_id": "trace-test", "err_msg": reason,
                                   "full_zip_url": "https://PRIVATE.invalid/signed", "PRIVATE_FIELD": "PRIVATE_BODY"})
    default = tool.inspect_job(document, job, query=query)
    assert "private_reason_saved" not in default and not (tmp_path / "results").exists()
    query.reset_mock()
    report = tool.inspect_job(document, job, query=query, private_reason_output="results/private_reason.json")
    query.assert_called_once_with(job.batch_id, job.data_id)
    assert report["private_reason_saved"] is True and report["cloud_queries"] == 1
    assert report["database_modified"] is False and report["cloud_submitted"] is False
    saved = json.loads((tmp_path / "results/private_reason.json").read_text(encoding="utf-8"))
    assert saved["err_msg"] == reason and saved["doc_id"] == str(document.doc_id)
    assert saved["err_msg_sha256"] == hashlib.sha256(reason.encode()).hexdigest()
    assert set(saved) == {"doc_id", "job_id", "batch_id", "data_id", "trace_id", "err_msg", "err_msg_sha256"}
    assert "PRIVATE" not in json.dumps(report, ensure_ascii=False) + caplog.text


@pytest.mark.parametrize("damage", ["outside", "extension", "existing", "no_query"])
def test_private_export_rejects_bad_target_or_missing_permission_before_query(context, monkeypatch, tmp_path, damage):
    """越界/非JSON/已有文件及未显式查询均提前停止；绝不覆盖文件或先花查询额度。"""
    _, document, job, _ = context
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    query = MagicMock()
    path = "results/private_reason.json"
    if damage == "outside":
        path = str(tmp_path / "outside.json")
    elif damage == "extension":
        path = "results/not-a-report.py"
    elif damage == "existing":
        target = tmp_path / path
        target.parent.mkdir()
        target.write_text("KEEP_EXISTING", encoding="utf-8")
    with pytest.raises(MinerUError) as failure:
        tool.inspect_job(document, job, query=None if damage == "no_query" else query, private_reason_output=path)
    assert failure.value.code == ("DIAGNOSTIC_OPTION" if damage == "no_query" else "DIAGNOSTIC_OUTPUT")
    query.assert_not_called()
    if damage == "existing":
        assert (tmp_path / path).read_text(encoding="utf-8") == "KEEP_EXISTING"


def test_private_reason_write_failure_keeps_actual_query_count_and_safe_remote_diagnostic(context, monkeypatch, tmp_path):
    """GET后磁盘失败不抹掉已发生的查询，不泄漏IO异常，也不自动重试或误称完全未写文件。"""
    _, document, job, _ = context
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    query = MagicMock(return_value={"state": "failed", "trace_id": "trace-test", "err_msg": "PRIVATE_REASON"})
    original = Path.open

    def denied(path, *args, **kwargs):
        """只拦截本次私有导出，模拟包含敏感内容的文件系统错误。"""
        if path.name == "denied.json":
            raise OSError("PRIVATE_PATH PRIVATE_TOKEN")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    report = tool.inspect_job(document, job, query=query, private_reason_output="results/denied.json")
    assert report["status"] == "REMOTE_STATUS_ONLY" and report["cloud_queries"] == 1
    assert report["private_reason_saved"] is None and report["private_reason_error_code"] == "DIAGNOSTIC_OUTPUT_IO"
    assert report["failure_diagnostic"]["reason_status"] == "MESSAGE_UNRECOGNIZED"
    assert "PRIVATE" not in json.dumps(report, ensure_ascii=False)
    query.assert_called_once()
