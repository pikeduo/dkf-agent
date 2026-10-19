"""READY Chunk 的只读来源与可重建 BM25 派生索引；不读取原件或保存第二套业务数据。"""

import hashlib
import math
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from heapq import nsmallest
from uuid import UUID

from sqlalchemy import String, Text, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only

from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.indexing import INDEX_VERSION
from app.services.rag.database import DATABASE_REASONS, database_diagnostic, database_error_codes
from app.services.rag.dense import (
    DEFAULT_TOP_K, DenseRetrievalError, validate_doc_ids, validate_query, validate_scope,
)

BM25_VERSION = "rank-bm25-plus-v1"
TOKENIZER_VERSION = "keyword-cjk-v1"
MAX_CORPUS_CHUNKS = 10_000
MAX_CORPUS_BYTES = 32 * 1024 * 1024
MAX_CORPUS_TOKENS = 4_000_000
MAX_QUERY_TERMS = 512
SOURCE_BATCH_SIZE = 256
# 必须覆盖完整Adapter输入及当前代次；测试核对字段集合，不能仅凭任务ID复用校验。
SOURCE_CHUNK_FIELDS = ("chunk_id", "kb_id", "doc_id", "text", "page_start", "page_end", "section", "chunk_metadata")
SOURCE_DOCUMENT_FIELDS = ("doc_id", "kb_id", "file_name", "file_type", "index_config", "status",
    "index_task_id", "processing_task_id", "chunk_task_id", "embedding_task_id")
TOKEN_PATTERN = re.compile(
    r"[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)%?(?![a-z0-9_]|[-./+][a-z0-9])"
    r"|[a-z0-9]+(?:[-_./+][a-z0-9]+)*%?"
    r"|[\u3400-\u4dbf\u4e00-\u9fff]+"
)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ERRORS = {
    **database_error_codes("BM25"),
    "BM25_INPUT": "知识库、文档范围、Query 或 Top-K 参数无效",
    "BM25_LIMIT": "BM25 输入或候选语料超过安全上限，未截断为部分检索",
    "BM25_SESSION": "BM25 仅接受独立无待写变更的只读会话",
    "BM25_DATABASE": "BM25 来源查询未完成，请检查现有 PostgreSQL",
    "BM25_RESULT": "BM25 来源身份、证据或评分无效，未返回部分结果",
    "BM25_DEPENDENCY": "缺少已声明的 rank-bm25 依赖，请检查 dkf-agent 环境",
    "BM25_INDEX": "BM25 派生索引构建未完成，未使用旧索引冒充当前结果",
    "BM25_FAILED": "BM25 检索未完成，请查看安全错误码",
}


class BM25RetrievalError(Exception):
    """只传递固定错误，不暴露正文、Query、SQL、连接串或底层异常。"""

    def __init__(self, code: str, *, database_error=None):
        """首次捕获数据库错误时保留安全SQLSTATE分类，不传递原SQL或消息。"""
        self.database_diagnostic = database_diagnostic(database_error) if code == "BM25_DATABASE" else None
        if self.database_diagnostic and self.database_diagnostic["reason"] in DATABASE_REASONS:
            code += "_" + self.database_diagnostic["reason"]
        self.code = code if code in ERRORS else "BM25_FAILED"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class CorpusLimits:
    """实例级语料预算；生产默认不变，离线全量评测可显式放大但不得截断。"""

    chunks: int = MAX_CORPUS_CHUNKS
    bytes: int = MAX_CORPUS_BYTES
    tokens: int = MAX_CORPUS_TOKENS

    def __post_init__(self):
        """拒绝布尔/无限预算，上限20万Chunk、512MiB原文和6400万词项。"""
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in (
                (self.chunks, 200_000), (self.bytes, 512 * 1024**2), (self.tokens, 64_000_000))):
            raise BM25RetrievalError("BM25_LIMIT")


def checked_limits(limits=None):
    """保留原默认及测试常量覆盖，显式预算必须为受检不可变类型。"""
    if limits is None:
        return CorpusLimits(MAX_CORPUS_CHUNKS, MAX_CORPUS_BYTES, MAX_CORPUS_TOKENS)
    if not isinstance(limits, CorpusLimits):
        raise BM25RetrievalError("BM25_INPUT")
    return limits


def bm25_scope(kb_id, top_k=DEFAULT_TOP_K, doc_ids=None):
    """复用阶段18的 UUID/Top-K/文档范围校验，返回冻结范围，不调用模型或数据库。"""
    try:
        return validate_scope(kb_id, top_k), validate_doc_ids(doc_ids)
    except DenseRetrievalError as exc:
        raise BM25RetrievalError("BM25_LIMIT" if exc.code == "DENSE_LIMIT" else "BM25_INPUT") from None


def bm25_query(value):
    """校验原 Query 而不改写文本；分词归一化仅用于派生索引，不改来源证据。"""
    try:
        return validate_query(value)
    except DenseRetrievalError as exc:
        raise BM25RetrievalError("BM25_LIMIT" if exc.code == "DENSE_LIMIT" else "BM25_INPUT") from None


def tokenize(text: str, *, max_terms=MAX_CORPUS_TOKENS) -> list[str]:
    """保留数字/型号完整词，中文使用字符/二元片段；扩展前检查词项预算，不截断语料。"""
    if not isinstance(text, str):
        raise BM25RetrievalError("BM25_INPUT")
    tokens = []
    for match in TOKEN_PATTERN.finditer(unicodedata.normalize("NFKC", text).casefold()):
        word = match.group()
        chinese = CJK_PATTERN.fullmatch(word) is not None
        if len(tokens) + (2 * len(word) - 1 if chinese else 1) > max_terms:
            raise BM25RetrievalError("BM25_LIMIT")
        if chinese:
            tokens.extend(word)
            tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            # 只去数字词中的千分位逗号，不转换数值、公式或业务正文。
            tokens.append(word.replace(",", ""))
    return tokens


def source_statement(kb_id, doc_ids=None, *, limits=None, chunk_ids=None, snapshot=False):
    """同一SQL范围可投影完整证据或逐行SHA256快照，预算+1检测超限而不截断。"""
    if type(snapshot) is not bool:
        raise BM25RetrievalError("BM25_INPUT")
    budget = checked_limits(limits)
    kb_id, doc_ids = bm25_scope(kb_id, doc_ids=doc_ids)
    config = Document.index_config
    parse_id = cast(Document.processing_task_id, String)
    filters = [DocumentChunk.kb_id == kb_id, Document.kb_id == kb_id, KnowledgeBase.kb_id == kb_id,
        KnowledgeBase.status == "ACTIVE", Document.status.in_(["READY", "PARTIAL_READY"]), Document.index_task_id.is_not(None),
        config["version"].as_string() == INDEX_VERSION,
        config["source_parse_task_id"].as_string() == parse_id,
        config["source_chunk_task_id"].as_string() == cast(Document.chunk_task_id, String),
        config["embedding_task_id"].as_string() == cast(Document.embedding_task_id, String),
        DocumentChunk.chunk_metadata["source_parse_task_id"].as_string() == parse_id]
    if doc_ids is not None:
        filters.append(DocumentChunk.doc_id.in_(doc_ids))
    if chunk_ids is not None:
        if (not isinstance(chunk_ids, (list, tuple)) or not 1 <= len(chunk_ids) <= 100
                or any(not isinstance(value, UUID) for value in chunk_ids)
                or len(set(chunk_ids)) != len(chunk_ids)):
            raise BM25RetrievalError("BM25_INPUT")
        filters.append(DocumentChunk.chunk_id.in_(chunk_ids))
    if snapshot:
        # PostgreSQL内置SHA256不需扩展。正文、完整元数据、页码和身份均参与指纹；
        # 热查询只传字节数和摘要，避免解码全库JSON/反复构造ORM与Node。
        fields = [getattr(DocumentChunk, field) for field in SOURCE_CHUNK_FIELDS if field != "text"]
        fields += [getattr(Document, field) for field in SOURCE_DOCUMENT_FIELDS]
        statement = select(DocumentChunk.chunk_id, func.octet_length(DocumentChunk.text).label("text_bytes"),
            func.sha256(func.convert_to(DocumentChunk.text, "UTF8")).label("text_hash"),
            func.sha256(func.convert_to(cast(func.jsonb_build_array(*fields), Text), "UTF8")).label("source_hash"))
    else:
        statement = select(DocumentChunk, Document).options(
            load_only(*(getattr(DocumentChunk, field) for field in SOURCE_CHUNK_FIELDS), raiseload=True),
            load_only(*(getattr(Document, field) for field in SOURCE_DOCUMENT_FIELDS), raiseload=True))
    return (statement.select_from(DocumentChunk)
        .join(Document, (DocumentChunk.doc_id == Document.doc_id) & (DocumentChunk.kb_id == Document.kb_id))
        .join(KnowledgeBase, KnowledgeBase.kb_id == Document.kb_id).where(*filters)
        .order_by(DocumentChunk.chunk_id.asc()).limit(budget.chunks + 1)
        .execution_options(populate_existing=True))


@contextmanager
def source_cursor(session, statement):
    """统一分批读取和关闭；首次SQLSTATE优先，关闭失败不得覆盖正在传播的根因。"""
    if session.new or session.dirty or session.deleted:
        raise BM25RetrievalError("BM25_SESSION")
    result, failing = None, False
    try:
        with session.no_autoflush:
            result = session.execute(statement.execution_options(yield_per=SOURCE_BATCH_SIZE))
            yield result
    except SQLAlchemyError as exc:
        failing = True
        raise BM25RetrievalError("BM25_DATABASE", database_error=exc) from None
    except BaseException:
        # 包括调用方提前关闭生成器；清理错误不能替换消费阶段已经发生的异常。
        failing = True
        raise
    finally:
        if result is not None:
            try:
                result.close()
            except SQLAlchemyError as exc:
                if not failing:
                    raise BM25RetrievalError("BM25_DATABASE", database_error=exc) from None


def read_source_snapshot(session, kb_id, doc_ids=None, *, limits=None):
    """分批核对全库来源/正文摘要，仅保留ID与正文Hash；返回指纹用于同实例验证复用。"""
    budget = checked_limits(limits)
    digest = hashlib.sha256(("bm25-source-snapshot-v1:" + BM25_VERSION + ":" + TOKENIZER_VERSION).encode("ascii"))
    items, total, last = [], 0, None
    statement = source_statement(kb_id, doc_ids, limits=limits, snapshot=True)
    with source_cursor(session, statement) as rows:
        for count, row in enumerate(rows, 1):
            if count > budget.chunks:
                raise BM25RetrievalError("BM25_LIMIT")
            try:
                chunk_id, text_bytes, text_hash, source_hash = row
                if (not isinstance(chunk_id, UUID) or chunk_id.int == 0
                        or (last is not None and chunk_id.int <= last)
                        or type(text_bytes) is not int or text_bytes < 1
                        or not isinstance(text_hash, (bytes, memoryview)) or len(text_hash) != 32
                        or not isinstance(source_hash, (bytes, memoryview)) or len(source_hash) != 32):
                    raise ValueError
                text_hash, source_hash = bytes(text_hash), bytes(source_hash)
            except (ValueError, TypeError):
                raise BM25RetrievalError("BM25_RESULT") from None
            total += text_bytes
            if total > budget.bytes:
                raise BM25RetrievalError("BM25_LIMIT")
            digest.update(chunk_id.bytes + text_bytes.to_bytes(8, "big") + text_hash + source_hash)
            items.append((chunk_id, text_hash))
            last = chunk_id.int
    return digest.hexdigest(), items


def iter_sources(session, kb_id, doc_ids=None, *, limits=None, chunk_ids=None):
    """分批读取并逐行验证来源；游标始终关闭，调用方须消费完毕才能发布完整结果。"""
    budget = checked_limits(limits)
    kb_id, doc_ids = bm25_scope(kb_id, doc_ids=doc_ids)
    statement = source_statement(kb_id, doc_ids, limits=limits, chunk_ids=chunk_ids)
    with source_cursor(session, statement) as result:
        total, last = 0, None
        for count, row in enumerate(result, 1):
            if count > budget.chunks:
                raise BM25RetrievalError("BM25_LIMIT")
            try:
                chunk, document = row
                config = document.index_config
                if (chunk.kb_id != kb_id or document.kb_id != kb_id or chunk.doc_id != document.doc_id
                        or (doc_ids is not None and chunk.doc_id not in doc_ids)
                        or (chunk_ids is not None and chunk.chunk_id not in chunk_ids)
                        or document.status not in {"READY", "PARTIAL_READY"} or document.index_task_id is None
                        or not isinstance(config, dict) or config.get("version") != INDEX_VERSION
                        or any(config.get(key) != str(getattr(document, attribute)) for key, attribute in (
                            ("source_parse_task_id", "processing_task_id"), ("source_chunk_task_id", "chunk_task_id"),
                            ("embedding_task_id", "embedding_task_id")))
                        or chunk.chunk_metadata.get("source_parse_task_id") != str(document.processing_task_id)
                        or not isinstance(chunk.chunk_id, UUID) or (last is not None and chunk.chunk_id.int <= last)
                        or not isinstance(chunk.text, str) or not chunk.text.strip()):
                    raise ValueError
                total += len(chunk.text.encode("utf-8"))
                last = chunk.chunk_id.int
            except Exception:
                raise BM25RetrievalError("BM25_RESULT") from None
            if total > budget.bytes:
                raise BM25RetrievalError("BM25_LIMIT")
            yield row


def select_sources(session, kb_id, doc_ids=None, *, limits=None, chunk_ids=None):
    """兼容原列表入口；Retriever仅用它读取Top-K证据，全库扫描使用分批迭代器。"""
    return list(iter_sources(session, kb_id, doc_ids, limits=limits, chunk_ids=chunk_ids))


def corpus_fingerprint(items: list[tuple[UUID, str]], *, limits=None) -> str:
    """原稳定身份不变，按实例预算检查全部语料；预算不改变分词或评分。"""
    budget = checked_limits(limits)
    if len(items) > budget.chunks:
        raise BM25RetrievalError("BM25_LIMIT")
    digest = hashlib.sha256((BM25_VERSION + TOKENIZER_VERSION).encode("ascii"))
    total, last = 0, None
    for chunk_id, text in items:
        if (not isinstance(chunk_id, UUID) or (last is not None and chunk_id.int <= last)
                or not isinstance(text, str) or not text.strip()):
            raise BM25RetrievalError("BM25_RESULT")
        data = text.encode("utf-8")
        total += len(data)
        if total > budget.bytes:
            raise BM25RetrievalError("BM25_LIMIT")
        digest.update(chunk_id.bytes + len(data).to_bytes(8, "big") + data)
        last = chunk_id.int
    return digest.hexdigest()


class LocalBM25Index:
    """仅保存词频和稳定 ID 的进程内派生索引，不保存 Node、业务记录或原件路径。"""

    def __init__(self, items: list[tuple[UUID, str]], *, limits=None):
        """逐块分词并共享相同词项字符串，保留原BM25Plus评分且不物化全库词项列表。"""
        budget = checked_limits(limits)
        self.fingerprint = corpus_fingerprint(items, **({"limits": limits} if limits is not None else {}))
        self.ids = tuple(item[0] for item in items)
        self._engine = None
        if items:
            try:
                from rank_bm25 import BM25Plus
            except ImportError:
                raise BM25RetrievalError("BM25_DEPENDENCY") from None

            def corpus():
                """按原序产出一块词项；词表只在本次构建共享，不使用全局永久缓存。"""
                vocabulary, token_count = {}, 0
                for _, text in items:
                    terms = tokenize(text, max_terms=budget.tokens - token_count)
                    token_count += len(terms)
                    for position, term in enumerate(terms):
                        terms[position] = vocabulary.setdefault(term, term)
                    yield terms

            # 不传 tokenizer：rank-bm25 的 tokenizer 回调会创建 multiprocessing Pool。
            engine = BM25Plus(corpus(), k1=1.5, b=0.75, delta=1)
            if any(engine.doc_len):
                self._engine = engine

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """仅对真实词项重叠的Chunk评分，以有界Top-K保持降序/UUID规则，不复制全量排序表。"""
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise BM25RetrievalError("BM25_INPUT")
        terms = list(dict.fromkeys(tokenize(bm25_query(query), max_terms=MAX_QUERY_TERMS)))
        if len(terms) > MAX_QUERY_TERMS:
            raise BM25RetrievalError("BM25_LIMIT")
        if self._engine is None or not terms:
            return []
        query_terms = set(terms)
        matches = [index for index, frequencies in enumerate(self._engine.doc_freqs)
                   if not query_terms.isdisjoint(frequencies)]
        if not matches:
            return []
        scores = self._engine.get_batch_scores(terms, matches)
        if len(scores) != len(matches) or any(not math.isfinite(score) or score <= 0 for score in scores):
            raise BM25RetrievalError("BM25_RESULT")
        return nsmallest(top_k, zip(matches, scores), key=lambda hit: (-hit[1], self.ids[hit[0]].int))
