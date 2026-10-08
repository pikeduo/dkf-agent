"""显式执行的 PostgreSQL 模型/迁移验收；仅操作事务内的独立 schema。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import PROJECT_ROOT
from app.db.seed import DEFAULT_KB_ID, ensure_default_knowledge_base
from app.db.session import get_engine
from app.models import Document, DocumentBlock, DocumentChunk, KnowledgeBase


@pytest.fixture(scope="module")
def migrated_connection():
    """在事务内创建隔离 schema 并迁移，提供连接、配置和 schema 名，结束后全部回滚。"""

    engine = get_engine()
    schema = f"dkf_test_{uuid4().hex}"
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield connection, config, schema
        finally:
            # PostgreSQL 支持事务 DDL，回滚同时移除测试 schema，不删除项目表或扩展。
            transaction.rollback()


@pytest.fixture
def session(migrated_connection):
    """为每个用例提供基于保存点的会话，结束后回滚数据且不影响外层迁移事务。"""

    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as db:
        yield db
        db.rollback()


def make_document(session, kb_id, **overrides):
    """创建并刷新指定知识库的测试文档，允许覆盖字段以验证约束，但不提交事务。"""

    values = {
        "kb_id": kb_id,
        "file_name": "sample.pdf",
        "file_type": "pdf",
        "file_hash": uuid4().hex * 2,
        "file_path": "test/sample.pdf",
        "source_type": "preset",
    }
    values.update(overrides)
    document = Document(**values)
    session.add(document)
    session.flush()
    return document


def test_migration_matches_models_and_has_expected_schema(migrated_connection):
    """验证迁移后的表、修订号及 vector 扩展符合预期，且模型与数据库结构无差异。"""

    connection, config, schema = migrated_connection
    assert set(inspect(connection).get_table_names(schema=schema)) == {
        "alembic_version",
        "knowledge_bases",
        "documents",
        "document_blocks",
        "document_chunks",
    }
    command.check(config)
    assert (
        connection.scalar(text("SELECT version_num FROM alembic_version"))
        == "0001_knowledge_base"
    )
    assert connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
    )


def test_seed_is_idempotent_and_does_not_overwrite_existing_state(session):
    """验证默认知识库重复初始化不新增记录，也不覆盖已修改的描述。"""

    first = ensure_default_knowledge_base(session)
    assert first.kb_id == DEFAULT_KB_ID
    first.description = "保留已有说明"
    session.flush()
    second = ensure_default_knowledge_base(session)
    assert second.kb_id == first.kb_id
    assert second.description == "保留已有说明"
    assert session.scalar(select(func.count()).select_from(KnowledgeBase)) == 1


def test_hash_dedup_is_per_knowledge_base(session):
    """验证相同文件哈希可跨知识库保存，但在同一知识库重复插入会触发唯一约束。"""

    first = KnowledgeBase(name="第一个知识库")
    second = KnowledgeBase(name="第二个知识库")
    session.add_all([first, second])
    session.flush()
    file_hash = "a" * 64
    make_document(session, first.kb_id, file_hash=file_hash)
    make_document(session, second.kb_id, file_hash=file_hash)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_document(session, first.kb_id, file_hash=file_hash)


def test_chunk_cannot_reference_another_knowledge_base(session):
    """验证复合外键拒绝文档与知识库归属不一致的 Chunk，防止证据跨库关联。"""

    first = KnowledgeBase(name="文档所属库")
    second = KnowledgeBase(name="错误的库")
    session.add_all([first, second])
    session.flush()
    document = make_document(session, first.kb_id)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            DocumentChunk(
                doc_id=document.doc_id,
                kb_id=second.kb_id,
                text="证据",
                page_start=1,
                page_end=1,
            )
        )
        session.flush()


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_type": "unknown"},
        {"status": "unknown"},
        {"file_hash": "bad"},
    ],
)
def test_document_checks(session, overrides):
    """逐项传入非法来源、状态或哈希，验证文档检查约束拒绝对应记录。"""

    kb = ensure_default_knowledge_base(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_document(session, kb.kb_id, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"page": 0},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"block_type": "unknown"},
    ],
)
def test_block_checks(session, overrides):
    """逐项传入非法页码、置信度或块类型，验证解析块检查约束拒绝对应记录。"""

    kb = ensure_default_knowledge_base(session)
    document = make_document(session, kb.kb_id)
    values = {
        "doc_id": document.doc_id,
        "page": 1,
        "block_type": "text",
        "text": "正文",
        "source": "native",
        "confidence": 0.9,
    }
    values.update(overrides)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(DocumentBlock(**values))
        session.flush()


def test_vector_json_roundtrip_and_delete_cascade(session):
    """验证向量维度、JSON 读写及页码约束，并确认删除知识库会级联删除下属数据。"""

    kb = ensure_default_knowledge_base(session)
    document = make_document(session, kb.kb_id)
    block = DocumentBlock(
        doc_id=document.doc_id,
        page=1,
        block_type="text",
        text="正文",
        source="native",
        bbox=[0, 0, 10, 10],
    )
    chunk = DocumentChunk(
        doc_id=document.doc_id,
        kb_id=kb.kb_id,
        text="证据",
        page_start=1,
        page_end=1,
        chunk_metadata={"section": "简介"},
        embedding=[1.0] + [0.0] * 1023,
    )
    session.add_all([block, chunk])
    session.flush()
    session.expire_all()
    assert session.get(DocumentChunk, chunk.chunk_id).chunk_metadata == {
        "section": "简介"
    }
    assert session.scalar(select(func.vector_dims(DocumentChunk.embedding))) == 1024
    chunk.chunk_metadata["page"] = 1
    session.flush()
    session.expire_all()
    assert chunk.chunk_metadata["page"] == 1
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            DocumentChunk(
                doc_id=document.doc_id,
                kb_id=kb.kb_id,
                text="非法页码",
                page_start=2,
                page_end=1,
            )
        )
        session.flush()
    session.delete(kb)
    session.flush()
    for model in (Document, DocumentBlock, DocumentChunk):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_updated_at_is_maintained_for_direct_sql(session):
    """通过直接 SQL 更新知识库，验证触发器也能维护更新时间而不依赖 ORM。"""

    kb = KnowledgeBase(name="更新时间验证", updated_at=datetime(2000, 1, 1, tzinfo=UTC))
    session.add(kb)
    session.flush()
    old = kb.updated_at
    session.execute(
        text("UPDATE knowledge_bases SET description='更新说明' WHERE kb_id=:id"),
        {"id": kb.kb_id},
    )
    session.refresh(kb)
    assert kb.updated_at > old


def test_downgrade_and_reupgrade_preserve_extension(migrated_connection):
    """在保存点内验证迁移可回滚再升级，且全过程保留共享的 vector 扩展。"""

    connection, config, schema = migrated_connection
    with connection.begin_nested():
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names(schema=schema) == ["alembic_version"]
        assert connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
        )
        command.upgrade(config, "head")
        command.check(config)
