"""短步骤云端任务状态机；数据库检查点和行锁防止重复提交、过期覆盖及重复写块。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.models import Document, DocumentParseJob
from app.parsers.routing.contracts import RouteDecision
from app.parsers.routing.guard import CompletenessError, guard_result
from app.services.document_processing import (
    persist_blocks,
    source_path,
    transition_document,
)
from app.services.mineru.adapter import MinerUResultAdapter, page_geometry
from app.services.mineru.errors import MinerUError, api_error
from app.services.mineru.provider import LocalFile, validate_local_file
from app.services.hybrid_processing import bundle_file, checked_context, complete_hybrid, metrics
from app.parsers.hybrid import BundleManifest


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
    """整份任务读取原件；Hybrid 仅映射一个受检 Bundle，Document 仍属于源 XLSX。"""

    if "_hybrid" in job.parse_options:
        path, file_hash = bundle_file(document, job.parse_options["_hybrid"])
        return LocalFile(path, "visual_bundle.pdf", job.data_id, file_hash)

    return LocalFile(
        source_path(document), document.file_name, job.data_id, job.file_hash
    )


def create_job(
    session: Session,
    document: Document,
    settings: Settings,
    *,
    routing_task_id: UUID | None = None,
    route_decision: dict | None = None,
    hybrid_context: dict | None = None,
) -> DocumentParseJob:
    """创建 Whole/单 Bundle 云身份；冻结 Hybrid 检查点，活动恢复不改为新 Whole 任务。"""

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
    if hybrid_context is not None:
        checked_context(document, hybrid_context)
        if (routing_task_id is None or document.processing_task_id != routing_task_id
            or hybrid_context["routing_task_id"] != str(routing_task_id)
            or document.status != "PARSING"):
            raise MinerUError("ROUTE_INVALID", "Hybrid 检查点必须属于当前路由处理代次")
        options = options | {"_hybrid": hybrid_context}
    identity = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    if active:
        # 原 /mineru 恢复入口没有 Hybrid 参数；只恢复同原件、同配置和当前活动检查点。
        if (hybrid_context is None and "_hybrid" in active.parse_options
            and active.file_hash == document.file_hash
            and active.task_id == document.processing_task_id
            and {key: active.parse_options.get(key) for key in options} == options):
            return active
        if (
            active.file_hash == document.file_hash
            and active.options_hash == identity
            and active.task_id == document.processing_task_id
        ):
            return active
        raise MinerUError("ACTIVE_JOB", "文档已有其他有效解析任务，不重复提交")
    handoff = (
        document.status == "PARSING"
        and routing_task_id is not None
        and document.processing_task_id == routing_task_id
    )
    if document.status not in {"UPLOADED", "FAILED"} and not handoff:
        raise MinerUError(
            "DOCUMENT_STATE", "文档已进入处理阶段，不提供 Reparse 或 Reindex"
        )
    if route_decision is not None:
        # 决策只作内部元数据，不进入官方请求参数，也不改变供应商解析身份 Hash。
        checked = RouteDecision.model_validate(route_decision)
        if (
            checked.route_strategy != ("hybrid_bundle" if hybrid_context is not None else "mineru_whole")
            or checked.file_type != document.file_type
            or (hybrid_context is not None and checked.parse_mode != "auto")
        ):
            raise MinerUError("ROUTE_INVALID", "路由决策与云解析原件不一致")
        options = options | {"_route_decision": checked.model_dump(mode="json")}
    elif hybrid_context is not None:
        raise MinerUError("ROUTE_INVALID", "Hybrid 必须由当前路由代次交接")
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
        batch_log = {
            "doc_id": document.doc_id,
            "job_id": job_id,
            "batch_id": job.batch_id,
        }
        # 申请结果先提交，再开始 PUT；临时上传失败继续使用同一 URL 与 batch。
        session.commit()
        log_event("mineru.batch_created", **batch_log)
        if "_route_decision" in job.parse_options:
            log_event("router.cloud_submitted", **batch_log, mineru_file_submit_count=1)
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
    upload_log = {"doc_id": document.doc_id, "job_id": job_id, "batch_id": job.batch_id}
    session.commit()
    log_event("mineru.upload_complete", **upload_log, upload_complete=True)
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
    """只检查一次；Whole 或 Hybrid 全部校验后原子写块，未完成延时再查。"""

    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    check_deadline(job, settings)
    if not job.upload_complete or not job.batch_id:
        return "submit", 0
    result = provider.query(job.batch_id, job.data_id)
    job.state, job.trace_id = result["state"], result["trace_id"]
    log_event(
        "mineru.remote_state",
        doc_id=document.doc_id,
        job_id=job_id,
        batch_id=job.batch_id,
        state=job.state,
    )
    if job.state == "failed":
        error = result_failure(result)
        if "_route_decision" in job.parse_options:
            # Router 每代次最多一份源文件任务；已终止的远端失败由管理员决定是否重新提交。
            raise MinerUError(
                "ROUTED_REMOTE_FAILED",
                "MinerU 远端解析失败，未自动改用 Native 或追加新批次，请人工核对",
            )
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
    log_event(
        "mineru.result_downloaded",
        doc_id=document.doc_id,
        job_id=job_id,
        size_bytes=len(archive),
    )
    source = local_file(document, job)
    validate_local_file(source)
    parsed = MinerUResultAdapter().normalize(
        archive,
        doc_id=document.doc_id,
        file_name=document.file_name,
        file_type="pdf" if "_hybrid" in job.parse_options else document.file_type,
        geometry=page_geometry(source.path, "pdf" if "_hybrid" in job.parse_options else document.file_type),
        identity=job.file_hash + ":" + job.options_hash,
    )
    log_event(
        "mineru.adapter_validated",
        doc_id=document.doc_id,
        job_id=job_id,
        block_count=len(parsed.blocks),
    )
    hybrid_metrics = {}
    if "_hybrid" in job.parse_options:
        parsed, completed_manifest = complete_hybrid(
            document, job.parse_options["_hybrid"], parsed,
            RouteDecision.model_validate(job.parse_options["_route_decision"]),
        )
        job.parse_options = job.parse_options | {"_hybrid_result": completed_manifest}
        hybrid_metrics = metrics(BundleManifest.model_validate(completed_manifest))
    elif "_route_decision" in job.parse_options:
        try:
            guard_result(
                parsed,
                RouteDecision.model_validate(job.parse_options["_route_decision"]),
            )
        except CompletenessError as exc:
            log_event(
                "router.guard_failed",
                doc_id=document.doc_id,
                job_id=job_id,
                error_code="COMPLETENESS",
            )
            raise MinerUError("COMPLETENESS", str(exc)) from None
        log_event(
            "router.guard_passed",
            doc_id=document.doc_id,
            job_id=job_id,
            route_strategy="mineru_whole",
        )
    persist_blocks(session, document, parsed)
    transition_document(document, "CHUNKING")
    job.state, job.is_active = "done", False
    job.finished_at = datetime.now(UTC)
    job.error_code = job.error_message = None
    completed_doc_id = document.doc_id
    session.commit()
    log_event(
        "mineru.blocks_persisted",
        doc_id=completed_doc_id,
        job_id=job_id,
        block_count=len(parsed.blocks),
        status="CHUNKING",
        state="done",
    )
    if hybrid_metrics:
        log_event("hybrid.persisted", doc_id=completed_doc_id, job_id=job_id,
                  block_count=len(parsed.blocks), status="CHUNKING", **hybrid_metrics)
    return "done", len(parsed.blocks)


def record_error(
    session: Session, job_id: UUID, error: MinerUError, settings: Settings
) -> bool:
    """独立事务保存安全错误及 Hybrid 失败账本；旧任务不能覆盖新代次或完成结果。"""

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
        if "_hybrid" in job.parse_options:
            try:
                manifest = BundleManifest.model_validate(job.parse_options["_hybrid"]["manifest"])
                for entry in manifest.entries:
                    if entry.state in {"pending", "reused"}:
                        entry.state = "failed"
                ledger = manifest.model_dump(mode="json")
            except (KeyError, TypeError, ValueError):
                # 检查点损坏也必须记录失败，不能因账本校验再次阻断安全错误持久化。
                ledger = {"state": "failed", "error_code": "HYBRID_CHECKPOINT"}
            job.parse_options = job.parse_options | {"_hybrid_result": ledger}
    job.error_code, job.error_message = error.code, str(error)
    if retry:
        document.error_message = str(error)
    session.commit()
    return retry
