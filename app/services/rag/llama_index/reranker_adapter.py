"""LlamaIndex Node Postprocessor仅委托项目共享Reranker；不自行加载框架模型或调用云端。"""

import asyncio
import logging
from time import perf_counter
from typing import Any

from llama_index.core.callbacks import CallbackManager
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import NodeWithScore, QueryBundle
from pydantic import Field, PrivateAttr

from app.core.business_logging import log_event
from app.schemas.retrieval import RerankProvenance, RetrievalResult
from app.services.rag.llama_index.node_adapter import node_to_result, result_to_node
from app.services.rag.llama_index.validation import json_snapshot
from app.services.reranking import MAX_CANDIDATES, RerankError, RerankerService, get_reranker_service, validate_inputs, validate_scores


class BGERerankerAdapter(BaseNodePostprocessor):
    """在线默认同步及Top5，保留原检索/融合分及全部业务证据；只有服务负责模型。"""

    top_n: int = Field(default=5, ge=1, le=100, strict=True, frozen=True)
    _service: Any = PrivateAttr(default=None)

    def __init__(self, *, top_n: int = 5, service: RerankerService | None = None):
        """保存可注入的共享服务；默认构造不取模型/配置、不启动GPU或框架追踪。"""
        if type(top_n) is not int or not 1 <= top_n <= MAX_CANDIDATES:
            raise RerankError("RERANK_CONFIG")
        super().__init__(top_n=top_n, callback_manager=CallbackManager([]))
        self._service = service

    @classmethod
    def class_name(cls) -> str:
        """返回固定适配器名称，不把私有模型实例或Query序列化。"""
        return "BGERerankerAdapter"

    def _backend(self) -> RerankerService:
        """显式重排时获取项目进程内共享服务，不构造另一份模型或LlamaIndex内置Reranker。"""
        return self._service if self._service is not None else get_reranker_service()

    def postprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None,
                          query_str: str | None = None) -> list[NodeWithScore]:
        """校验原Query/候选后一次批量委托；错误整体失败，不修改传入Node或静默退回原排名。"""
        started, stage, scope_log = perf_counter(), "input", {"top_k": self.top_n}
        try:
            if query_bundle is not None:
                if (query_str is not None or not isinstance(query_bundle, QueryBundle)
                        or query_bundle.embedding is not None or query_bundle.custom_embedding_strs is not None
                        or query_bundle.image_path is not None):
                    raise RerankError("RERANK_INPUT")
                query = query_bundle.query_str
            else:
                query = query_str
            if not isinstance(nodes, list):
                raise RerankError("RERANK_INPUT")
            if len(nodes) > MAX_CANDIDATES:
                raise RerankError("RERANK_LIMIT")
            snapshots, results, ids = [], [], set()
            for hit in nodes:
                if not isinstance(hit, NodeWithScore):
                    raise RerankError("RERANK_RESULT")
                snapshot = hit.model_copy(deep=True)
                result = node_to_result(snapshot, log_success=False)
                if result.rerank is not None or result.retrieval_score is None or result.chunk_id in ids:
                    raise RerankError("RERANK_RESULT")
                if results and result.kb_id != results[0].kb_id:
                    raise RerankError("RERANK_RESULT")
                snapshots.append(snapshot.node)
                results.append(result)
                ids.add(result.chunk_id)
            validate_inputs(query, [result.text for result in results])
            if not nodes:
                log_event("rerank.skipped", item_count=0, **scope_log)
                return []
            scope_log["kb_id"] = results[0].kb_id
            log_event("rerank.started", item_count=len(nodes), **scope_log)
            stage = "model"
            service = self._backend()
            before = json_snapshot(service.identity())
            # 类型化身份先校验，坏部署/Stub不应消耗候选推理，也不猜模型版本。
            RerankProvenance(**before, input_rank=1, retrieval_score=results[0].retrieval_score,
                             fusion_score=results[0].fusion_score, rerank_score=0.0)
            scores = validate_scores(service.score(query, [result.text for result in results]), len(results))
            if json_snapshot(service.identity()) != before:
                raise RerankError("RERANK_MODEL_CHANGED")
            stage = "result"
            ranked = []
            for rank, (node, result, score) in enumerate(zip(snapshots, results, scores), start=1):
                provenance = RerankProvenance(**before, input_rank=rank,
                    retrieval_score=result.retrieval_score, fusion_score=result.fusion_score, rerank_score=score)
                node.metadata["rerank"] = provenance.model_dump()
                node.excluded_embed_metadata_keys = sorted(node.metadata)
                node.excluded_llm_metadata_keys = sorted(node.metadata)
                hit = NodeWithScore(node=node, score=score)
                node_to_result(hit, log_success=False)
                ranked.append((result.chunk_id, hit))
            ranked.sort(key=lambda pair: (-pair[1].score, pair[0].int))
            selected = [hit for _, hit in ranked[:self.top_n]]
            log_event("rerank.finished", item_count=len(selected), **scope_log,
                      duration_ms=(perf_counter() - started) * 1000)
            return selected
        except Exception as exc:
            error = exc if isinstance(exc, RerankError) else RerankError(
                "RERANK_INFERENCE" if stage == "model" else "RERANK_RESULT")
            log_event("rerank.failed", level=logging.WARNING, stage=stage, error_code=error.code,
                      **scope_log, duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    def _postprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None):
        """满足框架私有契约，仍走同一个安全同步入口，不触发含正文回调。"""
        return self.postprocess_nodes(nodes, query_bundle=query_bundle)

    async def apostprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None,
                                query_str: str | None = None) -> list[NodeWithScore]:
        """显式异步仅在线程中调用同步服务，不另建模型、Worker或新的数据库会话。"""
        return await asyncio.to_thread(self.postprocess_nodes, nodes, query_bundle, query_str)

    async def _apostprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None):
        """满足框架异步私有入口，保持与公共入口一致的证据和异常约束。"""
        return await self.apostprocess_nodes(nodes, query_bundle=query_bundle)

    def postprocess_results(self, results: list[RetrievalResult], *, query: str) -> list[RetrievalResult]:
        """支持项目DTO边界，与Node双向适配且保留全部分数，不加载原件/Embedding或落库。"""
        if not isinstance(results, list):
            raise RerankError("RERANK_INPUT")
        if len(results) > MAX_CANDIDATES:
            raise RerankError("RERANK_LIMIT")
        try:
            nodes = [result_to_node(result, log_success=False) for result in results]
            return [node_to_result(hit, log_success=False) for hit in self.postprocess_nodes(nodes, query_str=query)]
        except RerankError:
            raise
        except Exception:
            raise RerankError("RERANK_RESULT") from None
