"""显式执行的云端流程验收：隔离数据库和文件，Mock MinerU、Broker，不消耗平台额度。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from kombu.exceptions import OperationalError as BrokerError
from mineru_samples import content_items, pdf_bytes, png_bytes, result_zip
from office_samples import office_bytes
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
        parsed_dir=directory.parent / "parsed",
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
    """默认上传与单页 Mock 结果匹配的 PDF；多页用例显式提供原件和对应结果。"""

    response = context.api(
        "POST",
        f"{PREFIX}/{context.kb_id}/mineru/documents",
        files={"file": (name, pdf_bytes(1) if content is None else content)},
    )
    assert response.status_code == 202, response.text
    return response.json()


def detail(context, job):
    """同时读取供应商无关文档状态及云端任务安全视图。"""

    prefix = f"{PREFIX}/{context.kb_id}/documents/{job['doc_id']}"
    return context.api("GET", prefix).json(), context.api(
        "GET", prefix + "/parse-jobs"
    ).json()[0]


def execute(job, step="submit", *, expected_state="SUCCESS"):
    """执行受控短任务并检查预期状态；失败时展示安全原因，负向用例显式声明 FAILURE。"""

    task = tasks.submit_mineru_parse if step == "submit" else tasks.check_mineru_result
    result = task.apply(args=[job["job_id"]], throw=False)
    # throw=False 将异常放在 result 中；先检查状态，避免把异常当作字典而掩盖根因。
    assert result.state == expected_state, (
        f"MinerU {step} 步骤预期 {expected_state}，实际 {result.state}；"
        f"任务结果：{result.result}"
    )
    return result


def test_execute_reports_task_failure_reason(monkeypatch):
    """成功路径意外失败时提示实际安全异常；显式负向路径仍能检查异常结果。"""

    failure = SimpleNamespace(
        state="FAILURE",
        result=tasks.DocumentProcessingError("PDF 部分非空页面缺少解析结果，需要人工核对"),
    )

    def failed_task(*args, **kwargs):
        """仅返回受控 Celery 结果，不执行任务、解析文档或连接服务。"""

        return failure

    monkeypatch.setattr(tasks.check_mineru_result, "apply", failed_task)
    job = {"job_id": str(uuid4())}
    with pytest.raises(AssertionError, match="PDF 部分非空页面缺少解析结果"):
        execute(job, "check")
    assert execute(job, "check", expected_state="FAILURE") is failure


@pytest.mark.parametrize("explicit_cloud", [False, True])
def test_incremental_upload_cloud_generation_and_atomic_blocks_continue_to_chunk(cloud_context, monkeypatch, explicit_cloud):
    """隔离DB验证缓存miss至ingest/done全链；仅模型/供应商/Broker为Stub，不运行GPU。"""
    from app.api import admin_knowledge_bases as admin
    from app.services import document_management as service
    from app.tasks import documents, chunks, management, embeddings, indexing as index_tasks
    from app.services.embedding import processing as embedding_processing
    from app.services.parse_cache import ParseCache
    from embedding_db_smoke import DenseStub
    from router_samples import router_pdf
    context = cloud_context
    managers, chunk_messages, downstream = [], [], []
    context.settings = context.settings.model_copy(update={
        "embeddings_dir": context.directory.parent / "vectors", "embedding_device": "cuda:0"})

    def manager_publish(doc_id, task_id):
        """只记录已提交的独立编排消息，不连接真实Broker。"""
        managers.append((doc_id, task_id))
        return True

    def chunk_publish(**kwargs):
        """观察下一阶段投递，绝不运行切片或生成真实样本。"""
        chunk_messages.append(kwargs)

    for module in (admin, api_module):
        # 原逐阶段夹具有意隔离管理；本用例显式恢复真实新编排准备/投递边界。
        monkeypatch.setattr(module, "prepare_ingestion", service.prepare_ingestion)
        monkeypatch.setattr(module, "publish_ingestion", service.publish_ingestion)
    monkeypatch.setattr(service, "publish_management", manager_publish)
    monkeypatch.setattr(documents, "processing_session", context.session)
    monkeypatch.setattr(documents, "get_settings", lambda: context.settings)
    monkeypatch.setattr(management, "processing_session", context.session)
    monkeypatch.setattr(management, "get_settings", lambda: context.settings)
    monkeypatch.setattr(management.manage_document, "retry", lambda **kwargs: Retry())
    monkeypatch.setattr(chunks.chunk_document, "apply_async", chunk_publish)
    for module in (chunks, embeddings, index_tasks):
        monkeypatch.setattr(module, "processing_session", context.session)
    monkeypatch.setattr(embedding_processing, "get_embedding_service", lambda *args: DenseStub())
    monkeypatch.setattr(embeddings.embed_document, "apply_async", lambda **kwargs: downstream.append(("embed", kwargs)))
    monkeypatch.setattr(index_tasks.index_document, "apply_async", lambda **kwargs: downstream.append(("index", kwargs)))
    if explicit_cloud:
        job = upload(context)
        doc_id = UUID(job["doc_id"])
    else:
        saved = context.api("POST", f"{PREFIX}/{context.kb_id}/documents",
            files={"file": ("当前路由.pdf", router_pdf("scan"))}).json()
        doc_id = UUID(saved["doc_id"])
        result = documents.process_document.apply(args=[saved["doc_id"]],
            task_id=saved["task_id"])
        assert result.state == "SUCCESS" and result.result["status"] == "delegated_to_mineru"
        assert result.result["route_decision"]["parse_mode"] == "auto"
        assert result.result["route_decision"]["route_strategy"] == "mineru_whole"
        job = context.api("GET", f"{PREFIX}/{context.kb_id}/documents/{doc_id}/parse-jobs").json()[0]
        assert job["task_id"] != saved["task_id"]
    with context.session() as session:
        document = session.get(Document, doc_id)
        manager_id = document.management_task_id
        assert managers == [(doc_id, manager_id)]
        assert service.config_for(document)["source_parse_task_id"] == job["task_id"]
        assert service.config_for(document)["stage"] == "cloud"
    assert execute(job).state == "SUCCESS"
    # 真实提交检查点必须先落库；重复submit/未完成check不能再POST或PUT。
    with context.session() as session:
        checkpoint = session.get(DocumentParseJob, UUID(job["job_id"]))
        assert checkpoint.batch_id and checkpoint.data_id and checkpoint.submission_attempted and checkpoint.upload_complete
        assert ParseCache(context.settings).job_document(
            session.get(Document, doc_id), checkpoint.parse_options["_cache_recipe"], checkpoint.job_id) is None
    assert execute(job).result["status"] == "scheduled_check"
    assert execute(job, "check").result["status"] == "scheduled_check"
    context.provider.state = "done"
    assert execute(job, "check").result["status"] == "awaiting_chunk"
    result = management.manage_document.apply(args=[str(doc_id)], task_id=str(manager_id))
    assert result.state == "RETRY" and len(chunk_messages) == 1
    with context.session() as session:
        document = session.get(Document, doc_id)
        assert document.status == "CHUNKING" and document.chunk_task_id is not None
        assert service.config_for(document)["stage"] == "chunk"
        assert service.config_for(document)["source_parse_task_id"] == str(document.processing_task_id)
        assert ParseCache(context.settings).job_document(
            document, session.get(DocumentParseJob, UUID(job["job_id"])).parse_options["_cache_recipe"],
            UUID(job["job_id"])) is not None
    # 用生产阶段任务与管理决策推进，不手动置READY；GPU只提供确定性单位向量。
    first_chunk = chunk_messages[0]
    assert chunks.chunk_document.apply(args=first_chunk["args"], task_id=first_chunk["task_id"]).state == "SUCCESS"
    assert management.manage_document.apply(args=[str(doc_id)], task_id=str(manager_id)).state == "RETRY"
    assert downstream[-1][0] == "embed"
    message = downstream[-1][1]
    assert message["queue"] == "gpu_queue"
    assert embeddings.embed_document.apply(args=message["args"], task_id=message["task_id"]).state == "SUCCESS"
    assert management.manage_document.apply(args=[str(doc_id)], task_id=str(manager_id)).state == "RETRY"
    assert downstream[-1][0] == "index"
    message = downstream[-1][1]
    assert index_tasks.index_document.apply(args=message["args"], task_id=message["task_id"]).state == "SUCCESS"
    assert management.manage_document.apply(args=[str(doc_id)], task_id=str(manager_id)).result["status"] == "done"
    assert execute(job).result["status"] == execute(job, "check").result["status"] == "skipped"
    with context.session() as session:
        document = session.get(Document, doc_id)
        config = service.config_for(document)
        assert document.status == "READY" and (config["operation"], config["state"]) == ("ingest", "done")
        total, vectors = session.execute(select(func.count(), func.count(DocumentChunk.embedding))
            .where(DocumentChunk.doc_id == doc_id)).one()
        assert total == vectors > 0
        assert session.scalar(select(func.count()).select_from(DocumentParseJob)
            .where(DocumentParseJob.doc_id == doc_id)) == 1
    assert context.provider.submissions == 1
    assert context.provider.uploads == 1


@pytest.mark.parametrize(
    "name,content",
    [("测试.pdf", None), ("图片.png", png_bytes())]
    + [
        (f"办公样本.{kind}", office_bytes(kind))
        for kind in ("doc", "docx", "ppt", "pptx", "xls", "xlsx")
    ],
    ids=["pdf", "png", "doc", "docx", "ppt", "pptx", "xls", "xlsx"],
)
def test_cloud_pipeline_persists_blocks_and_skips_duplicate(
    cloud_context, name, content
):
    """PDF/图片/六种 Office 事务入库，Office 坐标为空，重复完成消息不增块。"""

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
    expected_blocks = 6 if name.endswith(".xlsx") else 5
    assert completed.state == "SUCCESS" and completed.result["block_count"] == expected_blocks
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
        assert [block.block_index for block in blocks] == list(range(1, expected_blocks + 1))
        if name.endswith(".xlsx"):
            assert blocks[0].source == "native_parser" and "=B2*2" in blocks[0].text
            assert all(block.source == "mineru_cloud" for block in blocks[1:])
        else:
            assert all(block.source == "mineru_cloud" for block in blocks)
        if name.endswith((".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")):
            assert all(block.bbox is None and block.page == 1 for block in blocks)
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


def test_two_page_cloud_result_covers_all_pages_and_keeps_ids(cloud_context):
    """两页非空原件必须有两页结果；成功写块后重复消息不改变页码、顺序或 ID。"""

    context = cloud_context
    items = content_items()
    items[-1]["page_idx"] = 1
    context.provider.archive = result_zip(items)
    job = upload(context, content=pdf_bytes(2))
    execute(job)
    context.provider.state = "done"
    assert execute(job, "check").result["block_count"] == len(items)
    document, current = detail(context, job)
    assert document["status"] == "CHUNKING" and current["state"] == "done"
    with context.session() as session:
        blocks = session.scalars(
            select(DocumentBlock)
            .where(DocumentBlock.doc_id == UUID(job["doc_id"]))
            .order_by(DocumentBlock.block_index)
        ).all()
        identities = [block.block_id for block in blocks]
        assert [block.page for block in blocks] == [1, 1, 1, 1, 2]
        assert [block.block_index for block in blocks] == list(range(1, len(items) + 1))
        assert len(set(identities)) == len(items)
    assert execute(job, "check").result["status"] == "skipped"
    with context.session() as session:
        assert session.scalars(
            select(DocumentBlock.block_id)
            .where(DocumentBlock.doc_id == UUID(job["doc_id"]))
            .order_by(DocumentBlock.block_index)
        ).all() == identities
    assert context.provider.submissions == context.provider.downloads == 1


def test_two_page_cloud_result_missing_page_fails_without_cache(cloud_context):
    """两页原件只返回第一页时安全失败，不提交部分块、发布成功缓存或追加收费批次。"""

    context = cloud_context
    job = upload(context, content=pdf_bytes(2))
    execute(job)
    context.provider.state = "done"
    failed = execute(job, "check", expected_state="FAILURE")
    assert "PDF 部分非空页面缺少解析结果" in str(failed.result)
    document, current = detail(context, job)
    assert document["status"] == "FAILED" and current["state"] == "failed"
    assert current["error_code"] == "COMPLETENESS" and not current["is_active"]
    assert "unit-secret" not in current["error_message"]
    with context.session() as session:
        assert session.scalar(
            select(func.count()).select_from(DocumentBlock)
            .where(DocumentBlock.doc_id == UUID(job["doc_id"]))
        ) == 0
    assert not list(context.settings.parsed_dir.rglob("ready.json"))
    assert not (context.settings.parsed_dir / job["doc_id"] / "latest.json").exists()
    assert execute(job, "check").result["status"] == "skipped"
    assert context.provider.submissions == context.provider.uploads == 1
    assert context.provider.queries == context.provider.downloads == 1


def test_verified_cross_page_merge_persists_source_pages_once(cloud_context):
    """跨页空占位恢复原页并事务入库，重复消息不增块、不再次下载或提交批次。"""

    from test_mineru_merge import merged_case

    context = cloud_context
    values, layout, _ = merged_case(cross_page=True)
    context.provider.archive = result_zip(values, layout=layout)
    job = upload(context, content=pdf_bytes(2))
    execute(job)
    context.provider.state = "done"
    assert execute(job, "check").result["block_count"] == 2
    with context.session() as session:
        rows = session.scalars(select(DocumentBlock).where(
            DocumentBlock.doc_id == UUID(job["doc_id"])).order_by(DocumentBlock.block_index)).all()
        ids = [row.block_id for row in rows]
        assert [row.page for row in rows] == [1, 2]
        assert [row.text.strip() for row in rows] == ["前段正文", "续段正文"]
        assert [row.block_index for row in rows] == [1, 2]
    assert execute(job, "check").result["status"] == "skipped"
    with context.session() as session:
        assert session.scalars(select(DocumentBlock.block_id).where(
            DocumentBlock.doc_id == UUID(job["doc_id"])).order_by(DocumentBlock.block_index)).all() == ids
    assert detail(context, job)[0]["status"] == "CHUNKING"
    assert context.provider.submissions == context.provider.downloads == 1


def test_uncertain_merge_fails_without_blocks_or_success_cache(cloud_context):
    """合并来源不完整必须 FAILED/零块/无成功缓存，保留原批次且不自动再次付费。"""

    from test_mineru_merge import merged_case

    context = cloud_context
    values, layout, _ = merged_case()
    layout["pdf_info"][0]["para_blocks"][0]["lines"][1]["spans"][0]["content"] = "来源冲突"
    context.provider.archive = result_zip(values, layout=layout)
    job = upload(context)
    execute(job)
    context.provider.state = "done"
    execute(job, "check", expected_state="FAILURE")
    document, current = detail(context, job)
    assert document["status"] == "FAILED" and current["error_code"] == "MERGE_EVIDENCE"
    assert current["state"] == "failed" and not current["is_active"]
    with context.session() as session:
        assert session.scalar(select(func.count()).select_from(DocumentBlock).where(
            DocumentBlock.doc_id == UUID(job["doc_id"]))) == 0
    assert not list(context.settings.parsed_dir.rglob("ready.json"))
    assert not (context.settings.parsed_dir / job["doc_id"] / "latest.json").exists()
    assert execute(job, "check").result["status"] == "skipped"
    assert context.provider.submissions == context.provider.downloads == 1


def test_old_frozen_normalizer_does_not_publish_new_version_under_old_key(cloud_context):
    """旧活动任务不可用新代码覆盖 v1 冻结键，安全停止且不追加批次或下载 ZIP。"""

    context = cloud_context
    job = upload(context)
    execute(job)
    with context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        old_recipe = row.parse_options["_cache_recipe"] | {"normalizer_version": "mineru-adapter-v1"}
        row.parse_options = row.parse_options | {"_cache_recipe": old_recipe}
        session.commit()
    context.provider.state = "done"
    execute(job, "check", expected_state="FAILURE")
    assert detail(context, job)[1]["error_code"] == "CACHE_CONFIG_CHANGED"
    assert context.provider.submissions == 1 and context.provider.queries == context.provider.downloads == 0
    assert not list(context.settings.parsed_dir.rglob("ready.json"))


def test_old_queued_normalizer_is_rejected_before_cloud_submission(cloud_context):
    """升级前未提交的旧冻结任务先拒绝，不能先花云额度再发现版本不兼容。"""

    context = cloud_context
    job = upload(context)
    with context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        row.parse_options = row.parse_options | {"_cache_recipe":
            row.parse_options["_cache_recipe"] | {"normalizer_version": "mineru-adapter-v1"}}
        session.commit()
    execute(job, expected_state="FAILURE")
    assert detail(context, job)[1]["error_code"] == "CACHE_CONFIG_CHANGED"
    assert context.provider.submissions == context.provider.uploads == 0


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


def test_image_network_exhaustion_requires_confirmed_force_reparse(
    cloud_context,
):
    """查询耗尽无块；普通显式提交同样不得重新收费，只有确认 Force Reparse 可新建批次。"""

    job = upload(cloud_context, "图片.png", png_bytes())
    execute(job)
    cloud_context.provider.query_error = MinerUError(
        "NETWORK", "网络暂时无法连接", retryable=True
    )
    for _ in range(cloud_context.settings.mineru_max_retries):
        with pytest.raises(Retry):
            tasks.execute_step(RetryTask(), job["job_id"], "check")
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
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
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "CACHE_REPARSE_REQUIRED"
    with cloud_context.session() as session:
        new_job = create_job(session, session.get(Document, UUID(job["doc_id"])),
                             cloud_context.settings, force_reparse=True)
        session.commit()
        new = {"job_id": str(new_job.job_id), "doc_id": str(new_job.doc_id)}
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
    """原显式云入口重复提交沿用活动身份；完成后拒绝借此入口绕过维护规则。"""

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


@pytest.mark.parametrize("kind", ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"])
def test_put_retry_reuses_checkpoint_and_backoff(cloud_context, kind):
    """PDF/Office PUT 临时失败沿用 batch，恢复后只写一次 Block，重复消息不增加块。"""

    job = upload(
        cloud_context, f"重试.{kind}", None if kind == "pdf" else office_bytes(kind)
    )
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
    cloud_context.provider.state = "done"
    assert execute(job, "check").state == "SUCCESS"
    assert execute(job, "check").result["status"] == "skipped"
    with cloud_context.session() as session:
        blocks = session.scalars(
            select(DocumentBlock).where(DocumentBlock.doc_id == UUID(job["doc_id"]))
        ).all()
        assert len(blocks) == len({block.block_id for block in blocks}) == (6 if kind == "xlsx" else 5)
        if kind != "pdf":
            assert all(block.bbox is None for block in blocks)


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


def test_confirmed_remote_failure_never_creates_second_batch(cloud_context):
    """显式云入口远端 failed 同样终止，不自动新建可能额外收费的批次。"""

    job = upload(cloud_context)
    execute(job)
    cloud_context.provider.state, cloud_context.provider.err_msg = (
        "failed",
        "模型服务暂时不可用",
    )
    batch_id = detail(cloud_context, job)[1]["batch_id"]
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert document["status"] == "FAILED" and current["state"] == "failed"
    assert current["batch_id"] == batch_id and not current["is_active"]
    assert execute(job).result["status"] == "skipped"
    assert cloud_context.provider.submissions == 1


@pytest.mark.parametrize(
    "code", ["A0202", "FILE_DAMAGED", "DAILY_LIMIT", "SUBMIT_UNCERTAIN"]
)
def test_permanent_error_marks_document_failed(cloud_context, code):
    """鉴权、损坏、日额度和不确定提交立即失败，不盲目循环重试或伪造结果。"""

    job = upload(cloud_context)
    cloud_context.provider.error = MinerUError(code, "需要人工处理")
    assert execute(job, expected_state="FAILURE").state == "FAILURE"
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
    assert execute(job, expected_state="FAILURE").state == "FAILURE"
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
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
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
        files={"file": ("队列.pdf", pdf_bytes(1))},
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
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert document["status"] == "FAILED" and current["error_code"] == "WRITE_FAILED"
    with cloud_context.session() as session:
        assert session.scalars(select(DocumentBlock.text)).all() == ["事务保留证据"]


def test_retry_exhaustion_and_total_deadline(cloud_context):
    """同批次查询重试耗尽后拒绝普通新提交；显式 Force 新代次仍受总等待期限约束。"""

    job = upload(cloud_context)
    execute(job)
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(job["job_id"]))
        row.retry_count = cloud_context.settings.mineru_max_retries
        session.commit()
    # 使用幂等 GET 的暂时错误检验重试上限；POST 的 5xx 属于提交不确定，不能混为一谈。
    cloud_context.provider.query_error = MinerUError(
        "HTTP_503", "临时服务错误", retryable=True
    )
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
    document, current = detail(cloud_context, job)
    assert document["status"] == "FAILED" and current["state"] == "failed"
    assert current["error_code"] == "HTTP_503" and not current["is_active"]
    assert current["retry_count"] == cloud_context.settings.mineru_max_retries
    with cloud_context.session() as session:
        document = session.get(Document, UUID(job["doc_id"]))
        with pytest.raises(MinerUError) as blocked:
            create_job(session, document, cloud_context.settings)
        assert blocked.value.code == "CACHE_REPARSE_REQUIRED"
        assert session.scalar(select(func.count()).select_from(DocumentParseJob)) == 1
        assert document.status == "FAILED" and cloud_context.provider.submissions == 1
        # 此处只模拟管理员已确认 Force Reparse，不放宽普通重投的防重复收费保护。
        next_job = create_job(
            session, document, cloud_context.settings, force_reparse=True
        )
        next_job.created_at = datetime.now(UTC) - timedelta(
            seconds=cloud_context.settings.mineru_job_timeout_seconds + 1
        )
        session.commit()
        identifier = str(next_job.job_id)
    cloud_context.provider.query_error = None
    assert execute({"job_id": identifier}, expected_state="FAILURE").state == "FAILURE"
    with cloud_context.session() as session:
        assert (
            session.get(DocumentParseJob, UUID(identifier)).error_code == "JOB_TIMEOUT"
        )
        assert session.get(Document, UUID(job["doc_id"])).status == "FAILED"
        assert session.scalar(select(func.count()).select_from(DocumentParseJob)) == 2
        assert session.scalar(select(func.count()).select_from(DocumentBlock)) == 0
    assert cloud_context.provider.submissions == cloud_context.provider.uploads == 1


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
        files={"file": ("确认丢失.pdf", pdf_bytes(1))},
    )
    assert response.status_code == 503
    ids = response.json()["detail"]
    with cloud_context.session() as session:
        row = session.get(DocumentParseJob, UUID(ids["job_id"]))
        assert row.state == "done" and row.error_code is None
        assert session.get(Document, row.doc_id).status == "CHUNKING"


def test_local_timeout_retry_api_recovers_original_uploaded_batch(cloud_context, monkeypatch):
    """隔离DB通过正常Retry恢复旧JOB_TIMEOUT；旧/重复消息不申请第二个收费批次。"""
    from app.api import admin_documents
    context = cloud_context
    monkeypatch.setattr(admin_documents, "get_settings", lambda: context.settings)
    monkeypatch.setattr(admin_documents, "publish_management", lambda *args: True)
    job = upload(context)
    execute(job)
    with context.session() as session:
        current = session.get(DocumentParseJob, UUID(job["job_id"]))
        original = current.job_id, current.task_id, current.batch_id, current.data_id
        current.created_at = datetime.now(UTC) - timedelta(seconds=context.settings.mineru_job_timeout_seconds + 10)
        session.commit()
    assert execute(job, "check", expected_state="FAILURE").state == "FAILURE"
    assert detail(context, job)[1]["error_code"] == "JOB_TIMEOUT"
    recovered = context.api("POST", f"/api/admin/documents/{job['doc_id']}/retry")
    assert recovered.status_code == 202
    with context.session() as session:
        current = session.get(DocumentParseJob, UUID(job["job_id"]))
        assert current.is_active and current.error_code is None
        assert (current.job_id, current.task_id, current.batch_id, current.data_id) == original
        assert "_local_wait_started_at" in current.parse_options
    context.provider.state = "done"
    assert execute(job, "check").result["status"] == "awaiting_chunk"
    assert execute(job, "check").result["status"] == "skipped"
    assert context.provider.submissions == context.provider.uploads == 1
