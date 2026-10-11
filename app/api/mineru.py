"""显式 MinerU 管理入口，不自动选择 Parser，不改变现有原生上传接口。"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from sqlalchemy import select

from app.api.admin_knowledge_bases import (
    AdminSession,
    require_document,
    require_knowledge_base,
)
from app.core.config import get_settings
from app.core.business_logging import log_event
from app.core.file_formats import MINERU_TYPES
from app.models import Document, DocumentParseJob
from app.schemas.batch_upload import BatchUploadResponse
from app.schemas.knowledge_base import DocumentResponse
from app.schemas.parse_job import ParseJobResponse
from app.services.batch_upload import MAX_BATCH_FILES, store_batch, validate_batch
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
    """读取配置，缓存命中无需 Token；真正 miss 时在提交本地云任务前校验 Token。"""

    settings = get_settings()
    return settings


def start_job(session, document: Document, settings) -> ParseJobResponse:
    """先提交本地任务，再投递短步骤；队列失败保留活动任务，显式入口可恢复投递。"""

    try:
        job = create_job(session, document, settings)
        if job.is_active:
            provider = MinerUCloudProvider(settings)
            provider.close()
        session.commit()
    except MinerUError as exc:
        session.rollback()
        code = (
            503 if exc.code in {"CONFIG", "TOKEN_MISSING"} else 409
            if exc.code in {"ACTIVE_JOB", "DOCUMENT_STATE", "CACHE_IN_PROGRESS", "CACHE_REPARSE_REQUIRED"}
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
    if not job.is_active:
        # done 且 batch=NULL 表示缓存回放，不伪造外部上传/解析状态，也不发短任务。
        session.commit()
        log_event("cache.persisted", doc_id=response.doc_id, job_id=response.job_id,
                  parse_cache_hit=True, status="CHUNKING", mineru_file_submit_count=0)
        return response
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
    response_model=ParseJobResponse | BatchUploadResponse,
    status_code=202,
    summary="上传一份或多份 PDF、图片或 Office 并显式提交 MinerU 云端解析",
    responses={
        200: {
            "model": BatchUploadResponse,
            "description": "多文件逐项报告；不代表全部成功",
        },
        404: {"description": "知识库不存在"},
        409: {"description": "内容重复，返回已有 doc_id"},
        413: {"description": "文件、页数超限或文件数量超过 50"},
        415: {"description": "格式不支持或内容不匹配；不接受 CSV/TXT/MD"},
        422: {"description": "原件无法读取，已保存时返回 doc_id"},
    },
)
def upload_mineru_document(
    kb_id: UUID,
    session: AdminSession,
    response: Response,
    file: Annotated[
        list[UploadFile],
        File(
            description="可多选 1～50 份 PDF、图片或 Office；每份独立 doc_id，将传至 MinerU 云端",
            # 和原生入口共用多选插件，但文件类型初检限于显式云解析支持范围。
            json_schema_extra={
                "items": {"type": "string", "format": "binary"},
                "x-dkf-file-picker": {
                    "extensions": sorted(MINERU_TYPES),
                    "maxFiles": MAX_BATCH_FILES,
                },
            },
        ),
    ],
) -> ParseJobResponse | BatchUploadResponse:
    """原显式入口支持多选；单份保留原响应，多份各建云任务，不自动选择 Parser。"""

    try:
        validate_batch(file)
        require_knowledge_base(session, kb_id)
        settings = require_configuration()
        if len(file) == 1:
            upload = file[0]
            if (
                Path(upload.filename or "").suffix.lower().lstrip(".")
                not in MINERU_TYPES
            ):
                raise HTTPException(
                    status_code=415,
                    detail="MinerU 仅接受 PDF、图片与 DOC/DOCX/PPT/PPTX/XLS/XLSX",
                )
            saved = store_document(session, kb_id, upload, settings)
            return start_job(session, session.get(Document, saved.doc_id), settings)

        def dispatch(response: DocumentResponse) -> ParseJobResponse:
            """复用当前单文档云任务建立与投递，仍为每份原件保留独立任务身份。"""

            return start_job(session, session.get(Document, response.doc_id), settings)

        response.status_code = 200
        return store_batch(
            session, kb_id, file, settings, dispatch, allowed_types=MINERU_TYPES
        )
    finally:
        for upload in file:
            upload.file.close()


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
