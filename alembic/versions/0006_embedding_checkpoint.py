"""独立 GPU 向量化检查点；不写向量、不创建 pgvector 索引或回填历史记录。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0006_embedding_checkpoint"
down_revision = "0005_chunk_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空 GPU 代次和私有参数快照，保持历史 Document/Chunk 内容不变。"""

    op.add_column("documents", sa.Column("embedding_task_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("embedding_config", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint(op.f("uq_documents_embedding_task_id"), "documents", ["embedding_task_id"])
    op.create_check_constraint(op.f("ck_documents_embedding_checkpoint"), "documents",
        "(embedding_task_id IS NULL AND embedding_config IS NULL) OR "
        "(embedding_task_id IS NOT NULL AND embedding_config IS NOT NULL AND jsonb_typeof(embedding_config) = 'object')")


def downgrade() -> None:
    """删除 GPU 检查点但不清理 Chunk 或本地向量文件，回滚前应停止所有 Worker。"""

    op.drop_constraint(op.f("ck_documents_embedding_checkpoint"), "documents", type_="check")
    op.drop_constraint(op.f("uq_documents_embedding_task_id"), "documents", type_="unique")
    op.drop_column("documents", "embedding_config")
    op.drop_column("documents", "embedding_task_id")
