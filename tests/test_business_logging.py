"""业务日志的离线验收：隐私、请求隔离、任务关联、每日合并和故障降级。"""

import ast
import asyncio
import io
import logging
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from business_log_helpers import read_log_line
from fastapi import FastAPI

from app.core import business_logging as logs
from app.core import celery_logging as task_logs
from app.core.config import Settings


@pytest.fixture
def events():
    """隔离全局业务 Logger，用内存处理器读取真实中文摘要，结束后恢复原有设置。"""

    logger = logs.LOGGER
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    disabled = logger.disabled
    logger.disabled = False
    token = logs.CONTEXT.set(None)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter())
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

    return [read_log_line(line) for line in stream.getvalue().splitlines()]


def test_agent_workflow_fields_only_accept_registered_safe_scalars(events):
    """新增Agent观测字段仅允许UUID/固定枚举/有界步数/有限耗时，不开放任意请求内容。"""
    run_id = uuid4()
    logs.log_event("agent.finished", agent_run_id=run_id, selected_route="knowledge_query",
        tool_name="query_knowledge_base", step_count=5, tool_duration_ms=2.5, final_status="success",
        question="PRIVATE_QUERY", evidence="PRIVATE_BODY", formula_results="PRIVATE_RESULT")
    result = records(events)[0]
    assert result["agent_run_id"] == str(run_id) and result["step_count"] == 5
    assert result["selected_route"] == "knowledge_query" and result["tool_name"] == "query_knowledge_base"
    assert result["tool_duration_ms"] == 2.5 and result["final_status"] == "success"
    assert "PRIVATE" not in events.getvalue()
    assert logs.safe_fields({"agent_run_id": "PRIVATE", "selected_route": "PRIVATE", "tool_name": "PRIVATE",
        "final_status": "PRIVATE", "step_count": True, "tool_duration_ms": float("nan")}) == {}


def test_management_task_uses_worker_doc_context_and_safe_registered_event(events):
    """管理任务保留doc/request关联和原Worker角色，不输出私有返回值或另建日志文件。"""
    doc_id, task_id = str(uuid4()), str(uuid4())
    task = SimpleNamespace(name="app.tasks.management.manage_document", request=SimpleNamespace(headers={}))
    task_logs.begin_task_log(task=task, task_id=task_id, args=[doc_id])
    try:
        logs.log_event("management.advanced", stage="chunk", file_path="PRIVATE_PATH", body="PRIVATE_BODY")
    finally:
        task_logs.end_task_log(task=task, state="RETRY", retval={"PRIVATE": "PRIVATE_BODY"})
    output = records(events)
    assert all(item["doc_id"] == doc_id and item["task_id"] == task_id for item in output)
    assert any(item["event"] == "management.advanced" for item in output)
    assert "PRIVATE" not in events.getvalue() and logs.CONTEXT.get() is None


@pytest.mark.parametrize("provider", ["deepseek", "custom"])
def test_llm_provider_uses_safe_enum_and_generic_event_labels(events, provider):
    """提供方只记录固定枚举，保留每日中文格式、真实PID及原有脱敏白名单。"""
    logs.log_event("llm.started", provider=provider, model_name="compatible-model",
                   api_key="PRIVATE_KEY", url="http://PRIVATE", prompt="PRIVATE_PROMPT")
    item = records(events)[0]
    assert item["event"] == "llm.started" and item["pid"] == os.getpid()
    assert item["provider"] == provider and item["model_name"] == "compatible-model"
    assert "开始单次 LLM" in events.getvalue() and "PRIVATE" not in events.getvalue()
    assert logs.safe_fields({"provider": "PRIVATE"}) == {}


@pytest.mark.parametrize("role,label", [
    ("api", "API"), ("worker", "Worker"), ("dense-cli", "Dense CLI"),
    ("bm25-cli", "BM25 CLI"), ("hybrid-cli", "Hybrid CLI"),
    ("rerank-cli", "Reranker CLI"), ("retrieval-acceptance", "检索验收"),
    ("rag-cli", "RAG CLI"),
    ("evidence-cli", "Evidence CLI"),
    ("formula-cli", "Formula CLI"),
    ("formula-binding-cli", "Formula Binding CLI"),
    ("formula-calculation-cli", "Formula Calculation CLI"),
    ("knowledge-agent-cli", "Knowledge Agent CLI"),
    ("preset-cli", "Preset CLI"),
])
def test_registered_process_roles_keep_exact_labels_and_real_pid(role, label):
    """API/Worker及CLI角色显示固定名称，格式/真实PID不变，不将CLI冒充服务进程。"""
    record = logging.LogRecord("dkf.business", logging.INFO, "", 0, "rerank.finished", (), None)
    formatter = logs.ReadableFormatter(role)
    line = formatter.format(record)
    assert logs.ROLE_LABELS[role] == label and line.split(" | ")[2] == label
    parsed = read_log_line(line)
    assert parsed["role"] == role and parsed["pid"] == record.process == os.getpid()
    assert parsed["event"] == "rerank.finished" and parsed["level"] == "INFO"
    assert len(line.split(" | ")) == 6 and formatter.role == role


@pytest.mark.parametrize("role", [None, 1, [], {}, object(), "unregistered-cli", "API",
    "api\nSECRET_ROLE", "worker | pid=1", "postgresql://SECRET_ROLE"])
def test_unknown_process_role_is_a_safe_fixed_fallback(role):
    """未注册/非法类型角色只显示固定未知来源，不能注入换行、字段、敏感文本或伪造PID。"""
    record = logging.LogRecord("dkf.business", logging.INFO, "", 0, "task.started", (), None)
    line = logs.ReadableFormatter(role).format(record)
    assert line.split(" | ")[2] == "未知进程" and len(line.split(" | ")) == 6
    assert read_log_line(line)["role"] == "unknown" and read_log_line(line)["pid"] == os.getpid()
    assert "SECRET_ROLE" not in line and "unregistered-cli" not in line and "\n" not in line


def test_roles_reconfigure_same_daily_file_and_match_console(events, tmp_path, capsys):
    """所有角色的终端/文件一致、同日仍一份日志，重配不变更操作系统PID或进程名称。"""
    settings = Settings(_env_file=None, log_dir=tmp_path)
    process_name = multiprocessing.current_process().name
    for role in logs.ROLE_LABELS:
        logs.configure_logging(settings, role=role)
        logs.log_event("task.started")
    console = capsys.readouterr().err.splitlines()
    [path] = tmp_path.glob("*.log")
    disk = path.read_text(encoding="utf-8").splitlines()
    assert console == disk and len(disk) == len(logs.ROLE_LABELS)
    entries = [read_log_line(line) for line in disk]
    assert [entry["role"] for entry in entries] == list(logs.ROLE_LABELS)
    assert {entry["pid"] for entry in entries} == {os.getpid()}
    assert multiprocessing.current_process().name == process_name
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize("role", [name for name in logs.ROLE_LABELS if name not in {"api", "worker", "unknown"}])
def test_cli_roles_do_not_weaken_payload_redaction_or_identity_protection(events, role):
    """CLI同样只输出白名单摘要，Query/正文/连接串/密钥/私有路径及伪造角色/PID均不透传。"""
    logs.LOGGER.handlers[0].setFormatter(logs.ReadableFormatter(role))
    logs.log_event("rerank.finished", item_count=5, role="worker", pid=1,
                   query="PRIVATE_QUERY", text="PRIVATE_BODY", database_url="PRIVATE_DATABASE",
                   token="PRIVATE_TOKEN", model_dir="PRIVATE_MODEL_PATH")
    [entry] = records(events)
    assert entry["role"] == role and entry["pid"] == os.getpid() and entry["item_count"] == 5
    assert "PRIVATE" not in events.getvalue() and " | Worker | " not in events.getvalue()


def test_application_logging_entry_roles_are_registered():
    """只读取应用/脚本源码审计固定角色，无模块导入/业务执行；新角色须先注册才可通过。"""
    assert len(set(logs.ROLE_LABELS.values())) == len(logs.ROLE_LABELS)
    for directory in (logs.PROJECT_ROOT / "app", logs.PROJECT_ROOT / "scripts"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)
                if name != "configure_logging":
                    continue
                role = next((keyword.value for keyword in call.keywords if keyword.arg == "role"), None)
                if role is None and len(call.args) >= 2:
                    role = call.args[1]
                if isinstance(role, ast.Constant):
                    assert isinstance(role.value, str) and role.value in logs.ROLE_LABELS, path.name


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


def test_daily_merge_reconfiguration_and_no_empty_files(events, tmp_path):
    """同日 API/Worker 和重配共用一个日志，未记录事件时不创建空文件。"""

    settings = Settings(_env_file=None, log_dir=tmp_path)
    logs.configure_logging(settings, "api")
    logs.configure_logging(settings, "api")
    assert len(logs.LOGGER.handlers) == 2
    assert not list(tmp_path.iterdir())
    for _ in range(3):
        logs.log_event("upload.stored", file_name="长" * 255)
    logs.configure_logging(settings, "worker")
    logs.log_event("task.started")
    [path] = tmp_path.glob("*.log")
    assert (
        path.name == f"knowledge-service-{datetime.now(UTC).astimezone():%Y-%m-%d}.log"
    )
    entries = [
        read_log_line(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["role"] for entry in entries] == ["api", "api", "api", "worker"]
    assert {entry["pid"] for entry in entries} == {os.getpid()}
    assert len(list(tmp_path.iterdir())) == 2  # 一份每日日志和固定的跨进程锁文件。
    assert not list(tmp_path.glob("*.log.*"))


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
    """每日写盘失败不会抛出异常或暴露路径，终端仍有日志且警告不反复刷屏。"""

    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "api")
    file_handler = logs.LOGGER.handlers[1]

    def unavailable(record, payload):
        """模拟磁盘满的每日追加错误。"""

        raise OSError("SECRET_PATH")

    monkeypatch.setattr(file_handler, "append_line", unavailable)
    logs.log_event("upload.stored")
    logs.log_event("upload.stored")
    output = capsys.readouterr().err
    assert output.count("业务日志写盘失败") == 1
    assert output.count("event=upload.stored") == 2
    assert "SECRET_PATH" not in output


def test_bad_observer_cannot_fail_business(events, monkeypatch):
    """即使第三方改动 Logger 后抛出异常，日志调用也不能影响业务流程。"""

    def broken_logger(*args, **kwargs):
        """模拟意外的处理器内部异常。"""

        raise RuntimeError("SECRET")

    monkeypatch.setattr(logs.LOGGER, "log", broken_logger)
    logs.log_event("parse.persisted", block_count=3)


def test_daily_boundary_and_delayed_record_keep_original_date(tmp_path):
    """以本机事件日期选文件，跨月/跨年不重命名，延迟记录不会覆盖当天文件。"""

    handler = logs.SafeDailyFileHandler(tmp_path)
    handler.setFormatter(logs.ReadableFormatter("worker"))
    local_zone = datetime.now(UTC).astimezone().tzinfo
    dates = [
        datetime(2025, 12, 31, 23, 59, tzinfo=local_zone),
        datetime(2026, 1, 1, tzinfo=local_zone),
        datetime(2025, 12, 31, tzinfo=local_zone),
    ]
    try:
        for date in dates:
            record = logging.LogRecord(
                "dkf.business", logging.INFO, "", 0, "task.started", (), None
            )
            record.created = date.timestamp()
            handler.handle(record)
    finally:
        handler.close()
    assert {path.name for path in tmp_path.glob("*.log")} == {
        "knowledge-service-2025-12-31.log",
        "knowledge-service-2026-01-01.log",
    }
    assert (
        len(
            (tmp_path / "knowledge-service-2025-12-31.log")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 2
    )
    assert (
        len(
            (tmp_path / "knowledge-service-2026-01-01.log")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 1
    )


def write_process_events(directory, role, index, count, barrier):
    """独立子进程同时初始化并写入真实锁保护日志，测试不访问数据库或启动 Worker。"""

    logs.configure_logging(Settings(_env_file=None, log_dir=Path(directory)), role)
    # 仅压力测试静默终端，生产配置仍同时保留文件与终端处理器。
    logs.LOGGER.removeHandler(logs.LOGGER.handlers[0])
    barrier.wait(timeout=15)
    for sequence in range(count):
        logs.log_event("task.finished", file_name=f"{index}-{sequence}-" + "中文" * 80)


@pytest.mark.parametrize(
    "start_method", ["spawn"] + (["fork"] if os.name != "nt" else [])
)
def test_independent_processes_merge_complete_text_lines(tmp_path, start_method):
    """多 API/Worker 进程竞争同一文件，中文摘要行完整、唯一且保留来源标识。"""

    context = multiprocessing.get_context(start_method)
    barrier = context.Barrier(4)
    processes = [
        context.Process(
            target=write_process_events,
            args=(str(tmp_path), "api" if index == 0 else "worker", index, 80, barrier),
        )
        for index in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.pid:
                process.close()
    [path] = tmp_path.glob("*.log")
    entries = [
        read_log_line(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 320
    assert len({entry["file_name"] for entry in entries}) == 320
    assert len({entry["pid"] for entry in entries}) == 4
    assert {entry["role"] for entry in entries} == {"api", "worker"}
    assert len(list(tmp_path.iterdir())) == 2


def test_concurrent_threads_keep_complete_lines(events, tmp_path):
    """Windows 线程池并发记录仍逐行串行，中文、多线程来源和单文件数量正确。"""

    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "worker")

    def write_event(index):
        """每个线程写入不同摘要，以检测重复、拼接或丢行。"""

        logs.log_event("task.finished", file_name=f"并发-{index}.txt")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_event, range(80)))
    [path] = tmp_path.glob("*.log")
    entries = [
        read_log_line(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == len({entry["file_name"] for entry in entries}) == 80


def test_lock_timeout_and_permissions_fail_safely(
    events, tmp_path, monkeypatch, capsys
):
    """锁竞争有等待上限，非竞争错误立即降级，二者均不泄露原始异常或路径。"""

    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "api")
    original_lock = logs.lock_file

    def busy(descriptor, **kwargs):
        """模拟被其他进程长时间占用的 OS 锁。"""

        raise BlockingIOError(logs.errno.EAGAIN, "SECRET_PATH")

    monkeypatch.setattr(logs, "LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(logs, "lock_file", busy)
    logs.log_event("upload.stored")
    assert not list(tmp_path.glob("*.log"))
    assert "SECRET_PATH" not in capsys.readouterr().err
    monkeypatch.setattr(logs, "lock_file", original_lock)
    logs.log_event("upload.stored")
    assert len(list(tmp_path.glob("*.log"))) == 1

    attempts = 0

    def invalid_lock(descriptor, **kwargs):
        """模拟不可重试的系统锁错误，必须立即降级而不是一直等待。"""

        nonlocal attempts
        attempts += 1
        raise OSError(logs.errno.EINVAL, "SECRET_PATH")

    monkeypatch.setattr(logs, "lock_file", invalid_lock)
    monkeypatch.setattr(logs, "LOCK_TIMEOUT_SECONDS", 1)
    logs.log_event("upload.stored")
    assert attempts == 1
    assert "SECRET_PATH" not in capsys.readouterr().err


def crash_with_lock(directory):
    """模拟写入者进程异常退出，不执行 finally，以验证 OS 会回收文件锁。"""

    with logs.shared_file_lock(Path(directory)):
        os._exit(0)


def test_crashed_process_does_not_leave_stale_lock(events, tmp_path):
    """锁持有者退出后下一进程可继续写，不需要删除锁文件或 PID 标记。"""

    process = multiprocessing.get_context("spawn").Process(
        target=crash_with_lock, args=(str(tmp_path),)
    )
    try:
        process.start()
        process.join(timeout=20)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "api")
    logs.log_event("http.request", status_code=200)
    [path] = tmp_path.glob("*.log")
    assert read_log_line(path.read_text(encoding="utf-8").strip())["status_code"] == 200


def test_role_cannot_be_forged_and_retired_configuration_is_ignored():
    """角色只能来自配置；旧容量/备份参数不恢复拆文件或阻碍配置兼容加载。"""

    settings = Settings(_env_file=None, log_max_bytes=1, log_backup_count=999)
    assert "log_max_bytes" not in type(settings).model_fields
    assert "log_backup_count" not in type(settings).model_fields
    record = logging.LogRecord(
        "dkf.business", logging.INFO, "", 0, "task.started", (), None
    )
    record.business_fields = {"role": "SECRET", "pid": 1}
    entry = read_log_line(logs.ReadableFormatter("worker").format(record))
    assert entry["role"] == "worker"
    assert entry["pid"] == os.getpid()


def test_readable_chinese_summary_and_deterministic_field_order(events):
    """日志直接可读且不再是 JSON 对象；中文字段按固定顺序，不随传参顺序变化。"""

    doc_id = uuid4()
    logs.log_event(
        "parse.persisted",
        duration_ms=12.5,
        block_count=3,
        file_name="中文验收.txt",
        doc_id=doc_id,
        state="CHUNKING",
    )
    [line] = events.getvalue().splitlines()
    assert not line.startswith("{")
    assert " | INFO | 未知进程 | 原生解析结果已落库 | event=parse.persisted | " in line
    assert '文件="中文验收.txt"' in line
    assert '状态="CHUNKING" | Block数=3 | 耗时_ms=12.5' in line
    assert line.index("doc_id=") < line.index("文件=") < line.index("状态=")
    assert set(logs.FIELD_LABELS) == logs.FIELDS
    assert records(events)[0]["doc_id"] == str(doc_id)
    record = logging.LogRecord(
        "dkf.business", logging.INFO, "", 0, "parse.persisted", (), None
    )
    record.business_fields = {"duration_ms": 12.5, "file_name": "中文验收.txt"}
    formatter = logs.ReadableFormatter("worker")
    forward = formatter.format(record)
    record.business_fields = dict(reversed(list(record.business_fields.items())))
    assert formatter.format(record) == forward


def test_local_timestamp_has_milliseconds_and_explicit_offset():
    """时间使用与每日文件一致的本机时区，保留毫秒及 UTC 偏移用于跨进程对照。"""

    record = logging.LogRecord(
        "dkf.business", logging.INFO, "", 0, "http.request", (), None
    )
    record.created = datetime(2026, 10, 11, 4, 5, 6, 123000, tzinfo=UTC).timestamp()
    line = logs.ReadableFormatter("api").format(record)
    timestamp = read_log_line(line)["timestamp"]
    assert timestamp == (
        datetime.fromtimestamp(record.created, UTC)
        .astimezone()
        .isoformat(sep=" ", timespec="milliseconds")
    )
    assert datetime.fromisoformat(timestamp).microsecond == 123000
    assert datetime.fromisoformat(timestamp).utcoffset() is not None


def test_delimiter_quotes_and_controls_cannot_forge_fields(events):
    """文件名中的竖线、引号、反斜杠和换行不能伪造事件、进程身份或额外日志行。"""

    filename = '文档 | event=upload.failed | pid=1 "引号" \\正文\r\n.txt'
    logs.log_event("upload.stored", file_name=filename)
    [record] = records(events)
    assert record["event"] == "upload.stored"
    assert record["pid"] == os.getpid()
    assert record["file_name"] == filename.replace("\r", "").replace("\n", "")
    assert len(events.getvalue().splitlines()) == 1
    assert events.getvalue().count(" | event=") == 1


@pytest.mark.parametrize("message", ["SECRET=%s", "SECRET\n", object()])
def test_invalid_event_and_exception_details_are_not_formatted(message):
    """不使用 getMessage 或异常堆栈，非法事件及参数不能泄露原始敏感内容。"""

    record = logging.LogRecord(
        "dkf.business", logging.ERROR, "SECRET_PATH", 0, message, ("SECRET",), None
    )
    record.exc_text = "SECRET_TRACE"
    line = logs.ReadableFormatter("api").format(record)
    assert "SECRET" not in line
    assert " | 业务事件 | event=unknown | " in line


def test_nonfinite_numbers_are_omitted_without_losing_event(events):
    """NaN 和无限数不是有效业务摘要，应舍弃字段但保留该次日志事件。"""

    logs.log_event("parse.retry", duration_ms=float("nan"), delay_seconds=float("inf"))
    [record] = records(events)
    assert record["event"] == "parse.retry"
    assert "duration_ms" not in record and "delay_seconds" not in record


def test_existing_json_history_is_preserved_in_same_daily_file(events, tmp_path):
    """切换格式只追加新摘要，不覆盖已有 JSON 历史，也不新建格式专属文件。"""

    path = tmp_path / f"knowledge-service-{datetime.now(UTC).astimezone():%Y-%m-%d}.log"
    history = '{"event": "historical.record"}\n'
    path.write_text(history, encoding="utf-8")
    logs.configure_logging(Settings(_env_file=None, log_dir=tmp_path), "api")
    logs.log_event("http.request", status_code=200)
    first, second = path.read_text(encoding="utf-8").splitlines()
    assert first + "\n" == history
    assert read_log_line(second)["status_code"] == 200
    assert len(list(tmp_path.glob("*.log"))) == 1


def test_chunk_summary_is_readable_and_does_not_include_evidence_text(events):
    """切片日志仅包含白名单计数/参数，不记录正文、字符证据或原件路径。"""

    logs.log_event("chunk.persisted", doc_id=uuid4(), task_id=uuid4(), chunk_count=3,
                   chunk_size=600, chunk_overlap=80, oversized_count=1,
                   text="SECRET_BODY", source_spans=[{"text": "SECRET_BODY"}], file_path="SECRET_PATH")
    [record] = records(events)
    assert record["chunk_count"] == 3 and record["oversized_count"] == 1
    assert "切片已事务落库" in events.getvalue()
    assert "SECRET" not in events.getvalue()


def test_chunk_task_context_uses_document_id_and_restores_context(events):
    """切片用独立 task_id 关联原 doc_id，不误用云 job_id，执行完恢复上层上下文。"""

    doc_id, task_id = str(uuid4()), str(uuid4())
    task = SimpleNamespace(name="app.tasks.chunks.chunk_document", request=SimpleNamespace(headers={}))
    task_logs.begin_task_log(task=task, task_id=task_id, args=[doc_id])
    logs.log_event("chunk.persisted", chunk_count=1)
    task_logs.end_task_log(task=task, state="SUCCESS", retval={"text": "SECRET"})
    assert logs.CONTEXT.get() is None
    assert all(record["doc_id"] == doc_id and record["task_id"] == task_id for record in records(events))
    assert "SECRET" not in events.getvalue()


def test_embedding_summary_and_task_context_do_not_log_vectors_or_text(events):
    """GPU 日志复用每日中文摘要，关联 doc_id/task_id，不输出正文/向量/模型目录。"""

    doc_id, task_id = str(uuid4()), str(uuid4())
    task = SimpleNamespace(name="app.tasks.embeddings.embed_document", request=SimpleNamespace(headers={}))
    task_logs.begin_task_log(task=task, task_id=task_id, args=[doc_id])
    logs.log_event("embedding.persisted", chunk_count=5, dimension=1024, artifact_reused=True,
                   text="SECRET_BODY", vector=[1.0], model_dir="SECRET_PATH")
    task_logs.end_task_log(task=task, state="SUCCESS", retval={"vector": [1.0], "text": "SECRET"})
    assert logs.CONTEXT.get() is None
    assert all(record["doc_id"] == doc_id and record["task_id"] == task_id for record in records(events))
    result = next(record for record in records(events) if record["event"] == "embedding.persisted")
    assert result["dimension"] == 1024 and result["chunk_count"] == 5
    assert "SECRET" not in events.getvalue() and "向量结果与文档状态已提交" in events.getvalue()


def test_index_logs_restore_context_and_only_include_safe_summary(events):
    """入库日志关联文档/任务并保持中文摘要，不泄漏向量、正文、模型指纹或私有路径。"""

    doc_id, task_id = str(uuid4()), str(uuid4())
    task = SimpleNamespace(name="app.tasks.indexing.index_document", request=SimpleNamespace(headers={}))
    task_logs.begin_task_log(task=task, task_id=task_id, args=[doc_id])
    logs.log_event("index.persisted", state="READY", chunk_count=5, dimension=1024,
                   vector=[1.0], text="SECRET_BODY", output_dir="SECRET_PATH", model_fingerprint="SECRET_HASH")
    task_logs.end_task_log(task=task, state="SUCCESS", retval={"vector": [1.0]})
    assert logs.CONTEXT.get() is None
    assert all(record["doc_id"] == doc_id and record["task_id"] == task_id for record in records(events))
    result = next(record for record in records(events) if record["event"] == "index.persisted")
    assert result["state"] == "READY" and result["chunk_count"] == 5 and result["dimension"] == 1024
    assert "SECRET" not in events.getvalue() and "READY 状态已事务提交" in events.getvalue()


def test_rag_adapter_events_are_chinese_and_do_not_log_evidence_or_vectors(events):
    """Adapter 日志只含关联 UUID/数量/耗时等摘要，不输出 Node/正文/向量或模型路径。"""

    chunk_id = uuid4()
    logs.log_event("rag.node_converted", chunk_id=chunk_id, doc_id=uuid4(), kb_id=uuid4(),
                   node="PRIVATE_NODE", metadata={"text": "PRIVATE_BODY"}, vector=[1.0], file_path="PRIVATE_PATH")
    logs.log_event("rag.query_encoded", dimension=1024, text="PRIVATE_QUERY", embedding=[1.0])
    logs.log_event("rag.adapter_failed", stage="node_to_result", error_code="RAG_ADAPTER_IDENTITY")
    result = records(events)
    assert result[0]["chunk_id"] == str(chunk_id) and result[1]["dimension"] == 1024
    assert "PRIVATE" not in events.getvalue() and "已有 Chunk 已转换为运行时 Node" in events.getvalue()


def test_preset_import_logs_are_chinese_safe_summaries(events):
    """预置 CLI/Worker 复用中文事件与关联标识，不记录清单、正文、向量或私有路径。"""

    doc_id = uuid4()
    logs.log_event("preset.imported", doc_id=doc_id, kb_id=uuid4(), status="UPLOADED",
                   manifest="PRIVATE_MANIFEST", directory="PRIVATE_PATH", text="PRIVATE_BODY")
    logs.log_event("preset.finished", file_count=12, accepted_count=11, failed_count=1, status="INCOMPLETE")
    result = records(events)
    assert result[0]["doc_id"] == str(doc_id) and result[1]["failed_count"] == 1
    assert "PRIVATE" not in events.getvalue() and "预置原件已提交" in events.getvalue()
