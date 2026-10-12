"""每文档一个 GPU 任务，内部批处理全部 Chunk，不建立每 Chunk 一个子任务。"""

import logging
from time import perf_counter
from uuid import UUID
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.services.embedding.processing import record_embedding_failure, run_embedding_step
from app.services.embedding.service import EmbeddingError
from app.tasks.documents import DocumentProcessingError, processing_session


def save_embedding_failure(doc_id: UUID, task_id: UUID, message: str, final: bool) -> None:
    """用独立会话保存固定错误，不记录模型异常、文档正文或存储路径。"""

    try:
        with processing_session() as session, session.begin():
            record_embedding_failure(session, doc_id, task_id, message, final)
    except (SQLAlchemyError, RuntimeError, ValueError):
        log_event("embedding.error_record_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="EMBEDDING_DATABASE")


@celery_app.task(bind=True, name="app.tasks.embeddings.embed_document", max_retries=3,
                 acks_late=True, reject_on_worker_lost=True)
def embed_document(self, doc_id: str) -> dict:
    """GPU Worker 批量生成本地结果；仅临时数据库错误重试 5/10/20 秒，模型错误不自动反复重算。"""

    try:
        identifier, task_id = UUID(doc_id), UUID(self.request.id)
    except (TypeError, ValueError, AttributeError):
        raise DocumentProcessingError("Embedding 文档或任务标识必须为合法 UUID") from None
    started = perf_counter()
    try:
        with processing_session() as session, session.begin():
            result = run_embedding_step(session, identifier, task_id)
    except OperationalError:
        final = self.request.retries >= self.max_retries
        message = "EMBEDDING_DATABASE：数据库重试已耗尽" if final else "EMBEDDING_DATABASE：数据库暂时不可用"
        save_embedding_failure(identifier, task_id, message, final)
        log_event("embedding.failed" if final else "embedding.retry",
                  level=logging.ERROR if final else logging.WARNING, doc_id=identifier, task_id=task_id,
                  error_code="EMBEDDING_DATABASE", retry_count=self.request.retries)
        if final:
            raise DocumentProcessingError(message) from None
        raise self.retry(exc=DocumentProcessingError(message), countdown=5 * 2 ** self.request.retries,
                         throw=False) from None
    except Exception as exc:
        message = str(exc) if isinstance(exc, EmbeddingError) else "EMBEDDING_FAILED：向量化未完成，原有 Chunk 已保留"
        save_embedding_failure(identifier, task_id, message, True)
        log_event("embedding.failed", level=logging.ERROR, doc_id=identifier, task_id=task_id,
                  error_code=message.split("：", 1)[0])
        raise DocumentProcessingError(message) from None
    log_event("embedding.persisted" if result["status"] == "awaiting_index" else "embedding.skipped",
              doc_id=identifier, task_id=task_id, state=result.get("document_status"), status=result["status"],
              chunk_count=result.get("chunk_count"), dimension=result.get("dimension"),
              artifact_reused=result.get("artifact_reused"), duration_ms=(perf_counter() - started) * 1000)
    return result
