"""公式发现与显式执行契约；发现不授予云调用，也不自动选择或绑定参数。"""

from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.schemas.document_analysis import AnalysisModel
from app.schemas.evidence import PageSemantics


class FormulaExtractRequest(AnalysisModel):
    """准确选择库/文档/Chunk/Block，每次明确允许外传后才结构化原式。"""

    kb_id: UUID
    doc_id: UUID
    chunk_id: UUID
    block_id: UUID
    allow_cloud: StrictBool = False

    @field_validator("kb_id", "doc_id", "chunk_id", "block_id")
    @classmethod
    def nonzero_identity(cls, value):
        """显式来源选择不能使用零UUID表示自动寻找或全库。"""
        if value.int == 0:
            raise ValueError("公式来源标识无效")
        return value


class FormulaCandidate(AnalysisModel):
    """来自已重新核对Evidence的完整公式；仅是候选，不代表含义已经正确识别。"""

    kb_id: UUID
    doc_id: UUID
    chunk_id: UUID
    block_id: UUID
    file_name: StrictStr
    file_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_parse_task_id: UUID
    source_chunk_task_id: UUID
    page: StrictInt = Field(ge=1)
    page_semantics: PageSemantics
    section: StrictStr | None
    raw_formula: StrictStr = Field(min_length=1, max_length=20000)
    block_text_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_rank: StrictInt = Field(ge=1, le=5)
    document_status: Literal["READY", "PARTIAL_READY"]


class FormulaDiscoveryResult(AnalysisModel):
    """只覆盖本次最终检索Chunk，不把空候选说成整份文档没有公式。"""

    status: Literal["candidates", "no_candidate_in_retrieved_chunks"]
    scope: Literal["retrieved_chunks_only"] = "retrieved_chunks_only"
    candidates: list[FormulaCandidate] = Field(max_length=100)
    retrieved_chunk_count: StrictInt = Field(ge=0, le=5)
    rejected_formula_span_count: StrictInt = Field(ge=0)
    omitted_candidate_count: StrictInt = Field(ge=0)
    semantic_validation: Literal["manual_pending"] = "manual_pending"

    @model_validator(mode="after")
    def consistent_candidates(self):
        """候选状态与数量一致，相同文档Block不得因多个检索Chunk重复发布。"""
        identities = [(item.doc_id, item.block_id) for item in self.candidates]
        if ((self.status == "candidates") != bool(identities) or len(set(identities)) != len(identities)
                or any(item.retrieval_rank > self.retrieved_chunk_count for item in self.candidates)):
            raise ValueError("公式候选状态无效")
        return self
