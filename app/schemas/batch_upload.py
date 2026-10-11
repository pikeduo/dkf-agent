"""批量上传的逐文件结果；一个 Document 始终对应一个原件。"""

from uuid import UUID

from pydantic import BaseModel

from app.schemas.knowledge_base import DocumentResponse
from app.schemas.parse_job import ParseJobResponse


class BatchUploadItem(BaseModel):
    """按请求顺序返回单项状态；失败也可能已有原件，不能据此盲目重新上传。"""

    index: int
    file_name: str | None
    status_code: int
    doc_id: UUID | None = None
    document: DocumentResponse | None = None
    parse_job: ParseJobResponse | None = None
    detail: str | dict | None = None


class BatchUploadResponse(BaseModel):
    """汇总本次逐项接收结果，不代表后台解析成功或数据库整批原子提交。"""

    total: int
    accepted_count: int
    failed_count: int
    items: list[BatchUploadItem]
