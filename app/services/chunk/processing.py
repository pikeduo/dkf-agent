"""切片检查点、事务持久化和提交后投递；不触发重新解析或云调用。"""

import logging
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob
from app.services.chunk.splitter import MAX_BLOCKS, STREAM_VERSIONS, ChunkError, ChunkOptions, SourceBlock, build_chunks, source_char_limit
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
    """行锁下创建/恢复切片检查点；切片或向量已完成只返回原编号，不重建 GPU 输入。"""

    active = session.scalar(select(DocumentParseJob.job_id).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))
    if active is not None:
        raise ChunkError("CHUNK_BUSY：文档仍有活动解析任务")
    if document.status in {"EMBEDDING", "INDEXING", "READY", "PARTIAL_READY"} and document.chunk_task_id is not None:
        checkpoint_options(document)
        return document.chunk_task_id, False
    if document.status not in {"CHUNKING", "FAILED"} or (
            document.status == "FAILED" and document.chunk_task_id is None):
        raise ChunkError("CHUNK_STATE：文档尚未完成解析或不允许切片")
    if document.embedding_task_id is not None:
        raise ChunkError("CHUNK_STATE：已有向量化检查点，请恢复 /embed 或显式 Reindex 后再切片")
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
    """v6/v7逐块读取/每256片写入，同一事务完成；更旧版本保留冻结预算和算法。"""

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
    limit = source_char_limit(options.version)
    if limit is not None and (total_chars > limit or block_count > MAX_BLOCKS):
        chars_exceeded = total_chars > source_char_limit(options.version)
        log_event("chunk.failed", doc_id=doc_id, task_id=task_id, stage="chunk.source_budget",
                  step="SOURCE_CHAR_LIMIT" if chars_exceeded else "BLOCK_COUNT_LIMIT",
                  block_count=block_count, item_count=total_chars if chars_exceeded else block_count,
                  limit_count=source_char_limit(options.version) if chars_exceeded else MAX_BLOCKS,
                  error_code="CHUNK_LIMIT")
        raise ChunkError("CHUNK_LIMIT：切片源文本超过本地资源上限")
    if options.version in STREAM_VERSIONS:
        return result | stream_chunk_step(session, document, options, block_count, total_chars)
    blocks = list(session.scalars(select(DocumentBlock).where(DocumentBlock.doc_id == doc_id)
                                 .order_by(DocumentBlock.block_index.asc().nulls_last())))
    log_event("chunk.started", doc_id=doc_id, kb_id=document.kb_id, task_id=task_id,
              block_count=len(blocks), chunk_size=options.size, chunk_overlap=options.overlap,
              step=options.version)
    inputs = [SourceBlock(block.block_id, block.block_index, block.page, block.section,
                          block.block_type, block.text, block.source, block.bbox,
                          getattr(block, "provenance", None)) for block in blocks]
    drafts = build_chunks(doc_id, document.kb_id, document.file_type, inputs, options)
    session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == doc_id))
    document.embedding_task_id, document.embedding_config = None, None
    document.index_task_id, document.index_config = None, None
    for draft in drafts:
        metadata = draft.metadata | {"source_parse_task_id": source_generation(document),
                                     "file_hash": document.file_hash}
        if getattr(document, "parse_quality", None):
            metadata["parse_quality"] = document.parse_quality
        session.add(DocumentChunk(chunk_id=draft.chunk_id, doc_id=doc_id, kb_id=document.kb_id,
            text=draft.text, page_start=draft.page_start, page_end=draft.page_end,
            section=draft.section, chunk_metadata=metadata, embedding=None))
    transition_document(document, "EMBEDDING")
    session.flush()
    return result | {"status": "awaiting_embedding", "document_status": document.status,
                     "chunk_count": len(drafts), "block_count": len(blocks),
                     "oversized_count": sum(draft.metadata["oversized"] for draft in drafts)}


def stream_chunk_step(session, document, options, block_count, total_chars):
    """新版本不收集全部Block/Chunk；任何后批失败由外层事务回滚，来源及质量不变。"""
    from app.services.chunk.streaming import iter_chunks

    def factory():
        """在文档行锁内可重复顺序读取原块，每次仅预取少量ORM行。"""
        query = select(DocumentBlock).where(DocumentBlock.doc_id == document.doc_id).order_by(
            DocumentBlock.block_index.asc().nulls_last()).execution_options(yield_per=1)
        for block in session.scalars(query):
            yield SourceBlock(block.block_id, block.block_index, block.page, block.section,
                block.block_type, block.text, block.source, block.bbox, getattr(block, "provenance", None))

    log_event("chunk.started", doc_id=document.doc_id, kb_id=document.kb_id, task_id=document.chunk_task_id,
        block_count=block_count, item_count=total_chars, chunk_size=options.size,
        chunk_overlap=options.overlap, step=options.version)
    session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == document.doc_id))
    document.embedding_task_id = document.embedding_config = None
    document.index_task_id = document.index_config = None
    batch, count, oversized, batch_index = [], 0, 0, 0
    for draft in iter_chunks(document.doc_id, document.kb_id, document.file_type, factory, options):
        metadata = draft.metadata | {"source_parse_task_id": source_generation(document), "file_hash": document.file_hash}
        if getattr(document, "parse_quality", None):
            metadata["parse_quality"] = document.parse_quality
        batch.append({"chunk_id": draft.chunk_id, "doc_id": document.doc_id, "kb_id": document.kb_id,
            "text": draft.text, "page_start": draft.page_start, "page_end": draft.page_end,
            "section": draft.section, "chunk_metadata": metadata, "embedding": None})
        count += 1
        oversized += int(draft.metadata["oversized"])
        if len(batch) == 256:
            session.execute(insert(DocumentChunk), batch)
            batch, batch_index = [], batch_index + 1
            # 此事件仅表示批次已暂存，不冒充事务提交或业务成功。
            log_event("chunk.batch_staged", doc_id=document.doc_id, task_id=document.chunk_task_id,
                      batch_index=batch_index, item_count=256)
    if batch:
        session.execute(insert(DocumentChunk), batch)
    transition_document(document, "EMBEDDING")
    session.flush()
    return {"status": "awaiting_embedding", "document_status": document.status,
            "chunk_count": count, "block_count": block_count, "oversized_count": oversized}


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
