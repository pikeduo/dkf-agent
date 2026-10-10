"""供应商解析任务持久化与有效任务唯一约束，不修改文档供应商无关状态。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.knowledge import Timestamps


class DocumentParseJob(Timestamps, Base):
    """记录一次云端解析；签名上传 URL 仅用于恢复上传，不进入公开响应。"""

    __tablename__ = "document_parse_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('waiting-file','pending','running','converting','done','failed')",
            name="state",
        ),
        CheckConstraint("retry_count >= 0", name="retry_count"),
        Index(
            "uq_parse_jobs_active_identity",
            "doc_id",
            "file_hash",
            "provider",
            "model_version",
            "options_hash",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_parse_jobs_doc_created", "doc_id", "created_at"),
    )

    job_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    doc_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(32))
    file_hash: Mapped[str] = mapped_column(String(64))
    parse_options: Mapped[dict] = mapped_column(JSONB)
    options_hash: Mapped[str] = mapped_column(String(64))
    data_id: Mapped[str] = mapped_column(String(128), unique=True)
    batch_id: Mapped[str | None] = mapped_column(String(128))
    # task_id 保存本地处理代次；外部批量接口以 batch_id / data_id 标识对象。
    task_id: Mapped[UUID] = mapped_column()
    trace_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), default="waiting-file")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 在非幂等 POST 前提交检查点；响应丢失时不盲目申请第二个 batch。
    submission_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    upload_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    upload_url: Mapped[str | None] = mapped_column(Text)
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
