"""切片检查点、事务持久化和提交后投递；不触发重新解析或云调用。"""

import logging
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob
from app.services.chunk.splitter import MAX_BLOCKS, MAX_SOURCE_CHARS, ChunkError, ChunkOptions, SourceBlock, build_chunks
from app.services.document_processing import transition_document


def source_generation(document: Document) -> str | None:
    """用解析代次识别 Block 生命周期，保留历史文档的空代次，不假造标识。"""

    return str(document.processing_task_id) if document.processing_task_id else None


def checkpoint_options(document: Document) -> ChunkOptions:
    """读取冻结配置，拒绝被改变的来源代次和不可识别的参数，不静默使用新默认值。"""

    config = document.chunk_config
    if (not isinstance(config, dict) or set(config) != {"options", "source_parse_task_id"}
            or config["source_parse_task_id"] != source_generation(document)
            or not isinstance(config["options"], dict)):
        raise ChunkError("CHUNK_CHECKPOINT_INVALID：切片检查点与解析代次不一致")
    try:
        options = ChunkOptions(**config["options"])
    except TypeError as exc:
        raise ChunkError("CHUNK_CONFIG_INVALID：切片参数不可用") from exc
    options.validate()
    return options


def prepare_chunk(session: Session, document: Document, settings: Settings) -> tuple[UUID, bool]:
    """在文档行锁下创建或恢复检查点；返回任务编号及是否需要投递，不提交调用方事务。"""

    active = session.scalar(select(DocumentParseJob.job_id).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))
    if active is not None:
        raise ChunkError("CHUNK_BUSY：文档仍有活动解析任务")
    if document.status == "EMBEDDING" and document.chunk_task_id is not None:
        checkpoint_options(document)
        return document.chunk_task_id, False
    if document.status not in {"CHUNKING", "FAILED"} or (
            document.status == "FAILED" and document.chunk_task_id is None):
        raise ChunkError("CHUNK_STATE：文档尚未完成解析或不允许切片")
    if document.chunk_task_id is not None:
        checkpoint_options(document)
    else:
        options = ChunkOptions(settings.chunk_size, settings.chunk_overlap)
        document.chunk_task_id = uuid4()
        document.chunk_config = {"options": options.snapshot(), "source_parse_task_id": source_generation(document)}
    # FAILED 只允许恢复有检查点的切片任务，不把解析失败误认为已有完整输入。
    document.status, document.error_message = "CHUNKING", None
    session.flush()
    return document.chunk_task_id, True


def run_chunk_step(session: Session, doc_id: UUID, task_id: UUID) -> dict:
    """以文档行锁保护全量替换；切片和 EMBEDDING 状态同事务，失败由调用方整体回滚。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    result = {"doc_id": str(doc_id), "task_id": str(task_id)}
    if document is None:
        return result | {"status": "not_found"}
    result["document_status"] = document.status
    if document.chunk_task_id != task_id:
        return result | {"status": "stale_task"}
    if document.status != "CHUNKING":
        return result | {"status": "skipped"}
    options = checkpoint_options(document)
    total_chars, block_count = session.execute(select(
        func.coalesce(func.sum(func.length(DocumentBlock.text)), 0), func.count())
        .select_from(DocumentBlock).where(DocumentBlock.doc_id == doc_id)).one()
    if total_chars > MAX_SOURCE_CHARS or block_count > MAX_BLOCKS:
        raise ChunkError("CHUNK_LIMIT：切片源文本超过本地资源上限")
    blocks = list(session.scalars(select(DocumentBlock).where(DocumentBlock.doc_id == doc_id)
                                 .order_by(DocumentBlock.block_index.asc().nulls_last())))
    log_event("chunk.started", doc_id=doc_id, kb_id=document.kb_id, task_id=task_id,
              block_count=len(blocks), chunk_size=options.size, chunk_overlap=options.overlap)
    inputs = [SourceBlock(block.block_id, block.block_index, block.page, block.section,
                          block.block_type, block.text, block.source, block.bbox) for block in blocks]
    drafts = build_chunks(doc_id, document.kb_id, document.file_type, inputs, options)
    session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == doc_id))
    for draft in drafts:
        metadata = draft.metadata | {"source_parse_task_id": source_generation(document),
                                     "file_hash": document.file_hash}
        session.add(DocumentChunk(chunk_id=draft.chunk_id, doc_id=doc_id, kb_id=document.kb_id,
            text=draft.text, page_start=draft.page_start, page_end=draft.page_end,
            section=draft.section, chunk_metadata=metadata, embedding=None))
    transition_document(document, "EMBEDDING")
    session.flush()
    return result | {"status": "awaiting_embedding", "document_status": document.status,
                     "chunk_count": len(drafts), "block_count": len(blocks),
                     "oversized_count": sum(draft.metadata["oversized"] for draft in drafts)}


def record_chunk_failure(session: Session, doc_id: UUID, task_id: UUID, message: str, final: bool) -> None:
    """失败回滚后独立记录安全原因；旧任务、已完成切片或新解析代次不能被覆盖。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    if (document is None or document.chunk_task_id != task_id or document.status != "CHUNKING"
            or not isinstance(document.chunk_config, dict)
            or document.chunk_config.get("source_parse_task_id") != source_generation(document)):
        return
    if final:
        transition_document(document, "FAILED", message)
    else:
        document.error_message = message


def publish_chunk(session: Session, doc_id: UUID, task_id: UUID) -> bool:
    """检查点提交后投递同一任务；确认丢失时只标记仍在等待的代次，不覆盖已完成结果。"""

    from app.tasks.chunks import chunk_document

    try:
        chunk_document.apply_async(args=[str(doc_id)], task_id=str(task_id), queue="default_queue")
    except Exception:
        session.rollback()
        with session.begin():
            record_chunk_failure(session, doc_id, task_id, "CHUNK_DISPATCH：切片任务投递确认失败，请恢复同一任务", True)
        log_event("chunk.dispatch_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="CHUNK_DISPATCH")
        return False
    log_event("chunk.dispatched", doc_id=doc_id, task_id=task_id, queue="default_queue")
    return True
