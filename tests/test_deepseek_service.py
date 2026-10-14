"""用户执行的 DeepSeek 离线 HTTP Mock 用例，不请求云 API、不安装依赖。"""

import io
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core import business_logging as logs
from app.core.config import Settings
from app.services.deepseek import DeepSeekError, DeepSeekService, MAX_RESPONSE_BYTES, strict_json


def settings(**values):
    """创建不读取本地 .env 的安全 Mock 配置，密钥不是用户真实值。"""
    defaults = {"deepseek_api_key": SecretStr("mock-secret"), "deepseek_model": "deepseek-flash",
                "deepseek_api_base_url": "https://api.deepseek.com", "deepseek_max_tokens": 2048,
                "deepseek_timeout_seconds": 60, "rag_max_context_chars": 60000}
    return Settings(_env_file=None, **(defaults | values))


def envelope(content='{"insufficient_evidence":true,"claims":[]}', reason="stop", **message):
    """构造供应商 JSON envelope，保留测试需要的结束状态和消息类型。"""
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "content": content, **message}}]}


def test_single_official_request_and_bounded_non_thinking_json_mode(monkeypatch):
    """一个调用只发一个官方 POST，显式关闭思考/流式，TLS/直连/跳转限制不放宽。"""
    requests, clients = [], []
    original_client = httpx.Client

    def respond(request):
        """只记录 Mock 请求并返回完成响应，不访问网络。"""
        requests.append(request)
        return httpx.Response(200, json=envelope())

    def client(**kwargs):
        """检查正式客户端约束，再委托内存 transport。"""
        clients.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    service = DeepSeekService(settings(), transport=httpx.MockTransport(respond))
    assert requests == [] and "mock-secret" not in repr(service)
    assert service.complete("只返回 JSON", "PRIVATE_PROMPT") == envelope()["choices"][0]["message"]["content"]
    assert len(requests) == 1 and requests[0].url == "https://api.deepseek.com/chat/completions"
    payload = json.loads(requests[0].content)
    assert payload["messages"] == [{"role": "system", "content": "只返回 JSON"},
                                   {"role": "user", "content": "PRIVATE_PROMPT"}]
    assert payload["thinking"] == {"type": "disabled"} and payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"} and payload["stream"] is False
    assert payload["model"] == settings().deepseek_model and payload["max_tokens"] == 2048
    assert clients[0]["verify"] is True and clients[0]["trust_env"] is False
    assert clients[0]["follow_redirects"] is False


@pytest.mark.parametrize("status,code", [(401, "DEEPSEEK_AUTH"), (403, "DEEPSEEK_AUTH"),
    (402, "DEEPSEEK_QUOTA"), (429, "DEEPSEEK_RATE_LIMIT"), (500, "DEEPSEEK_HTTP"), (302, "DEEPSEEK_HTTP")])
def test_http_failures_are_safe_and_never_retried(status, code):
    """鉴权/限额/错误/跳转不重试、不跟随 URL，不暴露上游私有错误。"""
    requests = []

    def respond(request):
        """模拟 HTTP 错误与恶意跳转头，不做外部请求。"""
        requests.append(request)
        return httpx.Response(status, text="PRIVATE_RESPONSE mock-secret",
                              headers={"Location": "https://invalid.example/PRIVATE"})

    service = DeepSeekService(settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(DeepSeekError, match=code) as failure:
        service.complete("JSON", "PRIVATE_QUESTION")
    assert len(requests) == 1 and "PRIVATE" not in str(failure.value) and "mock-secret" not in str(failure.value)


def test_timeout_no_retry_and_preserves_safe_network_classification():
    """响应可能已计费的超时只返回固定失败，不隐式重试或伪装为证据不足。"""
    requests = []

    def timeout(request):
        """模拟单次网络故障，错误原文不得外泄。"""
        requests.append(request)
        raise httpx.ReadTimeout("PRIVATE_PATH PRIVATE_KEY", request=request)

    with pytest.raises(DeepSeekError, match="DEEPSEEK_NETWORK") as failure:
        DeepSeekService(settings(), transport=httpx.MockTransport(timeout)).complete("JSON", "question")
    assert len(requests) == 1 and "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("body", [envelope(reason="length"), envelope(content=""),
    envelope(role="user"), envelope(tool_calls=[{"private": "PRIVATE"}]), {},
    {"choices": []}, {"choices": [envelope()["choices"][0]] * 2}])
def test_invalid_or_truncated_envelopes_do_not_publish_partial_text(body):
    """缺字段/多结果/工具/未完整结束统一拒绝，不返回部分内容。"""
    def respond(request):
        """供应商内存 envelope，不调用真实服务。"""
        return httpx.Response(200, json=body)

    with pytest.raises(DeepSeekError):
        DeepSeekService(settings(), transport=httpx.MockTransport(respond)).complete("JSON", "question")


def test_response_size_budget_and_missing_key_fail_safely():
    """响应预算有限，空密钥在请求前失败，不读取本地用户凭据。"""
    requests = []

    def respond(request):
        """生成内存超量响应，不创建文档/文件。"""
        requests.append(request)
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    service = DeepSeekService(settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(DeepSeekError, match="DEEPSEEK_RESPONSE"):
        service.complete("JSON", "question")
    config = settings()
    config.deepseek_api_key = SecretStr("")
    with pytest.raises(DeepSeekError, match="DEEPSEEK_CONFIG"):
        DeepSeekService(config, transport=httpx.MockTransport(respond)).complete("JSON", "question")
    assert len(requests) == 1


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '"\\ud800"', "invalid"])
def test_strict_json_rejects_ambiguous_nonfinite_and_invalid_unicode(raw):
    """重复字段、非有限数、非法 Unicode 不靠宽松解析绕过回答契约。"""
    with pytest.raises((ValueError, UnicodeError)):
        strict_json(raw)


@pytest.mark.parametrize("initially_disabled", [False, True])
def test_business_logs_never_include_llm_content_or_credentials(initially_disabled):
    """隔离并恢复日志状态，验证已有INFO禁用缓存/Logger禁用不影响摘要及脱敏断言。"""
    logger = logs.LOGGER
    output = io.StringIO()
    previous, level, propagate, disabled = list(logger.handlers), logger.level, logger.propagate, logger.disabled
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("rag-cli"))
    token = logs.CONTEXT.set(None)

    def respond(request):
        """模拟私有输出，不发真实请求。"""
        return httpx.Response(200, json=envelope(content="PRIVATE_ANSWER"))

    try:
        # 明确复现前序测试留下的INFO禁用缓存，不能只给level属性赋值来开启日志。
        logger.disabled = False
        logger.setLevel(logging.WARNING)
        assert not logger.isEnabledFor(logging.INFO)
        logger.disabled = initially_disabled
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        DeepSeekService(settings(), transport=httpx.MockTransport(respond)).complete("PRIVATE_SYSTEM", "PRIVATE_QUERY")
        content = output.getvalue()
        assert "deepseek.started" in content and "deepseek.finished" in content and "RAG CLI" in content
        assert "PRIVATE" not in content and "mock-secret" not in content
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous, propagate, disabled
        # 恢复同样使用setLevel清理缓存，避免污染后续日志测试。
        logger.setLevel(level)
        logs.CONTEXT.reset(token)
