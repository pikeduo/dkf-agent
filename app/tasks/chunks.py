"""本地 CPU 切片任务，受控数据库重试，不解析原件、不调用任何模型。"""

import logging
from time import perf_counter
from uuid import UUID

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.services.chunk.processing import record_chunk_failure, run_chunk_step
from app.services.chunk.splitter import ChunkError
from app.tasks.documents import DocumentProcessingError, processing_session


def save_chunk_failure(doc_id: UUID, task_id: UUID, message: str, final: bool) -> None:
    """使用独立会话保存固定错误；错误落库失败只写脱敏摘要，不输出 SQL 或正文。"""

    try:
        with processing_session() as session, session.begin():
            record_chunk_failure(session, doc_id, task_id, message, final)
    except (SQLAlchemyError, RuntimeError, ValueError):
        log_event("chunk.error_record_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="CHUNK_DATABASE")


@celery_app.task(bind=True, name="app.tasks.chunks.chunk_document", max_retries=3,
                 acks_late=True, reject_on_worker_lost=True)
def chunk_document(self, doc_id: str) -> dict:
    """按冻结参数事务生成 Chunk；仅临时数据库错误退避 5/10/20 秒，永久失败不重试。"""

    try:
        identifier, task_id = UUID(doc_id), UUID(self.request.id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DocumentProcessingError("切片文档或任务标识必须为合法 UUID") from exc
    started = perf_counter()
    try:
        with processing_session() as session, session.begin():
            result = run_chunk_step(session, identifier, task_id)
    except OperationalError:
        final = self.request.retries >= self.max_retries
        message = "CHUNK_DATABASE：切片数据库暂时不可用" if not final else "CHUNK_DATABASE：切片数据库重试已耗尽"
        save_chunk_failure(identifier, task_id, message, final)
        log_event("chunk.failed" if final else "chunk.retry", level=logging.ERROR if final else logging.WARNING,
                  doc_id=identifier, task_id=task_id, error_code="CHUNK_DATABASE",
                  retry_count=self.request.retries)
        if final:
            raise DocumentProcessingError(message) from None
        raise self.retry(exc=DocumentProcessingError(message), countdown=5 * 2 ** self.request.retries,
                         throw=False) from None
    except Exception as exc:
        message = str(exc) if isinstance(exc, ChunkError) else "CHUNK_FAILED：切片未完成，原有结果已保留"
        save_chunk_failure(identifier, task_id, message, True)
        log_event("chunk.failed", level=logging.ERROR, doc_id=identifier, task_id=task_id,
                  error_code=message.split("：", 1)[0])
        raise DocumentProcessingError(message) from None
    # 只能在事务提交后记录落库成功；跳过旧消息不是生成成功。
    log_event("chunk.persisted" if result["status"] == "awaiting_embedding" else "chunk.skipped",
              doc_id=identifier, task_id=task_id, status=result["status"],
              state=result.get("document_status"), chunk_count=result.get("chunk_count"),
              block_count=result.get("block_count"), oversized_count=result.get("oversized_count"),
              duration_ms=(perf_counter() - started) * 1000)
    return result
