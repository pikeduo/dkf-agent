"""用户执行的只读 ORM Adapter 验收；隔离 schema，不创建第二套存储或真实文档样本。"""

from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, load_only

pytest.importorskip("llama_index.core.schema", reason="用户需先手动安装已确认的 llama-index-core")

from llama_index.core.schema import NodeWithScore

from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result
from app.services.rag.llama_index.validation import RagAdapterError

pytest_plugins = ("knowledge_db_smoke",)


@pytest.fixture
def rows(migrated_connection):
    """在保存点中模拟已有业务记录，由用户运行时创建，退出后回滚不写 public。"""

    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        kb = KnowledgeBase(name=f"Adapter 隔离-{uuid4()}")
        session.add(kb)
        session.flush()
        document = Document(kb_id=kb.kb_id, file_name="已有记录.txt", file_type="txt", file_hash="a" * 64,
                            file_path="PRIVATE_DO_NOT_READ", source_type="uploaded", status="EMBEDDING")
        session.add(document)
        session.flush()
        chunk = DocumentChunk(kb_id=kb.kb_id, doc_id=document.doc_id, text="已有数据库正文",
                              page_start=1, page_end=1, section="已有章节", embedding=None,
                              chunk_metadata={"page_semantics": "logical", "source_spans": []})
        session.add(chunk)
        session.flush()
        try:
            yield session, document, chunk
        finally:
            # 关闭当前保存点而不是提交，模拟记录不会残留到模块中的下一个用例。
            session.rollback()


def test_conversion_issues_no_sql_and_does_not_modify_database_truth(rows, migrated_connection):
    """转换前读取字段；转换本身不发 SQL、不 autoflush、不增加记录或触发文档处理。"""

    session, document, chunk = rows
    connection, _, _ = migrated_connection
    before = session.scalar(select(func.count()).select_from(DocumentChunk))
    # 显式读取数据库默认值，避免未加载字段被 Adapter 猜测或触发隐式读取。
    session.refresh(document)
    session.refresh(chunk)
    statements = []

    def collect(conn, cursor, statement, parameters, context, executemany):
        """只计数 SQL，不记录语句正文/连接串，也不执行查询或数据库变更。"""

        statements.append(True)

    event.listen(connection, "before_cursor_execute", collect)
    try:
        node = chunk_to_node(chunk, document)
        result = node_to_result(NodeWithScore(node=node, score=0.0))
        assert result.chunk_id == chunk.chunk_id and result.doc_id == document.doc_id
        assert result.metadata == chunk.chunk_metadata and result.section == "已有章节"
        assert statements == [] and not session.dirty and not session.new
    finally:
        event.remove(connection, "before_cursor_execute", collect)
    assert session.scalar(select(func.count()).select_from(DocumentChunk)) == before
    assert document.status == "EMBEDDING" and document.processing_task_id is None and chunk.embedding is None


def test_unloaded_fields_do_not_trigger_lazy_select_or_autoflush(rows, migrated_connection):
    """未加载正文必须失败，不能在转换时隐式读取原件/数据库或提交其他待保存数据。"""

    session, document, chunk = rows
    identifier = chunk.chunk_id
    session.expunge(chunk)
    partial = session.scalar(select(DocumentChunk).options(load_only(DocumentChunk.chunk_id))
                             .where(DocumentChunk.chunk_id == identifier))
    session.refresh(document)
    connection, _, _ = migrated_connection
    statements = []

    def collect(conn, cursor, statement, parameters, context, executemany):
        """捕获转换过程中是否出现任何 SQL，不输出原始语句。"""

        statements.append(True)

    event.listen(connection, "before_cursor_execute", collect)
    try:
        with pytest.raises(RagAdapterError, match="RAG_ADAPTER_UNLOADED"):
            chunk_to_node(partial, document)
        assert statements == []
    finally:
        event.remove(connection, "before_cursor_execute", collect)
