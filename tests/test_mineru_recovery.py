"""本地观察超时与原批次恢复的纯内存测试；不读PDF、不连接DB/Redis、不发HTTP。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models import Document, DocumentParseJob
from app.services import document_management as management
from app.services.mineru import jobs
from app.services.mineru.errors import MinerUError


class SessionStub:
    """只记录事务边界，查询返回预设Job，不模拟数据库事务保证。"""

    def __init__(self, job):
        """保存当前云检查点和提交次数。"""
        self.job, self.commits = job, 0

    def scalar(self, statement):
        """不执行SQL，供显式Retry读取当前代次的候选。"""
        return self.job

    def commit(self):
        """记录短步骤提交；测试不发布真实消息。"""
        self.commits += 1

    def flush(self):
        """保持管理准备接口兼容，不产生写入。"""


class ProviderStub:
    """只允许同一Batch/Data ID的GET及结果下载，任何上传或申请都立即失败。"""

    def __init__(self, job, state):
        """保存预期云身份和可控的远端状态。"""
        self.job, self.state, self.queries = job, state, 0

    def query(self, batch_id, data_id):
        """一次GET模拟远端状态，不阻塞等待。"""
        assert (batch_id, data_id) == (self.job.batch_id, self.job.data_id)
        self.queries += 1
        return {"state": self.state, "trace_id": "trace-test", "full_zip_url": "PRIVATE_SIGNED_URL"}

    def download_result(self, url):
        """仅返回内存结果哨兵，不创建文档或压缩包。"""
        return b"archive-stub"


@pytest.fixture
def context(monkeypatch):
    """建立过期但已上传的Job；只替代I/O和适配边界，保留生产短步骤与管理决策。"""
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek")
    document = Document(doc_id=uuid4(), kb_id=uuid4(), status="PARSING", file_hash="a" * 64,
        file_type="pdf", file_name="metadata.pdf", processing_task_id=uuid4(),
        management_task_id=None, management_config=None)
    job = DocumentParseJob(job_id=uuid4(), doc_id=document.doc_id, task_id=document.processing_task_id,
        provider="mineru_cloud", file_hash=document.file_hash, parse_options={}, options_hash="b" * 64,
        created_at=datetime.now(UTC) - timedelta(seconds=settings.mineru_job_timeout_seconds + 10),
        batch_id="existing-batch", data_id="existing-data", state="running", upload_complete=True,
        is_active=True, retry_count=0, submission_attempted=True)
    session = SessionStub(job)
    monkeypatch.setattr(jobs, "lock_job", lambda *args: (document, job) if job.is_active else None)
    monkeypatch.setattr(jobs, "restore_cached_result", lambda *args: None)
    monkeypatch.setattr(jobs, "load_raw_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(management, "active_job", lambda *args: job if job.is_active else None)
    return settings, document, job, session


def test_old_uploaded_submit_redirects_to_check_without_second_post(context):
    """迟到的submit消息只切到原Batch查询，不因创建时间过期提前失败或再次上传。"""
    settings, _, job, session = context
    assert jobs.submit_step(session, job.job_id, object(), settings) == ("check", 0)


def test_worker_downtime_done_is_recovered_before_local_deadline(context, monkeypatch):
    """远端早已done时仍回收原结果；Stub只证明编排，不冒充真实ZIP/Block验收。"""
    settings, document, job, session = context
    provider = ProviderStub(job, "done")
    parsed = SimpleNamespace(blocks=["block-stub"])
    calls = []
    monkeypatch.setattr(jobs, "local_file", lambda *args: SimpleNamespace(path="PRIVATE_PATH"))
    monkeypatch.setattr(jobs, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(jobs, "page_geometry", lambda *args: {})
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", lambda *args, **kwargs: parsed)
    monkeypatch.setattr(jobs, "save_cloud_cache", lambda *args: calls.append("cache"))

    def finish(session, document, job, parsed):
        """仅模拟已验证持久化边界，真实事务由隔离数据库用例覆盖。"""
        calls.append("blocks")
        document.status, job.state, job.is_active = "CHUNKING", "done", False

    monkeypatch.setattr(jobs, "finish_job", finish)
    assert jobs.check_step(session, job.job_id, provider, settings) == ("done", 1)
    assert calls == ["cache", "blocks"] and provider.queries == 1 and session.commits == 1
    assert document.status == "CHUNKING"


def test_pending_timeout_is_local_and_explicit_retry_keeps_all_cloud_identity(context):
    """未完成的过期GET停止调度；显式Retry续看同一Job，不改变创建时间或收费身份。"""
    settings, document, job, session = context
    provider = ProviderStub(job, "pending")
    identity = job.job_id, job.task_id, job.batch_id, job.data_id, job.created_at, job.options_hash
    with pytest.raises(MinerUError) as error:
        jobs.check_step(session, job.job_id, provider, settings)
    assert error.value.code == "JOB_TIMEOUT" and provider.queries == 1
    assert not jobs.record_error(session, job.job_id, error.value, settings)
    assert document.status == "FAILED" and job.error_code == "JOB_TIMEOUT" and not job.is_active
    manager, dispatch = management.prepare_management(session, document, settings, "retry")
    assert dispatch and manager == document.management_task_id
    assert management.next_action(session, document, settings)["cloud_step"] == "check"
    assert management.next_action(session, document, settings) == {"kind": "wait"}
    assert (job.job_id, job.task_id, job.batch_id, job.data_id, job.created_at, job.options_hash) == identity
    assert "_local_wait_started_at" in job.parse_options and job.retry_count == 0
    assert jobs.check_step(session, job.job_id, provider, settings)[0] == "check"


@pytest.mark.parametrize("change", ["hash", "batch", "upload", "provider"])
def test_local_timeout_recovery_rejects_incomplete_or_changed_identity(context, change):
    """原批次恢复必须身份完整，不能以本地超时为由重新申请链接。"""
    settings, document, job, session = context
    job.state, job.error_code, job.is_active, document.status = "failed", "JOB_TIMEOUT", False, "FAILED"
    if change == "hash":
        job.file_hash = "c" * 64
    elif change == "batch":
        job.batch_id = None
    elif change == "upload":
        job.upload_complete = False
    else:
        job.provider = "other"
    with pytest.raises(management.ManagementError, match="MANAGEMENT_CHECKPOINT"):
        management.prepare_management(session, document, settings, "retry")
    assert not job.is_active


@pytest.mark.parametrize("error", ["SUBMIT_UNCERTAIN", "REMOTE_FAILED", "ROUTED_REMOTE_FAILED", "ZIP_INVALID"])
def test_retry_query_cannot_select_other_terminal_failures(context, error):
    """SQL候选仅接受当前代次JOB_TIMEOUT，不自动复活未知提交或结果损坏等终态。"""
    settings, document, job, session = context
    document.status, job.is_active = "FAILED", False
    statements = []

    def empty(statement):
        """验证过滤参数，模拟没有符合恢复条件的行。"""
        statements.append(statement.compile().params)
        return None

    session.scalar = empty
    assert management.retry_cloud_job(session, document, settings) is None
    assert "JOB_TIMEOUT" in statements[0].values() and error not in statements[0].values()
    assert document.processing_task_id in statements[0].values()


@pytest.mark.parametrize("marker", ["invalid", "2099-01-01T00:00:00+00:00", "2020-01-01T00:00:00"])
def test_corrupt_observation_checkpoint_fails_without_cloud_submit(context, marker):
    """损坏/未来/无时区检查点明确拒绝，不延长无限观察或更改解析身份。"""
    settings, _, job, _ = context
    job.parse_options = {"_local_wait_started_at": marker}
    with pytest.raises(MinerUError) as error:
        jobs.check_deadline(job, settings)
    assert error.value.code == "JOB_CHECKPOINT"


def test_network_retry_keeps_batch_and_exhaustion_never_reposts(context, monkeypatch):
    """GET临时异常只用原批次有限重试；耗尽明确失败，不把超时恢复推广到未知故障。"""
    settings, document, job, session = context
    jobs.renew_local_wait(job, settings)
    provider = ProviderStub(job, "pending")
    identity = job.job_id, job.batch_id, job.data_id, job.submission_attempted

    def unavailable(*args):
        """模拟安全网络错误，不创建HTTP请求。"""
        raise MinerUError("NETWORK", "MinerU 网络暂时无法连接", retryable=True)

    monkeypatch.setattr(provider, "query", unavailable)
    with pytest.raises(MinerUError) as error:
        jobs.check_step(session, job.job_id, provider, settings)
    assert jobs.record_error(session, job.job_id, error.value, settings)
    assert job.retry_count == 1 and job.is_active and document.status == "PARSING"
    job.retry_count = settings.mineru_max_retries
    assert not jobs.record_error(session, job.job_id, error.value, settings)
    assert document.status == "FAILED" and not job.is_active
    assert (job.job_id, job.batch_id, job.data_id, job.submission_attempted) == identity


def test_corrupt_archive_never_calls_cache_or_block_publish(context, monkeypatch):
    """真实Adapter拒绝损坏ZIP字节，先校验后持久化；不解析PDF或改已有Block。"""
    settings, document, job, session = context
    provider = ProviderStub(job, "done")
    monkeypatch.setattr(jobs, "local_file", lambda *args: SimpleNamespace(path="PRIVATE_PATH"))
    monkeypatch.setattr(jobs, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(jobs, "page_geometry", lambda *args: {})
    calls = []
    monkeypatch.setattr(jobs, "save_cloud_cache", lambda *args: calls.append("cache"))
    monkeypatch.setattr(jobs, "finish_job", lambda *args: calls.append("blocks"))
    with pytest.raises(MinerUError) as error:
        jobs.check_step(session, job.job_id, provider, settings)
    assert error.value.code == "ZIP_INVALID" and not calls
    assert not jobs.record_error(session, job.job_id, error.value, settings)
    assert document.status == "FAILED" and job.batch_id == "existing-batch"


def test_preflight_history_only_reads_counts_and_offers_no_submit_path(context):
    """费用前置SQL只查计数/全库Hash，不取私有URL，不新增Job或读取正文。"""
    from scripts.mineru_cache_preflight import history_counts
    from sqlalchemy.dialects import postgresql
    statements = []

    class HistorySession:
        """只支持SELECT计数，任何业务写入都没有可调用入口。"""

        def scalar(self, statement):
            """保存并返回同Hash文档数量。"""
            statements.append(statement)
            return 0

        def execute(self, statement):
            """保存云提交历史查询，不连接PostgreSQL。"""
            statements.append(statement)
            return SimpleNamespace(one=lambda: (0, 0, 0))

    assert not any(history_counts(HistorySession(), "a" * 64).values())
    for statement in statements:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert statement.is_select and "upload_url" not in sql and "parse_options" not in sql
        assert "file_hash =" in sql


def test_preflight_does_not_use_materializing_cache_lookup(context, tmp_path, monkeypatch):
    """首次候选只读Hash及路径；不用document()创建缓存，也不生成/解析真实PDF验收文件。"""
    from scripts import mineru_cache_preflight as preflight
    from app.services.parse_cache import ParseCache
    settings, _, _, _ = context
    settings = settings.model_copy(update={"parsed_dir": tmp_path / "untouched-cache"})

    class SourceStub:
        """用内存字节流模拟只读文件属性，不向磁盘写PDF。"""
        suffix = ".pdf"

        def is_file(self):
            """模拟普通文件。"""
            return True

        def is_symlink(self):
            """明确不跟随符号链接。"""
            return False

        def stat(self):
            """只提供大小元数据。"""
            return SimpleNamespace(st_size=11)

        def open(self, mode):
            """仅返回内存哨兵，代码不会解析它。"""
            from io import BytesIO
            return BytesIO(b"%PDF-STUB")

    monkeypatch.setattr(preflight, "Path", lambda value: SourceStub())

    def forbidden(*args, **kwargs):
        """只读前置检查不允许materialize或写缓存。"""
        raise AssertionError("unexpected cache write")

    monkeypatch.setattr(ParseCache, "document", forbidden)
    history = {"same_hash_documents": 0, "same_hash_jobs": 0, "confirmed_batch_count": 0,
               "uncertain_submission_count": 0}
    result = preflight.inspect_candidate("unused", settings, history=lambda value: history)
    assert result["status"] == "ELIGIBLE_SNAPSHOT" and result["submitted"] is False
    assert not settings.parsed_dir.exists()
    history["uncertain_submission_count"] = 1
    assert preflight.inspect_candidate("unused", settings, history=lambda value: history)["status"] == "BLOCKED_OR_NOT_CHECKED"
