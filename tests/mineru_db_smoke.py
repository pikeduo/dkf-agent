"""显式执行的云端流程验收：隔离数据库和文件，Mock MinerU、Broker，不消耗平台额度。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from kombu.exceptions import OperationalError as BrokerError
from mineru_samples import pdf_bytes, png_bytes, result_zip
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import mineru as api_module
from app.core.config import Settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob
from app.services.mineru import jobs as jobs_module
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import create_job, record_error
from app.services.mineru.provider import UploadBatch
from app.tasks import mineru as tasks

pytest_plugins = ("document_upload_smoke",)
PREFIX = "/api/admin/knowledge-bases"


class MockProvider:
    """离线供应商，保存调用次数和可控状态，用于观察重试是否重复申请 batch。"""

    def __init__(self):
        """初始化一份独立的外部任务模拟状态。"""

        self.submissions = self.uploads = self.queries = self.downloads = 0
        self.state = "pending"
        self.error = None
        self.query_error = None
        self.upload_error = None
        self.err_msg = "错误的 Token unit-secret-not-real"
        self.archive = result_zip()

    def close(self):
        """模拟释放连接，不访问系统服务。"""

    def request_upload_urls(self, files, options):
        """为第一次调用产生批次；模拟错误发生时保留调用计数。"""

        self.submissions += 1
        if self.error:
            raise self.error
        return UploadBatch(
            f"batch-{self.submissions}",
            ["https://mineru.net/mock-upload"],
            "trace-mock",
        )

    def upload(self, url, source):
        """同一上传 URL 可重复 PUT，不产生新远端解析身份。"""

        self.uploads += 1
        if self.upload_error:
            raise self.upload_error

    def query(self, batch_id, data_id):
        """每次只返回一个指定 data_id 的状态，不在 Worker 中循环等待。"""

        self.queries += 1
        if self.query_error:
            raise self.query_error
        return {
            "state": self.state,
            "trace_id": "trace-mock",
            "err_msg": self.err_msg,
            "full_zip_url": "https://mineru.net/mock-result",
            "data_id": data_id,
        }

    def download_result(self, url):
        """返回内存中的结构化 ZIP，不写持久化解析缓存。"""

        self.downloads += 1
        return self.archive


class RetryTask:
    """替代 Celery 自动 eager 重试，捕获退避参数以逐步验证数据库检查点。"""

    def __init__(self):
        """保存当前请求重试计数及下一次延时。"""

        self.request = SimpleNamespace(retries=0)
        self.arguments = None

    def retry(self, **kwargs):
        """返回安全 Retry 对象，由被测任务抛出，不在测试中立即递归重试。"""

        self.arguments = kwargs
        return Retry(exc=kwargs["exc"], when=kwargs["countdown"])


@pytest.fixture
def cloud_context(upload_context, migrated_connection, monkeypatch):
    """绑定新任务的会话、配置和外部依赖，全部写入外层可回滚的临时 schema。"""

    api, kb_id, directory = upload_context
    connection, _, _ = migrated_connection
    settings = Settings(
        _env_file=None,
        upload_dir=directory,
        max_upload_size_mb=50,
        mineru_api_token="unit-secret-not-real",
    )
    provider = MockProvider()
    messages = []

    def configured_settings():
        """返回测试配置，不读取或改变真实 Token。"""

        return settings

    def provider_factory(configuration):
        """所有云任务共享同一离线外部状态，不发送 HTTP。"""

        return provider

    @contextmanager
    def isolated_session():
        """允许任务分步骤 commit，但不提交最外层数据库隔离事务。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    def accepted(*args, **kwargs):
        """记录短任务投递，不连接真实 Broker 或启动 Worker。"""

        messages.append(kwargs)

    monkeypatch.setattr(api_module, "get_settings", configured_settings)
    monkeypatch.setattr(tasks, "get_settings", configured_settings)
    monkeypatch.setattr(tasks, "MinerUCloudProvider", provider_factory)
    monkeypatch.setattr(tasks, "processing_session", isolated_session)
    monkeypatch.setattr(tasks.submit_mineru_parse, "apply_async", accepted)
    monkeypatch.setattr(tasks.check_mineru_result, "apply_async", accepted)
    return SimpleNamespace(
        api=api,
        kb_id=kb_id,
        directory=directory,
        provider=provider,
        messages=messages,
        settings=settings,
        session=isolated_session,
    )


def upload(context, name="测试.pdf", content=None):
    """调用独立云端上传入口，成功返回 Parse Job 而不是误投原生 Parser。"""

    response = context.api(
        "POST",
        f"{PREFIX}/{context.kb_id}/mineru/documents",
        files={"file": (name, pdf_bytes() if content is None else content)},
    )
    assert response.status_code == 202, response.text
    return response.json()


def detail(context, job):
    """同时读取供应商无关文档状态及云端任务安全视图。"""

    prefix = f"{PREFIX}/{context.kb_id}/documents/{job['doc_id']}"
    return context.api("GET", prefix).json(), context.api(
        "GET", prefix + "/parse-jobs"
    ).json()[0]


def execute(job, step="submit"):
    """在当前进程执行短任务成功路径，后续消息仍由夹具截获而不自动运行。"""

    task = tasks.submit_mineru_parse if step == "submit" else tasks.check_mineru_result
    return task.apply(args=[job["job_id"]], throw=False)


@pytest.mark.parametrize(
    "name,content", [("测试.pdf", None), ("图片.png", png_bytes())]
)
def test_cloud_pipeline_persists_blocks_and_skips_duplicate(
    cloud_context, name, content
):
    """从上传到五类 Block 事务入库，重复完成消息跳过，不生成 Chunk 或 READY。"""

    context = cloud_context
    job = upload(context, name, content)
    document, current = detail(context, job)
    assert document["status"] == "PARSING" and current["state"] == "waiting-file"
    assert execute(job).state == "SUCCESS"
    assert context.provider.submissions == context.provider.uploads == 1
    assert detail(context, job)[1]["batch_id"] == "batch-1"
    assert execute(job, "check").state == "SUCCESS"
    assert context.provider.queries == 1 and context.provider.downloads == 0
    context.provider.state = "done"
    completed = execute(job, "check")
    assert completed.state == "SUCCESS" and completed.result["block_count"] == 5
    document, current = detail(context, job)
    assert document["status"] == "CHUNKING" and current["state"] == "done"
    assert not current["is_active"] and current["finished_at"]
    assert "upload_url" not in current and "unit-secret" not in str(current)
    with context.session() as session:
        blocks = session.scalars(
            select(DocumentBlock)
            .where(DocumentBlock.doc_id == UUID(job["doc_id"]))
            .order_by(DocumentBlock.block_index)
        ).all()
        identities = [block.block_id for block in blocks]
        assert [block.block_index for block in blocks] == [1, 2, 3, 4, 5]
        assert all(block.source == "mineru_cloud" for block in blocks)
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert execute(job, "check").result["status"] == "skipped"
    with context.session() as session:
        assert (
            session.scalars(
                select(DocumentBlock.block_id)
                .where(DocumentBlock.doc_id == UUID(job["doc_id"]))
                .order_by(DocumentBlock.block_index)
            ).all()
            == identities
        )
    assert context.provider.downloads == 1


@pytest.mark.parametrize("state", ["waiting-file", "pending", "running", "converting"])
def test_pending_state_schedules_one_short_check(cloud_context, state):
    """未完成状态只 GET 一次并投递 countdown，保存外部状态而不阻塞等待。"""

    job = upload(cloud_context)
    execute(job)
    cloud_context.provider.state = state
    result = execute(job, "check")
    assert result.state == "SUCCESS" and cloud_context.provider.queries == 1
    assert detail(cloud_context, job)[1]["state"] == state
    assert cloud_context.messages[-1]["countdown"] == 15


def test_layout_order_is_persisted_once_with_stable_ids(cloud_context):
    """官方页眉索引恢复后按新顺序事务入库；重复短消息不改 ID、不增加块或 Chunk。"""

    items = [
        {"type": "text", "text": "正文", "bbox": [80, 200, 650, 230], "page_idx": 0},
        {"type": "text", "text": "页底", "bbox": [80, 900, 500, 920], "page_idx": 0},
        {"type": "header", "text": "页眉", "bbox": [80, 50, 650, 80], "page_idx": 0},
    ]
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [1000, 1000],
                "para_blocks": [
                    dict(value, index=index) for value, index in zip(items[:2], [1, 2])
                ],
                "discarded_blocks": [dict(items[2], index=0)],
            }
        ]
    }
    cloud_context.provider.archive = result_zip(items, layout=layout)
    job = upload(cloud_context)
    assert execute(job).state == "SUCCESS"
    cloud_context.provider.state = "done"
    assert execute(job, "check").result["block_count"] == 3
    with cloud_context.session() as session:
        blocks = session.scalars(
            select(DocumentBlock).order_by(DocumentBlock.block_index)
        ).all()
        identities = [block.block_id for block in blocks]
        assert [block.text for block in blocks] == ["页眉", "正文", "页底"]
        assert [block.block_index for block in blocks] == [1, 2, 3]
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert execute(job, "check").result["status"] == "skipped"
    with cloud_context.session() as session:
        assert (
            session.scalars(
                select(DocumentBlock.block_id).order_by(DocumentBlock.block_index)
            ).all()
            == identities
        )
    assert detail(cloud_context, job)[0]["status"] == "CHUNKING"


def test_image_network_exhaustion_has_no_blocks_and_requires_explicit_new_job(
    cloud_context,
):
    """图片上传完成但查询重试耗尽时无块；远端后来成功也不能自动复活已终止的任务。"""

    job = upload(cloud_context, "图片.png", png_bytes())
    execute(job)
    cloud_context.provider.query_error = MinerUError(
        "NETWORK", "网络暂时无法连接", retryable=True
    )
    for _ in range(cloud_context.settings.mineru_max_retries):
        with pytest.raises(Retry):
            tasks.execute_step(RetryTask(), job["job_id"], "check")
    assert execute(job, "check").state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert document["status"] == "FAILED" and current["state"] == "failed"
    assert current["retry_count"] == 5 and current["error_code"] == "NETWORK"
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        assert row.upload_complete and row.batch_id and not row.is_active
        assert session.scalar(select(func.count()).select_from(DocumentBlock)) == 0
    cloud_context.provider.query_error = None
    cloud_context.provider.state = "done"
    assert execute(job, "check").result["status"] == "skipped"
    assert detail(cloud_context, job)[0]["status"] == "FAILED"
    response = cloud_context.api(
        "POST", f"{PREFIX}/{cloud_context.kb_id}/documents/{job['doc_id']}/mineru"
    )
    assert response.status_code == 202
    new = response.json()
    assert new["job_id"] != job["job_id"] and new["doc_id"] == job["doc_id"]
    with cloud_context.session() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentParseJob)) == 2
    assert execute(new).state == "SUCCESS"
    assert (
        cloud_context.provider.submissions == 2
    )  # 显式重试新建批次，不宣称仅重新下载旧 ZIP。
    assert execute(new, "check").result["block_count"] == 5


def test_active_identity_reused_and_completed_reparse_rejected(cloud_context):
    """重复提交沿用同一活动身份；文档完成后不提供阶段 12 的缓存或 Reparse。"""

    job = upload(cloud_context)
    url = f"{PREFIX}/{cloud_context.kb_id}/documents/{job['doc_id']}/mineru"
    assert cloud_context.api("POST", url).json()["job_id"] == job["job_id"]
    with cloud_context.session() as session:
        assert session.scalar(select(func.count()).select_from(DocumentParseJob)) == 1
    execute(job)
    cloud_context.provider.state = "done"
    execute(job, "check")
    assert cloud_context.api("POST", url).status_code == 409


def test_database_rejects_duplicate_active_identity(cloud_context):
    """除了行锁，数据库部分唯一索引也阻止相同解析身份的并发有效记录。"""

    job = upload(cloud_context)
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                DocumentParseJob(
                    doc_id=row.doc_id,
                    provider=row.provider,
                    model_version=row.model_version,
                    file_hash=row.file_hash,
                    parse_options=row.parse_options,
                    options_hash=row.options_hash,
                    data_id=f"dkf-{uuid4().hex}",
                    task_id=uuid4(),
                    is_active=True,
                )
            )
            session.flush()


def test_put_retry_reuses_checkpoint_and_backoff(cloud_context):
    """PUT 临时失败保留 batch；再次执行仅 PUT，不重复 POST，退避包含抖动。"""

    job = upload(cloud_context)
    cloud_context.provider.upload_error = MinerUError(
        "UPLOAD_NETWORK", "临时上传失败", retryable=True
    )
    task = RetryTask()
    with pytest.raises(Retry):
        tasks.execute_step(task, job["job_id"], "submit")
    assert 5 <= task.arguments["countdown"] <= 10
    current = detail(cloud_context, job)[1]
    assert current["batch_id"] == "batch-1" and current["retry_count"] == 1
    cloud_context.provider.upload_error = None
    assert execute(job).state == "SUCCESS"
    assert (
        cloud_context.provider.submissions == 1 and cloud_context.provider.uploads == 2
    )


def test_429_retries_with_retry_after_and_no_raw_token(cloud_context):
    """明确的 429 响应允许重新申请，遵守 Retry-After 并安全持久化原因。"""

    job = upload(cloud_context)
    cloud_context.provider.error = MinerUError(
        "HTTP_429", "MinerU 临时限流", retryable=True, retry_after=60
    )
    task = RetryTask()
    with pytest.raises(Retry):
        tasks.execute_step(task, job["job_id"], "submit")
    assert task.arguments["countdown"] >= 60
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        assert not row.submission_attempted and row.retry_count == 1
    cloud_context.provider.error = None
    assert execute(job).state == "SUCCESS"


def test_confirmed_remote_transient_failure_creates_new_batch(cloud_context):
    """只有远端明确 failed 后才重建 batch，终止旧批次的检查点不被错误回滚丢失。"""

    job = upload(cloud_context)
    execute(job)
    cloud_context.provider.state, cloud_context.provider.err_msg = (
        "failed",
        "模型服务暂时不可用",
    )
    with pytest.raises(Retry):
        tasks.execute_step(RetryTask(), job["job_id"], "check")
    assert detail(cloud_context, job)[1]["batch_id"] is None
    assert execute(job, "check").result["status"] == "scheduled_submit"
    assert execute(job).state == "SUCCESS"
    assert cloud_context.provider.submissions == 2


@pytest.mark.parametrize(
    "code", ["A0202", "FILE_DAMAGED", "DAILY_LIMIT", "SUBMIT_UNCERTAIN"]
)
def test_permanent_error_marks_document_failed(cloud_context, code):
    """鉴权、损坏、日额度和不确定提交立即失败，不盲目循环重试或伪造结果。"""

    job = upload(cloud_context)
    cloud_context.provider.error = MinerUError(code, "需要人工处理")
    assert execute(job).state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert document["status"] == "FAILED" and current["state"] == "failed"
    assert not current["is_active"] and current["retry_count"] == 0
    assert len(list(cloud_context.directory.iterdir())) == 1


def test_worker_restart_uncertain_intent_does_not_repost(cloud_context):
    """模拟申请后进程中断：新 Worker 先等待检查点期限，超时安全失败而非再 POST。"""

    job = upload(cloud_context)
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        row.submission_attempted = True
        row.submission_started_at = datetime.now(UTC)
        session.commit()
    assert execute(job).result["status"] == "scheduled_submit"
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        row.submission_started_at = datetime.now(UTC) - timedelta(minutes=5)
        session.commit()
    assert execute(job).state == "FAILURE"
    assert cloud_context.provider.submissions == 0
    assert detail(cloud_context, job)[1]["error_code"] == "SUBMIT_UNCERTAIN"


def test_failed_result_preserves_old_blocks_and_safe_error(cloud_context):
    """未知外部错误不透传，失败不替换原有 Block，保留原件供人工核对。"""

    job = upload(cloud_context)
    with cloud_context.session() as session:
        session.add(
            DocumentBlock(
                doc_id=UUID(job["doc_id"]),
                page=1,
                block_index=1,
                block_type="text",
                text="保留旧证据",
                source="native_parser",
            )
        )
        session.commit()
    execute(job)
    cloud_context.provider.state = "failed"
    assert execute(job, "check").state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert "unit-secret" not in str(current) and document["status"] == "FAILED"
    with cloud_context.session() as session:
        assert session.scalar(select(DocumentBlock.text)) == "保留旧证据"


def test_stale_failure_cannot_overwrite_new_job(cloud_context):
    """旧代次失败回调不能覆盖新任务、清除有效解析或生成重复块。"""

    old = upload(cloud_context)
    with cloud_context.session() as session:
        record_error(
            session,
            UUID(old["job_id"]),
            MinerUError("TEST", "旧任务失败"),
            cloud_context.settings,
        )
        document = session.get(Document, UUID(old["doc_id"]))
        new = create_job(session, document, cloud_context.settings)
        session.commit()
        new_id = new.job_id
        assert not record_error(
            session,
            UUID(old["job_id"]),
            MinerUError("TEST", "过期回调"),
            cloud_context.settings,
        )
    assert execute(old).result["status"] == "skipped"
    with cloud_context.session() as session:
        assert session.get(DocumentParseJob, new_id).is_active
        assert session.get(Document, UUID(old["doc_id"])).status == "PARSING"


def test_queue_failure_returns_recoverable_job(cloud_context, monkeypatch):
    """API 投递失败返回已有标识，恢复入口重用同一任务，不自动创建补投器。"""

    def unavailable(*args, **kwargs):
        """模拟 Broker 故障，异常中的敏感文本不能进入 API 响应。"""

        raise BrokerError("unit-secret-not-real")

    monkeypatch.setattr(tasks.submit_mineru_parse, "apply_async", unavailable)
    response = cloud_context.api(
        "POST",
        f"{PREFIX}/{cloud_context.kb_id}/mineru/documents",
        files={"file": ("队列.pdf", pdf_bytes())},
    )
    assert response.status_code == 503 and "unit-secret" not in response.text
    ids = response.json()["detail"]
    with cloud_context.session() as session:
        assert session.get(DocumentParseJob, UUID(ids["job_id"])).is_active


def test_api_configuration_scope_and_type(cloud_context, monkeypatch):
    """未配置 Token 不影响原生能力；云入口不接受 TXT，也不能跨知识库查询任务。"""

    response = cloud_context.api(
        "POST",
        f"{PREFIX}/{cloud_context.kb_id}/mineru/documents",
        files={"file": ("不支持.txt", b"text")},
    )
    assert response.status_code == 415
    job = upload(cloud_context)
    assert (
        cloud_context.api(
            "GET", f"{PREFIX}/{uuid4()}/documents/{job['doc_id']}/parse-jobs"
        ).status_code
        == 404
    )

    def blank_settings():
        """仅覆盖云端入口配置，不清除真实环境中的 Token。"""

        return Settings(_env_file=None)

    monkeypatch.setattr(api_module, "get_settings", blank_settings)
    assert (
        cloud_context.api(
            "POST", f"{PREFIX}/{cloud_context.kb_id}/documents/{job['doc_id']}/mineru"
        ).status_code
        == 503
    )
    assert cloud_context.api("GET", "/health").status_code == 200


def test_partial_block_write_is_rolled_back(cloud_context, monkeypatch):
    """模拟写块后失败，旧块保留且新块、CHUNKING 与 done 不会半提交。"""

    job = upload(cloud_context)
    with cloud_context.session() as session:
        session.add(
            DocumentBlock(
                doc_id=UUID(job["doc_id"]),
                page=1,
                block_index=1,
                block_type="text",
                text="事务保留证据",
                source="native_parser",
            )
        )
        session.commit()
    execute(job)
    cloud_context.provider.state = "done"
    original = jobs_module.persist_blocks

    def write_then_fail(session, document, parsed):
        """写入并 flush 全部新块后制造永久错误，检查外层会话回滚是否完整。"""

        original(session, document, parsed)
        raise MinerUError("WRITE_FAILED", "模拟写块后失败")

    monkeypatch.setattr(jobs_module, "persist_blocks", write_then_fail)
    assert execute(job, "check").state == "FAILURE"
    assert detail(cloud_context, job)[0]["status"] == "FAILED"
    with cloud_context.session() as session:
        assert session.scalars(select(DocumentBlock.text)).all() == ["事务保留证据"]


def test_retry_exhaustion_and_total_deadline(cloud_context):
    """持久化重试次数达到上限即终止，长期未完成也有总等待期限。"""

    job = upload(cloud_context)
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        row.retry_count = cloud_context.settings.mineru_max_retries
        session.commit()
    cloud_context.provider.error = MinerUError(
        "HTTP_503", "临时服务错误", retryable=True
    )
    assert execute(job).state == "FAILURE"
    assert detail(cloud_context, job)[0]["status"] == "FAILED"
    with cloud_context.session() as session:
        document = session.get(Document, UUID(job["doc_id"]))
        next_job = create_job(session, document, cloud_context.settings)
        next_job.created_at = datetime.now(UTC) - timedelta(hours=3)
        session.commit()
        identifier = str(next_job.job_id)
    cloud_context.provider.error = None
    assert (
        tasks.submit_mineru_parse.apply(args=[identifier], throw=False).state
        == "FAILURE"
    )
    with cloud_context.session() as session:
        assert (
            session.get(DocumentParseJob, UUID(identifier)).error_code == "JOB_TIMEOUT"
        )
        assert session.get(Document, UUID(job["doc_id"])).status == "FAILED"


def test_ambiguous_publish_failure_does_not_overwrite_completed_job(
    cloud_context, monkeypatch
):
    """Broker 已投递但确认丢失时，即使请求返回 503，也不能覆盖刚完成任务的成功状态。"""

    def delivered_then_failed(job_id, *args):
        """模拟并发 Worker 已处理已接收消息，然后 API 才收到 Broker 的失败确认。"""

        job = {"job_id": str(job_id)}
        assert execute(job).state == "SUCCESS"
        cloud_context.provider.state = "done"
        assert execute(job, "check").state == "SUCCESS"
        raise MinerUError("QUEUE", "队列确认丢失", retryable=True)

    monkeypatch.setattr(api_module, "publish_step", delivered_then_failed)
    response = cloud_context.api(
        "POST",
        f"{PREFIX}/{cloud_context.kb_id}/mineru/documents",
        files={"file": ("确认丢失.pdf", pdf_bytes())},
    )
    assert response.status_code == 503
    ids = response.json()["detail"]
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(ids["job_id"]))
        assert row.state == "done" and row.error_code is None
        assert session.get(Document, row.doc_id).status == "CHUNKING"
