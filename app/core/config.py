"""应用运行配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 配置文件固定从项目根目录加载，避免因启动目录不同导致本地 .env 未生效。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """集中管理服务运行配置，后续基础设施配置在此逐步补充。"""

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    upload_dir: Path = Path("data/uploads")
    max_upload_size_mb: int = Field(default=50, gt=0)

    # 云端精准解析单独启用；空 Token 不影响原生 Parser 和基础服务启动。
    mineru_api_base_url: str = "https://mineru.net/api/v4"
    mineru_api_token: SecretStr = SecretStr("")
    mineru_model_version: str = "vlm"
    mineru_language: str = Field(default="ch", min_length=1, max_length=32)
    mineru_enable_table: bool = True
    mineru_enable_formula: bool = True
    mineru_is_ocr: bool = True
    mineru_submit_file_limit_per_minute: int = Field(default=50, ge=1)
    mineru_result_query_limit_per_minute: int = Field(default=1000, ge=1)
    mineru_daily_file_limit: int = Field(default=5000, ge=1)
    mineru_high_priority_page_limit: int = Field(default=1000, ge=1)
    mineru_upload_batch_max_files: int = Field(default=50, ge=1, le=50)
    mineru_rate_safety_ratio: float = Field(default=0.9, gt=0, le=1)
    mineru_quota_namespace: str = Field(
        default="dkf-agent:mineru", min_length=1, max_length=128
    )
    mineru_request_timeout_seconds: int = Field(default=60, ge=1, le=300)
    mineru_poll_interval_seconds: int = Field(default=15, ge=1, le=300)
    mineru_job_timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    mineru_max_retries: int = Field(default=5, ge=0, le=20)
    mineru_result_max_mb: int = Field(default=256, ge=1, le=512)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例，避免每个请求重复解析环境变量。"""

    return Settings()
