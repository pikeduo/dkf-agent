"""增加可恢复管理员维护检查点，不回填历史任务、不改变向量或既有业务状态。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_document_management"
down_revision = "0007_pgvector_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """由用户手动增加独立编排标识与私有配置；历史文档保持两列为空。"""
    op.add_column("documents", sa.Column("management_task_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("management_config", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint(op.f("uq_documents_management_task_id"), "documents", ["management_task_id"])
    op.create_check_constraint(op.f("ck_documents_management_checkpoint"), "documents",
        "(management_task_id IS NULL AND management_config IS NULL) OR "
        "(management_task_id IS NOT NULL AND management_config IS NOT NULL AND jsonb_typeof(management_config) = 'object')")


def downgrade() -> None:
    """停止相关进程后由用户回滚，保留原件、Block、Chunk和向量，不自动取消任务。"""
    op.drop_constraint(op.f("ck_documents_management_checkpoint"), "documents", type_="check")
    op.drop_constraint(op.f("uq_documents_management_task_id"), "documents", type_="unique")
    op.drop_column("documents", "management_config")
    op.drop_column("documents", "management_task_id")
