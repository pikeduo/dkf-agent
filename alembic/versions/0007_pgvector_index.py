"""新增独立入库检查点及 HNSW cosine 索引，不自动回填向量或标记历史文档 READY。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_pgvector_index"
down_revision = "0006_embedding_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """用户手动迁移：创建检查点及部分 HNSW 索引，需已安装支持 HNSW 的 vector 扩展。"""

    op.add_column("documents", sa.Column("index_task_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("index_config", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint(op.f("uq_documents_index_task_id"), "documents", ["index_task_id"])
    op.create_check_constraint(op.f("ck_documents_index_checkpoint"), "documents",
        "(index_task_id IS NULL AND index_config IS NULL) OR "
        "(index_task_id IS NOT NULL AND index_config IS NOT NULL AND jsonb_typeof(index_config) = 'object')")
    # 使用事务 DDL，迁移期间可能阻塞写入；不在文档任务中反复建索引，也不自动升级扩展。
    op.create_index("ix_document_chunks_embedding_hnsw", "document_chunks", ["embedding"],
                    postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"},
                    postgresql_where=sa.text("embedding IS NOT NULL"))


def downgrade() -> None:
    """停止所有相关进程后手动回滚；删除索引/检查点但保留已有 Chunk 向量与本地结果。"""

    op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
    op.drop_constraint(op.f("ck_documents_index_checkpoint"), "documents", type_="check")
    op.drop_constraint(op.f("uq_documents_index_task_id"), "documents", type_="unique")
    op.drop_column("documents", "index_config")
    op.drop_column("documents", "index_task_id")
