"""在一个原文档 Parse Job 内串行推进 PDF 分片，不建立子业务记录或第二套 HTTP。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.business_logging import log_event
from app.models import DocumentParseJob
from app.parsers.common import ParserError
from app.parsers.routing.contracts import RouteDecision
from app.parsers.routing.guard import CompletenessError, guard_result
from app.services.cache_claims import lock_keys
from app.services.document_processing import check_source_hash, source_path
from app.services.mineru.errors import MinerUError
from app.services.mineru.pdf_split import (checked_manifest, prepare_manifest, part_path,
    file_digest, piece_recipe, load_piece, merge_parts, result_prefix)
from app.services.mineru.provider import LocalFile, validate_local_file
from app.services.parse_cache import CacheError, ParseCache, atomic_write, canonical, archive_artifacts, key_for


def current_part(manifest):
    """只处理尚未完成的第一片，已成功片不会重新提交。"""
    return next((part for part in manifest.parts if part.state != "done"), None)


def save_manifest(job, manifest, settings) -> None:
    """显式重赋 JSONB 并保存无 URL 的磁盘副本；收费恢复始终以数据库为准。"""
    value = manifest.model_dump(mode="json")
    job.parse_options = job.parse_options | {"_pdf_split": value}
    cache = ParseCache(settings)
    try:
        target = part_path(cache, manifest.doc_id, job.job_id, 1).parent / "manifest.json"
        atomic_write(target, canonical(value))
    except (CacheError, OSError, MinerUError):
        # 失败事务也必须能记录原因；副本不可用不允许据磁盘猜测收费状态。
        log_event("mineru.split_manifest_failed", doc_id=manifest.doc_id, job_id=job.job_id,
                  error_code="PDF_SPLIT_IO")


def activate_part(job, part) -> None:
    """切换当前片的既有 Job 列；原 Document processing_task_id 与父 Job/task_id 不变。"""
    job.data_id = part.data_id
    job.batch_id = job.trace_id = job.upload_url = None
    job.upload_expires_at = job.submission_started_at = job.submitted_at = None
    job.submission_attempted = job.upload_complete = False
    job.state = "waiting-file"
    job.error_code = job.error_message = None
    # 每片独立的有界观察/重试窗口，不因前片耗时让新片立即 JOB_TIMEOUT。
    job.retry_count = 0
    job.parse_options = job.parse_options | {"_local_wait_started_at": datetime.now(UTC).isoformat()}


def sync_current(document, job, settings, *, error_code=None) -> None:
    """把当前片的提交意图/Batch/状态写回账本；旧成功片及费用身份不清空。"""
    if "_pdf_split" not in job.parse_options:
        return
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if part is None:
        save_manifest(job, manifest, settings)
        return
    if part.data_id != job.data_id:
        raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 当前分片身份不一致")
    part.batch_id = job.batch_id
    part.state = "running" if job.state == "done" else job.state
    part.submission_attempted, part.upload_complete = job.submission_attempted, job.upload_complete
    part.submitted_at = job.submitted_at.isoformat() if job.submitted_at else None
    part.submission_started_at = job.submission_started_at.isoformat() if job.submission_started_at else None
    part.error_code = error_code
    save_manifest(job, manifest, settings)


def ensure_plan(session, document, job, settings) -> None:
    """整份缓存 miss 后才物化计划；任何分片超限/损坏均在首次 POST 之前失败。"""
    if "_pdf_split" not in job.parse_options:
        manifest = prepare_manifest(document, job, settings, source_path(document))
        activate_part(job, manifest.parts[0])
        save_manifest(job, manifest, settings)
        session.commit()
        log_event("mineru.split_planned", doc_id=document.doc_id, job_id=job.job_id,
                  file_count=len(manifest.parts), page_count=manifest.page_count)
    else:
        manifest = checked_manifest(document, job)
        part = current_part(manifest)
        if part is not None and (part.data_id != job.data_id or part.batch_id != job.batch_id
                or part.submission_attempted != job.submission_attempted
                or part.upload_complete != job.upload_complete):
            raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 当前任务与分片检查点不一致")


def source_for_part(document, job, settings) -> LocalFile:
    """每次上传/回收前校验当前分片文件大小与 Hash；缺片不悄悄创建新收费输入。"""
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if part is None or part.data_id != job.data_id:
        raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 当前分片不存在或身份不一致")
    cache = ParseCache(settings)
    path = part_path(cache, document.doc_id, job.job_id, part.ordinal)
    try:
        check_source_hash(document, source_path(document))
        if path.stat().st_size != part.size_bytes or file_digest(path) != part.file_hash:
            raise MinerUError("PDF_SPLIT_CHANGED", "PDF 分片 Hash 或实际大小不一致")
    except OSError:
        raise MinerUError("PDF_SPLIT_MISSING", "PDF 分片文件缺失，请恢复原检查点文件，未重新收费") from None
    except ParserError:
        raise MinerUError("FILE_HASH", "PDF 原件 Hash 已变化，未继续提交分片") from None
    return LocalFile(path, path.name, part.data_id, part.file_hash)


def claim_piece(session, document, job) -> None:
    """按片Hash及缓存身份保护跨文档/Normalizer历史，升级不能绕过旧分片收费记录。"""
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    key = key_for(piece_recipe(job, part))
    lock_keys(session, [key, part.file_hash])
    owners = session.scalars(select(DocumentParseJob).where(
        DocumentParseJob.job_id != job.job_id,
        DocumentParseJob.provider == "mineru_cloud",
        or_(DocumentParseJob.parse_options.contains({"_pdf_split_claims": [key]}),
            DocumentParseJob.parse_options["_pdf_split"]["parts"].contains([{"file_hash": part.file_hash}])))).all()
    for owner in owners:
        # 仅检查相同片的历史，而不是把另一片的提交当成此片已收费。
        rows = owner.parse_options.get("_pdf_split", {}).get("parts", [])
        charged = any(row.get("file_hash") == part.file_hash
                      and (row.get("batch_id") or row.get("submission_attempted")) for row in rows)
        if owner.is_active or charged and not job.parse_options.get("_force_reparse", False):
            raise MinerUError("CACHE_IN_PROGRESS" if owner.is_active else "CACHE_REPARSE_REQUIRED",
                              "相同 PDF 分片已有活动或收费历史，未重复提交", retryable=owner.is_active)
    claims = sorted(set(job.parse_options.get("_pdf_split_claims", [])) | {key})
    job.parse_options = job.parse_options | {"_pdf_split_claims": claims}


def complete_cached_piece(session, document, job, settings):
    """片缓存/同代次成功指针优先；失败不改变提交身份，Force 也能恢复自身结果。"""
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if part is None:
        return finalize_split(session, document, job, settings, manifest)
    # 有 Batch 的结果先写缓存再提交 SQL，回滚时以本片独立指针恢复，不重复 GET/POST。
    prefix = result_prefix(job)
    pointer = part_path(ParseCache(settings), document.doc_id, job.job_id, part.ordinal).parent / f"{prefix}-{part.ordinal:04d}.json"
    if pointer.exists():
        import json
        try:
            part.result = json.loads(ParseCache(settings).read_bytes(pointer, 65536))
        except (TypeError, ValueError):
            raise MinerUError("PDF_SPLIT_RESULT", "PDF 分片恢复指针损坏，未重新收费") from None
    elif job.submission_attempted or job.batch_id:
        return None
    cache = ParseCache(settings)
    parsed = load_piece(cache, job, part)
    if parsed is None:
        return None
    validate_piece(document, job, part, parsed)
    part.state = "done"
    return advance_piece(session, document, job, settings, manifest, cache_hit=True)


def validate_piece(document, job, part, parsed) -> None:
    """平移真实原页要求；无可靠文字片仅保存PARTIAL，整份仍须有可索引的可靠块。"""
    decision = RouteDecision.model_validate(job.parse_options.get("_route_decision") or job.parse_options["_cache_guard"])
    pages = tuple(page.model_copy(update={"page": page.page - part.page_start + 1})
                  for page in decision.signals.pages if part.page_start <= page.page <= part.page_end)
    local = decision.model_copy(update={"signals": decision.signals.model_copy(update={
        "page_count": part.page_end - part.page_start + 1, "pages": pages})})
    if (not parsed.blocks and parsed.file_type == "pdf" and parsed.quality is not None
            and parsed.quality.content == "PARTIAL"
            and (parsed.quarantined_content or parsed.quality.visual_only_count)):
        parsed.quality.required_pages = sorted(page.page for page in pages if page.required)
        parsed.quality.missing_pages = list(parsed.quality.required_pages)
        if "MISSING_REQUIRED_PAGES" not in parsed.quality.warnings and len(parsed.quality.warnings) < 128:
            parsed.quality.warnings.append("MISSING_REQUIRED_PAGES")
        return
    try:
        guard_result(parsed, local)
    except CompletenessError:
        raise MinerUError("COMPLETENESS", "PDF 分片非空页缺失或来源不符，未保存部分 Block") from None


def complete_piece(session, document, job, settings, parsed, archive):
    """本片完整结果先进入内容缓存再提交检查点；SQL 回滚不会导致新 Batch。"""
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    validate_piece(document, job, part, parsed)
    cache = ParseCache(settings)
    record = cache.save(piece_recipe(job, part), archive_artifacts(archive) | {
        "normalized_document.json": parsed.model_dump_json().encode()})
    prefix = result_prefix(job)
    pointer = part_path(cache, document.doc_id, job.job_id, part.ordinal).parent / f"{prefix}-{part.ordinal:04d}.json"
    atomic_write(pointer, record.model_dump_json().encode())
    part.result = record.model_dump(mode="json")
    part.batch_id, part.submission_attempted, part.upload_complete = job.batch_id, job.submission_attempted, job.upload_complete
    part.state, part.error_code = "done", None
    return advance_piece(session, document, job, settings, manifest, cache_hit=False)


def advance_piece(session, document, job, settings, manifest, *, cache_hit):
    """推进原游标；默认只读本地，仅显式授权清单内从未提交的片可以转为收费提交。"""
    completed = sum(p.state == "done" for p in manifest.parts)
    part = current_part(manifest)
    if part is None:
        return finalize_split(session, document, job, settings, manifest)
    # 后续片可能早已有Batch/原ZIP，不能清空它的收费身份后再尝试提交。
    restricted = job.parse_options.get("_local_replay_only") is True or "_authorized_new_split_parts" in job.parse_options
    allowed = job.parse_options.get("_authorized_new_split_parts", [])
    new_submission = (part.ordinal in allowed and not part.submission_attempted
                      and not part.batch_id and not part.upload_complete and part.result is None)
    if restricted and not new_submission:
        job.parse_options = job.parse_options | {"_local_replay_only": True}
        job.data_id, job.batch_id = part.data_id, part.batch_id
        job.submission_attempted, job.upload_complete = part.submission_attempted, part.upload_complete
        job.submitted_at = datetime.fromisoformat(part.submitted_at) if part.submitted_at else None
        job.submission_started_at = datetime.fromisoformat(part.submission_started_at) if part.submission_started_at else None
        job.state, job.upload_url, job.upload_expires_at = "pending", None, None
        job.error_code = job.error_message = None
    else:
        if restricted:
            job.parse_options = job.parse_options | {"_local_replay_only": False,
                "_authorized_new_split_parts": [ordinal for ordinal in allowed if ordinal != part.ordinal]}
            log_event("mineru.split_continuation_authorized", doc_id=document.doc_id, job_id=job.job_id,
                      file_index=part.ordinal, file_count=len(allowed), stage="submit")
        activate_part(job, part)
    save_manifest(job, manifest, settings)
    config = getattr(document, "management_config", None)
    if config and config.get("state") == "running" and config.get("stage") == "cloud" and config.get("stage_task_id") == str(job.job_id):
        # 多片按片观察，避免上一片耗时耗尽后续 Chunk 的管理窗口；不复活已失败编排。
        document.management_config = config | {"expires_at": (
            datetime.now(UTC) + timedelta(seconds=settings.mineru_job_timeout_seconds)).isoformat()}
    session.commit()
    log_event("mineru.split_piece_done", doc_id=document.doc_id, job_id=job.job_id,
              accepted_count=completed, file_count=len(manifest.parts), parse_cache_hit=cache_hit)
    return ("check" if job.parse_options.get("_local_replay_only") is True else "submit"), 0


def authorize_split_continuation(document, job, settings):
    """管理员显式确认剩余新片费用；只授权冻结清单中的未提交片，绝不重投有收费历史的片。"""
    manifest = checked_manifest(document, job)
    current = current_part(manifest)
    if current is None or (current.data_id != job.data_id or current.batch_id != job.batch_id
            or current.submission_attempted != job.submission_attempted
            or current.upload_complete != job.upload_complete):
        raise MinerUError("PDF_SPLIT_CHECKPOINT", "当前分片与原Job不一致，未授权新增提交")
    allowed = [part.ordinal for part in manifest.parts if part.state != "done" and part.result is None
               and not part.batch_id and not part.submission_attempted and not part.upload_complete]
    if not allowed:
        raise MinerUError("PDF_SPLIT_CHECKPOINT", "没有可授权的未提交分片，未重新解析已有Batch")
    source_for_part(document, job, settings)
    job.parse_options = job.parse_options | {"_authorized_new_split_parts": allowed}
    return current.ordinal in allowed


def finalize_split(session, document, job, settings, manifest):
    """核对原件、全片输入/结果与完整 Guard 后一次落库；仍由原 Index 流程决定 READY。"""
    from app.services.mineru.jobs import finish_job

    cache = ParseCache(settings)
    try:
        check_source_hash(document, source_path(document))
    except (OSError, ParserError):
        raise MinerUError("FILE_HASH", "PDF 原件无法校验或 Hash 已变化，未发布完整结果") from None
    results = []
    for part in manifest.parts:
        path = part_path(cache, document.doc_id, job.job_id, part.ordinal)
        if path.stat().st_size != part.size_bytes:
            raise MinerUError("PDF_SPLIT_CHANGED", "PDF 分片实际大小与冻结清单不符")
        validate_local_file(LocalFile(path, path.name, part.data_id, part.file_hash))
        parsed = load_piece(cache, job, part)
        if parsed is None:
            raise MinerUError("PDF_SPLIT_RESULT", "PDF 分片缺少完整结果，未写入部分 Block")
        validate_piece(document, job, part, parsed)
        results.append(parsed)
    merged = merge_parts(document, manifest, results)
    try:
        guard_result(merged, RouteDecision.model_validate(
            job.parse_options.get("_route_decision") or job.parse_options["_cache_guard"]))
    except CompletenessError:
        raise MinerUError("COMPLETENESS", "PDF 完整原页覆盖不符，未写入部分 Block") from None
    save_manifest(job, manifest, settings)
    cache.save_document(document, job.parse_options.get("_adapter_recipe") or job.parse_options["_cache_recipe"], merged,
                        {"pdf_split/manifest.json": canonical(manifest.model_dump(mode="json"))}, job_id=job.job_id)
    finish_job(session, document, job, merged)
    session.commit()
    log_event("mineru.blocks_persisted", doc_id=document.doc_id, job_id=job.job_id,
              block_count=len(merged.blocks), status="CHUNKING", state="done",
              mineru_file_submit_count=sum(p.batch_id is not None for p in manifest.parts))
    return "done", len(merged.blocks)


def submitted_count(job) -> int:
    """按确认 Batch 的不同分片计费观测，不累加重复日志，也不把不确定提交视为零费用证明。"""
    parts = job.parse_options.get("_pdf_split", {}).get("parts")
    if parts is None:
        return int(job.batch_id is not None)
    return sum(bool(part.get("batch_id")) for part in parts)


def has_submission_history(job) -> bool:
    """父 Job 当前片未提交时仍保留旧片收费保护。"""
    return bool(job.submission_attempted or job.batch_id or any(
        part.get("submission_attempted") or part.get("batch_id")
        for part in job.parse_options.get("_pdf_split", {}).get("parts", [])))


def resumable_split(document, job) -> bool:
    """显式 Retry 续原Batch或从未提交的安全等待片；远端失败/未知POST不复活。"""
    if "_pdf_split" not in job.parse_options:
        return False
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if part is not None and (part.data_id != job.data_id or part.batch_id != job.batch_id
            or part.submission_attempted != job.submission_attempted
            or part.upload_complete != job.upload_complete):
        return False
    code = job.error_code or ""
    transport = code in {"JOB_TIMEOUT", "NETWORK", "UPLOAD_NETWORK", "DOWNLOAD_NETWORK", "DATABASE", "QUEUE", "HTTP_429"} or code.startswith("HTTP_5")
    if transport and job.batch_id and job.upload_complete:
        return True
    if (transport and job.batch_id and job.upload_url and job.upload_expires_at
            and job.upload_expires_at > datetime.now(UTC)):
        return True
    waiting = code in {"JOB_TIMEOUT", "CACHE_IN_PROGRESS", "RATE_LIMIT", "QUOTA_UNAVAILABLE",
                       "DAILY_LIMIT", "NETWORK", "QUEUE", "DATABASE"}
    return bool(waiting and not job.batch_id and not job.submission_attempted)
