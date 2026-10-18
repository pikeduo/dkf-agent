"""双服务整合的冻结协议；仅做结构校验，权限、只读执行及费用仍由服务端落实。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from app.schemas.evidence import EvidenceTrace
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_binding import EvidenceVariableInput, UserVariableInput
from app.schemas.formula_calculation import FormulaCalculationResult
from app.schemas.rag import RAGAnswer

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")]
Fingerprint = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
ErrorCode = Literal[
    "PERMISSION_DENIED", "SCOPE_INVALID", "AUTHORIZATION_REQUIRED", "BUDGET_EXHAUSTED",
    "MODEL_FAILURE", "SQL_REJECTED", "SQL_EXECUTION_FAILED", "EXECUTION_UNKNOWN",
    "DEPENDENCY_UNAVAILABLE", "RESULT_INVALID", "UNSUPPORTED", "INTERNAL_ERROR",
]
# 数值以列类型配合无损字符串传输，避免浏览器的大整数/Decimal精度丢失。
SQLCell = StrictStr | StrictBool | None


class ContractModel(BaseModel):
    """拒绝额外字段并隐藏异常字符串输入；HTTP错误仍需安全映射，资源权限另行核对。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SQLScope(ContractModel):
    """仅接收服务端注册表中的数据源/子库标识，不接收DSN、路径或客户端表授权。"""

    db_id: Identifier
    sub_db: Identifier


class KnowledgeScope(ContractModel):
    """文档范围与原API保持一致；null代表本库合格文档，空数组不能扩大为全库。"""

    kb_id: UUID
    doc_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_scope(self):
        """拒绝零身份及重复文档，避免多轮范围摘要出现不同解释。"""
        if self.kb_id.int == 0 or self.doc_ids is not None and (
            any(value.int == 0 for value in self.doc_ids) or len(set(self.doc_ids)) != len(self.doc_ids)
        ):
            raise ValueError("知识范围无效")
        return self


class CloudAuthorization(ContractModel):
    """本次外传与费用许可；consent_id须在服务端绑定用户、范围、接收方和预算。"""

    allow_cloud: StrictBool = False
    consent_id: UUID | None = None
    provider_id: Identifier | None = None
    max_calls: StrictInt = Field(default=0, ge=0, le=4)
    max_total_tokens: StrictInt = Field(default=0, ge=0, le=32768)
    allow_sql_repair: StrictBool = False

    @model_validator(mode="after")
    def explicit_budget(self):
        """默认零调用；语义修复需单独许可及至少两次预算，禁止隐式重试/供应商切换。"""
        if self.allow_cloud:
            if (self.consent_id is None or self.consent_id.int == 0 or self.provider_id is None
                    or self.max_calls == 0 or self.max_total_tokens == 0
                    or self.allow_sql_repair and self.max_calls < 2):
                raise ValueError("云调用授权或预算不完整")
        elif (self.consent_id is not None or self.provider_id is not None or self.max_calls
              or self.max_total_tokens or self.allow_sql_repair):
            raise ValueError("未授权云调用不能携带执行预算")
        return self


class ClarificationAnswer(ContractModel):
    """用户只提交服务端问题/选项身份或自由文本，不提交自称已确认的SQL槽位。"""

    question_id: Identifier
    option_id: Identifier | None = None
    text: StrictStr | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def one_answer(self):
        """选项与自由文本二选一；服务端仍须核对原问题及是否允许自由输入。"""
        if (self.option_id is None) == (self.text is None) or self.text is not None and not self.text.strip():
            raise ValueError("澄清答案无效")
        return self


class ClarificationReply(ContractModel):
    """恢复同一会话内的待答问题；revision用于拒绝过期或重复消费的澄清回复。"""

    clarification_id: UUID
    reply_to_run_id: UUID
    revision: StrictInt = Field(ge=1)
    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_answers(self):
        """每道问题只答一次；答案合法性与范围固定检查属于后续会话服务。"""
        if (self.clarification_id.int == 0 or self.reply_to_run_id.int == 0
                or len({item.question_id for item in self.answers}) != len(self.answers)):
            raise ValueError("澄清回复身份无效")
        return self


class FormulaSelection(ContractModel):
    """显式选中原公式；参数和结果复用知识库既有契约，不新建公式计算引擎。"""

    doc_id: UUID
    chunk_id: UUID
    block_id: UUID
    definition: FormulaSemanticResult | None = None
    user_inputs: list[UserVariableInput] = Field(default_factory=list, max_length=32)
    evidence_inputs: list[EvidenceVariableInput] = Field(default_factory=list, max_length=32)


class AgentRequest(ContractModel):
    """预留统一入口请求；本轮不注册HTTP，auto仅保留协议且不能触发跨源规划。"""

    version: Literal["contest-agent-v1"] = "contest-agent-v1"
    client_request_id: UUID
    session_id: UUID | None = None
    question: StrictStr = Field(min_length=1, max_length=4000)
    intent: Literal["sql", "rag", "formula", "auto"]
    sql_scope: SQLScope | None = None
    knowledge_scope: KnowledgeScope | None = None
    formula: FormulaSelection | None = None
    clarification: ClarificationReply | None = None
    cloud: CloudAuthorization = Field(default_factory=CloudAuthorization)

    @model_validator(mode="after")
    def explicit_scope(self):
        """要求明确范围和公式来源；后续仍复用原服务输入校验及当前来源核对。"""
        if (self.client_request_id.int == 0 or not self.question.strip()
                or self.session_id is not None and self.session_id.int == 0
                or self.sql_scope is None and self.knowledge_scope is None
                or self.clarification is not None and self.session_id is None):
            raise ValueError("请求身份或范围缺失")
        if self.intent == "sql":
            if self.sql_scope is None or self.knowledge_scope is not None or self.formula is not None:
                raise ValueError("SQL意图范围无效")
        elif self.intent in {"rag", "formula"}:
            if self.knowledge_scope is None or self.sql_scope is not None:
                raise ValueError("知识意图范围无效")
            if (self.intent == "formula") != (self.formula is not None):
                raise ValueError("公式意图与来源不一致")
        elif self.formula is not None:
            raise ValueError("自动意图不接受隐式公式调用")
        if self.formula is not None:
            selection = self.formula
            identities = [selection.doc_id, selection.chunk_id, selection.block_id]
            identities.extend(value for item in selection.evidence_inputs
                              for value in (item.doc_id, item.chunk_id, item.block_id))
            selected_docs = [selection.doc_id, *[item.doc_id for item in selection.evidence_inputs]]
            if (any(value.int == 0 for value in identities)
                    or self.knowledge_scope.doc_ids is not None
                    and not set(selected_docs).issubset(self.knowledge_scope.doc_ids)):
                raise ValueError("公式来源超出范围")
            definition = selection.definition
            if definition is not None and (
                definition.status != "success" or definition.source.kb_id != self.knowledge_scope.kb_id
                or definition.source.doc_id != selection.doc_id
                or definition.source.chunk_id != selection.chunk_id
                or definition.formula_block_id != selection.block_id
            ):
                raise ValueError("复用公式与来源不一致")
        return self


class ConfirmedSlot(ContractModel):
    """服务端核实的业务口径；值必须实际传入Schema Linking及SQL生成/校验链路。"""

    slot: Identifier
    value: StrictStr = Field(min_length=1, max_length=1000)
    source_request_id: UUID
    clarification_id: UUID | None = None
    revision: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def explicit_source(self):
        """原问题中的显式条件使用revision=0；澄清条件必须保留非零问题身份与版本。"""
        if (self.source_request_id.int == 0 or not self.value.strip()
                or (self.clarification_id is None) != (self.revision == 0)
                or self.clarification_id is not None and self.clarification_id.int == 0):
            raise ValueError("已确认条件缺少明确来源")
        return self


class RunContext(ContractModel):
    """服务端分配且与会话授权绑定的关联身份；不是客户端可用来获得权限的凭据。"""

    request_id: UUID
    client_request_id: UUID
    session_id: UUID
    run_id: UUID
    trace_id: UUID
    scope_fingerprint: Fingerprint
    confirmed_slots: list[ConfirmedSlot] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def stable_identity(self):
        """拒绝空关联身份和重复槽位；会话所有权及摘要真实性仍由服务端核验。"""
        if (any(value.int == 0 for value in (
            self.request_id, self.client_request_id, self.session_id, self.run_id, self.trace_id,
        )) or len({item.slot for item in self.confirmed_slots}) != len(self.confirmed_slots)):
            raise ValueError("执行上下文无效")
        return self


class LinkEvidence(ContractModel):
    """来源为服务端目录/别名/外键/槽位记录的稳定标识，不暴露私有文件路径。"""

    kind: Literal["schema", "alias", "foreign_key", "confirmed_slot", "value_match"]
    reference_id: Identifier
    catalog_fingerprint: Fingerprint


class SchemaCandidate(ContractModel):
    """真实目录中的候选表或列；匹配分数允许未知，不能当概率置信度。"""

    candidate_id: Identifier
    schema_name: StrictStr = Field(min_length=1, max_length=128)
    table: StrictStr = Field(min_length=1, max_length=128)
    column: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    evidence: list[LinkEvidence] = Field(min_length=1, max_length=16)
    score: float | None = Field(default=None, allow_inf_nan=False)


class SchemaLink(ContractModel):
    """歧义候选保留给用户确认，不能把needs_user转换成SQL执行失败或任意选首项。"""

    mention: StrictStr = Field(min_length=1, max_length=256)
    state: Literal["selected", "ambiguous", "unmatched"]
    candidates: list[SchemaCandidate] = Field(max_length=20)
    selected_id: Identifier | None = None

    @model_validator(mode="after")
    def resolved_candidate(self):
        """已选择项必须来自真实候选；未解决项不能偷偷携带选择结果。"""
        ids = {item.candidate_id for item in self.candidates}
        if len(ids) != len(self.candidates):
            raise ValueError("Schema候选重复")
        if self.state == "selected":
            if self.selected_id is None or self.selected_id not in ids:
                raise ValueError("Schema选择没有候选依据")
        elif (self.selected_id is not None or self.state == "ambiguous" and len(ids) < 2
              or self.state == "unmatched" and ids):
            raise ValueError("Schema歧义状态无效")
        return self


class SQLParameter(ContractModel):
    """实际绑定参数的类型与无损值；区分相同文本表示的日期、数值和普通字符串。"""

    data_type: StrictStr = Field(min_length=1, max_length=128)
    value: SQLCell


class SQLAttempt(ContractModel):
    """分别记录模型输出与实际交给数据库的语句/绑定值；未知执行结果不得重放。"""

    attempt: StrictInt = Field(ge=1, le=2)
    generated_sql: StrictStr = Field(min_length=1, max_length=20000)
    validation: Literal["passed", "rejected"]
    execution: Literal["not_run", "succeeded", "failed", "unknown"]
    executed_sql: StrictStr | None = Field(default=None, min_length=1, max_length=20000)
    parameters: dict[Identifier, SQLParameter] = Field(default_factory=dict, max_length=128)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def execution_evidence(self):
        """没有执行凭证不能声称成功；被拒绝的SQL禁止有执行记录或绑定值。"""
        if self.execution == "not_run":
            if self.executed_sql is not None or self.parameters:
                raise ValueError("未执行SQL不能携带执行凭证")
        elif self.validation != "passed" or self.executed_sql is None:
            raise ValueError("执行SQL未通过校验或缺失")
        failed = self.validation == "rejected" or self.execution in {"failed", "unknown"}
        if failed != (self.error_code is not None) or self.validation == "rejected" and self.execution != "not_run":
            raise ValueError("SQL错误与执行状态不一致")
        return self


class SQLColumn(ContractModel):
    """列按游标顺序显示，允许真实同名列；类型来自实际结果元数据。"""

    name: StrictStr = Field(max_length=256)
    data_type: StrictStr = Field(min_length=1, max_length=128)


class SQLResult(ContractModel):
    """最多500行真实结果；空集与失败分开，截断只在实际多读到第501行时确认。"""

    kind: Literal["sql"] = "sql"
    status: Literal["success", "empty", "failed"]
    scope: SQLScope
    schema_links: list[SchemaLink] = Field(default_factory=list, max_length=128)
    attempts: list[SQLAttempt] = Field(default_factory=list, max_length=2)
    columns: list[SQLColumn] = Field(default_factory=list, max_length=256)
    rows: list[list[SQLCell]] = Field(default_factory=list, max_length=500)
    returned_rows: StrictInt = Field(default=0, ge=0, le=500)
    truncated: StrictBool = False
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def real_result(self):
        """表格必须对应最后一次成功执行；前次成功/执行未知均禁止隐式第二次尝试。"""
        if [item.attempt for item in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("SQL尝试顺序无效")
        if any(item.execution in {"succeeded", "unknown"} for item in self.attempts[:-1]):
            raise ValueError("SQL不能在已成功或执行未知后自动重放")
        if self.returned_rows != len(self.rows) or any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("SQL表格形状与计数不一致")
        if self.status == "failed":
            if (self.columns or self.rows or self.truncated or self.error_code is None
                    or any(item.execution == "succeeded" for item in self.attempts)):
                raise ValueError("失败不能冒充已有查询结果")
        elif (not self.attempts or self.attempts[-1].execution != "succeeded" or not self.columns
              or any(item.state != "selected" for item in self.schema_links)
              or self.error_code is not None or (self.status == "success") != bool(self.rows)
              or self.truncated and self.returned_rows != 500):
            raise ValueError("查询结果缺少真实执行或状态不一致")
        return self


class ClarificationOption(ContractModel):
    """选项值由服务端保存和解释，前端只提交不可歧义的option_id。"""

    option_id: Identifier
    label: StrictStr = Field(min_length=1, max_length=500)


class ClarificationQuestion(ContractModel):
    """一条有业务槽位标识的澄清问题；不得用默认答案跳过用户确认。"""

    question_id: Identifier
    slot: Identifier
    prompt: StrictStr = Field(min_length=1, max_length=1000)
    options: list[ClarificationOption] = Field(default_factory=list, max_length=20)
    allow_text: StrictBool = False

    @model_validator(mode="after")
    def answerable_question(self):
        """问题必须可回答，选项身份不能重复。"""
        if (not self.options and not self.allow_text
                or len({item.option_id for item in self.options}) != len(self.options)):
            raise ValueError("澄清问题没有有效输入方式")
        return self


class ClarificationPrompt(ContractModel):
    """暂停执行并携带真实歧义依据；范围摘要由RunContext固定，不附带伪查询结果。"""

    kind: Literal["clarification"] = "clarification"
    clarification_id: UUID
    reply_to_run_id: UUID
    revision: StrictInt = Field(ge=1)
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=32)
    schema_links: list[SchemaLink] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def unique_questions(self):
        """拒绝空身份、重复问题或重复槽位，具体候选仍在会话内核验。"""
        if (self.clarification_id.int == 0 or self.reply_to_run_id.int == 0
                or len({item.question_id for item in self.questions}) != len(self.questions)
                or len({item.slot for item in self.questions}) != len(self.questions)):
            raise ValueError("澄清问题身份无效")
        return self


class RAGResult(ContractModel):
    """完整保留原RAGAnswer/Citation；原引用快照不冒充已重新核对的当前来源。"""

    kind: Literal["rag"] = "rag"
    result: RAGAnswer
    source_validation: Literal["snapshot_only", "verified"] = "snapshot_only"
    sources: list[EvidenceTrace] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def source_identity(self):
        """只有逐条核对原Citation身份的EvidenceTrace才可声明verified。"""
        citations = {(item.kb_id, item.doc_id, item.chunk_id) for item in self.result.citations}
        sources = {(item.kb_id, item.doc_id, item.chunk_id) for item in self.sources}
        if self.source_validation == "snapshot_only":
            if self.sources:
                raise ValueError("未核对来源不能携带已验证快照")
        elif not citations or sources != citations or len(sources) != len(self.sources):
            raise ValueError("已核对来源与引用不一致")
        return self


class FormulaResult(ContractModel):
    """原公式定义/缺参/机械计算结果直传，保留manual_pending语义与单位状态。"""

    kind: Literal["formula"] = "formula"
    result: FormulaCalculationResult | FormulaSemanticResult


class FailureResult(ContractModel):
    """固定安全错误码，前端使用本地说明映射；不携带异常原文、凭据或半份答案。"""

    kind: Literal["failure"] = "failure"
    code: ErrorCode


class UnsupportedResult(ContractModel):
    """未实现能力显式标记，不能用旧Demo结果或模拟数据冒充成功。"""

    kind: Literal["unsupported"] = "unsupported"
    capability: Literal["automatic_cross_source_planning", "unified_agent_http", "source_resolution_http"]


class TraceStep(ContractModel):
    """真实业务步骤的安全摘要；不是模型思维链，也不包含问题、正文、SQL或异常原文。"""

    step_id: UUID
    tool: Literal["sql", "rag", "formula", "clarification"]
    operation: Literal[
        "intent", "schema_linking", "clarification", "sql_generate", "sql_validate", "sql_execute",
        "rag_search", "rag_synthesize", "formula_extract", "formula_bind", "formula_calculate",
    ]
    status: Literal["succeeded", "failed", "skipped", "unknown"]
    sql_attempt: StrictInt | None = Field(default=None, ge=1, le=2)
    elapsed_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    llm_calls: StrictInt | None = Field(default=None, ge=0, le=4)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def consistent_step(self):
        """步骤归属与状态不能矛盾；耗时/调用数未知留null，不伪造测量值。"""
        operation_tool = self.operation.split("_", 1)[0]
        if self.operation == "schema_linking":
            operation_tool = "sql"
        if self.operation != "intent" and self.tool != operation_tool:
            raise ValueError("Trace步骤与工具不一致")
        if self.operation.startswith("sql_") != (self.sql_attempt is not None):
            raise ValueError("SQL步骤缺少尝试关联或非SQL步骤附带尝试")
        if (self.status == "succeeded" and self.error_code is not None
                or self.status in {"failed", "unknown"} and self.error_code is None):
            raise ValueError("Trace结果与错误码不一致")
        return self


Result = Annotated[
    SQLResult | RAGResult | FormulaResult | ClarificationPrompt | FailureResult | UnsupportedResult,
    Field(discriminator="kind"),
]


class AgentResponse(ContractModel):
    """预留统一响应；所有页面区块由真实工具结果派生，不能仅凭answer文本判成功。"""

    version: Literal["contest-agent-v1"] = "contest-agent-v1"
    context: RunContext
    status: Literal[
        "answered", "empty", "needs_clarification", "insufficient_evidence",
        "need_variables", "unsupported", "failed",
    ]
    result: Result
    trace: list[TraceStep] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def consistent_status(self):
        """终态与原工具状态一致；澄清关联当前run，重复Trace身份拒绝。"""
        payload = self.result
        if isinstance(payload, SQLResult):
            expected = {"success": "answered", "empty": "empty", "failed": "failed"}[payload.status]
            # 同一run中的两次SQL尝试分别关联生成/校验/执行，避免修复前后的记录混用。
            steps = {(item.sql_attempt, item.operation, item.status) for item in self.trace}
            for attempt in payload.attempts:
                required = {(attempt.attempt, "sql_generate", "succeeded"),
                            (attempt.attempt, "sql_validate", "succeeded" if attempt.validation == "passed" else "failed")}
                if attempt.execution != "not_run":
                    required.add((attempt.attempt, "sql_execute", attempt.execution))
                elif any(item.sql_attempt == attempt.attempt and item.operation == "sql_execute"
                         and item.status != "skipped" for item in self.trace):
                    raise ValueError("未执行SQL与Trace矛盾")
                if not required.issubset(steps):
                    raise ValueError("SQL尝试缺少对应生成、校验或执行Trace")
        elif isinstance(payload, RAGResult):
            expected = "answered" if payload.result.status == "success" else "insufficient_evidence"
        elif isinstance(payload, FormulaResult):
            expected = {"success": "answered", "need_variables": "need_variables",
                        "insufficient_evidence": "insufficient_evidence",
                        "unsupported_formula": "unsupported"}[payload.result.status]
        elif isinstance(payload, ClarificationPrompt):
            expected = "needs_clarification"
            if payload.reply_to_run_id != self.context.run_id:
                raise ValueError("澄清关联了不同执行")
        else:
            expected = "failed" if isinstance(payload, FailureResult) else "unsupported"
        if (self.status != expected or any(item.step_id.int == 0 for item in self.trace)
                or len({item.step_id for item in self.trace}) != len(self.trace)):
            raise ValueError("响应终态或Trace身份无效")
        sql_steps = [(item.sql_attempt, item.operation) for item in self.trace if item.sql_attempt is not None]
        if len(set(sql_steps)) != len(sql_steps):
            raise ValueError("同次SQL尝试不能重复记录相同步骤")
        return self
