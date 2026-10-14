"""Knowledge API自有请求/响应契约，不包含LlamaIndex内部类型。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.schemas.retrieval import RetrievalResult
from app.services.rag.dense import MAX_DOC_IDS, MAX_QUERY_CHARS, MAX_TOP_K, validate_doc_ids, validate_query, validate_scope


class KnowledgeSearchRequest(BaseModel):
    """原问题及固定知识库/文档范围；top_k是融合候选上限，不是最终Evidence数量。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kb_id: UUID = Field(description="指定知识库非零UUID，不跨库检索")
    question: StrictStr = Field(min_length=1, max_length=MAX_QUERY_CHARS, description="原始问题，不改写")
    doc_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=MAX_DOC_IDS,
                                     description="省略/null表示该库全部合格文档，空列表拒绝")
    top_k: StrictInt = Field(default=30, ge=1, le=MAX_TOP_K, description="融合候选上限；最终重排最多5条")

    @field_validator("kb_id")
    @classmethod
    def valid_kb(cls, value):
        """复用非零业务UUID校验，不接收零标识作为全库范围。"""
        try:
            return validate_scope(value, 30)
        except Exception:
            raise ValueError("知识库标识无效") from None

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        """保留原问题，拒绝空白、控制符和非法Unicode，不添加指令或修剪文本。"""
        try:
            value = validate_query(value)
            value.encode("utf-8", errors="strict")
            return value
        except Exception:
            raise ValueError("问题无效") from None

    @field_validator("doc_ids")
    @classmethod
    def valid_documents(cls, value):
        """显式文档范围去重排序，None保留全库语义，空范围/零UUID拒绝。"""
        try:
            scope = validate_doc_ids(value)
            return list(scope) if scope is not None else None
        except Exception:
            raise ValueError("文档范围无效") from None


class KnowledgeQueryRequest(KnowledgeSearchRequest):
    """真实问答须每次明确同意问题/最终正文外传及费用，默认不触发云端。"""

    allow_cloud: StrictBool = Field(default=False, description="本次明确确认问题/最终正文云外传及费用；不是鉴权")


class KnowledgeSearchResponse(BaseModel):
    """统一DKF Evidence列表，空集不是服务错误，分数不是概率置信度。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: Literal["success", "empty"]
    evidence: list[RetrievalResult] = Field(max_length=5)

    @model_validator(mode="after")
    def consistent_evidence(self):
        """状态与证据数量一致，同一稳定Chunk仅一次，不能混合知识库。"""
        if ((self.status == "success") != bool(self.evidence)
                or len({item.chunk_id for item in self.evidence}) != len(self.evidence)
                or len({item.kb_id for item in self.evidence}) > 1):
            raise ValueError("检索响应无效")
        return self


class KnowledgeErrorDetail(BaseModel):
    """固定错误码/中文说明，不承载上游原文、输入或堆栈。"""

    code: StrictStr
    message: StrictStr


class KnowledgeErrorResponse(BaseModel):
    """HTTP错误统一包裹，与FastAPI detail字段保持兼容。"""

    detail: KnowledgeErrorDetail
