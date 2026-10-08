"""为文档记录当前处理任务标识，不改动已有文档状态。

Revision ID: 0002_document_tasks
Revises: 0001_knowledge_base
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_document_tasks"
down_revision = "0001_knowledge_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空任务 UUID 及唯一约束，已有文档保持空值，需手动投递才开始处理。"""

    op.add_column(
        "documents", sa.Column("processing_task_id", sa.UUID(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_documents_processing_task_id", "documents", ["processing_task_id"]
    )


def downgrade() -> None:
    """删除任务关联字段及约束，保留文档与文件，回滚会丢失任务关联信息。"""

    op.drop_constraint("uq_documents_processing_task_id", "documents", type_="unique")
    op.drop_column("documents", "processing_task_id")
