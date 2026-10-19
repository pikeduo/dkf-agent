"""用户显式执行的隔离数据库切片验收，不创建原件、不解析文件、不访问云端或 Redis。"""

from contextlib import contextmanager
from uuid import UUID, uuid4
import pytest
from celery.exceptions import Retry
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import admin_knowledge_bases as admin_api
from app.core.config import Settings
from app.core.celery_app import celery_app
from app.models import Document, DocumentBlock, DocumentChunk, KnowledgeBase
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.chunk import processing
from app.services.document_processing import persist_blocks
from app.tasks import chunks as tasks
from app.tasks.documents import DocumentProcessingError

pytest_plugins = ("knowledge_admin_smoke",)
PREFIX = "/api/admin/knowledge-bases"


@pytest.fixture
def context(api, migrated_connection, monkeypatch):
    """只插入 Block Stub，绑定独立事务会话并收集任务消息，隔离真实 public 数据。"""

    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session, session.begin():
        kb = KnowledgeBase(name=f"切片验收-{uuid4()}")
        session.add(kb)
        session.flush()
        document = Document(kb_id=kb.kb_id, file_name="已有资料.xlsx", file_type="xlsx",
            file_hash="a" * 64, file_path="unused", source_type="uploaded", status="CHUNKING",
            processing_task_id=uuid4())
        session.add(document)
        session.flush()
        for index, kind, text in [(1, "title", "收入说明"), (2, "table", "收入\n=SUM(A1:A10)"), (3, "formula", "=B2/C2")]:
            session.add(DocumentBlock(doc_id=document.doc_id, block_index=index, page=1,
                section="收入Sheet", block_type=kind, text=text, source="native_parser"))
        ids = document.kb_id, document.doc_id, document.processing_task_id
    messages = []

    @contextmanager
    def isolated_session():
        """每个 Worker 步骤使用保存点会话，提交不影响夹具最外层回滚。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    def accepted(*args, **kwargs):
        """检查发送时检查点已经可读，参数仅含 doc_id，不带原文或连接串。"""

        with isolated_session() as session:
            row = session.get(Document, UUID(kwargs["args"][0]))
            assert str(row.chunk_task_id) == kwargs["task_id"]
        messages.append(kwargs)

    monkeypatch.setattr(tasks, "processing_session", isolated_session)
    monkeypatch.setattr(tasks.chunk_document, "apply_async", accepted)
    monkeypatch.setattr(admin_api, "get_settings", settings)
    return api, ids, isolated_session, messages


def settings():
    """提供不读取真实 .env 的切片配置，测试不获取云 Token 或本机密钥。"""

    return Settings(_env_file=None, chunk_size=600, chunk_overlap=80)


def endpoint(context, suffix="chunk"):
    """组装限定知识库与文档的接口路径。"""

    _, (kb_id, doc_id, _), _, _ = context
    return f"{PREFIX}/{kb_id}/documents/{doc_id}/{suffix}"


def submit(context):
    """创建或恢复切片检查点，返回独立任务标识。"""

    response = context[0]("POST", endpoint(context))
    assert response.status_code == 202, response.text
    return UUID(response.json()["chunk_task_id"])


def execute(context, task_id):
    """仅由用户运行 pytest 时执行 eager 切片，不启动 Worker 或调用 Broker。"""

    return tasks.chunk_document.apply(args=[str(context[1][1])], task_id=str(task_id), throw=False)


def test_chunk_request_execution_trace_and_duplicate(context):
    """请求、执行、查询证据及重复请求幂等，原始公式保留且所有 embedding 为空。"""

    task_id = submit(context)
    result = execute(context, task_id)
    assert result.state == "SUCCESS", str(result.result)
    assert result.result["status"] == "awaiting_embedding"
    response = context[0]("GET", endpoint(context, "chunks"), params={"limit": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1 and len(data["items"]) == 1
    with context[2]() as session:
        document = session.get(Document, context[1][1])
        assert document.status == "EMBEDDING" and document.processing_task_id == context[1][2]
        assert document.error_message is None
        assert all(row.embedding is None for row in session.scalars(select(DocumentChunk)))
        for chunk in session.scalars(select(DocumentChunk)):
            assert (chunk.doc_id, chunk.kb_id) == (document.doc_id, document.kb_id)
            for span in chunk.chunk_metadata["source_spans"]:
                source = session.get(DocumentBlock, UUID(span["block_id"]))
                assert source.text[span["start_char"]:span["end_char"]] == chunk.text[span["chunk_start_char"]:span["chunk_end_char"]]
    assert "=SUM(A1:A10)" in data["items"][0]["text"]
    assert submit(context) == task_id and len(context[3]) == 1
    assert execute(context, task_id).result["status"] == "skipped"


def test_waiting_request_reuses_checkpoint_and_freezes_options(context, monkeypatch):
    """队列未消费时重复请求保持相同代次，即使部署配置改变也不修改冻结参数。"""

    task_id = submit(context)

    def changed_settings():
        """模拟 API 重启后的新默认值，既有检查点仍应使用原参数。"""

        return Settings(_env_file=None, chunk_size=700, chunk_overlap=100)

    monkeypatch.setattr(admin_api, "get_settings", changed_settings)
    assert submit(context) == task_id
    assert len(context[3]) == 2
    with context[2]() as session:
        assert session.get(Document, context[1][1]).chunk_config["options"]["size"] == 600
    assert execute(context, task_id).state == "SUCCESS"


def test_old_generation_is_skipped(context):
    """旧消息不能覆盖新解析代次或清除旧结果。"""

    task_id = submit(context)
    with context[2]() as session, session.begin():
        document = session.get(Document, context[1][1])
        document.chunk_task_id, document.chunk_config = None, None
        document.processing_task_id, document.status = uuid4(), "UPLOADED"
    assert execute(context, task_id).result["status"] == "stale_task"


def test_failure_rolls_back_chunks_and_can_resume_same_task(context, monkeypatch):
    """v6批量替换过程中失败全回滚，旧结果保留，修复后可恢复同一切片任务。"""

    from app.services.chunk import streaming
    task_id = submit(context)
    original = streaming.iter_chunks
    with context[2]() as session, session.begin():
        old = DocumentChunk(doc_id=context[1][1], kb_id=context[1][0], text="旧切片",
                            page_start=1, page_end=1)
        session.add(old)
        session.flush()
        old_id = old.chunk_id

    def broken_drafts(*args, **kwargs):
        """模拟生成合法结果后落库类型错误，验证 DELETE 后异常也回滚。"""

        for draft in original(*args, **kwargs):
            draft.metadata["unserializable"] = object()
            yield draft

    monkeypatch.setattr(streaming, "iter_chunks", broken_drafts)
    result = execute(context, task_id)
    assert result.state == "FAILURE" and "CHUNK_FAILED" in str(result.result)
    assert "object" not in str(result.result)
    with context[2]() as session:
        assert session.get(Document, context[1][1]).status == "FAILED"
        assert session.get(DocumentChunk, old_id) is not None
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 1
    monkeypatch.setattr(streaming, "iter_chunks", original)
    assert submit(context) == task_id
    assert execute(context, task_id).state == "SUCCESS"
    with context[2]() as session:
        assert session.get(DocumentChunk, old_id) is None


@pytest.mark.parametrize("retry_count,delay", [(0, 5), (1, 10), (2, 20), (3, None)])
def test_temporary_database_retry_budget_and_safe_error(context, monkeypatch, retry_count, delay):
    """临时错误只使用有限退避，最终安全失败不泄露 SQL、密码或文档路径。"""

    task_id = submit(context)
    delays = []

    def database_failure(*args, **kwargs):
        """向步骤注入私密异常原文，任务对外只可展示固定安全错误。"""

        raise OperationalError("SECRET SQL", {}, RuntimeError("SECRET password/path"))

    def retry_stub(**kwargs):
        """捕获退避参数，不发送消息，不访问 Broker。"""

        delays.append(kwargs["countdown"])
        return Retry(str(kwargs["exc"]))

    monkeypatch.setattr(tasks, "run_chunk_step", database_failure)
    monkeypatch.setattr(tasks.chunk_document, "retry", retry_stub)
    tasks.chunk_document.push_request(id=str(task_id), retries=retry_count)
    try:
        with pytest.raises(Retry if delay is not None else DocumentProcessingError) as error:
            tasks.chunk_document.run(str(context[1][1]))
    finally:
        tasks.chunk_document.pop_request()
    assert "SECRET" not in str(error.value)
    assert delays == ([] if delay is None else [delay])
    with context[2]() as session:
        document = session.get(Document, context[1][1])
        assert document.status == ("FAILED" if delay is None else "CHUNKING")
        assert "CHUNK_DATABASE" in document.error_message


def test_publish_confirmation_lost_does_not_overwrite_completed_state(context, monkeypatch):
    """Worker 已提交后发送端报确认失败，不把 EMBEDDING 或已完成 Chunk 改成 FAILED。"""

    def accepted_but_confirmation_lost(*args, **kwargs):
        """在消息返回之前执行本地切片，再模拟 Broker 确认丢失。"""

        result = execute(context, UUID(kwargs["task_id"]))
        assert result.state == "SUCCESS", str(result.result)
        raise RuntimeError("SECRET broker URL")

    monkeypatch.setattr(tasks.chunk_document, "apply_async", accepted_but_confirmation_lost)
    response = context[0]("POST", endpoint(context))
    assert response.status_code == 503 and "SECRET" not in response.text
    with context[2]() as session:
        assert session.get(Document, context[1][1]).status == "EMBEDDING"
    assert context[0]("POST", endpoint(context)).status_code == 202


def test_new_blocks_invalidate_chunks_only_after_successful_transaction(context):
    """解析替换与缓存回放成功清旧 Chunk，失败保留全部旧结果及切片检查点。"""

    task_id = submit(context)
    assert execute(context, task_id).state == "SUCCESS"
    with context[2]() as session:
        document = session.get(Document, context[1][1])
        parsed = ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type=document.file_type,
            blocks=[ParsedBlock(block_id=uuid4(), page=1, block_type="table", text="新 =SUM(A1:A10)", source="native_parser")])
        with pytest.raises(RuntimeError), session.begin_nested():
            persist_blocks(session, document, parsed)
            raise RuntimeError("模拟事务回滚")
        assert document.chunk_task_id == task_id
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) > 0
        persist_blocks(session, document, parsed)
        assert document.chunk_task_id is None and document.chunk_config is None
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_invalid_source_order_fails_without_new_chunks(context):
    """未知历史序号必须安全失败，不生成 Chunk 或云任务，不伪造原文顺序。"""

    with context[2]() as session, session.begin():
        source = session.scalar(select(DocumentBlock).where(DocumentBlock.block_index == 1))
        source.block_index = None
    result = execute(context, submit(context))
    assert result.state == "FAILURE" and "CHUNK_SOURCE_ORDER" in str(result.result)
    with context[2]() as session:
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_cross_kb_and_incomplete_parse_rejected(context):
    """错误知识库 404，解析未完成或失败且无切片检查点 409，不投递任务。"""

    path = f"{PREFIX}/{uuid4()}/documents/{context[1][1]}/chunks"
    assert context[0]("GET", path).status_code == 404
    for status in ("UPLOADED", "PARSING", "FAILED"):
        with context[2]() as session, session.begin():
            session.get(Document, context[1][1]).status = status
        assert context[0]("POST", endpoint(context)).status_code == 409
    assert context[3] == []


def test_cpu_task_is_registered():
    """新任务注册到已有 CPU 队列，不引入 GPU 模型或新队列。"""

    assert "app.tasks.chunks" in celery_app.conf.include
    assert celery_app.conf.task_routes[tasks.chunk_document.name]["queue"] == "default_queue"


def test_same_blocks_replayed_with_new_parse_generation_keep_chunk_ids(context):
    """模拟缓存回放同一 Block 而非重新解析，切片 ID 不依赖本地解析/切片任务代次。"""

    first_task = submit(context)
    assert execute(context, first_task).state == "SUCCESS"
    with context[2]() as session, session.begin():
        document = session.get(Document, context[1][1])
        first_ids = set(session.scalars(select(DocumentChunk.chunk_id)))
        inputs = list(session.scalars(select(DocumentBlock).order_by(DocumentBlock.block_index)))
        parsed = ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type=document.file_type,
            blocks=[ParsedBlock(block_id=source.block_id, page=source.page, section=source.section,
                block_type=source.block_type, text=source.text, source=source.source, bbox=source.bbox)
                for source in inputs])
        persist_blocks(session, document, parsed)
        document.processing_task_id, document.status = uuid4(), "CHUNKING"
    second_task = submit(context)
    assert second_task != first_task and execute(context, second_task).state == "SUCCESS"
    with context[2]() as session:
        assert set(session.scalars(select(DocumentChunk.chunk_id))) == first_ids


def test_maintenance_cannot_override_waiting_chunk_but_allows_after_completion(context, monkeypatch):
    """活动切片拒绝维护，完成后 Reindex 建立新解析代次且只投递缓存回放操作。"""

    task_id = submit(context)
    assert context[0]("POST", endpoint(context, "reindex")).status_code == 409
    assert context[0]("POST", endpoint(context, "reparse") + "?force=true").status_code == 409
    assert execute(context, task_id).state == "SUCCESS"
    operations = []

    def accepted_maintenance(session, doc_id, processing_id, **kwargs):
        """只捕获消息，不运行 Parser/Cache/云端，检查维护检查点已提交并清除切片代次。"""

        assert not session.in_transaction()
        operations.append(kwargs["operation"])
        return True

    monkeypatch.setattr(admin_api, "publish_document", accepted_maintenance)
    response = context[0]("POST", endpoint(context, "reindex"))
    assert response.status_code == 202 and operations == ["reindex"]
    with context[2]() as session:
        document = session.get(Document, context[1][1])
        assert document.status == "UPLOADED" and document.chunk_task_id is None
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) > 0
