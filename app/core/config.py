"""应用运行配置。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 配置文件固定从项目根目录加载，避免因启动目录不同导致本地 .env 未生效。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """集中管理服务运行配置，后续基础设施配置在此逐步补充。"""

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: Path = Path("logs")
    database_url: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    # 模型由用户手动下载；只读本地目录，默认单张 CUDA GPU，不隐式回退 CPU。
    embedding_model_dir: Path = Path("models/bge-m3")
    embedding_device: str = Field(default="cuda:0", pattern=r"^(cpu|cuda:[0-9]+)$")
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    embedding_max_length: int = Field(default=8192, ge=8, le=8192)
    embedding_use_fp16: bool = True
    # 在线重排共用进程内服务；用户手动部署本地模型，不自动下载或回退设备。
    reranker_model_dir: Path = Path("models/bge-reranker-v2-m3")
    reranker_device: str = Field(default="cuda:0", pattern=r"^(cpu|cuda:[0-9]+)$")
    reranker_batch_size: int = Field(default=2, ge=1, le=32)
    reranker_max_length: int = Field(default=8192, ge=8, le=8192)
    reranker_use_fp16: bool = True
    embeddings_dir: Path = Path("data/embeddings")
    upload_dir: Path = Path("data/uploads")
    max_upload_size_mb: int = Field(default=50, gt=0)
    # 解析缓存与原件分开管理；API/Worker 必须共享该本地目录，不自动清理历史结果。
    parsed_dir: Path = Path("data/parsed")
    parse_cache_max_entry_mb: int = Field(default=768, ge=64, le=2048)
    # 本地估算 token；表格/公式完整性优先，结构块可能超过此软目标。
    chunk_size: int = Field(default=600, ge=400, le=700)
    chunk_overlap: int = Field(default=80, ge=50, le=100)

    # 预检阈值是结构风险指标，不是解析置信度；不触发逐页/逐图云端调用。
    router_pdf_image_area_ratio: float = Field(default=0.01, gt=0, le=1)
    router_pdf_drawing_area_ratio: float = Field(default=0.01, gt=0, le=1)
    router_pdf_drawing_count: int = Field(default=6, ge=1, le=1000)
    router_pdf_min_text_chars: int = Field(default=20, ge=1, le=1000)
    router_pdf_max_pages: int = Field(default=1000, ge=1, le=2000)
    router_pdf_max_lines_per_page: int = Field(default=2000, ge=1, le=10000)
    router_xml_part_max_mb: int = Field(default=16, ge=1, le=64)

    # auto 下复杂 XLSX 默认尝试 Hybrid；关闭仅改为带 Native 主结果保护的 Whole。
    hybrid_xlsx_enabled: bool = True
    hybrid_max_candidates: int = Field(default=64, ge=1, le=256)
    hybrid_max_bundle_pages: int = Field(default=32, ge=1, le=200)
    hybrid_min_image_side: int = Field(default=32, ge=1, le=4096)
    hybrid_max_image_pixels: int = Field(default=16000000, ge=1, le=40000000)
    hybrid_max_image_mb: int = Field(default=16, ge=1, le=64)

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
