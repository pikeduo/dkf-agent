"""安全的业务事件日志：白名单摘要、请求关联和跨进程每日合并，不接管第三方日志。"""

import errno
import json
import logging
import math
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
    "decision_version",
    "parse_mode",
    "route_strategy",
    "reason_codes",
    "fallback_count",
    "mineru_file_submit_count",
    "page_count",
    "sheet_count",
    "image_count",
    "chart_count",
    "source_document_count",
    "hybrid_visual_candidate_count",
    "hybrid_visual_filtered_count",
    "hybrid_visual_dedup_count",
    "hybrid_bundle_page_count",
    "hybrid_chart_count",
    "cache_key",
    "parse_cache_hit",
    "visual_cache_hit_count",
    "operation",
}
LOCAL_IDS = {"request_id", "task_id", "doc_id", "kb_id", "job_id"}
# FastAPI 自带文档路由不总是设置 scope.route，只接受这些固定公开路径作为回退。
PUBLIC_ROUTES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
FILE_PREFIX = "knowledge-service"
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_RETRY_SECONDS = 0.01
# 中文说明便于直接阅读；保留事件码便于跨 API/Worker 搜索和定位。
EVENT_LABELS = {
    "http.request": "接口请求结束",
    "logging.file_unavailable": "日志目录不可用，已降级到终端",
    "upload.received": "收到文档上传",
    "upload.rejected": "文档上传校验未通过",
    "upload.duplicate": "检测到重复文档",
    "upload.stored": "文档原件与记录已保存",
    "upload.failed": "文档上传失败",
    "upload.constraint_conflict": "文档上传数据库约束冲突",
    "upload.cleanup_failed": "上传失败后的原件清理失败",
    "upload.batch_rejected": "批量上传数量校验未通过",
    "upload.batch_started": "开始批量上传",
    "upload.batch_item": "批量上传单文件处理结束",
    "upload.batch_finished": "批量上传处理结束",
    "document.dispatched": "文档解析任务已投递",
    "document.dispatch_failed": "文档解析任务投递失败",
    "task.started": "异步任务开始执行",
    "task.finished": "异步任务执行结束",
    "parse.started": "开始原生文档解析",
    "parse.validated": "原生解析结果结构校验通过",
    "parse.persisted": "原生解析结果已落库",
    "parse.skipped": "跳过无需处理的文档任务",
    "parse.retry": "原生解析任务等待重试",
    "parse.failed": "原生解析任务失败",
    "parse.error_record_failed": "原生解析失败原因未能写入数据库",
    "mineru.dispatched": "MinerU 任务已投递",
    "mineru.dispatch_failed": "MinerU 任务投递失败",
    "mineru.step_started": "MinerU 处理步骤开始",
    "mineru.step_finished": "MinerU 处理步骤结束",
    "mineru.http_started": "开始调用 MinerU 接口",
    "mineru.http_response": "收到 MinerU HTTP 响应",
    "mineru.http_failed": "MinerU HTTP 请求失败",
    "mineru.batch_created": "MinerU 批次检查点已保存",
    "mineru.upload_complete": "MinerU 原件上传检查点已保存",
    "mineru.remote_state": "收到 MinerU 远端解析状态",
    "mineru.result_downloaded": "MinerU 结果 ZIP 下载完成",
    "mineru.adapter_validated": "MinerU 解析结果结构校验通过",
    "mineru.blocks_persisted": "MinerU 解析结果已落库",
    "mineru.retry": "MinerU 任务等待重试",
    "mineru.failed": "MinerU 任务失败",
    "mineru.error_record_failed": "MinerU 失败原因未能写入数据库",
    "router.decided": "文档完整性路由决策已生成",
    "router.failed": "文档自动路由安全拒绝，未生成最终结果",
    "router.fallback": "解析预检要求一次提交前整份云回退",
    "router.guard_passed": "解析结构覆盖检查通过",
    "router.guard_failed": "解析覆盖检查未通过",
    "router.finished": "路由任务处理结束",
    "router.cloud_submitted": "路由整份云解析批次已确认",
    "router.cloud_dispatch_failed": "路由云任务投递确认失败",
    "hybrid.prepared": "Hybrid 候选与单个 Bundle 已准备，尚未写入最终 Block",
    "hybrid.started": "开始 XLSX 主解析与视觉候选检查",
    "hybrid.persisted": "Hybrid 主结果与增强结果已完整落库",
    "hybrid.preflight_rejected": "Hybrid 预检拒绝，按固定原因选择提交前回退或失败",
    "xlsx.primary_validated": "XLSX 原生单元格与公式主结果已校验，尚未最终落库",
    "xlsx.primary_persisted": "XLSX 原生主结果与整份云增强已一起落库",
    "cache.hit": "命中完整解析缓存，不调用云端",
    "cache.miss": "解析缓存未命中，尚未提交云任务",
    "cache.stored": "完整校验结果已持久化为解析缓存，尚未代表数据库提交",
    "cache.busy": "相同内容已有活动云任务，禁止重复收费提交",
    "cache.persisted": "解析缓存已事务回放到文档 Block",
    "cache.maintenance_requested": "收到显式缓存回放或强制重新解析请求",
}
# 显式固定字段顺序；关联编号保留原名，其他摘要使用中文标签。
FIELD_LABELS = {
    "request_id": "request_id",
    "doc_id": "doc_id",
    "kb_id": "kb_id",
    "task_id": "task_id",
    "job_id": "job_id",
    "batch_id": "batch_id",
    "file_name": "文件",
    "file_type": "格式",
    "size_bytes": "大小_bytes",
    "method": "方法",
    "route": "接口",
    "status_code": "HTTP状态",
    "parser": "解析器",
    "stage": "阶段",
    "step": "步骤",
    "queue": "队列",
    "task_name": "任务",
    "state": "状态",
    "status": "结果",
    "upload_complete": "上传完成",
    "block_count": "Block数",
    "file_count": "文件数",
    "file_index": "文件序号",
    "accepted_count": "接收数",
    "failed_count": "失败数",
    "retry_count": "重试次数",
    "delay_seconds": "等待秒数",
    "duration_ms": "耗时_ms",
    "error_code": "错误码",
    "error_type": "错误类型",
    "decision_version": "路由版本",
    "parse_mode": "解析模式",
    "route_strategy": "路由策略",
    "reason_codes": "路由理由",
    "fallback_count": "回退次数",
    "mineru_file_submit_count": "云文件提交数",
    "page_count": "物理页数",
    "sheet_count": "Sheet数",
    "image_count": "图片数",
    "chart_count": "图表数",
    "source_document_count": "源文档数",
    "hybrid_visual_candidate_count": "视觉候选数",
    "hybrid_visual_filtered_count": "视觉过滤数",
    "hybrid_visual_dedup_count": "视觉复用数",
    "hybrid_bundle_page_count": "Bundle页数",
    "hybrid_chart_count": "结构化图表数",
    "cache_key": "缓存键",
    "parse_cache_hit": "解析缓存命中",
    "visual_cache_hit_count": "视觉缓存命中数",
    "operation": "操作",
}


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
            # 清除控制字符；格式器再转义字段分隔符，防止伪造新行或新字段。
            value = "".join(c for c in value if c.isprintable())[:256]
            if key == "batch_id" and not re.fullmatch(r"[\w.:-]{1,128}", value):
                continue
        elif (
            not isinstance(value, (int, float, bool))
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
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


class ReadableFormatter(logging.Formatter):
    """仅格式化业务事件，不输出异常堆栈、任意响应、连接串或文档正文。"""

    def __init__(self, role: str = "unknown") -> None:
        """固定进程来源字段，不允许请求或业务字段伪造 API/Worker 身份。"""

        super().__init__()
        self.role = role if role in {"api", "worker"} else "unknown"

    def format(self, record: logging.LogRecord) -> str:
        """生成本机时间的中文单行摘要，保留事件码及关联字段，不输出结果正文。"""

        event = (
            record.msg
            if isinstance(record.msg, str)
            and re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", record.msg)
            else "unknown"
        )
        parts = [
            datetime.fromtimestamp(record.created, UTC)
            .astimezone()
            .isoformat(sep=" ", timespec="milliseconds"),
            logging.getLevelName(record.levelno),
            {"api": "API", "worker": "Worker", "unknown": "未知进程"}[self.role],
            EVENT_LABELS.get(event, "业务事件"),
            f"event={event}",
            f"pid={record.process}",
        ]
        fields = safe_fields(getattr(record, "business_fields", {}))
        for key, label in FIELD_LABELS.items():
            if key in fields:
                # 只借用 JSON 的标量引用/转义，不再输出 JSON 对象；竖线不能伪造分隔符。
                value = json.dumps(fields[key], ensure_ascii=False, allow_nan=False)
                value = value.replace("|", r"\u007c")
                parts.append(f"{label}={value}")
        return " | ".join(parts)


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
        """追加单行中文摘要；锁超时、磁盘或格式错误降级到终端，不影响业务结果。"""

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
    formatter = ReadableFormatter(role)
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
