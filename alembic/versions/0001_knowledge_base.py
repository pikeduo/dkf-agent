"""建立知识库、文档、解析块与 Chunk 表。

Revision ID: 0001_knowledge_base
Revises: None
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR

from alembic import context, op

revision = "0001_knowledge_base"
down_revision = None
branch_labels = None
depends_on = None


def id_column(name):
    """按指定列名创建 UUID 主键列，直接 SQL 插入时由数据库生成默认标识。"""

    return sa.Column(
        name,
        sa.UUID(),
        primary_key=True,
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def timestamp_columns():
    """返回带时区的创建、更新时间列，首次插入时默认使用数据库当前时间。"""

    return [
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )
        for name in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    """建立四张知识库核心表、约束、索引和更新时间触发器，在线迁移先检查 vector。"""

    # 扩展安装与启用属于人工部署；迁移只检查，避免项目用户需要管理员权限。
    if not context.is_offline_mode():
        enabled = op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
        )
        if not enabled:
            raise RuntimeError(
                "当前数据库未启用 vector，请管理员手动执行 CREATE EXTENSION vector;"
            )

    op.create_table(
        "knowledge_bases",
        id_column("kb_id"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        *timestamp_columns(),
        sa.PrimaryKeyConstraint("kb_id", name=op.f("pk_knowledge_bases")),
        sa.UniqueConstraint("name", name=op.f("uq_knowledge_bases_name")),
        sa.CheckConstraint(
            "btrim(name) <> ''", name=op.f("ck_knowledge_bases_name_not_blank")
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name=op.f("ck_knowledge_bases_status")
        ),
    )
    op.create_table(
        "documents",
        id_column("doc_id"),
        sa.Column("kb_id", sa.UUID(), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="UPLOADED"),
        sa.Column("error_message", sa.Text(), nullable=True),
        *timestamp_columns(),
        sa.PrimaryKeyConstraint("doc_id", name=op.f("pk_documents")),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.kb_id"],
            ondelete="CASCADE",
            name=op.f("fk_documents_kb_id_knowledge_bases"),
        ),
        sa.UniqueConstraint(
            "kb_id", "file_hash", name=op.f("uq_documents_kb_id_file_hash")
        ),
        sa.UniqueConstraint("doc_id", "kb_id", name=op.f("uq_documents_doc_id_kb_id")),
        sa.CheckConstraint(
            "file_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_file_hash")
        ),
        sa.CheckConstraint(
            "source_type IN ('preset', 'uploaded')",
            name=op.f("ck_documents_source_type"),
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADED', 'PARSING', 'OCR_PROCESSING', 'CHUNKING', "
            "'EMBEDDING', 'INDEXING', 'READY', 'FAILED')",
            name=op.f("ck_documents_status"),
        ),
    )
    op.create_index("ix_documents_kb_id_status", "documents", ["kb_id", "status"])
    op.create_table(
        "document_blocks",
        id_column("block_id"),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("block_type", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("bbox", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("block_id", name=op.f("pk_document_blocks")),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["documents.doc_id"],
            ondelete="CASCADE",
            name=op.f("fk_document_blocks_doc_id_documents"),
        ),
        sa.CheckConstraint("page >= 1", name=op.f("ck_document_blocks_page")),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_document_blocks_confidence"),
        ),
        sa.CheckConstraint(
            "block_type IN ('title', 'text', 'table', 'formula', 'image_text')",
            name=op.f("ck_document_blocks_block_type"),
        ),
    )
    op.create_index(
        "ix_document_blocks_doc_id_page", "document_blocks", ["doc_id", "page"]
    )
    op.create_table(
        "document_chunks",
        id_column("chunk_id"),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("kb_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embedding", VECTOR(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_document_chunks")),
        sa.ForeignKeyConstraint(
            ["doc_id", "kb_id"],
            ["documents.doc_id", "documents.kb_id"],
            ondelete="CASCADE",
            name=op.f("fk_document_chunks_doc_id_kb_id_documents"),
        ),
        sa.CheckConstraint(
            "page_start >= 1 AND page_end >= page_start",
            name=op.f("ck_document_chunks_pages"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name=op.f("ck_document_chunks_metadata_object"),
        ),
    )
    op.create_index(
        "ix_document_chunks_kb_id_doc_id", "document_chunks", ["kb_id", "doc_id"]
    )
    op.create_index("ix_document_chunks_doc_id", "document_chunks", ["doc_id"])

    # updated_at 同时覆盖 ORM 更新和直接 SQL 更新；函数与触发器均随迁移管理。
    op.execute("""
        CREATE FUNCTION dkf_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$;
    """)
    for table in ("knowledge_bases", "documents"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION dkf_set_updated_at()"
        )


def downgrade() -> None:
    """按依赖顺序删除本次建立的表及触发器函数，保留人工启用的 vector 扩展。"""

    op.drop_table("document_chunks")
    op.drop_table("document_blocks")
    op.drop_table("documents")
    op.drop_table("knowledge_bases")
    op.execute("DROP FUNCTION dkf_set_updated_at()")
    # vector 为人工部署的共享扩展，回滚不删除它。
