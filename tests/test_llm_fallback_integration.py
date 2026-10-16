"""用户运行的Fallback业务集成Mock；真实业务校验+内存来源/候选+HTTPX Mock，无真实模型/数据库/API。"""

import json
from uuid import uuid4

import httpx
import pytest

from app.services import formula as formula_service
from app.services.llm import CLOUD_CALL_ALLOWED, LLMError, create_llm_service
from test_formula import source, wire
from test_llm_fallback import config
from test_llm_service import envelope


@pytest.mark.parametrize("change", [None, "raw", "variables", "quote", "json"])
def test_formula_fallback_keeps_original_formula_variables_and_verified_sources(change, monkeypatch):
    """阶段27真实绑定校验仍执行；备用改原式/变量/引文或坏JSON都拒绝，不第三次请求。"""
    context, requests = source(), []
    reply = wire()
    if change == "raw":
        reply["raw_formula"] = "PRIVATE_FORGED_FORMULA"
    elif change == "variables":
        reply["variables"] = ["revenue"]
    elif change == "quote":
        reply["variable_meanings"]["profit"]["evidence"][0]["quote"] = "PRIVATE_MISSING_QUOTE"

    def respond(request):
        """主模型超时，备用只返回内存信封，问题/证据不外传真实服务。"""
        requests.append(request)
        if request.url.host == "compatible.example":
            raise httpx.ReadTimeout("PRIVATE", request=request)
        return httpx.Response(200, json=envelope("PRIVATE_BAD_JSON" if change == "json" else json.dumps(reply, ensure_ascii=False)))

    def factory():
        """真实统一工厂只使用内存Transport，禁止构造无Transport的HTTP服务。"""
        return create_llm_service(config(), transport=httpx.MockTransport(respond))

    monkeypatch.setattr(formula_service, "create_llm_service", factory)
    evidence, _, chunk, document, blocks = context
    service = formula_service.FormulaService(evidence_service=evidence)
    if change is None:
        result = service.extract_formula(chunk.kb_id, chunk.chunk_id, blocks[0].block_id,
                                         doc_id=document.doc_id, allow_cloud=True)
        assert result.status == "success" and result.raw_formula == blocks[0].text
        assert result.variables == ["profit", "revenue"] and result.source.doc_id == document.doc_id
        assert result.source.source_spans[0].block_id == blocks[0].block_id
    else:
        expected = "DEEPSEEK_RESPONSE" if change == "json" else "FORMULA_OUTPUT"
        with pytest.raises((formula_service.FormulaError, LLMError), match=expected):
            service.extract_formula(chunk.kb_id, chunk.chunk_id, blocks[0].block_id,
                                    doc_id=document.doc_id, allow_cloud=True)
    assert len(requests) == 2 and CLOUD_CALL_ALLOWED.get() is False
    assert json.loads(requests[0].content)["messages"] == json.loads(requests[1].content)["messages"]


@pytest.mark.parametrize("status", ["insufficient_evidence", "unsupported_formula"])
def test_primary_formula_insufficiency_or_unsupported_result_never_falls_back(status, monkeypatch):
    """业务不足/不支持不是运输故障；即便配置允许备用也不请求第二家修补定义。"""
    context, calls = source(), []
    reply = wire() | {"status": status, "formula_name": None, "normalized_formula": None,
                      "variables": [], "variable_meanings": {}, "formula_name_evidence": []}

    def respond(request):
        """首模型正常返回不足/不支持，禁止产生额外请求。"""
        calls.append(request)
        return httpx.Response(200, json=envelope(json.dumps(reply, ensure_ascii=False)))

    def factory():
        """仅注入内存Transport。"""
        return create_llm_service(config(), transport=httpx.MockTransport(respond))

    monkeypatch.setattr(formula_service, "create_llm_service", factory)
    evidence, _, chunk, document, blocks = context
    result = formula_service.FormulaService(evidence_service=evidence).extract_formula(
        chunk.kb_id, chunk.chunk_id, blocks[0].block_id, doc_id=document.doc_id, allow_cloud=True)
    assert result.status == status and len(calls) == 1


def test_formula_without_permission_never_constructs_or_calls_any_provider(monkeypatch):
    """本次未授权时，连统一工厂和当前来源查询都不应触发。"""
    calls = []

    def forbidden():
        """如果提前构造LLM则直接暴露越权缺陷。"""
        calls.append(True)
        pytest.fail("cloud permission must precede factory")

    monkeypatch.setattr(formula_service, "create_llm_service", forbidden)
    with pytest.raises(formula_service.FormulaError, match="FORMULA_CLOUD_PERMISSION"):
        formula_service.extract_formula(uuid4(), uuid4(), uuid4(), allow_cloud=False)
    assert calls == []


def rag_case(monkeypatch, *, invalid_citation=False, primary_success=False):
    """复用现有内存候选/评分Stub，真正执行Query Engine与Citation校验，不加载模型/读库。"""
    pytest.importorskip("llama_index.core.schema", reason="用户准备已有core；跳过不等于通过")
    from app.services.rag.llama_index.llm_adapter import DKFLLMAdapter
    from app.services.rag.llama_index.response_synthesizer import DKFResponseSynthesizer
    from test_rag_query_engine import setup_engine
    engine, retriever, scoring, _ = setup_engine()
    calls = []

    def respond(request):
        """从内存最终ID白名单构造模拟模型回答，绝不查询真实答案或服务。"""
        calls.append(request)
        if request.url.host == "compatible.example" and not primary_success:
            return httpx.Response(500, text="PRIVATE_ERROR")
        user_payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        selected = str(uuid4()) if invalid_citation else user_payload["evidence"][0]["evidence_id"]
        content = {"insufficient_evidence": False, "claims": [{"text": "内存证据支持的结论", "evidence_ids": [selected]}]}
        return httpx.Response(200, json=envelope(json.dumps(content, ensure_ascii=False)))

    service = create_llm_service(config(), transport=httpx.MockTransport(respond))
    engine._synthesizer = DKFResponseSynthesizer(DKFLLMAdapter(service))
    return engine, retriever, scoring, calls


@pytest.mark.parametrize("primary_success", [False, True])
def test_knowledge_agent_query_uses_same_rag_and_accepts_valid_fallback_citations(monkeypatch, primary_success):
    """真实LangGraph→原KnowledgeService→Query Engine→HTTP Mock；备用回答仍有合法原Citation。"""
    pytest.importorskip("langgraph.graph", reason="用户先安装LangGraph；跳过不是通过")
    from app.agents.knowledge import KnowledgeAgent
    from app.services import knowledge_query
    engine, retriever, scoring, calls = rag_case(monkeypatch, primary_success=primary_success)

    def query(kb_id, question, **kwargs):
        """只替换真实数据库/模型组件装配，保留原Query/RAG/引用校验业务。"""
        assert kb_id == retriever.kb_id and kwargs == {"doc_ids": None, "top_k": 30, "allow_cloud": True}
        return engine.query(question, allow_cloud=kwargs["allow_cloud"])

    monkeypatch.setattr(knowledge_query, "query_knowledge_base", query)
    result = KnowledgeAgent(max_steps=8).run({"kb_id": str(retriever.kb_id), "question": "PRIVATE_QUERY", "allow_cloud": True})
    assert result.status == "success" and result.selected_route == "knowledge_query" and result.step_count == 5
    assert result.answer.endswith("[1]") and result.evidence and result.evidence[0].metadata["source_spans"]
    assert all(item.kb_id == retriever.kb_id for item in result.evidence)
    assert len(calls) == (1 if primary_success else 2) and len(retriever.calls) == len(scoring.calls) == 1
    assert CLOUD_CALL_ALLOWED.get() is False


@pytest.mark.parametrize("primary_success", [False, True])
def test_unknown_rag_citation_never_triggers_repair_or_another_fallback(monkeypatch, primary_success):
    """主/备用响应违反原引用白名单时原样安全拒绝，不能通过换模型自动修补业务输出。"""
    from app.services.rag.synthesis import RAGError
    engine, _, _, calls = rag_case(monkeypatch, invalid_citation=True, primary_success=primary_success)
    with pytest.raises(RAGError, match="RAG_OUTPUT"):
        engine.query("PRIVATE_QUERY", allow_cloud=True)
    assert len(calls) == (1 if primary_success else 2)


def test_rag_without_permission_does_not_call_either_http_provider(monkeypatch):
    """原RAG许可检查仍早于检索/模型和主备请求，工厂开关不授予业务许可。"""
    from app.services.rag.synthesis import RAGError
    engine, retriever, scoring, calls = rag_case(monkeypatch)
    with pytest.raises(RAGError, match="RAG_CLOUD_PERMISSION"):
        engine.query("PRIVATE_QUERY", allow_cloud=False)
    assert calls == retriever.calls == scoring.calls == []
