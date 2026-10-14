"""项目唯一 DeepSeek HTTP 边界；显式单次请求，不记录输入、输出或上游错误原文。"""

import json
import logging
from time import perf_counter

import httpx

from app.core.business_logging import log_event
from app.core.config import Settings, get_settings

MAX_RESPONSE_BYTES = 262144
ERRORS = {
    "DEEPSEEK_CONFIG": "DeepSeek 配置或密钥不可用，请人工检查本地配置",
    "DEEPSEEK_INPUT": "DeepSeek 输入为空、无效或超过本地预算",
    "DEEPSEEK_AUTH": "DeepSeek 鉴权失败，请人工检查密钥与权限",
    "DEEPSEEK_QUOTA": "DeepSeek 余额或配额不足",
    "DEEPSEEK_RATE_LIMIT": "DeepSeek 限流，本次没有自动追加请求",
    "DEEPSEEK_HTTP": "DeepSeek HTTP 请求未成功，请查看安全状态码",
    "DEEPSEEK_NETWORK": "DeepSeek 网络请求未完成；可能已产生费用，请先人工核对",
    "DEEPSEEK_RESPONSE": "DeepSeek 返回结构无效或超限，未返回部分回答",
    "DEEPSEEK_TRUNCATED": "DeepSeek 输出未完整结束，未返回截断回答",
}


class DeepSeekError(Exception):
    """固定错误分类，不包含请求、响应正文、URL 或凭据。"""

    def __init__(self, code: str):
        """仅允许已注册安全错误码，未知异常归为响应错误。"""
        self.code = code if code in ERRORS else "DEEPSEEK_RESPONSE"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def strict_json(text: str | bytes):
    """严格解析 JSON，拒绝重复字段、非有限数值及非法 Unicode，不修补供应商输出。"""

    def pairs(items):
        """重复键可能隐藏错误引用，必须整体拒绝而不是取最后一项。"""
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


class DeepSeekService:
    """所有正式 LLM 请求统一在此发送；构造不联网，不下载资源，不自动重试。"""

    def __init__(self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None):
        """冻结本次配置；transport 仅供离线 Mock 注入，密钥不放入公开表示。"""
        try:
            self._settings = (settings or get_settings()).model_copy(deep=True)
        except Exception:
            raise DeepSeekError("DEEPSEEK_CONFIG") from None
        self._transport = transport

    @property
    def model_name(self) -> str:
        """返回固定配置中的模型名称，不返回配置对象或密钥。"""
        return self._settings.deepseek_model

    @property
    def max_tokens(self) -> int:
        """提供 Adapter 元数据所需输出预算，不加载模型。"""
        return self._settings.deepseek_max_tokens

    @property
    def context_chars(self) -> int:
        """返回本地完整 Prompt 字符预算，不冒充精确 token 限制。"""
        return self._settings.rag_max_context_chars

    def complete(self, system_prompt: str, user_content: str) -> str:
        """单次发送 JSON-mode 非流式请求；超时/限流/截断均失败，不重复计费重试。"""
        started, status_code = perf_counter(), None
        try:
            key = self._settings.deepseek_api_key.get_secret_value()
            if not key.strip() or any(ord(char) < 33 or ord(char) > 126 for char in key):
                raise DeepSeekError("DEEPSEEK_CONFIG")
            # 即使注入 model_construct 的配置，也不能向任意地址发送密钥。
            base = self._settings.deepseek_api_base_url
            if base not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
                raise DeepSeekError("DEEPSEEK_CONFIG")
            if (not isinstance(system_prompt, str) or not isinstance(user_content, str)
                    or not system_prompt.strip() or not user_content.strip()
                    or len(system_prompt) + len(user_content) > self.context_chars):
                raise DeepSeekError("DEEPSEEK_INPUT")
            payload = {"model": self.model_name, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}],
                "stream": False, "thinking": {"type": "disabled"}, "temperature": 0,
                "max_tokens": self.max_tokens, "response_format": {"type": "json_object"}}
            log_event("deepseek.started", model_name=self.model_name)
            # 官方直连、证书校验、无跳转；不记录 HTTP 请求头/内容或供应商错误正文。
            timeout = httpx.Timeout(self._settings.deepseek_timeout_seconds, connect=10.0)
            with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False,
                              verify=True, timeout=timeout) as client:
                with client.stream("POST", base + "/chat/completions",
                                   headers={"Authorization": "Bearer " + key}, json=payload) as response:
                    status_code = response.status_code
                    if status_code != 200:
                        code = {401: "DEEPSEEK_AUTH", 403: "DEEPSEEK_AUTH", 402: "DEEPSEEK_QUOTA",
                                429: "DEEPSEEK_RATE_LIMIT"}.get(status_code, "DEEPSEEK_HTTP")
                        raise DeepSeekError(code)
                    body = bytearray()
                    for part in response.iter_bytes():
                        if len(body) + len(part) > MAX_RESPONSE_BYTES:
                            raise DeepSeekError("DEEPSEEK_RESPONSE")
                        body.extend(part)
            envelope = strict_json(bytes(body))
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise DeepSeekError("DEEPSEEK_RESPONSE")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise DeepSeekError("DEEPSEEK_TRUNCATED")
            message = choice["message"]
            content = message["content"]
            if (message.get("role") != "assistant" or message.get("tool_calls")
                    or not isinstance(content, str) or not content.strip()):
                raise DeepSeekError("DEEPSEEK_RESPONSE")
            content.encode("utf-8", errors="strict")
            log_event("deepseek.finished", model_name=self.model_name, status_code=status_code,
                      duration_ms=(perf_counter() - started) * 1000)
            return content
        except Exception as exc:
            error = exc if isinstance(exc, DeepSeekError) else DeepSeekError(
                "DEEPSEEK_NETWORK" if isinstance(exc, httpx.HTTPError) else "DEEPSEEK_RESPONSE")
            log_event("deepseek.failed", level=logging.WARNING, error_code=error.code,
                      status_code=status_code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None
