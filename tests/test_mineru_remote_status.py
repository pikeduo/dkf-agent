"""原Batch错误定位与安全持久化回归；仅内存Mock，由用户手动执行。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

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
