"""将当前 READY Chunk 派生的本地 BM25 接入 LlamaIndex；不调用模型或建立框架真相表。"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import closing
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
    corpus_fingerprint, iter_sources, select_sources, tokenize, CorpusLimits, checked_limits,
)
from app.services.rag.llama_index.dense_retriever import readonly_session
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result


def corpus_item(chunk, document):
    """沿用完整Adapter校验后只留下ID/原文，临时Node及完整证据随本行释放。"""
    node = chunk_to_node(chunk, document, include_embedding=False, log_success=False)
    return UUID(node.node_id), node.text


class BM25Retriever(BaseRetriever):
    """与 Dense 同形的只读关键词边界，最多保留一个实例内可重建词频索引。"""

    def __init__(self, kb_id: UUID | str, *,
                 doc_ids: list[UUID | str] | tuple[UUID | str, ...] | None = None,
                 top_k: int = DEFAULT_TOP_K, session_factory: Callable | None = None,
                 corpus_limits: CorpusLimits | None = None):
        """冻结原范围及可选实例预算；默认生产上限不变，构造不读库或加载模型。"""
        checked_limits(corpus_limits)
        self._corpus_limits = corpus_limits
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
        """分批验证整库并在同一只读快照取回Top-K证据，不同时保留全库ORM与Node。"""
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
                # 全库评分与命中证据属于同一查询快照，避免两次SELECT间发生代次变化。
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                budget_args = {"limits": self._corpus_limits} if self._corpus_limits is not None else {}
                stage = "node_adapter"
                with closing(iter_sources(session, kb_id, doc_ids, **budget_args)) as rows:
                    items = [corpus_item(chunk, document) for chunk, document in rows]
                chunk_count = len(items)
                stage = "index"
                key = corpus_fingerprint(items, **budget_args)
                # 缓存只含词频/ID；失效时先释放旧索引，避免重建期间同时驻留两份词频。
                with self._index_lock:
                    reused = (self._index is not None and self._index.fingerprint == key
                              and self._index_scope == (kb_id, doc_ids))
                    if not reused:
                        self._index, self._index_scope = None, None
                        self._index = LocalBM25Index(items, **budget_args)
                        self._index_scope = (kb_id, doc_ids)
                    index = self._index
                log_event("bm25.index_reused" if reused else "bm25.index_built", **scope_log,
                          chunk_count=chunk_count, artifact_reused=reused)
                stage = "score"
                ranked = index.search(query, self._top_k)
                expected = {items[position][0]: items[position][1] for position, _ in ranked}
                del items
                nodes = {}
                if ranked:
                    stage = "node_adapter"
                    for chunk, document in select_sources(session, kb_id, doc_ids,
                            chunk_ids=list(expected), **budget_args):
                        if chunk.text != expected[chunk.chunk_id]:
                            raise BM25RetrievalError("BM25_RESULT")
                        nodes[chunk.chunk_id] = chunk_to_node(chunk, document,
                            include_embedding=False, log_success=False)
                    if nodes.keys() != expected.keys():
                        raise BM25RetrievalError("BM25_RESULT")
                result = [NodeWithScore(node=nodes[index.ids[position]], score=score)
                          for position, score in ranked]
            log_event("bm25.finished" if result else "bm25.empty", **scope_log,
                      chunk_count=chunk_count, item_count=len(result), artifact_reused=reused,
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
