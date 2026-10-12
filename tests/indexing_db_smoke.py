"""用户显式运行的隔离 pgvector 入库验收；复用数值 Stub，不解析文档或调用真实模型。"""

import json
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

import embedding_db_smoke as embedding_tests
from app.api import admin_knowledge_bases as admin_api
from app.core.celery_app import celery_app
from app.models import Document, DocumentChunk, KnowledgeBase
from app.services import indexing
from app.services.embedding.artifacts import ArtifactStore
from app.tasks import indexing as tasks
from app.tasks.documents import DocumentProcessingError

pytest_plugins = ("embedding_db_smoke",)


@pytest.fixture
def indexed_source(context, monkeypatch):
    """在用户运行测试时用现有 GPU Stub 发布五个数值结果，再隔离 CPU 消息和会话。"""

    embedding_id = embedding_tests.submit(context)
    result = embedding_tests.execute(context, embedding_id)
    assert result.state == "SUCCESS", str(result.result)
    context.embedding_id, context.index_messages = embedding_id, []

    def accepted(*args, **kwargs):
        """检查入库检查点已提交，消息只含 doc_id，使用 CPU 队列而非 GPU。"""

        assert kwargs["queue"] == "default_queue" and kwargs["args"] == [str(context.ids[1])]
        with context.session() as session:
            document = session.get(Document, context.ids[1])
            assert str(document.index_task_id) == kwargs["task_id"] and document.status == "INDEXING"
        context.index_messages.append(kwargs)

    monkeypatch.setattr(tasks, "processing_session", context.session)
    monkeypatch.setattr(tasks.index_document, "apply_async", accepted)
    monkeypatch.setattr(indexing, "WRITE_BATCH_SIZE", 2)
    return context


def submit(context):
    """通过真实 ASGI 接口提交独立入库代次，响应不包含向量或模型/结果路径。"""

    response = context.api("POST", embedding_tests.endpoint(context, "index"))
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"doc_id", "kb_id", "index_task_id", "status"}
    return UUID(response.json()["index_task_id"])


def execute(context, task_id):
    """仅在用户运行 pytest 时 eager 执行 CPU 任务，不连接 Broker 或加载推理框架。"""

    return tasks.index_document.apply(args=[str(context.ids[1])], task_id=str(task_id), throw=False)


def vector_count(context):
    """只统计当前文档的已入库向量，避免其他隔离用例数据影响断言。"""

    with context.session() as session:
        return session.scalar(select(func.count(DocumentChunk.embedding))
                              .where(DocumentChunk.doc_id == context.ids[1]))


def test_full_transaction_provenance_ready_and_repeat_requests(indexed_source):
    """所有向量与 READY 同事务，模型来源可追踪，重复入库/切片/向量化都不重算。"""

    context, task_id = indexed_source, submit(indexed_source)
    assert vector_count(context) == 0 and len(context.index_messages) == 1
    result = execute(context, task_id)
    assert result.state == "SUCCESS", str(result.result)
    assert result.result["status"] == "ready" and result.result["dimension"] == 1024
    assert vector_count(context) == 5
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "READY" and document.error_message is None
        assert document.embedding_task_id == context.embedding_id
        assert (document.processing_task_id, document.chunk_task_id) == context.ids[2:]
        assert document.index_config["result"]["model"]["model_fingerprint"] == "f" * 64
        indexing.require_complete_vectors(session, document, 5)
        assert session.scalar(select(func.count()).select_from(DocumentChunk)
                              .where(DocumentChunk.doc_id == document.doc_id)) == 5
    assert submit(context) == task_id and len(context.index_messages) == 1
    assert execute(context, task_id).result["status"] == "skipped"
    for suffix in ("chunk", "embed"):
        response = context.api("POST", embedding_tests.endpoint(context, suffix))
        assert response.status_code == 202 and response.json()["status"] == "READY"
    detail = context.api("GET", embedding_tests.endpoint(context, "").rstrip("/"))
    assert detail.json()["index_task_id"] == str(task_id)
    assert len(context.messages) == 1 and len(context.stub.calls) == 3
    assert "vector" not in result.result and "output_dir" not in result.result


def test_second_batch_failure_rolls_back_all_updates_and_resumes(indexed_source, monkeypatch):
    """第二批失败撤销第一批写入，无半份 READY；同代次恢复不重新调用模型。"""

    context, task_id = indexed_source, submit(indexed_source)
    original, calls, events = indexing.write_batch, [], []

    def fail_second(session, document, batch):
        """注入批次失败，已经执行的第一批仍属于同一个尚未提交的事务。"""

        calls.append(len(batch))
        if len(calls) == 2:
            raise indexing.IndexingError("INDEX_INCOMPLETE")
        original(session, document, batch)

    def collect(event, **fields):
        """仅收集任务摘要，验证失败不能留下事务提交成功事件。"""

        events.append(event)

    monkeypatch.setattr(indexing, "write_batch", fail_second)
    monkeypatch.setattr(tasks, "log_event", collect)
    result = execute(context, task_id)
    assert result.state == "FAILURE" and "INDEX_INCOMPLETE" in str(result.result)
    assert vector_count(context) == 0 and "index.persisted" not in events
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "FAILED" and "INDEX_INCOMPLETE" in document.error_message
    assert context.api("POST", embedding_tests.endpoint(context, "embed")).status_code == 409
    monkeypatch.setattr(indexing, "write_batch", original)
    assert submit(context) == task_id and execute(context, task_id).state == "SUCCESS"
    assert vector_count(context) == 5 and len(context.stub.calls) == 3


@pytest.mark.parametrize("change", ["missing", "checksum", "model", "precision", "input", "empty"])
def test_corrupt_or_mismatched_source_is_failed_without_vectors(indexed_source, change):
    """文件/模型身份/冻结配置或 Chunk 变化必须安全 FAILED，不重新生成向量或写半份结果。"""

    context, task_id = indexed_source, submit(indexed_source)
    directory = ArtifactStore(context.settings.embeddings_dir).directory(context.ids[1], context.embedding_id)
    if change == "missing":
        (directory / "manifest.json").unlink()
    elif change == "checksum":
        with (directory / "vectors.jsonl").open("ab") as stream:
            stream.write(b"PRIVATE\n")
    elif change in {"model", "precision"}:
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["model"]["model_fingerprint" if change == "model" else "precision"] = "c" * 64 if change == "model" else "fp32"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        with context.session() as session, session.begin():
            rows = list(session.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == context.ids[1])))
            if change == "input":
                rows[0].text = "已变化的切片"
            else:
                for row in rows:
                    session.delete(row)
    result = execute(context, task_id)
    assert result.state == "FAILURE" and "PRIVATE" not in str(result.result)
    assert str(context.settings.embeddings_dir) not in str(result.result)
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == "FAILED"
    assert vector_count(context) == 0 and len(context.stub.calls) == 3


def test_active_index_blocks_maintenance_and_ready_maintenance_invalidates_old_task(indexed_source, monkeypatch):
    """入库期间不换输入，READY 可显式维护；旧入库消息和失败回调不能覆盖新代次。"""

    context, task_id = indexed_source, submit(indexed_source)
    assert context.api("POST", embedding_tests.endpoint(context, "reindex")).status_code == 409
    assert context.api("POST", embedding_tests.endpoint(context, "reparse") + "?force=true").status_code == 409
    assert execute(context, task_id).state == "SUCCESS"
    with context.session() as session, session.begin():
        indexing.record_index_failure(session, context.ids[1], task_id, "INDEX_FAILED", True)
        assert session.get(Document, context.ids[1]).status == "READY"

    def accepted(*args, **kwargs):
        """只确认显式缓存维护消息，不解析原件或访问真实缓存/云端。"""

        assert kwargs["operation"] == "reindex"
        return True

    monkeypatch.setattr(admin_api, "publish_document", accepted)
    assert context.api("POST", embedding_tests.endpoint(context, "reindex")).status_code == 202
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.index_task_id is None and document.index_config is None and document.status == "UPLOADED"
    assert execute(context, task_id).result["status"] == "stale_task"


def test_api_rejects_cross_kb_and_unfinished_embedding(indexed_source):
    """跨知识库请求 404，向量化未完成或无入库检查点的普通失败不创建 CPU 消息。"""

    context = indexed_source
    wrong = f"{embedding_tests.PREFIX}/{uuid4()}/documents/{context.ids[1]}/index"
    assert context.api("POST", wrong).status_code == 404
    for state in ("UPLOADED", "CHUNKING", "EMBEDDING", "FAILED"):
        with context.session() as session, session.begin():
            session.get(Document, context.ids[1]).status = state
        assert context.api("POST", embedding_tests.endpoint(context, "index")).status_code == 409
    assert context.index_messages == []


def test_publish_confirmation_loss_does_not_reset_ready(indexed_source, monkeypatch):
    """确认丢失但 Worker 已提交 READY 时返回安全 503，重试不重写向量或回退状态。"""

    context = indexed_source

    def completed_then_failed(*args, **kwargs):
        """模拟 CPU 已提交后丢失 Broker 确认，不触发真实 Redis 故障。"""

        assert execute(context, UUID(kwargs["task_id"])).state == "SUCCESS"
        raise RuntimeError("PRIVATE Redis connection")

    monkeypatch.setattr(tasks.index_document, "apply_async", completed_then_failed)
    response = context.api("POST", embedding_tests.endpoint(context, "index"))
    assert response.status_code == 503 and "PRIVATE" not in response.text
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "READY"
        task_id = document.index_task_id
    assert submit(context) == task_id and vector_count(context) == 5


@pytest.mark.parametrize("retry_count,delay", [(0, 5), (1, 10), (2, 20), (3, None)])
def test_database_retry_budget_and_safe_errors(indexed_source, monkeypatch, retry_count, delay):
    """数据库错误有限退避，最终 FAILED，记录固定原因而非 SQL/连接串。"""

    context, task_id, delays = indexed_source, submit(indexed_source), []

    def unavailable(*args, **kwargs):
        """注入临时数据库错误，不关闭或改变真实 PostgreSQL 服务。"""

        raise OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE URL"))

    def retry_stub(**kwargs):
        """捕获退避秒数，不访问 Broker 或等待执行重试。"""

        delays.append(kwargs["countdown"])
        return Retry(str(kwargs["exc"]))

    monkeypatch.setattr(tasks, "run_index_step", unavailable)
    monkeypatch.setattr(tasks.index_document, "retry", retry_stub)
    tasks.index_document.push_request(id=str(task_id), retries=retry_count)
    try:
        with pytest.raises(Retry if delay is not None else DocumentProcessingError) as error:
            tasks.index_document.run(str(context.ids[1]))
    finally:
        tasks.index_document.pop_request()
    assert "PRIVATE" not in str(error.value) and delays == ([] if delay is None else [delay])
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == ("FAILED" if delay is None else "INDEXING")


def test_missing_index_is_rejected_and_checkpoint_constraints_match_models(indexed_source, migrated_connection):
    """迁移包含有效 HNSW/cosine 部分索引；索引缺失或检查点不成对时拒绝，不自动建索引。"""

    context = indexed_source
    connection, _, schema = migrated_connection
    indexes = inspect(connection).get_indexes("document_chunks", schema=schema)
    index = next(row for row in indexes if row["name"] == indexing.INDEX_NAME)
    assert index["column_names"] == ["embedding"]
    assert index["dialect_options"]["postgresql_using"] == "hnsw"
    with context.session() as session, session.begin():
        with pytest.raises(IntegrityError), session.begin_nested():
            session.get(Document, context.ids[1]).index_task_id = uuid4()
            session.flush()
        with pytest.raises(indexing.IndexingError, match="INDEX_SCHEMA"), session.begin_nested():
            session.execute(text("DROP INDEX ix_document_chunks_embedding_hnsw"))
            # 错误退出保存点自动恢复索引，不影响其他用例的迁移结构。
            indexing.require_vector_index(session)
    assert "app.tasks.indexing" in celery_app.conf.include
    assert celery_app.conf.task_routes[tasks.index_document.name]["queue"] == "default_queue"


def test_sql_failure_preserves_old_vectors_and_retry_does_not_infer(indexed_source, monkeypatch):
    """状态提交前 SQL 失败，恢复全部旧向量；重试仅重读本地结果，不重新推理。"""

    context, task_id = indexed_source, submit(indexed_source)
    with context.session() as session, session.begin():
        for chunk in session.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == context.ids[1])):
            chunk.embedding = [0.0, 1.0] + [0.0] * 1022
    original = indexing.transition_document

    def unavailable(document, target):
        """模拟全部批次写完后数据库状态更新故障，直接调用仍由事务上下文回滚。"""

        raise OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE connection"))

    monkeypatch.setattr(indexing, "transition_document", unavailable)
    with pytest.raises(OperationalError), context.session() as session, session.begin():
        indexing.run_index_step(session, context.ids[1], task_id)
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "INDEXING" and "result" not in document.index_config
        vectors = session.scalars(select(DocumentChunk.embedding).where(DocumentChunk.doc_id == context.ids[1]))
        assert all(vector[0] == 0.0 and vector[1] == 1.0 for vector in vectors)
    monkeypatch.setattr(indexing, "transition_document", original)
    assert execute(context, task_id).state == "SUCCESS" and len(context.stub.calls) == 3


def test_other_document_vectors_and_chunk_evidence_are_unchanged(indexed_source):
    """当前文档写向量不新增 Chunk、不改变证据正文/页码/metadata，也不更新其他知识库。"""

    context = indexed_source
    with context.session() as session, session.begin():
        before = [(row.chunk_id, row.text, row.page_start, row.page_end, row.section, dict(row.chunk_metadata))
                  for row in session.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == context.ids[1])
                                             .order_by(DocumentChunk.chunk_id))]
        kb = KnowledgeBase(name=f"入库隔离-{uuid4()}")
        session.add(kb)
        session.flush()
        other = Document(kb_id=kb.kb_id, file_name="其他已有文本.txt", file_type="txt", file_hash="b" * 64,
                         file_path="unused", source_type="uploaded", status="UPLOADED")
        session.add(other)
        session.flush()
        chunk = DocumentChunk(doc_id=other.doc_id, kb_id=kb.kb_id, text="其他证据", page_start=3, page_end=3,
                              embedding=[0.0, 1.0] + [0.0] * 1022)
        session.add(chunk)
        session.flush()
        other_id = chunk.chunk_id
    assert execute(context, submit(context)).state == "SUCCESS"
    with context.session() as session:
        after = [(row.chunk_id, row.text, row.page_start, row.page_end, row.section, dict(row.chunk_metadata))
                 for row in session.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == context.ids[1])
                                            .order_by(DocumentChunk.chunk_id))]
        assert after == before
        vector = session.get(DocumentChunk, other_id).embedding
        assert vector[0] == 0.0 and vector[1] == 1.0


def test_invalid_index_checkpoint_fails_and_old_generation_is_skipped(indexed_source):
    """冻结代次被改坏必须记录失败；新任务编号使旧消息直接跳过而非写入新输入。"""

    context, task_id = indexed_source, submit(indexed_source)
    with context.session() as session, session.begin():
        document = session.get(Document, context.ids[1])
        document.index_config = document.index_config | {"embedding_task_id": str(uuid4())}
    result = execute(context, task_id)
    assert result.state == "FAILURE" and "INDEX_CHECKPOINT" in str(result.result)
    with context.session() as session, session.begin():
        document = session.get(Document, context.ids[1])
        assert document.status == "FAILED" and vector_count(context) == 0
        document.index_task_id = uuid4()
        document.status = "INDEXING"
    assert execute(context, task_id).result["status"] == "stale_task"
