"""LlamaIndex Query Engine 组合已有 Hybrid/Reranker 与项目统一 LLM 合成边界。"""

import asyncio
import logging
from time import perf_counter
from uuid import UUID

from llama_index.core.base.base_query_engine import BaseQueryEngine
from llama_index.core.callbacks import CallbackManager
from llama_index.core.schema import NodeWithScore

from app.core.business_logging import log_event
from app.schemas.rag import RAGAnswer
from app.services.llm import LLMError
from app.services.rag.dense import validate_doc_ids, validate_scope
from app.services.rag.llama_index.hybrid_retriever import HybridRetriever, HybridRetrievalError
from app.services.rag.llama_index.node_adapter import node_to_result
from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
from app.services.rag.llama_index.response_synthesizer import DKFResponseSynthesizer, query_text
from app.services.rag.synthesis import RAGError
from app.services.reranking import RerankError


class DKFQueryEngine(BaseQueryEngine):
    """默认 Hybrid Top30/每路20 → Reranker Top5 → 单次合成，原范围与证据保持不变。"""

    def __init__(self, kb_id: UUID | str, *, doc_ids=None, top_k: int = 30,
                 retriever: HybridRetriever | None = None, reranker: BGERerankerAdapter | None = None,
                 synthesizer: DKFResponseSynthesizer | None = None):
        """惰性构造并冻结范围；注入组件仍须匹配范围/数量，不连接数据库或加载模型。"""
        try:
            self._kb_id, self._doc_ids = validate_scope(kb_id, top_k), validate_doc_ids(doc_ids)
            self._top_k = top_k
            self._retriever = retriever if retriever is not None else HybridRetriever(
                self._kb_id, doc_ids=self._doc_ids, top_k=top_k)
            if (self._retriever.kb_id != self._kb_id or self._retriever.doc_ids != self._doc_ids
                    or self._retriever.top_k != top_k):
                raise ValueError
            self._reranker = reranker if reranker is not None else BGERerankerAdapter(top_n=5)
            if self._reranker.top_n != 5:
                raise ValueError
            self._synthesizer = synthesizer if synthesizer is not None else DKFResponseSynthesizer()
        except (RAGError, LLMError):
            # 配置错误不是问题格式错误，保留唯一LLM边界的安全分类供人工排障。
            raise
        except Exception:
            raise RAGError("RAG_INPUT") from None
        super().__init__(callback_manager=CallbackManager([]))

    def _get_prompt_modules(self):
        """不注册默认 Prompt/全局 LLM，合成策略仅由项目审查后的模块管理。"""
        return {}

    def _update_prompts(self, prompts):
        """禁止通过 Query Engine 动态覆盖正式 Prompt。"""
        if prompts:
            raise RAGError("RAG_UNSUPPORTED")

    def _validate_nodes(self, nodes, *, ranked: bool = False) -> dict:
        """最终边界再次验证原业务身份、范围及完整分数，不用 Python 静默删越界证据。"""
        if not isinstance(nodes, list) or len(nodes) > (5 if ranked else self._top_k):
            raise RAGError("RAG_EVIDENCE")
        results = {}
        for node in nodes:
            if not isinstance(node, NodeWithScore):
                raise RAGError("RAG_EVIDENCE")
            item = node_to_result(node, log_success=False)
            if (item.kb_id != self._kb_id or item.chunk_id in results or item.fusion is None
                    or (self._doc_ids is not None and item.doc_id not in self._doc_ids)
                    or (item.rerank is not None) != ranked):
                raise RAGError("RAG_EVIDENCE")
            results[item.chunk_id] = item
        return results

    def query(self, query, *, allow_cloud: bool = False) -> RAGAnswer:
        """只读同步问答；显式同意云外传后才检索，避开基类携带问题/回答的 callback。"""
        started, stage = perf_counter(), "input"
        try:
            if allow_cloud is not True:
                raise RAGError("RAG_CLOUD_PERMISSION")
            question = query_text(query)
            log_event("rag.started", kb_id=self._kb_id, top_k=self._top_k)
            stage = "retrieval"
            candidates = self._retriever.retrieve(question)
            originals = self._validate_nodes(candidates)
            log_event("rag.retrieved", kb_id=self._kb_id, item_count=len(candidates),
                      duration_ms=(perf_counter() - started) * 1000)
            stage = "rerank"
            final = self._reranker.postprocess_nodes(candidates, query_str=question)
            checked = self._validate_nodes(final, ranked=True)
            if len(final) != min(5, len(candidates)):
                raise RAGError("RAG_EVIDENCE")
            for identity, item in checked.items():
                if identity not in originals:
                    raise RAGError("RAG_EVIDENCE")
                # 只能增加运行时重排来源/分，不得偷换正文、页码、范围或原融合证据。
                if item.model_dump(exclude={"rerank", "rerank_score"}) != originals[identity].model_dump(
                        exclude={"rerank", "rerank_score"}):
                    raise RAGError("RAG_EVIDENCE")
            stage = "synthesis"
            answer = self._synthesizer.synthesize(question, final, allow_cloud=True)
            log_event("rag.finished", kb_id=self._kb_id, status=answer.status,
                      item_count=len(answer.citations), duration_ms=(perf_counter() - started) * 1000)
            return answer
        except Exception as exc:
            error = exc if isinstance(exc, (RAGError, LLMError)) else RAGError({
                "retrieval": "RAG_RETRIEVAL", "rerank": "RAG_RERANK"}.get(stage, "RAG_SYNTHESIS"))
            # 上游原文不透传，已知 LLM/RAG 分类可追踪；异常不作为证据不足返回。
            log_event("rag.failed", level=logging.WARNING, kb_id=self._kb_id,
                      stage=stage, error_code=error.code,
                      error_type=exc.code if isinstance(exc, (HybridRetrievalError, RerankError)) else None,
                      duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    async def aquery(self, query, *, allow_cloud: bool = False) -> RAGAnswer:
        """异步调用在同进程线程复用现有模型服务；无新 Worker 或分支并发。"""
        return await asyncio.to_thread(self.query, query, allow_cloud=allow_cloud)

    def _query(self, query_bundle):
        """基类私有调用不授予云调用许可；正式调用必须走显式授权公共入口。"""
        return self.query(query_bundle)

    async def _aquery(self, query_bundle):
        """私有异步路径同样不隐式开启云请求。"""
        return await self.aquery(query_bundle)
