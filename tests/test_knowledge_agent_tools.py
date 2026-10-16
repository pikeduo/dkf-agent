"""薄Tool/请求/配置的内存测试；无需LangGraph运行、真实来源、模型或云服务。"""

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.knowledge import tools as module
from app.core.config import Settings
from app.schemas.formula_binding import FormulaBindingRequest
from app.schemas.knowledge_agent import KnowledgeAgentRequest
from app.schemas.knowledge_query import KnowledgeQueryRequest, KnowledgeSearchRequest
from test_formula_binding import prepared, user


@pytest.mark.parametrize("querying", [False, True])
def test_search_query_tools_delegate_once_to_existing_service_with_exact_scope(querying, monkeypatch):
    """薄适配只创建已有请求契约；kb/doc/原问题/Top-K/云许可完整传递，不复制检索实现。"""
    from app.services import knowledge_query
    calls, sentinel = [], object()
    request = KnowledgeAgentRequest(kb_id=uuid4(), doc_ids=[uuid4()], question="PRIVATE_QUERY", top_k=17, allow_cloud=True)

    class ServiceStub:
        """代替正式服务，任何调用只记录契约，不进入Retriever或模型。"""

        def search(self, payload):
            """只捕获搜索请求。"""
            calls.append(("search", payload))
            return sentinel

        def query(self, payload):
            """只捕获问答请求。"""
            calls.append(("query", payload))
            return sentinel

    monkeypatch.setattr(knowledge_query, "KnowledgeService", ServiceStub)
    tool = module.KnowledgeTools()
    result = tool.query_knowledge_base(request) if querying else tool.search_knowledge_base(request)
    expected = KnowledgeQueryRequest if querying else KnowledgeSearchRequest
    assert result is sentinel and len(calls) == 1 and type(calls[0][1]) is expected
    payload = calls[0][1]
    assert (payload.kb_id, payload.doc_ids, payload.question, payload.top_k) == (
        request.kb_id, request.doc_ids, request.question, request.top_k)
    assert not querying or payload.allow_cloud is True


def test_formula_tools_only_delegate_to_original_extract_and_calculate(monkeypatch):
    """既有公式/绑定契约原样传递；薄层不添加HTTP客户端、SQL或SymPy求值。"""
    from app.services import formula, formula_calculation
    context, _, definition, _ = prepared()
    request = KnowledgeAgentRequest(kb_id=context[3].kb_id, doc_ids=[context[3].doc_id], question="q",
        intent="formula_request", allow_cloud=True, formula={"doc_id": context[3].doc_id,
            "chunk_id": context[2].chunk_id, "block_id": definition.formula_block_id,
            "user_inputs": [user("profit", "20")]})
    calls, sentinel = [], object()

    def extract(*args, **kwargs):
        """模拟原提取入口，不请求云。"""
        calls.append((args, kwargs))
        return definition

    def calculate(value):
        """模拟原计算入口，不执行SymPy或查询来源。"""
        calls.append(value)
        return sentinel

    monkeypatch.setattr(formula, "extract_formula", extract)
    monkeypatch.setattr(formula_calculation, "calculate_formula", calculate)
    tools = module.KnowledgeTools()
    assert tools.extract_formula(request) == definition
    assert tools.calculate_formula(request, definition) is sentinel
    assert calls[0] == ((request.kb_id, request.formula.chunk_id, request.formula.block_id),
                        {"doc_id": request.formula.doc_id, "allow_cloud": True})
    assert isinstance(calls[1], FormulaBindingRequest) and calls[1].formula == definition
    assert calls[1].user_inputs == request.formula.user_inputs and calls[1].evidence_inputs == []


@pytest.mark.parametrize("change", ["empty_docs", "zero_kb", "bad_intent", "missing_formula", "conflict", "formula_outside", "parameter_outside", "definition_kb"])
def test_invalid_agent_requests_reject_scope_or_intent_before_tools(change):
    """显式范围、公式与参数来源必须相容，不能利用auto/公式输入扩大权限或调用其他节点。"""
    context, _, definition, _ = prepared()
    data = {"kb_id": context[3].kb_id, "doc_ids": [context[3].doc_id], "question": "q", "intent": "formula_request",
        "formula": {"doc_id": context[3].doc_id, "chunk_id": context[2].chunk_id,
            "block_id": definition.formula_block_id}}
    if change == "empty_docs":
        data["doc_ids"] = []
    elif change == "zero_kb":
        data["kb_id"] = "00000000-0000-0000-0000-000000000000"
    elif change == "bad_intent":
        data["intent"] = "PRIVATE_NODE"
    elif change == "missing_formula":
        data.pop("formula")
    elif change == "conflict":
        data["intent"] = "knowledge_search"
    elif change == "formula_outside":
        data["formula"]["doc_id"] = uuid4()
    elif change == "parameter_outside":
        data["formula"]["evidence_inputs"] = [{"name": "profit", "doc_id": uuid4(), "chunk_id": uuid4(),
            "block_id": uuid4(), "quote": "x", "value_text": "1"}]
    else:
        definition.source.kb_id = uuid4()
        data["formula"]["definition"] = definition
    with pytest.raises(ValidationError):
        KnowledgeAgentRequest.model_validate(data)


@pytest.mark.parametrize("value", ["1", "33", "invalid"])
def test_agent_step_configuration_is_bounded(value, monkeypatch):
    """环境步数不允许零预算/无限值；严格业务调用另拒绝布尔型覆盖。"""
    monkeypatch.setenv("KNOWLEDGE_AGENT_MAX_STEPS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_tool_source_has_no_parallel_storage_http_or_math_implementation():
    """静态守卫薄Tool中不出现新Retriever/SQL/HTTP/计算/AgentExecutor等生产旁路。"""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    forbidden = {"eval", "exec", "execute", "AgentExecutor", "StateGraph", "PGVectorStore", "VectorStoreIndex",
        "BM25Retriever", "DenseRetriever", "Client", "AsyncClient", "sympify", "parse_expr", "Rational"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            assert name not in forbidden
