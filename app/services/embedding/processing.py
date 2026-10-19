"""GPU 文档向量化检查点和批处理，结果本地保存，阶段 15 前不写入 Chunk.embedding。"""

import logging
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings
from app.models import Document, DocumentChunk, DocumentParseJob
from app.services.document_processing import transition_document
from app.services.chunk.splitter import STREAM_VERSIONS
from app.services.embedding.artifacts import ArtifactStore, digest_json
from app.services.embedding.service import DIMENSION, MAX_ITEMS, MAX_TEXT_CHARS, MAX_TOTAL_CHARS, EmbeddingError, EmbeddingOptions, get_embedding_service
from app.services.embedding.refinement import POLICY as INPUT_POLICY, first_oversized, rebuild_chunks

# 旧检查点保留聚合预算；v6/v7按批次读取，Service每次调用仍保留1000万字符限制。
MAX_DOCUMENT_TOTAL_CHARS = 16_000_000


def streaming_document(document) -> bool:
    """v6/v7及显式Token重切配方按批处理；旧成功向量检查点不隐式换算法。"""
    config = getattr(document, "chunk_config", None)
    embedding = getattr(document, "embedding_config", None)
    return (isinstance(config, dict) and isinstance(config.get("options"), dict)
            and config["options"].get("version") in STREAM_VERSIONS
            or isinstance(embedding, dict) and embedding.get("input_policy") == INPUT_POLICY)


class ChunkRows:
    """可重复遍历的当前Chunk视图；仅保留计数，每遍有界读取并复核来源/连续序号。"""

    def __init__(self, session, document, query, count, *, allow_refinement=False):
        """绑定行锁会话；重切预检可读取巨片，但后续索引读取仍执行单片字符保护。"""
        self.session, self.document, self.query, self.count = session, document, query, count
        self.allow_refinement = allow_refinement

    def __len__(self):
        """返回已核对的SQL计数，供结果与索引完整性校验。"""
        return self.count

    def __iter__(self):
        """每次预取256行并核对来源/序号；仅重切输入阶段把字符超限交给预检处理。"""
        source = str(self.document.processing_task_id) if self.document.processing_task_id else None
        seen = 0
        for seen, row in enumerate(self.session.execute(self.query.execution_options(yield_per=256)), 1):
            if row.chunk_index != seen or row.source_parse_task_id != source or not row.text.strip():
                raise EmbeddingError("EMBEDDING_SOURCE")
            if not self.allow_refinement and len(row.text) > MAX_TEXT_CHARS:
                error = EmbeddingError("EMBEDDING_LIMIT", step="TEXT_CHAR_LIMIT", item_index=seen,
                                       item_count=len(row.text), limit_count=MAX_TEXT_CHARS)
                log_event("embedding.chunk_resource_rejected", level=logging.ERROR, doc_id=self.document.doc_id,
                          task_id=getattr(self.document, "embedding_task_id", None), chunk_id=row.chunk_id,
                          stage="embedding.input_budget", error_code=error.code, **error.diagnostic)
                raise error
            yield row
        if seen != self.count:
            raise EmbeddingError("EMBEDDING_SOURCE")


def read_chunks(session: Session, document: Document, *, allow_refinement=False) -> list | ChunkRows:
    """重切预检使用有界视图接住旧巨片；其他入口保留冻结预算与来源/字符保护。"""

    predicate = (DocumentChunk.doc_id == document.doc_id, DocumentChunk.kb_id == document.kb_id)
    count, chars = session.execute(select(func.count(), func.coalesce(func.sum(func.length(DocumentChunk.text)), 0))
                                  .select_from(DocumentChunk).where(*predicate)).one()
    if count == 0:
        raise EmbeddingError("EMBEDDING_EMPTY")
    if not allow_refinement and not streaming_document(document) and (count > MAX_ITEMS or chars > MAX_DOCUMENT_TOTAL_CHARS):
        chars_exceeded = chars > MAX_DOCUMENT_TOTAL_CHARS
        log_event("embedding.input_rejected", level=logging.ERROR, doc_id=document.doc_id,
            stage="embedding.document_budget", step="TOTAL_CHAR_LIMIT" if chars_exceeded else "CHUNK_COUNT_LIMIT",
            chunk_count=count, item_count=chars if chars_exceeded else count,
            limit_count=MAX_DOCUMENT_TOTAL_CHARS if chars_exceeded else MAX_ITEMS, error_code="EMBEDDING_LIMIT")
        raise EmbeddingError("EMBEDDING_LIMIT")
    query = (select(DocumentChunk.chunk_id, DocumentChunk.text,
        DocumentChunk.chunk_metadata["chunk_index"].as_integer().label("chunk_index"),
        DocumentChunk.chunk_metadata["source_parse_task_id"].as_string().label("source_parse_task_id"))
        .where(*predicate).order_by(DocumentChunk.chunk_metadata["chunk_index"].as_integer().asc().nulls_last()))
    if allow_refinement or streaming_document(document):
        return ChunkRows(session, document, query, count, allow_refinement=allow_refinement)
    rows = session.execute(query).all()
    source = str(document.processing_task_id) if document.processing_task_id else None
    if any(row.chunk_index != index or row.source_parse_task_id != source or not row.text.strip()
           for index, row in enumerate(rows, 1)):
        raise EmbeddingError("EMBEDDING_SOURCE")
    return rows


def chunks_hash(rows: list) -> str:
    """增量生成与旧规范JSON完全相同的摘要，不收集整份正文或Hash列表。"""

    digest = hashlib.sha256()
    digest.update(b"[")
    for index, row in enumerate(rows):
        if index:
            digest.update(b",")
        value = {"id": str(row.chunk_id), "index": row.chunk_index, "text_hash": digest_json(row.text)}
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    digest.update(b"]")
    return digest.hexdigest()


def embedding_batches(rows, batch_size):
    """同时按批大小和Service单次字符预算分组；总文档再大也不合并成一次模型调用。"""
    batch, chars = [], 0
    for row in rows:
        if batch and (len(batch) >= batch_size or chars + len(row.text) > MAX_TOTAL_CHARS):
            yield batch
            batch, chars = [], 0
        batch.append(row)
        chars += len(row.text)
    if batch:
        yield batch


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
    if "input_policy" in config and config["input_policy"] != INPUT_POLICY:
        raise EmbeddingError("EMBEDDING_CHECKPOINT")
    return config, options


def prepare_embedding(session: Session, document: Document, settings: Settings) -> tuple[UUID, bool]:
    """新输入和长度失败Retry启用真实Token重切；保留模型配置，已完成向量不重算。"""

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
    previous = document.embedding_config
    refine = (document.embedding_task_id is None
        or isinstance(previous, dict) and previous.get("input_policy") == INPUT_POLICY
        or document.status == "FAILED" and (document.error_message or "").split("：", 1)[0]
            in {"EMBEDDING_TOO_LONG", "EMBEDDING_LIMIT"})
    rows = read_chunks(session, document, allow_refinement=True) if refine else read_chunks(session, document)
    source_hash = chunks_hash(rows)
    if document.embedding_task_id is not None:
        config, _ = checked_config(document)
        if config["source_hash"] != source_hash:
            raise EmbeddingError("EMBEDDING_SOURCE")
        if refine:
            document.embedding_config = config | {"input_policy": INPUT_POLICY}
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
            "source_chunk_task_id": str(document.chunk_task_id), "output_dir": output_dir,
            "input_policy": INPUT_POLICY}
    document.status, document.error_message = "EMBEDDING", None
    session.flush()
    return document.embedding_task_id, True


def run_embedding_step(session: Session, doc_id: UUID, task_id: UUID) -> dict:
    """新版先真实Token预检；超长先提交新切片/向量代次，再投递编码，不发布旧输入产物。"""

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
    refine = config.get("input_policy") == INPUT_POLICY
    rows = read_chunks(session, document, allow_refinement=True) if refine else read_chunks(session, document)
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
        if refine:
            repaired = config.get("token_refinement")
            if repaired is not None and (not isinstance(repaired, dict) or repaired.get("version") != INPUT_POLICY
                    or repaired.get("model_fingerprint") != identity["model_fingerprint"]
                    or repaired.get("max_length") != identity["max_length"]):
                raise EmbeddingError("EMBEDDING_MODEL_CHANGED")
            log_event("embedding.preflight_started", doc_id=doc_id, task_id=task_id,
                      stage="embedding.token_preflight", chunk_count=len(rows))
            oversized = first_oversized(rows, service, options.max_length, batch_size=options.batch_size)
            if oversized is not None:
                if repaired is not None or getattr(document, "index_task_id", None) is not None:
                    # 同模型重切结果必已逐片通过；不允许状态损坏形成无限重切/队列循环。
                    raise EmbeddingError("EMBEDDING_SOURCE")
                row, step, observed, limit = oversized
                log_event("embedding.rechunk_started", doc_id=doc_id, task_id=task_id, chunk_id=row.chunk_id,
                    stage="embedding.rechunk", step=step, item_index=row.chunk_index,
                    item_count=observed, limit_count=limit)
                if (not isinstance(document.chunk_config, dict)
                        or not isinstance(document.chunk_config.get("options"), dict)):
                    raise EmbeddingError("EMBEDDING_CHECKPOINT")
                previous_options = document.chunk_config["options"]
                count = rebuild_chunks(session, document, service, identity)
                # 新代次先与Chunk同事务提交；当前任务从不写任何旧/新向量文件。
                # SQL失败则整批回滚；提交后投递丢失可用Retry恢复新的向量检查点。
                document.chunk_task_id = uuid5(document.chunk_task_id, INPUT_POLICY + ":" + digest_json(identity))
                new_rows = read_chunks(session, document)
                source_hash = chunks_hash(new_rows)
                next_task_id = uuid5(task_id, INPUT_POLICY + ":" + source_hash)
                document.embedding_task_id = next_task_id
                document.embedding_config = {key: value for key, value in config.items() if key != "result"} | {
                    "source_hash": source_hash, "source_chunk_task_id": str(document.chunk_task_id),
                    "token_refinement": {"version": INPUT_POLICY, "previous_source_hash": config["source_hash"],
                        "previous_chunk_options": previous_options, "model_fingerprint": identity["model_fingerprint"],
                        "max_length": options.max_length}}
                management = getattr(document, "management_config", None)
                if (isinstance(management, dict) and management.get("state") == "running"
                        and management.get("stage") == "embed" and management.get("stage_task_id") == str(task_id)):
                    document.management_config = management | {"stage_task_id": str(next_task_id)}
                session.flush()
                return result | {"status": "rechunked", "document_status": "EMBEDDING",
                                 "chunk_count": count, "next_task_id": str(next_task_id)}
            log_event("embedding.preflight_passed", doc_id=doc_id, task_id=task_id,
                      stage="embedding.token_preflight", chunk_count=len(rows))
        streamed = streaming_document(document)
        with store.writer(doc_id, task_id, streaming=streamed) as writer:
            for batch_index, batch in enumerate(embedding_batches(rows, options.batch_size), 1):
                try:
                    vectors = service.embed_documents([row.text for row in batch])
                except EmbeddingError as exc:
                    position = exc.diagnostic.get("item_index")
                    if exc.code in {"EMBEDDING_TOO_LONG", "EMBEDDING_LIMIT"} and position is not None and 1 <= position <= len(batch):
                        row = batch[position - 1]
                        log_event("embedding.chunk_rejected" if exc.code == "EMBEDDING_TOO_LONG" else "embedding.chunk_resource_rejected",
                            level=logging.ERROR, doc_id=doc_id, task_id=task_id,
                            chunk_id=row.chunk_id, item_index=row.chunk_index,
                            stage="embedding.token_limit" if exc.code == "EMBEDDING_TOO_LONG" else "embedding.input_budget",
                            error_code=exc.code, **{key: value for key, value in exc.diagnostic.items() if key != "item_index"})
                    raise
                if len(vectors) != len(batch):
                    raise EmbeddingError("EMBEDDING_OUTPUT")
                for row, vector in zip(batch, vectors):
                    writer.append(str(row.chunk_id), vector)
                log_event("embedding.batch_completed", doc_id=doc_id, task_id=task_id,
                          batch_index=batch_index, item_count=len(batch))
        manifest = {"format": "dense-jsonl-stream-v2" if streamed else "dense-jsonl-v1", "doc_id": str(doc_id), "kb_id": str(document.kb_id),
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
