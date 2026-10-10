"""显式 MinerU 管理入口，不自动选择 Parser，不改变现有原生上传接口。"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import select

from app.api.admin_knowledge_bases import (
    AdminSession,
    require_document,
    require_knowledge_base,
)
from app.core.config import get_settings
from app.models import Document, DocumentParseJob
from app.schemas.parse_job import ParseJobResponse
from app.services.document_upload import store_document
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import create_job, lock_job
from app.services.mineru.provider import MinerUCloudProvider
from app.tasks.mineru import publish_step

router = APIRouter(
    prefix="/api/admin/knowledge-bases",
    tags=["admin-mineru"],
    responses={503: {"description": "配置、数据库、迁移或队列不可用"}},
)


def require_configuration():
    """入口只校验配置，不发送 API、不消耗额度；缺少 Token 不影响原生功能启动。"""

    settings = get_settings()
    try:
        provider = MinerUCloudProvider(settings)
        provider.close()
    except MinerUError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return settings


def start_job(session, document: Document, settings) -> ParseJobResponse:
    """先提交本地任务，再投递短步骤；队列失败保留活动任务，显式入口可恢复投递。"""

    try:
        job = create_job(session, document, settings)
        session.commit()
    except MinerUError as exc:
        session.rollback()
        code = (
            409
            if exc.code in {"ACTIVE_JOB", "DOCUMENT_STATE"}
            else (413 if exc.code in {"FILE_SIZE", "PAGE_LIMIT"} else 422)
        )
        raise HTTPException(
            status_code=code,
            detail={
                "message": str(exc),
                "doc_id": str(document.doc_id),
                "error_code": exc.code,
            },
        ) from None
    response = ParseJobResponse.model_validate(job)
    next_step = "check" if job.upload_complete else "submit"
    # ORM 刷新会开启读事务；发送消息前也释放它，避免占用连接及妨碍并发 Worker。
    session.commit()
    try:
        publish_step(response.job_id, next_step)
    except MinerUError as exc:
        session.rollback()
        # Broker 可能已接收消息但响应丢失；错误回调不能覆盖 Worker 刚完成的终态。
        locked = lock_job(session, response.job_id)
        if locked:
            _, current = locked
            current.error_code, current.error_message = exc.code, str(exc)
        session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": str(exc),
                "doc_id": str(response.doc_id),
                "job_id": str(response.job_id),
            },
        ) from None
    return response


@router.post(
    "/{kb_id}/mineru/documents",
    response_model=ParseJobResponse,
    status_code=202,
    summary="上传 PDF 或图片并显式提交 MinerU 云端解析",
    responses={
        404: {"description": "知识库不存在"},
        409: {"description": "内容重复，返回已有 doc_id"},
        413: {"description": "文件或页数超限"},
        415: {"description": "非 PDF、JPG、JPEG、PNG"},
        422: {"description": "原件无法读取，已保存时返回 doc_id"},
    },
)
def upload_mineru_document(
    kb_id: UUID,
    session: AdminSession,
    file: Annotated[
        UploadFile, File(description="单个 PDF、JPG、JPEG 或 PNG；将传至 MinerU 云端")
    ],
) -> ParseJobResponse:
    """复用原有文件保存和去重，不投递原生任务；原件留存不表示云端处理成功。"""

    try:
        require_knowledge_base(session, kb_id)
        settings = require_configuration()
        if Path(file.filename or "").suffix.lower() not in {
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
        }:
            raise HTTPException(
                status_code=415, detail="MinerU 入口仅接受 PDF、JPG、JPEG、PNG"
            )
        saved = store_document(session, kb_id, file, settings)
        return start_job(session, session.get(Document, saved.doc_id), settings)
    finally:
        file.file.close()


@router.post(
    "/{kb_id}/documents/{doc_id}/mineru",
    response_model=ParseJobResponse,
    status_code=202,
    summary="显式提交已有文档或恢复活动 MinerU 任务",
    responses={
        404: {"description": "文档不存在"},
        409: {"description": "状态不允许，不提供 Reparse 或 Reindex"},
    },
)
def submit_existing_document(
    kb_id: UUID, doc_id: UUID, session: AdminSession
) -> ParseJobResponse:
    """只接收 UPLOADED、FAILED 或同代次活动云任务；重复请求沿用已有任务身份。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    return start_job(session, document, require_configuration())


@router.get(
    "/{kb_id}/documents/{doc_id}/parse-jobs",
    response_model=list[ParseJobResponse],
    summary="查询最近 20 条云端解析任务与安全错误",
    responses={404: {"description": "文档不存在"}},
)
def list_parse_jobs(
    kb_id: UUID, doc_id: UUID, session: AdminSession
) -> list[ParseJobResponse]:
    """按知识库归属读取历史任务；不依赖 Redis，也不暴露签名下载和上传 URL。"""

    require_document(session, kb_id, doc_id)
    jobs = session.scalars(
        select(DocumentParseJob)
        .where(DocumentParseJob.doc_id == doc_id)
        .order_by(DocumentParseJob.created_at.desc(), DocumentParseJob.job_id.desc())
        .limit(20)
    ).all()
    return [ParseJobResponse.model_validate(job) for job in jobs]
