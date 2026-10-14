"""双提供方隔离及错误/日志 Mock；用户运行，不访问真实 API 或读取用户 .env。"""

import io
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core import business_logging as logs
from app.core.config import Settings
from app.services.custom_llm import CustomLLMService
from app.services.deepseek import DeepSeekService
from app.services.llm import LLMError, create_llm_service


@pytest.fixture(autouse=True)
def isolated_llm_environment(monkeypatch):
    """避免用户终端旧选择器或真实 Key 污染纯内存配置断言。"""
    for name in Settings.model_fields:
        if name.startswith(("deepseek_", "custom_llm_")) or name in {"llm_provider", "rag_max_context_chars"}:
            monkeypatch.delenv(name.upper(), raising=False)


def settings(**values):
    """两组不同 Mock 凭据、预算及模型，禁止把示例当成用户真实密钥。"""
    return Settings(_env_file=None, **({
        "deepseek_provider": None, "deepseek_api_base_url": "https://api.deepseek.com",
        "deepseek_model": "deepseek-flash", "deepseek_max_tokens": 2048, "deepseek_timeout_seconds": 60,
        "deepseek_api_key": SecretStr("official-mock-secret"),
        "custom_llm_api_key": SecretStr("custom-mock-secret"),
        "custom_llm_api_base_url": "https://compatible.example/api/v1",
        "custom_llm_model": "org/compatible-model:latest",
        "custom_llm_allow_insecure_http": False,
        "custom_llm_max_tokens": 1024, "custom_llm_timeout_seconds": 45,
    } | values))


def envelope(content='{"insufficient_evidence":true,"claims":[]}', reason="stop"):
    """返回严格 JSON-mode 所需完成消息，只有内存结构。"""
    return {"choices": [{"finish_reason": reason, "message": {"role": "assistant", "content": content}}]}


def test_provider_default_invalid_selector_and_legacy_migration():
    """默认官方、旧 official 兼容；旧网关及非法新选择器初始化失败且不泄漏输入。"""
    assert settings().llm_provider == "deepseek"
    assert settings(deepseek_provider="official").llm_provider == "deepseek"
    for values in ({"llm_provider": "sub2api"}, {"deepseek_provider": "sub2api"},
                   {"llm_provider": "custom", "deepseek_provider": "sub2api"}):
        with pytest.raises(ValidationError) as failure:
            settings(**values)
        assert "official-mock-secret" not in str(failure.value)
        assert "custom-mock-secret" not in str(failure.value)


@pytest.mark.parametrize("provider", ["deepseek", "custom"])
def test_factory_only_reads_selected_fields_and_sends_one_isolated_request(provider, monkeypatch):
    """未选中配置对象的任何属性读取都失败，以证明不会访问另一方凭据。"""
    config = settings(llm_provider=provider)
    prefix = "deepseek_" if provider == "deepseek" else "custom_llm_"

    class SelectedConfig:
        """只公开所选字段，不允许共享配置退回另一方。"""
        llm_provider = provider
        rag_max_context_chars = config.rag_max_context_chars

        def __getattr__(self, name):
            """未选中字段不允许读取，也不自动提供默认密钥。"""
            if not name.startswith(prefix):
                raise AssertionError("访问了未选中提供方配置")
            return getattr(config, name)

    requests, clients = [], []
    original_client = httpx.Client

    def respond(request):
        """记录唯一 Mock 请求，返回完成 JSON 消息。"""
        requests.append(request)
        return httpx.Response(200, json=envelope())

    def client(**kwargs):
        """检查 TLS/代理/跳转与所选超时，不创建真实网络请求。"""
        clients.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    service = create_llm_service(SelectedConfig(), transport=httpx.MockTransport(respond))
    assert isinstance(service, DeepSeekService if provider == "deepseek" else CustomLLMService)
    service.validate_config()
    assert requests == [] and clients == []
    service.complete("PRIVATE_SYSTEM", "PRIVATE_QUESTION")
    assert len(requests) == len(clients) == 1
    body = json.loads(requests[0].content)
    key = "official-mock-secret" if provider == "deepseek" else "custom-mock-secret"
    assert requests[0].headers["Authorization"] == "Bearer " + key
    assert str(requests[0].url) == getattr(config, prefix + "api_base_url") + "/chat/completions"
    assert body["model"] == getattr(config, prefix + "model")
    assert body["max_tokens"] == getattr(config, prefix + "max_tokens")
    assert ("thinking" in body) == (provider == "deepseek")
    assert clients[0]["verify"] is True and clients[0]["trust_env"] is False
    assert clients[0]["follow_redirects"] is False
    assert clients[0]["timeout"].read == getattr(config, prefix + "timeout_seconds")


@pytest.mark.parametrize("provider", ["deepseek", "custom"])
def test_unselected_empty_key_and_invalid_url_do_not_affect_selected_provider(provider):
    """另一方无密钥甚至地址未部署时，只验证选中配置，保持单一请求。"""
    unused = "custom_llm" if provider == "deepseek" else "deepseek"
    config = settings(llm_provider=provider, **{unused + "_api_key": SecretStr(""), unused + "_api_base_url": "invalid"})
    requests = []

    def respond(request):
        """只有选中端点得到 Mock 响应，不能尝试另一方。"""
        requests.append(request)
        return httpx.Response(200, json=envelope())

    create_llm_service(config, transport=httpx.MockTransport(respond)).complete("JSON", "question")
    assert len(requests) == 1


def test_selected_service_keeps_independent_frozen_budget_and_config():
    """已有服务冻结当前配置；切换选择器或修改另一组预算不改变在途服务。"""
    config = settings(llm_provider="custom")
    service = create_llm_service(config)
    config.llm_provider = "deepseek"
    config.custom_llm_model = "different-model"
    config.custom_llm_max_tokens = 4096
    assert service.provider == "custom" and service.model_name == "org/compatible-model:latest"
    assert service.max_tokens == 1024
    assert create_llm_service(config).provider == "deepseek"


@pytest.mark.parametrize("url", ["invalid", "ftp://compatible.example/v1", "http://compatible.example/v1",
    "https://user:PRIVATE_KEY@compatible.example/v1", "https://compatible.example/v1?key=PRIVATE_KEY",
    "https://compatible.example/v1#PRIVATE", "https://compatible.example:0/v1", "https://compatible.example:99999/v1",
    "https://compatible.example/v1/../private", "https://compatible.example/v1/chat/completions",
    "https://compatible.example/v1\n", "https://compatible.example/%2e%2e/v1"])
def test_custom_invalid_endpoint_refused_before_request(url):
    """无效端点、明文HTTP及地址注入在任何请求前安全拒绝。"""
    calls = []

    def respond(request):
        """若校验失效只记意外调用，不访问外网。"""
        calls.append(request)
        return httpx.Response(200, json=envelope())

    config = settings(llm_provider="custom", custom_llm_api_base_url=url)
    with pytest.raises(LLMError, match="LLM_CONFIG") as failure:
        create_llm_service(config, transport=httpx.MockTransport(respond)).complete("JSON", "PRIVATE_QUESTION")
    assert calls == [] and failure.value.provider == "custom"
    assert "PRIVATE" not in str(failure.value) and "compatible.example" not in str(failure.value)


def test_custom_http_requires_own_opt_in_and_official_service_never_becomes_gateway():
    """旧 DeepSeek HTTP 开关不能授权自定义HTTP；直接官方服务也不能冒充网关。"""
    config = settings(llm_provider="custom", custom_llm_api_base_url="http://compatible.example:8080/v1",
                      deepseek_allow_insecure_http=True)
    with pytest.raises(LLMError, match="LLM_CONFIG"):
        CustomLLMService(config).validate_config()
    config.custom_llm_allow_insecure_http = True
    CustomLLMService(config).validate_config()
    DeepSeekService(config).validate_config()
    config.deepseek_api_base_url = config.custom_llm_api_base_url
    with pytest.raises(LLMError, match="DEEPSEEK_CONFIG"):
        DeepSeekService(config).validate_config()


@pytest.mark.parametrize("key", ["", "PRIVATE KEY", "PRIVATE\nKEY", "中文密钥"])
def test_selected_custom_invalid_key_never_sends_request(key):
    """选中提供方的空/非法 Key 拒绝，而不是使用另一个已配置 Key。"""
    config = settings(llm_provider="custom", custom_llm_api_key=SecretStr(key))
    with pytest.raises(LLMError, match="LLM_CONFIG"):
        create_llm_service(config).validate_config()


@pytest.mark.parametrize("status,code", [(401, "LLM_AUTH"), (403, "LLM_AUTH"), (402, "LLM_QUOTA"),
    (429, "LLM_RATE_LIMIT"), (500, "LLM_HTTP"), (302, "LLM_HTTP")])
def test_custom_errors_never_retry_or_fallback_and_hide_upstream(status, code):
    """即使官方密钥可用，自定义错误也只调用一次，不切换或泄漏上游内容。"""
    requests = []

    def respond(request):
        """错误体和Location包含私有标记，断言异常仍为安全摘要。"""
        requests.append(request)
        return httpx.Response(status, text="PRIVATE_BODY", headers={"Location": "https://api.deepseek.com"})

    with pytest.raises(LLMError, match=code) as failure:
        create_llm_service(settings(llm_provider="custom"), transport=httpx.MockTransport(respond)).complete("JSON", "PRIVATE")
    assert len(requests) == 1 and requests[0].url.host == "compatible.example"
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("kind", ["network", "bad_structure", "truncated"])
def test_custom_network_and_response_errors_are_distinct(kind):
    """区分网络、响应结构及截断，失败不把部分输出作为证据不足回答。"""
    calls = []

    def respond(request):
        """模拟单次故障，所有异常只留在内存。"""
        calls.append(request)
        if kind == "network":
            raise httpx.ReadTimeout("PRIVATE_ERROR", request=request)
        return httpx.Response(200, json={} if kind == "bad_structure" else envelope(reason="length"))

    expected = {"network": "LLM_NETWORK", "bad_structure": "LLM_RESPONSE", "truncated": "LLM_TRUNCATED"}[kind]
    with pytest.raises(LLMError, match=expected):
        create_llm_service(settings(llm_provider="custom"), transport=httpx.MockTransport(respond)).complete("JSON", "question")
    assert len(calls) == 1


@pytest.mark.parametrize("provider,status", [("deepseek", 200), ("custom", 200), ("custom", 401)])
def test_provider_logs_are_generic_safe_and_whitelisted(provider, status):
    """记录固定提供方/模型/状态，不记录URL、两个Key、Prompt、问题、输出或上游原文。"""
    output, logger = io.StringIO(), logs.LOGGER
    previous = list(logger.handlers), logger.level, logger.propagate, logger.disabled
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("rag-cli"))
    token = logs.CONTEXT.set(None)

    def respond(request):
        """成功及失败均含私有标记，不能进入业务日志。"""
        return httpx.Response(status, json=envelope("PRIVATE_ANSWER"))

    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        service = create_llm_service(settings(llm_provider=provider), transport=httpx.MockTransport(respond))
        if status == 200:
            service.complete("PRIVATE_SYSTEM", "PRIVATE_QUESTION")
        else:
            with pytest.raises(LLMError):
                service.complete("PRIVATE_SYSTEM", "PRIVATE_QUESTION")
        content = output.getvalue()
        assert "llm.started" in content and ("llm.finished" if status == 200 else "llm.failed") in content
        assert f'提供方="{provider}"' in content and "开始单次 LLM" in content
        assert not any(value in content for value in ("PRIVATE", "official-mock-secret", "custom-mock-secret",
                                                     "compatible.example", "api.deepseek.com"))
        assert logs.safe_fields({"provider": "https://PRIVATE", "api_key": "PRIVATE"}) == {}
        assert logs.safe_fields({"provider": []}) == {}
    finally:
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
        handler.close()
