"""领域流水线的确认条件、Trace及失败边界，仅编写待用户执行。"""

import json

import pytest

from contracts.contest_v1 import SQLScope
from contest_backend.errors import ContestError
from contest_backend.registry import ConditionSpec, ForeignKey
from contest_backend.tests.conftest import RecordingExecutor, RecordingLLM, request_for


def reply_to(prompt, answers):
    """保留服务端澄清身份和revision，构造当前问题回复。"""
    return {"clarification_id": prompt.clarification_id, "reply_to_run_id": prompt.reply_to_run_id,
            "revision": prompt.revision, "answers": answers}


def test_single_table_success_uses_real_adapter_and_separate_sql(build_service):
    """隔离调用真实Service/Policy，生成与实际执行SQL及表格逐项对应。"""
    service, session, _, policy, model, database = build_service()
    response = service.query(request_for(session, policy), session)
    assert response.status == "answered" and response.result.rows == [["7"]]
    attempt = response.result.attempts[0]
    assert attempt.generated_sql == model.sql
    assert attempt.executed_sql == database.calls[0][0] != attempt.generated_sql
    assert {step.operation for step in response.trace} >= {"intent", "schema_linking", "clarification",
                                                          "sql_generate", "sql_validate", "sql_execute"}
    assert sum(step.llm_calls for step in response.trace) == 1
    assert all(step.elapsed_ms >= 0 for step in response.trace)


def test_empty_result_is_not_failure_or_fabricated_rows(build_service):
    """真正返回空列表的执行器得到empty，列元数据保留且不补演示数据。"""
    service, session, _, policy, _, _ = build_service(executor=RecordingExecutor(rows=[]))
    response = service.query(request_for(session, policy), session)
    assert response.status == "empty" and response.result.rows == [] and response.result.columns


def test_multiple_rounds_keep_original_and_apply_confirmed_conditions(source, build_service):
    """字段与年度分两轮补充，未完成时零调用，最终Prompt和WHERE使用同一确认值。"""
    source.conditions = [ConditionSpec(slot="year", table="sales", column="year", label="请确认年度",
                                       value_type="integer", required=True)]
    model = RecordingLLM("SELECT SUM(amount) FROM sales WHERE year=2024")
    service, session, _, policy, _, database = build_service(catalog=source, llm=model)
    original = "统计收入"
    first = service.query(request_for(session, policy, question=original), session)
    assert first.status == "needs_clarification" and not model.messages and not database.calls
    field = next(item for item in first.result.questions if item.slot.startswith("schema_"))
    second = service.query(request_for(session, policy, question=original,
        clarification=reply_to(first.result, [{"question_id": field.question_id,
                                               "option_id": field.options[0].option_id}])), session)
    assert second.status == "needs_clarification" and not model.messages and not database.calls
    year = next(item for item in second.result.questions if item.slot == "year")
    third = service.query(request_for(session, policy, question=original,
        clarification=reply_to(second.result, [{"question_id": year.question_id, "text": "2024"}])), session)
    assert third.status == "answered" and len(database.calls) == 1
    data = json.loads(model.messages[0][1]["content"])
    assert data["question"] == original and data["confirmed_slots"]["year"] == "2024"
    assert "2024" in database.calls[0][0]
    assert session.traces[third.context.trace_id]["reply_to_run_id"] == str(second.context.run_id)


def test_explicit_condition_is_consumed_without_becoming_projected_metric(source, build_service):
    """原问题中明确slot赋值保留revision=0，并进入真正执行的过滤条件。"""
    source.conditions = [ConditionSpec(slot="year", table="sales", column="year", label="年度",
                                       value_type="integer", required=True)]
    model = RecordingLLM("SELECT COUNT(*) FROM sales WHERE year=2024")
    service, session, _, policy, _, database = build_service(catalog=source, llm=model)
    result = service.query(request_for(session, policy, question="统计sales year=2024"), session)
    assert result.status == "answered" and database.calls
    assert session.slots["year"].revision == 0


def test_confirmed_condition_ignored_by_model_cannot_execute(source, build_service):
    """模型丢条件时语法树拒绝，不用模型回答代替真实结果。"""
    source.conditions = [ConditionSpec(slot="year", table="sales", column="year", label="年度",
                                       value_type="integer", required=True)]
    service, session, _, policy, _, database = build_service(catalog=source)
    response = service.query(request_for(session, policy, question="统计sales year=2024"), session)
    assert response.status == "failed" and response.result.error_code == "SQL_REJECTED"
    assert not database.calls


def test_foreign_key_path_ambiguity_stays_pending_until_selected(source, build_service):
    """多个真实外键产生路径选择，未确认时不能按第一条关系执行。"""
    source.foreign_keys.append(ForeignKey(id="alternate_customer", from_table="sales", to_table="customers",
                                         pairs=[("id", "id")]))
    model = RecordingLLM("SELECT SUM(s.amount) FROM sales s JOIN customers c ON s.customer_id=c.id")
    service, session, _, policy, _, database = build_service(catalog=source, llm=model)
    response = service.query(request_for(session, policy, question="统计销售额客户"), session)
    assert response.status == "needs_clarification" and not model.messages and not database.calls
    prompt = response.result
    item = next(item for item in prompt.questions if item.slot.startswith("join_"))
    option = next(option for option in item.options if option.label == "sales_customer")
    resolved = service.query(request_for(session, policy, question="统计销售额客户",
        clarification=reply_to(prompt, [{"question_id": item.question_id, "option_id": option.option_id}])), session)
    assert resolved.status == "answered" and len(database.calls) == 1


def test_stale_reply_scope_change_and_reentry_are_rejected(build_service):
    """错会话问题、过期版本和换源不能污染确认状态；重复操作身份不再收费。"""
    service, session, _, policy, model, database = build_service()
    first = service.query(request_for(session, policy, question="统计收入"), session)
    question = first.result.questions[0]
    stale = reply_to(first.result, [{"question_id": question.question_id, "option_id": question.options[0].option_id}])
    stale["revision"] += 1
    rejected = service.query(request_for(session, policy, question="统计收入", clarification=stale), session)
    assert rejected.result.code == "RESULT_INVALID" and not session.slots
    changed = service.query(request_for(session, policy, question="统计收入",
        sql_scope=SQLScope(db_id="other", sub_db="sales")), session)
    assert changed.result.code == "SCOPE_INVALID" and not model.messages and not database.calls
    duplicate = request_for(session, policy, question="统计收入")
    service.query(duplicate, session)
    with pytest.raises(ContestError):
        service.query(duplicate, session)


def test_reset_invalidates_old_conditions_and_cloud_grants(build_service):
    """用户修正问题需reset；旧许可、旧待答项与条件不会用于新问题。"""
    service, session, token, policy, _, _ = build_service()
    service.query(request_for(session, policy, question="统计收入"), session)
    session.reset()
    assert not session.slots and session.pending is None and session.question is None
    with pytest.raises(ContestError):
        session.grant(policy)
    other, _ = service.sessions.create(session.source)
    with pytest.raises(ContestError):
        service.sessions.get(other.id, token)


def test_cloud_permission_is_required_before_model_request(build_service):
    """即使目录无歧义，零云许可仍不产生模型或数据库请求。"""
    service, session, _, _, model, database = build_service()
    response = service.query(request_for(session), session)
    assert response.result.code == "AUTHORIZATION_REQUIRED" and not model.messages and not database.calls


@pytest.mark.parametrize("error", [
    ContestError("DEPENDENCY_UNAVAILABLE", 503),
    ContestError("SQL_EXECUTION_FAILED", 504, execution="failed"),
    ContestError("EXECUTION_UNKNOWN", 503, execution="unknown"),
])
def test_database_failures_do_not_look_like_success_or_retry(build_service, error):
    """超时/不可用/未知执行均明确失败，任何未知结果都禁止重放。"""
    database = RecordingExecutor(errors=[error])
    service, session, _, policy, _, _ = build_service(executor=database)
    result = service.query(request_for(session, policy), session)
    assert result.status == "failed" and not result.result.rows and len(database.calls) == 1


def test_model_failure_is_safe_and_never_reaches_executor(build_service):
    """模型技术失败只发布固定分类，不伪造SQL尝试/查询表格。"""
    service, session, _, policy, _, database = build_service(llm=RecordingLLM(error=ContestError("MODEL_FAILURE", 502)))
    response = service.query(request_for(session, policy), session)
    assert response.result.code == "MODEL_FAILURE" and not database.calls


@pytest.mark.parametrize("execution", ["failed", "not_run"])
def test_sql_repair_requires_separate_grant_and_carries_same_context(build_service, execution):
    """明确许可的一次技术修复保留同一run/Trace，预算累计且最多两次执行。"""
    database = RecordingExecutor(errors=[ContestError("SQL_EXECUTION_FAILED", 502, execution=execution, repairable=True)])
    service, session, _, _, model, _ = build_service(executor=database)
    policy = service.issue_consent(session, provider_id="contest", max_calls=2,
                                   max_total_tokens=32768, allow_sql_repair=True)
    response = service.query(request_for(session, policy), session)
    assert response.status == "answered" and len(model.messages) == len(database.calls) == 2
    assert [item.attempt for item in response.result.attempts] == [1, 2]
    assert {item.sql_attempt for item in response.trace if item.operation == "sql_generate"} == {1, 2}


def test_unrecognized_intent_requires_confirmation(build_service):
    """仅选择SQL领域而问题不明确时，先确认操作，不偷跑模型或查询。"""
    service, session, _, policy, model, database = build_service()
    response = service.query(request_for(session, policy, question="sales情况"), session)
    assert response.status == "needs_clarification" and response.result.questions[0].slot == "intent"
    assert not model.messages and not database.calls


def test_auto_capability_is_explicitly_unsupported_without_tool_calls(build_service):
    """auto只返回冻结未实现标识，不能据此启动任何跨源计划或付费工具。"""
    service, session, _, policy, model, database = build_service()
    response = service.query(request_for(session, policy, intent="auto"), session)
    assert response.status == "unsupported" and response.result.capability == "automatic_cross_source_planning"
    assert not model.messages and not database.calls


def test_missing_relationship_requires_user_action_and_never_guesses(source, build_service):
    """目录没有真实关系时暂停要求缩小范围/补齐目录，不自由输入伪造连接。"""
    source.foreign_keys = []
    service, session, _, policy, model, database = build_service(catalog=source)
    response = service.query(request_for(session, policy, question="统计销售额客户"), session)
    assert response.status == "needs_clarification" and not model.messages and not database.calls


def test_conflicting_explicit_values_require_clarification(source, build_service):
    """原问题同一槽位给出不同值时不能取首值，且可选槽位显式赋值也会进入确认链。"""
    source.conditions = [ConditionSpec(slot="year", table="sales", column="year", label="请确认年度",
                                       value_type="integer", required=False)]
    service, session, _, policy, model, database = build_service(catalog=source)
    response = service.query(request_for(session, policy, question="统计sales year=2024 year=2023"), session)
    assert response.status == "needs_clarification" and response.result.questions[0].slot == "year"
    assert not session.slots and not model.messages and not database.calls
