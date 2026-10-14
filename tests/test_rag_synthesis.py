"""用户执行的最终 Evidence/引用/Adapter 用例，只有内存 Node 与单次服务 Stub。"""

import asyncio
import copy
import io
import json
import logging
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="用户先准备 core/NLP；跳过不等于通过")

from llama_index.core.base.llms.types import ChatMessage, MessageRole
from llama_index.core.response_synthesizers.base import BaseSynthesizer
from llama_index.core.schema import QueryBundle

from app.core import business_logging as logs
from app.schemas.retrieval import RetrievalResult
from app.services.rag.llama_index.deepseek_adapter import DKFDeepSeekAdapter
from app.services.rag.llama_index.node_adapter import node_to_result, result_to_node
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


def quality_candidate(hit, text, *, page=95):
    """将内存候选设为用户报告场景的摘要Stub；非官方GT，不生成或解析真实文档。"""
    values = node_to_result(hit, log_success=False).model_dump()
    values.update(text=text, file_type="pptx", file_name="内存来源.pptx", page_start=page, page_end=page,
                  section="指标说明")
    values["metadata"]["page_semantics"] = "logical"
    for span in values["metadata"]["source_spans"]:
        span.update(page=page, start_char=0, end_char=len(text), chunk_start_char=0, chunk_end_char=len(text))
    return result_to_node(RetrievalResult(**values), log_success=False)


def test_supported_meaning_and_usage_with_noise_and_negative_logits_can_be_answered():
    """实质性名称/权衡信息不进入空分支；保留无关候选和负logit，不以分数代替语义支持。"""
    incoming = candidates(3)
    incoming[0] = quality_candidate(incoming[0],
        "An Information Criterion；Akaike's Information Criterion。它在拟合优度与模型灵活性（参数数量）之间做权衡。")
    incoming[1] = quality_candidate(incoming[1], "本节是无关内容的目录。", page=12)
    incoming[2] = quality_candidate(incoming[2], "AIC使用对未来数据的预期预测评估模型适切性。", page=97)
    original = copy.deepcopy([hit.model_dump() for hit in incoming])
    question = "文档中提到的 An Information Criterion 是什么？请根据文档说明其含义和用途"
    final = BGERerankerAdapter(service=ServiceStub([-1.0, -3.0, -2.0])).postprocess_nodes(
        incoming, query_str=question)
    ids = [incoming[index].node.node_id for index in (0, 2)]
    backend = LLMServiceStub(json.dumps({"insufficient_evidence": False, "claims": [
        {"text": "该准则权衡拟合优度与参数数量。", "evidence_ids": [ids[0]]},
        {"text": "模型适切性可按未来数据的预期预测评估。", "evidence_ids": [ids[1]]}]}))
    answer = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)).synthesize(question, final, allow_cloud=True)
    assert answer.status == "success" and not answer.insufficient_evidence
    assert len(backend.calls) == 1 and backend.calls[0][0] == RAG_SYSTEM_PROMPT
    assert "至少一个实质性部分" in RAG_SYSTEM_PROMPT and "忠实翻译、概括" in RAG_SYSTEM_PROMPT
    sent = json.loads(backend.calls[0][1])
    assert sent["question"] == question and len(sent["evidence"]) == 3
    assert {str(item.chunk_id) for item in answer.citations} == set(ids)
    assert [item.page_start for item in answer.citations] == [95, 97]
    assert all(item.metadata["page_semantics"] == "logical" and item.rerank_score < 0 for item in answer.citations)
    assert [hit.model_dump() for hit in incoming] == original


def test_partially_supported_question_keeps_grounded_claim_and_explicit_scope_limit():
    """额外子问题无证据不阻止有支持事实，局限附在对应回答内，不补数字或新增输出字段。"""
    incoming = [quality_candidate(candidates(1)[0], "指标权衡拟合优度与参数数量。")]
    final = BGERerankerAdapter(service=ServiceStub([0.0])).postprocess_nodes(incoming, query_str="question")
    backend = LLMServiceStub(json.dumps({"insufficient_evidence": False, "claims": [
        {"text": "指标权衡拟合优度与参数数量；当前证据未提供训练数据的具体行数。",
         "evidence_ids": [final[0].node.node_id]}]}))
    result = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)).synthesize(
        "说明指标含义，并提供训练数据的具体行数。", final, allow_cloud=True)
    assert result.status == "success" and result.answer.endswith("[1]")
    assert "未提供" in result.answer and len(result.citations) == 1 and len(backend.calls) == 1


@pytest.mark.parametrize("passage", ["AIC", "无关文档的目录。", "两段相关证据的数值相互冲突，无法判定。"])
def test_nonempty_or_keyword_hit_never_forces_model_refusal_into_success(passage):
    """非空、关键词命中或模型明确相关冲突仍能安全拒答，不强制覆盖模型true或重试。"""
    incoming = [quality_candidate(candidates(1)[0], passage)]
    final = BGERerankerAdapter(service=ServiceStub([20.0])).postprocess_nodes(incoming, query_str="question")
    backend = LLMServiceStub('{"insufficient_evidence":true,"claims":[]}')
    result = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)).synthesize(
        "解释指标的含义和用途。", final, allow_cloud=True)
    assert result == insufficient_answer() and len(backend.calls) == 1


def test_local_conflict_does_not_block_independent_supported_claim():
    """局部数值冲突不强制否定独立名称事实；只接受模型已合法引用的受支持部分。"""
    incoming = candidates(3)
    passages = ["指标名称为示例信息准则。", "文段甲说参数值为2。", "文段乙说相同参数值为3。"]
    incoming = [quality_candidate(hit, passage) for hit, passage in zip(incoming, passages)]
    final = BGERerankerAdapter(service=ServiceStub([3.0, 2.0, 1.0])).postprocess_nodes(incoming, query_str="question")
    backend = LLMServiceStub(json.dumps({"insufficient_evidence": False, "claims": [
        {"text": "名称为示例信息准则；当前证据的参数值存在冲突，不能判定。",
         "evidence_ids": [item.node.node_id for item in final]}]}))
    result = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend)).synthesize(
        "说明指标名称及参数值。", final, allow_cloud=True)
    assert result.status == "success" and "不能判定" in result.answer and len(backend.calls) == 1
    assert {str(cite.chunk_id) for cite in result.citations} == {hit.node.node_id for hit in final}


def test_refusal_diagnostics_distinguish_empty_from_model_judgment_without_payload():
    """日志可分辨空证据/非空模型拒答，记录原数量但不泄露问题、正文或响应。"""
    logger, stream = logs.LOGGER, io.StringIO()
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter("rag-cli"))
    token = logs.CONTEXT.set(None)
    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        backend = LLMServiceStub('{"insufficient_evidence":true,"claims":[]}')
        synthesizer = DKFResponseSynthesizer(DKFDeepSeekAdapter(backend))
        assert synthesizer.synthesize("PRIVATE_QUERY", [], allow_cloud=True) == insufficient_answer()
        empty = stream.getvalue()
        assert '阶段="empty_final_evidence"' in empty and "Chunk数=0" in empty
        assert "rag.synthesis_started" not in empty and backend.calls == []
        stream.seek(0)
        stream.truncate(0)
        incoming = [quality_candidate(candidates(1)[0], "PRIVATE_BODY")]
        final = BGERerankerAdapter(service=ServiceStub([1.0])).postprocess_nodes(incoming, query_str="PRIVATE_QUERY")
        assert synthesizer.synthesize("PRIVATE_QUERY", final, allow_cloud=True) == insufficient_answer()
        refused = stream.getvalue()
        assert "rag.synthesis_started" in refused and '阶段="model_judged_insufficient"' in refused
        assert "Chunk数=1" in refused and len(backend.calls) == 1
        assert "PRIVATE" not in empty + refused and "claims" not in refused
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
