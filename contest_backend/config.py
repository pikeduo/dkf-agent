"""竞赛服务独立配置；不复用知识库连接或模型凭据。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """只读取CONTEST命名空间，路径统一相对项目根解析。"""

    model_config = SettingsConfigDict(env_prefix="CONTEST_", env_file=Path(__file__).resolve().parent / ".env",
                                     extra="ignore", hide_input_in_errors=True)
    api_token: SecretStr = SecretStr("")
    registry_path: Path = Path("data/contest/registry.json")
    database_urls: dict[str, SecretStr] = Field(default_factory=dict)
    llm_provider_id: str = "contest"
    llm_api_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_max_output_tokens: int = Field(default=1024, ge=64, le=4096)
    llm_timeout_seconds: int = Field(default=30, ge=1, le=120)
    statement_timeout_ms: int = Field(default=15000, ge=100, le=30000)
    connect_timeout_seconds: int = Field(default=5, ge=1, le=10)
    max_plan_cost: int = Field(default=100000, ge=1, le=1000000)
    max_result_bytes: int = Field(default=1000000, ge=1024, le=2000000)
    session_ttl_seconds: int = Field(default=1800, ge=60, le=7200)
    max_sessions: int = Field(default=128, ge=1, le=1024)
    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    def registry_file(self) -> Path:
        """从唯一配置确定注册表位置，禁止根据当前工作目录猜测数据源。"""
        return self.registry_path if self.registry_path.is_absolute() else ROOT / self.registry_path


@lru_cache
def get_settings() -> Settings:
    """只加载配置快照，不联网、不连接数据库或加载模型。"""
    return Settings()
