"""READY Chunk 的只读来源与可重建 BM25 派生索引；不读取原件或保存第二套业务数据。"""

import hashlib
import math
import re
import unicodedata
from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only

from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.indexing import INDEX_VERSION
from app.services.rag.dense import (
    DEFAULT_TOP_K, DenseRetrievalError, validate_doc_ids, validate_query, validate_scope,
)

BM25_VERSION = "rank-bm25-plus-v1"
TOKENIZER_VERSION = "keyword-cjk-v1"
MAX_CORPUS_CHUNKS = 10_000
MAX_CORPUS_BYTES = 32 * 1024 * 1024
MAX_CORPUS_TOKENS = 4_000_000
MAX_QUERY_TERMS = 512
TOKEN_PATTERN = re.compile(
    r"[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)%?(?![a-z0-9_]|[-./+][a-z0-9])"
    r"|[a-z0-9]+(?:[-_./+][a-z0-9]+)*%?"
    r"|[\u3400-\u4dbf\u4e00-\u9fff]+"
)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ERRORS = {
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

    def __init__(self, code: str):
        """将未知类别归为通用错误，禁止把上游异常原文充当错误代码。"""
        self.code = code if code in ERRORS else "BM25_FAILED"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


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


def source_statement(kb_id, doc_ids=None):
    """在 SQL 中限定 ACTIVE 库、READY 文档及当前代次，读取文本/证据，不读取向量或原件。"""
    kb_id, doc_ids = bm25_scope(kb_id, doc_ids=doc_ids)
    config = Document.index_config
    parse_id = cast(Document.processing_task_id, String)
    filters = [DocumentChunk.kb_id == kb_id, Document.kb_id == kb_id, KnowledgeBase.kb_id == kb_id,
        KnowledgeBase.status == "ACTIVE", Document.status == "READY", Document.index_task_id.is_not(None),
        config["version"].as_string() == INDEX_VERSION,
        config["source_parse_task_id"].as_string() == parse_id,
        config["source_chunk_task_id"].as_string() == cast(Document.chunk_task_id, String),
        config["embedding_task_id"].as_string() == cast(Document.embedding_task_id, String),
        DocumentChunk.chunk_metadata["source_parse_task_id"].as_string() == parse_id]
    if doc_ids is not None:
        filters.append(DocumentChunk.doc_id.in_(doc_ids))
    return (select(DocumentChunk, Document).select_from(DocumentChunk)
        .join(Document, (DocumentChunk.doc_id == Document.doc_id) & (DocumentChunk.kb_id == Document.kb_id))
        .join(KnowledgeBase, KnowledgeBase.kb_id == Document.kb_id).where(*filters)
        .options(load_only(DocumentChunk.chunk_id, DocumentChunk.doc_id, DocumentChunk.kb_id,
                          DocumentChunk.text, DocumentChunk.page_start, DocumentChunk.page_end,
                          DocumentChunk.section, DocumentChunk.chunk_metadata, raiseload=True),
                 load_only(Document.doc_id, Document.kb_id, Document.file_name, Document.file_type,
                           Document.status, Document.index_config, Document.index_task_id,
                           Document.processing_task_id, Document.chunk_task_id, Document.embedding_task_id, raiseload=True))
        .order_by(DocumentChunk.chunk_id.asc()).limit(MAX_CORPUS_CHUNKS + 1)
        .execution_options(populate_existing=True))


def select_sources(session, kb_id, doc_ids=None):
    """只读获取当前语料并检查来源；超限/越界全批失败，不使用旧缓存或事后删行。"""
    kb_id, doc_ids = bm25_scope(kb_id, doc_ids=doc_ids)
    if session.new or session.dirty or session.deleted:
        raise BM25RetrievalError("BM25_SESSION")
    try:
        with session.no_autoflush:
            rows = session.execute(source_statement(kb_id, doc_ids)).all()
    except SQLAlchemyError:
        raise BM25RetrievalError("BM25_DATABASE") from None
    if len(rows) > MAX_CORPUS_CHUNKS:
        raise BM25RetrievalError("BM25_LIMIT")
    total, last = 0, None
    for row in rows:
        try:
            chunk, document = row
            config = document.index_config
            if (chunk.kb_id != kb_id or document.kb_id != kb_id or chunk.doc_id != document.doc_id
                    or (doc_ids is not None and chunk.doc_id not in doc_ids)
                    or document.status != "READY" or document.index_task_id is None
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
        if total > MAX_CORPUS_BYTES:
            raise BM25RetrievalError("BM25_LIMIT")
    return rows


def corpus_fingerprint(items: list[tuple[UUID, str]]) -> str:
    """按稳定 Chunk ID/原文/算法版本计算派生身份；不把摘要或正文写入日志/持久化。"""
    if len(items) > MAX_CORPUS_CHUNKS:
        raise BM25RetrievalError("BM25_LIMIT")
    digest = hashlib.sha256((BM25_VERSION + TOKENIZER_VERSION).encode("ascii"))
    total, last = 0, None
    for chunk_id, text in items:
        if (not isinstance(chunk_id, UUID) or (last is not None and chunk_id.int <= last)
                or not isinstance(text, str) or not text.strip()):
            raise BM25RetrievalError("BM25_RESULT")
        data = text.encode("utf-8")
        total += len(data)
        if total > MAX_CORPUS_BYTES:
            raise BM25RetrievalError("BM25_LIMIT")
        digest.update(chunk_id.bytes + len(data).to_bytes(8, "big") + data)
        last = chunk_id.int
    return digest.hexdigest()


class LocalBM25Index:
    """仅保存词频和稳定 ID 的进程内派生索引，不保存 Node、业务记录或原件路径。"""

    def __init__(self, items: list[tuple[UUID, str]]):
        """构建 BM25Plus，避免单 Chunk/高频词的负 IDF；手动分词避免依赖启动进程池。"""
        self.fingerprint = corpus_fingerprint(items)
        self.ids = tuple(item[0] for item in items)
        corpus, token_count = [], 0
        for _, text in items:
            terms = tokenize(text, max_terms=MAX_CORPUS_TOKENS - token_count)
            token_count += len(terms)
            if token_count > MAX_CORPUS_TOKENS:
                raise BM25RetrievalError("BM25_LIMIT")
            corpus.append(terms)
        self._engine = None
        if token_count:
            try:
                from rank_bm25 import BM25Plus
            except ImportError:
                raise BM25RetrievalError("BM25_DEPENDENCY") from None
            # 不传 tokenizer：rank-bm25 的 tokenizer 回调会创建 multiprocessing Pool。
            self._engine = BM25Plus(corpus, k1=1.5, b=0.75, delta=1)

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """仅对真实词项重叠的 Chunk 评分，降序/UUID稳定排序；无命中不返回背景基线分。"""
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
        return sorted(zip(matches, scores), key=lambda hit: (-hit[1], self.ids[hit[0]].int))[:top_k]
