"""为解析块保留原文阅读顺序，已有块保留空值，不反推未知位置。

Revision ID: 0003_block_order
Revises: 0002_document_tasks
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_block_order"
down_revision = "0002_document_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空正整数序号和文档内唯一约束，不覆盖历史块或安装扩展。"""

    op.add_column(
        "document_blocks", sa.Column("block_index", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_document_blocks_block_index"), "document_blocks", "block_index >= 1"
    )
    op.create_unique_constraint(
        "uq_document_blocks_doc_index", "document_blocks", ["doc_id", "block_index"]
    )


def downgrade() -> None:
    """删除序号约束与列，保留原文解析块，但回滚后不再保存阅读顺序。"""

    op.drop_constraint(
        "uq_document_blocks_doc_index", "document_blocks", type_="unique"
    )
    op.drop_constraint(
        op.f("ck_document_blocks_block_index"), "document_blocks", type_="check"
    )
    op.drop_column("document_blocks", "block_index")
