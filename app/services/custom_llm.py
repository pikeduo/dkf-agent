"""通用 OpenAI Chat Completions 兼容 API，不与 DeepSeek 配置或品牌绑定。"""

from urllib.parse import urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.services.llm import ChatCompletionService, LLMError


class CustomLLMService(ChatCompletionService):
    """只读取 CUSTOM_LLM_*；Sub2API 与其他兼容服务均走此独立边界。"""

    def __init__(self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None):
        """冻结自定义配置及 HTTP 授权，不联网，也不读官方 API Key。"""
        try:
            config = settings if settings is not None else get_settings()
            self._allow_http = config.custom_llm_allow_insecure_http
            super().__init__(provider="custom", base_url=config.custom_llm_api_base_url,
                             api_key=config.custom_llm_api_key, model=config.custom_llm_model,
                             max_tokens=config.custom_llm_max_tokens, timeout_seconds=config.custom_llm_timeout_seconds,
                             context_chars=config.rag_max_context_chars, transport=transport)
        except Exception:
            raise LLMError("LLM_CONFIG", provider="custom") from None

    def _validate_endpoint(self) -> None:
        """允许显式可信 HTTPS 根路径；拒绝凭据、参数、控制字符和未授权明文 HTTP。"""
        base = self._base_url
        if not isinstance(base, str) or any(ord(char) <= 32 or ord(char) > 126 for char in base):
            raise ValueError
        endpoint = urlsplit(base)
        # 兼容 /v1、/api/v1 等部署前缀；不允许路径编码/遍历或完整 completion 地址。
        path = endpoint.path.rstrip("/")
        segments = path.split("/")[1:]
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or endpoint.port == 0
                or "\\" in base or "%" in base
                or any(segment in {"", ".", ".."} for segment in segments)
                or path.endswith("/chat/completions")
                or base != f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path}"):
            raise ValueError
        if endpoint.scheme == "http" and not self._allow_http:
            raise ValueError
