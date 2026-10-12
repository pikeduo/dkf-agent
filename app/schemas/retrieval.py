"""项目内部检索结果 DTO，只描述证据与分数，不包含 LlamaIndex 类型或持久化操作。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.core.file_formats import FileType


class RetrievalResult(BaseModel):
    """Chunk→Page/Section→Document 的运行时表示；无分数时保留 None，不虚构相关性。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kb_id: UUID
    doc_id: UUID
    chunk_id: UUID
    file_name: StrictStr = Field(min_length=1, max_length=255)
    file_type: FileType
    text: StrictStr
    page_start: StrictInt = Field(ge=1)
    page_end: StrictInt = Field(ge=1)
    section: StrictStr | None
    metadata: dict[str, Any]
    embedding_identity: dict[str, Any] | None = None
    retrieval_score: float | None = Field(default=None, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_evidence(self) -> "RetrievalResult":
        """检查原文非空和页范围；保留原字符串与逻辑/物理页语义，不修剪或猜测内容。"""

        if not self.text.strip() or self.page_end < self.page_start:
            raise ValueError("证据正文或页范围无效")
        return self
