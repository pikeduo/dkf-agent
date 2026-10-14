"""用户执行的Citation/Node往返测试，只使用已有内存Stub，不触发模型/数据库/云请求。"""

import copy

import pytest

pytest.importorskip("llama_index.core.schema", reason="用户准备core/NLP；跳过不是通过")

from app.services.rag import evidence as domain
from app.services.rag.llama_index.citation_adapter import citation_from_node, citation_to_node
from app.services.rag.llama_index.node_adapter import node_to_result
from test_rag_synthesis import final_nodes


def test_node_citation_roundtrip_preserves_all_sources_scores_and_stable_identity(monkeypatch):
    """框架互转不查DB/重新处理原件；所有业务ID、正文、source_spans及三分数不丢失。"""
    def forbidden():
        """任何意外取得数据库引擎都应失败，禁止用转换冒充来源核对。"""
        pytest.fail("Citation转换不能查询数据库")

    monkeypatch.setattr(domain, "get_engine", forbidden)
    node = final_nodes(1)[0]
    before = copy.deepcopy(node.model_dump())
    citation = citation_from_node(node, 1)
    restored = citation_to_node(citation)
    original = node_to_result(node, log_success=False)
    assert node_to_result(restored, log_success=False).model_dump() == original.model_dump()
    assert citation.chunk_id == original.chunk_id and citation.doc_id == original.doc_id
    assert citation.text == original.text and citation.metadata == original.metadata
    assert (citation.retrieval_score, citation.fusion_score, citation.rerank_score) == (
        original.retrieval_score, original.fusion_score, original.rerank_score)
    assert citation_from_node(node, 1).model_dump() == citation.model_dump()
    restored.node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 999
    assert node.model_dump() == before and citation.metadata["source_spans"][0]["page"] != 999


@pytest.mark.parametrize("number", [0, 6, True, "1"])
def test_invalid_citation_number_is_rejected_without_rebinding_node(number):
    """展示编号无效不能更改Node身份、伪造来源或生成新Chunk。"""
    node = final_nodes(1)[0]
    before = node.model_dump()
    with pytest.raises(domain.EvidenceError, match="EVIDENCE_CITATION"):
        citation_from_node(node, number)
    assert node.model_dump() == before
