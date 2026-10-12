"""管理员文档向量化接收响应，不返回正文、向量或模型/结果私有路径。"""

from typing import Literal
from uuid import UUID
from pydantic import BaseModel


class EmbeddingTaskResponse(BaseModel):
    """独立 GPU 任务标识，INDEXING 只表示等待下一阶段，不声称已入库或可检索。"""

    doc_id: UUID
    kb_id: UUID
    embedding_task_id: UUID
    status: Literal["EMBEDDING", "INDEXING"]
