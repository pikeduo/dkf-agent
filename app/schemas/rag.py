"""项目自有回答与最小引用契约，不向调用方泄漏 LlamaIndex 类型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from app.schemas.retrieval import RetrievalResult

INSUFFICIENT_ANSWER = "当前检索到的证据不足以回答该问题。"


class Citation(RetrievalResult):
    """引用继承完整业务证据及分数；页码/章节只取原 Node，不由 LLM 生成。"""

    citation_id: int = Field(ge=1, le=5, strict=True)


class RAGAnswer(BaseModel):
    """稳定项目 Schema；成功必须具有可追溯引用，证据不足不是云端异常的伪装。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: Literal["success", "insufficient_evidence"]
    answer: StrictStr = Field(min_length=1, max_length=20000)
    citations: list[Citation] = Field(default_factory=list, max_length=5)
    insufficient_evidence: StrictBool

    @model_validator(mode="after")
    def consistent_answer(self):
        """约束状态、引用序号与唯一身份；不足回答使用固定安全文本且不捏造引用。"""
        if self.insufficient_evidence:
            if self.status != "insufficient_evidence" or self.citations or self.answer != INSUFFICIENT_ANSWER:
                raise ValueError("证据不足状态无效")
        elif (self.status != "success" or not self.answer.strip() or not self.citations
              or [item.citation_id for item in self.citations] != list(range(1, len(self.citations) + 1))
              or len({item.chunk_id for item in self.citations}) != len(self.citations)
              or len({item.kb_id for item in self.citations}) != 1):
            raise ValueError("回答引用状态无效")
        return self


class EvidenceClaim(BaseModel):
    """LLM 只返回逐项事实及最终 Chunk 引用，不接受 LLM 自行生成来源字段。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    text: StrictStr = Field(min_length=1, max_length=4096)
    evidence_ids: list[StrictStr] = Field(min_length=1, max_length=5)


class SynthesisOutput(BaseModel):
    """云端受限 JSON 输出；所有 ID 仍由合成层与最终 Evidence 白名单核对。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    insufficient_evidence: StrictBool
    claims: list[EvidenceClaim] = Field(max_length=12)

    @model_validator(mode="after")
    def consistent_claims(self):
        """证据不足禁止附带猜测，成功不能返回无引用的自由答案。"""
        if self.insufficient_evidence == bool(self.claims):
            raise ValueError("证据与结论状态矛盾")
        return self
