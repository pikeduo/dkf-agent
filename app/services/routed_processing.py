"""行锁内预检、路由与原子落库；云端交接只创建检查点，不发送 HTTP。"""

import hashlib
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.core.file_formats import MINERU_TYPES
from app.models import Document
from app.parsers.common import OCRRequiredError, ParserError
from app.parsers.hybrid import HybridReview, merge_hybrid
from app.parsers.registry import get_parser
from app.parsers.routing.contracts import ParseMode, RouteDecision
from app.parsers.routing.guard import CompletenessError, guard_result
from app.parsers.routing.inspector import inspect_document
from app.parsers.routing.router import decide_route, fallback_route
from app.services.document_processing import (
    check_source,
    persist_blocks,
    source_path,
    transition_document,
)
from app.services.mineru.jobs import create_job
from app.services.hybrid_processing import metrics, prepare_checkpoint


def log_decision(
    event: str, decision: RouteDecision, doc_id: UUID, task_id: UUID
) -> None:
    """日志只记录白名单理由与计数，完整结构决策由任务结果和云任务参数保存。"""

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
    )


def run_routed_step(
    session: Session, doc_id: UUID, task_id: UUID, mode: ParseMode, settings: Settings
) -> dict:
    """在当前代次路由和暂存；Hybrid 只有全部合并后写块，review 不调用云端。"""

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
    inspection = inspect_document(path, document.file_type, settings)
    decision = decide_route(document.file_type, mode, inspection,
                            hybrid_enabled=settings.hybrid_xlsx_enabled)
    hybrid_context = None
    hybrid_metrics = {}
    plan = None
    if decision.route_strategy == "hybrid_bundle":
        log_event("hybrid.started", doc_id=doc_id, task_id=task_id, file_type="xlsx")
        try:
            plan, hybrid_context = prepare_checkpoint(document, task_id, settings, decision)
            hybrid_metrics = metrics(plan.manifest)
        except HybridReview as exc:
            decision = decision.model_copy(update={
                "route_strategy": "review", "reason_codes": decision.reason_codes + (exc.code,)
            })
    log_decision("router.decided", decision, doc_id, task_id)
    transition_document(document, "PARSING")
    if decision.route_strategy == "review":
        message = "ROUTER_REVIEW：无法确认完整覆盖，请核对路由理由并显式选择 Native / MinerU 对照"
        if "MINERU_UNSUPPORTED_FORMAT" in decision.reason_codes:
            message = (
                "ROUTER_REVIEW：当前格式不支持 MinerU，请选择 Native 或补齐原件所需附件"
            )
        elif "MD_ASSET_NOT_UPLOADED" in decision.reason_codes:
            message = "ROUTER_REVIEW：Markdown 图片附件未上传，不自动获取 URL 或发送 MD 到 MinerU"
        elif "PREFLIGHT_LIMIT" in decision.reason_codes:
            message = (
                "ROUTER_REVIEW：预检超出资源限制，未执行部分解析，请人工核对文件规模"
            )
        transition_document(document, "FAILED", message)
        log_decision("router.review", decision, doc_id, task_id)
        return result | {
            "status": "review",
            "document_status": document.status,
            "route_decision": decision.model_dump(mode="json"),
            "mineru_file_submit_count": 0,
        }
    if plan is not None:
        log_event("hybrid.prepared", doc_id=doc_id, task_id=task_id, **hybrid_metrics)
        if not plan.manifest.bundle_page_count:
            parsed = merge_hybrid(plan, None)
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
    )
    # 换成云任务代次后旧 Native 消息/失败回调全部失效；提交之后才投递短任务。
    return result | {
        "status": "delegated_to_mineru",
        "document_status": document.status,
        "job_id": str(job.job_id),
        "task_id": str(job.task_id),
        "route_decision": decision.model_dump(mode="json"),
        "mineru_file_submit_count": 0,
        "planned_mineru_file_count": 1,
        **hybrid_metrics,
    }


def record_dispatch_error(session: Session, job_id: UUID) -> None:
    """投递确认失败只保留活动检查点和安全提示，绝不回退已完成云解析状态。"""

    from app.services.mineru.jobs import lock_job

    locked = lock_job(session, job_id)
    if locked:
        document, job = locked
        job.error_code = "QUEUE"
        job.error_message = document.error_message = (
            "MinerU 短任务投递确认失败，请用已有文档 MinerU 入口恢复同一任务"
        )
    log_event(
        "router.cloud_dispatch_failed",
        level=logging.ERROR,
        job_id=job_id,
        error_code="QUEUE",
    )
