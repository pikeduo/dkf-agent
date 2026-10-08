"""文档处理状态机与占位步骤，不伪造解析、切片或索引结果。"""

from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT
from app.models import Document

TRANSITIONS = {
    "UPLOADED": {"PARSING", "FAILED"},
    "PARSING": {"OCR_PROCESSING", "CHUNKING", "FAILED"},
    "OCR_PROCESSING": {"CHUNKING", "FAILED"},
    "CHUNKING": {"EMBEDDING", "FAILED"},
    "EMBEDDING": {"INDEXING", "FAILED"},
    "INDEXING": {"READY", "FAILED"},
    "READY": set(),
    "FAILED": {"UPLOADED"},
}


class DocumentSourceError(Exception):
    """原件缺失或为空等不可通过自动重试修复的业务错误。"""


def transition_document(
    document: Document, target: str, error: str | None = None
) -> None:
    """验证合法状态流转，失败必须有原因，正常推进则清除上次错误。"""

    if target not in TRANSITIONS or (
        target != document.status and target not in TRANSITIONS[document.status]
    ):
        raise ValueError("文档状态流转不合法")
    if target == "FAILED" and not error:
        raise ValueError("FAILED 状态必须记录失败原因")
    document.status = target
    document.error_message = error


def check_source(document: Document) -> None:
    """仅检查原件存在且至少可读取一个字节，不解析正文，也不加载任何模型。"""

    path = Path(document.file_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        with path.open("rb") as source:
            if not source.read(1):
                raise DocumentSourceError("文档原件为空，请检查上传文件")
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise DocumentSourceError("文档原件不存在或不是文件，请检查存储目录") from exc


def run_processing_step(
    session: Session, doc_id: UUID, task_id: UUID
) -> dict[str, str]:
    """持有文档行锁执行当前占位步骤，重复及过期消息不生成数据或覆盖新任务状态。"""

    document = session.scalar(
        select(Document).where(Document.doc_id == doc_id).with_for_update()
    )
    result = {"doc_id": str(doc_id)}
    if document is None:
        return result | {"status": "not_found"}
    result["document_status"] = document.status
    if document.processing_task_id != task_id:
        return result | {"status": "stale_task"}
    if document.status not in {"UPLOADED", "PARSING"}:
        return result | {"status": "skipped"}
    check_source(document)
    # 后续 Parser 在这里接入；当前不能把没有解析结果的文档标记为 CHUNKING 或 READY。
    transition_document(document, "PARSING")
    return result | {"status": "awaiting_parser", "document_status": document.status}


def record_failure(
    session: Session, doc_id: UUID, task_id: UUID, message: str, final: bool
) -> None:
    """给当前任务写入安全的失败原因，旧任务及已完成文档不被失败回调回退。"""

    document = session.scalar(
        select(Document).where(Document.doc_id == doc_id).with_for_update()
    )
    if (
        document is None
        or document.processing_task_id != task_id
        or document.status not in {"UPLOADED", "PARSING"}
    ):
        return
    if final:
        transition_document(document, "FAILED", message)
    else:
        # 暂时性失败保留可恢复的阶段，由同一 Celery 任务标识继续重试。
        document.error_message = message
