"""原生文档解析、事务性 Block 落库与状态机，不执行 OCR、切片或索引。"""

from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT
from app.models import Document, DocumentBlock
from app.parsers.common import NoTextError, ParserError
from app.parsers.registry import get_parser
from app.schemas.parsed_document import ParsedDocument

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


def source_path(document: Document) -> Path:
    """将数据库中的原件路径解析为项目根目录下或指定绝对路径，不改变文件。"""

    path = Path(document.file_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def check_source(document: Document) -> None:
    """在解析前检查原件存在且非空，缺失文件作为安全永久失败处理。"""

    try:
        with source_path(document).open("rb") as source:
            if not source.read(1):
                raise DocumentSourceError("文档原件为空，请检查上传文件")
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise DocumentSourceError("文档原件不存在或不是文件，请检查存储目录") from exc


def persist_blocks(
    session: Session, document: Document, parsed: ParsedDocument
) -> None:
    """在调用方事务中整体替换该文档解析块；失败时旧块与状态随事务回滚。"""

    parsed = ParsedDocument.model_validate(parsed)
    if (parsed.doc_id, parsed.file_name, parsed.file_type) != (
        document.doc_id,
        document.file_name,
        document.file_type,
    ):
        raise ParserError("解析结果与原始文档元信息不一致")
    if not parsed.blocks or not any(block.text.strip() for block in parsed.blocks):
        raise NoTextError("文档没有可用原生文本")
    # 先完成全部解析与校验再替换，禁止半份 PDF 或格式错误清掉旧解析结果。
    session.execute(
        delete(DocumentBlock).where(DocumentBlock.doc_id == document.doc_id)
    )
    session.add_all(
        [
            DocumentBlock(
                block_id=block.block_id,
                doc_id=document.doc_id,
                block_index=index,
                page=block.page,
                section=block.section,
                block_type=block.block_type,
                text=block.text,
                bbox=block.bbox,
                source=block.source,
                confidence=block.confidence,
            )
            for index, block in enumerate(parsed.blocks, start=1)
        ]
    )
    session.flush()


def run_processing_step(
    session: Session, doc_id: UUID, task_id: UUID
) -> dict[str, str | int]:
    """持有行锁校验代次、解析原件并写块，同事务推进到等待切片，重复任务跳过。"""

    document = session.scalar(
        select(Document).where(Document.doc_id == doc_id).with_for_update()
    )
    result: dict[str, str | int] = {"doc_id": str(doc_id)}
    if document is None:
        return result | {"status": "not_found"}
    result["document_status"] = document.status
    if document.processing_task_id != task_id:
        return result | {"status": "stale_task"}
    if document.status not in {"UPLOADED", "PARSING"}:
        return result | {"status": "skipped"}
    check_source(document)
    transition_document(document, "PARSING")
    parsed = get_parser(document.file_type).parse(
        doc_id=document.doc_id,
        file_path=source_path(document),
        file_name=document.file_name,
        file_type=document.file_type,
    )
    persist_blocks(session, document, parsed)
    # CHUNKING 仅表示等待下一阶段；本阶段不产生 Chunk、Embedding 或 READY。
    transition_document(document, "CHUNKING")
    return result | {
        "status": "awaiting_chunk",
        "document_status": document.status,
        "block_count": len(parsed.blocks),
    }


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
