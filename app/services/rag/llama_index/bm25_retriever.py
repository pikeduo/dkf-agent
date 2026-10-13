"""将当前 READY Chunk 派生的本地 BM25 接入 LlamaIndex；不调用模型或建立框架真相表。"""

import asyncio
import logging
from collections.abc import Callable
from threading import Lock
from time import perf_counter
from uuid import UUID

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.callbacks import CallbackManager
from llama_index.core.schema import NodeWithScore, QueryBundle
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.business_logging import log_event
from app.schemas.retrieval import RetrievalResult
from app.services.rag.bm25 import (
    DEFAULT_TOP_K, MAX_QUERY_TERMS, BM25RetrievalError, LocalBM25Index, bm25_query, bm25_scope,
    corpus_fingerprint, select_sources, tokenize,
)
from app.services.rag.llama_index.dense_retriever import readonly_session
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result


class BM25Retriever(BaseRetriever):
    """与 Dense 同形的只读关键词边界，最多保留一个实例内可重建词频索引。"""

    def __init__(self, kb_id: UUID | str, *,
                 doc_ids: list[UUID | str] | tuple[UUID | str, ...] | None = None,
                 top_k: int = DEFAULT_TOP_K, session_factory: Callable | None = None):
        """仅冻结范围和会话工厂；不读库、构建索引、加载 BGE-M3 或连接外部服务。"""
        self._kb_id, self._doc_ids = bm25_scope(kb_id, top_k, doc_ids)
        self._top_k = top_k
        if session_factory is not None and not callable(session_factory):
            raise BM25RetrievalError("BM25_INPUT")
        super().__init__(callback_manager=CallbackManager([]), object_map={}, verbose=False)
        self._session_factory = session_factory if session_factory is not None else readonly_session
        self._index = None
        self._index_scope = None
        self._index_lock = Lock()

    @property
    def kb_id(self) -> UUID:
        """返回固定知识库；查询不能临时覆盖业务隔离范围。"""
        return self._kb_id

    @property
    def doc_ids(self) -> tuple[UUID, ...] | None:
        """返回冻结文档元组；None 表示库内所有合格文档，空范围不放宽。"""
        return self._doc_ids

    @property
    def top_k(self) -> int:
        """返回经过校验的固定检索上限。"""
        return self._top_k

    def retrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """避开框架含原文 callback/递归 Query Engine，统一走受保护的当前快照检索。"""
        return self._retrieve(str_or_query_bundle)

    async def aretrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """同进程执行，线程内新建只读 Session；索引发布由实例锁保护，无新 Worker。"""
        return await asyncio.to_thread(self.retrieve, str_or_query_bundle)

    def _retrieve(self, query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """每次读取 READY 快照/转换原 Node，再复用或重建词频索引；来源失败不返回旧证据。"""
        started, stage = perf_counter(), "input"
        scope_log = {"kb_id": self._kb_id, "top_k": self._top_k}
        try:
            kb_id, doc_ids = bm25_scope(self._kb_id, self._top_k, self._doc_ids)
            scope_log.update(doc_id=doc_ids[0] if doc_ids and len(doc_ids) == 1 else None,
                             source_document_count=len(doc_ids) if doc_ids is not None else None)
            if isinstance(query_bundle, QueryBundle):
                if (query_bundle.embedding is not None or query_bundle.custom_embedding_strs is not None
                        or query_bundle.image_path is not None):
                    raise BM25RetrievalError("BM25_INPUT")
                query = bm25_query(query_bundle.query_str)
            else:
                query = bm25_query(query_bundle)
            # 词项超限在来源查询/建索引之前拒绝；评分层仍独立校验以保护直接调用。
            tokenize(query, max_terms=MAX_QUERY_TERMS)
            log_event("bm25.started", **scope_log)
            stage = "database"
            with self._session_factory() as session:
                if session.new or session.dirty or session.deleted or session.in_transaction():
                    raise BM25RetrievalError("BM25_SESSION")
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                rows = select_sources(session, kb_id, doc_ids)
                stage = "node_adapter"
                nodes = [chunk_to_node(chunk, document, include_embedding=False, log_success=False)
                         for chunk, document in rows]
            items = [(UUID(node.node_id), node.text) for node in nodes]
            stage = "index"
            key = corpus_fingerprint(items)
            # 缓存只含词频/ID，返回 Node 永远来自本次 SQL；新元数据不会被旧缓存覆盖。
            with self._index_lock:
                reused = (self._index is not None and self._index.fingerprint == key
                          and self._index_scope == (kb_id, doc_ids))
                if not reused:
                    index = LocalBM25Index(items)
                    self._index, self._index_scope = index, (kb_id, doc_ids)
                index = self._index
            log_event("bm25.index_reused" if reused else "bm25.index_built", **scope_log,
                      chunk_count=len(nodes), artifact_reused=reused)
            stage = "score"
            ranked = index.search(query, self._top_k)
            result = [NodeWithScore(node=nodes[position], score=score) for position, score in ranked]
            log_event("bm25.finished" if result else "bm25.empty", **scope_log,
                      chunk_count=len(nodes), item_count=len(result), artifact_reused=reused,
                      duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            if isinstance(exc, BM25RetrievalError):
                error = exc
            elif isinstance(exc, SQLAlchemyError) or stage == "database":
                error = BM25RetrievalError("BM25_DATABASE")
            else:
                error = BM25RetrievalError("BM25_RESULT" if stage in {"node_adapter", "score"} else "BM25_INDEX")
            log_event("bm25.failed", level=logging.WARNING, **scope_log, stage=stage,
                      error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """满足框架私有异步契约，仍走相同过滤、当前快照及脱敏入口。"""
        return await self.aretrieve(query_bundle)

    def retrieve_results(self, query: str | QueryBundle) -> list[RetrievalResult]:
        """复用已有 Node→项目证据DTO，不生成回答、随机标识或新的数据库记录。"""
        return [node_to_result(node) for node in self.retrieve(query)]
