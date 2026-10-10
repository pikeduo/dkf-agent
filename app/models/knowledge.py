"""知识库数据结构及证据来源约束。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeBase(Timestamps, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="status"),
    )

    kb_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default="ACTIVE", server_default="ACTIVE"
    )


class Document(Timestamps, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # 同一文档可属于不同知识库，但同一知识库内按 SHA256 去重。
        UniqueConstraint("kb_id", "file_hash", name="uq_documents_kb_id_file_hash"),
        UniqueConstraint("doc_id", "kb_id", name="uq_documents_doc_id_kb_id"),
        CheckConstraint("file_hash ~ '^[0-9a-f]{64}$'", name="file_hash"),
        CheckConstraint("source_type IN ('preset', 'uploaded')", name="source_type"),
        CheckConstraint(
            "status IN ('UPLOADED', 'PARSING', 'OCR_PROCESSING', 'CHUNKING', "
            "'EMBEDDING', 'INDEXING', 'READY', 'FAILED')",
            name="status",
        ),
        Index("ix_documents_kb_id_status", "kb_id", "status"),
    )

    doc_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()")
    )
    kb_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE")
    )
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(32))
    file_hash: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32), default="UPLOADED", server_default="UPLOADED"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    # 每次重新投递分配新标识，旧消息不能覆盖新一轮处理的状态。
    processing_task_id: Mapped[UUID | None] = mapped_column(unique=True)


class DocumentBlock(Base):
    """解析块及阅读序号；历史块无序号时不假造原文位置。"""

    __tablename__ = "document_blocks"
    __table_args__ = (
        CheckConstraint("page >= 1", name="page"),
        CheckConstraint("block_index >= 1", name="block_index"),
        UniqueConstraint("doc_id", "block_index", name="uq_document_blocks_doc_index"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint(
            "block_type IN ('title', 'text', 'table', 'formula', 'image_text')",
            name="block_type",
        ),
        Index("ix_document_blocks_doc_id_page", "doc_id", "page"),
    )

    block_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()")
    )
    doc_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE")
    )
    # 原生 Parser 始终写入从 1 开始的序号；允许空值以兼容历史解析块。
    block_index: Mapped[int | None] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    block_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # 复合外键防止 Chunk 指向文档 A，却填写知识库 B 的 kb_id。
        ForeignKeyConstraint(
            ["doc_id", "kb_id"],
            ["documents.doc_id", "documents.kb_id"],
            ondelete="CASCADE",
            name="fk_document_chunks_doc_id_kb_id_documents",
        ),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="pages"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        Index("ix_document_chunks_kb_id_doc_id", "kb_id", "doc_id"),
        Index("ix_document_chunks_doc_id", "doc_id"),
    )

    chunk_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()")
    )
    doc_id: Mapped[UUID] = mapped_column()
    kb_id: Mapped[UUID] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    # metadata 是 Declarative 保留名；数据库列名不变，ORM 属性使用 chunk_metadata。
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        MutableDict.as_mutable(JSONB),
        default=dict,
        server_default=sql_text("'{}'::jsonb"),
    )
    # BGE-M3 的 Dense 向量为 1024 维，入库前允许为空；本阶段不建立 ANN 索引。
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
