"""管理员知识库接口的数据契约，不直接暴露 ORM 对象或本地文件路径。"""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Item = TypeVar("Item")


class KnowledgeBaseCreate(BaseModel):
    """创建知识库仅接收名称与说明，标识、状态和时间由服务端维护。"""

    model_config = ConfigDict(extra="forbid")

    # 先去除首尾空白，再检查长度，避免空白名称入库或形成难以区分的重复名称。
    name: Annotated[
        str,
        StringConstraints(
            strict=True, strip_whitespace=True, min_length=1, max_length=255
        ),
    ]
    description: str | None = None


class KnowledgeBaseResponse(BaseModel):
    """知识库详情和列表项共用的返回结构。"""

    model_config = ConfigDict(from_attributes=True)

    kb_id: UUID
    name: str
    description: str | None
    status: Literal["ACTIVE", "DISABLED"]
    created_at: datetime
    updated_at: datetime


class DocumentResponse(BaseModel):
    """文档列表展示元信息与失败原因，不返回磁盘路径、正文或向量。"""

    model_config = ConfigDict(from_attributes=True)

    doc_id: UUID
    kb_id: UUID
    file_name: str
    file_type: str
    file_hash: str
    source_type: Literal["preset", "uploaded"]
    status: Literal[
        "UPLOADED",
        "PARSING",
        "OCR_PROCESSING",
        "CHUNKING",
        "EMBEDDING",
        "INDEXING",
        "READY",
        "FAILED",
    ]
    error_message: str | None
    task_id: UUID | None = Field(default=None, validation_alias="processing_task_id")
    created_at: datetime
    updated_at: datetime


class PageResponse(BaseModel, Generic[Item]):
    """分页列表保留总数与请求窗口，方便管理员界面逐页读取。"""

    items: list[Item]
    total: int
    limit: int
    offset: int
