"""短步骤云端任务状态机；数据库检查点和行锁防止重复提交、过期覆盖及重复写块。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Document, DocumentParseJob
from app.services.document_processing import (
    persist_blocks,
    source_path,
    transition_document,
)
from app.services.mineru.adapter import MinerUResultAdapter, page_geometry
from app.services.mineru.errors import MinerUError, api_error
from app.services.mineru.provider import LocalFile, validate_local_file


def parse_options(settings: Settings) -> dict:
    """保存当前解析参数快照，不把 Token、URL 或频控配置纳入业务解析身份。"""

    return {
        "model_version": settings.mineru_model_version,
        "language": settings.mineru_language,
        "is_ocr": settings.mineru_is_ocr,
        "enable_table": settings.mineru_enable_table,
        "enable_formula": settings.mineru_enable_formula,
    }


def local_file(document: Document, job: DocumentParseJob) -> LocalFile:
    """把已有存储记录映射为当前代次原件，Provider 再校验实际内容 Hash。"""

    return LocalFile(
        source_path(document), document.file_name, job.data_id, job.file_hash
    )


def create_job(
    session: Session, document: Document, settings: Settings
) -> DocumentParseJob:
    """在文档行锁内创建有效解析身份；相同活动任务返回原记录，不做完成结果缓存。"""

    document = session.scalar(
        select(Document)
        .where(Document.doc_id == document.doc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    active = session.scalar(
        select(DocumentParseJob).where(
            DocumentParseJob.doc_id == document.doc_id,
            DocumentParseJob.is_active.is_(True),
        )
    )
    options = parse_options(settings)
    identity = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    if active:
        if (
            active.file_hash == document.file_hash
            and active.options_hash == identity
            and active.task_id == document.processing_task_id
        ):
            return active
        raise MinerUError("ACTIVE_JOB", "文档已有其他有效解析任务，不重复提交")
    if document.status not in {"UPLOADED", "FAILED"}:
        raise MinerUError(
            "DOCUMENT_STATE", "文档已进入处理阶段，不提供 Reparse 或 Reindex"
        )
    job_id = uuid4()
    job = DocumentParseJob(
        job_id=job_id,
        doc_id=document.doc_id,
        provider="mineru_cloud",
        model_version=settings.mineru_model_version,
        file_hash=document.file_hash,
        parse_options=options,
        options_hash=identity,
        data_id=f"dkf-{job_id.hex}",
        task_id=uuid4(),
        state="waiting-file",
        retry_count=0,
        is_active=True,
        submission_attempted=False,
        upload_complete=False,
    )
    validate_local_file(local_file(document, job))
    if document.status == "FAILED":
        transition_document(document, "UPLOADED")
    transition_document(document, "PARSING")
    document.processing_task_id = job.task_id
    session.add(job)
    session.flush()
    return job


def lock_job(
    session: Session, job_id: UUID
) -> tuple[Document, DocumentParseJob] | None:
    """固定按文档再任务加锁；过期代次、终态和缺失记录不执行云端或写块。"""

    initial = session.get(DocumentParseJob, job_id)
    if initial is None:
        return None
    document = session.scalar(
        select(Document)
        .where(Document.doc_id == initial.doc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = session.scalar(
        select(DocumentParseJob)
        .where(DocumentParseJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        document is None
        or job is None
        or not job.is_active
        or document.status != "PARSING"
        or document.processing_task_id != job.task_id
    ):
        return None
    return document, job


def check_deadline(job: DocumentParseJob, settings: Settings) -> None:
    """限制云端任务总体等待时间，避免消息无限调度或挂起。"""

    if (
        datetime.now(UTC) - job.created_at
    ).total_seconds() > settings.mineru_job_timeout_seconds:
        raise MinerUError("JOB_TIMEOUT", "MinerU 云端解析等待超时，请人工核对任务")


def submit_step(
    session: Session, job_id: UUID, provider, settings: Settings
) -> tuple[str, int]:
    """持久化申请意图及 batch 后 PUT，同一 batch 上传恢复不重复 POST；返回下一短步骤。"""

    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    check_deadline(job, settings)
    if job.upload_complete:
        return "check", 0
    if job.batch_id is None:
        if job.submission_attempted:
            age = (datetime.now(UTC) - job.submission_started_at).total_seconds()
            wait = settings.mineru_request_timeout_seconds + 30 - age
            if wait > 0:
                return "submit", max(1, int(wait))
            raise MinerUError(
                "SUBMIT_UNCERTAIN", "MinerU 提交结果不确定，禁止自动重复申请批次"
            )
        source = local_file(document, job)
        validate_local_file(source)
        job.submission_attempted = True
        job.submission_started_at = datetime.now(UTC)
        # 必须在非幂等 POST 前提交意图；重启时不能无条件再创建远端批次。
        session.commit()
        batch = provider.request_upload_urls([source], job.parse_options)
        locked = lock_job(session, job_id)
        if not locked:
            return "skipped", 0
        document, job = locked
        job.batch_id, job.trace_id = batch.batch_id, batch.trace_id
        job.upload_url = batch.urls[0]
        job.upload_expires_at = datetime.now(UTC) + timedelta(hours=24)
        job.submitted_at = datetime.now(UTC)
        # 申请结果先提交，再开始 PUT；临时上传失败继续使用同一 URL 与 batch。
        session.commit()
        locked = lock_job(session, job_id)
        if not locked:
            return "skipped", 0
        document, job = locked
    if job.upload_expires_at is None or job.upload_expires_at <= datetime.now(UTC):
        raise MinerUError("UPLOAD_EXPIRED", "MinerU 上传链接已失效，请人工重新提交")
    provider.upload(job.upload_url, local_file(document, job))
    job.upload_complete = True
    job.upload_url = None
    job.error_code = job.error_message = document.error_message = None
    session.commit()
    return "check", settings.mineru_poll_interval_seconds


def result_failure(payload: dict) -> MinerUError:
    """对官方明确的临时失败原因做受控分类，其他失败不猜测、不暴露 err_msg。"""

    transient = {
        "服务异常": "-10001",
        "模型服务暂时不可用": "-60007",
        "任务提交队列已满": "-60009",
        "解析失败": "-60010",
        "文件拆分失败": "-60020",
        "读取文件页数失败": "-60021",
    }
    message = payload.get("err_msg")
    code = transient.get(message) if isinstance(message, str) else None
    return (
        api_error(code)
        if code
        else MinerUError(
            "REMOTE_FAILED", "MinerU 云端解析失败，请在平台核对格式、权限和原件"
        )
    )


def check_step(
    session: Session, job_id: UUID, provider, settings: Settings
) -> tuple[str, int]:
    """只检查一次；完成时下载并事务落库，未完成立即结束并由 Celery 延时再检查。"""

    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    check_deadline(job, settings)
    if not job.upload_complete or not job.batch_id:
        return "submit", 0
    result = provider.query(job.batch_id, job.data_id)
    job.state, job.trace_id = result["state"], result["trace_id"]
    if job.state == "failed":
        error = result_failure(result)
        if error.retryable and job.retry_count < settings.mineru_max_retries:
            # 仅远端明确终止后可申请新 batch；旧外部任务已终态，不同时存在有效任务。
            job.batch_id = job.upload_url = job.upload_expires_at = None
            job.submission_attempted = job.upload_complete = False
            job.state = "waiting-file"
            # 已确认远端失败的检查点不能随后续错误记录回滚，否则会不断查询旧批次。
            session.commit()
        raise error
    if job.state != "done":
        job.error_code = job.error_message = document.error_message = None
        session.commit()
        return "check", settings.mineru_poll_interval_seconds
    archive = provider.download_result(result.get("full_zip_url"))
    source = local_file(document, job)
    validate_local_file(source)
    parsed = MinerUResultAdapter().normalize(
        archive,
        doc_id=document.doc_id,
        file_name=document.file_name,
        file_type=document.file_type,
        geometry=page_geometry(source.path, document.file_type),
        identity=job.file_hash + ":" + job.options_hash,
    )
    persist_blocks(session, document, parsed)
    transition_document(document, "CHUNKING")
    job.state, job.is_active = "done", False
    job.finished_at = datetime.now(UTC)
    job.error_code = job.error_message = None
    session.commit()
    return "done", len(parsed.blocks)


def record_error(
    session: Session, job_id: UUID, error: MinerUError, settings: Settings
) -> bool:
    """用新事务保存安全错误，返回是否继续重试；旧任务不得覆盖新代次或完成结果。"""

    locked = lock_job(session, job_id)
    if not locked:
        return False
    document, job = locked
    # 明确收到失败响应且未建立 batch 可重新申请；响应 / 提交确认不确定不重置意图。
    if (
        error.retryable
        and job.batch_id is None
        and error.code not in {"DATABASE", "UNKNOWN"}
    ):
        job.submission_attempted = False
    retry = error.retryable and job.retry_count < settings.mineru_max_retries
    if retry:
        job.retry_count += 1
    else:
        job.state, job.is_active = "failed", False
        job.finished_at = datetime.now(UTC)
        job.upload_url = None
        transition_document(document, "FAILED", str(error))
    job.error_code, job.error_message = error.code, str(error)
    if retry:
        document.error_message = str(error)
    session.commit()
    return retry
