"""按环境变量连接已部署 PostgreSQL，不在应用启动时自动建表。"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """读取 DATABASE_URL 并缓存 PostgreSQL 引擎，未配置或驱动不符合要求时拒绝创建。

    引擎仅在实际使用连接时访问已有数据库，不负责安装服务或自动建表。
    """

    database_url = get_settings().database_url
    if not database_url:
        raise RuntimeError("请在本地 .env 中配置 DATABASE_URL")
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("DATABASE_URL 必须使用 PostgreSQL psycopg 驱动")
    return create_engine(
        url.set(drivername="postgresql+psycopg"),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def get_session() -> Iterator[Session]:
    """提供独立数据库会话，并在使用结束后关闭；事务提交由调用方显式控制。"""

    with Session(get_engine()) as session:
        yield session
