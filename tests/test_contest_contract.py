"""冻结契约的结构边界用例；不调用工具、服务、数据库、模型或文档解析器。"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.rag import INSUFFICIENT_ANSWER, RAGAnswer
from contracts.contest_v1 import (
    AgentRequest, AgentResponse, ClarificationAnswer, ClarificationPrompt, ClarificationQuestion,
    ClarificationReply, CloudAuthorization, ConfirmedSlot, KnowledgeScope, RAGResult,
    RunContext, SchemaCandidate, SchemaLink, SQLAttempt, SQLColumn, SQLParameter, SQLResult, SQLScope, TraceStep,
)


def sql_request(**changes):
    """构造纯协议问数请求，默认无云授权，不执行所描述的查询。"""
    return AgentRequest(**{
        "client_request_id": UUID(int=1), "question": "统计明确范围内的记录数", "intent": "sql",
        "sql_scope": {"db_id": "registered_db", "sub_db": "registered_schema"}, **changes,
    })


def context():
    """提供固定关联身份用于结构断言，不代表真实会话、授权或执行记录。"""
    return RunContext(request_id=UUID(int=2), client_request_id=UUID(int=1), session_id=UUID(int=3),
                      run_id=UUID(int=4), trace_id=UUID(int=5), scope_fingerprint="a" * 64)


def attempt(**changes):
    """提供生成/执行分离的协议输入，测试本身不访问SQL执行器。"""
    return SQLAttempt(**{
        "attempt": 1, "generated_sql": "SELECT COUNT(*) FROM permitted_table",
        "validation": "passed", "execution": "not_run", **changes,
    })


def test_cloud_is_disabled_and_client_cannot_inject_confirmed_slots():
    """默认零预算，客户端额外槽位不能越过服务端澄清确认链路。"""
    request = sql_request()
    assert request.cloud.allow_cloud is False
    assert request.cloud.max_calls == request.cloud.max_total_tokens == 0
    with pytest.raises(ValidationError):
        sql_request(confirmed_slots={"year": "2025"})


@pytest.mark.parametrize("changes", [
    {"allow_cloud": True},
    {"max_calls": 1},
    {"allow_cloud": True, "consent_id": UUID(int=6), "provider_id": "approved_provider",
     "max_calls": 1, "max_total_tokens": 4000, "allow_sql_repair": True},
])
def test_cloud_budget_must_be_explicit(changes):
    """缺许可、默认状态附带预算或只够首次调用时，均拒绝额外修复。"""
    with pytest.raises(ValidationError):
        CloudAuthorization(**changes)


def test_explicit_cloud_budget_round_trips_without_credentials():
    """协议可序列化本次接收方及有限预算；凭据和真实授权核验不在DTO内。"""
    policy = CloudAuthorization(allow_cloud=True, consent_id=UUID(int=6), provider_id="approved_provider",
                                max_calls=2, max_total_tokens=4000, allow_sql_repair=True)
    assert CloudAuthorization.model_validate_json(policy.model_dump_json()) == policy
    with pytest.raises(ValidationError):
        CloudAuthorization(api_key="not-a-real-secret")


@pytest.mark.parametrize("doc_ids", [[], [UUID(int=0)], [UUID(int=7), UUID(int=7)]])
def test_document_scope_cannot_silently_expand(doc_ids):
    """空/零身份/重复文档拒绝；null全库语义与非空指定范围保持明确。"""
    with pytest.raises(ValidationError):
        KnowledgeScope(kb_id=UUID(int=8), doc_ids=doc_ids)
    assert KnowledgeScope(kb_id=UUID(int=8)).doc_ids is None


def test_explicit_intent_does_not_enable_cross_source_planning():
    """SQL请求不能携带第二知识源；auto只可建模，不在本轮产生运行入口。"""
    with pytest.raises(ValidationError):
        sql_request(knowledge_scope={"kb_id": UUID(int=8)})
    assert sql_request(intent="auto", knowledge_scope={"kb_id": UUID(int=8)}).intent == "auto"


def test_clarification_needs_session_and_versioned_answers():
    """补参必须指向原run/问题版本，不能以无会话请求跳过恢复校验。"""
    reply = ClarificationReply(clarification_id=UUID(int=9), reply_to_run_id=UUID(int=4), revision=1,
                               answers=[ClarificationAnswer(question_id="year", text="2025")])
    with pytest.raises(ValidationError):
        sql_request(clarification=reply)
    request = sql_request(session_id=UUID(int=3), clarification=reply)
    assert request.clarification.answers[0].text == "2025"
    with pytest.raises(ValidationError):
        ClarificationAnswer(question_id="year", text="2025", option_id="year_2025")
    with pytest.raises(ValidationError):
        ConfirmedSlot(slot="year", value="2025", source_request_id=UUID(int=2), revision=1)


def test_clarification_response_is_bound_to_current_run():
    """澄清终态保留关联身份，不允许旧run的问题冒充本轮结果。"""
    prompt = ClarificationPrompt(clarification_id=UUID(int=9), reply_to_run_id=UUID(int=4), revision=1,
                                 questions=[ClarificationQuestion(question_id="year", slot="year",
                                                                  prompt="请选择统计年份", allow_text=True)])
    response = AgentResponse(context=context(), status="needs_clarification", result=prompt)
    assert AgentResponse.model_validate_json(response.model_dump_json()) == response
    with pytest.raises(ValidationError):
        AgentResponse(context=context(), status="answered", result=prompt)
    with pytest.raises(ValidationError):
        AgentResponse(context=context(), status="needs_clarification",
                      result={**prompt.model_dump(), "reply_to_run_id": UUID(int=10)})


def test_schema_ambiguity_does_not_become_implicit_first_choice():
    """两个真实目录候选保持歧义，指定不存在的候选或直接标success均拒绝。"""
    candidates = [SchemaCandidate(candidate_id=name, schema_name="public", table=name,
                                   evidence=[{"kind": "schema", "reference_id": name,
                                              "catalog_fingerprint": "b" * 64}])
                  for name in ("revenue_gross", "revenue_net")]
    link = SchemaLink(mention="收入", state="ambiguous", candidates=candidates)
    assert link.selected_id is None
    with pytest.raises(ValidationError):
        SchemaLink(mention="收入", state="selected", candidates=candidates, selected_id="missing")
    with pytest.raises(ValidationError):
        SQLResult(status="success", scope=SQLScope(db_id="db", sub_db="schema"), schema_links=[link],
                  attempts=[attempt(execution="succeeded", executed_sql="SELECT 1")],
                  columns=[SQLColumn(name="count", data_type="int8")], rows=[["1"]], returned_rows=1)


def test_generated_sql_is_not_execution_success():
    """仅生成SQL不能产生成功表格，被拒绝语句不得附执行SQL。"""
    with pytest.raises(ValidationError):
        SQLResult(status="success", scope=SQLScope(db_id="db", sub_db="schema"), attempts=[attempt()],
                  columns=[SQLColumn(name="count", data_type="int8")], rows=[["1"]], returned_rows=1)
    with pytest.raises(ValidationError):
        attempt(validation="rejected", execution="succeeded", executed_sql="SELECT 1", error_code="SQL_REJECTED")


def test_sql_round_trip_preserves_actual_statement_null_and_large_integer():
    """执行SQL与生成SQL分别展示；大整数、Decimal文本和NULL均不经过JSON浮点数。"""
    result = SQLResult(status="success", scope=SQLScope(db_id="db", sub_db="schema"),
                       attempts=[attempt(execution="succeeded", executed_sql="SELECT COUNT(*) FROM permitted_table WHERE id >= %(min_id)s LIMIT 501",
                                         parameters={"min_id": SQLParameter(data_type="int8", value="9007199254740993")})],
                       columns=[SQLColumn(name="big", data_type="int8"), SQLColumn(name="amount", data_type="numeric"),
                                SQLColumn(name="missing", data_type="text")],
                       rows=[["9007199254740993", "0.1234567890123456789", None]], returned_rows=1)
    restored = SQLResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.attempts[0].generated_sql != restored.attempts[0].executed_sql
    assert restored.attempts[0].parameters["min_id"].value == "9007199254740993"
    with pytest.raises(ValidationError):
        SQLResult(**{**result.model_dump(), "rows": [[9007199254740993, "0.1", None]]})


def test_unknown_execution_cannot_be_retried_or_displayed_as_empty():
    """超时后的未知状态禁止自动再次执行，也不能伪装成合法空结果。"""
    unknown = attempt(execution="unknown", executed_sql="SELECT 1", error_code="EXECUTION_UNKNOWN")
    with pytest.raises(ValidationError):
        SQLResult(status="failed", scope=SQLScope(db_id="db", sub_db="schema"),
                  attempts=[unknown, attempt(attempt=2)], error_code="EXECUTION_UNKNOWN")
    with pytest.raises(ValidationError):
        SQLResult(status="empty", scope=SQLScope(db_id="db", sub_db="schema"), attempts=[unknown],
                  columns=[SQLColumn(name="count", data_type="int8")])


def test_rag_insufficiency_keeps_original_contract_and_snapshot_state():
    """证据不足复用原RAGAnswer，不能标已回答或伪称来源已核验。"""
    result = RAGResult(result=RAGAnswer(status="insufficient_evidence", answer=INSUFFICIENT_ANSWER,
                                       insufficient_evidence=True))
    response = AgentResponse(context=context(), status="insufficient_evidence", result=result)
    assert response.result.source_validation == "snapshot_only"
    with pytest.raises(ValidationError):
        AgentResponse(context=context(), status="answered", result=result)
    with pytest.raises(ValidationError):
        RAGResult(result=result.result, source_validation="verified")


def test_trace_rejects_raw_payloads_mismatched_tools_and_fake_timings():
    """Trace只容纳安全业务摘要，禁止原文扩展、跨工具步骤或非有限耗时。"""
    fields = {"step_id": UUID(int=11), "tool": "sql", "operation": "sql_execute", "status": "succeeded", "sql_attempt": 1}
    assert TraceStep(**fields).elapsed_ms is None
    for changes in ({"question": "原问题"}, {"tool": "rag"}, {"elapsed_ms": float("nan")},
                    {"error_code": "SQL_EXECUTION_FAILED"}, {"sql_attempt": None}):
        with pytest.raises(ValidationError):
            TraceStep(**{**fields, **changes})


def test_sql_response_requires_trace_for_the_same_attempt():
    """真实执行状态还须有同次尝试的Trace关联，修复前后步骤不能混用。"""
    result = SQLResult(status="empty", scope=SQLScope(db_id="db", sub_db="schema"),
                       attempts=[attempt(execution="succeeded", executed_sql="SELECT 1 WHERE FALSE")],
                       columns=[SQLColumn(name="value", data_type="int4")])
    with pytest.raises(ValidationError):
        AgentResponse(context=context(), status="empty", result=result)
    trace = [TraceStep(step_id=UUID(int=20 + index), tool="sql", operation=operation,
                       status="succeeded", sql_attempt=1)
             for index, operation in enumerate(("sql_generate", "sql_validate", "sql_execute"))]
    assert AgentResponse(context=context(), status="empty", result=result, trace=trace).status == "empty"
    trace[-1] = TraceStep(step_id=UUID(int=22), tool="sql", operation="sql_execute", status="succeeded", sql_attempt=2)
    with pytest.raises(ValidationError):
        AgentResponse(context=context(), status="empty", result=result, trace=trace)
