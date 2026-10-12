"""校验阶段 14 的本地完整结果，事务性批量写 pgvector；不加载模型或执行检索。"""

import hashlib
import json
import logging
import math
import re
from numbers import Real
from uuid import UUID, uuid4

from sqlalchemy import bindparam, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.models import Document, DocumentChunk, DocumentParseJob
from app.services.document_processing import transition_document
from app.services.embedding.artifacts import MAX_BYTES, ArtifactStore, digest_json
from app.services.embedding.processing import checked_config, chunks_hash, read_chunks
from app.services.embedding.service import DIMENSION, MAX_ITEMS, MODEL_ID, VERSION, EmbeddingError

INDEX_NAME = "ix_document_chunks_embedding_hnsw"
INDEX_VERSION = "pgvector-index-v1"
WRITE_BATCH_SIZE = 256
NORM_TOLERANCE = 1e-4
ERRORS = {
    "INDEX_STATE": "文档尚未完成本地向量化或当前状态不允许入库",
    "INDEX_BUSY": "文档仍有活动解析任务",
    "INDEX_CHECKPOINT": "入库检查点与当前向量、切片或解析代次不一致",
    "INDEX_SOURCE": "当前 Chunk 的身份、顺序或内容与向量输入不一致",
    "INDEX_ARTIFACT": "完整本地向量结果缺失、损坏或与冻结身份不一致",
    "INDEX_VECTOR": "向量维度、数值或 L2 归一化无效，未修改原始向量",
    "INDEX_SCHEMA": "pgvector HNSW 索引不可用，请检查扩展及数据库迁移",
    "INDEX_INCOMPLETE": "向量入库数量、维度或范数不完整，已回滚本次写入",
    "INDEX_DATABASE": "向量入库数据库暂时不可用",
    "INDEX_DATABASE_EXHAUSTED": "向量入库数据库重试已耗尽",
    "INDEX_DISPATCH": "入库任务投递确认失败，请查询状态并恢复 /index",
    "INDEX_FAILED": "向量入库未完成，原有 Chunk 和本地结果已保留",
}


class IndexingError(Exception):
    """固定错误码与安全文案，不透传 SQL、向量、正文或存储路径。"""

    def __init__(self, code: str):
        """未知异常类别使用通用失败文案，防止原始异常进入接口及任务结果。"""

        self.code = code if code in ERRORS else "INDEX_FAILED"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def input_identity(document: Document) -> dict:
    """仅从已完成的 GPU 检查点冻结身份；不访问模型、磁盘或外部服务。"""

    try:
        config, _ = checked_config(document)
        result = config.get("result")
        if (document.embedding_task_id is None or not isinstance(result, dict)
                or set(result) != {"chunk_count", "dimension", "model_name", "model_fingerprint", "vectors_sha256"}
                or type(result["chunk_count"]) is not int or not 1 <= result["chunk_count"] <= MAX_ITEMS
                or result["dimension"] != DIMENSION or result["model_name"] != MODEL_ID
                or any(not isinstance(result[key], str) or re.fullmatch(r"[0-9a-f]{64}", result[key]) is None
                       for key in ("model_fingerprint", "vectors_sha256"))):
            raise IndexingError("INDEX_CHECKPOINT")
        return {"version": INDEX_VERSION, "embedding_task_id": str(document.embedding_task_id),
                "source_parse_task_id": config["source_parse_task_id"],
                "source_chunk_task_id": config["source_chunk_task_id"], "source_hash": config["source_hash"],
                "options_hash": digest_json(config["options"]), "embedding_result": dict(result)}
    except (EmbeddingError, ValueError, TypeError, KeyError):
        raise IndexingError("INDEX_CHECKPOINT") from None


def checked_index_config(document: Document) -> dict:
    """核对完整冻结身份，拒绝把旧向量写到新 Chunk；结果摘要不参与输入身份。"""

    config = document.index_config
    if (not isinstance(config, dict)
            or {key: value for key, value in config.items() if key != "result"} != input_identity(document)):
        raise IndexingError("INDEX_CHECKPOINT")
    return config


def require_vector_index(session: Session) -> None:
    """只检查当前业务表的有效 HNSW/cosine 部分索引，不在任务中创建或修复索引。"""

    available = session.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam "
        "JOIN pg_opclass oc ON oc.oid=i.indclass[0] "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attname='embedding' "
        "WHERE i.indrelid=to_regclass('document_chunks') AND c.relname=:name "
        "AND am.amname='hnsw' AND oc.opcname='vector_cosine_ops' "
        "AND i.indisvalid AND i.indisready AND i.indnatts=1 AND i.indkey[0]=a.attnum "
        "AND pg_get_expr(i.indpred,i.indrelid)='(embedding IS NOT NULL)')"
    ), {"name": INDEX_NAME})
    if not available:
        raise IndexingError("INDEX_SCHEMA")


def require_complete_vectors(session: Session, document: Document, expected_count: int) -> None:
    """推进 READY 前在数据库核对非空数量、1024 维及 float32 存储后的范数。"""

    invalid = or_(func.vector_dims(DocumentChunk.embedding) != DIMENSION,
                  func.abs(func.vector_norm(DocumentChunk.embedding) - 1) > NORM_TOLERANCE)
    total, present, invalid_count = session.execute(select(
        func.count(), func.count(DocumentChunk.embedding), func.count().filter(invalid)
    ).select_from(DocumentChunk).where(DocumentChunk.doc_id == document.doc_id,
                                       DocumentChunk.kb_id == document.kb_id)).one()
    if total != expected_count or present != expected_count or invalid_count != 0:
        raise IndexingError("INDEX_INCOMPLETE")


def prepare_index(session: Session, document: Document) -> tuple[UUID, bool]:
    """文档行锁下创建或恢复独立 CPU 入库代次，重复完成请求不投递、不重算 Embedding。"""

    active = session.scalar(select(DocumentParseJob.job_id).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))
    if active is not None:
        raise IndexingError("INDEX_BUSY")
    if document.status not in {"INDEXING", "FAILED", "READY"} or (
            document.status in {"FAILED", "READY"} and document.index_task_id is None):
        raise IndexingError("INDEX_STATE")
    identity = input_identity(document)
    require_vector_index(session)
    if document.index_task_id is not None:
        checked_index_config(document)
    elif document.index_config is not None:
        raise IndexingError("INDEX_CHECKPOINT")
    else:
        document.index_task_id, document.index_config = uuid4(), identity
    if document.status == "READY":
        require_complete_vectors(session, document, identity["embedding_result"]["chunk_count"])
        return document.index_task_id, False
    # FAILED 只恢复自己的入库检查点；不把解析/模型失败的旧结果当作完整 GPU 输出。
    document.status, document.error_message = "INDEXING", None
    session.flush()
    return document.index_task_id, True


def checked_manifest(document: Document, chunk_ids: list[str]) -> tuple[ArtifactStore, dict]:
    """复用已有全量文件校验，再绑定模型指纹、编码版本、精度和 GPU 结果摘要。"""

    try:
        config, options = checked_config(document)
        store = ArtifactStore(config["output_dir"])
        manifest = store.ready(document.doc_id, document.embedding_task_id, config["source_hash"],
                               digest_json(config["options"]), chunk_ids)
        if manifest is None:
            raise IndexingError("INDEX_ARTIFACT")
        model, expected = manifest["model"], config["result"]
        if (manifest.get("kb_id") != str(document.kb_id)
                or manifest.get("source_chunk_task_id") != config["source_chunk_task_id"]
                or model.get("version") != VERSION or model.get("max_length") != options.max_length
                or model.get("precision") != ("fp16" if options.use_fp16 else "fp32")
                or any(model.get(key) != expected[key] for key in ("model_name", "dimension", "model_fingerprint"))
                or manifest["chunk_count"] != expected["chunk_count"]
                or manifest["vectors_sha256"] != expected["vectors_sha256"]):
            raise IndexingError("INDEX_ARTIFACT")
        return store, manifest
    except (EmbeddingError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise IndexingError("INDEX_ARTIFACT") from None


def checked_vector(vector) -> list[float]:
    """严格接受原有 1024 维有限、L2 单位向量，不悄悄重新归一化或改变数据。"""

    try:
        if (not isinstance(vector, list) or len(vector) != DIMENSION
                or any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
                       for value in vector)):
            raise IndexingError("INDEX_VECTOR")
        values = [float(value) for value in vector]
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not math.isfinite(norm) or abs(norm - 1) > NORM_TOLERANCE:
            raise IndexingError("INDEX_VECTOR")
        return values
    except (ValueError, TypeError, OverflowError):
        raise IndexingError("INDEX_VECTOR") from None


def vector_batches(store: ArtifactStore, doc_id: UUID, embedding_task_id: UUID,
                   manifest: dict, chunk_ids: list[str]):
    """有界逐批读取原文件；再次核对身份和校验和，防止预检后文件变化导致半份入库。"""

    count = total_bytes = 0
    digest, batch = hashlib.sha256(), []
    try:
        with (store.directory(doc_id, embedding_task_id) / "vectors.jsonl").open("rb") as stream:
            while line := stream.readline(128 * 1024 + 1):
                total_bytes += len(line)
                if len(line) > 128 * 1024 or total_bytes > MAX_BYTES or count >= len(chunk_ids):
                    raise IndexingError("INDEX_ARTIFACT")
                row = json.loads(line)
                if (not isinstance(row, dict) or set(row) != {"chunk_id", "chunk_index", "vector"}
                        or row["chunk_id"] != chunk_ids[count] or type(row["chunk_index"]) is not int
                        or row["chunk_index"] != count + 1):
                    raise IndexingError("INDEX_ARTIFACT")
                batch.append({"_chunk_id": UUID(row["chunk_id"]), "_vector": checked_vector(row["vector"])})
                digest.update(line)
                count += 1
                if len(batch) == WRITE_BATCH_SIZE:
                    yield batch
                    batch = []
        if (count != len(chunk_ids) or count != manifest["chunk_count"]
                or total_bytes != manifest["size_bytes"] or digest.hexdigest() != manifest["vectors_sha256"]):
            raise IndexingError("INDEX_ARTIFACT")
        if batch:
            yield batch
    except (OSError, ValueError, TypeError, KeyError):
        raise IndexingError("INDEX_ARTIFACT") from None


def write_batch(session: Session, document: Document, batch: list[dict]) -> None:
    """用参数化 Core executemany 更新本文件现有 Chunk；不插入新行、不提交部分批次。"""

    table = DocumentChunk.__table__
    result = session.execute(update(table).where(
        table.c.chunk_id == bindparam("_chunk_id"), table.c.doc_id == document.doc_id,
        table.c.kb_id == document.kb_id).values(embedding=bindparam("_vector")), batch)
    if result.rowcount != len(batch):
        raise IndexingError("INDEX_INCOMPLETE")


def run_index_step(session: Session, doc_id: UUID, task_id: UUID) -> dict:
    """行锁保护全量向量与 READY 同事务；任一文件、批次或提交失败由任务整体回滚。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    result = {"doc_id": str(doc_id), "task_id": str(task_id)}
    if document is None:
        return result | {"status": "not_found"}
    result["document_status"] = document.status
    if document.index_task_id != task_id:
        return result | {"status": "stale_task"}
    if document.status != "INDEXING":
        return result | {"status": "skipped"}
    config = checked_index_config(document)
    require_vector_index(session)
    try:
        # 先执行现有数量/字符上限检查，避免异常海量 Chunk 在锁阶段无界读取。
        read_chunks(session, document)
        # 文档锁阻止维护/新增引用，Chunk 行锁保护已有行，取得锁后重新读取完整输入。
        session.execute(select(DocumentChunk.chunk_id).where(DocumentChunk.doc_id == doc_id)
                        .order_by(DocumentChunk.chunk_id).with_for_update()).all()
        rows = read_chunks(session, document)
        if (chunks_hash(rows) != config["source_hash"]
                or len(rows) != config["embedding_result"]["chunk_count"]):
            raise IndexingError("INDEX_SOURCE")
    except EmbeddingError:
        raise IndexingError("INDEX_SOURCE") from None
    chunk_ids = [str(row.chunk_id) for row in rows]
    store, manifest = checked_manifest(document, chunk_ids)
    log_event("index.started", doc_id=doc_id, kb_id=document.kb_id, task_id=task_id,
              chunk_count=len(rows), dimension=DIMENSION)
    for index, batch in enumerate(vector_batches(store, doc_id, document.embedding_task_id, manifest, chunk_ids), 1):
        write_batch(session, document, batch)
        log_event("index.batch_written", doc_id=doc_id, task_id=task_id, batch_index=index, item_count=len(batch))
    require_complete_vectors(session, document, len(rows))
    summary = {"chunk_count": len(rows), "dimension": DIMENSION, "model": dict(manifest["model"]),
               "vectors_sha256": manifest["vectors_sha256"], "index_name": INDEX_NAME}
    document.index_config = config | {"result": summary}
    transition_document(document, "READY")
    session.flush()
    return result | {"status": "ready", "document_status": document.status,
                     "chunk_count": len(rows), "dimension": DIMENSION, "model_name": MODEL_ID}


def record_index_failure(session: Session, doc_id: UUID, task_id: UUID, code: str, final: bool) -> None:
    """回滚后独立记录固定失败原因，旧消息和已提交 READY 不被迟到错误覆盖。"""

    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update()
                              .execution_options(populate_existing=True))
    if document is None or document.index_task_id != task_id or document.status != "INDEXING":
        return
    message = str(IndexingError(code))
    if final:
        transition_document(document, "FAILED", message)
    else:
        document.error_message = message


def publish_index(session: Session, doc_id: UUID, task_id: UUID) -> bool:
    """提交检查点后投递一个 CPU 任务，确认丢失保留同代次，不回退已提交 READY。"""

    from app.tasks.indexing import index_document

    try:
        index_document.apply_async(args=[str(doc_id)], task_id=str(task_id), queue="default_queue")
    except Exception:
        session.rollback()
        with session.begin():
            record_index_failure(session, doc_id, task_id, "INDEX_DISPATCH", True)
        log_event("index.dispatch_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="INDEX_DISPATCH")
        return False
    log_event("index.dispatched", doc_id=doc_id, task_id=task_id, queue="default_queue")
    return True
