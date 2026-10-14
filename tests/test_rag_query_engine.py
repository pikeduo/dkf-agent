"""用户执行的问答编排 Mock；复用已有 Node/重排 Stub，不读库、运行模型或调用云端。"""

import asyncio
import copy
import json
from uuid import UUID, uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="用户准备 core/NLP；跳过不是验收通过")

from llama_index.core.base.base_query_engine import BaseQueryEngine

from app.services.deepseek import DeepSeekError
from app.services.rag.llama_index.deepseek_adapter import DKFDeepSeekAdapter
from app.services.rag.llama_index.query_engine import DKFQueryEngine
from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
from app.services.rag.llama_index.response_synthesizer import DKFResponseSynthesizer
from app.services.rag.synthesis import RAGError
from test_rag_synthesis import LLMServiceStub, answer_for
from test_reranker_adapter import ServiceStub, candidates


class RetrieverStub:
    """只返回已建立合法身份的融合 Node，不重复检索/编码/创建记录。"""

    def __init__(self, nodes, *, kb_id=None, doc_ids=None):
        """冻结输入范围与调用记录，用于核对 Query 原样且只调用一次。"""
        self.nodes, self.calls = nodes, []
        self.kb_id = kb_id or nodes[0].node.metadata["kb_id"]
        self.kb_id = UUID(str(self.kb_id))
        self.doc_ids, self.top_k = doc_ids, 30

    def retrieve(self, query):
        """提供内存候选或故障，不启动任何业务服务。"""
        self.calls.append(query)
        if isinstance(self.nodes, Exception):
            raise self.nodes
        return self.nodes


def setup_engine(nodes=None, *, doc_ids=None):
    """注入三层 Mock，仅实际复用生产 Node Adapter 与受保护编排。"""
    nodes = candidates(6) if nodes is None else nodes
    retriever = RetrieverStub(nodes, doc_ids=doc_ids)
    rerank_service = ServiceStub(list(range(len(nodes))))
    backend = LLMServiceStub(answer_for(list(reversed(nodes))))
    engine = DKFQueryEngine(retriever.kb_id, doc_ids=doc_ids, retriever=retriever,
        reranker=BGERerankerAdapter(service=rerank_service),
        synthesizer=DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)))
    return engine, retriever, rerank_service, backend


def test_pipeline_reuses_candidates_and_only_sends_final_top5_without_mutation():
    """融合→一次批量重排→一次合成，淘汰候选不上云且返回项目Schema而非框架Response。"""
    engine, retriever, reranker, backend = setup_engine()
    original = copy.deepcopy([node.model_dump() for node in retriever.nodes])
    assert isinstance(engine, BaseQueryEngine)
    result = engine.query("PRIVATE_QUERY", allow_cloud=True)
    assert result.status == "success" and retriever.calls == ["PRIVATE_QUERY"]
    assert len(reranker.calls) == len(backend.calls) == 1
    payload = json.loads(backend.calls[0][1])
    final_ids = [item["evidence_id"] for item in payload["evidence"]]
    assert len(final_ids) == 5 and retriever.nodes[0].node.node_id not in final_ids
    assert [node.model_dump() for node in retriever.nodes] == original
    assert result.citations[0].rerank_score == 5.0 and result.citations[0].fusion_score is not None
    assert set(result.model_dump()) == {"status", "answer", "citations", "insufficient_evidence"}


def test_consent_and_bad_query_fail_before_retrieval_or_models():
    """缺许可或空问题在任何检索/模型/云调用前安全拒绝。"""
    engine, retriever, reranker, backend = setup_engine()
    with pytest.raises(RAGError, match="RAG_CLOUD_PERMISSION"):
        engine.query("question")
    with pytest.raises(RAGError, match="RAG_INPUT"):
        engine.query("", allow_cloud=True)
    assert retriever.calls == [] and reranker.calls == [] and backend.calls == []


def test_empty_result_skips_reranker_model_and_cloud():
    """无候选是真正证据不足，不加载重排服务、不调用模型或扩范围。"""
    engine, retriever, reranker, backend = setup_engine()
    retriever.nodes = []
    result = engine.query("question", allow_cloud=True)
    assert result.insufficient_evidence and result.citations == []
    assert reranker.calls == [] and backend.calls == []


@pytest.mark.parametrize("stage,code", [("retrieval", "RAG_RETRIEVAL"), ("rerank", "RAG_RERANK"),
    ("cloud", "DEEPSEEK_RATE_LIMIT")])
def test_upstream_failure_is_not_insufficiency_or_silent_fallback(stage, code):
    """异常不同于证据不足，不重检索/回退原候选/重试收费请求，固定错误可追踪。"""
    engine, retriever, reranker, backend = setup_engine()
    if stage == "retrieval":
        retriever.nodes = RuntimeError("PRIVATE_SQL")
    elif stage == "rerank":
        reranker.scores = RuntimeError("PRIVATE_MODEL_PATH")
    else:
        backend.output = DeepSeekError("DEEPSEEK_RATE_LIMIT")
    with pytest.raises(Exception, match=code) as failure:
        engine.query("question", allow_cloud=True)
    assert "PRIVATE" not in str(failure.value) and len(retriever.calls) == 1
    assert len(backend.calls) == (1 if stage == "cloud" else 0)


def test_out_of_scope_evidence_rejected_before_rerank_and_cloud():
    """合法Node仍需满足调用范围，不能先检索后静默删除其他文档。"""
    engine, retriever, reranker, backend = setup_engine(doc_ids=(uuid4(),))
    with pytest.raises(RAGError, match="RAG_EVIDENCE"):
        engine.query("question", allow_cloud=True)
    assert reranker.calls == [] and backend.calls == []


def test_reranker_cannot_replace_original_text_or_source_fields(monkeypatch):
    """即便组件返回类型合法结果，偷换正文/证据仍在云调用前整体拒绝。"""
    engine, retriever, reranker, backend = setup_engine()
    original = BGERerankerAdapter.postprocess_nodes

    def changed(adapter, nodes, **kwargs):
        """调用合法Mock评分后模拟正文被修改，验证最终证据一致性保护。"""
        final = original(adapter, nodes, **kwargs)
        final[0].node.text = "PRIVATE_REPLACEMENT"
        return final

    monkeypatch.setattr(BGERerankerAdapter, "postprocess_nodes", changed)
    with pytest.raises(RAGError, match="RAG_EVIDENCE"):
        engine.query("question", allow_cloud=True)
    assert backend.calls == []


def test_async_does_not_use_framework_payload_callbacks(monkeypatch):
    """异步也走同一个项目入口，无默认LLM/Query Engine递归和含正文回调。"""
    def forbidden(*args, **kwargs):
        """任何使用基类公共Query的路径都应失败，不触发真实工作负载。"""
        pytest.fail("禁止框架默认 Query 回调")

    monkeypatch.setattr(BaseQueryEngine, "query", forbidden)
    monkeypatch.setattr(BaseQueryEngine, "aquery", forbidden)
    engine, retriever, reranker, backend = setup_engine()
    response = asyncio.run(engine.aquery("question", allow_cloud=True))
    assert response.status == "success" and len(retriever.calls) == len(reranker.calls) == len(backend.calls) == 1


@pytest.mark.parametrize("value", [[], ["not-uuid"]])
def test_invalid_doc_scope_is_not_expanded(value):
    """无效/空doc_ids在构造时拒绝，不自动改为全知识库。"""
    with pytest.raises(RAGError, match="RAG_INPUT"):
        DKFQueryEngine(uuid4(), doc_ids=value)
