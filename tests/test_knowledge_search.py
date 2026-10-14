"""用户执行的搜索编排Mock；复用内存Node/评分Stub，不运行真实检索、模型或云API。"""

import copy
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="需已有core/NLP缓存；跳过不是通过")

from app.services.rag.llama_index import hybrid_retriever, reranker_adapter
from app.services.rag.llama_index.node_adapter import node_to_result
from app.services.rag.search import search_knowledge_base
from app.services.rag.synthesis import RAGError
from test_reranker_adapter import ServiceStub, candidates


def mock_pipeline(monkeypatch, nodes, *, scores=None):
    """仅替换候选与项目评分服务，保留正式Node Adapter、重排排序与Evidence校验。"""
    calls = []
    scoring = ServiceStub(scores if scores is not None else list(range(len(nodes))))
    adapter_class = reranker_adapter.BGERerankerAdapter

    class RetrieverStub:
        """内存候选，不连接数据库、编码Query或启动任何任务。"""

        def __init__(self, kb_id, **options):
            """记录固定知识库/doc/Top-K范围。"""
            calls.append((kb_id, options))

        def retrieve(self, question):
            """只返回原内存Node，供包装层来源不变性校验。"""
            calls.append(question)
            return nodes

    def ranker(**options):
        """给正式Postprocessor注入已有内存评分Stub，不加载BGE模型。"""
        return adapter_class(service=scoring, **options)

    monkeypatch.setattr(hybrid_retriever, "HybridRetriever", RetrieverStub)
    monkeypatch.setattr(reranker_adapter, "BGERerankerAdapter", ranker)
    return calls, scoring


def test_search_reuses_hybrid_and_reranker_with_exact_scope_and_original_evidence(monkeypatch):
    """只检索一次/批量重排一次，返回DTO Top5；原身份、正文、页及source_spans不变。"""
    nodes = candidates(6)
    original = {node.node.node_id: node_to_result(node, log_success=False) for node in nodes}
    before = copy.deepcopy([node.model_dump() for node in nodes])
    first = next(iter(original.values()))
    docs = sorted({item.doc_id for item in original.values()})
    calls, scoring = mock_pipeline(monkeypatch, nodes)
    result = search_knowledge_base(first.kb_id, "PRIVATE_QUERY", doc_ids=docs, top_k=30)
    assert calls == [(first.kb_id, {"doc_ids": tuple(docs), "top_k": 30}), "PRIVATE_QUERY"]
    assert len(scoring.calls) == 1 and len(result) == 5
    assert [item.rerank_score for item in result] == [5., 4., 3., 2., 1.]
    for item in result:
        assert item.model_dump(exclude={"rerank", "rerank_score"}) == original[str(item.chunk_id)].model_dump(
            exclude={"rerank", "rerank_score"})
    assert [node.model_dump() for node in nodes] == before


def test_empty_search_skips_reranker_without_fabricating_evidence(monkeypatch):
    """空候选不构造评分服务、加载模型或调用LLM补源。"""
    mock_pipeline(monkeypatch, [])

    def forbidden(**options):
        """空集误触发重排应直接暴露编排缺陷。"""
        pytest.fail("空搜索不能构造Reranker")

    monkeypatch.setattr(reranker_adapter, "BGERerankerAdapter", forbidden)
    assert search_knowledge_base(uuid4(), "question") == []


def test_out_of_scope_candidates_fail_before_reranker(monkeypatch):
    """越界证据整体失败，不Python删行或扩大doc_ids，也不消耗评分计算。"""
    nodes = candidates(2)
    first = node_to_result(nodes[0], log_success=False)
    _, scoring = mock_pipeline(monkeypatch, nodes)
    with pytest.raises(RAGError, match="RAG_EVIDENCE"):
        search_knowledge_base(first.kb_id, "question", doc_ids=[uuid4()])
    assert scoring.calls == []


def test_rerank_failure_is_not_silently_returned_as_unranked_search(monkeypatch):
    """评分失败不返回原排名、不伪装空集，私有错误原文不透传。"""
    nodes = candidates(2)
    first = node_to_result(nodes[0], log_success=False)
    mock_pipeline(monkeypatch, nodes, scores=RuntimeError("PRIVATE_MODEL_PATH"))
    with pytest.raises(RAGError, match="RAG_RERANK") as error:
        search_knowledge_base(first.kb_id, "question")
    assert "PRIVATE" not in str(error.value)


def test_reranker_cannot_replace_original_chunk_text(monkeypatch):
    """即使重排DTO结构合法，正文偷换也整体失败，不以新正文冒充原Chunk证据。"""
    nodes = candidates(2)
    first = node_to_result(nodes[0], log_success=False)
    mock_pipeline(monkeypatch, nodes)
    real_factory = reranker_adapter.BGERerankerAdapter

    class ChangedReranker:
        """模拟替换文本的异常后处理器，不运行实际模型。"""

        def postprocess_nodes(self, incoming, *, query_str):
            """先生成合法重排Node再故意改变文本，供来源保护断言。"""
            final = real_factory(top_n=5).postprocess_nodes(incoming, query_str=query_str)
            final[0].node.text = "PRIVATE_REPLACEMENT"
            return final

    def changed_factory(**options):
        """替换构造入口而不改变真实生产Adapter或数据库。"""
        return ChangedReranker()

    monkeypatch.setattr(reranker_adapter, "BGERerankerAdapter", changed_factory)
    with pytest.raises(RAGError, match="RAG_EVIDENCE") as error:
        search_knowledge_base(first.kb_id, "question")
    assert "PRIVATE" not in str(error.value)
