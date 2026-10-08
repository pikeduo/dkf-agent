"""迁移使用与应用一致的 DATABASE_URL，不把密码写入 alembic.ini。"""

from logging.config import fileConfig

from pgvector.sqlalchemy import VECTOR

from alembic import context
from app.db.base import Base
from app.db.session import get_engine
from app.models import (  # noqa: F401
    Document,
    DocumentBlock,
    DocumentChunk,
    KnowledgeBase,
)

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """为向量列生成类型表达式并补齐导入，其他对象返回 False 以沿用默认渲染。"""

    # 自定义向量类型必须有可执行的导入，避免后续 autogenerate 生成缺少 pgvector 的迁移。
    if type_ == "type" and isinstance(obj, VECTOR):
        autogen_context.imports.add("from pgvector.sqlalchemy import VECTOR")
        return f"VECTOR({obj.dim})"
    return False


def run_migrations_offline() -> None:
    """按模型元数据生成离线迁移 SQL，不连接数据库，也不检查实际扩展状态。"""

    # 离线生成 SQL 不连接服务；执行这些 SQL 前须已手动启用 vector 扩展。
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """优先使用外部注入的连接执行迁移，否则连接环境变量指定的数据库。"""

    # 测试可注入独立 schema 的连接；正常部署连接本地 .env 指定的项目库。
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        migrate(supplied_connection)
        return
    with get_engine().connect() as connection:
        migrate(connection)


def migrate(connection) -> None:
    """在给定连接的迁移事务中执行修订，并启用列类型及服务端默认值比较。"""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
