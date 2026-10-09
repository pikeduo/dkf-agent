"""独立于数据库、任务队列和 RAG 的统一文档解析数据契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

FileType = Literal["pdf", "docx", "txt", "md", "jpg", "jpeg", "png"]
BlockType = Literal["title", "text", "table", "formula", "image_text"]

# 不接受布尔值、数字字符串、NaN 或无穷值，避免无效坐标和置信度进入后续链路。
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
BoundingBox = Annotated[
    list[Coordinate], Field(strict=True, min_length=4, max_length=4)
]


class ParsedBlock(BaseModel):
    """保留单个解析块的正文和来源；坐标顺序为 x0、y0、x1、y1。"""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    # 标识由 Parser 显式提供；契约不随机生成 ID，避免重试时隐式改变块标识。
    block_id: UUID
    page: Annotated[int, Field(strict=True, ge=1)]
    section: StrictStr = ""
    block_type: BlockType
    text: StrictStr
    bbox: BoundingBox | None = None
    source: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    confidence: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        """拒绝空白来源标识，不强制限制未来 OCR Provider 的名称。"""

        if not value.strip():
            raise ValueError("解析块来源不能全为空白")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: list[float] | None) -> list[float] | None:
        """检查矩形方向；未知坐标允许为空，页面范围由具体 Parser 校验。"""

        if value is not None:
            x0, y0, x1, y1 = value
            if x1 < x0 or y1 < y0:
                raise ValueError("bbox 的右下角不能位于左上角之前")
        return value


class ParsedDocument(BaseModel):
    """所有 Parser 共用的有序块集合，不携带 Chunk、向量或生成答案。"""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    doc_id: UUID
    file_name: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=255)
    ]
    file_type: FileType
    title: StrictStr = ""
    # blocks 必须显式返回；允许空集合，但空集合不代表 OCR 或入库已经完成。
    blocks: Annotated[list[ParsedBlock], Field(strict=True)]

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        """拒绝全空白文件名，并保留数据库中的原始文件名。"""

        if not value.strip():
            raise ValueError("原始文件名不能全为空白")
        return value

    @model_validator(mode="after")
    def validate_block_ids(self) -> Self:
        """禁止同一解析结果出现重复块标识，避免后续定位和入库歧义。"""

        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("同一文档内的 block_id 必须唯一")
        return self
