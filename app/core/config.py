"""应用运行配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 配置文件固定从项目根目录加载，避免因启动目录不同导致本地 .env 未生效。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """集中管理服务运行配置，后续基础设施配置在此逐步补充。"""

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例，避免每个请求重复解析环境变量。"""

    return Settings()
