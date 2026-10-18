"""项目引用生成和显式只读来源反查；不依赖LlamaIndex，不解析原件或运行模型。"""

import hashlib
import json
import logging
from collections.abc import Callable
from time import perf_counter
from uuid import UUID

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only
from sqlalchemy.orm.attributes import NO_VALUE

from app.core.business_logging import log_event
from app.db.session import get_engine
from app.models import Document, DocumentBlock, DocumentChunk, KnowledgeBase
from app.schemas.evidence import Citation, EvidenceTrace, ResolvedSourceSpan, SourceSpan
from app.schemas.rag import RAGAnswer
from app.schemas.retrieval import RetrievalResult

MAX_SPANS = 1024
MAX_SOURCE_CHARS = 10000000
CHUNK_FIELDS = ("chunk_id", "kb_id", "doc_id", "text", "page_start", "page_end", "section", "chunk_metadata")
DOCUMENT_FIELDS = ("doc_id", "kb_id", "file_name", "file_type", "file_hash", "source_type", "status",
                   "processing_task_id", "chunk_task_id", "chunk_config", "index_config")
BLOCK_FIELDS = ("block_id", "doc_id", "block_index", "page", "section", "block_type", "text", "bbox", "source", "confidence")
ERRORS = {
    "EVIDENCE_INPUT": "知识库、文档或Chunk标识无效",
    "EVIDENCE_CITATION": "引用或回答契约无效",
    "EVIDENCE_UNAVAILABLE": "当前范围没有可反查的ACTIVE知识库/READY文档来源",
    "EVIDENCE_METADATA": "字符来源或页码语义缺失/无效，不猜测历史来源",
    "EVIDENCE_SOURCE": "Block归属、原文Hash、字符位置或页码与Chunk不一致",
    "EVIDENCE_STALE": "引用或切片检查点已失效，不把当前记录冒充旧回答来源",
    "EVIDENCE_LIMIT": "来源数量或正文超过只读反查预算",
    "EVIDENCE_SESSION": "来源反查需要独立无待写入变更的只读会话",
    "EVIDENCE_DATABASE": "来源数据库只读查询未完成",
}


class EvidenceError(Exception):
    """固定来源错误，禁止包含正文、SQL、路径、连接串或验证输入。"""

    def __init__(self, code: str):
        """未知类别安全归为来源错误，不透传上游异常。"""
        self.code = code if code in ERRORS else "EVIDENCE_SOURCE"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def _json_copy(value: dict) -> dict:
    """复制严格JSON证据，不共享调用方metadata或接受非有限数/非法Unicode。"""
    if not isinstance(value, dict):
        raise ValueError
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    encoded.encode("utf-8", errors="strict")
    return json.loads(encoded)


def make_citation(result: RetrievalResult, citation_id: int) -> Citation:
    """仅从项目检索结果生成稳定引用，复制原分数/正文/来源，不隐式查询数据库。"""
    try:
        if not isinstance(result, RetrievalResult):
            raise ValueError
        for identity in (result.kb_id, result.doc_id, result.chunk_id):
            _uuid(identity)
        _json_copy(result.metadata)
        value = _json_copy(result.model_dump(mode="json", exclude={"citation_id"}))
        citation = Citation(**value, citation_id=citation_id)
    except Exception:
        raise EvidenceError("EVIDENCE_CITATION") from None
    log_event("citation.created", kb_id=citation.kb_id, doc_id=citation.doc_id, chunk_id=citation.chunk_id)
    return citation


def citation_to_result(citation: Citation) -> RetrievalResult:
    """去除展示编号后还原原证据DTO，保留未知分数None，不依赖框架或重Embedding。"""
    try:
        if not isinstance(citation, Citation):
            raise ValueError
        checked = Citation.model_validate(_json_copy(citation.model_dump(mode="json")))
        return RetrievalResult.model_validate(checked.model_dump(exclude={"citation_id"}))
    except Exception:
        raise EvidenceError("EVIDENCE_CITATION") from None


def _uuid(value) -> UUID:
    """限定非零UUID，拒绝布尔值/其他对象；不把原输入写入错误。"""
    try:
        if not isinstance(value, (UUID, str)):
            raise ValueError
        identity = value if isinstance(value, UUID) else UUID(value)
        if identity.int == 0:
            raise ValueError
        return identity
    except Exception:
        raise EvidenceError("EVIDENCE_INPUT") from None


def _loaded(model, fields) -> dict:
    """只读已加载ORM字段；不隐式SELECT，也不导入框架Node Adapter获取快照。"""
    values = {field: inspect(model).attrs[field].loaded_value for field in fields}
    if any(value is NO_VALUE for value in values.values()):
        raise EvidenceError("EVIDENCE_SOURCE")
    return values


def evidence_statement(kb_id: UUID, chunk_id: UUID, doc_id: UUID | None = None):
    """范围、ACTIVE/READY和唯一Chunk均下推SQL，不读取原件路径/向量或扩大范围。"""
    filters = [DocumentChunk.kb_id == kb_id, Document.kb_id == kb_id, KnowledgeBase.kb_id == kb_id,
               DocumentChunk.chunk_id == chunk_id, KnowledgeBase.status == "ACTIVE", Document.status.in_(["READY", "PARTIAL_READY"])]
    if doc_id is not None:
        filters.append(Document.doc_id == doc_id)
    return (select(DocumentChunk, Document).join(Document,
        (DocumentChunk.doc_id == Document.doc_id) & (DocumentChunk.kb_id == Document.kb_id))
        .join(KnowledgeBase, KnowledgeBase.kb_id == Document.kb_id).where(*filters)
        .options(load_only(*(getattr(DocumentChunk, field) for field in CHUNK_FIELDS), raiseload=True),
                 load_only(*(getattr(Document, field) for field in DOCUMENT_FIELDS), raiseload=True))
        .limit(2).execution_options(populate_existing=True))


def readonly_evidence_session() -> Session:
    """仅显式反查时创建独立会话，构造服务/导入不连接数据库。"""
    return Session(get_engine(), autoflush=False, expire_on_commit=False)


def _source_metadata(chunk: dict, document: dict) -> tuple[dict, list[SourceSpan]]:
    """核对真实页语义/文件Hash/解析代次及冻结切片检查点，缺字段不从文件名猜测。"""
    try:
        metadata = _json_copy(chunk["chunk_metadata"])
        raw = metadata["source_spans"]
        if not isinstance(raw, list) or not raw:
            raise ValueError
        if len(raw) > MAX_SPANS or len(chunk["text"]) > MAX_SOURCE_CHARS:
            raise EvidenceError("EVIDENCE_LIMIT")
        expected = ("physical_pdf" if document["file_type"] == "pdf" else "image_page"
                    if document["file_type"] in {"jpg", "jpeg", "png"} else "logical")
        if metadata["page_semantics"] != expected:
            raise ValueError
        if (document["processing_task_id"] is None or document["chunk_task_id"] is None
                or metadata["source_parse_task_id"] != str(document["processing_task_id"])
                or metadata["file_hash"] != document["file_hash"]
                or not isinstance(document["chunk_config"], dict)
                or document["chunk_config"].get("source_parse_task_id") != str(document["processing_task_id"])):
            raise EvidenceError("EVIDENCE_STALE")
        spans = [SourceSpan.model_validate(value) for value in raw]
        if not any(span.role == "content" for span in spans):
            raise ValueError
        return metadata, spans
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError("EVIDENCE_METADATA") from None


def _trace(chunk: dict, document: dict, metadata: dict, spans: list[SourceSpan], blocks: list) -> EvidenceTrace:
    """逐字符/整Block Hash核对来源，保留重复context/overlap及原坐标，不拆表/重排。"""
    try:
        lookup = {}
        total = 0
        for block in blocks:
            value = _loaded(block, BLOCK_FIELDS)
            if any(s.block_id == value["block_id"] and s.provenance is not None for s in spans):
                value["provenance"] = _loaded(block, ("provenance",))["provenance"]
            if value["doc_id"] != document["doc_id"] or value["block_id"] in lookup:
                raise ValueError
            total += len(value["text"])
            if total > MAX_SOURCE_CHARS:
                raise EvidenceError("EVIDENCE_LIMIT")
            lookup[value["block_id"]] = value
        if set(lookup) != {span.block_id for span in spans}:
            raise ValueError
        hashes = {key: hashlib.sha256(value["text"].encode("utf-8")).hexdigest() for key, value in lookup.items()}
        resolved, parts, offset = [], [], 0
        for span in spans:
            block = lookup[span.block_id]
            if not span.page_reliable or span.provenance is not None and (
                    span.provenance != block.get("provenance") or not span.provenance.get("page_reliable")):
                raise ValueError
            if (any(getattr(span, field) != block[field] for field in ("block_index", "page", "block_type", "source", "bbox"))
                    or span.block_text_hash != hashes[span.block_id] or span.end_char > len(block["text"])
                    or span.chunk_end_char > len(chunk["text"])
                    or span.chunk_start_char != offset):
                raise ValueError
            piece = block["text"][span.start_char:span.end_char]
            if piece != chunk["text"][span.chunk_start_char:span.chunk_end_char]:
                raise ValueError
            if (metadata["page_semantics"] == "logical" and block["bbox"] is not None
                    or metadata["page_semantics"] == "image_page" and block["page"] != 1
                    or document["file_type"] in {"txt", "md", "csv"} and block["page"] != 1
                    or document["file_type"] in {"docx", "xlsx"} and block["source"] == "native_parser" and block["page"] != 1):
                raise ValueError
            resolved.append(ResolvedSourceSpan(**span.model_dump(), section=block["section"], text=piece,
                                               confidence=block["confidence"]))
            parts.append(piece)
            offset = span.chunk_end_char + 2
        # 当前Chunker仅用两个换行连接来源；不能把未映射正文当成可追溯证据。
        pages = [span.page for span in spans]
        if (chunk["text"] != "\n\n".join(parts) or chunk["page_start"] != min(pages)
                or chunk["page_end"] != max(pages)):
            raise ValueError
        return EvidenceTrace(kb_id=chunk["kb_id"], doc_id=chunk["doc_id"], chunk_id=chunk["chunk_id"],
            file_name=document["file_name"], file_type=document["file_type"], file_hash=document["file_hash"],
            source_type=document["source_type"], document_status=document["status"],
            source_parse_task_id=document["processing_task_id"], source_chunk_task_id=document["chunk_task_id"],
            text=chunk["text"], page_start=chunk["page_start"], page_end=chunk["page_end"], section=chunk["section"],
            page_semantics=metadata["page_semantics"], source_spans=resolved)
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError("EVIDENCE_SOURCE") from None


class EvidenceService:
    """显式反查当前持久化真相；整批在同一REPEATABLE READ只读快照内验证，不自动重试。"""

    def __init__(self, *, session_factory: Callable | None = None):
        """保存独立会话工厂；默认不取引擎、不查询、不读原件或运行模型。"""
        if session_factory is not None and not callable(session_factory):
            raise EvidenceError("EVIDENCE_SESSION")
        self._session_factory = session_factory if session_factory is not None else readonly_evidence_session

    def _load_trace(self, session, kb_id, chunk_id, doc_id, expected) -> EvidenceTrace:
        """读取完整源字段和指定Block，越界/缺失/已改写引用整体失败，不修补引用。"""
        rows = session.execute(evidence_statement(kb_id, chunk_id, doc_id)).all()
        if not rows:
            raise EvidenceError("EVIDENCE_UNAVAILABLE")
        if len(rows) != 1:
            raise EvidenceError("EVIDENCE_SOURCE")
        chunk, document = _loaded(rows[0][0], CHUNK_FIELDS), _loaded(rows[0][1], DOCUMENT_FIELDS)
        if (chunk["chunk_id"] != chunk_id or chunk["kb_id"] != kb_id or document["kb_id"] != kb_id
                or chunk["doc_id"] != document["doc_id"] or document["status"] not in {"READY", "PARTIAL_READY"}
                or doc_id is not None and document["doc_id"] != doc_id):
            raise EvidenceError("EVIDENCE_SOURCE")
        metadata, spans = _source_metadata(chunk, document)
        if expected is not None:
            if (any(getattr(expected, field) != chunk[field] for field in
                    ("chunk_id", "kb_id", "doc_id", "text", "page_start", "page_end", "section"))
                    or expected.file_name != document["file_name"] or expected.file_type != document["file_type"]
                    or _json_copy(expected.metadata) != metadata):
                raise EvidenceError("EVIDENCE_STALE")
            if expected.embedding_identity is not None:
                actual = (document["index_config"] or {}).get("result", {}).get("model")
                if actual != expected.embedding_identity:
                    raise EvidenceError("EVIDENCE_STALE")
        ids = {span.block_id for span in spans}
        statement = (select(DocumentBlock).where(DocumentBlock.doc_id == document["doc_id"], DocumentBlock.block_id.in_(ids))
            .options(load_only(*(getattr(DocumentBlock, field) for field in (*BLOCK_FIELDS, "provenance")), raiseload=True))
            .order_by(DocumentBlock.block_index, DocumentBlock.block_id).limit(MAX_SPANS + 1))
        blocks = session.execute(statement).scalars().all()
        return _trace(chunk, document, metadata, spans, blocks)

    def _resolve_batch(self, requests: list[tuple]) -> list[EvidenceTrace]:
        """独立快照里查询所有引用，失败全批拒绝；只读事务关闭即回滚，不flush/commit。"""
        started, stage = perf_counter(), "database"
        scope = {"kb_id": requests[0][0], "chunk_id": requests[0][1] if len(requests) == 1 else None,
                 "doc_id": requests[0][2] if len(requests) == 1 else None}
        try:
            log_event("evidence.started", **scope, item_count=len(requests))
            with self._session_factory() as session:
                if session.new or session.dirty or session.deleted or session.in_transaction():
                    raise EvidenceError("EVIDENCE_SESSION")
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                stage = "source"
                with session.no_autoflush:
                    traces = [self._load_trace(session, *request) for request in requests]
            log_event("evidence.finished", **scope, item_count=len(traces),
                      block_count=len({(trace.doc_id, span.block_id) for trace in traces for span in trace.source_spans}),
                      duration_ms=(perf_counter() - started) * 1000)
            return traces
        except Exception as exc:
            error = exc if isinstance(exc, EvidenceError) else EvidenceError(
                "EVIDENCE_DATABASE" if stage == "database" or isinstance(exc, SQLAlchemyError) else "EVIDENCE_SOURCE")
            log_event("evidence.failed", level=logging.WARNING, **scope, stage=stage, error_code=error.code,
                      item_count=len(requests), duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    def resolve(self, kb_id: UUID | str, chunk_id: UUID | str, *, doc_id: UUID | str | None = None) -> EvidenceTrace:
        """只反查指定范围的当前Chunk，无相关行返回安全失败，不扩库或读取原件补证据。"""
        return self._resolve_batch([(_uuid(kb_id), _uuid(chunk_id), _uuid(doc_id) if doc_id is not None else None, None)])[0]

    def resolve_citation(self, citation: Citation) -> EvidenceTrace:
        """核对现存引用与当前数据库快照一致；旧文本/来源代次不能自动绑到新记录。"""
        expected = citation_to_result(citation)
        return self._resolve_batch([(_uuid(expected.kb_id), _uuid(expected.chunk_id), _uuid(expected.doc_id), expected)])[0]

    def resolve_answer(self, answer: RAGAnswer) -> list[EvidenceTrace]:
        """对答案所有引用同快照核对，任一失效整批失败；不足答案不打开数据库会话。"""
        try:
            if not isinstance(answer, RAGAnswer):
                raise ValueError
            checked = RAGAnswer.model_validate(_json_copy(answer.model_dump(mode="json")))
        except Exception:
            raise EvidenceError("EVIDENCE_CITATION") from None
        if checked.insufficient_evidence:
            log_event("evidence.skipped", item_count=0)
            return []
        expected = [citation_to_result(cite) for cite in checked.citations]
        return self._resolve_batch([(_uuid(item.kb_id), _uuid(item.chunk_id), _uuid(item.doc_id), item) for item in expected])
