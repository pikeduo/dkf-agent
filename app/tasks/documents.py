"""文档异步入口：受控重试、幂等行锁和持久化失败原因。"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from uuid import UUID

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.db.session import get_engine
from app.parsers.common import ParserError
from app.services.document_processing import (
    DocumentSourceError,
    record_failure,
    run_processing_step,
)
from app.services.mineru.errors import MinerUError

logger = logging.getLogger(__name__)


class DocumentProcessingError(Exception):
    """写入任务结果的安全错误，不携带连接串、原始 SQL 或本地文件路径。"""


@contextmanager
def processing_session() -> Iterator[Session]:
    """每次执行或错误落库创建独立会话，禁止 Worker 多线程共享 Session。"""

    with Session(get_engine()) as session:
        yield session


def save_failure(doc_id: UUID, task_id: UUID, message: str, final: bool) -> None:
    """在失败步骤回滚后以新事务记录原因，数据库不可用时保留安全日志提示。"""

    try:
        with processing_session() as session, session.begin():
            record_failure(session, doc_id, task_id, message, final)
    except (SQLAlchemyError, RuntimeError, ValueError):
        logger.error("文档错误未能写入数据库：doc_id=%s，原因=%s", doc_id, message)
        log_event(
            "parse.error_record_failed",
            level=logging.ERROR,
            doc_id=doc_id,
            task_id=task_id,
            error_code="DATABASE",
        )


@celery_app.task(
    bind=True,
    name="app.tasks.documents.process_document",
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_document(self, doc_id: str, parse_mode: str | None = "auto") -> dict:
    """默认自动路由；拒绝终态标记任务失败，云检查点提交后才投递短任务。"""

    try:
        identifier = UUID(doc_id)
        task_id = UUID(self.request.id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DocumentProcessingError("文档或任务标识必须为合法 UUID") from exc
    started = perf_counter()
    try:
        with processing_session() as session, session.begin():
            if parse_mode == "native":
                # 管理员的显式 Native 是范围对照，保留前九阶段 Parser/事务/重试实现。
                result = run_processing_step(session, identifier, task_id)
                if result["status"] == "awaiting_chunk":
                    result["coverage"] = "explicit_native_scope"
            else:
                from app.services.routed_processing import run_routed_step

                result = run_routed_step(
                    session, identifier, task_id, parse_mode or "auto", get_settings()
                )
        if result["status"] == "failed":
            # 决策拒绝不是成功解析：数据库终态已提交，Celery 同样返回安全 FAILURE。
            raise ParserError(result["error_message"])
        if result["status"] == "delegated_to_mineru":
            # 延迟导入避免短任务对 processing_session 的复用形成循环导入。
            from app.services.routed_processing import record_dispatch_error
            from app.tasks.mineru import publish_step

            try:
                publish_step(UUID(result["job_id"]))
            except MinerUError:
                with processing_session() as session, session.begin():
                    record_dispatch_error(session, UUID(result["job_id"]))
                result["status"] = "cloud_dispatch_failed"
        # 事务上下文退出完成提交后才报告成功；验证通过不等同于落库完成。
        log_event(
            "parse.persisted"
            if result["status"] == "awaiting_chunk"
            else "router.finished"
            if result["status"] in {"delegated_to_mineru", "cloud_dispatch_failed"}
            else "parse.skipped",
            doc_id=identifier,
            task_id=task_id,
            status=result["status"],
            state=result.get("document_status"),
            block_count=result.get("block_count"),
            mineru_file_submit_count=result.get("mineru_file_submit_count"),
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )
        if result["status"] == "awaiting_chunk" and "hybrid_bundle_page_count" in result:
            log_event("hybrid.persisted", doc_id=identifier, task_id=task_id,
                      block_count=result.get("block_count"), status="CHUNKING",
                      **{key: value for key, value in result.items() if key.startswith("hybrid_")})
        return result
    except (DocumentSourceError, ParserError, MinerUError) as exc:
        log_event(
            "parse.failed",
            level=logging.ERROR,
            doc_id=identifier,
            task_id=task_id,
            error_code=getattr(exc, "code", type(exc).__name__),
        )
        message = f"{exc.code}：{exc}" if isinstance(exc, MinerUError) else str(exc)
        save_failure(identifier, task_id, message, final=True)
        raise DocumentProcessingError(message) from None
    except (OperationalError, OSError):
        final = self.request.retries >= self.max_retries
        log_event(
            "parse.failed" if final else "parse.retry",
            level=logging.ERROR if final else logging.WARNING,
            doc_id=identifier,
            task_id=task_id,
            error_code="DATABASE_OR_FILE_IO",
            retry_count=self.request.retries,
            delay_seconds=None if final else min(5 * 2**self.request.retries, 60),
        )
        message = (
            "文档处理重试已耗尽：数据库或文件访问暂时不可用"
            if final
            else f"数据库或文件访问暂时不可用，等待第 {self.request.retries + 1} 次重试"
        )
        save_failure(identifier, task_id, message, final)
        if final:
            raise DocumentProcessingError(message) from None
        # 先取得 Retry 再显式抛出，隔离原始数据库 / I/O 异常链，避免敏感信息进入结果。
        raise self.retry(
            exc=DocumentProcessingError(message),
            countdown=min(5 * 2**self.request.retries, 60),
            throw=False,
        ) from None
    # 任务边界必须兜底记录未知错误，但只暴露异常类型，避免原始信息泄漏凭据。
    except Exception as exc:  # noqa: BLE001
        log_event(
            "parse.failed",
            level=logging.ERROR,
            doc_id=identifier,
            task_id=task_id,
            error_code="UNKNOWN",
            error_type=type(exc).__name__,
        )
        message = f"文档处理框架执行失败（{type(exc).__name__}），请检查配置与 Worker"
        save_failure(identifier, task_id, message, final=True)
        raise DocumentProcessingError(message) from None
