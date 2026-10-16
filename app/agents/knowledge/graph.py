"""有限状态Knowledge Agent；LangGraph仅编排，所有业务经项目Tool委托。"""

import json
import logging
from time import perf_counter
from typing import TypedDict
from uuid import uuid4

from app.agents.knowledge.tools import KnowledgeTools
from app.core.business_logging import CONTEXT, log_event
from app.core.config import get_settings
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_calculation import FormulaCalculationResult
from app.schemas.knowledge_agent import KnowledgeAgentRequest, KnowledgeAgentResponse
from app.schemas.knowledge_query import KnowledgeSearchResponse
from app.schemas.rag import INSUFFICIENT_ANSWER, RAGAnswer

ROUTES = {"knowledge_search", "knowledge_query", "formula_request"}
ERROR_CODES = {"AGENT_INPUT", "AGENT_PERMISSION", "AGENT_ROUTE", "AGENT_STEPS",
               "AGENT_DEPENDENCY", "AGENT_TOOL", "AGENT_RESULT", "AGENT_INTERNAL"}


class AgentState(TypedDict, total=False):
    """状态只保存项目JSON值；无Session、异常、模型、框架Node或回调对象，不持久化。"""

    request: dict
    question: str
    kb_id: str
    doc_ids: list[str] | None
    intent: str
    agent_run_id: str
    selected_route: str | None
    evidence: list[dict]
    answer: str | None
    formula_results: list[dict]
    formula_definition: dict | None
    insufficient_evidence: bool
    status: str
    error_code: str | None
    step_count: int


class AgentError(Exception):
    """Agent仅公开固定错误码；原始Tool异常仍可从关联的脱敏业务事件定位。"""

    def __init__(self, code):
        """未知代码降为内部错误，禁止透传外部异常.code/str。"""
        self.code = code if code in ERROR_CODES else "AGENT_INTERNAL"
        super().__init__(self.code)


def select_route(request):
    """显式规则而非LLM路由：选公式才进公式；auto普通问题默认Query，不猜意图。"""
    if request.formula is not None:
        return "formula_request"
    return "knowledge_query" if request.intent == "auto" else request.intent


def failed(code):
    """失败原子清空业务半成品；不能把异常洗成空搜索、证据不足或计算成功。"""
    return {"status": "failed", "error_code": code, "answer": None, "evidence": [],
            "formula_results": [], "formula_definition": None, "insufficient_evidence": False}


def checked_model(value, schema):
    """复用既有安全响应复制/非有限数校验，状态只接收序列化的项目契约。"""
    from app.services.knowledge_query import _checked_response
    try:
        return _checked_response(value, schema)
    except Exception:
        raise AgentError("AGENT_RESULT") from None


def check_scope(request, items):
    """整批拒绝越界/重复Chunk来源，不删行冒充成功，不隐式扩大doc_ids。"""
    seen = set()
    for item in items:
        if (item.kb_id != request.kb_id or item.doc_id.int == 0 or item.chunk_id.int == 0
                or item.chunk_id in seen or request.doc_ids is not None and item.doc_id not in request.doc_ids):
            raise AgentError("AGENT_RESULT")
        seen.add(item.chunk_id)


class KnowledgeAgent:
    """每次run构造独立有限图，无自动重试/检查点重放，不新增Agent Runtime或框架RAG。"""

    def __init__(self, *, tools=None, max_steps=None, route_selector=None):
        """允许测试注入薄Tool/路由；默认步数运行时读配置，构造不导入LangGraph或加载模型。"""
        self._tools = tools if tools is not None else KnowledgeTools()
        self._max_steps = max_steps
        self._route_selector = route_selector if route_selector is not None else select_route

    def _call_tool(self, name, request, state, *args):
        """一个节点只调用一次既有Tool；记录固定工具名/耗时，不记录参数、返回值或异常原文。"""
        started = perf_counter()
        log_event("agent.tool_started", tool_name=name, step_count=state["step_count"])
        try:
            # Tool不能通过修改传入模型影响本节点之后的范围/定义一致性判断。
            value = getattr(self._tools, name)(request.model_copy(deep=True),
                                             *[item.model_copy(deep=True) for item in args])
        except Exception:
            log_event("agent.tool_failed", level=logging.WARNING, tool_name=name,
                tool_duration_ms=(perf_counter() - started) * 1000, step_count=state["step_count"], error_code="AGENT_TOOL")
            raise AgentError("AGENT_TOOL") from None
        log_event("agent.tool_finished", tool_name=name, step_count=state["step_count"],
                  tool_duration_ms=(perf_counter() - started) * 1000)
        return value

    def _compile(self, max_steps, progress):
        """惰性构造StateGraph及固定边；无循环/默认RetryPolicy/持久化Checkpointer/Node Cache。"""
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError:
            raise AgentError("AGENT_DEPENDENCY") from None

        def validate_input(state):
            """重核自有请求，预先检查完整路径预算/本次云许可，避免调用后才发现步数不足。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            route = select_route(request)
            if max_steps < (6 if route == "formula_request" else 5):
                raise AgentError("AGENT_STEPS")
            if (route == "knowledge_query" or route == "formula_request" and request.formula.definition is None
                    ) and request.allow_cloud is not True:
                raise AgentError("AGENT_PERMISSION")
            return {}

        def route_request(state):
            """路由只能为固定枚举且不能偏离调用者明确意图；非法输出直接安全终止。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            route = self._route_selector(request)
            if not isinstance(route, str) or route not in ROUTES or route != select_route(request):
                raise AgentError("AGENT_ROUTE")
            log_event("agent.routed", selected_route=route, step_count=state["step_count"])
            return {"selected_route": route}

        def knowledge_search(state):
            """委托原Search服务并保持原始DTO/范围；空结果不是异常，不另执行公式或LLM。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            result = checked_model(self._call_tool("search_knowledge_base", request, state), KnowledgeSearchResponse)
            check_scope(request, result.evidence)
            if len(result.evidence) > min(5, request.top_k):
                raise AgentError("AGENT_RESULT")
            return {"status": result.status, "evidence": [item.model_dump(mode="json") for item in result.evidence]}

        def knowledge_query(state):
            """一次委托原Query/RAG/引用白名单，不再次检索、合成或修改不足判断。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            result = checked_model(self._call_tool("query_knowledge_base", request, state), RAGAnswer)
            check_scope(request, result.citations)
            if len(result.citations) > min(5, request.top_k):
                raise AgentError("AGENT_RESULT")
            return {"status": result.status, "answer": result.answer,
                "evidence": [item.model_dump(mode="json") for item in result.citations],
                "insufficient_evidence": result.insufficient_evidence}

        def extract_formula(state):
            """显式提取一次或复用成功定义；不足/不支持直接结束，不补式或自动重试收费。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            selected = request.formula
            if selected.definition is not None:
                result = selected.definition
                log_event("agent.tool_skipped", tool_name="extract_formula", step_count=state["step_count"])
            else:
                result = self._call_tool("extract_formula", request, state)
            result = checked_model(result, FormulaSemanticResult)
            check_scope(request, [result.source])
            if (result.source.doc_id != selected.doc_id or result.source.chunk_id != selected.chunk_id
                    or result.formula_block_id != selected.block_id):
                raise AgentError("AGENT_RESULT")
            output = {"formula_definition": result.model_dump(mode="json")}
            if result.status != "success":
                output |= {"status": result.status, "formula_results": [result.model_dump(mode="json")],
                    "evidence": [result.source.model_dump(mode="json")],
                    "insufficient_evidence": result.status == "insufficient_evidence"}
            return output

        def calculate_formula(state):
            """复用原绑定/安全计算；参数证据仍在同kb/doc范围，不猜值或再调用模型。"""
            request = KnowledgeAgentRequest.model_validate(state["request"])
            definition = FormulaSemanticResult.model_validate(state["formula_definition"])
            result = checked_model(self._call_tool("calculate_formula", request, state, definition), FormulaCalculationResult)
            if result.formula != definition:
                raise AgentError("AGENT_RESULT")
            traces = {result.formula_source.chunk_id: result.formula_source}
            for value in result.variables:
                if value.source.kind == "document_evidence":
                    trace = value.source.trace
                    if trace.chunk_id in traces and traces[trace.chunk_id] != trace:
                        raise AgentError("AGENT_RESULT")
                    traces[trace.chunk_id] = trace
            check_scope(request, list(traces.values()))
            return {"status": result.status, "formula_results": [result.model_dump(mode="json")],
                    "evidence": [trace.model_dump(mode="json") for trace in traces.values()]}

        def synthesize_or_return(state):
            """原Query已有最终答案；公式仅固定过程说明，不额外请求LLM或编造解释/单位。"""
            if state["selected_route"] != "formula_request":
                return {}
            answers = {"success": "已完成精确算术；公式语义与单位仍需人工核验。",
                "need_variables": "公式仍缺少显式参数，未执行计算。",
                "unsupported_formula": "该公式不在当前支持的计算范围内。",
                "insufficient_evidence": INSUFFICIENT_ANSWER}
            return {"answer": answers[state["status"]]}

        def finalize_response(state):
            """只发布符合DKF协议的最终结果，框架State/异常或部分成功不向调用方泄漏。"""
            self._response(state)
            return {"formula_definition": None}

        def guarded(name, function):
            """逐节点计步并保留终结槽位；任何异常进入固定失败状态，不重放收费节点。"""
            def node(state):
                """状态只做JSON深复制，业务异常收敛到安全码并清空半成品。"""
                current = dict(state)
                if name != "finalize_response" and current["step_count"] >= max_steps - 1:
                    current.update(failed("AGENT_STEPS"))
                else:
                    current["step_count"] = min(max_steps, current["step_count"] + 1)
                    try:
                        current.update(function(current))
                    except Exception as exc:
                        current.update(failed(exc.code if isinstance(exc, AgentError) else "AGENT_INTERNAL"))
                # 严格JSON只验证内存状态，不输出正文；终结后不保留可重放的公式检查点。
                current = json.loads(json.dumps(current, ensure_ascii=False, allow_nan=False))
                progress.clear()
                progress.update(current)
                log_event("agent.step", stage=name, step_count=current["step_count"],
                          selected_route=current["selected_route"], error_code=current["error_code"])
                return current
            return node

        def after_validation(state):
            """校验失败直达终结，成功才路由，不因异常扩大工具权限。"""
            return "finalize_response" if state["status"] == "failed" else "route_request"

        def after_route(state):
            """分支目标仅取固定表，绝不将外部字符串作为LangGraph节点名。"""
            return "finalize_response" if state["status"] == "failed" else {
                "knowledge_search": "knowledge_search", "knowledge_query": "knowledge_query",
                "formula_request": "extract_formula"}[state["selected_route"]]

        def after_extraction(state):
            """只有成功定义进入计算；不足/不支持不执行计算，异常直接终结。"""
            if state["status"] == "failed":
                return "finalize_response"
            return "calculate_formula" if state["status"] == "running" else "synthesize_or_return"

        def after_tool(state):
            """Tool失败不合成正常回答，其他状态只返回已有结果。"""
            return "finalize_response" if state["status"] == "failed" else "synthesize_or_return"

        builder = StateGraph(AgentState)
        for name, function in (("validate_input", validate_input), ("route_request", route_request),
                ("knowledge_search", knowledge_search), ("knowledge_query", knowledge_query),
                ("extract_formula", extract_formula), ("calculate_formula", calculate_formula),
                ("synthesize_or_return", synthesize_or_return), ("finalize_response", finalize_response)):
            builder.add_node(name, guarded(name, function))
        builder.add_edge(START, "validate_input")
        builder.add_conditional_edges("validate_input", after_validation, ["route_request", "finalize_response"])
        builder.add_conditional_edges("route_request", after_route,
            ["knowledge_search", "knowledge_query", "extract_formula", "finalize_response"])
        builder.add_conditional_edges("extract_formula", after_extraction,
            ["calculate_formula", "synthesize_or_return", "finalize_response"])
        for name in ("knowledge_search", "knowledge_query", "calculate_formula"):
            builder.add_conditional_edges(name, after_tool, ["synthesize_or_return", "finalize_response"])
        builder.add_edge("synthesize_or_return", "finalize_response")
        builder.add_edge("finalize_response", END)
        return builder.compile(debug=False)

    def _response(self, state):
        """只挑选稳定业务字段，重新校验JSON响应；内部请求与临时定义不泄漏到前端。"""
        return KnowledgeAgentResponse.model_validate({name: state[name] for name in
            ("status", "answer", "evidence", "formula_results", "insufficient_evidence", "agent_run_id",
             "selected_route", "error_code", "step_count")})

    def run(self, payload: KnowledgeAgentRequest | dict) -> KnowledgeAgentResponse:
        """同步单次运行；默认不许可外传，不接受调用方框架config/callback或恢复旧状态。"""
        started = perf_counter()
        state = {"agent_run_id": str(uuid4()), "status": "running", "step_count": 0,
            "selected_route": None, "error_code": None, "answer": None, "evidence": [],
            "formula_results": [], "formula_definition": None, "insufficient_evidence": False}
        token = CONTEXT.set((CONTEXT.get() or {}) | {"agent_run_id": state["agent_run_id"]})
        try:
            log_event("agent.started", step_count=0)
            try:
                raw = payload.model_dump(mode="python") if isinstance(payload, KnowledgeAgentRequest) else payload
                request = KnowledgeAgentRequest.model_validate(raw)
                state.update(request=request.model_dump(mode="json"), question=request.question,
                    kb_id=str(request.kb_id), doc_ids=[str(value) for value in request.doc_ids] if request.doc_ids else None,
                    intent=request.intent)
                max_steps = self._max_steps if self._max_steps is not None else get_settings().knowledge_agent_max_steps
                if type(max_steps) is not int or not 2 <= max_steps <= 32:
                    raise ValueError
            except Exception:
                raise AgentError("AGENT_INPUT") from None
            # 不允许自动追踪把完整State/Tool参数发往LangSmith，不修改全局环境变量。
            try:
                from langsmith import tracing_context
            except ImportError:
                raise AgentError("AGENT_DEPENDENCY") from None
            with tracing_context(enabled=False):
                graph = self._compile(max_steps, state)
                outcome = graph.invoke(dict(state), config={"callbacks": [], "recursion_limit": max_steps + 2})
                result = self._response(outcome)
        except Exception as exc:
            state.update(failed(exc.code if isinstance(exc, AgentError) else "AGENT_INTERNAL"))
            result = self._response(state)
        finally:
            # 正常与安全失败均有固定终态；外层Context不会被Agent永久覆盖。
            log_event("agent.finished", final_status=state["status"], selected_route=state["selected_route"],
                step_count=state["step_count"], error_code=state["error_code"], duration_ms=(perf_counter() - started) * 1000)
            CONTEXT.reset(token)
        return result


def run_knowledge_agent(payload: KnowledgeAgentRequest | dict) -> KnowledgeAgentResponse:
    """统一项目入口，未来Coordinator无需依赖LangGraph类型或检索实现。"""
    return KnowledgeAgent().run(payload)
