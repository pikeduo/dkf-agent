"""按环境变量连接已部署 PostgreSQL，不在应用启动时自动建表。"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
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
    """每次调用使用独立 Session，避免 API 或 Celery 任务共享事务。"""

    with Session(get_engine()) as session:
        yield session
