"""DeepSeek 官方 API 专属服务；历史错误码和严格 JSON 导入保持兼容。"""

import httpx

from app.core.config import Settings, get_settings
from app.services.llm import ChatCompletionService, LLMError, ERRORS as LLM_ERRORS, MAX_RESPONSE_BYTES, strict_json

# 保留官方服务既有公开错误码；自定义 API 使用独立的 LLM_* 错误码。
ERRORS = {code.replace("LLM_", "DEEPSEEK_", 1): message for code, message in LLM_ERRORS.items()}


class DeepSeekError(LLMError):
    """兼容既有官方调用者，同时允许上层统一捕获 LLMError。"""

    def __init__(self, code: str, *, status_code: int | None = None):
        """仅保留官方安全码/HTTP状态，不包含密钥、URL或上游正文。"""
        safe = code if code in ERRORS else "DEEPSEEK_RESPONSE"
        super().__init__(safe.replace("DEEPSEEK_", "LLM_", 1), provider="deepseek", status_code=status_code)
        self.code = safe
        self.args = (f"{safe}：{ERRORS[safe]}",)


class DeepSeekService(ChatCompletionService):
    """只冻结 DEEPSEEK_* 配置；不读取或使用任何自定义端点或密钥。"""

    def __init__(self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None):
        """读取官方配置快照，不联网；直接构造仍始终表示官方服务。"""
        try:
            config = settings if settings is not None else get_settings()
            super().__init__(provider="deepseek", base_url=config.deepseek_api_base_url,
                             api_key=config.deepseek_api_key, model=config.deepseek_model,
                             max_tokens=config.deepseek_max_tokens, timeout_seconds=config.deepseek_timeout_seconds,
                             context_chars=config.rag_max_context_chars, transport=transport)
        except Exception:
            raise DeepSeekError("DEEPSEEK_CONFIG") from None

    def _error(self, suffix: str, *, status_code: int | None = None) -> DeepSeekError:
        """保留原官方错误码及HTTP状态，不在官方服务内部再次Fallback。"""
        return DeepSeekError("DEEPSEEK_" + suffix, status_code=status_code)

    def _validate_endpoint(self) -> None:
        """官方模式仅允许固定 HTTPS 官方根地址，旧 HTTP 开关无效。"""
        if self._base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
            raise ValueError

    def _payload(self, system_prompt: str, user_content: str) -> dict:
        """官方请求保留原有关闭 thinking 设置，不将其强加给通用提供方。"""
        return super()._payload(system_prompt, user_content) | {"thinking": {"type": "disabled"}}
