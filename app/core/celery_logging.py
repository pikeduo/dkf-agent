"""Celery 业务日志上下文；线程隔离，父/子进程通过文件锁合并每日业务日志。"""

import logging
from time import perf_counter

from celery import signals

from app.core.business_logging import CONTEXT, configure_logging, log_event
from app.core.config import get_settings


@signals.after_setup_logger.connect
@signals.worker_process_init.connect
def setup_worker_logging(**kwargs) -> None:
    """Worker 与 prefork 子进程启动时重建处理器，以 worker 身份写每日合并日志。"""

    configure_logging(get_settings(), "worker")


@signals.before_task_publish.connect
def propagate_request_id(headers=None, **kwargs) -> None:
    """向任务头传递请求编号，任务参数保持不变，不传递 Token 或文档内容。"""

    if headers is not None and (CONTEXT.get() or {}).get("request_id"):
        headers["dkf_request_id"] = CONTEXT.get()["request_id"]


@signals.task_prerun.connect
def begin_task_log(
    task_id=None, task=None, args=None, kwargs=None, **signal_kwargs
) -> None:
    """为解析/切片/向量化/入库设置文档关联；嵌套 eager 结束后恢复上层上下文。"""

    if task is None:
        return
    request = task.request
    headers = request.headers or {}
    fields = {
        "task_id": task_id,
        "request_id": headers.get("dkf_request_id"),
        "task_name": task.name,
    }
    identity_key = (
        "doc_id"
        if task.name in {"app.tasks.documents.process_document", "app.tasks.chunks.chunk_document",
                         "app.tasks.embeddings.embed_document", "app.tasks.indexing.index_document"}
        else "job_id"
        if task.name
        in {
            "app.tasks.mineru.submit_mineru_parse",
            "app.tasks.mineru.check_mineru_result",
        }
        else None
    )
    if identity_key:
        fields[identity_key] = (kwargs or {}).get(identity_key) or (
            args[0] if args else None
        )
    request._business_log_token = CONTEXT.set(fields)
    request._business_log_started = perf_counter()
    log_event("task.started")


@signals.task_postrun.connect
def end_task_log(task=None, state=None, **kwargs) -> None:
    """记录 Celery 调用状态和耗时，不序列化任务返回值或异常，最后清除任务上下文。"""

    if task is None:
        return
    request = task.request
    token = getattr(request, "_business_log_token", None)
    if token is None:
        return
    try:
        log_event(
            "task.finished",
            level=logging.ERROR if state == "FAILURE" else logging.INFO,
            state=state,
            duration_ms=round(
                (perf_counter() - request._business_log_started) * 1000, 2
            ),
        )
    finally:
        CONTEXT.reset(token)
