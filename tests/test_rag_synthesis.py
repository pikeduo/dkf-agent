"""用户执行的最终 Evidence/引用/Adapter 用例，只有内存 Node 与单次服务 Stub。"""

import asyncio
import copy
import json
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="用户先准备 core/NLP；跳过不等于通过")

from llama_index.core.base.llms.types import ChatMessage, MessageRole
from llama_index.core.response_synthesizers.base import BaseSynthesizer
from llama_index.core.schema import QueryBundle

from app.services.rag.llama_index.deepseek_adapter import DKFDeepSeekAdapter
from app.services.rag.llama_index.node_adapter import node_to_result
from app.services.rag.llama_index.response_synthesizer import DKFResponseSynthesizer
from app.services.rag.synthesis import RAGError, RAG_SYSTEM_PROMPT, bind_answer, insufficient_answer
from test_reranker_adapter import ServiceStub, candidates
from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter


class LLMServiceStub:
    """仅返回预先定义的 JSON；不持有 HTTP 客户端、真实密钥或本地模型。"""

    model_name, max_tokens, context_chars = "deepseek-flash", 2048, 60000

    def __init__(self, output):
        """保存单次输出及内存调用记录，用于断言只发送最终证据。"""
        self.output, self.calls = output, []

    def complete(self, system, user):
        """模拟项目唯一服务，任何异常保持可控且不联网。"""
        self.calls.append((system, user))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def final_nodes(count=2):
    """复用合法融合候选及内存评分 Stub，不解析或生成验收原件。"""
    incoming = candidates(count)
    return BGERerankerAdapter(service=ServiceStub(list(range(count))), top_n=5).postprocess_nodes(
        incoming, query_str="question")


def answer_for(nodes):
    """构造真实最终 Chunk ID 引用的最小输出，不造数据库/文档结果。"""
    return json.dumps({"insufficient_evidence": False, "claims": [
        {"text": "受证据约束的回答", "evidence_ids": [nodes[0].node.node_id]}]}, ensure_ascii=False)


def test_only_final_evidence_is_sent_and_citations_are_local_originals():
    """完整最终正文/原ID进入模型；页码/章节/分数/source_spans由原本地节点回填。"""
    nodes = final_nodes()
    before = copy.deepcopy([node.model_dump() for node in nodes])
    backend = LLMServiceStub(answer_for(nodes))
    synthesizer = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend))
    assert isinstance(synthesizer, BaseSynthesizer) and backend.calls == []
    result = synthesizer.synthesize("原始问题", nodes, allow_cloud=True)
    assert len(backend.calls) == 1 and backend.calls[0][0] == RAG_SYSTEM_PROMPT
    payload = json.loads(backend.calls[0][1])
    assert payload == {"question": "原始问题", "evidence": [
        {"evidence_id": node.node.node_id, "text": node.node.text} for node in nodes]}
    assert result.status == "success" and result.answer.endswith("[1]")
    original = node_to_result(nodes[0], log_success=False)
    assert result.citations[0].model_dump(exclude={"citation_id"}) == original.model_dump()
    assert result.citations[0].metadata["source_spans"] == original.metadata["source_spans"]
    assert [node.model_dump() for node in nodes] == before
    result.citations[0].metadata["source_spans"][0]["page"] = 999
    assert [node.model_dump() for node in nodes] == before


def test_empty_or_explicit_insufficiency_never_fabricates_sources():
    """空集不调模型；非空证据仍允许模型明确不足，固定文本无猜测或虚构引用。"""
    backend = LLMServiceStub('{"insufficient_evidence":true,"claims":[]}')
    synthesizer = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend))
    assert synthesizer.synthesize("question", [], allow_cloud=True) == insufficient_answer()
    assert backend.calls == []
    assert synthesizer.synthesize("question", final_nodes(), allow_cloud=True) == insufficient_answer()
    assert len(backend.calls) == 1


@pytest.mark.parametrize("kind", ["unknown_id", "extra_page", "no_citation", "duplicate_id", "nan",
    "fence", "inconsistent", "extra_answer", "duplicate_key", "wrong_bool", "blank_text", "inline_citation"])
def test_invalid_outputs_are_atomic_safe_failures(kind):
    """未知/非最终引用、模型来源字段、无引用事实与格式错误均拒绝，不修补输出。"""
    nodes = final_nodes()
    raw = json.loads(answer_for(nodes))
    if kind == "unknown_id":
        raw["claims"][0]["evidence_ids"] = [str(uuid4())]
    elif kind == "extra_page":
        raw["claims"][0]["page_start"] = 999
    elif kind == "no_citation":
        raw["claims"][0]["evidence_ids"] = []
    elif kind == "duplicate_id":
        raw["claims"][0]["evidence_ids"] *= 2
    elif kind == "inconsistent":
        raw["insufficient_evidence"] = True
    elif kind == "extra_answer":
        raw["answer"] = "PRIVATE_GUESS"
    elif kind == "wrong_bool":
        raw["insufficient_evidence"] = "false"
    elif kind == "blank_text":
        raw["claims"][0]["text"] = "  "
    elif kind == "inline_citation":
        raw["claims"][0]["text"] = "PRIVATE_GUESS [999]"
    raw = json.dumps(raw)
    if kind == "nan":
        raw = '{"insufficient_evidence":false,"claims":[],"PRIVATE":NaN}'
    elif kind == "fence":
        raw = "```json\n" + raw + "\n```"
    elif kind == "duplicate_key":
        raw = '{"insufficient_evidence":true,"insufficient_evidence":false,"claims":[]}'
    with pytest.raises(RAGError, match="RAG_OUTPUT") as failure:
        bind_answer(raw, [node_to_result(node, log_success=False) for node in nodes])
    assert "PRIVATE" not in str(failure.value)


def test_multiple_claims_reuse_local_citations_in_first_reference_order():
    """重复引用只保留一份原证据，编号按首次使用稳定生成而非由模型猜测。"""
    nodes = final_nodes()
    identities = [node.node.node_id for node in nodes]
    raw = json.dumps({"insufficient_evidence": False, "claims": [
        {"text": "事实甲", "evidence_ids": [identities[1], identities[0]]},
        {"text": "事实乙", "evidence_ids": [identities[1]]}]})
    result = bind_answer(raw, [node_to_result(node, log_success=False) for node in nodes])
    assert [str(item.chunk_id) for item in result.citations] == [identities[1], identities[0]]
    assert result.answer == "事实甲 [1][2]\n\n事实乙 [1]"


@pytest.mark.parametrize("kind", ["consent", "long", "duplicate", "not_ranked", "extra_nodes", "external_query"])
def test_bad_context_fails_before_cloud_and_without_truncation(kind):
    """未授权/超预算/重复或未重排证据/额外候选/变换Query不能进入云端。"""
    nodes, backend = final_nodes(), LLMServiceStub("PRIVATE_OUTPUT")
    query, kwargs = "question", {"allow_cloud": True}
    if kind == "consent":
        kwargs = {}
    elif kind == "long":
        backend.context_chars = 10
    elif kind == "duplicate":
        nodes[1] = nodes[0].model_copy(deep=True)
    elif kind == "not_ranked":
        nodes = candidates(2)
    elif kind == "extra_nodes":
        kwargs["additional_source_nodes"] = nodes
    else:
        query = QueryBundle(query_str="question", embedding=[0.0])
    with pytest.raises(RAGError):
        DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)).synthesize(query, nodes, **kwargs)
    assert backend.calls == []


def test_framework_default_callback_and_prompt_overrides_are_not_used(monkeypatch):
    """同步/异步只委托项目服务一次，不调用携带正文的基类入口或接受自由Prompt。"""
    def forbidden(*args, **kwargs):
        """框架默认合成一旦被调用即失败，不加载默认 LLM 或访问网络。"""
        pytest.fail("不得调用框架默认合成")

    monkeypatch.setattr(BaseSynthesizer, "synthesize", forbidden)
    nodes = final_nodes(1)
    backend = LLMServiceStub(answer_for(nodes))
    adapter = DKFDeepSeekAdapter(backend)
    synth = DKFResponseSynthesizer(adapter)
    response = asyncio.run(synth.asynthesize("question", nodes, allow_cloud=True))
    assert response.status == "success" and len(backend.calls) == 1
    assert "_service" not in adapter.model_dump()
    with pytest.raises(RAGError):
        synth.update_prompts({"text_qa_template": "PRIVATE_OVERRIDE"})
    with pytest.raises(RAGError):
        synth.get_response("question", ["body"])
    with pytest.raises(RAGError):
        adapter.chat([ChatMessage(role=MessageRole.SYSTEM, content="PRIVATE_OVERRIDE"),
                      ChatMessage(role=MessageRole.USER, content="JSON")])
    for method in (adapter.complete, adapter.stream_complete, adapter.stream_chat):
        with pytest.raises(RAGError):
            method("PRIVATE_PROMPT")
    assert len(backend.calls) == 1
