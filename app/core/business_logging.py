"""安全的业务事件日志：白名单摘要、请求关联和跨进程每日合并，不接管第三方日志。"""

import errno
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep
from uuid import UUID, uuid4

from app.core.config import PROJECT_ROOT, Settings

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOGGER = logging.getLogger("dkf.business")
CONTEXT: ContextVar[dict | None] = ContextVar("business_log_context", default=None)
FIELDS = {
    "request_id",
    "task_id",
    "doc_id",
    "kb_id",
    "job_id",
    "batch_id",
    "file_name",
    "file_type",
    "size_bytes",
    "parser",
    "stage",
    "state",
    "status",
    "status_code",
    "method",
    "route",
    "duration_ms",
    "block_count",
    "retry_count",
    "delay_seconds",
    "error_code",
    "error_type",
    "queue",
    "task_name",
    "step",
    "upload_complete",
    "file_count",
    "file_index",
    "accepted_count",
    "failed_count",
}
LOCAL_IDS = {"request_id", "task_id", "doc_id", "kb_id", "job_id"}
# FastAPI 自带文档路由不总是设置 scope.route，只接受这些固定公开路径作为回退。
PUBLIC_ROUTES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
FILE_PREFIX = "knowledge-service"
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_RETRY_SECONDS = 0.01


def lock_file(descriptor: int, *, unlock: bool = False) -> None:
    """在固定锁文件上加锁/解锁；Windows 锁首字节，Unix 使用非阻塞独占 flock。"""

    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(
            descriptor, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB
        )


@contextmanager
def shared_file_lock(directory: Path):
    """串行化完整日志行写入；每次独立打开锁描述符，避免 prefork 继承同一锁身份。"""

    # 锁文件不能运行中删除/替换，否则新旧进程可能锁住不同文件而失去互斥。
    # Windows 允许锁住 EOF 之后的字节，无需向锁文件写入任何业务内容。
    descriptor = os.open(
        directory / f".{FILE_PREFIX}.lock", os.O_CREAT | os.O_RDWR, 0o600
    )
    acquired = False
    try:
        deadline = perf_counter() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock_file(descriptor)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if perf_counter() >= deadline:
                    raise TimeoutError("日志写入锁等待超时") from None
                sleep(LOCK_RETRY_SECONDS)
        yield
    finally:
        try:
            if acquired:
                lock_file(descriptor, unlock=True)
        finally:
            # 包括异常退出在内，描述符关闭释放 OS 锁，不遗留 PID 占用标记。
            os.close(descriptor)


def safe_fields(fields: dict) -> dict:
    """只保留已批准的标量；本地标识必须为 UUID，不序列化任意对象或敏感字段。"""

    result = {}
    for key, value in fields.items():
        if key not in FIELDS or value is None:
            continue
        if key in LOCAL_IDS:
            try:
                value = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
        elif isinstance(value, UUID):
            value = str(value)
        if isinstance(value, str):
            # JSON 单行编码加上控制字符清理，防止文件名伪造新的日志行。
            value = "".join(c for c in value if c.isprintable())[:256]
            if key == "batch_id" and not re.fullmatch(r"[\w.:-]{1,128}", value):
                continue
        elif not isinstance(value, (int, float, bool)):
            continue
        result[key] = value
    return result


def log_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    """记录白名单业务摘要；日志系统异常不得改变上传、事务或任务的业务结果。"""

    try:
        LOGGER.log(
            level,
            event,
            extra={"business_fields": safe_fields((CONTEXT.get() or {}) | fields)},
        )
    except Exception:  # noqa: BLE001
        # 可观测性不是事务的一部分；不得因为磁盘满而回退已成功的文档处理。
        return


class JsonFormatter(logging.Formatter):
    """仅格式化业务事件，不输出异常堆栈、任意响应、连接串或文档正文。"""

    def __init__(self, role: str = "unknown") -> None:
        """固定进程来源字段，不允许请求或业务字段伪造 API/Worker 身份。"""

        super().__init__()
        self.role = role if role in {"api", "worker"} else "unknown"

    def format(self, record: logging.LogRecord) -> str:
        """生成 UTF-8 单行 JSON，时间统一为 UTC，便于跨进程按标识关联。"""

        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "event": record.msg
                if isinstance(record.msg, str)
                and re.fullmatch(r"[a-z][a-z0-9_.]+", record.msg)
                else "unknown",
                "pid": record.process,
                "role": self.role,
                **safe_fields(getattr(record, "business_fields", {})),
            },
            ensure_ascii=False,
            allow_nan=False,
        )


class SafeDailyFileHandler(logging.Handler):
    """所有进程按本机日期追加同一文件，不按 PID/容量拆分，不删除历史日志。"""

    def __init__(self, directory: Path) -> None:
        """保存日志目录；不提前创建空日志，不保持跨日或跨 fork 的数据文件句柄。"""

        super().__init__()
        self.directory = directory
        self.warned = False

    def append_line(self, record: logging.LogRecord, payload: bytes) -> None:
        """在 OS 锁内完整写入 UTF-8 行；按事件产生的本机日期选文件，不跨日重命名。"""

        date = (
            datetime.fromtimestamp(record.created, UTC)
            .astimezone()
            .strftime("%Y-%m-%d")
        )
        path = self.directory / f"{FILE_PREFIX}-{date}.log"
        with shared_file_lock(self.directory), path.open("ab", buffering=0) as stream:
            pending = memoryview(payload)
            while pending:
                written = stream.write(pending)
                if not written:
                    raise OSError("日志行未完整写入")
                pending = pending[written:]

    def emit(self, record: logging.LogRecord) -> None:
        """追加单行 JSON；锁超时、磁盘或格式错误降级到终端，不影响业务结果。"""

        try:
            self.append_line(record, (self.format(record) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """首次写盘或锁失败安全提示，不输出原始异常；终端仍记录业务事件。"""

        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write(
                    "业务日志写盘失败，请检查目录权限、磁盘空间或写入锁占用；终端日志继续输出。\n"
                )
            except OSError:
                return


def configure_logging(settings: Settings, role: str) -> None:
    """配置业务 Logger；API/Worker 合并每日文件，重复配置或 prefork 不增加文件数量。"""

    LOGGER.setLevel(settings.log_level)
    LOGGER.disabled = False
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = JsonFormatter(role)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    directory = settings.log_dir
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = SafeDailyFileHandler(directory)
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    except OSError:
        log_event(
            "logging.file_unavailable", level=logging.WARNING, error_code="LOG_FILE_IO"
        )


class RequestLoggingMiddleware:
    """纯 ASGI 中间件，不读取请求或响应正文，覆盖完成、校验失败和未处理异常。"""

    def __init__(self, app) -> None:
        """保存下游应用；不改变流式传输、上传文件或数据库依赖生命周期。"""

        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        """为每次 HTTP 请求生成可信编号，返回 X-Request-ID 并记录完整响应发送耗时。"""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        token = CONTEXT.set({"request_id": request_id})
        start, status, error_type = perf_counter(), 500, None

        async def send_response(message):
            """只增加响应关联头并捕获状态码，不缓冲、解析或记录响应正文。"""

            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            # 使用匹配路由模板而非用户传入的路径或查询串，避免 URL 中的密钥进入文件。
            route = getattr(scope.get("route"), "path", None)
            if route is None:
                path = scope.get("path")
                route = path if path in PUBLIC_ROUTES else "<unmatched>"
            log_event(
                "http.request",
                level=logging.ERROR
                if error_type or status >= 500
                else logging.WARNING
                if status >= 400
                else logging.INFO,
                method=scope["method"],
                route=route,
                status_code=status,
                duration_ms=round((perf_counter() - start) * 1000, 2),
                error_type=error_type,
                **{
                    key: value
                    for key, value in scope.get("path_params", {}).items()
                    if key in LOCAL_IDS
                },
            )
            CONTEXT.reset(token)
