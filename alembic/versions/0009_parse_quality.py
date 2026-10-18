"""解析质量/来源及部分索引终态；由用户手动迁移，不回填或修改已有证据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_parse_quality"
down_revision = "0008_document_management"
branch_labels = depends_on = None


def upgrade():
    """新增可空质量/来源字段和部分终态，历史READY及原有块保持原样。"""
    op.add_column("documents", sa.Column("parse_quality", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("document_blocks", sa.Column("provenance", postgresql.JSONB(none_as_null=True), nullable=True))
    op.drop_constraint(op.f("ck_documents_status"), "documents", type_="check")
    op.create_check_constraint(op.f("ck_documents_status"), "documents",
        "status IN ('UPLOADED','PARSING','OCR_PROCESSING','CHUNKING','EMBEDDING','INDEXING','READY','PARTIAL_READY','FAILED')")


def downgrade():
    """存在部分终态时拒绝回滚，不擅自把不完整内容升级READY或改写业务状态。"""
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM documents WHERE status='PARTIAL_READY'")):
        raise RuntimeError("仍有PARTIAL_READY文档，须先人工决定保留策略，未修改状态")
    op.drop_constraint(op.f("ck_documents_status"), "documents", type_="check")
    op.create_check_constraint(op.f("ck_documents_status"), "documents",
        "status IN ('UPLOADED','PARSING','OCR_PROCESSING','CHUNKING','EMBEDDING','INDEXING','READY','FAILED')")
    op.drop_column("document_blocks", "provenance")
    op.drop_column("documents", "parse_quality")
