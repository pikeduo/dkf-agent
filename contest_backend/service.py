"""问数领域服务：显式SQL意图、固定会话澄清、授权生成和真实只读执行。"""

import re
from time import monotonic
from uuid import uuid4

from contracts.contest_v1 import (
    AgentRequest, AgentResponse, ClarificationOption, ClarificationPrompt, ClarificationQuestion,
    CloudAuthorization, ConfirmedSlot, FailureResult, RunContext, SQLAttempt, SQLResult, UnsupportedResult,
)
from contest_backend.config import Settings
from contest_backend.errors import ContestError
from contest_backend.execution import SQLExecutor
from contest_backend.generation import SQLGenerator, extract_sql
from contest_backend.llm import LLMClient
from contest_backend.registry import Registry
from contest_backend.schema_linking import LinkResult, SchemaLinker
from contest_backend.sql_policy import normalize_value, prepare_sql
from contest_backend.state import Grant, Session, SessionStore
from contest_backend.tracing import Trace


class IntentService:
    """本阶段只识别已选源内的问数，未知操作交由用户确认，不调用跨源规划器。"""

    def interpret(self, request: AgentRequest, slots: dict[str, str]) -> str:
        """显式非SQL意图不降级到SQL；短语识别不能覆盖SQL执行器的安全检查。"""
        if request.intent != "sql":
            return "unsupported"
        if slots.get("intent") == "cancel":
            return "unsupported"
        if slots.get("intent") == "sql" or re.search(
            r"查询|统计|多少|总额|平均|最高|最低|列出|数量|排名|对比|select\b|count\b|sum\b|list\b|show\b|how\b|find\b",
            request.question, re.I,
        ):
            return "sql"
        return "needs_user"


class ClarificationService:
    """实现冻结ClarificationTool边界，选项和值映射只保存在当前会话。"""

    def __init__(self, session: Session):
        """每次请求绑定自己的会话，禁止复用其他会话的待答状态。"""
        self.session, self.linked = session, LinkResult()

    def accept(self, request: AgentRequest, context: RunContext) -> list[ConfirmedSlot]:
        """原问题、会话、问题身份及revision一致后原子接收答案；不执行SQL。"""
        reply, session = request.clarification, self.session
        if reply is None:
            return list(session.slots.values())
        pending = session.pending
        if (pending is None or reply.clarification_id != pending["prompt"].clarification_id
                or reply.reply_to_run_id != pending["prompt"].reply_to_run_id
                or reply.revision != pending["prompt"].revision):
            raise ContestError("RESULT_INVALID", 409)
        proposed = {}
        for answer in reply.answers:
            item = pending["questions"].get(answer.question_id)
            if item is None:
                raise ContestError("RESULT_INVALID", 409)
            if answer.option_id is not None:
                value = item["choices"].get(answer.option_id)
                if value is None:
                    raise ContestError("RESULT_INVALID", 422)
            elif item["choices"]:
                raise ContestError("RESULT_INVALID", 422)
            else:
                value = answer.text
                spec = next((spec for spec in session.source.conditions if spec.slot == item["slot"]), None)
                if spec is None:
                    raise ContestError("RESULT_INVALID", 422)
                value = normalize_value(spec, value)
            proposed[item["slot"]] = ConfirmedSlot(slot=item["slot"], value=value,
                source_request_id=context.request_id, clarification_id=reply.clarification_id, revision=reply.revision)
        if len(set(session.slots) | set(proposed)) > 32:
            raise ContestError("UNSUPPORTED", 422)
        session.slots.update(proposed)
        session.pending = None
        return list(session.slots.values())

    def propose(self, request: AgentRequest, context: RunContext, schema_links) -> ClarificationPrompt | None:
        """根据真实待答项保存带版本问题，部分回答后重新定位并继续剩余问题。"""
        questions = self.linked.questions
        if not questions:
            return None
        session = self.session
        session.revision += 1
        records, public = {}, []
        for index, item in enumerate(questions):
            key = f"q{index + 1}"
            records[key] = item
            public.append(ClarificationQuestion(question_id=key, slot=item["slot"], prompt=item["prompt"],
                allow_text=not bool(item["choices"]),
                options=[ClarificationOption(option_id=identity, label=value) for identity, value in item["choices"].items()]))
        prompt = ClarificationPrompt(clarification_id=uuid4(), reply_to_run_id=context.run_id,
                                    revision=session.revision, questions=public, schema_links=schema_links)
        session.pending = {"prompt": prompt, "questions": records}
        return prompt


class SQLToolAdapter:
    """实现SQLTool协议，所有模型调用/SQL尝试共享一个上下文及预算。"""

    def __init__(self, session: Session, settings: Settings, trace: Trace, llm, executor):
        """以请求级对象承载工具状态，生产依赖与单元测试注入边界明确分离。"""
        self.session, self.settings, self.trace = session, settings, trace
        self.llm, self.executor = llm, executor
        self.linker, self.generator = SchemaLinker(), SQLGenerator()

    def run(self, request: AgentRequest, context: RunContext) -> SQLResult | ClarificationPrompt:
        """确认完成后才生成/执行SQL；任何未知执行、未授权范围或歧义均不重试。"""
        session, trace = self.session, self.trace
        if request.intent != "sql":
            with trace.step("intent"):
                raise ContestError("UNSUPPORTED", 422)
        if (request.session_id != context.session_id or request.client_request_id != context.client_request_id
                or context.scope_fingerprint != session.source.fingerprint() or request.sql_scope is None
                or (request.sql_scope.db_id, request.sql_scope.sub_db) != (session.source.db_id, session.source.sub_db)):
            raise ContestError("SCOPE_INVALID", 403)
        clarification = ClarificationService(session)
        with trace.step("clarification", tool="clarification"):
            context.confirmed_slots = clarification.accept(request, context)
        slots = {key: value.value for key, value in session.slots.items()}
        with trace.step("intent"):
            intent = IntentService().interpret(request, slots)
            if intent == "unsupported":
                raise ContestError("UNSUPPORTED", 422)
        if intent == "needs_user":
            clarification.linked.questions = [{"slot": "intent", "prompt": "请确认本次是否在选定数据库执行问数",
                                               "choices": {"sql": "sql", "cancel": "cancel"}}]
            with trace.step("clarification", tool="clarification"):
                return clarification.propose(request, context, [])
        with trace.step("schema_linking"):
            linked = self.linker.link(session.source, request.question, slots)
            explicit = {item["slot"]: item.get("explicit") for item in linked.questions if item.get("explicit") is not None}
            for name, value in explicit.items():
                spec = next(item for item in session.source.conditions if item.slot == name)
                session.slots[name] = ConfirmedSlot(slot=name, value=normalize_value(spec, value),
                                                    source_request_id=context.request_id, revision=0)
            if explicit:
                slots = {key: value.value for key, value in session.slots.items()}
                linked = self.linker.link(session.source, request.question, slots)
            context.confirmed_slots = list(session.slots.values())
        clarification.linked = linked
        if linked.questions:
            with trace.step("clarification", tool="clarification"):
                return clarification.propose(request, context, linked.links)
        if not linked.tables:
            raise ContestError("SCOPE_INVALID", 403)
        if session.expires_at <= monotonic():
            raise ContestError("PERMISSION_DENIED", 403)
        grant = session.grant(request.cloud)
        attempts, bad_sql = [], None
        for number in (1, 2):
            with trace.step("sql_generate", attempt=number) as step:
                messages = self.generator.messages(request.question, session.source, linked, slots, bad_sql)
                before = grant.attempted_calls
                try:
                    generated = extract_sql(self.llm.complete(messages, grant))
                    if not generated.strip():
                        raise ContestError("MODEL_FAILURE", 502)
                finally:
                    step["llm_calls"] = grant.attempted_calls - before
            validation_error = None
            with trace.step("sql_validate", attempt=number) as step:
                try:
                    query = prepare_sql(generated, session.source, linked, slots)
                except ContestError as error:
                    step.update(status="failed", error_code=error.code)
                    validation_error = error
            if validation_error is not None:
                attempts.append(SQLAttempt(attempt=number, generated_sql=generated, validation="rejected",
                                            execution="not_run", error_code="SQL_REJECTED"))
                return SQLResult(status="failed", scope=request.sql_scope, schema_links=linked.links,
                                 attempts=attempts, error_code="SQL_REJECTED")
            execution_error = None
            with trace.step("sql_execute", attempt=number) as step:
                try:
                    if session.expires_at <= monotonic():
                        raise ContestError("PERMISSION_DENIED", 403)
                    columns, rows, truncated = self.executor.execute(query, session.source)
                except ContestError as error:
                    if error.execution == "succeeded":
                        # 数据库已读回但结果无法安全编码，整次工具失败，不发布半份表格。
                        raise ContestError("RESULT_INVALID", 502) from None
                    step.update(status={"not_run": "skipped", "failed": "failed", "unknown": "unknown"}[error.execution],
                                error_code=error.code)
                    execution_error = error
            if execution_error is None:
                attempts.append(SQLAttempt(attempt=number, generated_sql=generated, validation="passed",
                                            execution="succeeded", executed_sql=query))
                return SQLResult(status="success" if rows else "empty", scope=request.sql_scope,
                    schema_links=linked.links, attempts=attempts, columns=columns, rows=rows,
                    returned_rows=len(rows), truncated=truncated)
            execution = execution_error.execution
            attempts.append(SQLAttempt(attempt=number, generated_sql=generated, validation="passed", execution=execution,
                executed_sql=query if execution != "not_run" else None,
                error_code=execution_error.code if execution != "not_run" else None))
            if (number == 1 and execution_error.repairable and execution in {"not_run", "failed"}
                    and grant.policy.allow_sql_repair and not grant.uncertain):
                bad_sql = generated
                continue
            return SQLResult(status="failed", scope=request.sql_scope, schema_links=linked.links,
                             attempts=attempts, error_code=execution_error.code)
        raise ContestError("INTERNAL_ERROR", 500)


class ContestService:
    """API共享基础设施只包含配置、只读目录及有界状态，不持有当前请求Trace。"""

    def __init__(self, settings: Settings, *, registry=None, llm=None, executor=None):
        """生产按唯一注册表构造真实服务；注入依赖仅用于编写隔离单元测试。"""
        self.settings = settings
        self.registry = registry if registry is not None else Registry.load(settings.registry_file())
        self.sessions = SessionStore(settings.session_ttl_seconds, settings.max_sessions)
        self.llm = llm if llm is not None else LLMClient(settings)
        self.executor = executor if executor is not None else SQLExecutor(settings)

    def issue_consent(self, session: Session, *, provider_id: str, max_calls: int, max_total_tokens: int,
                      allow_sql_repair: bool) -> CloudAuthorization:
        """显式确认后发放任务级许可；不能重用旧任务或自动提升调用/修复预算。"""
        if provider_id != self.settings.llm_provider_id or len(session.grants) >= 4:
            raise ContestError("AUTHORIZATION_REQUIRED", 403)
        policy = CloudAuthorization(allow_cloud=True, consent_id=uuid4(), provider_id=provider_id,
                                    max_calls=max_calls, max_total_tokens=max_total_tokens, allow_sql_repair=allow_sql_repair)
        session.grants[policy.consent_id] = Grant(policy, session.task_id)
        return policy.model_copy(deep=True)

    def query(self, request: AgentRequest, session: Session, *, request_id=None) -> AgentResponse:
        """会话锁由API持有；继承入口request_id，重入不重放，结果仅返回本次响应。"""
        if request.session_id != session.id or request.client_request_id in session.requests:
            raise ContestError("RESULT_INVALID", 409)
        if len(session.requests) >= 256 or session.question is not None and session.question != request.question:
            raise ContestError("RESULT_INVALID", 409)
        session.requests.add(request.client_request_id)
        session.question = request.question
        context = RunContext(request_id=request_id or uuid4(), client_request_id=request.client_request_id, session_id=session.id,
                             run_id=uuid4(), trace_id=uuid4(), scope_fingerprint=session.source.fingerprint(),
                             confirmed_slots=list(session.slots.values()))
        trace = Trace(context)
        try:
            if request.intent != "sql":
                with trace.step("intent"):
                    payload = UnsupportedResult(capability="automatic_cross_source_planning"
                        if request.intent == "auto" else "unified_agent_http")
                status = "unsupported"
            else:
                payload = SQLToolAdapter(session, self.settings, trace, self.llm, self.executor).run(request, context)
                status = "needs_clarification" if isinstance(payload, ClarificationPrompt) else {
                    "success": "answered", "empty": "empty", "failed": "failed"}[payload.status]
        except ContestError as error:
            payload, status = FailureResult(code=error.code), "failed"
        finally:
            session.traces[context.trace_id] = trace.safe_snapshot(
                request.clarification.reply_to_run_id if request.clarification else None)
            if len(session.traces) > 64:
                session.traces.pop(next(iter(session.traces)))
        return AgentResponse(context=context, status=status, result=payload, trace=trace.steps)
