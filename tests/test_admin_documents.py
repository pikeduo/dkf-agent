"""文档管理API的内存ASGI用例；只用数据库/消息Stub，无真实上传或业务处理。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.api import admin_documents as api
from app.api import admin_knowledge_bases as legacy
from app.core.config import Settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob


class SessionStub:
    """记录真实SQL表达式但不执行SQL，输出只来自内存元数据。"""

    def __init__(self, document):
        """保存隔离当前文档、查询记录和阶段证据。"""
        self.document, self.statements, self.rows = document, [], {}
        self.commits = self.rollbacks = 0
        self.lock_error = False

    def scalar(self, statement):
        """按SQL实体返回文档或计数，必要时模拟行锁占用。"""
        self.statements.append(statement)
        if self.lock_error:
            raise OperationalError("PRIVATE_SQL", {}, SimpleNamespace(sqlstate="55P03"))
        description = statement.column_descriptions[0]
        if description.get("entity") is Document and description.get("expr") is Document:
            return self.document
        if description.get("entity") is DocumentParseJob:
            return None
        model = description.get("entity")
        if model is None:
            tables = {DocumentBlock.__tablename__: DocumentBlock, DocumentChunk.__tablename__: DocumentChunk}
            model = tables.get(statement.get_final_froms()[0].name)
        return len(self.rows.get(model, []))

    def scalars(self, statement):
        """提供已排序的内存证据；SQL排序和范围由独立断言核对。"""
        self.statements.append(statement)
        return iter(self.rows.get(statement.column_descriptions[0]["entity"], []))

    def execute(self, statement):
        """模拟一次SQL快照返回统计，不计算真实向量。"""
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: (self.document, 2, 3, 3, 0))

    def flush(self):
        """在内存保存准备结果，不写数据库。"""
        return None

    def commit(self):
        """记录检查点提交必须先于消息投递。"""
        self.commits += 1

    def rollback(self):
        """记录安全回滚，不影响共用数据库。"""
        self.rollbacks += 1


def request(app, method, path):
    """用户运行时调用内存ASGI，不启动API服务或进行实际联网。"""
    async def run():
        """单次请求结束即关闭Mock客户端。"""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path)
    return asyncio.run(run())


@pytest.fixture
def context(monkeypatch, tmp_path):
    """独立应用/会话/配置，旧业务入口仍可以证明删除屏障。"""
    now = datetime.now(UTC)
    identifier = uuid4()
    document = Document(doc_id=identifier, kb_id=uuid4(), file_name="已准备.txt", file_type="txt",
        file_hash="a" * 64, file_path=str(tmp_path / "upload" / f"{identifier}.txt"), source_type="uploaded",
        status="READY", error_message=None, processing_task_id=uuid4(), chunk_task_id=None, chunk_config=None,
        embedding_task_id=None, embedding_config=None, index_task_id=None, index_config=None,
        management_task_id=None, management_config=None, created_at=now, updated_at=now)
    session = SessionStub(document)
    settings = Settings(_env_file=None, llm_provider="deepseek", deepseek_provider=None,
        upload_dir=tmp_path / "upload", parsed_dir=tmp_path / "parsed", embeddings_dir=tmp_path / "vectors")
    app = FastAPI()
    app.include_router(api.router)

    def session_dependency():
        """每个请求都使用封闭Stub，不创建真实连接。"""
        yield session

    calls = []

    def publish(doc_id, task_id):
        """只记录消息，检查投递前已提交，不运行Celery任务。"""
        assert session.commits > 0
        calls.append((doc_id, task_id))
        return True

    app.dependency_overrides[legacy.get_admin_session] = session_dependency
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "publish_management", publish)
    return SimpleNamespace(app=app, session=session, document=document, calls=calls,
                           url=f"/api/admin/documents/{identifier}")


def test_detail_and_status_hide_private_configuration_and_use_current_vector_counts(context):
    """详情/状态能观察原任务及数量，但无私有路径、向量数组、配置或签名URL。"""
    detail = request(context.app, "GET", context.url)
    assert detail.status_code == 200 and detail.json()["doc_id"] == str(context.document.doc_id)
    assert detail.json()["parse_jobs"] == [] and detail.json()["management"] is None
    status = request(context.app, "GET", context.url + "/status")
    assert status.status_code == 200
    assert (status.json()["block_count"], status.json()["chunk_count"], status.json()["vector_count"]) == (2, 3, 3)
    assert status.json()["invalid_vector_count"] == 0
    for response in (detail, status):
        assert "file_path" not in response.json() and "embedding_config" not in response.json()
        assert str(context.document.file_path) not in response.text


def test_failed_parse_is_explicit_in_status_and_detail_even_with_old_counts(context):
    """旧块/向量数量不能掩盖当前解析失败；详情和状态直接展示FAILED及安全具体原因。"""
    context.document.status = "FAILED"
    message = context.document.error_message = "-60006：文件解析失败：MinerU 文件页数超过 200 页"
    for suffix in ("", "/status"):
        response = request(context.app, "GET", context.url + suffix)
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED" and response.json()["error_message"] == message
    assert "200 页" in response.json()["error_message"] and response.json()["vector_count"] == 3


def test_detail_exposes_cloud_checkpoint_booleans_without_private_urls_or_options(context):
    """可观察batch/上传/申请状态，不能把私有Token、签名URL或Provider参数泄漏给前端。"""
    now = datetime.now(UTC)
    context.session.rows[DocumentParseJob] = [SimpleNamespace(
        job_id=uuid4(), doc_id=context.document.doc_id, provider="mineru_cloud", model_version="vlm",
        data_id=str(uuid4()), batch_id="existing-batch", task_id=context.document.processing_task_id,
        trace_id=None, state="done", retry_count=0, error_code=None, error_message=None, is_active=False,
        submitted_at=now, finished_at=now, created_at=now, updated_at=now,
        upload_complete=True, submission_attempted=True, upload_url="PRIVATE_SIGNED_URL",
        parse_options={"token": "PRIVATE_KEY"})]
    response = request(context.app, "GET", context.url)
    assert response.status_code == 200
    job = response.json()["parse_jobs"][0]
    assert job["batch_id"] == "existing-batch" and job["upload_complete"] is True and job["submission_attempted"] is True
    assert "PRIVATE" not in response.text and "upload_url" not in job and "parse_options" not in job


def test_blocks_and_chunks_have_stable_sql_scope_and_keep_original_evidence(context):
    """所有分页有文档/知识库范围和稳定顺序，响应保留原页/bbox/source_spans且无向量。"""
    doc = context.document
    block = DocumentBlock(block_id=uuid4(), doc_id=doc.doc_id, block_index=1, page=2, section="原章节",
        block_type="table", text="仅内存证据", bbox=[1, 2, 3, 4], source="native_parser", confidence=1.0)
    chunk = DocumentChunk(chunk_id=uuid4(), doc_id=doc.doc_id, kb_id=doc.kb_id, text=block.text,
        page_start=2, page_end=2, section=block.section, chunk_metadata={"chunk_index": 1,
            "source_spans": [{"block_id": str(block.block_id), "page": 2}]}, created_at=doc.created_at)
    context.session.rows = {DocumentBlock: [block], DocumentChunk: [chunk]}
    blocks = request(context.app, "GET", context.url + "/blocks?limit=10&offset=0")
    chunks = request(context.app, "GET", context.url + "/chunks?limit=10&offset=0")
    assert blocks.status_code == chunks.status_code == 200
    assert blocks.json()["total"] == chunks.json()["total"] == 1
    assert blocks.json()["items"][0]["bbox"] == [1, 2, 3, 4]
    assert chunks.json()["items"][0]["metadata"] == chunk.chunk_metadata
    assert "embedding" not in chunks.json()["items"][0]
    sql = "\n".join(str(statement.compile(dialect=postgresql.dialect())) for statement in context.session.statements)
    assert "WHERE document_blocks.doc_id" in sql and "document_chunks.kb_id" in sql
    assert "block_index ASC NULLS LAST" in sql and "chunk_id" in sql


@pytest.mark.parametrize("method,suffix", [("POST", "/reindex"), ("POST", "/reparse?force=true"), ("DELETE", "")])
def test_new_maintenance_is_async_and_commit_precedes_dispatch(context, method, suffix):
    """三个维护入口202只建立检查点，原件不删除/解析、不调用模型或云服务。"""
    response = request(context.app, method, context.url + suffix)
    assert response.status_code == 202 and len(context.calls) == 1
    data = response.json()
    assert data["management_task_id"] == str(context.document.management_task_id)
    assert data["state"] == "running"
    assert "cleanup_roots" not in response.text and "file_path" not in response.text
    repeated = request(context.app, method, context.url + suffix)
    assert repeated.status_code == 202 and repeated.json()["management_task_id"] == data["management_task_id"]
    assert len(context.calls) == 1


def test_reparse_requires_explicit_force_and_never_reaches_dispatch_without_it(context):
    """省略/false的Force400，不改变文档、不构造云任务。"""
    for suffix in ("/reparse", "/reparse?force=false"):
        assert request(context.app, "POST", context.url + suffix).status_code == 400
    assert context.calls == [] and context.document.status == "READY"


def test_delete_pending_blocks_legacy_mutating_routes_and_retry_preserves_checkpoint(context):
    """删除接收后原文档不可检索，旧process/chunk/embed等不能重新激活它。"""
    response = request(context.app, "DELETE", context.url)
    assert response.status_code == 202 and context.document.status == "FAILED"
    with pytest.raises(HTTPException) as rejected:
        legacy.require_document(context.session, context.document.kb_id, context.document.doc_id, lock=True)
    assert rejected.value.status_code == 409
    retry = request(context.app, "POST", context.url + "/retry")
    assert retry.status_code == 202 and retry.json()["management_task_id"] == response.json()["management_task_id"]


def test_busy_row_returns_409_not_database_503(context):
    """NOWAIT占用准确返回冲突，异常中的SQL/底层文本不能进入HTTP。"""
    context.session.lock_error = True
    response = request(context.app, "POST", context.url + "/reindex")
    assert response.status_code == 409 and "PRIVATE" not in response.text
    assert context.calls == [] and context.session.rollbacks == 1


def test_dispatch_failure_preserves_safe_failed_management_checkpoint(context, monkeypatch):
    """消息确认失败503且管理检查点保存，不能返回202冒充已经执行；原件与业务代次保留。"""
    from contextlib import nullcontext
    monkeypatch.setattr(api, "publish_management", lambda *args: False)
    monkeypatch.setattr(context.session, "begin", lambda: nullcontext(), raising=False)
    response = request(context.app, "POST", context.url + "/reindex")
    assert response.status_code == 503 and "MANAGEMENT_DISPATCH" in response.text
    assert context.document.management_config["state"] == "failed"
    assert context.document.file_hash == "a" * 64


def test_corrupt_private_management_checkpoint_returns_safe_conflict(context):
    """不能把坏私有配置序列化进HTTP或悄悄当成无管理任务。"""
    context.document.management_task_id = uuid4()
    context.document.management_config = {"PRIVATE_KEY": "PRIVATE_URL"}
    response = request(context.app, "GET", context.url)
    assert response.status_code == 409 and "MANAGEMENT_CHECKPOINT" in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("suffix", ["", "/status", "/blocks", "/chunks"])
def test_missing_document_returns_404_and_bad_id_or_page_window_returns_422(context, suffix):
    """不存在文档与非法标识/分页明确区分，不发布空结果假装文档存在。"""
    context.session.document = None
    assert request(context.app, "GET", context.url + suffix).status_code == 404
    assert request(context.app, "GET", "/api/admin/documents/not-a-uuid" + suffix).status_code == 422
    if suffix in {"/blocks", "/chunks"}:
        assert request(context.app, "GET", context.url + suffix + "?limit=101").status_code == 422
