"""用户执行的双提供方HTTPX Mock；不访问真实网络、不读取本地.env或产生费用。"""

import io
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.core import business_logging as logs
from app.core.config import Settings
from app.services.llm import CLOUD_CALL_ALLOWED, FallbackLLMService, LLMError, create_llm_service, llm_cloud_call
from test_llm_service import envelope, settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """避免PowerShell同名环境变量或真实密钥污染纯Mock配置。"""
    for name in Settings.model_fields:
        if name.startswith(("llm_", "custom_llm_", "deepseek_")):
            monkeypatch.delenv(name.upper(), raising=False)


def config(**overrides):
    """明确双方独立Mock配置，授权开关只在本测试内，不修改用户配置。"""
    return settings(**({"llm_provider": "custom", "llm_fallback_enabled": True,
                        "llm_fallback_provider": "deepseek"} | overrides))


def complete(service):
    """模拟已有业务allow_cloud=true保护域，结束后授权必须恢复。"""
    with llm_cloud_call(allow_cloud=True):
        return service.complete("PRIVATE_SYSTEM", "PRIVATE_CONTEXT")


@pytest.mark.parametrize("fault", [None, "connect", "connect_timeout", "read_timeout", "protocol", 500, 502, 503, 504, 599, 429])
def test_success_or_one_eligible_fallback_preserves_messages_json_and_independent_credentials(fault):
    """只对网络/超时/5xx/429切换，原messages/JSON要求逐字相同，密钥和模型不混用。"""
    requests = []

    def respond(request):
        """所有请求均在内存模拟，不创建真实连接。"""
        requests.append(request)
        if request.url.host == "compatible.example" and fault is not None:
            errors = {"connect": httpx.ConnectError, "connect_timeout": httpx.ConnectTimeout,
                      "read_timeout": httpx.ReadTimeout, "protocol": httpx.RemoteProtocolError}
            if isinstance(fault, str):
                raise errors[fault]("PRIVATE_ERROR", request=request)
            return httpx.Response(fault, text="PRIVATE_UPSTREAM")
        return httpx.Response(200, json=envelope('{"ok":true}'))

    service = create_llm_service(config(), transport=httpx.MockTransport(respond))
    assert isinstance(service, FallbackLLMService) and complete(service) == '{"ok":true}'
    assert [item.url.host for item in requests] == (["compatible.example"] if fault is None else ["compatible.example", "api.deepseek.com"])
    first = json.loads(requests[0].content)
    assert first["model"] == "org/compatible-model:latest"
    assert requests[0].headers["authorization"] == "Bearer custom-mock-secret"
    if fault is not None:
        second = json.loads(requests[1].content)
        assert second["model"] == "deepseek-flash" and second["max_tokens"] == 2048
        assert requests[1].headers["authorization"] == "Bearer official-mock-secret"
        for field in ("messages", "response_format", "temperature", "stream"):
            assert second[field] == first[field]
    assert CLOUD_CALL_ALLOWED.get() is False


@pytest.mark.parametrize("status", [301, 400, 401, 402, 403, 404, 408, 409, 413, 422, 451])
def test_client_errors_and_redirects_never_fallback_even_with_valid_backup(status):
    """普通4xx（包括HTTP408）、鉴权/配额及跳转不切换；只有429明确例外。"""
    requests = []

    def respond(request):
        """返回原始错误状态，错误体不能透传。"""
        requests.append(request)
        return httpx.Response(status, text="PRIVATE_ERROR")

    with pytest.raises(LLMError) as error:
        complete(create_llm_service(config(), transport=httpx.MockTransport(respond)))
    assert len(requests) == 1 and error.value.status_code == status and error.value.provider == "custom"
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("fault", ["network", "http", "content_json", "envelope", "truncated"])
def test_backup_failure_stops_after_two_requests_and_keeps_official_error(fault):
    """备用失败不回custom、不继续循环；非法JSON/响应/截断也不能发布为成功。"""
    requests = []

    def respond(request):
        """分别模拟主网络错误与备用失败，至多触发两次内存请求。"""
        requests.append(request)
        if request.url.host == "compatible.example" or fault == "network":
            raise httpx.ReadTimeout("PRIVATE", request=request)
        if fault == "http":
            return httpx.Response(503, text="PRIVATE")
        return httpx.Response(200, json={} if fault == "envelope" else envelope(
            "PRIVATE_INVALID_JSON" if fault == "content_json" else '{}', reason="length" if fault == "truncated" else "stop"))

    expected = {"network": "DEEPSEEK_NETWORK", "http": "DEEPSEEK_HTTP", "content_json": "DEEPSEEK_RESPONSE",
                "envelope": "DEEPSEEK_RESPONSE", "truncated": "DEEPSEEK_TRUNCATED"}[fault]
    with pytest.raises(LLMError, match=expected) as error:
        complete(create_llm_service(config(), transport=httpx.MockTransport(respond)))
    assert len(requests) == 2 and error.value.provider == "deepseek" and "PRIVATE" not in str(error.value)
    assert CLOUD_CALL_ALLOWED.get() is False


@pytest.mark.parametrize("content", ["PRIVATE_BAD_JSON", '[1]', '{"x":1,"x":2}', '{"x":NaN}'])
def test_primary_invalid_content_is_not_a_reason_to_switch(content):
    """模型输出不是运输故障；主模型即使返回非法JSON也不调用备用修补。"""
    requests = []

    def respond(request):
        """返回成功信封中的非法JSON-mode内容。"""
        requests.append(request)
        return httpx.Response(200, json=envelope(content))

    with pytest.raises(LLMError, match="LLM_RESPONSE"):
        complete(create_llm_service(config(), transport=httpx.MockTransport(respond)))
    assert len(requests) == 1


def test_fallback_disabled_keeps_single_selected_provider_behavior():
    """关闭时不读取备用密钥/端点，保留原单提供方类型和网络错误。"""
    calls = []

    def respond(request):
        """仅主请求一次，不联网。"""
        calls.append(request)
        raise httpx.ConnectError("PRIVATE", request=request)

    service = create_llm_service(config(llm_fallback_enabled=False, deepseek_api_key="",
        deepseek_api_base_url="invalid"), transport=httpx.MockTransport(respond))
    assert not isinstance(service, FallbackLLMService)
    with pytest.raises(LLMError, match="LLM_NETWORK"):
        complete(service)
    assert len(calls) == 1


@pytest.mark.parametrize("system,user", [("", "q"), ("s", " "), (None, "q"), ("s", "x" * 60001)])
def test_invalid_or_over_budget_input_never_sends_or_switches_provider(system, user):
    """普通参数或完整上下文预算错误不是备用理由，主备请求数均为零。"""
    calls = []

    def respond(request):
        """参数拒绝发生在创建HTTP请求之前。"""
        calls.append(request)
        return httpx.Response(200, json=envelope('{}'))

    service = create_llm_service(config(), transport=httpx.MockTransport(respond))
    with llm_cloud_call(allow_cloud=True):
        with pytest.raises(LLMError, match="LLM_INPUT"):
            service.complete(system, user)
    assert calls == []


@pytest.mark.parametrize("permission", [False, None, 1, "true"])
def test_no_per_request_cloud_permission_means_zero_requests(permission):
    """开启配置不等于本次授权；非布尔True也不能调用任何提供方。"""
    calls = []

    def respond(request):
        """不应该被调用，仍仅使用内存Transport。"""
        calls.append(request)
        return httpx.Response(200, json=envelope('{}'))

    service = create_llm_service(config(), transport=httpx.MockTransport(respond))
    with llm_cloud_call(allow_cloud=permission):
        with pytest.raises(LLMError, match="LLM_CLOUD_PERMISSION"):
            service.complete("JSON", "PRIVATE")
    assert calls == [] and CLOUD_CALL_ALLOWED.get() is False


@pytest.mark.parametrize("overrides", [{"deepseek_api_key": ""}, {"deepseek_api_base_url": "http://api.deepseek.com"},
    {"custom_llm_api_base_url": "http://custom.example/v1"}])
def test_invalid_primary_or_backup_configuration_never_sends_request(overrides):
    """双方配置/TLS预检失败不先产生主模型费用，也不能借Fallback绕过HTTP授权。"""
    calls = []

    def respond(request):
        """配置无效时任何调用均属缺陷。"""
        calls.append(request)
        return httpx.Response(200, json=envelope('{}'))

    with pytest.raises(LLMError):
        complete(create_llm_service(config(**overrides), transport=httpx.MockTransport(respond)))
    assert calls == []


def test_same_or_reverse_provider_configuration_is_rejected():
    """配置不允许官方→官方、自定义→自定义或自动循环切换。"""
    with pytest.raises(ValidationError):
        config(llm_provider="deepseek")
    with pytest.raises(ValidationError):
        config(llm_fallback_provider="custom")


def test_request_authorization_is_restored_after_nested_scope_and_failure():
    """嵌套拒绝可临时收紧授权，异常离开后不向下一次请求泄漏许可。"""
    assert CLOUD_CALL_ALLOWED.get() is False
    with pytest.raises(RuntimeError):
        with llm_cloud_call(allow_cloud=True):
            raise RuntimeError("PRIVATE_STUB")
    assert CLOUD_CALL_ALLOWED.get() is False
    with llm_cloud_call(allow_cloud=True):
        assert CLOUD_CALL_ALLOWED.get() is True
        with llm_cloud_call(allow_cloud=False):
            assert CLOUD_CALL_ALLOWED.get() is False
        assert CLOUD_CALL_ALLOWED.get() is True
    assert CLOUD_CALL_ALLOWED.get() is False


def test_fallback_logs_only_safe_provider_models_error_codes_and_final_actual_provider():
    """主失败/备用/结束均可追溯，禁止两个Key、Prompt、上下文、原响应和完整URL进入日志。"""
    output, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("knowledge-agent-cli"))
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)

    def respond(request):
        """只产生一次主失败和一次备用成功。"""
        if request.url.host == "compatible.example":
            raise httpx.ConnectError("PRIVATE_NETWORK", request=request)
        return httpx.Response(200, json=envelope('{"answer":"PRIVATE_ANSWER"}'))

    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        complete(create_llm_service(config(), transport=httpx.MockTransport(respond)))
        content = output.getvalue()
        assert all(event in content for event in ("llm.failed", "LLM_NETWORK", "llm.fallback_started", "llm.fallback_finished", "llm.call_finished"))
        final = [line for line in content.splitlines() if "event=llm.call_finished" in line][0]
        assert '提供方="deepseek"' in final and '模型="deepseek-flash"' in final
        assert not any(value in content for value in ("PRIVATE", "official-mock-secret", "custom-mock-secret", "compatible.example", "api.deepseek.com"))
    finally:
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
        handler.close()
