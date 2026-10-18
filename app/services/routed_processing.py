"""行锁内预检、路由与原子落库；云端交接只创建检查点，不发送 HTTP。"""

import hashlib
import logging
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.core.file_formats import MINERU_TYPES
from app.models import Document, DocumentBlock
from app.parsers.common import OCRRequiredError, ParserError
from app.parsers.hybrid import HybridPreflightError, merge_hybrid
from app.parsers.registry import get_parser
from app.parsers.routing.contracts import OFFICE_SNAPSHOT_VERSION, ParseMode, RouteDecision
from app.parsers.routing.guard import CompletenessError, guard_result
from app.parsers.routing.inspector import inspect_document
from app.parsers.routing.router import decide_route, fallback_route
from app.services.document_processing import (
    check_source,
    check_source_hash,
    persist_blocks,
    source_path,
    transition_document,
)
from app.services.mineru.jobs import create_job
from app.services.hybrid_processing import metrics, prepare_checkpoint
from app.services.parse_cache import CacheError, ParseCache, recipe, guard_cached


def log_decision(
    event: str, decision: RouteDecision, doc_id: UUID, task_id: UUID
) -> None:
    """日志仅记录固定理由与安全计数，保存快照不输出外链地址或缓存值。"""

    log_event(
        event,
        doc_id=doc_id,
        task_id=task_id,
        decision_version=decision.decision_version,
        parse_mode=decision.parse_mode,
        route_strategy=decision.route_strategy,
        reason_codes=",".join(decision.reason_codes),
        fallback_count=decision.fallback_count,
        file_type=decision.file_type,
        page_count=decision.signals.page_count,
        sheet_count=decision.signals.sheet_count,
        image_count=decision.signals.image_count,
        chart_count=decision.signals.chart_count,
        source_document_count=1,
        **({"item_count": decision.signals.office_snapshot_rel_count}
           if decision.signals.office_snapshot_rel_count else {}),
    )


def run_routed_step(
    session: Session, doc_id: UUID, task_id: UUID, mode: ParseMode, settings: Settings,
    *, force_reparse: bool = False, cache_only: bool = False,
) -> dict:
    """复用冻结缓存并隔离Office保存快照；Reindex不解析或发云，Reparse才绕过缓存。"""

    if mode not in {"auto", "native", "mineru"}:
        raise ParserError("解析模式仅允许 auto、native 或 mineru")
    document = session.scalar(
        select(Document).where(Document.doc_id == doc_id).with_for_update()
    )
    result = {"doc_id": str(doc_id)}
    if document is None:
        return result | {"status": "not_found"}
    result["document_status"] = document.status
    if document.processing_task_id != task_id:
        return result | {"status": "stale_task"}
    if document.status not in {"UPLOADED", "PARSING"}:
        return result | {"status": "skipped"}
    check_source(document)
    path = source_path(document)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
            raise ParserError("文档原件 Hash 已变化，请核对存储内容")
    cache = ParseCache(settings)
    if cache_only:
        parsed = cache.replay(document, settings)
        check_source_hash(document, path)
        transition_document(document, "PARSING")
        persist_blocks(session, document, parsed)
        transition_document(document, "CHUNKING")
        return result | {"status": "awaiting_chunk", "document_status": document.status,
                         "block_count": len(parsed.blocks), "parse_cache_hit": True,
                         "mineru_file_submit_count": 0}
    inspection = inspect_document(path, document.file_type, settings)
    decision = decide_route(document.file_type, mode, inspection,
                            hybrid_enabled=settings.hybrid_xlsx_enabled)
    hybrid_context = None
    hybrid_metrics = {}
    plan = None
    if decision.route_strategy == "hybrid_bundle":
        log_event("hybrid.started", doc_id=doc_id, task_id=task_id, file_type="xlsx")
        try:
            plan, hybrid_context = prepare_checkpoint(document, task_id, settings, decision,
                **({"force_reparse": True} if force_reparse else {}))
            hybrid_metrics = metrics(plan.manifest)
        except HybridPreflightError as exc:
            log_event("hybrid.preflight_rejected", doc_id=doc_id, task_id=task_id,
                      error_code=exc.code, route_strategy="mineru_whole" if exc.whole_compatible else "failed")
            decision = decision.model_copy(update={
                "route_strategy": "mineru_whole" if exc.whole_compatible else "failed",
                "fallback_count": 1 if exc.whole_compatible else 0,
                # 终止原因优先，避免管理/阶段33把“Hybrid已启用”误显示成真实错误。
                "reason_codes": tuple(dict.fromkeys((exc.code,) + decision.reason_codes + (
                    ("HYBRID_PREFLIGHT_WHOLE_FALLBACK", "XLSX_WHOLE_WITH_NATIVE_PRIMARY")
                    if exc.whole_compatible else ()
                ))),
            })
            # 尚未创建云 Job/POST；云端错误或恢复消息绝不进入此提交前分支。
            if exc.whole_compatible:
                log_decision("router.fallback", decision, doc_id, task_id)
    log_decision("router.decided", decision, doc_id, task_id)
    transition_document(document, "PARSING")
    if decision.route_strategy == "failed":
        message = "当前文件无法在安全边界内完整解析，未提交云任务"
        if "MINERU_UNSUPPORTED_FORMAT" in decision.reason_codes:
            message = (
                "当前文件格式不受云端支持，未提交云任务"
            )
        elif "MD_ASSET_NOT_UPLOADED" in decision.reason_codes:
            message = "Markdown 所需图片附件未上传，不自动获取外部 URL"
        elif "PREFLIGHT_LIMIT" in decision.reason_codes:
            message = (
                "预检超出资源限制，未提交云任务或保存部分结果"
            )
        elif "EXTERNAL_ATTACHMENT_MISSING" in decision.reason_codes:
            message = "Office 所需外部附件未提供，不能完整解析"
        elif "XLSX_EXTERNAL_ASSET_MISSING" in decision.reason_codes:
            message = "XLSX 外部工作簿附件未提供，不能完整解析"
        message = f"ROUTER_FAILED[{','.join(decision.reason_codes)}]：{message}"
        transition_document(document, "FAILED", message)
        log_decision("router.failed", decision, doc_id, task_id)
        return result | {
            "status": "failed", "error_message": message,
            "document_status": document.status,
            "route_decision": decision.model_dump(mode="json"),
            "mineru_file_submit_count": 0,
        }
    if plan is not None:
        identity = recipe(document, settings, "hybrid_bundle", manifest=plan.manifest)
        cached = None if force_reparse else cache.document(document, identity)
        if cached is not None:
            guard_cached(cached, decision)
            check_source_hash(document, path)
            persist_blocks(session, document, cached)
            transition_document(document, "CHUNKING")
            return result | {"status": "awaiting_chunk", "document_status": document.status,
                "block_count": len(cached.blocks), "route_decision": decision.model_dump(mode="json"),
                "parse_cache_hit": True, "mineru_file_submit_count": 0, **hybrid_metrics}
        log_event("hybrid.prepared", doc_id=doc_id, task_id=task_id, **hybrid_metrics)
        if not plan.manifest.bundle_page_count:
            parsed = merge_hybrid(plan, None)
            check_source_hash(document, path)
            from app.services.parse_cache import canonical
            cache.save_document(document, identity, parsed, {
                "hybrid/bundle_manifest.json": canonical(plan.manifest.model_dump(mode="json")),
                "hybrid/normalized_visual_blocks.json": canonical({h: [b.model_dump(mode="json")
                    for b in blocks] for h, blocks in plan.cached.items()}),
            })
            persist_blocks(session, document, parsed)
            transition_document(document, "CHUNKING")
            return result | {
                "status": "awaiting_chunk", "document_status": document.status,
                "block_count": len(parsed.blocks), "route_decision": decision.model_dump(mode="json"),
                "coverage": "hybrid_structural_guard_passed", "mineru_file_submit_count": 0,
                **hybrid_metrics,
            }
    if decision.route_strategy == "native":
        try:
            identity = recipe(document, settings, "native",
                              scope="explicit_native_scope" if mode == "native" else "complete",
                              office_snapshot_version=(OFFICE_SNAPSHOT_VERSION
                                  if decision.signals.office_snapshot_rel_count else None))
            cached = None if force_reparse else cache.document(document, identity)
            if cached is not None:
                guard_cached(cached, decision)
                check_source_hash(document, path)
                persist_blocks(session, document, cached)
                transition_document(document, "CHUNKING")
                return result | {"status": "awaiting_chunk", "document_status": document.status,
                    "block_count": len(cached.blocks), "route_decision": decision.model_dump(mode="json"),
                    "parse_cache_hit": True, "mineru_file_submit_count": 0}
            parser = get_parser(document.file_type)
            log_event(
                "parse.started",
                doc_id=doc_id,
                task_id=task_id,
                parser=type(parser).__name__,
                file_type=document.file_type,
                file_name=document.file_name,
            )
            parsed = parser.parse(
                doc_id=doc_id,
                file_path=path,
                file_name=document.file_name,
                file_type=document.file_type,
            )
            guard_result(parsed, decision)
        except (OCRRequiredError, CompletenessError):
            log_decision("router.guard_failed", decision, doc_id, task_id)
            # 非完整性错误（非法编码、空 CSV、损坏文档等）不会发给云端碰运气。
            if mode != "auto" or document.file_type not in MINERU_TYPES:
                raise
            decision = fallback_route(decision)
            log_decision("router.fallback", decision, doc_id, task_id)
        else:
            log_event(
                "parse.validated",
                doc_id=doc_id,
                task_id=task_id,
                block_count=len(parsed.blocks),
            )
            log_decision("router.guard_passed", decision, doc_id, task_id)
            check_source_hash(document, path)
            cache.save_document(document, identity, parsed)
            persist_blocks(session, document, parsed)
            transition_document(document, "CHUNKING")
            return result | {
                "status": "awaiting_chunk",
                "document_status": document.status,
                "block_count": len(parsed.blocks),
                "route_decision": decision.model_dump(mode="json"),
                "coverage": "explicit_native_scope"
                if mode == "native"
                else "structural_guard_passed",
                "mineru_file_submit_count": 0,
            }
    job = create_job(
        session,
        document,
        settings,
        routing_task_id=task_id,
        route_decision=decision.model_dump(mode="json"),
        hybrid_context=hybrid_context,
        force_reparse=force_reparse,
    )
    if not job.is_active:
        return result | {"status": "awaiting_chunk", "document_status": document.status,
            "block_count": session.scalar(select(func.count()).select_from(DocumentBlock)
                .where(DocumentBlock.doc_id == doc_id)),
            "route_decision": decision.model_dump(mode="json"), "parse_cache_hit": True,
            "mineru_file_submit_count": 0}
    # 换成云任务代次后旧 Native 消息/失败回调全部失效；提交之后才投递短任务。
    return result | {
        "status": "delegated_to_mineru",
        "document_status": document.status,
        "job_id": str(job.job_id),
        "task_id": str(job.task_id),
        "route_decision": decision.model_dump(mode="json"),
        "mineru_file_submit_count": 0,
        # 实际字节分片只能由 Worker 生成后确定；此时不把页数估算冒充收费文件数。
        "planned_mineru_file_count": None if "_pdf_split_request" in job.parse_options else 1,
        **hybrid_metrics,
    }


def record_dispatch_error(session: Session, job_id: UUID) -> None:
    """投递失败保留活动云检查点；已有编排标记失败供Retry恢复，不回退已完成云状态。"""

    from app.services.mineru.jobs import lock_job

    locked = lock_job(session, job_id)
    if locked:
        document, job = locked
        from app.services.document_management import mark_failed
        if document.management_task_id is not None:
            mark_failed(session, document.doc_id, document.management_task_id, "MANAGEMENT_DISPATCH")
        job.error_code = "QUEUE"
        job.error_message = document.error_message = (
            "MinerU 短任务投递确认失败，请用文档Retry恢复同一任务"
            if document.management_task_id is not None else
            "MinerU 短任务投递确认失败，请用已有文档 MinerU 入口恢复同一任务"
        )
    log_event(
        "router.cloud_dispatch_failed",
        level=logging.ERROR,
        job_id=job_id,
        error_code="QUEUE",
    )
