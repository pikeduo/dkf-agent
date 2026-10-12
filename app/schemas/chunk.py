"""管理员切片投递与证据查询契约，不返回向量、本机路径或原件连接串。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ChunkTaskResponse(BaseModel):
    """切片独立任务编号；202 表示接收/恢复，不等于完成向量化。"""

    doc_id: UUID
    kb_id: UUID
    chunk_task_id: UUID
    status: Literal["CHUNKING", "EMBEDDING"]


class ChunkResponse(BaseModel):
    """返回切片正文及来源跨度，仅供可信管理员反查证据。"""

    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    doc_id: UUID
    kb_id: UUID
    text: str
    page_start: int
    page_end: int
    section: str | None
    metadata: dict[str, Any] = Field(validation_alias="chunk_metadata")
    created_at: datetime
