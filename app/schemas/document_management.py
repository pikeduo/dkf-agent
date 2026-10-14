"""管理员文档可观测视图；不暴露私有检查点、磁盘路径、密钥或签名链接。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.knowledge_base import DocumentResponse
from app.schemas.parse_job import ParseJobResponse


class ManagementResponse(BaseModel):
    """202表示编排已接收，任务标识及数据库状态仍须后续查询确认。"""
    doc_id: UUID
    kb_id: UUID
    management_task_id: UUID
    operation: Literal["retry", "reindex", "reparse", "delete"]
    stage: Literal["parse", "cloud", "chunk", "embed", "index", "delete", "done"]
    state: Literal["running", "failed", "done"]
    status: str
    error_code: str | None = None
    cause_code: str | None = None


class AdminParseJobResponse(ParseJobResponse):
    """补充本地云提交/上传检查点布尔值，不增加签名URL或供应商原始参数。"""
    upload_complete: bool = False
    submission_attempted: bool = False


class DocumentDetailResponse(DocumentResponse):
    """详情保留原文档协议，增加各阶段标识及最近安全云任务视图。"""
    chunk_task_id: UUID | None = None
    embedding_task_id: UUID | None = None
    management: ManagementResponse | None = None
    parse_jobs: list[AdminParseJobResponse] = Field(default_factory=list)


class DocumentStatusResponse(BaseModel):
    """只读阶段、数量和部署文件存在性；存在不代表缓存完整性或解析质量已验收。"""
    doc_id: UUID
    kb_id: UUID
    status: str
    error_message: str | None
    task_id: UUID | None
    chunk_task_id: UUID | None
    embedding_task_id: UUID | None
    index_task_id: UUID | None
    management: ManagementResponse | None
    block_count: int
    chunk_count: int
    vector_count: int
    invalid_vector_count: int
    original_present: bool
    parse_cache_present: bool
    updated_at: datetime


class BlockResponse(BaseModel):
    """原Block证据及真实阅读序号；历史空序号不伪造。"""
    model_config = ConfigDict(from_attributes=True)
    block_id: UUID
    doc_id: UUID
    block_index: int | None
    page: int
    section: str | None
    block_type: str
    text: str
    bbox: list[float] | None
    source: str
    confidence: float | None
