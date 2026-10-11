"""安全的业务事件日志：白名单摘要、请求关联和每进程独立轮转，不接管第三方日志。"""

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from time import perf_counter
from uuid import UUID, uuid4

from app.core.config import PROJECT_ROOT, Settings

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
}
LOCAL_IDS = {"request_id", "task_id", "doc_id", "kb_id", "job_id"}
# FastAPI 自带文档路由不总是设置 scope.route，只接受这些固定公开路径作为回退。
PUBLIC_ROUTES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


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
                **safe_fields(getattr(record, "business_fields", {})),
            },
            ensure_ascii=False,
            allow_nan=False,
        )


class SafeRotatingHandler(RotatingFileHandler):
    """磁盘失败时只提示安全信息，不抛错、不输出原始异常与文件路径。"""

    warned = False

    def handleError(self, record: logging.LogRecord) -> None:
        """第一次写盘失败提示检查权限或磁盘；终端处理器继续记录业务事件。"""

        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write(
                    "业务日志写盘失败，请检查日志目录权限与磁盘空间；终端日志继续输出。\n"
                )
            except OSError:
                return


def configure_logging(settings: Settings, role: str) -> None:
    """配置独立业务 Logger；重复调用替换处理器，prefork 子进程重新建立独立文件。"""

    LOGGER.setLevel(settings.log_level)
    LOGGER.disabled = False
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = JsonFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    directory = settings.log_dir
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingHandler(
            directory / f"{role}-{os.getpid()}.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
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
