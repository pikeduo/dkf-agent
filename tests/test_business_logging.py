"""业务日志的离线验收：隐私、请求隔离、任务关联、轮转和故障降级。"""

import asyncio
import io
import json
import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.core import business_logging as logs
from app.core import celery_logging as task_logs
from app.core.config import Settings


@pytest.fixture
def events():
    """隔离全局业务 Logger，用内存处理器读取真实 JSON，结束后恢复原有设置。"""

    logger = logs.LOGGER
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    disabled = logger.disabled
    logger.disabled = False
    token = logs.CONTEXT.set(None)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield stream
    finally:
        for current in logger.handlers:
            current.close()
        logger.handlers, logger.level, logger.propagate = handlers, level, propagate
        logger.disabled = disabled
        logs.CONTEXT.reset(token)


def records(stream):
    """将内存日志按行解析为事件字典，不依赖终端文本或第三方日志格式。"""

    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_fields_are_allowlisted_and_single_line(events):
    """敏感字段、正文和任意对象不进入日志；文件名控制字符不会伪造第二条记录。"""

    logs.log_event(
        "upload.stored",
        file_name="中文\n文档.txt",
        doc_id=uuid4(),
        api_token="SECRET",
        password="SECRET",
        url="https://cdn/?signature=SECRET",
        text="PRIVATE_BODY",
        response={"data": "PRIVATE_BODY"},
        parser=object(),
    )
    [record] = records(events)
    assert record["file_name"] == "中文文档.txt"
    assert "parser" not in record
    assert "SECRET" not in events.getvalue()
    assert "PRIVATE_BODY" not in events.getvalue()
    assert UUID(record["doc_id"])


def test_context_propagates_to_task_headers_and_is_restored(events):
    """请求编号传递到任务头，任务结束恢复上层上下文，任务结果正文不记录。"""

    request_id, task_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
    token = logs.CONTEXT.set({"request_id": request_id})
    try:
        headers = {}
        task_logs.propagate_request_id(headers=headers)
        assert headers == {"dkf_request_id": request_id}
        task = SimpleNamespace(
            name="app.tasks.mineru.check_mineru_result",
            request=SimpleNamespace(headers=headers),
        )
        task_logs.begin_task_log(task=task, task_id=task_id, args=[job_id])
        logs.log_event("mineru.http_response", status_code=200)
        task_logs.end_task_log(task=task, state="SUCCESS", retval={"text": "SECRET"})
        assert logs.CONTEXT.get() == {"request_id": request_id}
        for event in records(events):
            assert event["request_id"] == request_id
            assert event["task_id"] == task_id
            assert event["job_id"] == job_id
        assert "SECRET" not in events.getvalue()
    finally:
        logs.CONTEXT.reset(token)


def test_http_requests_concurrent_safe_route_and_summary(events):
    """并发请求有独立编号，路由使用模板，不记录查询串、请求头和正文。"""

    app = FastAPI()
    app.add_middleware(logs.RequestLoggingMiddleware)

    @app.post("/documents/{doc_id}")
    async def endpoint(doc_id: UUID):
        """返回私有响应正文以确认中间件不会自动写入日志。"""

        await asyncio.sleep(0)
        logs.log_event("upload.stored", doc_id=doc_id, file_name="中文.txt")
        return {"text": "PRIVATE_RESPONSE"}

    async def request_all():
        """同时执行两个上传请求及非法路径请求，核对各自响应关联头。"""

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        f"/documents/{uuid4()}?token=SECRET",
                        json={"text": "PRIVATE_BODY"},
                        headers={
                            "Authorization": "Bearer SECRET",
                            "X-Request-ID": "untrusted",
                        },
                    )
                    for _ in range(2)
                ]
            )
            responses.append(await client.get("/SECRET?key=SECRET"))
            return responses

    responses = asyncio.run(request_all())
    request_ids = {response.headers["x-request-id"] for response in responses}
    assert len(request_ids) == 3
    http_events = [
        event for event in records(events) if event["event"] == "http.request"
    ]
    assert {event["request_id"] for event in http_events} == request_ids
    assert {event["route"] for event in http_events} == {
        "/documents/{doc_id}",
        "<unmatched>",
    }
    assert {event["status_code"] for event in http_events} == {200, 404}
    assert all(event["duration_ms"] >= 0 for event in http_events)
    assert not logs.CONTEXT.get()
    assert "SECRET" not in events.getvalue()
    assert "PRIVATE" not in events.getvalue()


def test_unhandled_exception_logged_without_sensitive_details(events):
    """未处理异常仅记录类型并向上抛出；日志不保存异常原文，且清除上下文。"""

    async def failing_app(scope, receive, send):
        """模拟框架外层尚未处理的敏感异常，验证日志不会吞掉业务错误。"""

        raise RuntimeError("password=SECRET")

    async def exercise():
        """直接调用 ASGI 中间件验证失败路径；此路径尚未发送响应。"""

        with pytest.raises(RuntimeError):
            await logs.RequestLoggingMiddleware(failing_app)(
                {"type": "http", "method": "GET"}, None, None
            )
        assert not logs.CONTEXT.get()

    asyncio.run(exercise())
    [event] = records(events)
    assert event["status_code"] == 500
    assert event["error_type"] == "RuntimeError"
    assert "SECRET" not in events.getvalue()


def test_validation_errors_are_logged(events):
    """参数错误也产生请求日志，但不会记录 Pydantic 中包含的输入原文。"""

    app = FastAPI()
    app.add_middleware(logs.RequestLoggingMiddleware)

    @app.get("/documents/{doc_id}")
    def endpoint(doc_id: UUID):
        """构建需要 UUID 参数的验收接口。"""

        return {"doc_id": str(doc_id)}

    async def exercise():
        """请求无效参数并验证原有 HTTP 422 行为不变。"""

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/documents/SECRET")).status_code == 422

    asyncio.run(exercise())
    [event] = records(events)
    assert event["status_code"] == 422
    assert event["level"] == "WARNING"
    assert "doc_id" not in event
    assert "SECRET" not in events.getvalue()


def test_builtin_docs_routes_are_identified_safely(events):
    """框架内置 Swagger/OpenAPI 路由可识别，仍然不记录查询串或任意未知路径。"""

    app = FastAPI()
    app.add_middleware(logs.RequestLoggingMiddleware)

    async def exercise():
        """请求公开文档地址并确认回退路径来自固定白名单。"""

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/docs?token=SECRET")).status_code == 200
            assert (await client.get("/openapi.json?token=SECRET")).status_code == 200

    asyncio.run(exercise())
    assert {event["route"] for event in records(events)} == {"/docs", "/openapi.json"}
    assert "SECRET" not in events.getvalue()


def test_rotation_reconfiguration_and_process_files(events, tmp_path, monkeypatch):
    """小容量文件能轮转，重复初始化不重复写，不同进程标识使用不同文件。"""

    settings = Settings(
        _env_file=None, log_dir=tmp_path, log_max_bytes=1024, log_backup_count=2
    )
    monkeypatch.setattr(logs.os, "getpid", lambda: 101)
    logs.configure_logging(settings, "worker")
    logs.configure_logging(settings, "worker")
    assert len(logs.LOGGER.handlers) == 2
    for _ in range(25):
        logs.log_event("upload.stored", file_name="长" * 255)
    assert (tmp_path / "worker-101.log.1").exists()
    assert len(list(tmp_path.glob("worker-101.log*"))) == 3
    monkeypatch.setattr(logs.os, "getpid", lambda: 102)
    logs.configure_logging(settings, "worker")
    logs.log_event("task.started")
    assert (tmp_path / "worker-102.log").exists()


def test_unwritable_directory_falls_back_to_console(events, tmp_path, capsys):
    """目录不可创建时 API 或 Worker 仍可启动，并输出安全的降级摘要。"""

    not_directory = tmp_path / "file"
    not_directory.write_text("occupied", encoding="utf-8")
    logs.configure_logging(Settings(_env_file=None, log_dir=not_directory), "api")
    logs.log_event("upload.stored")
    assert len(logs.LOGGER.handlers) == 1
    assert "logging.file_unavailable" in capsys.readouterr().err


def test_handler_io_failure_does_not_break_business(
    events, tmp_path, monkeypatch, capsys
):
    """轮转写盘失败不会抛出异常或暴露路径，终端仍然有日志且警告不反复刷屏。"""

    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "api")
    file_handler = logs.LOGGER.handlers[1]

    def unavailable(record):
        """模拟磁盘满的轮转检查错误。"""

        raise OSError("SECRET_PATH")

    monkeypatch.setattr(file_handler, "shouldRollover", unavailable)
    logs.log_event("upload.stored")
    logs.log_event("upload.stored")
    output = capsys.readouterr().err
    assert output.count("业务日志写盘失败") == 1
    assert output.count('"event": "upload.stored"') == 2
    assert "SECRET_PATH" not in output


def test_bad_observer_cannot_fail_business(events, monkeypatch):
    """即使第三方改动 Logger 后抛出异常，日志调用也不能影响业务流程。"""

    def broken_logger(*args, **kwargs):
        """模拟意外的处理器内部异常。"""

        raise RuntimeError("SECRET")

    monkeypatch.setattr(logs.LOGGER, "log", broken_logger)
    logs.log_event("parse.persisted", block_count=3)
