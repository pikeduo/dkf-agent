"""Knowledge Agent自有协议；不向调用方暴露LangGraph、LangChain或LlamaIndex对象。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from app.schemas.evidence import Citation, EvidenceTrace
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_binding import EvidenceVariableInput, UserVariableInput
from app.schemas.formula_calculation import FormulaCalculationResult
from app.schemas.knowledge_query import KnowledgeQueryRequest
from app.schemas.rag import RAGAnswer
from app.schemas.retrieval import RetrievalResult

AgentRoute = Literal["knowledge_search", "knowledge_query", "formula_request"]
AgentStatus = Literal["success", "empty", "insufficient_evidence", "need_variables", "unsupported_formula", "failed"]
AgentErrorCode = Literal["AGENT_INPUT", "AGENT_PERMISSION", "AGENT_ROUTE", "AGENT_STEPS",
                         "AGENT_DEPENDENCY", "AGENT_TOOL", "AGENT_RESULT", "AGENT_INTERNAL"]


class AgentFormulaRequest(BaseModel):
    """显式定位已有公式及参数；可复用旧定义避免补参时再次收费，计算仍复核当前来源。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    doc_id: UUID
    chunk_id: UUID
    block_id: UUID
    definition: FormulaSemanticResult | None = None
    user_inputs: list[UserVariableInput] = Field(default_factory=list, max_length=32)
    evidence_inputs: list[EvidenceVariableInput] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_selection(self):
        """非零来源身份及复用定义必须与所选原公式完全一致，不接受未完成定义重放。"""
        if any(value.int == 0 for value in (self.doc_id, self.chunk_id, self.block_id)):
            raise ValueError("公式来源无效")
        if self.definition is not None and (self.definition.status != "success"
                or self.definition.source.doc_id != self.doc_id or self.definition.source.chunk_id != self.chunk_id
                or self.definition.formula_block_id != self.block_id):
            raise ValueError("公式定义与所选来源不一致")
        return self


class KnowledgeAgentRequest(KnowledgeQueryRequest):
    """默认普通问答；明确search仅检索，公式必须选来源，不以关键词猜测并调用收费Tool。"""

    intent: Literal["auto", "knowledge_search", "knowledge_query", "formula_request"] = "auto"
    formula: AgentFormulaRequest | None = None

    @model_validator(mode="after")
    def fixed_formula_scope(self):
        """公式、参数文档与全局doc_ids保持一致；拒绝互斥意图及跨库旧定义。"""
        if self.intent == "formula_request" and self.formula is None:
            raise ValueError("公式请求缺少来源")
        if self.formula is not None:
            if self.intent not in {"auto", "formula_request"}:
                raise ValueError("请求意图冲突")
            selected = self.formula
            if (selected.definition is not None and selected.definition.source.kb_id != self.kb_id
                    or self.doc_ids is not None and any(doc_id not in self.doc_ids for doc_id in
                        [selected.doc_id, *[item.doc_id for item in selected.evidence_inputs]])
                    or any(value.int == 0 for item in selected.evidence_inputs
                           for value in (item.doc_id, item.chunk_id, item.block_id))):
                raise ValueError("公式或参数来源超出指定范围")
        return self


class KnowledgeAgentResponse(BaseModel):
    """未来Coordinator只消费此项目协议；公式证据使用原EvidenceTrace，不伪造Citation分数。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: AgentStatus
    answer: StrictStr | None = Field(default=None, max_length=20000)
    entities: dict = Field(default_factory=dict)
    evidence: list[Citation | RetrievalResult | EvidenceTrace] = Field(default_factory=list, max_length=33)
    formula_results: list[FormulaCalculationResult | FormulaSemanticResult] = Field(default_factory=list, max_length=1)
    insufficient_evidence: StrictBool = False
    agent_run_id: UUID
    selected_route: AgentRoute | None = None
    error_code: AgentErrorCode | None = None
    step_count: StrictInt = Field(ge=0, le=32)

    @model_validator(mode="after")
    def consistent_result(self):
        """错误清空业务半成品；Query沿用原引用校验，缺参/不支持不能伪装成功。"""
        if self.agent_run_id.int == 0 or self.entities:
            raise ValueError("Agent身份或实体状态无效")
        if self.status == "failed":
            if (self.error_code is None or self.answer is not None or self.evidence
                    or self.formula_results or self.insufficient_evidence):
                raise ValueError("失败不能发布部分结果")
            return self
        if self.error_code is not None or self.selected_route is None:
            raise ValueError("Agent完成状态无效")
        if self.insufficient_evidence != (self.status == "insufficient_evidence"):
            raise ValueError("证据不足状态不一致")
        if self.selected_route == "knowledge_query":
            if self.formula_results or any(not isinstance(item, Citation) for item in self.evidence):
                raise ValueError("问答引用契约无效")
            RAGAnswer(status=self.status, answer=self.answer, citations=self.evidence,
                      insufficient_evidence=self.insufficient_evidence)
        elif self.selected_route == "knowledge_search":
            if (self.status not in {"success", "empty"} or self.answer is not None or self.formula_results
                    or (self.status == "success") != bool(self.evidence)
                    or len(self.evidence) > 5 or any(type(item) is not RetrievalResult for item in self.evidence)):
                raise ValueError("搜索结果无效")
        else:
            if len(self.formula_results) != 1 or self.formula_results[0].status != self.status:
                raise ValueError("公式状态不一致")
            if any(not isinstance(item, EvidenceTrace) for item in self.evidence):
                raise ValueError("公式来源无效")
        return self
