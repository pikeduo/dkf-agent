"""保存独立切片任务代次和冻结参数，不启动基础服务或回填历史文档。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0005_chunk_checkpoint"
down_revision = "0004_mineru_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空检查点及一致性约束，已有文档不自动切片。"""

    op.add_column("documents", sa.Column("chunk_task_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("chunk_config", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint(op.f("uq_documents_chunk_task_id"), "documents", ["chunk_task_id"])
    op.create_check_constraint(op.f("ck_documents_chunk_checkpoint"), "documents",
        "(chunk_task_id IS NULL AND chunk_config IS NULL) OR "
        "(chunk_task_id IS NOT NULL AND chunk_config IS NOT NULL AND jsonb_typeof(chunk_config) = 'object')")


def downgrade() -> None:
    """删除切片检查点，不删除已有 Block 或 Chunk；回滚前应停止所有 Worker。"""

    op.drop_constraint(op.f("ck_documents_chunk_checkpoint"), "documents", type_="check")
    op.drop_constraint(op.f("uq_documents_chunk_task_id"), "documents", type_="unique")
    op.drop_column("documents", "chunk_config")
    op.drop_column("documents", "chunk_task_id")
