"""显式 pgvector 入库响应，不输出向量、模型路径或私有配置。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class IndexTaskResponse(BaseModel):
    """独立 CPU 入库任务编号；202 为接收/恢复，READY 以数据库文档状态为准。"""

    doc_id: UUID
    kb_id: UUID
    index_task_id: UUID
    status: Literal["INDEXING", "READY", "PARTIAL_READY"]
