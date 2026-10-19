"""管理员显式请求的短编排任务：每次只推进一阶段，等待用countdown而非阻塞循环。"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.models import Document
from app.services.document_cleanup import cleanup_owned_files
from app.services.document_management import ManagementError, config_for, dispatch_action, exception_cause, exception_stage, mark_failed, next_action
from app.tasks.documents import DocumentProcessingError, processing_session


def save_management_failure(doc_id: UUID, task_id: UUID, code: str, cause_code=None, *, stage=None) -> None:
    """回滚后独立保存安全原因和准备阶段，不覆盖新代次，也不伪造业务任务失败/投递。"""
    try:
        with processing_session() as session, session.begin():
            mark_failed(session, doc_id, task_id, code, cause_code, stage=stage)
    except Exception:
        log_event("management.error_record_failed", level=logging.ERROR,
                  doc_id=doc_id, task_id=task_id, error_code=code)


@celery_app.task(bind=True, name="app.tasks.management.manage_document", max_retries=17280,
                 acks_late=True, reject_on_worker_lost=True)
def manage_document(self, doc_id: str) -> dict:
    """短编排保留真实失败阶段/原因，解析或云拒收明确失败；不执行解析、模型或扩大重试。"""
    try:
        identifier, task_id = UUID(doc_id), UUID(self.request.id)
    except (ValueError, TypeError, AttributeError):
        raise DocumentProcessingError("管理文档或任务标识必须为合法UUID") from None
    try:
        with processing_session() as session:
            with session.begin():
                document = session.scalar(select(Document).where(Document.doc_id == identifier).with_for_update(nowait=True)
                                          .execution_options(populate_existing=True))
                if document is None:
                    return {"doc_id": doc_id, "status": "not_found"}
                if document.management_task_id != task_id:
                    return {"doc_id": doc_id, "status": "stale_task"}
                config = config_for(document)
                action = next_action(session, document, get_settings())
                if action["kind"] == "delete":
                    summary = cleanup_owned_files(session, document, config["cleanup_roots"])
                    # FK CASCADE 同事务移除Blocks/Chunks/向量/Parse Job，不删除全局HNSW索引。
                    session.delete(document)
                    action = {"kind": "deleted", **summary}
            if action["kind"] == "dispatch" and not dispatch_action(session, identifier, action):
                raise ManagementError("MANAGEMENT_DISPATCH")
        if action["kind"] in {"done", "deleted", "stopped"}:
            log_event("management.deleted" if action["kind"] == "deleted" else "management.finished",
                      doc_id=identifier, task_id=task_id, status=action["kind"])
            return {"doc_id": doc_id, "status": action["kind"],
                    **{key: value for key, value in action.items() if key.endswith(("_count", "_retained"))}}
        if action["kind"] == "dispatch":
            log_event("management.advanced", doc_id=identifier, task_id=task_id, stage=action["stage"])
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            # GPU/Parser持锁时结束本次短检查，不占CPU线程等待整份推理/解析。
            with processing_session() as snapshot:
                current = snapshot.scalar(select(Document).where(Document.doc_id == identifier))
                if current is None or current.management_task_id != task_id:
                    return {"doc_id": doc_id, "status": "stale_task"}
                frozen = config_for(current)
                if datetime.now(UTC) >= datetime.fromisoformat(frozen["expires_at"]):
                    # 不能为写失败标记再等待同一GPU锁；/status由截止时间显示失败，显式Retry恢复。
                    log_event("management.failed", level=logging.ERROR, doc_id=identifier,
                              task_id=task_id, error_code="MANAGEMENT_TIMEOUT")
                    raise DocumentProcessingError(str(ManagementError("MANAGEMENT_TIMEOUT"))) from None
            raise self.retry(countdown=5, throw=False) from None
        save_management_failure(identifier, task_id, "MANAGEMENT_CHECKPOINT")
        raise DocumentProcessingError(str(ManagementError("MANAGEMENT_CHECKPOINT"))) from None
    except Exception as exc:
        # 编排不主动重试未知投递失败/费用；文件局部清理失败也保留DB删除检查点供用户恢复。
        cause = exception_cause(exc)
        stage = exception_stage(exc)
        code = exc.code if isinstance(exc, ManagementError) else (
            "MANAGEMENT_STAGE_FAILED" if cause is not None else "MANAGEMENT_CHECKPOINT")
        save_management_failure(identifier, task_id, code, cause, stage=stage)
        log_event("management.failed", level=logging.ERROR, doc_id=identifier, task_id=task_id,
                  error_code=code, error_type=type(exc).__name__, stage=stage, step=cause)
        message = str(ManagementError(code))
        if code == "MANAGEMENT_STAGE_FAILED" and stage in {"parse", "cloud"}:
            message = f"{code}：文件解析失败"
        if stage is not None:
            message += f"；阶段={stage}"
        if cause is not None:
            message += f"；原因={cause}"
        raise DocumentProcessingError(message) from None
    # 仅用户启动的管理任务查询本地DB状态，有持久化截止时间；不轮询MinerU HTTP或占用Worker睡眠。
    raise self.retry(countdown=5, throw=False) from None
