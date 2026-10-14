"""提供方无关的单次 Chat Completions 边界；不重试、不切换提供方、不记录正文。"""

import json
import logging
from time import perf_counter

import httpx

from app.core.business_logging import log_event
from app.core.config import Settings, get_settings

MAX_RESPONSE_BYTES = 262144
ERRORS = {
    "LLM_CONFIG": "LLM 提供方配置或密钥不可用，请人工检查本地配置",
    "LLM_INPUT": "LLM 输入为空、无效或超过本地预算",
    "LLM_AUTH": "LLM 鉴权失败，请人工检查选中提供方的密钥与权限",
    "LLM_QUOTA": "LLM 余额或配额不足",
    "LLM_RATE_LIMIT": "LLM 限流，本次没有自动追加请求",
    "LLM_HTTP": "LLM HTTP 请求未成功，请查看安全状态码",
    "LLM_NETWORK": "LLM 网络请求未完成；可能已产生费用，请先人工核对",
    "LLM_RESPONSE": "LLM 返回结构无效或超限，未返回部分回答",
    "LLM_TRUNCATED": "LLM 输出未完整结束，未返回截断回答",
}


class LLMError(Exception):
    """固定错误分类与提供方枚举，不承载地址、凭据或上游原文。"""

    def __init__(self, code: str, *, provider: str | None = None):
        """仅允许安全错误码和已注册提供方，未知输入不回显。"""
        self.code = code if code in ERRORS else "LLM_RESPONSE"
        self.provider = provider if provider in {"deepseek", "custom"} else None
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def strict_json(text: str | bytes):
    """拒绝重复字段、非有限数值和非法 Unicode，不修补供应商返回内容。"""
    def pairs(items):
        """重复键可能隐藏错误引用，必须整体拒绝。"""
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        """拒绝 NaN/Infinity，参数不进入异常或日志。"""
        raise ValueError

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    return value


class ChatCompletionService:
    """共享安全 HTTP 实现；各子类仅冻结自己提供方的配置，不读另一方密钥。"""

    def __init__(self, *, provider, base_url, api_key, model, max_tokens, timeout_seconds,
                 context_chars, transport=None):
        """保存所选配置快照，不联网、不输出凭据，不持有完整 Settings。"""
        self.provider = provider
        self._base_url, self._api_key, self._model = base_url, api_key, model
        self._max_tokens, self._timeout_seconds = max_tokens, timeout_seconds
        self._context_chars, self._transport = context_chars, transport

    @property
    def model_name(self) -> str:
        """只提供安全模型标识，不暴露配置对象。"""
        return self._model

    @property
    def max_tokens(self) -> int:
        """提供框架元数据需要的独立输出预算。"""
        return self._max_tokens

    @property
    def context_chars(self) -> int:
        """提供完整 Prompt 字符预算，不冒充精确 token 限制。"""
        return self._context_chars

    def _error(self, suffix: str) -> LLMError:
        """将内部安全类别转换为选中提供方错误，子类可保留历史错误码。"""
        return LLMError("LLM_" + suffix, provider=self.provider)

    def validate_config(self) -> None:
        """只读检查密钥与供应商端点；预检绝不创建客户端或发送请求。"""
        try:
            key = self._api_key.get_secret_value()
            if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
                raise ValueError
            self._validate_endpoint()
        except Exception:
            raise self._error("CONFIG") from None

    def _validate_endpoint(self) -> None:
        """端点必须由具体提供方校验，禁止直接使用未约束的共享基类。"""
        raise ValueError

    def _payload(self, system_prompt: str, user_content: str) -> dict:
        """构建通用 JSON-mode 非流式消息，不混入供应商专有参数。"""
        return {"model": self.model_name, "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}],
            "stream": False, "temperature": 0, "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}}

    def complete(self, system_prompt: str, user_content: str) -> str:
        """只向选中端点发送一次请求；任何失败不重试、不回退、不发布部分回答。"""
        started, status_code = perf_counter(), None
        try:
            self.validate_config()
            if (not isinstance(system_prompt, str) or not isinstance(user_content, str)
                    or not system_prompt.strip() or not user_content.strip()
                    or len(system_prompt) + len(user_content) > self.context_chars):
                raise self._error("INPUT")
            payload = self._payload(system_prompt, user_content)
            log_event("llm.started", provider=self.provider, model_name=self.model_name)
            # TLS 始终校验；不读取系统代理、不跟随跳转，避免跨端点传递凭据。
            timeout = httpx.Timeout(self._timeout_seconds, connect=min(10.0, self._timeout_seconds))
            with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False,
                              verify=True, timeout=timeout) as client:
                with client.stream("POST", self._base_url.rstrip("/") + "/chat/completions",
                                   headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
                                   json=payload) as response:
                    status_code = response.status_code
                    if status_code != 200:
                        suffix = {401: "AUTH", 403: "AUTH", 402: "QUOTA", 429: "RATE_LIMIT"}.get(status_code, "HTTP")
                        raise self._error(suffix)
                    body = bytearray()
                    for part in response.iter_bytes():
                        if len(body) + len(part) > MAX_RESPONSE_BYTES:
                            raise self._error("RESPONSE")
                        body.extend(part)
            envelope = strict_json(bytes(body))
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise self._error("RESPONSE")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise self._error("TRUNCATED")
            message = choice["message"]
            content = message["content"]
            if (message.get("role") != "assistant" or message.get("tool_calls")
                    or not isinstance(content, str) or not content.strip()):
                raise self._error("RESPONSE")
            content.encode("utf-8", errors="strict")
            log_event("llm.finished", provider=self.provider, model_name=self.model_name, status_code=status_code,
                      duration_ms=(perf_counter() - started) * 1000)
            return content
        except Exception as exc:
            error = exc if isinstance(exc, LLMError) else self._error(
                "NETWORK" if isinstance(exc, httpx.HTTPError) else "RESPONSE")
            log_event("llm.failed", level=logging.WARNING, provider=self.provider, model_name=self.model_name,
                      error_code=error.code, status_code=status_code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None


def create_llm_service(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None) -> ChatCompletionService:
    """只实例化全局选择器指定的服务；构造失败不尝试另一方，不产生请求。"""
    try:
        config = settings if settings is not None else get_settings()
    except Exception:
        raise LLMError("LLM_CONFIG") from None
    if config.llm_provider == "deepseek":
        from app.services.deepseek import DeepSeekService
        return DeepSeekService(config, transport=transport)
    if config.llm_provider == "custom":
        from app.services.custom_llm import CustomLLMService
        return CustomLLMService(config, transport=transport)
    raise LLMError("LLM_CONFIG")
