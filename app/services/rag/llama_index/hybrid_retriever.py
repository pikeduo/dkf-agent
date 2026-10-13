"""组合现有 Dense/BM25 的稳定 RRF 边界，不生成 Query、调用 LLM 或建立新索引。"""

import asyncio
import logging
import math
from collections.abc import Callable
from time import perf_counter
from uuid import UUID

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.callbacks import CallbackManager
from llama_index.core.schema import NodeWithScore, QueryBundle

from app.core.business_logging import log_event
from app.schemas.retrieval import FusionProvenance, RetrievalResult
from app.services.rag.bm25 import MAX_QUERY_TERMS, BM25RetrievalError, tokenize
from app.services.rag.dense import DenseRetrievalError, validate_doc_ids, validate_query, validate_scope
from app.services.rag.llama_index.bm25_retriever import BM25Retriever
from app.services.rag.llama_index.dense_retriever import DenseRetriever
from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
from app.services.rag.llama_index.node_adapter import node_to_result

ERRORS = {
    "FUSION_INPUT": "知识库、文档范围、Query 或融合参数无效",
    "FUSION_LIMIT": "Query、文档范围或融合参数超过安全上限",
    "FUSION_DENSE": "Dense 分支未完成，未静默返回 BM25 单路结果",
    "FUSION_BM25": "BM25 分支未完成，未静默返回 Dense 单路结果",
    "FUSION_RESULT": "分支结果身份、排序、分数或证据无效，未返回部分候选",
    "FUSION_CONFLICT": "同一 Chunk 的两路证据不一致，请核对并发维护后手动重试",
    "FUSION_FAILED": "Hybrid 检索未完成，请查看安全错误码",
}


class HybridRetrievalError(Exception):
    """融合异常只携带固定中文原因；不透传查询、正文、SQL 或上游异常原文。"""

    def __init__(self, code: str):
        """未知分类安全归为通用错误，拒绝把任意异常文本写入输出。"""
        self.code = code if code in ERRORS else "FUSION_FAILED"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def fusion_scope(kb_id, doc_ids, top_k: int, branch_top_k: int, rrf_k: int):
    """复用阶段18范围校验并冻结两路 Top-K；RRF 常量须为1～1000的严格整数。"""
    try:
        identity = validate_scope(kb_id, top_k)
        validate_scope(identity, branch_top_k)
        scope = validate_doc_ids(doc_ids)
        if type(rrf_k) is not int or rrf_k < 1:
            raise HybridRetrievalError("FUSION_INPUT")
        if rrf_k > 1000:
            raise HybridRetrievalError("FUSION_LIMIT")
        return identity, scope
    except DenseRetrievalError as exc:
        raise HybridRetrievalError("FUSION_LIMIT" if exc.code == "DENSE_LIMIT" else "FUSION_INPUT") from None


def fuse_nodes(dense: list[NodeWithScore], bm25: list[NodeWithScore], *,
               kb_id: UUID | str, doc_ids=None, top_k: int = 20,
               branch_top_k: int = 20, rrf_k: int = 60) -> list[NodeWithScore]:
    """以原 Chunk UUID 去重，按1-based排名等权RRF；证据冲突/坏行全批拒绝且不修改输入。"""
    kb_id, doc_ids = fusion_scope(kb_id, doc_ids, top_k, branch_top_k, rrf_k)
    candidates = {}
    try:
        for name, hits in (("dense", dense), ("bm25", bm25)):
            if not isinstance(hits, list) or len(hits) > branch_top_k:
                raise HybridRetrievalError("FUSION_RESULT")
            seen, last = set(), None
            for rank, hit in enumerate(hits, start=1):
                if not isinstance(hit, NodeWithScore):
                    raise HybridRetrievalError("FUSION_RESULT")
                result = node_to_result(hit, log_success=False)
                score = result.retrieval_score
                if (result.kb_id != kb_id or (doc_ids is not None and result.doc_id not in doc_ids)
                        or result.fusion is not None or hit.node.embedding is not None
                        or score is None or not math.isfinite(score)
                        or (name == "dense" and not -1 <= score <= 1)
                        or (name == "bm25" and score <= 0) or result.chunk_id in seen):
                    raise HybridRetrievalError("FUSION_RESULT")
                key = (-score, result.chunk_id.int)
                if last is not None and key < last:
                    raise HybridRetrievalError("FUSION_RESULT")
                seen.add(result.chunk_id)
                last = key
                entry = candidates.get(result.chunk_id)
                if entry is None:
                    # 返回新的深复制 Node，仅加运行时来源；原正文、Chunk元数据及SOURCE不变。
                    entry = {"node": hit.node.model_copy(deep=True), "ranks": {}, "scores": {}}
                    candidates[result.chunk_id] = entry
                elif (entry["node"].text != hit.node.text or entry["node"].metadata != hit.node.metadata
                      or entry["node"].relationships != hit.node.relationships):
                    raise HybridRetrievalError("FUSION_CONFLICT")
                entry["ranks"][name], entry["scores"][name] = rank, score
        fused = []
        for chunk_id, entry in candidates.items():
            ranks, scores = entry["ranks"], entry["scores"]
            score = math.fsum(1.0 / (rrf_k + rank) for rank in ranks.values())
            provenance = FusionProvenance(rrf_k=rrf_k,
                sources=[name for name in ("dense", "bm25") if name in ranks],
                dense_rank=ranks.get("dense"), dense_score=scores.get("dense"),
                bm25_rank=ranks.get("bm25"), bm25_score=scores.get("bm25"), fusion_score=score)
            node = entry["node"]
            node.metadata["fusion"] = provenance.model_dump()
            # 来源解释不混入未来模型输入；每个原Node仅追加一个可校验的运行时字段。
            node.excluded_embed_metadata_keys = sorted(node.metadata)
            node.excluded_llm_metadata_keys = sorted(node.metadata)
            fused.append((chunk_id, NodeWithScore(node=node, score=score)))
        fused.sort(key=lambda item: (-item[1].score, item[0].int))
        return [hit for _, hit in fused[:top_k]]
    except HybridRetrievalError:
        raise
    except Exception:
        raise HybridRetrievalError("FUSION_RESULT") from None


class HybridRetriever(BaseRetriever):
    """同知识库/文档范围的两路组合，仅Query一次Dense编码；没有Query Engine或LLM。"""

    def __init__(self, kb_id: UUID | str, *, doc_ids=None, top_k: int = 20,
                 branch_top_k: int = 20, rrf_k: int = 60,
                 embedding_adapter: DKFEmbeddingAdapter | None = None,
                 session_factory: Callable | None = None):
        """惰性构造并冻结范围；两路使用已有服务/独立只读会话，不安装依赖或连接数据库。"""
        self._kb_id, self._doc_ids = fusion_scope(kb_id, doc_ids, top_k, branch_top_k, rrf_k)
        self._top_k, self._branch_top_k, self._rrf_k = top_k, branch_top_k, rrf_k
        if (session_factory is not None and not callable(session_factory)
                or embedding_adapter is not None and not isinstance(embedding_adapter, DKFEmbeddingAdapter)):
            raise HybridRetrievalError("FUSION_INPUT")
        super().__init__(callback_manager=CallbackManager([]), object_map={}, verbose=False)
        self._dense = DenseRetriever(self._kb_id, doc_ids=self._doc_ids, top_k=branch_top_k,
                                    embedding_adapter=embedding_adapter, session_factory=session_factory)
        self._bm25 = BM25Retriever(self._kb_id, doc_ids=self._doc_ids, top_k=branch_top_k,
                                  session_factory=session_factory)

    @property
    def kb_id(self) -> UUID:
        """返回冻结知识库，不允许Query临时改变范围。"""
        return self._kb_id

    @property
    def doc_ids(self) -> tuple[UUID, ...] | None:
        """返回不可变文档范围，None沿用全库语义，空列表不变为全库。"""
        return self._doc_ids

    @property
    def top_k(self) -> int:
        """返回融合后的候选上限，不是每路原始候选数。"""
        return self._top_k

    @property
    def branch_top_k(self) -> int:
        """返回两路相同的候选上限，默认各20项。"""
        return self._branch_top_k

    @property
    def rrf_k(self) -> int:
        """返回固定的排名平滑常量，不能解释为置信阈值。"""
        return self._rrf_k

    def retrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """复写公共入口，不走含Query/Node的基类callback、递归检索或默认LLM。"""
        return self._retrieve(str_or_query_bundle)

    async def aretrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """同进程线程内串行两路检索；沿用独立短会话和进程共享模型，不另起GPU进程。"""
        return await asyncio.to_thread(self.retrieve, str_or_query_bundle)

    def _retrieve(self, query_bundle: str | QueryBundle) -> list[NodeWithScore]:
        """原Query各调用一次两路；任一路失败整体失败，正常空分支允许另一支参与RRF。"""
        started, stage = perf_counter(), "input"
        scope_log = {"kb_id": self._kb_id, "top_k": self._top_k,
                     "doc_id": self._doc_ids[0] if self._doc_ids and len(self._doc_ids) == 1 else None,
                     "source_document_count": len(self._doc_ids) if self._doc_ids is not None else None}
        try:
            if isinstance(query_bundle, QueryBundle):
                if (query_bundle.embedding is not None or query_bundle.custom_embedding_strs is not None
                        or query_bundle.image_path is not None):
                    raise HybridRetrievalError("FUSION_INPUT")
                query = validate_query(query_bundle.query_str)
            else:
                query = validate_query(query_bundle)
            # 先检查BM25词项预算，避免已知超限时仍消耗Query GPU编码/数据库读取。
            tokenize(query, max_terms=MAX_QUERY_TERMS)
            log_event("fusion.started", **scope_log)
            branches = []
            for stage, retriever in (("dense", self._dense), ("bm25", self._bm25)):
                branch_started = perf_counter()
                hits = retriever.retrieve(query)
                branches.append(hits)
                log_event("fusion.branch_finished", **scope_log, stage=stage, item_count=len(hits),
                          duration_ms=(perf_counter() - branch_started) * 1000)
            stage = "rrf"
            nodes = fuse_nodes(*branches, kb_id=self._kb_id, doc_ids=self._doc_ids,
                               top_k=self._top_k, branch_top_k=self._branch_top_k, rrf_k=self._rrf_k)
            log_event("fusion.finished" if nodes else "fusion.empty", **scope_log, item_count=len(nodes),
                      duration_ms=(perf_counter() - started) * 1000)
            return nodes
        except Exception as exc:
            cause = None
            if isinstance(exc, HybridRetrievalError):
                error = exc
            elif stage == "input" and isinstance(exc, (DenseRetrievalError, BM25RetrievalError)):
                error = HybridRetrievalError("FUSION_LIMIT" if exc.code.endswith("LIMIT") else "FUSION_INPUT")
            else:
                error = HybridRetrievalError({"dense": "FUSION_DENSE", "bm25": "FUSION_BM25"}.get(stage, "FUSION_FAILED"))
                cause = exc.code if isinstance(exc, (DenseRetrievalError, BM25RetrievalError)) else None
            log_event("fusion.failed", level=logging.WARNING, **scope_log, stage=stage,
                      error_code=error.code, error_type=cause, duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """满足LlamaIndex私有异步契约，仍走同一个脱敏入口。"""
        return await self.aretrieve(query_bundle)

    def retrieve_results(self, query: str | QueryBundle) -> list[RetrievalResult]:
        """转为既有证据DTO并保留类型化fusion来源，不写库、不生成回答。"""
        return [node_to_result(hit) for hit in self.retrieve(query)]
