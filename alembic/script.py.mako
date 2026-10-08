"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """应用本次数据库结构变更；生成迁移后须按实际操作补充中文说明。"""

    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚本次数据库结构变更；生成迁移后须说明回滚范围和潜在数据损失。"""

    ${downgrades if downgrades else "pass"}
