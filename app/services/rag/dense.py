"""知识库/指定文档的只读 Dense SQL 边界；只查询既有 Chunk，不接管向量存储。"""

import math
import re
from dataclasses import dataclass
from numbers import Real
from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only

from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.embedding.service import DIMENSION, MODEL_ID, VERSION
from app.services.indexing import INDEX_VERSION
from app.services.rag.database import DATABASE_REASONS, database_diagnostic, database_error_codes
from app.services.rag.llama_index.validation import json_snapshot, vector_snapshot

DEFAULT_TOP_K = 20
MAX_TOP_K = 100
MAX_DOC_IDS = 100
MAX_QUERY_CHARS = 32768
MODEL_FIELDS = {"model_name", "model_fingerprint", "dimension", "normalized", "version", "max_length", "precision"}
ERRORS = {
    **database_error_codes("DENSE"),
    "DENSE_INPUT": "知识库、文档范围、Query 或 Top-K 参数无效",
    "DENSE_LIMIT": "文档数量、Query 或 Top-K 超过安全上限",
    "DENSE_MODEL": "Query 模型身份或编码版本不可用",
    "DENSE_MODEL_CHANGED": "Query 编码期间模型身份变化，本次未返回检索结果",
    "DENSE_QUERY_EMBEDDING": "Query 编码未完成，请人工检查已有 BGE-M3 部署",
    "DENSE_DATABASE": "Dense 数据库查询未完成，请检查现有 PostgreSQL/pgvector 配置",
    "DENSE_SESSION": "Dense 仅接受独立无待写入变更的只读会话",
    "DENSE_RESULT": "数据库检索结果的身份、证据或距离无效，未返回部分结果",
    "DENSE_FAILED": "Dense 检索未完成，请查看安全错误码",
}


class DenseRetrievalError(Exception):
    """仅输出固定中文错误，禁止泄露 Query、向量、SQL、连接串及模型路径。"""

    def __init__(self, code: str, *, database_error=None):
        """在首次数据库捕获点保留SQLSTATE分类，未知类别不透传异常原文。"""
        self.database_diagnostic = database_diagnostic(database_error) if code == "DENSE_DATABASE" else None
        if self.database_diagnostic and self.database_diagnostic["reason"] in DATABASE_REASONS:
            code += "_" + self.database_diagnostic["reason"]
        self.code = code if code in ERRORS else "DENSE_FAILED"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class DenseHit:
    """会话内已加载的原 Chunk/Document 与余弦距离，不是新数据库记录或概率置信度。"""

    chunk: DocumentChunk
    document: Document
    distance: float

    @property
    def similarity(self) -> float:
        """以 1-distance 表示余弦相似度，保留零/负分，不解释为概率。"""
        return 1.0 - self.distance


def validate_scope(kb_id: UUID | str, top_k: int) -> UUID:
    """知识库须为真实 UUID 表示；Top-K 严格整数 1～100，布尔值/浮点值不转换。"""
    try:
        if not isinstance(kb_id, (UUID, str)):
            raise ValueError
        identity = kb_id if isinstance(kb_id, UUID) else UUID(kb_id)
        if identity.int == 0 or type(top_k) is not int or top_k < 1:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DenseRetrievalError("DENSE_INPUT") from None
    if top_k > MAX_TOP_K:
        raise DenseRetrievalError("DENSE_LIMIT")
    return identity


def validate_query(query: str) -> str:
    """接受非空原始 Query，拒绝超限和非法控制符；不修剪、加前缀或写入日志。"""
    if not isinstance(query, str):
        raise DenseRetrievalError("DENSE_INPUT")
    if len(query) > MAX_QUERY_CHARS:
        raise DenseRetrievalError("DENSE_LIMIT")
    if (not query.strip()
            or any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in query)):
        raise DenseRetrievalError("DENSE_INPUT")
    return query


def validate_doc_ids(doc_ids: list[UUID | str] | tuple[UUID | str, ...] | None) -> tuple[UUID, ...] | None:
    """省略表示全库；显式范围须为1～100个非零UUID，去重排序并冻结，空列表不放宽为全库。"""
    if doc_ids is None:
        return None
    if not isinstance(doc_ids, (list, tuple)) or not doc_ids:
        raise DenseRetrievalError("DENSE_INPUT")
    if len(doc_ids) > MAX_DOC_IDS:
        raise DenseRetrievalError("DENSE_LIMIT")
    # 复用现有 UUID 严格校验；不读取 Document、不按字符串插入 SQL。
    return tuple(sorted({validate_scope(value, DEFAULT_TOP_K) for value in doc_ids}))


def model_snapshot(value: dict) -> dict:
    """严格冻结阶段 14 实际身份：文件指纹、版本、维度、归一化、长度及精度均须相容。"""
    try:
        identity = json_snapshot(value)
        if (set(identity) != MODEL_FIELDS or identity["model_name"] != MODEL_ID
                or type(identity["dimension"]) is not int or identity["dimension"] != DIMENSION
                or identity["normalized"] is not True or identity["version"] != VERSION
                or not isinstance(identity["model_fingerprint"], str)
                or re.fullmatch(r"[0-9a-f]{64}", identity["model_fingerprint"]) is None
                or type(identity["max_length"]) is not int or not 8 <= identity["max_length"] <= 8192
                or identity["precision"] not in {"fp16", "fp32"}):
            raise ValueError
        return identity
    except Exception:
        raise DenseRetrievalError("DENSE_MODEL") from None


def eligibility(kb_id: UUID, identity: dict, doc_ids: tuple[UUID, ...] | None = None) -> tuple:
    """知识库及可选文档过滤均进入候选 SQL，再做 Top-K；未知模型/失效代次不参与。"""
    config = Document.index_config
    parse_id = cast(Document.processing_task_id, String)
    filters = (
        DocumentChunk.kb_id == kb_id, Document.kb_id == kb_id, KnowledgeBase.kb_id == kb_id,
        KnowledgeBase.status == "ACTIVE", Document.status.in_(["READY", "PARTIAL_READY"]), DocumentChunk.embedding.is_not(None),
        Document.index_task_id.is_not(None),
        config["version"].as_string() == INDEX_VERSION,
        config["result"]["model"] == identity,
        config["embedding_task_id"].as_string() == cast(Document.embedding_task_id, String),
        config["source_chunk_task_id"].as_string() == cast(Document.chunk_task_id, String),
        config["source_parse_task_id"].as_string() == parse_id,
        DocumentChunk.chunk_metadata["source_parse_task_id"].as_string() == parse_id,
    )
    return filters if doc_ids is None else filters + (DocumentChunk.doc_id.in_(doc_ids),)


def dense_statement(kb_id: UUID | str, query_vector: list[float], identity: dict,
                    top_k: int = DEFAULT_TOP_K, *,
                    doc_ids: list[UUID | str] | tuple[UUID | str, ...] | None = None):
    """物化已过滤候选的ID/标量距离，按距离及UUID精确取Top-K后读取正文。

    物化屏障避免 HNSW 近似候选截断造成过滤后的漏项；不修改索引或向量值。
    """
    kb_id = validate_scope(kb_id, top_k)
    doc_ids = validate_doc_ids(doc_ids)
    identity = model_snapshot(identity)
    try:
        vector = vector_snapshot(query_vector)
    except Exception:
        raise DenseRetrievalError("DENSE_MODEL") from None
    owner_join = (DocumentChunk.doc_id == Document.doc_id) & (DocumentChunk.kb_id == Document.kb_id)
    distance = DocumentChunk.embedding.cosine_distance(vector)
    # 物化全部1024维向量会产生大量临时I/O；屏障内计算距离，仅保留ID与标量。
    candidates = (select(DocumentChunk.chunk_id, distance.label("cosine_distance")).select_from(DocumentChunk)
        .join(Document, owner_join).join(KnowledgeBase, KnowledgeBase.kb_id == Document.kb_id)
        .where(*eligibility(kb_id, identity, doc_ids)).cte("dense_eligible").prefix_with("MATERIALIZED", dialect="postgresql"))
    ranked = (select(candidates.c.chunk_id, candidates.c.cosine_distance)
        .order_by(candidates.c.cosine_distance.asc(), candidates.c.chunk_id.asc()).limit(top_k).cte("dense_ranked"))
    return (select(DocumentChunk, Document, ranked.c.cosine_distance).select_from(DocumentChunk)
        .join(ranked, ranked.c.chunk_id == DocumentChunk.chunk_id).join(Document, owner_join)
        .options(load_only(DocumentChunk.chunk_id, DocumentChunk.kb_id, DocumentChunk.doc_id,
                          DocumentChunk.text, DocumentChunk.page_start, DocumentChunk.page_end,
                          DocumentChunk.section, DocumentChunk.chunk_metadata, raiseload=True),
                 load_only(Document.doc_id, Document.kb_id, Document.file_name, Document.file_type,
                           Document.index_config, Document.status, Document.index_task_id,
                           Document.processing_task_id, Document.chunk_task_id, Document.embedding_task_id, raiseload=True))
        .order_by(ranked.c.cosine_distance.asc(), DocumentChunk.chunk_id.asc())
        .execution_options(populate_existing=True))


def select_dense_hits(session: Session, kb_id: UUID | str, query_vector: list[float], identity: dict,
                      top_k: int = DEFAULT_TOP_K, *,
                      doc_ids: list[UUID | str] | tuple[UUID | str, ...] | None = None) -> list[DenseHit]:
    """只读执行已下推范围的 SELECT；越界行全批拒绝，不以 Python 删除其他文档冒充 SQL 过滤。"""
    kb_id = validate_scope(kb_id, top_k)
    doc_ids = validate_doc_ids(doc_ids)
    identity = model_snapshot(identity)
    statement = dense_statement(kb_id, query_vector, identity, top_k, doc_ids=doc_ids)
    if session.new or session.dirty or session.deleted:
        raise DenseRetrievalError("DENSE_SESSION")
    try:
        with session.no_autoflush:
            rows = session.execute(statement).all()
    except SQLAlchemyError as exc:
        raise DenseRetrievalError("DENSE_DATABASE", database_error=exc) from None
    if len(rows) > top_k:
        raise DenseRetrievalError("DENSE_RESULT")
    hits, seen, last = [], set(), None
    for row in rows:
        try:
            chunk, document, distance = row
            config = document.index_config
            if (chunk.kb_id != kb_id or document.kb_id != kb_id or chunk.doc_id != document.doc_id
                    or (doc_ids is not None and chunk.doc_id not in doc_ids)
                    or document.status not in {"READY", "PARTIAL_READY"} or document.index_task_id is None
                    or not isinstance(config, dict) or config.get("version") != INDEX_VERSION
                    or model_snapshot(config["result"]["model"]) != identity
                    or any(config.get(key) != str(getattr(document, attribute)) for key, attribute in (
                        ("embedding_task_id", "embedding_task_id"), ("source_chunk_task_id", "chunk_task_id"),
                        ("source_parse_task_id", "processing_task_id")))
                    or chunk.chunk_metadata.get("source_parse_task_id") != str(document.processing_task_id)
                    or not isinstance(chunk.chunk_id, UUID) or chunk.chunk_id in seen
                    or isinstance(distance, bool) or not isinstance(distance, Real)
                    or not math.isfinite(distance) or not 0 <= distance <= 2):
                raise ValueError
            key = (float(distance), chunk.chunk_id.int)
            if last is not None and key < last:
                raise ValueError
            seen.add(chunk.chunk_id)
            last = key
            hits.append(DenseHit(chunk, document, float(distance)))
        except Exception:
            raise DenseRetrievalError("DENSE_RESULT") from None
    return hits
