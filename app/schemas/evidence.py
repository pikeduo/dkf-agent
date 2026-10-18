"""框架无关的引用及已核对来源契约，不包含模型对象、向量或原件路径。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from app.core.file_formats import FileType
from app.schemas.parsed_document import BlockType, BoundingBox, ParsedBlock
from app.schemas.retrieval import RetrievalResult

PageSemantics = Literal["physical_pdf", "image_page", "logical"]


class Citation(RetrievalResult):
    """原检索证据加本地引用编号；保持阶段22 JSON字段，来源不由模型生成。"""

    citation_id: int = Field(ge=1, le=5, strict=True)


class SourceSpan(BaseModel):
    """已有Chunk字符来源，偏移是Python Unicode字符的左闭右开区间，不是字节。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    block_id: UUID
    block_index: StrictInt = Field(ge=1)
    page: StrictInt = Field(ge=1)
    block_type: BlockType
    source: StrictStr = Field(min_length=1, max_length=64)
    bbox: BoundingBox | None
    block_text_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: dict | None = None
    page_reliable: bool = True
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    chunk_start_char: StrictInt = Field(ge=0)
    chunk_end_char: StrictInt = Field(gt=0)
    role: Literal["content", "context", "overlap"]

    @field_validator("bbox")
    @classmethod
    def ordered_bbox(cls, value):
        """复用解析契约的矩形校验，不自行推断缺失坐标。"""
        return ParsedBlock.validate_bbox(value)

    @model_validator(mode="after")
    def valid_intervals(self):
        """原文/Chunk区间长度须相同且非空，Block标识及来源不得空白。"""
        if (self.block_id.int == 0 or not self.source.strip() or self.end_char <= self.start_char
                or self.chunk_end_char <= self.chunk_start_char
                or self.end_char - self.start_char != self.chunk_end_char - self.chunk_start_char):
            raise ValueError("字符来源区间无效")
        return self


class ResolvedSourceSpan(SourceSpan):
    """已与数据库Block核对的原文片段，章节和置信度保留真实值。"""

    section: StrictStr | None
    text: StrictStr
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)


class EvidenceTrace(BaseModel):
    """当前只读快照内已核对的Chunk→Block→Document来源，不代表语义事实验真。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: Literal["verified"] = "verified"
    kb_id: UUID
    doc_id: UUID
    chunk_id: UUID
    file_name: StrictStr = Field(min_length=1, max_length=255)
    file_type: FileType
    file_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_type: Literal["preset", "uploaded"]
    document_status: Literal["READY", "PARTIAL_READY"]
    source_parse_task_id: UUID
    source_chunk_task_id: UUID
    text: StrictStr
    page_start: StrictInt = Field(ge=1)
    page_end: StrictInt = Field(ge=1)
    section: StrictStr | None
    page_semantics: PageSemantics
    source_spans: list[ResolvedSourceSpan] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def valid_source_identity(self):
        """已核对来源仍须有非空原文、有效页范围与非零身份，不生成展示占位来源。"""
        if (not self.file_name.strip() or not self.text.strip() or self.page_end < self.page_start
                or any(identity.int == 0 for identity in (self.kb_id, self.doc_id, self.chunk_id,
                    self.source_parse_task_id, self.source_chunk_task_id))):
            raise ValueError("来源身份或原文无效")
        return self
