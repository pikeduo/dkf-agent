"""统一Chat边界：每提供方单次HTTP；仅显式双方授权时允许一次custom→deepseek。"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

import httpx

from app.core.business_logging import log_event
from app.core.config import Settings, get_settings

MAX_RESPONSE_BYTES = 262144
CLOUD_CALL_ALLOWED: ContextVar[bool] = ContextVar("llm_cloud_call_allowed", default=False)
ERRORS = {
    "LLM_CONFIG": "LLM 提供方配置或密钥不可用，请人工检查本地配置",
    "LLM_INPUT": "LLM 输入为空、无效或超过本地预算",
    "LLM_AUTH": "LLM 鉴权失败，请人工检查选中提供方的密钥与权限",
    "LLM_QUOTA": "LLM 余额或配额不足",
    "LLM_RATE_LIMIT": "LLM 限流；仅显式授权的Fallback策略可追加一次备用请求",
    "LLM_CLOUD_PERMISSION": "本次未明确允许云调用，未请求主模型或备用模型",
    "LLM_HTTP": "LLM HTTP 请求未成功，请查看安全状态码",
    "LLM_NETWORK": "LLM 网络请求未完成；可能已产生费用，请先人工核对",
    "LLM_RESPONSE": "LLM 返回结构无效或超限，未返回部分回答",
    "LLM_TRUNCATED": "LLM 输出未完整结束，未返回截断回答",
}


class LLMError(Exception):
    """固定错误分类与提供方枚举，不承载地址、凭据或上游原文。"""

    def __init__(self, code: str, *, provider: str | None = None, status_code: int | None = None):
        """仅保留安全码、固定提供方及真实HTTP状态，不能携带上游响应正文。"""
        self.code = code if code in ERRORS else "LLM_RESPONSE"
        self.provider = provider if provider in {"deepseek", "custom"} else None
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@contextmanager
def llm_cloud_call(*, allow_cloud: bool = False):
    """由已有业务许可建立本次请求作用域；并发隔离，异常/嵌套退出后恢复，不持久授权。"""
    token = CLOUD_CALL_ALLOWED.set(allow_cloud is True)
    try:
        yield
    finally:
        CLOUD_CALL_ALLOWED.reset(token)


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

    def _error(self, suffix: str, *, status_code: int | None = None) -> LLMError:
        """转换安全错误并保留HTTP状态，禁止将所有HTTP错误都判为可切换。"""
        return LLMError("LLM_" + suffix, provider=self.provider, status_code=status_code)

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
        """只向自身端点发送一次请求；不自行重试或切换，由工厂包装器决定备用策略。"""
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
                        raise self._error(suffix, status_code=status_code)
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
                "NETWORK" if isinstance(exc, httpx.TransportError) else "RESPONSE")
            log_event("llm.failed", level=logging.WARNING, provider=self.provider, model_name=self.model_name,
                      error_code=error.code, status_code=status_code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None


class FallbackLLMService:
    """冻结两个独立服务；每次调用最多两次HTTP，无循环、同方重试或可变的实际提供方状态。"""

    def __init__(self, primary: ChatCompletionService, backup: ChatCompletionService):
        """只接受custom→deepseek，不读取第三家配置、不在构造时联网。"""
        if primary.provider != "custom" or backup.provider != "deepseek":
            raise LLMError("LLM_CONFIG")
        self._primary, self._backup = primary, backup

    @property
    def provider(self):
        """兼容现有元数据：表示计划主提供方；本次实际使用方以结束事件为准。"""
        return self._primary.provider

    @property
    def model_name(self):
        """返回计划主模型名，不能用共享实例字段记录并发请求实际使用方。"""
        return self._primary.model_name

    @property
    def max_tokens(self):
        """保留主模型元数据，各HTTP请求仍用本提供方的独立输出预算。"""
        return self._primary.max_tokens

    @property
    def context_chars(self):
        """完整提示词必须同时符合两方本地预算，不裁剪或改写备用上下文。"""
        return min(self._primary.context_chars, self._backup.context_chars)

    def validate_config(self):
        """开启时预检两方端点与密钥，备用无效则任何请求前安全失败。"""
        self._primary.validate_config()
        self._backup.validate_config()

    def _check_content(self, value, service):
        """JSON-mode内容仍须严格为对象；非法JSON不是重试/换模型理由，业务字段由原服务校验。"""
        try:
            if not isinstance(strict_json(value), dict):
                raise ValueError
        except Exception:
            raise service._error("RESPONSE") from None

    def complete(self, system_prompt: str, user_content: str) -> str:
        """本次许可+启用配置才最多切换一次；两方收到相同原Prompt，超时不代表未计费。"""
        started, actual, count = perf_counter(), self._primary, 0
        try:
            if CLOUD_CALL_ALLOWED.get() is not True:
                raise LLMError("LLM_CLOUD_PERMISSION", provider=self.provider)
            self.validate_config()
            try:
                content = self._primary.complete(system_prompt, user_content)
            except LLMError as error:
                eligible = error.provider == "custom" and (
                    error.code == "LLM_NETWORK"
                    or error.code == "LLM_RATE_LIMIT" and error.status_code == 429
                    or error.code == "LLM_HTTP" and error.status_code is not None and 500 <= error.status_code <= 599)
                if not eligible:
                    raise
                count, actual = 1, self._backup
                log_event("llm.fallback_started", provider=actual.provider, model_name=actual.model_name,
                    error_code=error.code, status_code=error.status_code, fallback_count=count)
                backup_started = perf_counter()
                try:
                    content = actual.complete(system_prompt, user_content)
                    self._check_content(content, actual)
                except LLMError as backup_error:
                    log_event("llm.fallback_failed", level=logging.WARNING, provider=actual.provider, model_name=actual.model_name,
                        error_code=backup_error.code, status_code=backup_error.status_code, fallback_count=count,
                        duration_ms=(perf_counter() - backup_started) * 1000)
                    raise
                log_event("llm.fallback_finished", provider=actual.provider, model_name=actual.model_name,
                          fallback_count=count, duration_ms=(perf_counter() - backup_started) * 1000)
            else:
                self._check_content(content, actual)
            log_event("llm.call_finished", provider=actual.provider, model_name=actual.model_name,
                fallback_count=count, duration_ms=(perf_counter() - started) * 1000)
            return content
        except LLMError as error:
            log_event("llm.call_failed", level=logging.WARNING, provider=actual.provider, model_name=actual.model_name,
                error_code=error.code, status_code=error.status_code, fallback_count=count,
                duration_ms=(perf_counter() - started) * 1000)
            raise


def create_llm_service(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None,
                       service_wrapper=None) -> ChatCompletionService | FallbackLLMService:
    """按配置构造原切换策略；可选薄代理逐提供方记账，不替换Fallback判断或触发HTTP。"""
    try:
        config = settings if settings is not None else get_settings()
    except Exception:
        raise LLMError("LLM_CONFIG") from None

    def wrap(service):
        """在组合备用策略前包装独立服务；默认原样返回，保留各方配置与异常。"""
        return service if service_wrapper is None else service_wrapper(service)

    if config.llm_provider == "deepseek":
        if getattr(config, "llm_fallback_enabled", False):
            raise LLMError("LLM_CONFIG")
        from app.services.deepseek import DeepSeekService
        return wrap(DeepSeekService(config, transport=transport))
    if config.llm_provider == "custom":
        from app.services.custom_llm import CustomLLMService
        primary = wrap(CustomLLMService(config, transport=transport))
        if getattr(config, "llm_fallback_enabled", False):
            if config.llm_fallback_provider != "deepseek":
                raise LLMError("LLM_CONFIG")
            from app.services.deepseek import DeepSeekService
            return FallbackLLMService(primary, wrap(DeepSeekService(config, transport=transport)))
        return primary
    raise LLMError("LLM_CONFIG")
