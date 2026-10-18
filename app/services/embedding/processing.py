"""GPU 文档向量化检查点和批处理，结果本地保存，阶段 15 前不写入 Chunk.embedding。"""

import logging
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings
from app.models import Document, DocumentChunk, DocumentParseJob
from app.services.document_processing import transition_document
from app.services.embedding.artifacts import ArtifactStore, digest_json
from app.services.embedding.service import DIMENSION, MAX_ITEMS, EmbeddingError, EmbeddingOptions, get_embedding_service

# 文档按GPU批次流式编码，聚合预算对齐v4大表；Service每次调用仍保留1000万字符限制。
MAX_DOCUMENT_TOTAL_CHARS = 16_000_000


def read_chunks(session: Session, document: Document) -> list:
    """以文档级预算读取当前Chunk；超限先记安全计数，来源/顺序及单批模型限制仍严格检查。"""

    predicate = (DocumentChunk.doc_id == document.doc_id, DocumentChunk.kb_id == document.kb_id)
    count, chars = session.execute(select(func.count(), func.coalesce(func.sum(func.length(DocumentChunk.text)), 0))
                                  .select_from(DocumentChunk).where(*predicate)).one()
    if count == 0:
        raise EmbeddingError("EMBEDDING_EMPTY")
    if count > MAX_ITEMS or chars > MAX_DOCUMENT_TOTAL_CHARS:
        chars_exceeded = chars > MAX_DOCUMENT_TOTAL_CHARS
        log_event("embedding.input_rejected", level=logging.ERROR, doc_id=document.doc_id,
            stage="embedding.document_budget", step="TOTAL_CHAR_LIMIT" if chars_exceeded else "CHUNK_COUNT_LIMIT",
            chunk_count=count, item_count=chars if chars_exceeded else count,
            limit_count=MAX_DOCUMENT_TOTAL_CHARS if chars_exceeded else MAX_ITEMS, error_code="EMBEDDING_LIMIT")
        raise EmbeddingError("EMBEDDING_LIMIT")
    rows = session.execute(select(DocumentChunk.chunk_id, DocumentChunk.text,
        DocumentChunk.chunk_metadata["chunk_index"].as_integer().label("chunk_index"),
        DocumentChunk.chunk_metadata["source_parse_task_id"].as_string().label("source_parse_task_id"))
        .where(*predicate).order_by(DocumentChunk.chunk_metadata["chunk_index"].as_integer().asc().nulls_last())).all()
    source = str(document.processing_task_id) if document.processing_task_id else None
    if any(row.chunk_index != index or row.source_parse_task_id != source or not row.text.strip()
           for index, row in enumerate(rows, 1)):
        raise EmbeddingError("EMBEDDING_SOURCE")
    return rows


def chunks_hash(rows: list) -> str:
    """冻结有序 Chunk 标识及完整文本摘要，不把正文放入消息或配置快照。"""

    return digest_json([{"id": str(row.chunk_id), "index": row.chunk_index,
                         "text_hash": digest_json(row.text)} for row in rows])


def checked_config(document: Document) -> tuple[dict, EmbeddingOptions]:
    """恢复私有配置并核对解析/切片代次；不能把旧代次向量附到新的 Chunk。"""

    config = document.embedding_config
    source = str(document.processing_task_id) if document.processing_task_id else None
    chunk_task = str(document.chunk_task_id) if document.chunk_task_id else None
    if (not isinstance(config, dict) or config.get("source_parse_task_id") != source
            or chunk_task is None or config.get("source_chunk_task_id") != chunk_task
            or not isinstance(config.get("source_hash"), str) or len(config["source_hash"]) != 64
            or not isinstance(config.get("output_dir"), str) or not Path(config["output_dir"]).is_absolute()
            or not isinstance(config.get("options"), dict)):
        raise EmbeddingError("EMBEDDING_CHECKPOINT")
    try:
        options = EmbeddingOptions(**config["options"])
        options.validate()
    except TypeError:
        raise EmbeddingError("EMBEDDING_CONFIG") from None
    if not options.device.startswith("cuda:"):
        raise EmbeddingError("EMBEDDING_GPU_REQUIRED")
    return config, options


def prepare_embedding(session: Session, document: Document, settings: Settings) -> tuple[UUID, bool]:
    """行锁下创建/恢复 GPU 代次；INDEXING/READY 不重算，入库失败应恢复 /index。"""

    active = session.scalar(select(DocumentParseJob.job_id).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))
    if active is not None:
        raise EmbeddingError("EMBEDDING_BUSY")
    if document.status in {"INDEXING", "READY", "PARTIAL_READY"} and document.embedding_task_id is not None:
        checked_config(document)
        return document.embedding_task_id, False
    if document.index_task_id is not None:
        raise EmbeddingError("EMBEDDING_STATE")
    if (document.status not in {"EMBEDDING", "FAILED"} or document.chunk_task_id is None
            or document.status == "FAILED" and document.embedding_task_id is None):
        raise EmbeddingError("EMBEDDING_STATE")
    rows = read_chunks(session, document)
    source_hash = chunks_hash(rows)
    if document.embedding_task_id is not None:
        config, _ = checked_config(document)
        if config["source_hash"] != source_hash:
            raise EmbeddingError("EMBEDDING_SOURCE")
    else:
        options = EmbeddingOptions.from_settings(settings)
        if not options.device.startswith("cuda:"):
            raise EmbeddingError("EMBEDDING_GPU_REQUIRED")
        root = settings.embeddings_dir
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        try:
            output_dir = str(root.resolve())
        except (OSError, ValueError):
            raise EmbeddingError("EMBEDDING_CONFIG") from None
        document.embedding_task_id = uuid4()
        document.embedding_config = {"options": options.snapshot(), "source_hash": source_hash,
            "source_parse_task_id": str(document.processing_task_id) if document.processing_task_id else None,
            "source_chunk_task_id": str(document.chunk_task_id), "output_dir": output_dir}
    document.status, document.error_message = "EMBEDDING", None
    session.flush()
    return document.embedding_task_id, True


def run_embedding_step(session: Session, doc_id: UUID, task_id: UUID) -> dict:
    """逐批原子发布；超限记录真实Chunk与token计数，失败不发布结果或写pgvector/READY。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    result = {"doc_id": str(doc_id), "task_id": str(task_id)}
    if document is None:
        return result | {"status": "not_found"}
    if document.embedding_task_id != task_id:
        return result | {"status": "stale_task"}
    result["document_status"] = document.status
    if document.status != "EMBEDDING":
        return result | {"status": "skipped"}
    config, options = checked_config(document)
    rows = read_chunks(session, document)
    if chunks_hash(rows) != config["source_hash"]:
        raise EmbeddingError("EMBEDDING_SOURCE")
    store = ArtifactStore(config["output_dir"])
    chunk_ids = [str(row.chunk_id) for row in rows]
    options_hash = digest_json(config["options"])
    manifest = store.ready(doc_id, task_id, config["source_hash"], options_hash, chunk_ids)
    reused = manifest is not None
    log_event("embedding.started", doc_id=doc_id, kb_id=document.kb_id, task_id=task_id,
              chunk_count=len(rows), batch_size=options.batch_size, device=options.device)
    if manifest is None:
        service = get_embedding_service(options)
        identity = service.identity()
        with store.writer(doc_id, task_id) as writer:
            for offset in range(0, len(rows), options.batch_size):
                batch = rows[offset:offset + options.batch_size]
                try:
                    vectors = service.embed_documents([row.text for row in batch])
                except EmbeddingError as exc:
                    position = exc.diagnostic.get("item_index")
                    if exc.code == "EMBEDDING_TOO_LONG" and position is not None and 1 <= position <= len(batch):
                        row = batch[position - 1]
                        log_event("embedding.chunk_rejected", level=logging.ERROR, doc_id=doc_id, task_id=task_id,
                            chunk_id=row.chunk_id, item_index=row.chunk_index, stage="embedding.token_limit",
                            error_code=exc.code, **{key: value for key, value in exc.diagnostic.items() if key != "item_index"})
                    raise
                if len(vectors) != len(batch):
                    raise EmbeddingError("EMBEDDING_OUTPUT")
                for row, vector in zip(batch, vectors):
                    writer.append(str(row.chunk_id), vector)
                log_event("embedding.batch_completed", doc_id=doc_id, task_id=task_id,
                          batch_index=offset // options.batch_size + 1, item_count=len(batch))
        manifest = {"format": "dense-jsonl-v1", "doc_id": str(doc_id), "kb_id": str(document.kb_id),
            "task_id": str(task_id), "source_hash": config["source_hash"], "options_hash": options_hash,
            "source_chunk_task_id": config["source_chunk_task_id"], "model": identity,
            "chunk_count": writer.count, "size_bytes": writer.size_bytes,
            "vectors_sha256": writer.digest.hexdigest()}
        store.publish(doc_id, task_id, manifest)
        # 以同一读取契约验证完整发布结果，避免文件写成功但身份/数量不完整就推进数据库状态。
        manifest = store.ready(doc_id, task_id, config["source_hash"], options_hash, chunk_ids)
    summary = {"chunk_count": manifest["chunk_count"], "dimension": DIMENSION,
               "model_name": manifest["model"]["model_name"],
               "model_fingerprint": manifest["model"]["model_fingerprint"],
               "vectors_sha256": manifest["vectors_sha256"]}
    if (manifest.get("kb_id") != str(document.kb_id)
            or manifest.get("source_chunk_task_id") != config["source_chunk_task_id"]
            or manifest["model"].get("max_length") != options.max_length
            or manifest["model"].get("precision") != ("fp16" if options.use_fp16 else "fp32")):
        raise EmbeddingError("EMBEDDING_ARTIFACT")
    document.embedding_config = config | {"result": summary}
    transition_document(document, "INDEXING")
    session.flush()
    return result | {"status": "awaiting_index", "document_status": document.status,
                     "artifact_reused": reused, **summary}


def record_embedding_failure(session: Session, doc_id: UUID, task_id: UUID, message: str, final: bool) -> None:
    """事务回滚后记录固定失败原因；迟到消息不得覆盖完成结果或新的解析/切片代次。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    if document is None or document.embedding_task_id != task_id or document.status != "EMBEDDING":
        return
    # 当前独立 task_id 已匹配；即便本代次检查点损坏也必须记录安全 FAILED，不能永久转圈。
    # 解析/切片重建会清除此标识，正常迟到消息不会进入此分支。
    if final:
        transition_document(document, "FAILED", message)
    else:
        document.error_message = message


def publish_embedding(session: Session, doc_id: UUID, task_id: UUID) -> bool:
    """检查点提交后只向 gpu_queue 投递 doc_id；确认丢失只标记等待代次，不覆盖已完成状态。"""

    from app.tasks.embeddings import embed_document

    try:
        embed_document.apply_async(args=[str(doc_id)], task_id=str(task_id), queue="gpu_queue")
    except Exception:
        session.rollback()
        with session.begin():
            record_embedding_failure(session, doc_id, task_id, "EMBEDDING_DISPATCH：GPU 任务投递确认失败，请恢复同一任务", True)
        log_event("embedding.dispatch_failed", level=logging.ERROR, doc_id=doc_id, task_id=task_id,
                  error_code="EMBEDDING_DISPATCH")
        return False
    log_event("embedding.dispatched", doc_id=doc_id, task_id=task_id, queue="gpu_queue")
    return True
