"""无需 Redis 的任务与接口边界测试；真实队列由独立 Worker 联调。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from kombu.exceptions import OperationalError

from app.core.celery_app import celery_app
from app.main import app
from app.tasks.demo import add


def api_request(method, path, **kwargs):
    # 直接使用项目已声明的 httpx，通过 ASGI 测试接口，不新增客户端依赖。
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


def test_add_is_idempotent_and_failure_is_recorded():
    assert add.apply(args=(2, 3)).get() == 5
    assert add.apply(args=(2, 3)).get() == 5
    failed = add.apply(args=("2", 3), throw=False)
    assert failed.state == "FAILURE"
    assert isinstance(failed.result, ValueError)
    assert failed.traceback


@pytest.mark.parametrize(
    "payload",
    [{"x": "2", "y": 3}, {"x": True, "y": 3}, {"x": 2, "y": 3, "queue": "unknown"}],
)
def test_invalid_task_is_not_published(payload, monkeypatch):
    def unexpected_publish(*args, **kwargs):
        pytest.fail("无效请求不应投递任务")

    monkeypatch.setattr(add, "apply_async", unexpected_publish)
    assert api_request("POST", "/tasks/add", json=payload).status_code == 422


def test_broker_failure_returns_503_without_credentials(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OperationalError("redis://secret:password@localhost")

    monkeypatch.setattr(add, "apply_async", unavailable)
    response = api_request("POST", "/tasks/add", json={"x": 2, "y": 3})
    assert response.status_code == 503
    assert "password" not in response.text


def test_result_lookup_and_invalid_id(monkeypatch):
    monkeypatch.setattr(
        type(celery_app.backend),
        "get_task_meta",
        lambda self, task_id: {"status": "SUCCESS", "result": 5},
    )
    response = api_request("GET", f"/tasks/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["result"] == 5
    assert api_request("GET", "/tasks/not-a-uuid").status_code == 422


def test_result_backend_failure_returns_503(monkeypatch):
    def unavailable(self, task_id):
        raise OperationalError("redis://secret:password@localhost")

    monkeypatch.setattr(type(celery_app.backend), "get_task_meta", unavailable)
    response = api_request("GET", f"/tasks/{uuid4()}")
    assert response.status_code == 503
    assert "password" not in response.text


def test_failure_result_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        type(celery_app.backend),
        "get_task_meta",
        lambda self, task_id: {"status": "FAILURE", "result": ValueError("secret")},
    )
    response = api_request("GET", f"/tasks/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["status"] == "FAILURE"
    assert response.json()["result"] is None
    assert "secret" not in response.text
