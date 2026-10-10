"""云端任务对外安全视图；不包含 Token、签名 URL 或本地磁盘路径。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ParseJobResponse(BaseModel):
    """查询持久化外部状态，不将单个 Celery 短任务 SUCCESS 当作整份解析完成。"""

    model_config = ConfigDict(from_attributes=True)
    job_id: UUID
    doc_id: UUID
    provider: str
    model_version: str
    data_id: str
    batch_id: str | None
    task_id: UUID
    trace_id: str | None
    state: str
    retry_count: int
    error_code: str | None
    error_message: str | None
    is_active: bool
    submitted_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
