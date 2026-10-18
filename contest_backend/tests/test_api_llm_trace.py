"""HTTP鉴权、模型费用与安全Trace用例，使用内存传输且不消耗真实费用。"""

import json
import logging
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import business_logging as logs
from contest_backend.errors import ContestError
from contest_backend.llm import LLMClient
from contest_backend.main import create_app
from contest_backend.tests.conftest import request_for


def configured_model(settings, handler):
    """配置保留域名和测试专用假Key，HTTP请求仅由MockTransport处理。"""
    config = settings.model_copy(update={"llm_api_base_url": "https://model.invalid/v1", "llm_model": "test"})
    from pydantic import SecretStr
    config.llm_api_key = SecretStr("unit-test-only")
    return LLMClient(config, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("mode", ["timeout", "upstream", "bad_json", "bad_shape", "redirect"])
def test_model_failure_is_one_request_and_blocks_future_budget(settings, build_service, mode):
    """超时/上游异常/坏响应均不重试、不带原异常返回，并封闭后续预算。"""
    service, session, _, _, _, _ = build_service()
    policy = service.issue_consent(session, provider_id="contest", max_calls=2,
                                   max_total_tokens=32768, allow_sql_repair=True)
    calls = []

    def handler(request):
        """只在测试内存中构造失败，不访问真实供应商。"""
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("SECRET-UPSTREAM", request=request)
        if mode == "upstream":
            return httpx.Response(500, text="SECRET-UPSTREAM")
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid"})
        return httpx.Response(200, text="invalid" if mode == "bad_json" else '{"choices":[null]}')

    model, grant = configured_model(settings, handler), session.grant(policy)
    with pytest.raises(ContestError) as caught:
        model.complete([{"role": "user", "content": "私密问题"}], grant)
    assert caught.value.code == "MODEL_FAILURE" and "SECRET-UPSTREAM" not in str(caught.value)
    assert len(calls) == 1 and grant.calls == 1 and grant.uncertain
    with pytest.raises(ContestError):
        model.complete([{"role": "user", "content": "私密问题"}], grant)
    assert len(calls) == 1


def test_unknown_usage_does_not_invent_tokens_or_allow_extra_request(settings, build_service):
    """本次完整结果可用，未知实际Token保持未知并阻断额外模型请求。"""
    service, session, _, _, _, _ = build_service()
    policy = service.issue_consent(session, provider_id="contest", max_calls=2,
                                   max_total_tokens=32768, allow_sql_repair=True)

    def handler(request):
        """模拟完整输出但缺少用量，不把估算当账单。"""
        return httpx.Response(200, json={"choices": [{"message": {"content": "SELECT COUNT(*) FROM sales"},
                                                    "finish_reason": "stop"}], "usage": None})

    grant = session.grant(policy)
    assert configured_model(settings, handler).complete([], grant).startswith("SELECT")
    assert grant.uncertain
    with pytest.raises(ContestError):
        grant.reserve(1)


def test_budget_exhaustion_never_opens_model_transport(settings, build_service):
    """调用前预算不足即拒绝，没有网络请求可产生收费。"""
    service, session, _, _, _, _ = build_service()
    policy = service.issue_consent(session, provider_id="contest", max_calls=1,
                                   max_total_tokens=1, allow_sql_repair=False)
    calls = []

    def handler(request):
        """该传输不应被触达，任何调用都让用例失败。"""
        calls.append(request)
        raise AssertionError("预算不足不应请求模型")

    with pytest.raises(ContestError) as caught:
        configured_model(settings, handler).complete([], session.grant(policy))
    assert caught.value.code == "BUDGET_EXHAUSTED" and calls == []


def test_http_requires_global_and_session_secrets_and_safe_validation(settings, build_service):
    """同源不等于鉴权；UUID无法读取其他会话Trace，422不回显私密输入。"""
    service, session, token, _, _, _ = build_service()

    def factory(config):
        """API仅使用隔离Service，不创建生产依赖或日志目录。"""
        return service

    client = TestClient(create_app(settings, service_factory=factory))
    headers = {"Authorization": "Bearer unit-test-token"}
    assert client.get("/api/catalog").status_code == 401
    assert client.get("/health").json()["database_verified"] is False
    body = request_for(session).model_dump(mode="json")
    rejected = client.post("/api/query", headers=headers, json=body)
    assert rejected.status_code == 403
    headers["X-Session-Token"] = token
    body["confirmed_slots"] = {"secret": "PRIVATE_INPUT"}
    invalid = client.post("/api/query", headers=headers, json=body)
    assert invalid.status_code == 422 and "PRIVATE_INPUT" not in invalid.text
    body.pop("confirmed_slots")
    response = client.post("/api/query", headers=headers, json=body)
    assert response.status_code == 403 and response.json()["result"]["code"] == "AUTHORIZATION_REQUIRED"
    assert response.headers["X-Request-ID"] == response.json()["context"]["request_id"]
    trace_id = response.json()["context"]["trace_id"]
    other, other_token = service.sessions.create(session.source)
    refused = client.get(f"/api/traces/{trace_id}", params={"session_id": str(other.id)},
                         headers={**headers, "X-Session-Token": other_token})
    assert refused.status_code == 403
    snapshot = client.get(f"/api/traces/{trace_id}", params={"session_id": str(session.id)}, headers=headers)
    assert snapshot.status_code == 200 and snapshot.json()["trace_id"] == trace_id
    assert client.post("/api/agent/query", headers=headers, json=body).status_code == 404


def test_safe_trace_and_logs_exclude_payloads(build_service, monkeypatch):
    """真实Service日志字段和安全Trace均不得保存原问题、Prompt、SQL或结果。"""
    records = []

    def capture(event, **fields):
        """只接收日志调用的参数用于泄漏断言，不写文件。"""
        records.append({"event": event, **fields})

    monkeypatch.setattr("contest_backend.tracing.log_event", capture)
    service, session, _, policy, model, _ = build_service()
    response = service.query(request_for(session, policy), session)
    serialized = json.dumps({"records": records, "snapshot": session.traces[response.context.trace_id]}, default=str)
    assert "统计sales" not in serialized and model.sql not in serialized and '"rows"' not in serialized
    assert str(response.context.trace_id) in serialized and str(response.context.run_id) in serialized
    assert "prompt" not in serialized.lower() and "confirmed_slots" not in serialized


def test_registered_role_and_trace_uuid_do_not_weaken_existing_redaction():
    """角色唯一表及UUID白名单只允许安全摘要，恶意字段不能透传。"""
    identity = uuid4()
    assert logs.ROLE_LABELS["contest-api"] == "竞赛问数 API"
    assert logs.safe_fields({"trace_id": identity, "question": "PRIVATE", "sql": "PRIVATE"}) == {"trace_id": str(identity)}
    assert logs.safe_fields({"trace_id": "PRIVATE"}) == {}
    record = logging.LogRecord("dkf.business", logging.INFO, __file__, 1, "contest.step", (), None)
    record.business_fields = {"trace_id": str(identity), "question": "PRIVATE"}
    line = logs.ReadableFormatter("contest-api").format(record)
    assert "竞赛问数 API" in line and "PRIVATE" not in line and str(identity) in line
