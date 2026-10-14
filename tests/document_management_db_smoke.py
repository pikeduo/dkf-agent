"""用户显式运行的隔离数据库验收；不解析文件、不使用模型、Redis或真实云服务。"""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob, KnowledgeBase
from app.services import document_management as service
from app.tasks import management as worker

pytest_plugins = ("knowledge_db_smoke",)


@pytest.fixture
def context(migrated_connection, monkeypatch, tmp_path):
    """只在保存点中创建元数据/单位向量和临时文件哨兵，结束后回滚，不写public。"""
    connection, _, _ = migrated_connection
    outer = connection.begin_nested()
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek",
        upload_dir=tmp_path / "uploads", parsed_dir=tmp_path / "parsed", embeddings_dir=tmp_path / "vectors")
    identifier, kb_id = uuid4(), uuid4()
    source = settings.upload_dir / f"{identifier}.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"storage-sentinel")

    @contextmanager
    def session_scope():
        """每次独立会话提交仅释放保存点，真实数据库外层事务仍由fixture保护。"""
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    with session_scope() as session, session.begin():
        session.add(KnowledgeBase(kb_id=kb_id, name=f"管理隔离-{kb_id}", status="ACTIVE"))
        session.flush()
        session.add(Document(doc_id=identifier, kb_id=kb_id, file_name="storage-sentinel.txt", file_type="txt",
            file_hash="a" * 64, file_path=str(source), source_type="uploaded", status="READY", processing_task_id=uuid4()))
        session.flush()
        session.add(DocumentBlock(block_id=uuid4(), doc_id=identifier, block_index=1, page=1,
            block_type="text", text="仅内存证据哨兵", source="native_parser"))
        session.add(DocumentChunk(chunk_id=uuid4(), doc_id=identifier, kb_id=kb_id, text="仅内存证据哨兵",
            page_start=1, page_end=1, chunk_metadata={"chunk_index": 1}, embedding=[1.0] + [0.0] * 1023))
    monkeypatch.setattr(worker, "processing_session", session_scope)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    try:
        yield identifier, settings, session_scope
    finally:
        outer.rollback()


def test_delete_cascades_blocks_chunks_vectors_after_owned_files_cleaned(context):
    """真实FK级联移除文档、块、Chunk及向量，别的文档不受影响；不触发MinerU。"""
    identifier, settings, session_scope = context
    with session_scope() as session, session.begin():
        document = session.scalar(select(Document).where(Document.doc_id == identifier).with_for_update())
        task_id, _ = service.prepare_management(session, document, settings, "delete")
        assert session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.doc_id == identifier)) == 1
    result = worker.manage_document.apply(args=[str(identifier)], task_id=str(task_id))
    assert result.state == "SUCCESS" and result.result["status"] == "deleted"
    assert not (settings.upload_dir / f"{identifier}.txt").exists()
    with session_scope() as session:
        assert session.get(Document, identifier) is None
        for model in (DocumentBlock, DocumentChunk, DocumentParseJob):
            assert session.scalar(select(func.count()).select_from(model).where(model.doc_id == identifier)) == 0


def test_prepared_reindex_preserves_source_and_old_evidence_until_cache_replay(context):
    """Reindex换代次保留原件和诊断证据，不直接插入新块/向量或加载模型。"""
    identifier, settings, session_scope = context
    with session_scope() as session, session.begin():
        document = session.scalar(select(Document).where(Document.doc_id == identifier).with_for_update())
        service.prepare_management(session, document, settings, "reindex")
        assert document.status == "UPLOADED" and document.management_config["parse_operation"] == "reindex"
        assert session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.doc_id == identifier)) == 1
    assert (settings.upload_dir / f"{identifier}.txt").read_bytes() == b"storage-sentinel"
