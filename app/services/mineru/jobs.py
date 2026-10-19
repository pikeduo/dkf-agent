"""短步骤云端任务状态机；数据库检查点和行锁防止重复提交、过期覆盖及重复写块。"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.models import Document, DocumentParseJob
from app.parsers.routing.contracts import OFFICE_SNAPSHOT_VERSION, RouteDecision
from app.parsers.routing.guard import CompletenessError, guard_result
from app.parsers.routing.inspector import inspect_document
from app.parsers.common import ParserError
from app.services.document_processing import (
    persist_blocks,
    check_source_hash,
    source_path,
    transition_document,
)
from app.services.mineru.adapter import MinerUResultAdapter, page_geometry, safe_merge_diagnostic
from app.services.mineru.errors import MinerUError, api_error, internal_failure
from app.services.mineru.provider import LocalFile, validate_local_file
from app.services.mineru.raw_results import obtain_raw_result, load_raw_result
from app.services.hybrid_processing import bundle_file, checked_context, complete_hybrid, metrics
from app.parsers.hybrid import BundleManifest
from app.services.xlsx_primary import prepare_primary, complete_primary
from app.services.parse_cache import (CacheError, ParseCache, NORMALIZER_VERSION, recipe, key_for, canonical,
    visual_recipe, archive_artifacts, guard_cached)
from app.services.cache_claims import lock_keys, claim_miss


def parse_options(settings: Settings) -> dict:
    """保存当前解析参数快照，不把 Token、URL 或频控配置纳入业务解析身份。"""

    return {
        "model_version": settings.mineru_model_version,
        "language": settings.mineru_language,
        "is_ocr": settings.mineru_is_ocr,
        "enable_table": settings.mineru_enable_table,
        "enable_formula": settings.mineru_enable_formula,
    }


def local_file(document: Document, job: DocumentParseJob, settings: Settings | None = None) -> LocalFile:
    """整份读取原件，Hybrid/超限 PDF 读取受检派生文件；始终保留原 Document 身份。"""

    if "_pdf_split" in job.parse_options:
        from app.services.mineru.pdf_split_jobs import source_for_part
        if settings is None:
            raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 分片缺少存储配置")
        return source_for_part(document, job, settings)

    if "_hybrid" in job.parse_options:
        path, file_hash = bundle_file(document, job.parse_options["_hybrid"])
        return LocalFile(path, "visual_bundle.pdf", job.data_id, file_hash)

    return LocalFile(
        source_path(document), document.file_name, job.data_id, job.file_hash
    )


def read_bundle_bytes(path) -> bytes:
    """缓存只保存已经 Hash 校验的当前 Bundle 字节，路径来自内部检查点。"""

    return path.read_bytes()


def create_job(
    session: Session,
    document: Document,
    settings: Settings,
    *,
    routing_task_id: UUID | None = None,
    route_decision: dict | None = None,
    hybrid_context: dict | None = None,
    force_reparse: bool = False,
) -> DocumentParseJob:
    """新Office冻结LibreOffice转换请求，新PDF采用180页；旧活动Job/缓存不改收费身份。"""

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
        if (hybrid_context is None
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
    try:
        with source_path(document).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
                raise MinerUError("FILE_HASH", "原件 Hash 已变化，禁止复用缓存或提交云任务")
    except OSError:
        raise MinerUError("FILE_MISSING", "原件无法读取，未复用缓存或提交云任务") from None
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
    if document.file_type in {"docx", "pptx"}:
        try:
            inspection = inspect_document(source_path(document), document.file_type, settings)
        except ParserError:
            raise MinerUError("FILE_STRUCTURE", "Office 原件或附件结构损坏，未提交云任务") from None
        if inspection.uncertain:
            raise MinerUError("PREFLIGHT_LIMIT", "Office 预检超出安全资源限制，未提交云任务")
        if "EXTERNAL_ATTACHMENT_MISSING" in inspection.risks:
            raise MinerUError("EXTERNAL_ATTACHMENT_MISSING", "Office 所需外部附件未提供，未提交云任务")
    if document.file_type == "xlsx" and hybrid_context is None:
        options = options | {"_xlsx_primary": prepare_primary(
            document, settings, RouteDecision.model_validate(route_decision) if route_decision else None
        )}
        identity = hashlib.sha256(json.dumps(
            {key: value for key, value in options.items() if key != "_route_decision"},
            sort_keys=True).encode()).hexdigest()
    if hybrid_context is None:
        try:
            guard_inspection = None if route_decision else inspect_document(source_path(document), document.file_type, settings)
        except ParserError:
            raise MinerUError("FILE_STRUCTURE", "原件无法安全预检，未复用缓存或提交云任务") from None
        if guard_inspection is not None and guard_inspection.uncertain:
            raise MinerUError("PREFLIGHT_LIMIT", "原件预检超出资源限制，未提交云任务")
        guard_decision = RouteDecision.model_validate(route_decision) if route_decision else RouteDecision(
            file_type=document.file_type, parse_mode="mineru", route_strategy="mineru_whole",
            reason_codes=("EXPLICIT_MINERU",),
            signals=guard_inspection.signals)
        options = options | {"_cache_guard": guard_decision.model_dump(mode="json")}
    manifest = BundleManifest.model_validate(hybrid_context["manifest"]) if hybrid_context else None
    split_policy, office_request = None, None
    if document.file_type == "pdf" and hybrid_context is None:
        from app.services.mineru.pdf_split import MAX_BYTES, TARGET_PAGES, policy
        page_count = guard_decision.signals.page_count
        if page_count and (page_count > TARGET_PAGES or source_path(document).stat().st_size > MAX_BYTES):
            split_policy = policy()
            options = options | {"_pdf_split_request": {"policy": split_policy, "page_count": page_count}}
            identity = hashlib.sha256(canonical(parse_options(settings) | {"pdf_split": split_policy})).hexdigest()
    if document.file_type in {"doc", "docx", "xls", "xlsx", "ppt", "pptx"} and hybrid_context is None:
        from app.services.mineru.office_pdf import conversion_request
        from app.services.mineru.pdf_split import policy
        office_request, split_policy = conversion_request(document, settings), policy()
        options = options | {"_office_pdf_request": office_request,
                             "_pdf_split_request": {"policy": split_policy, "page_count": None}}
        identity = hashlib.sha256(canonical(parse_options(settings) | {
            "pdf_split": split_policy, "office_pdf": office_request["identity"]})).hexdigest()
    cache_recipe = recipe(document, settings, "hybrid_bundle" if hybrid_context else "mineru_whole",
                          manifest=manifest, pdf_split_policy=split_policy,
                          office_pdf=office_request["identity"] if office_request else None,
                          office_snapshot_version=(OFFICE_SNAPSHOT_VERSION if hybrid_context is None
                              and guard_decision.signals.office_snapshot_rel_count else None))
    cache_key = key_for(cache_recipe)
    visual_keys = sorted({key_for(visual_recipe(entry.content_hash, options, manifest.version))
        for entry in manifest.entries if entry.source_type == "embedded_image"
        and entry.bundle_page is not None and not entry.cache_hit}) if manifest else []
    lock_keys(session, [cache_key, *visual_keys])
    try:
        cached = None if force_reparse else ParseCache(settings).document(document, cache_recipe)
        if cached is not None and hybrid_context is None:
            if office_request:
                from app.services.mineru.office_pdf import bound_decision, validate_receipt
                try:
                    receipt = json.loads(ParseCache(settings).load(cache_recipe)[1]["office_pdf/receipt.json"])
                except (KeyError, TypeError, ValueError):
                    raise CacheError() from None
                validate_receipt(receipt, office_request["identity"])
                guard_decision = bound_decision(guard_decision, receipt)
                options = options | {"_office_pdf": receipt, "_cache_guard": guard_decision.model_dump(mode="json")}
                if "_route_decision" in options:
                    options = options | {"_route_decision": guard_decision.model_dump(mode="json")}
            guard_cached(cached, guard_decision)
    except CacheError as exc:
        raise internal_failure(exc, exc.code, str(exc)) from None
    if cached is None:
        from app.services.mineru.office_recovery import checked_authorization
        permission = checked_authorization(session, document, settings) if office_request else None
        if permission:
            options = options | {"_office_pdf_recovery": permission}
            claim_miss(session, document, cache_recipe, visual_keys, force_reparse=False,
                       authorized_office_source=UUID(permission["source_job_id"]))
        else:
            claim_miss(session, document, cache_recipe, visual_keys, force_reparse=force_reparse)
    options = options | {"_cache_key": cache_key, "_cache_recipe": cache_recipe,
                         "_cache_visual_keys": visual_keys, "_force_reparse": force_reparse}
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
    if cached is None and split_policy is None:
        validate_local_file(local_file(document, job, settings))
    if document.status == "FAILED":
        transition_document(document, "UPLOADED")
    transition_document(document, "PARSING")
    # 云任务使用原有独立解析代次，管理编排必须同事务换绑，否则扫描PDF会被误判为旧检查点。
    from app.services.document_management import rebind_cloud_management
    rebind_cloud_management(document, job.task_id, job.job_id)
    document.processing_task_id = job.task_id
    session.add(job)
    session.flush()
    if cached is not None:
        try:
            check_source_hash(document, source_path(document))
        except ParserError:
            raise MinerUError("FILE_HASH", "缓存回放期间原件 Hash 已变化，未写入结果") from None
        finish_job(session, document, job, cached)
    return job


def finish_job(session, document, job, parsed) -> None:
    """在调用方事务中替换完整块并完成 Job；缓存命中不伪造 batch 或上传成功。"""

    persist_blocks(session, document, parsed)
    transition_document(document, "CHUNKING")
    job.state, job.is_active = "done", False
    job.finished_at = datetime.now(UTC)
    job.error_code = job.error_message = None
    job.parse_options = {k: v for k, v in job.parse_options.items()
                         if k not in {"_adapter_failure", "_runtime_failure"}}


def restore_cached_result(session, job_id: UUID, settings: Settings) -> tuple[str, int] | None:
    """恢复同一云代次已验证的本地结果，提交失败重试不需要 Token、GET 或 ZIP 下载。"""

    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    if "_cache_recipe" not in job.parse_options:
        return None
    if "_adapter_recipe" in job.parse_options:
        # 升级后的本地转换不误读旧Job的v3成功指针；原ZIP始终独立保全，可重复本地转换。
        return None
    try:
        restored = ParseCache(settings).job_document(document, job.parse_options["_cache_recipe"], job_id)
        if restored is None:
            return None
        from app.services.document_processing import check_source
        check_source(document)
        with source_path(document).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
                raise CacheError()
        parsed, files = restored
        if "_hybrid" in job.parse_options:
            completed = json.loads(files["hybrid/bundle_manifest.json"])
            job.parse_options = job.parse_options | {"_hybrid_result": completed}
        if "_pdf_split_request" in job.parse_options:
            from app.services.mineru.pdf_split import checked_manifest
            completed = json.loads(files["pdf_split/manifest.json"])
            job.parse_options = job.parse_options | {"_pdf_split": completed}
            checked_manifest(document, job)
        finish_job(session, document, job, parsed)
        session.commit()
        log_event("cache.persisted", doc_id=document.doc_id, job_id=job_id,
                  block_count=len(parsed.blocks), parse_cache_hit=True, status="CHUNKING")
        return "done", len(parsed.blocks)
    except CacheError as exc:
        raise internal_failure(exc, exc.code, str(exc)) from None


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
    """限制本轮本地观察窗口；显式Retry可续看同一批次，不篡改真实创建/提交时间。"""

    started = job.created_at
    renewed = job.parse_options.get("_local_wait_started_at")
    if renewed is not None:
        try:
            started = datetime.fromisoformat(renewed)
            if started.tzinfo is None or not job.created_at <= started <= datetime.now(UTC):
                raise ValueError
        except (TypeError, ValueError):
            raise MinerUError("JOB_CHECKPOINT", "MinerU 本地等待检查点无效，未提交新批次") from None
    if (datetime.now(UTC) - started).total_seconds() > settings.mineru_job_timeout_seconds:
        raise MinerUError("JOB_TIMEOUT", "MinerU 本地等待超时，不代表云任务失败；请核对原批次后Retry")


def renew_local_wait(job: DocumentParseJob, settings: Settings) -> None:
    """显式Retry延长原Batch或未提交分片的观察窗口，不重置收费身份/重试预算。"""

    waiting_split = "_pdf_split" in job.parse_options and (
        not job.batch_id and not job.submission_attempted
        or job.batch_id and job.upload_url and job.upload_expires_at and job.upload_expires_at > datetime.now(UTC))
    if not (job.batch_id and job.upload_complete or waiting_split):
        return
    check_normalizer_recipe(job)
    try:
        check_deadline(job, settings)
    except MinerUError as exc:
        if exc.code != "JOB_TIMEOUT":
            raise
        job.parse_options = job.parse_options | {"_local_wait_started_at": datetime.now(UTC).isoformat()}


def check_normalizer_recipe(job: DocumentParseJob) -> None:
    """校验冻结云身份；v4质量策略修订另建本地配方，不覆写旧缓存或更换收费输入。"""

    frozen = job.parse_options.get("_adapter_recipe") or job.parse_options.get("_cache_recipe")
    if frozen is not None and (
        not isinstance(frozen, dict) or frozen.get("normalizer_version") != NORMALIZER_VERSION
    ):
        raise MinerUError("CACHE_CONFIG_CHANGED", "MinerU 归一化版本已变化，旧任务未重新解析或提交云批次")
    if isinstance(frozen, dict):
        from app.services.parse_cache import CONTENT_POLICY_VERSION
        policy_version = frozen.get("content_policy_version")
        if policy_version not in {None, CONTENT_POLICY_VERSION}:
            raise MinerUError("CACHE_CONFIG_CHANGED", "本地内容策略版本不兼容，未复用错误质量缓存")
        if policy_version is None:
            job.parse_options = job.parse_options | {"_adapter_recipe": frozen | {
                "content_policy_version": CONTENT_POLICY_VERSION}}
    if "_pdf_split_request" in job.parse_options:
        from app.services.mineru.pdf_split import valid_policy
        if not valid_policy(job.parse_options["_pdf_split_request"].get("policy")):
            raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 分片生成器版本已变化，未重新提交")


def upgrade_local_adapter(job):
    """原ZIP已校验后的显式Retry只升级本地转换身份，冻结云参数/Batch/旧配方保持不变。"""
    frozen = job.parse_options.get("_cache_recipe")
    if not isinstance(frozen, dict) or frozen.get("normalizer_version") not in {"mineru-adapter-v3", NORMALIZER_VERSION}:
        raise MinerUError("CACHE_CONFIG_CHANGED", "原Job本地转换版本不受支持，未访问云端")
    from app.services.parse_cache import CONTENT_POLICY_VERSION
    job.parse_options = job.parse_options | {"_adapter_recipe": frozen | {
        "normalizer_version": NORMALIZER_VERSION, "content_policy_version": CONTENT_POLICY_VERSION}}
    job.parse_options = job.parse_options | {"_local_replay_only": True}


def prepare_split_step(session, document, job, settings):
    """物化分片后重新获得行锁，片缓存命中每个短任务最多推进一片。"""
    from app.services.mineru.pdf_split_jobs import ensure_plan, complete_cached_piece

    ensure_plan(session, document, job, settings)
    locked = lock_job(session, job.job_id)
    if not locked:
        return None, ("skipped", 0)
    document, job = locked
    return locked, complete_cached_piece(session, document, job, settings)


def submit_step(
    session: Session, job_id: UUID, provider, settings: Settings
) -> tuple[str, int]:
    """先核对版本与本地重放许可；离线恢复即使收到旧submit消息也不能提交云端。"""

    restored = restore_cached_result(session, job_id, settings)
    if restored is not None:
        return restored
    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    if job.parse_options.get("_local_replay_only") is True:
        return check_step(session, job_id, None, settings)
    check_normalizer_recipe(job)
    if "_pdf_split_request" in job.parse_options:
        locked, completed = prepare_split_step(session, document, job, settings)
        if completed is not None:
            return completed
        document, job = locked
    if job.upload_complete:
        return "check", 0
    check_deadline(job, settings)
    if job.batch_id is None:
        if job.submission_attempted:
            age = (datetime.now(UTC) - job.submission_started_at).total_seconds()
            wait = settings.mineru_request_timeout_seconds + 30 - age
            if wait > 0:
                return "submit", max(1, int(wait))
            raise MinerUError(
                "SUBMIT_UNCERTAIN", "MinerU 提交结果不确定，禁止自动重复申请批次"
            )
        source = local_file(document, job, settings)
        validate_local_file(source)
        if "_pdf_split" in job.parse_options:
            from app.services.mineru.pdf_split_jobs import claim_piece
            claim_piece(session, document, job)
        job.submission_attempted = True
        job.submission_started_at = datetime.now(UTC)
        from app.services.mineru.pdf_split_jobs import sync_current
        sync_current(document, job, settings)
        # 必须在非幂等 POST 前提交意图；重启时不能无条件再创建远端批次。
        session.commit()
        try:
            batch = provider.request_upload_urls([source], job.parse_options)
        except MinerUError as error:
            if error.code.startswith("HTTP_5"):
                # 网关/服务器 5xx 不能证明非幂等申请未被接受，禁止重发造成未知费用。
                raise MinerUError("SUBMIT_UNCERTAIN", "MinerU 申请返回服务端异常，提交结果不确定，禁止自动重发") from None
            raise
        locked = lock_job(session, job_id)
        if not locked:
            return "skipped", 0
        document, job = locked
        job.batch_id, job.trace_id = batch.batch_id, batch.trace_id
        job.upload_url = batch.urls[0]
        job.upload_expires_at = datetime.now(UTC) + timedelta(hours=24)
        job.submitted_at = datetime.now(UTC)
        sync_current(document, job, settings)
        batch_log = {
            "doc_id": document.doc_id,
            "job_id": job_id,
            "batch_id": job.batch_id,
        }
        if "_pdf_split" in job.parse_options:
            from app.services.mineru.pdf_split import checked_manifest
            from app.services.mineru.pdf_split_jobs import current_part
            manifest = checked_manifest(document, job)
            batch_log.update(file_index=current_part(manifest).ordinal, file_count=len(manifest.parts))
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
    provider.upload(job.upload_url, local_file(document, job, settings))
    job.upload_complete = True
    job.upload_url = None
    job.error_code = job.error_message = document.error_message = None
    from app.services.mineru.pdf_split_jobs import sync_current
    sync_current(document, job, settings)
    upload_log = {"doc_id": document.doc_id, "job_id": job_id, "batch_id": job.batch_id}
    session.commit()
    log_event("mineru.upload_complete", **upload_log, upload_complete=True)
    return "check", settings.mineru_poll_interval_seconds


def result_failure(payload: dict) -> MinerUError:
    """优先保留已知错误码，再匹配已确认固定原因；消息分类不授予新提交或自动分片。"""

    for key in ("err_code", "error_code"):
        value = payload.get(key)
        if type(value) in (str, int):
            error = api_error(value)
            if error.code != "UPSTREAM_ERROR":
                return error

    transient = {
        "服务异常": "-10001",
        "模型服务暂时不可用": "-60007",
        "任务提交队列已满": "-60009",
        "解析失败": "-60010",
        "文件拆分失败": "-60020",
        "读取文件页数失败": "-60021",
        # 用户私有原Batch诊断已证实此完整短语；只是本地分类，不冒称上游提供了数字码。
        "number of pages exceeds limit (200 pages), please split the file and try again": "-60006",
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
    """原ZIP优先；离线重放缺结果立即停止，只有正常云任务才能查询或下载。"""

    restored = restore_cached_result(session, job_id, settings)
    if restored is not None:
        return restored
    locked = lock_job(session, job_id)
    if not locked:
        return "skipped", 0
    document, job = locked
    check_normalizer_recipe(job)
    if "_pdf_split_request" in job.parse_options:
        locked, completed = prepare_split_step(session, document, job, settings)
        if completed is not None:
            return completed
        document, job = locked
    if not job.upload_complete or not job.batch_id:
        if job.parse_options.get("_local_replay_only") is True:
            raise MinerUError("LOCAL_RESULT_REQUIRED", "本地重放缺少已完成分片的原始结果，未访问云端或提交后续分片")
        check_deadline(job, settings)
        return "submit", 0
    # 已下载并保全的付费结果优先本地读取；不依赖可能过期的供应商下载链接。
    # 这里只定位原Batch，真正的原件/分片Hash仍由下方local_file验证。
    ordinal, raw_hash = 1, job.file_hash
    if "_pdf_split" in job.parse_options:
        from app.services.mineru.pdf_split import checked_manifest
        from app.services.mineru.pdf_split_jobs import current_part
        raw_part = current_part(checked_manifest(document, job))
        if raw_part is None:
            raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 当前分片缺失，未请求云端")
        ordinal, raw_hash = raw_part.ordinal, raw_part.file_hash
    elif "_hybrid" in job.parse_options:
        _, raw_hash = bundle_file(document, job.parse_options["_hybrid"])
    saved_raw = load_raw_result(settings, doc_id=document.doc_id, job_id=job_id,
        ordinal=ordinal, batch_id=job.batch_id, data_id=job.data_id, file_hash=raw_hash)
    if saved_raw is None and job.parse_options.get("_local_replay_only") is True:
        raise MinerUError("LOCAL_RESULT_REQUIRED", "本地原始结果缺失，未查询、下载或重新提交MinerU任务")
    result = ({"state": "done", "trace_id": getattr(job, "trace_id", None)} if saved_raw is not None else
              provider.query(job.batch_id, job.data_id))
    job.state, job.trace_id = result["state"], result["trace_id"]
    log_event(
        "mineru.local_replay_started" if saved_raw is not None else "mineru.remote_state",
        doc_id=document.doc_id,
        job_id=job_id,
        batch_id=job.batch_id,
        state=job.state,
    )
    if job.state == "failed":
        error = result_failure(result)
        # 路由终态码继续阻止重投，但安全根因独立保留，不能被管理汇总代码吞掉。
        failure = MinerUError("ROUTED_REMOTE_FAILED" if "_route_decision" in job.parse_options
                             else error.code,
                             f"{error.code}：{error}；远端任务已终止，未追加新批次")
        failure.remote_failure = {"cause_code": error.code, "batch_id": job.batch_id,
                                  "data_id": job.data_id, "trace_id": job.trace_id}
        raise failure
    if job.state != "done":
        # Worker停机时间不是供应商失败：已提交批次先做一次GET，done仍可回收；未完成才停止调度。
        check_deadline(job, settings)
        job.error_code = job.error_message = document.error_message = None
        from app.services.mineru.pdf_split_jobs import sync_current
        sync_current(document, job, settings)
        session.commit()
        return "check", settings.mineru_poll_interval_seconds
    source = local_file(document, job, settings)
    validate_local_file(source)
    archive, raw_result = saved_raw if saved_raw is not None else obtain_raw_result(provider, result.get("full_zip_url"), settings,
        doc_id=document.doc_id, job_id=job_id, ordinal=ordinal, batch_id=job.batch_id,
        data_id=job.data_id, file_hash=source.file_hash)
    return adapt_raw_result(session, document, job, settings, archive, raw_result, source)


def adapt_raw_result(session, document, job, settings, archive, raw_result, source):
    """只把已归档原ZIP转换、校验并发布本地结果；不接收Provider，不执行任何云HTTP。

    原始归档与Normalizer版本独立；转换失败仍由原失败事务记录，原ZIP不删除。
    分片成功后复用既有游标/全片事务；离线标记贯穿后续任务，缺片不转云。
    """
    job_id, ordinal = job.job_id, 1
    if "_pdf_split" in job.parse_options:
        # SQL 提交失败时沿用同片 Batch；片结果的 ID 不依赖临时文件名或 Celery 消息。
        from app.services.mineru.pdf_split import checked_manifest, piece_recipe
        from app.services.mineru.pdf_split_jobs import current_part, save_manifest
        manifest = checked_manifest(document, job)
        part = current_part(manifest)
        ordinal = part.ordinal
        adapter_identity = key_for(piece_recipe(job, part))
    else:
        adapter_identity = job.file_hash + ":" + job.options_hash
    if "_pdf_split" in job.parse_options:
        part.raw_result = raw_result
        save_manifest(job, manifest, settings)
    else:
        job.parse_options = job.parse_options | {"_raw_result": raw_result}
    # 不新增事务边界或松开文档锁；SQL回滚不删除独立ZIP/JSON回执，不发布成功缓存。
    try:
        parsed = MinerUResultAdapter().normalize(
            archive,
            doc_id=document.doc_id,
            file_name=document.file_name,
            file_type="pdf" if "_hybrid" in job.parse_options or "_pdf_split" in job.parse_options else document.file_type,
            geometry=page_geometry(source.path, "pdf" if "_hybrid" in job.parse_options or "_pdf_split" in job.parse_options else document.file_type),
            identity=adapter_identity,
            allow_empty_part="_pdf_split" in job.parse_options or "_hybrid" in job.parse_options,
        )
    except MinerUError as error:
        log_event("mineru.adapter_failed", doc_id=document.doc_id, job_id=job_id,
                  batch_id=job.batch_id, file_index=ordinal, error_code=error.code)
        raise
    log_event(
        "mineru.adapter_validated",
        doc_id=document.doc_id,
        job_id=job_id,
        block_count=len(parsed.blocks),
    )
    if "_pdf_split" in job.parse_options:
        from app.services.mineru.pdf_split_jobs import complete_piece
        return complete_piece(session, document, job, settings, parsed, archive)
    hybrid_metrics = {}
    original_visual = parsed
    if "_hybrid" in job.parse_options:
        parsed, completed_manifest = complete_hybrid(
            document, job.parse_options["_hybrid"], parsed,
            RouteDecision.model_validate(job.parse_options["_route_decision"]),
        )
        job.parse_options = job.parse_options | {"_hybrid_result": completed_manifest}
        hybrid_metrics = metrics(BundleManifest.model_validate(completed_manifest))
    elif "_route_decision" in job.parse_options or "_cache_guard" in job.parse_options:
        try:
            guard_result(
                parsed,
                RouteDecision.model_validate(job.parse_options.get("_route_decision") or job.parse_options["_cache_guard"]),
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
            "router.guard_partial" if getattr(parsed, "quality", None) and parsed.quality.content == "PARTIAL" else "router.guard_passed",
            level=logging.WARNING if getattr(parsed, "quality", None) and parsed.quality.content == "PARTIAL" else logging.INFO,
            doc_id=document.doc_id,
            job_id=job_id,
            route_strategy="mineru_whole",
        )
    if "_xlsx_primary" in job.parse_options:
        parsed = complete_primary(
            document, job.parse_options["_xlsx_primary"], parsed,
            RouteDecision.model_validate(job.parse_options["_route_decision"])
            if "_route_decision" in job.parse_options else None,
        )
    save_cloud_cache(document, job, settings, parsed, original_visual, archive, source)
    finish_job(session, document, job, parsed)
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
    if "_xlsx_primary" in job.parse_options:
        log_event("xlsx.primary_persisted", doc_id=completed_doc_id, job_id=job_id,
                  block_count=len(parsed.blocks), status="CHUNKING")
    return "done", len(parsed.blocks)


def save_cloud_cache(document, job, settings, parsed, visual, archive, source) -> None:
    """全量校验后保存原始结果和视觉缓存；旧代次不猜新身份，不回填未知覆盖结果。"""

    if "_cache_recipe" not in job.parse_options:
        return
    try:
        try:
            check_source_hash(document, source_path(document))
        except ParserError:
            raise MinerUError("FILE_HASH", "解析完成时原件 Hash 已变化，未发布缓存") from None
        cache = ParseCache(settings)
        artifacts = archive_artifacts(archive)
        if "_hybrid" in job.parse_options and not (visual.quality and visual.quality.content == "PARTIAL"):
            manifest = BundleManifest.model_validate(job.parse_options["_hybrid_result"])
            normalized = {}
            for entry in manifest.entries:
                if entry.source_type != "embedded_image" or entry.state == "filtered":
                    continue
                value = visual_recipe(entry.content_hash, job.parse_options, manifest.version)
                if entry.bundle_page is not None and entry.reused_from is None:
                    blocks = [block for block in visual.blocks if block.page == entry.bundle_page]
                    cache.save_visual(value, blocks)
                blocks = cache.visual(value)
                if blocks is None:
                    raise CacheError("CACHE_MISS")
                if entry.cache_hit and hashlib.sha256(json.dumps(
                    [block.model_dump(mode="json") for block in blocks], sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest() != entry.cache_result_hash:
                    raise CacheError()
                normalized[entry.content_hash] = [block.model_dump(mode="json") for block in blocks]
            artifacts.update({"hybrid/bundle_manifest.json": canonical(manifest.model_dump(mode="json")),
                "hybrid/visual_bundle.pdf": read_bundle_bytes(source.path),
                "hybrid/normalized_visual_blocks.json": canonical(normalized)})
        cache.save_document(document, job.parse_options.get("_adapter_recipe") or job.parse_options["_cache_recipe"], parsed,
                            artifacts, job_id=job.job_id)
    except CacheError as exc:
        raise internal_failure(exc, exc.code, str(exc)) from None


def record_error(
    session: Session, job_id: UUID, error: MinerUError, settings: Settings
) -> bool:
    """独立事务保存固定错误及无正文调用栈；保留原ZIP和成功片，不覆盖旧代次。"""

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
        if "_pdf_split" not in job.parse_options:
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
    remote = getattr(error, "remote_failure", None)
    if remote:
        # 主事务已回滚，在独立失败事务保存白名单类别和原收费身份，不保存err_msg。
        job.parse_options = job.parse_options | {"_remote_failure": remote}
    internal = getattr(error, "internal_diagnostic", None)
    if internal:
        job.parse_options = job.parse_options | {"_runtime_failure": internal}
    diagnostic = safe_merge_diagnostic(getattr(error, "diagnostic", None))
    if diagnostic:
        # 外层业务事务已回滚；在原有独立失败事务里保存子原因，不把异常正文写入JSONB。
        job.parse_options = job.parse_options | {"_adapter_failure": {
            "error_code": error.code, "diagnostic": diagnostic,
            "job_id": str(job.job_id), "batch_id": job.batch_id, "data_id": job.data_id}}
    else:
        job.parse_options = {k: v for k, v in job.parse_options.items() if k != "_adapter_failure"}
    from app.services.mineru.pdf_split_jobs import sync_current
    try:
        sync_current(document, job, settings, error_code=error.code)
    except MinerUError:
        # 坏账本不能再次阻止失败落库；保留原JSON供人工核对，不猜测/修补收费身份。
        retry = False
        job.state, job.is_active, job.finished_at = "failed", False, datetime.now(UTC)
        job.error_code = "PDF_SPLIT_CHECKPOINT"
        job.error_message = "PDF 分片检查点无效，请核对原批次，未重新提交"
        transition_document(document, "FAILED", job.error_message)
        log_event("mineru.split_checkpoint_invalid", doc_id=document.doc_id, job_id=job_id,
                  error_code=job.error_code)
    if retry:
        document.error_message = str(error)
    session.commit()
    return retry
