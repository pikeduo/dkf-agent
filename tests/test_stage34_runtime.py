"""阶段34与真实项目编排的Mock兼容；仅用户执行，不调用数据库、模型或云服务。"""

from types import SimpleNamespace

import pytest

pytest.importorskip("llama_index.core.schema", reason="需已有core/NLP缓存；跳过不等于验收通过")

from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
from app.services.rag.llama_index.node_adapter import node_to_result
from app.services.rag.bm25 import CorpusLimits
from scripts.stage34_runtime import Runtime, TimedComponent
from test_rag_query_engine import setup_engine
from test_stage34_fallback import configuration


def test_evaluation_wrappers_preserve_production_answer_and_final_evidence():
    """原Engine仍检索/重排/合成各一次；评测捕获同次五条最终证据及原有三类分数。"""
    engine, retriever, reranker, backend = setup_engine()
    runtime = SimpleNamespace(stage="input", timings={}, final_evidence=[])
    engine._retriever = TimedComponent(engine._retriever, runtime, "retrieval")
    engine._reranker = TimedComponent(engine._reranker, runtime, "rerank")
    engine._synthesizer = TimedComponent(engine._synthesizer, runtime, "synthesis")
    answer = engine.query("PRIVATE_QUERY", allow_cloud=True)
    assert answer.status == "success" and retriever.calls == ["PRIVATE_QUERY"]
    assert len(reranker.calls) == len(backend.calls) == 1
    assert len(runtime.final_evidence) == 5
    assert set(runtime.timings) == {"retrieval", "rerank", "synthesis"}
    by_id = {e["chunk_id"]: e for e in runtime.final_evidence}
    for citation in answer.citations:
        evidence = by_id[str(citation.chunk_id)]
        assert citation.text == evidence["text"] and citation.metadata == evidence["metadata"]
        assert citation.rerank_score == evidence["rerank_score"]
        assert citation.fusion_score == evidence["fusion_score"]
        assert citation.retrieval_score == evidence["retrieval_score"]
    originals = {str(node_to_result(n, log_success=False).chunk_id) for n in retriever.nodes}
    assert set(by_id) <= originals


def test_explicit_hybrid_budget_reaches_only_bm25_without_changing_scope():
    """显式大库预算传递到原BM25；生产默认仍保留，Dense和文档范围不改变。"""
    engine, retriever, _, _ = setup_engine()
    limits = CorpusLimits(100000, 256 * 1024**2, 32000000)
    hybrid = HybridRetriever(retriever.kb_id, doc_ids=engine._doc_ids, top_k=30, corpus_limits=limits)
    assert hybrid._bm25._corpus_limits is limits
    assert hybrid._dense.doc_ids == hybrid._bm25.doc_ids == engine._doc_ids
    assert HybridRetriever(retriever.kb_id)._bm25._corpus_limits is None


def test_runtime_identity_freezes_backup_model_endpoint_budget_and_timeout(monkeypatch, configuration):
    """主备指纹包含独立模型/端点摘要/预算；改变备用配置不能混写旧run，密钥不进身份。"""
    from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
    from app.services import reranking

    def embedding_identity(self):
        """只返回固定内存身份，不加载BGE-M3。"""
        return {"model": "embedding-stub"}

    def reranker_identity():
        """只返回固定内存身份，不加载重排模型。"""
        return {"model": "reranker-stub"}

    def reranker_service():
        """替代真实重排服务入口，不访问模型目录。"""
        return SimpleNamespace(identity=reranker_identity)

    monkeypatch.setattr(DKFEmbeddingAdapter, "query_model_identity", embedding_identity)
    monkeypatch.setattr(reranking, "get_reranker_service", reranker_service)
    identity = Runtime(configuration).identity()
    assert identity["fallback"] is True and identity["llm_calls_per_case_max"] == 2
    backup = identity["backup"]
    assert backup["model"] == configuration.deepseek_model and backup["provider"] == "deepseek"
    assert backup["max_tokens"] == configuration.deepseek_max_tokens
    assert backup["timeout_seconds"] == configuration.deepseek_timeout_seconds
    assert "endpoint_sha256" in backup and "mock-secret" not in str(identity)
    for field, value in (("deepseek_model", "other-model"),
                         ("deepseek_api_base_url", "https://api.deepseek.com/v1"),
                         ("deepseek_max_tokens", 1024), ("deepseek_timeout_seconds", 30)):
        changed = Runtime(configuration.model_copy(update={field: value})).identity()
        assert changed != identity and changed["model"] == identity["model"]
