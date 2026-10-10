"""持久化 MinerU 云端任务及恢复检查点。

Revision ID: 0004_mineru_jobs
Revises: 0003_block_order
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_mineru_jobs"
down_revision = "0003_block_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅创建供应商任务表和索引，保留已有文档、Block 及处理代次。"""

    op.create_table(
        "document_parse_jobs",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("parse_options", postgresql.JSONB(), nullable=False),
        sa.Column("options_hash", sa.String(64), nullable=False),
        sa.Column("data_id", sa.String(128), nullable=False),
        sa.Column("batch_id", sa.String(128)),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("trace_id", sa.String(128)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("submission_attempted", sa.Boolean(), nullable=False),
        sa.Column("submission_started_at", sa.DateTime(timezone=True)),
        sa.Column("upload_complete", sa.Boolean(), nullable=False),
        sa.Column("upload_url", sa.Text()),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('waiting-file','pending','running','converting','done','failed')",
            name=op.f("ck_document_parse_jobs_state"),
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name=op.f("ck_document_parse_jobs_retry_count")
        ),
        sa.ForeignKeyConstraint(["doc_id"], ["documents.doc_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("data_id"),
    )
    op.create_index(
        "uq_parse_jobs_active_identity",
        "document_parse_jobs",
        ["doc_id", "file_hash", "provider", "model_version", "options_hash"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_parse_jobs_doc_created", "document_parse_jobs", ["doc_id", "created_at"]
    )


def downgrade() -> None:
    """删除云端任务记录；不删除文档或 Block，运行中云端任务应先人工停止调度。"""

    op.drop_index("ix_parse_jobs_doc_created", table_name="document_parse_jobs")
    op.drop_index("uq_parse_jobs_active_identity", table_name="document_parse_jobs")
    op.drop_table("document_parse_jobs")
