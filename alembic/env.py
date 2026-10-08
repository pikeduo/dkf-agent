"""迁移使用与应用一致的 DATABASE_URL，不把密码写入 alembic.ini。"""

import re
from logging.config import fileConfig

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import inspect, text

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
    """执行迁移或结构比较；测试连接显式隔离版本表和反射范围，部署配置保持不变。"""

    test_schema = config.attributes.get("test_schema")
    original_schema = connection.dialect.default_schema_name
    include_name = None
    if test_schema is not None:
        # 仅接受测试生成的随机 schema，禁止隔离配置意外指向 public。
        if not re.fullmatch(r"dkf_test_[0-9a-f]{32}", test_schema):
            raise ValueError("迁移测试必须使用随机生成的 dkf_test_ schema")
        if connection.scalar(text("SELECT current_schema()")) != test_schema:
            raise RuntimeError("测试 search_path 与版本表 schema 不一致，拒绝执行迁移")
        local_tables = set(inspect(connection).get_table_names(schema=test_schema))

        def include_name(name, type_, parent_names):
            """仅比较测试 schema 内的业务表，排除版本表和 search_path 中的外部表。"""

            return type_ != "table" or (
                name != "alembic_version" and name in local_tables
            )

    try:
        if test_schema is not None:
            # 表和外键反射也需把临时 schema 当作默认值，否则可能错误比较 public。
            connection.dialect.default_schema_name = test_schema
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=test_schema,
            include_name=include_name,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        # 方言对象属于共享 Engine，即使迁移失败也必须还原，避免影响后续连接。
        connection.dialect.default_schema_name = original_schema


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
