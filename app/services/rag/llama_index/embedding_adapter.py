"""LlamaIndex Query Embedding 边界，复用已有服务；禁止框架重新向量化持久化文档。"""

import asyncio
import logging
from collections.abc import Sequence
from time import perf_counter
from typing import Any

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.callbacks import CallbackManager
from llama_index.core.schema import BaseNode
from pydantic import PrivateAttr

from app.core.business_logging import log_event
from app.services.embedding import EmbeddingService, get_embedding_service
from app.services.embedding.service import MODEL_ID, EmbeddingError
from app.services.rag.llama_index.validation import RagAdapterError, json_snapshot, vector_snapshot


class DKFEmbeddingAdapter(BaseEmbedding):
    """构造不加载模型、不修改全局 Settings；Query 使用项目共享实例，Node 只复用已有向量。"""

    _service: Any = PrivateAttr(default=None)

    def __init__(self, service: EmbeddingService | None = None):
        """仅保存可选服务；关闭框架 Embedding Cache 与额外 Worker，不创建第二个模型实例。"""

        super().__init__(model_name=MODEL_ID, embed_batch_size=1,
                         callback_manager=CallbackManager([]), embeddings_cache=None, num_workers=1)
        self._service = service

    @classmethod
    def class_name(cls) -> str:
        """为运行时表示提供固定 Adapter 名称，不序列化或重建私有模型实例。"""

        return "DKFEmbeddingAdapter"

    def _backend(self) -> EmbeddingService:
        """显式 Query 调用时才获取进程内已有服务，不自行构造 SentenceTransformer。"""

        return self._service if self._service is not None else get_embedding_service()

    def query_model_identity(self) -> dict:
        """显式检索时读取共享服务的实际身份，不猜指纹；首次调用可能加载用户已部署的模型。"""
        try:
            return json_snapshot(self._backend().identity())
        except Exception as exc:
            error = exc if isinstance(exc, (EmbeddingError, RagAdapterError)) else RagAdapterError("RAG_ADAPTER_EMBEDDING")
            log_event("rag.adapter_failed", level=logging.WARNING, stage="query_identity", error_code=error.code)
            raise error from None

    def get_query_embedding(self, query: str) -> list[float]:
        """原样转交 Query 并校验返回向量；不走框架携带正文/向量的回调事件或另一套缓存。"""

        started = perf_counter()
        try:
            vector = vector_snapshot(self._backend().embed_query(query))
        except Exception as exc:
            error = exc if isinstance(exc, (EmbeddingError, RagAdapterError)) else RagAdapterError("RAG_ADAPTER_EMBEDDING")
            log_event("rag.adapter_failed", level=logging.WARNING, stage="query_embedding",
                      error_code=error.code)
            raise error from None
        log_event("rag.query_encoded", dimension=len(vector), model_name=MODEL_ID,
                  duration_ms=(perf_counter() - started) * 1000)
        return vector

    async def aget_query_embedding(self, query: str) -> list[float]:
        """在同一进程线程中调用同步共享服务，不阻塞事件循环、不启动 GPU 子进程或任务。"""

        return await asyncio.to_thread(self.get_query_embedding, query)

    def _get_query_embedding(self, query: str) -> list[float]:
        """满足 BaseEmbedding 私有同步契约，继续使用统一 Query 路径。"""

        return self.get_query_embedding(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        """满足 BaseEmbedding 私有异步契约，不加载另一份模型。"""

        return await self.aget_query_embedding(query)

    def _reject_document_embedding(self) -> None:
        """任何文档重编码企图均安全拒绝；不隐式进入 IngestionPipeline 或产生新向量记录。"""

        error = RagAdapterError("RAG_DOCUMENT_EMBEDDING_FORBIDDEN")
        log_event("rag.adapter_failed", level=logging.WARNING, stage="document_embedding", error_code=error.code)
        raise error

    def get_text_embedding(self, text: str) -> list[float]:
        """拒绝框架单文本生成文档向量，文档仍由项目 GPU 任务管理。"""

        self._reject_document_embedding()

    async def aget_text_embedding(self, text: str) -> list[float]:
        """异步文档编码同样立即拒绝，不产生线程或模型调用。"""

        self._reject_document_embedding()

    def get_text_embedding_batch(self, texts: list[str], show_progress: bool = False, **kwargs) -> list[list[float]]:
        """拒绝框架整批文档重算，不把已有 Chunk 再送入第二套 Embedding 流程。"""

        self._reject_document_embedding()

    async def aget_text_embedding_batch(self, texts: list[str], show_progress: bool = False, **kwargs) -> list[list[float]]:
        """异步批量文档编码直接拒绝，不开启额外缓存或批处理 Worker。"""

        self._reject_document_embedding()

    def _get_text_embedding(self, text: str) -> list[float]:
        """满足 BaseEmbedding 单文本抽象方法，但禁止绕过公共文档编码保护。"""

        self._reject_document_embedding()

    async def _aget_text_embedding(self, text: str) -> list[float]:
        """私有异步方法也拒绝文档重编码，避免框架内部旁路。"""

        self._reject_document_embedding()

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """私有批处理入口不绕过保护，避免重新生成持久化文档向量。"""

        self._reject_document_embedding()

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """私有异步批处理保持相同拒绝语义。"""

        self._reject_document_embedding()

    def __call__(self, nodes: Sequence[BaseNode], **kwargs) -> Sequence[BaseNode]:
        """只验证并返回已有向量的 Node，不修改 Node、落库或调用文档编码。"""

        try:
            if (not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes))
                    or any(not isinstance(node, BaseNode) for node in nodes)):
                raise RagAdapterError("RAG_ADAPTER_NODE")
            if any(node.embedding is None for node in nodes):
                self._reject_document_embedding()
            for node in nodes:
                vector_snapshot(node.embedding)
        except RagAdapterError as error:
            if error.code != "RAG_DOCUMENT_EMBEDDING_FORBIDDEN":
                log_event("rag.adapter_failed", level=logging.WARNING, stage="nodes_reuse", error_code=error.code)
            raise
        log_event("rag.nodes_reused", item_count=len(nodes))
        return nodes

    async def acall(self, nodes: Sequence[BaseNode], **kwargs) -> Sequence[BaseNode]:
        """已有向量的异步 Transform 仍只验证，不生成新向量或额外模型。"""

        return self(nodes, **kwargs)
