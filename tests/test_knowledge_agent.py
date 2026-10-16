"""用户执行的真实LangGraph/Stub业务测试；不连接数据库、模型、HTTP或生成文档。"""

import copy
import io
import json
import logging
from uuid import uuid4

import pytest

pytest.importorskip("langgraph.graph", reason="用户先安装已声明LangGraph；跳过不是验收通过")
pytest.importorskip("langsmith", reason="LangGraph运行时必须能够显式禁用默认追踪")

from app.agents.knowledge.graph import KnowledgeAgent
from app.core import business_logging as logs
from app.schemas.evidence import Citation
from app.schemas.formula_calculation import CalculationValue, FormulaCalculationResult
from app.schemas.knowledge_agent import KnowledgeAgentRequest, KnowledgeAgentResponse
from app.schemas.knowledge_query import KnowledgeSearchResponse
from app.schemas.rag import INSUFFICIENT_ANSWER, RAGAnswer
from app.schemas.retrieval import RetrievalResult
from test_formula_binding import prepared, user


def evidence():
    """构造仅内存DTO以核对原字段透传，不冒充经过真实数据库验证的来源。"""
    return RetrievalResult(kb_id=uuid4(), doc_id=uuid4(), chunk_id=uuid4(), file_name="memory.pdf", file_type="pdf",
        text="PRIVATE_EVIDENCE", page_start=2, page_end=3, section="原章节", retrieval_score=0.2,
        metadata={"page_semantics": "physical_pdf", "source_spans": [{"block_id": str(uuid4()),
            "page": 2, "bbox": [1.0, 2.0, 3.0, 4.0], "role": "content", "block_text_hash": "a" * 64}]})


def query_case():
    """准备带合法原Citation的问答Stub，固定kb/doc范围和未经改写的原问题。"""
    item = evidence()
    answer = RAGAnswer(status="success", answer="PRIVATE_ANSWER [1]",
        citations=[Citation(**item.model_dump(), citation_id=1)], insufficient_evidence=False)
    request = KnowledgeAgentRequest(kb_id=item.kb_id, doc_ids=[item.doc_id], question="PRIVATE_QUERY", allow_cloud=True)
    return request, answer


def formula_case(*, missing=False):
    """复用已有内存来源绑定，不运行SymPy/云提取；计算结果由薄Tool的Stub提供。"""
    context, _, definition, binding = prepared()
    values = [user("profit", "20")] if missing else [user("profit", "20"), user("revenue", "100")]
    bound = binding.bind_formula_variables(definition, user_inputs=values)
    result = FormulaCalculationResult(status=bound.status, calculation_id=None if missing else uuid4(), formula=definition,
        variables=bound.variables, substitution={item.name: item.value for item in bound.variables},
        substituted_formula=None if missing else "((20) / (100)) * 100",
        result=None if missing else CalculationValue(exact="20", decimal="20", rounded=False),
        formula_source=definition.source, variable_sources={item.name: item.source for item in bound.variables},
        required_variables=bound.required_variables, steps=[], engine_version=None if missing else "1.14.0")
    request = KnowledgeAgentRequest(kb_id=context[3].kb_id, doc_ids=[context[3].doc_id], question="PRIVATE_CALC",
        intent="formula_request", allow_cloud=True, formula={"doc_id": context[3].doc_id,
            "chunk_id": context[2].chunk_id, "block_id": definition.formula_block_id, "user_inputs": values})
    return request, definition, result


class ToolsStub:
    """记录四类薄Tool调用；任何错误/输出均在内存，绝不访问正式服务。"""

    def __init__(self, *, answer=None, search=None, formula=None, computed=None, error=None):
        """固定每个分支的响应和可选失败工具名，捕获调用次数。"""
        self.answer, self.search, self.formula, self.computed = answer, search, formula, computed
        self.error, self.calls = error, []

    def _result(self, name, request, value):
        """复制结果避免共享状态，指定故障仅抛私有异常供脱敏断言。"""
        self.calls.append((name, request.model_copy(deep=True)))
        if self.error == name:
            raise RuntimeError("PRIVATE_KEY PRIVATE_URL PRIVATE_STACK")
        return copy.deepcopy(value)

    def search_knowledge_base(self, request):
        """搜索只返回自有DTO，不执行真实检索。"""
        return self._result("search_knowledge_base", request, self.search)

    def query_knowledge_base(self, request):
        """问答只返回原RAGAnswer，不构造LLM。"""
        return self._result("query_knowledge_base", request, self.answer)

    def extract_formula(self, request):
        """公式只返回既有内存定义，不请求云服务。"""
        return self._result("extract_formula", request, self.formula)

    def calculate_formula(self, request, definition):
        """核对原定义后返回已声明Stub，不执行数学引擎。"""
        assert definition == self.formula
        return self._result("calculate_formula", request, self.computed)


def test_normal_query_preserves_citation_scope_and_calls_only_query_once():
    """普通含“公式”字样的问题也不额外提取；原文/三端来源/引用编号完整保留。"""
    request, answer = query_case()
    request.question = "文档提到的公式是什么意思？"
    tools = ToolsStub(answer=answer)
    agent = KnowledgeAgent(tools=tools, max_steps=8)
    result = agent.run(request)
    assert result.status == "success" and result.selected_route == "knowledge_query" and result.step_count == 5
    assert result.answer == answer.answer and result.evidence == answer.citations and result.formula_results == []
    assert [name for name, _ in tools.calls] == ["query_knowledge_base"]
    assert tools.calls[0][1] == request
    result.evidence[0].metadata["source_spans"][0]["page"] = 99
    assert answer.citations[0].metadata["source_spans"][0]["page"] == 2


@pytest.mark.parametrize("empty", [False, True])
def test_explicit_search_does_not_require_cloud_or_call_formula_or_query(empty):
    """显式只检索保留kb/doc/Top-K，空集返回empty而非证据不足或错误。"""
    item = evidence()
    request = KnowledgeAgentRequest(kb_id=item.kb_id, doc_ids=[item.doc_id], question="q",
                                    intent="knowledge_search", top_k=7)
    tools = ToolsStub(search=KnowledgeSearchResponse(status="empty" if empty else "success", evidence=[] if empty else [item]))
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == ("empty" if empty else "success") and result.answer is None
    assert not result.insufficient_evidence and result.step_count == 5
    assert tools.calls == [("search_knowledge_base", request)]
    assert result.evidence == ([] if empty else [item])


@pytest.mark.parametrize("missing", [False, True])
def test_formula_extraction_then_calculation_keep_original_sources_and_missing_values(missing):
    """公式分支各Tool至多一次，缺参不猜值；源Trace/公式/变量全部保留。"""
    request, definition, computed = formula_case(missing=missing)
    tools = ToolsStub(formula=definition, computed=computed)
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == ("need_variables" if missing else "success") and result.step_count == 6
    assert result.formula_results == [computed] and result.evidence == [definition.source]
    assert [name for name, _ in tools.calls] == ["extract_formula", "calculate_formula"]
    assert all(payload.kb_id == request.kb_id and payload.doc_ids == request.doc_ids for _, payload in tools.calls)
    assert result.formula_results[0].required_variables == (["revenue"] if missing else [])


def test_reusing_definition_does_not_request_cloud_again():
    """补齐参数复用已有定义时不授予云权限、不再提取，仍委托正式计算核对来源。"""
    request, definition, computed = formula_case()
    request.formula.definition, request.allow_cloud = definition, False
    tools = ToolsStub(formula=definition, computed=computed)
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == "success" and [name for name, _ in tools.calls] == ["calculate_formula"]


@pytest.mark.parametrize("status", ["insufficient_evidence", "unsupported_formula"])
def test_unfinished_formula_never_calls_calculation(status):
    """模型明确不足/不支持保留安全分支，不用常见公式修补或自动收费重试。"""
    request, definition, _ = formula_case()
    definition = type(definition).model_validate(definition.model_dump() | {"status": status, "formula_name": None,
        "normalized_formula": None, "variables": [], "variable_meanings": {}, "formula_name_evidence": []})
    tools = ToolsStub(formula=definition)
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == status and result.insufficient_evidence == (status == "insufficient_evidence")
    assert [name for name, _ in tools.calls] == ["extract_formula"]


def test_insufficient_query_is_not_repaired_or_retried():
    """原RAG的证据不足判断保持不变，Agent不因候选非空强制生成答案。"""
    request, _ = query_case()
    tools = ToolsStub(answer=RAGAnswer(status="insufficient_evidence", answer=INSUFFICIENT_ANSWER,
                                    insufficient_evidence=True, citations=[]))
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == "insufficient_evidence" and result.insufficient_evidence and result.evidence == []
    assert len(tools.calls) == 1


@pytest.mark.parametrize("name", ["query_knowledge_base", "search_knowledge_base", "extract_formula", "calculate_formula"])
def test_each_tool_failure_is_structured_atomic_and_never_retried(name):
    """Tool任意私有异常只返回AGENT_TOOL，清空半成品、无默认重试/换提供方。"""
    if name in {"extract_formula", "calculate_formula"}:
        request, definition, computed = formula_case()
        tools = ToolsStub(formula=definition, computed=computed, error=name)
    else:
        request, answer = query_case()
        request.intent = "knowledge_search" if name == "search_knowledge_base" else "knowledge_query"
        tools = ToolsStub(answer=answer, error=name)
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.status == "failed" and result.error_code == "AGENT_TOOL"
    assert result.answer is None and result.evidence == result.formula_results == [] and not result.insufficient_evidence
    assert [item for item, _ in tools.calls].count(name) == 1 and "PRIVATE" not in result.model_dump_json()


@pytest.mark.parametrize("route", ["not-a-node", "extract_formula", None, {}, "knowledge_search"])
def test_invalid_or_intent_changing_route_cannot_invoke_tools(route):
    """非法枚举/有效但偏离明确意图的路由都安全终结，不动态生成节点名。"""
    request, _ = query_case()
    tools = ToolsStub()

    def selector(payload):
        """注入受控坏路由，不运行分类LLM。"""
        return route

    result = KnowledgeAgent(tools=tools, max_steps=8, route_selector=selector).run(request)
    assert result.error_code == "AGENT_ROUTE" and result.status == "failed" and tools.calls == []


@pytest.mark.parametrize("limit,expected", [(2, "AGENT_STEPS"), (4, "AGENT_STEPS"), (33, "AGENT_INPUT"), (True, "AGENT_INPUT")])
def test_budget_is_checked_before_any_paid_tool(limit, expected):
    """完整路径预算不足时收费前失败，非法配置不被当成允许无限Agent循环。"""
    request, _ = query_case()
    tools = ToolsStub()
    result = KnowledgeAgent(tools=tools, max_steps=limit).run(request)
    assert result.error_code == expected and tools.calls == []
    assert result.step_count <= (limit if type(limit) is int and limit <= 32 else 0)


@pytest.mark.parametrize("formula", [False, True])
def test_cloud_permission_is_required_for_query_and_fresh_extraction(formula):
    """每次云许可默认false；不能因框架/Tool存在而隐式授权外传。"""
    request = formula_case()[0] if formula else query_case()[0]
    request.allow_cloud = False
    tools = ToolsStub()
    result = KnowledgeAgent(tools=tools, max_steps=8).run(request)
    assert result.error_code == "AGENT_PERMISSION" and tools.calls == []


@pytest.mark.parametrize("change", ["kb", "doc", "duplicate", "nonfinite", "citation"])
def test_bad_tool_results_are_rejected_without_partial_evidence(change):
    """跨库/跨文档、重复/坏引用或非有限metadata全批拒绝，不靠删除越界项冒充成功。"""
    request, answer = query_case()
    if change == "kb":
        answer.citations[0].kb_id = uuid4()
    elif change == "doc":
        answer.citations[0].doc_id = uuid4()
    elif change == "duplicate":
        answer.citations.append(answer.citations[0])
    elif change == "nonfinite":
        answer.citations[0].metadata["bad"] = float("nan")
    else:
        answer.answer = "PRIVATE_ANSWER [2]"
    result = KnowledgeAgent(tools=ToolsStub(answer=answer), max_steps=8).run(request)
    assert result.error_code == "AGENT_RESULT" and result.evidence == [] and result.answer is None


def test_json_state_has_no_framework_objects_and_repeated_runs_are_independent():
    """实际图节点保留纯JSON State，两个run身份不同，不保留跨请求的临时定义/结果。"""
    request, answer = query_case()

    class CapturingAgent(KnowledgeAgent):
        """测试仅观察内部状态类型，不导出敏感运行状态。"""

        def _compile(self, max_steps, progress):
            """复用真实StateGraph构造，记录本次内存进度供断言。"""
            self.observed = progress
            return super()._compile(max_steps, progress)

    agent = CapturingAgent(tools=ToolsStub(answer=answer), max_steps=8)
    first, second = agent.run(request), agent.run(request)
    assert first.agent_run_id != second.agent_run_id and first.evidence == second.evidence
    assert json.loads(json.dumps(agent.observed, ensure_ascii=False, allow_nan=False)) == agent.observed
    assert agent.observed["formula_definition"] is None
    assert KnowledgeAgentResponse.model_validate_json(second.model_dump_json()) == second


def test_default_tracing_is_disabled_and_agent_logs_only_safe_workflow_fields(monkeypatch):
    """运行边界禁用追踪，日志仅ID/固定路由工具/步数耗时，且恢复原请求上下文。"""
    import langsmith
    request, answer = query_case()
    output, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("knowledge-agent-cli"))
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set({"request_id": str(uuid4())})
    original_context = logs.CONTEXT.get().copy()
    tracing, real_context = [], langsmith.tracing_context

    def disabled_context(**options):
        """记录显式False并沿用官方禁用上下文，不构造Tracing客户端。"""
        tracing.append(options)
        return real_context(**options)

    monkeypatch.setattr(langsmith, "tracing_context", disabled_context)
    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        result = KnowledgeAgent(tools=ToolsStub(answer=answer), max_steps=8).run(request)
        content = output.getvalue()
        assert result.status == "success" and tracing == [{"enabled": False}]
        assert all(key in content for key in ("agent_run_id", "selected_route", "tool_name", "tool_duration_ms", "step_count", "final_status"))
        assert "Knowledge Agent CLI" in content and str(result.agent_run_id) in content and "PRIVATE" not in content
        assert logs.CONTEXT.get() == original_context
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
