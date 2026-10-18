"""单文档 CPU 向量入库任务；不解析文档、不导入推理框架或调用云端。"""

import logging
from time import perf_counter
from uuid import UUID

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.services.indexing import IndexingError, record_index_failure, run_index_step
from app.tasks.documents import DocumentProcessingError, processing_session


def save_index_failure(doc_id: UUID, task_id: UUID, code: str, final: bool) -> None:
    """独立会话记录安全失败码；数据库不可用时只降级日志，不泄漏 SQL 原文。"""

    try:
        with processing_session() as session, session.begin():
            record_index_failure(session, doc_id, task_id, code, final)
    except (SQLAlchemyError, RuntimeError, ValueError):
        log_event("index.error_record_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="INDEX_DATABASE")


@celery_app.task(bind=True, name="app.tasks.indexing.index_document", max_retries=3,
                 acks_late=True, reject_on_worker_lost=True)
def index_document(self, doc_id: str) -> dict:
    """批量写入与 READY 同事务，数据库临时失败仅重试 5/10/20 秒，完整性失败不自动重算。"""

    try:
        identifier, task_id = UUID(doc_id), UUID(self.request.id)
    except (ValueError, TypeError, AttributeError):
        raise DocumentProcessingError("入库文档或任务标识必须为合法 UUID") from None
    started = perf_counter()
    try:
        with processing_session() as session, session.begin():
            result = run_index_step(session, identifier, task_id)
    except OperationalError:
        final = self.request.retries >= self.max_retries
        code = "INDEX_DATABASE_EXHAUSTED" if final else "INDEX_DATABASE"
        save_index_failure(identifier, task_id, code, final)
        log_event("index.failed" if final else "index.retry", level=logging.ERROR if final else logging.WARNING,
                  doc_id=identifier, task_id=task_id, error_code=code, retry_count=self.request.retries)
        if final:
            raise DocumentProcessingError(str(IndexingError(code))) from None
        raise self.retry(exc=DocumentProcessingError(str(IndexingError(code))),
                         countdown=5 * 2 ** self.request.retries, throw=False) from None
    except Exception as exc:
        code = exc.code if isinstance(exc, IndexingError) else "INDEX_FAILED"
        save_index_failure(identifier, task_id, code, True)
        log_event("index.failed", level=logging.ERROR, doc_id=identifier, task_id=task_id, error_code=code)
        raise DocumentProcessingError(str(IndexingError(code))) from None
    # 成功只在整个 SQL 事务提交后记录，批次执行日志不能充当落库成功。
    log_event("index.persisted" if result["status"] in {"ready", "partial_ready"} else "index.skipped",
              doc_id=identifier, task_id=task_id, state=result.get("document_status"), status=result["status"],
              chunk_count=result.get("chunk_count"), dimension=result.get("dimension"),
              duration_ms=(perf_counter() - started) * 1000)
    return result
