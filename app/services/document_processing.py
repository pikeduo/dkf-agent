"""原生文档解析、事务性 Block 落库与状态机，不执行 OCR、切片或索引。"""

from pathlib import Path
import hashlib
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.models import Document, DocumentBlock, DocumentChunk
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


def check_source_hash(document: Document, path=None) -> None:
    """读取前后核对原件 Hash，防止解析期间改变的文件被发布到错误缓存身份。"""

    with (source_path(document) if path is None else path).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
            raise ParserError("文档原件 Hash 已变化，请核对存储内容")


def persist_blocks(
    session: Session, document: Document, parsed: ParsedDocument
) -> None:
    """替换解析块并失效切片/向量检查点；失败时全部回滚，不删除本地历史向量文件。"""

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
    session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == document.doc_id))
    document.chunk_task_id, document.chunk_config = None, None
    document.embedding_task_id, document.embedding_config = None, None
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
    session: Session, doc_id: UUID, task_id: UUID, *, settings: Settings | None = None,
    force_reparse: bool = False, cache_only: bool = False,
) -> dict[str, str | int]:
    """显式 Native 使用范围独立缓存；Reindex 仅重放，强制 Reparse 才忽略已有缓存。"""

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
    from app.services.parse_cache import CacheError, ParseCache, recipe

    settings = settings or get_settings()
    check_source_hash(document)
    cache = ParseCache(settings)
    transition_document(document, "PARSING")
    identity = recipe(document, settings, "native", scope="explicit_native_scope")
    cached = cache.replay(document, settings) if cache_only else None if force_reparse else cache.document(document, identity)
    if cache_only and cached is None:
        raise CacheError("CACHE_MISS")
    if cached is not None:
        check_source_hash(document)
        persist_blocks(session, document, cached)
        transition_document(document, "CHUNKING")
        return result | {"status": "awaiting_chunk", "document_status": document.status,
                         "block_count": len(cached.blocks), "parse_cache_hit": True,
                         "mineru_file_submit_count": 0}
    parser = get_parser(document.file_type)
    log_event(
        "parse.started",
        doc_id=doc_id,
        kb_id=document.kb_id,
        task_id=task_id,
        file_name=document.file_name,
        file_type=document.file_type,
        parser=type(parser).__name__,
    )
    parsed = parser.parse(
        doc_id=document.doc_id,
        file_path=source_path(document),
        file_name=document.file_name,
        file_type=document.file_type,
    )
    log_event(
        "parse.validated",
        doc_id=doc_id,
        task_id=task_id,
        block_count=len(parsed.blocks),
    )
    check_source_hash(document)
    cache.save_document(document, identity, parsed)
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
