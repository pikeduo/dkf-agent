"""LlamaIndex Dense Retriever，只读检索已有 pgvector；不构建 Query Engine 或 RAG API。"""

import asyncio
import logging
from collections.abc import Callable
from time import perf_counter
from uuid import UUID

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.callbacks import CallbackManager
from llama_index.core.schema import NodeWithScore, QueryBundle
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.db.session import get_engine
from app.schemas.retrieval import RetrievalResult
from app.services.embedding.service import EmbeddingError
from app.services.rag.dense import (
    DEFAULT_TOP_K, DenseRetrievalError, model_snapshot, select_dense_hits, validate_query, validate_scope,
)
from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result
from app.services.rag.llama_index.validation import RagAdapterError, vector_snapshot


def readonly_session() -> Session:
    """仅查询调用时创建独立会话；构造/模块导入不连接数据库或启动服务。"""
    return Session(get_engine(), autoflush=False, expire_on_commit=False)


class DenseRetriever(BaseRetriever):
    """固定知识库的只读 Dense 边界；返回稳定 NodeWithScore，没有文档级筛选或状态写入。"""

    def __init__(self, kb_id: UUID | str, *, top_k: int = DEFAULT_TOP_K,
                 embedding_adapter: DKFEmbeddingAdapter | None = None,
                 session_factory: Callable | None = None):
        """构造仅验证参数、保存现有 Adapter/会话工厂，不加载 BGE-M3 或读取数据库。"""
        self._kb_id = validate_scope(kb_id, top_k)
        self._top_k = top_k
        if embedding_adapter is not None and not isinstance(embedding_adapter, DKFEmbeddingAdapter):
            raise DenseRetrievalError("DENSE_INPUT")
        if session_factory is not None and not callable(session_factory):
            raise DenseRetrievalError("DENSE_INPUT")
        super().__init__(callback_manager=CallbackManager([]), object_map={}, verbose=False)
        self._embedding = embedding_adapter if embedding_adapter is not None else DKFEmbeddingAdapter()
        self._session_factory = session_factory if session_factory is not None else readonly_session

    @property
    def kb_id(self) -> UUID:
        """公开固定业务范围，不允许查询参数临时覆盖知识库隔离。"""
        return self._kb_id

    @property
    def top_k(self) -> int:
        """公开经过校验的固定 Top-K，不以 QueryBundle 或额外 kwargs 放宽上限。"""
        return self._top_k

    def retrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """复写公共入口，避免基类含 Query/Node 正文的 callback payload 与递归 Query Engine。"""
        return self._retrieve(str_or_query_bundle)

    async def aretrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """在同进程线程执行同步检索，线程内独立建 Session，复用进程内模型锁。"""
        return await asyncio.to_thread(self.retrieve, str_or_query_bundle)

    def _retrieve(self, query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """原始 Query 一次编码、身份前后核对、只读精确查询与 Node 转换；失败不返回半份证据。"""
        started = perf_counter()
        stage = "input"
        try:
            validate_scope(self._kb_id, self._top_k)
            if isinstance(query_bundle, QueryBundle):
                if (query_bundle.embedding is not None or query_bundle.custom_embedding_strs is not None
                        or query_bundle.image_path is not None):
                    raise DenseRetrievalError("DENSE_INPUT")
                query = validate_query(query_bundle.query_str)
            else:
                query = validate_query(query_bundle)
            log_event("dense.started", kb_id=self._kb_id, top_k=self._top_k)
            stage = "query_embedding"
            identity = model_snapshot(self._embedding.query_model_identity())
            vector = vector_snapshot(self._embedding.get_query_embedding(query))
            if model_snapshot(self._embedding.query_model_identity()) != identity:
                raise DenseRetrievalError("DENSE_MODEL_CHANGED")
            stage = "database"
            with self._session_factory() as session:
                if session.new or session.dirty or session.deleted or session.in_transaction():
                    raise DenseRetrievalError("DENSE_SESSION")
                # 不提交也不写业务表；SQL 只读约束防止注入会话/后续改动意外写入。
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                hits = select_dense_hits(session, self._kb_id, vector, identity, self._top_k)
                stage = "node_adapter"
                nodes = [NodeWithScore(node=chunk_to_node(hit.chunk, hit.document, include_embedding=False),
                                       score=hit.similarity) for hit in hits]
            log_event("dense.empty" if not nodes else "dense.finished", kb_id=self._kb_id,
                      top_k=self._top_k, item_count=len(nodes), dimension=len(vector),
                      duration_ms=(perf_counter() - started) * 1000)
            return nodes
        except Exception as exc:
            if isinstance(exc, DenseRetrievalError):
                error = exc
            elif isinstance(exc, SQLAlchemyError) or stage == "database":
                error = DenseRetrievalError("DENSE_DATABASE")
            elif isinstance(exc, (EmbeddingError, RagAdapterError)) and stage == "query_embedding":
                error = DenseRetrievalError("DENSE_QUERY_EMBEDDING")
            else:
                error = DenseRetrievalError("DENSE_RESULT" if stage == "node_adapter" else "DENSE_FAILED")
            log_event("dense.failed", level=logging.WARNING, kb_id=self._kb_id, top_k=self._top_k,
                      stage=stage, error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """满足框架异步私有契约，仍走同一个受保护入口，无额外 GPU 进程或框架缓存。"""
        return await self.aretrieve(query_bundle)

    def retrieve_results(self, query: str | QueryBundle) -> list[RetrievalResult]:
        """按需还原已有项目 DTO，分数和证据不变；不创建新模型、数据库记录或 RAG 回答。"""
        return [node_to_result(node) for node in self.retrieve(query)]
