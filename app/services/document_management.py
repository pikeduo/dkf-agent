"""上传增量及管理员维护的持久化编排；复用既有文档任务，不重建整个知识库。"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.business_logging import log_event
from app.models import Document, DocumentParseJob
from app.schemas.document_management import ManagementResponse
from app.services.chunk.processing import prepare_chunk, publish_chunk
from app.services.embedding.processing import prepare_embedding, publish_embedding
from app.services.indexing import prepare_index, publish_index
from app.services.chunk.splitter import ChunkError
from app.services.embedding.service import EmbeddingError
from app.services.indexing import IndexingError
from app.services.mineru.errors import LOCAL_ADAPTER_ERROR_CODES

ERRORS = {
    "MANAGEMENT_BUSY": "文档仍有活动任务，请先查询状态；未覆盖现有代次",
    "MANAGEMENT_STATE": "当前状态不允许该操作，请使用Retry恢复失败阶段或明确Reparse",
    "MANAGEMENT_CHECKPOINT": "管理检查点与当前文档代次不一致，未执行操作",
    "MANAGEMENT_DISPATCH": "管理任务投递确认失败，请查询状态后使用Retry恢复",
    "MANAGEMENT_STAGE_FAILED": "文档处理阶段失败，请查看文档和解析任务的安全原因",
    "MANAGEMENT_TIMEOUT": "管理编排等待已超时；未取消后台任务，请查询状态后恢复",
    "DELETE_PATH": "待删除文件路径或所有权无法安全确认，未越界清理",
    "DELETE_STORAGE": "文件清理未完成，文档保留删除检查点，请恢复删除任务",
}
STAGES = {"parse", "cloud", "chunk", "embed", "index", "delete", "done"}
PUBLISHERS = {"chunk": publish_chunk, "embed": publish_embedding, "index": publish_index}
CHUNK_CODES = {"CHUNK_BUSY", "CHUNK_CHECKPOINT_INVALID", "CHUNK_CONFIG_INVALID", "CHUNK_DISPATCH",
               "CHUNK_LIMIT", "CHUNK_STATE", "CHUNK_EMPTY", "CHUNK_SOURCE_ORDER"}


class ManagementError(Exception):
    """管理错误使用固定白名单，禁止本机路径、任务原文或凭据透传。"""

    def __init__(self, code):
        """未知错误归为检查点失败，不接受任意异常文本作为返回内容。"""
        self.code = code if code in ERRORS else "MANAGEMENT_CHECKPOINT"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def config_for(document) -> dict | None:
    """读取私有检查点并核对绑定代次；没有检查点时不为旧流程开启自动编排。"""
    config = document.management_config
    if config is None and document.management_task_id is None:
        return None
    try:
        if (not isinstance(config, dict) or config["version"] != "document-management-v1"
                or document.management_task_id is None
                or config["source_parse_task_id"] != str(document.processing_task_id)
                or config["operation"] not in {"ingest", "retry", "reindex", "reparse", "delete"}
                or config["parse_operation"] not in {"ingest", "reindex", "reparse"}
                or config["parse_mode"] not in {"auto", "native", "mineru"}
                or config["stage"] not in STAGES or config["state"] not in {"running", "failed", "done"}
                or type(config["dispatched"]) is not bool or type(config["resume_failed"]) is not bool
                or datetime.fromisoformat(config["expires_at"]).tzinfo is None):
            raise ValueError
        if config.get("stage_task_id") is not None:
            UUID(config["stage_task_id"])
        return dict(config)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ManagementError("MANAGEMENT_CHECKPOINT") from None


def public_management(document) -> ManagementResponse | None:
    """只返回管理标识、阶段和状态，不输出冻结参数/路径/原件信息。"""
    config = config_for(document)
    if config is None:
        return None
    # 即使业务任务持锁，管理员仍能从已提交截止时间观察超时；不伪装取消了后台任务。
    if config["state"] == "running" and datetime.now(UTC) >= datetime.fromisoformat(config["expires_at"]):
        config.update(state="failed", error_code="MANAGEMENT_TIMEOUT")
    return ManagementResponse(doc_id=document.doc_id, kb_id=document.kb_id,
        management_task_id=document.management_task_id, operation=config["operation"],
        stage=config["stage"], state=config["state"], status=document.status,
        error_code=config.get("error_code") if config.get("error_code") in ERRORS else None,
        cause_code=safe_cause_code(config.get("cause_code")))


def safe_cause_code(code):
    """根因仅接受既有服务白名单，不把任意异常字符串加入日志或API。"""
    from app.services.embedding.service import ERRORS as embedding_errors
    from app.services.indexing import ERRORS as index_errors
    from app.parsers.routing.contracts import REASON_CODES
    return code if isinstance(code, str) and code in (CHUNK_CODES | embedding_errors.keys() | index_errors.keys() | REASON_CODES) else None


def exception_cause(exc):
    """保留准备阶段的固定失败类别，避免GPU配置/入库模式错误被笼统隐藏。"""
    if isinstance(exc, (EmbeddingError, IndexingError)):
        return safe_cause_code(exc.code)
    if isinstance(exc, ChunkError):
        return safe_cause_code(str(exc).split("：", 1)[0])
    return None


def active_job(session, document):
    """只查询该文档的活动云检查点，不请求供应商，不推测Celery结果。"""
    return session.scalar(select(DocumentParseJob).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))


def retry_cloud_job(session, document, settings):
    """显式Retry恢复原Job；本地适配失败强制离线重放，缺ZIP不能转云或新建Job。"""
    from app.services.mineru.jobs import renew_local_wait
    from app.services.mineru.errors import MinerUError
    from app.services.mineru.pdf_split_jobs import resumable_split, sync_current
    job = active_job(session, document)
    if job is None and document.status == "FAILED":
        job = session.scalar(select(DocumentParseJob).where(
            DocumentParseJob.doc_id == document.doc_id,
            DocumentParseJob.task_id == document.processing_task_id,
            DocumentParseJob.state == "failed", DocumentParseJob.is_active.is_(False),
            # 本地适配错误不受某个具体子码限制，但必须有同Batch的可校验原ZIP。
            or_(DocumentParseJob.error_code == "JOB_TIMEOUT",
                DocumentParseJob.error_code.in_(sorted(LOCAL_ADAPTER_ERROR_CODES)),
                DocumentParseJob.parse_options.has_key("_pdf_split")),
            ).with_for_update())
        if not isinstance(job, DocumentParseJob):
            return None
        try:
            split_resume = resumable_split(document, job)
            local_replay = False
            if job.error_code in LOCAL_ADAPTER_ERROR_CODES or job.parse_options.get("_local_replay_only") is True:
                from app.services.mineru.jobs import check_normalizer_recipe, local_file
                from app.services.mineru.provider import validate_local_file
                from app.services.mineru.raw_results import load_raw_result
                if (job.task_id != document.processing_task_id or job.file_hash != document.file_hash
                        or job.provider != "mineru_cloud" or not job.batch_id
                        or not job.upload_complete or not job.submission_attempted
                        or not isinstance(job.parse_options.get("_cache_recipe"), dict)):
                    raise MinerUError("RAW_RESULT_IDENTITY", "原付费结果或冻结身份不完整，未恢复")
                check_normalizer_recipe(job)
                source = local_file(document, job, settings)
                validate_local_file(source)
                ordinal = 1
                if "_pdf_split" in job.parse_options:
                    from app.services.mineru.pdf_split import checked_manifest
                    from app.services.mineru.pdf_split_jobs import current_part
                    part = current_part(checked_manifest(document, job))
                    if part is None:
                        raise MinerUError("PDF_SPLIT_CHECKPOINT", "缺少当前分片，未恢复")
                    ordinal = part.ordinal
                local_replay = load_raw_result(settings, doc_id=document.doc_id, job_id=job.job_id,
                    ordinal=ordinal, batch_id=job.batch_id, data_id=job.data_id,
                    file_hash=source.file_hash) is not None
                if not local_replay:
                    raise MinerUError("LOCAL_RESULT_REQUIRED", "本地原ZIP缺失，未恢复为云任务")
            permitted = job.error_code == "JOB_TIMEOUT" or split_resume or local_replay
        except MinerUError:
            raise ManagementError("MANAGEMENT_CHECKPOINT") from None
        if (job.doc_id != document.doc_id or job.task_id != document.processing_task_id
                or job.is_active or job.state != "failed" or not permitted
                or job.file_hash != document.file_hash
                or not (job.batch_id and job.upload_complete or split_resume)
                or job.provider != "mineru_cloud"):
            raise ManagementError("MANAGEMENT_CHECKPOINT")
        # 明确绑定本次恢复为纯本地；后续所有分片和迟到submit消息都继承禁止云访问的边界。
        if local_replay:
            job.parse_options = job.parse_options | {"_local_replay_only": True}
        try:
            renew_local_wait(job, settings)
        except MinerUError:
            raise ManagementError("MANAGEMENT_CHECKPOINT") from None
        job.is_active, job.state, job.finished_at = True, "pending", None
        job.error_code = job.error_message = document.error_message = None
        document.status = "PARSING"
        sync_current(document, job, settings)
    elif job is not None:
        try:
            renew_local_wait(job, settings)
        except MinerUError:
            raise ManagementError("MANAGEMENT_CHECKPOINT") from None
    return job


def require_idle(session, document) -> None:
    """行锁内拒绝活动云/切片/GPU/入库；排队原生任务通过换代次失效而非强杀进程。"""
    config = config_for(document)
    if (config and config["state"] == "running" or active_job(session, document) is not None
            or document.status in {"PARSING", "OCR_PROCESSING"}
            or document.status == "CHUNKING" and document.chunk_task_id is not None
            or document.status == "EMBEDDING" and document.embedding_task_id is not None
            or document.status == "INDEXING" and document.index_task_id is not None):
        raise ManagementError("MANAGEMENT_BUSY")


def retry_stage(document) -> str:
    """从最下游已有检查点恢复，不把旧向量/旧Block冒充新的完整处理输入。"""
    if document.status in {"UPLOADED", "CHUNKING", "EMBEDDING", "INDEXING"}:
        return {"UPLOADED": "parse", "CHUNKING": "chunk", "EMBEDDING": "embed", "INDEXING": "index"}[document.status]
    if document.index_task_id is not None:
        return "index"
    if document.embedding_task_id is not None:
        return "embed"
    if document.chunk_task_id is not None:
        return "chunk"
    return "parse"


def prepare_ingestion(session, document, settings, *, parse_mode="auto", job=None) -> UUID:
    """行锁内为新上传文件建立增量检查点；首阶段仍由原上传入口提交，不重置解析代次。

    显式云上传可绑定刚创建的Job或已成功回放的缓存；不恢复旧失败文档、不触发云请求。
    """
    previous = config_for(document)
    if previous is not None:
        if (previous["operation"] == "ingest" and previous["state"] == "running"
                and previous["parse_mode"] == parse_mode):
            return document.management_task_id
        raise ManagementError("MANAGEMENT_STATE")
    if (parse_mode not in {"auto", "native", "mineru"} or document.processing_task_id is None
            or any(getattr(document, field) is not None for field in
                   ("chunk_task_id", "embedding_task_id", "index_task_id"))):
        raise ManagementError("MANAGEMENT_CHECKPOINT")
    stage, stage_task_id = "parse", str(document.processing_task_id)
    if job is None:
        if document.status != "UPLOADED":
            raise ManagementError("MANAGEMENT_STATE")
    else:
        if (job.doc_id != document.doc_id or job.task_id != document.processing_task_id
                or job.file_hash != document.file_hash or parse_mode != "mineru"):
            raise ManagementError("MANAGEMENT_CHECKPOINT")
        if job.is_active and document.status in {"PARSING", "OCR_PROCESSING"}:
            stage, stage_task_id = "cloud", str(job.job_id)
        elif not job.is_active and job.state == "done" and document.status == "CHUNKING":
            stage, stage_task_id = "chunk", None
        else:
            raise ManagementError("MANAGEMENT_STATE")
    document.management_task_id = uuid4()
    document.management_config = {
        "version": "document-management-v1", "operation": "ingest", "parse_operation": "ingest",
        "source_parse_task_id": str(document.processing_task_id), "parse_mode": parse_mode,
        "state": "running", "stage": stage, "stage_task_id": stage_task_id,
        # 原入口已冻结首阶段发送意图；管理任务只接续后续阶段，缓存命中则直接准备Chunk。
        "dispatched": stage != "chunk", "resume_failed": False,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=max(600, settings.mineru_job_timeout_seconds))).isoformat(),
    }
    session.flush()
    return document.management_task_id


def rebind_cloud_management(document, task_id: UUID, job_id: UUID) -> None:
    """Router创建云代次时同步绑定管理检查点；同事务换代，管理UUID与既有收费Job不变。"""
    config = config_for(document)
    if config is None:
        return
    if config["stage"] != "parse" or config["operation"] == "delete":
        raise ManagementError("MANAGEMENT_CHECKPOINT")
    config.update(source_parse_task_id=str(task_id), stage="cloud", stage_task_id=str(job_id), dispatched=True)
    document.management_config = config


def record_ingestion_dispatch_failure(session, doc_id: UUID, manager_id: UUID) -> None:
    """独立短事务标记上传编排投递失败；不覆盖已推进的Document或新代次，原件始终保留。"""
    session.rollback()
    try:
        with session.begin():
            mark_failed(session, doc_id, manager_id, "MANAGEMENT_DISPATCH")
    except (SQLAlchemyError, ManagementError):
        session.rollback()
        log_event("management.error_record_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=manager_id, error_code="MANAGEMENT_DISPATCH")


def publish_ingestion(session, doc_id: UUID, manager_id: UUID) -> None:
    """首阶段提交后启动后续增量编排；确认失败安全记录并交由显式Retry恢复，不盲目补投。"""
    if not publish_management(doc_id, manager_id):
        record_ingestion_dispatch_failure(session, doc_id, manager_id)
        raise ManagementError("MANAGEMENT_DISPATCH")
    log_event("management.requested", doc_id=doc_id, task_id=manager_id, operation="ingest")


def prepare_management(session, document, settings, operation: str, *, parse_mode="auto") -> tuple[UUID, bool]:
    """在调用方行锁中建立/恢复检查点；新维护换解析代次，Retry只复用现有业务代次。"""
    previous = config_for(document)
    if previous and previous["state"] == "running" and datetime.now(UTC) >= datetime.fromisoformat(previous["expires_at"]):
        # 显式恢复时允许换编排代次；不取消仍在后台运行的业务任务或改变其冻结参数。
        previous.update(state="failed", error_code="MANAGEMENT_TIMEOUT")
        document.management_config = previous
    if operation not in {"retry", "reindex", "reparse", "delete"}:
        raise ManagementError("MANAGEMENT_STATE")
    if previous and previous["state"] == "running":
        if operation == "retry" or operation == previous["operation"]:
            if operation == "reparse" and parse_mode != previous["parse_mode"]:
                raise ManagementError("MANAGEMENT_BUSY")
            # 显式Retry可恢复丢失的编排消息，但阶段投递意图阻止重复收费/重算。
            return document.management_task_id, operation == "retry"
        raise ManagementError("MANAGEMENT_BUSY")
    if operation == "retry" and previous and previous["operation"] == "delete":
        operation = "delete"
    roots = None
    if operation == "delete":
        require_idle(session, document)
        from app.services.document_cleanup import cleanup_roots
        # 必须在清空GPU检查点之前冻结旧输出根，避免配置更改后遗留向量文件。
        roots = previous["cleanup_roots"] if previous and previous["operation"] == "delete" else cleanup_roots(document, settings)
    stage, stage_task_id, resume_failed = "parse", None, False
    if operation == "retry":
        if document.status == "READY":
            if previous is not None:
                # 入库已提交但最后编排消息丢失时，仅补完成标记，不重算任何阶段。
                previous.update(state="done", stage="done", error_code=None, cause_code=None)
                document.management_config = previous
                session.flush()
                return document.management_task_id, False
            raise ManagementError("MANAGEMENT_STATE")
        job = retry_cloud_job(session, document, settings)
        if job is not None:
            if job.task_id != document.processing_task_id or job.file_hash != document.file_hash or job.state == "failed":
                raise ManagementError("MANAGEMENT_CHECKPOINT")
            stage, stage_task_id = "cloud", str(job.job_id)
        else:
            if document.status in {"PARSING", "OCR_PROCESSING"}:
                raise ManagementError("MANAGEMENT_BUSY")
            stage = retry_stage(document)
            resume_failed = document.status == "FAILED"
        if document.processing_task_id is None:
            document.processing_task_id = uuid4()
        # 不继承Force：普通Retry绝不能重新申请已失败/收费的云批次。
        parse_mode = previous["parse_mode"] if previous else parse_mode
    else:
        if operation != "delete":
            require_idle(session, document)
        document.processing_task_id = uuid4()
        document.chunk_task_id, document.chunk_config = None, None
        document.embedding_task_id, document.embedding_config = None, None
        document.index_task_id, document.index_config = None, None
        document.status, document.error_message = "UPLOADED", None
        if operation == "delete":
            stage = "delete"
            document.status, document.error_message = "FAILED", "DOCUMENT_DELETE_PENDING：删除处理中，暂不可检索"

    config = {"version": "document-management-v1", "operation": operation,
        "parse_operation": operation if operation in {"reindex", "reparse"} else "ingest",
        "source_parse_task_id": str(document.processing_task_id), "parse_mode": parse_mode,
        "state": "running", "stage": stage, "stage_task_id": stage_task_id,
        "dispatched": False, "resume_failed": resume_failed,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=max(600, settings.mineru_job_timeout_seconds))).isoformat()}
    if operation == "delete":
        config["cleanup_roots"] = roots
    document.management_task_id, document.management_config = uuid4(), config
    session.flush()
    return document.management_task_id, True


def publish_management(doc_id: UUID, task_id: UUID) -> bool:
    """提交后只投递UUID；确认丢失不改业务状态，用户可用Retry恢复同一检查点。"""
    from app.tasks.management import manage_document
    try:
        manage_document.apply_async(args=[str(doc_id)], task_id=str(task_id), queue="default_queue")
    except Exception:
        log_event("management.dispatch_failed", level=logging.ERROR, doc_id=doc_id,
                  task_id=task_id, error_code="MANAGEMENT_DISPATCH")
        return False
    log_event("management.dispatched", doc_id=doc_id, task_id=task_id, queue="default_queue")
    return True


def next_action(session, document, settings) -> dict:
    """一次只准备一个阶段并持久化投递意图；等待只查询DB，不调用云API或加载模型。"""
    config = config_for(document)
    if config is None or config["state"] != "running":
        return {"kind": "stopped"}
    if config["operation"] == "delete":
        return {"kind": "delete"}
    if document.status == "READY":
        config.update(state="done", stage="done")
        document.management_config = config
        return {"kind": "done"}
    if datetime.now(UTC) >= datetime.fromisoformat(config["expires_at"]):
        raise ManagementError("MANAGEMENT_TIMEOUT")
    if document.status == "FAILED" and not config["resume_failed"]:
        raise ManagementError("MANAGEMENT_STAGE_FAILED")
    if (config["stage"] == "cloud" and not config["dispatched"]
            and document.status not in {"CHUNKING", "EMBEDDING", "INDEXING", "READY"}):
        job = active_job(session, document)
        if job is None or str(job.job_id) != config["stage_task_id"] or job.task_id != document.processing_task_id:
            raise ManagementError("MANAGEMENT_CHECKPOINT")
        config["dispatched"] = True
        document.management_config = config
        return {"kind": "dispatch", "stage": "cloud", "task_id": str(job.job_id),
                "cloud_step": "check" if job.upload_complete else "submit"}
    if document.status in {"PARSING", "OCR_PROCESSING"}:
        return {"kind": "wait"}
    stage = retry_stage(document) if document.status == "FAILED" else {
        "UPLOADED": "parse", "CHUNKING": "chunk", "EMBEDDING": "embed", "INDEXING": "index"}.get(document.status)
    if stage is None:
        raise ManagementError("MANAGEMENT_STATE")
    if config["stage"] == stage and config["dispatched"]:
        return {"kind": "wait"}
    if stage == "parse":
        document.status, document.error_message = "UPLOADED", None
        task_id, dispatch = document.processing_task_id, True
    elif stage == "chunk":
        task_id, dispatch = prepare_chunk(session, document, settings)
    elif stage == "embed":
        task_id, dispatch = prepare_embedding(session, document, settings)
    else:
        task_id, dispatch = prepare_index(session, document)
    config.update(stage=stage, stage_task_id=str(task_id), dispatched=True, resume_failed=False)
    document.management_config = config
    session.flush()
    return {"kind": "dispatch" if dispatch else "wait", "stage": stage, "task_id": str(task_id),
            "parse_mode": config["parse_mode"], "operation": config["parse_operation"]}


def dispatch_action(session, doc_id: UUID, action: dict) -> bool:
    """只在检查点事务提交后调用既有投递边界；不运行任务主体，不重复解析器实现。"""
    if action["kind"] != "dispatch":
        return True
    stage, task_id = action["stage"], UUID(action["task_id"])
    if stage == "cloud":
        from app.tasks.mineru import publish_step
        try:
            publish_step(task_id, action["cloud_step"])
        except Exception:
            raise ManagementError("MANAGEMENT_DISPATCH") from None
        return True
    if stage == "parse":
        from app.services.document_dispatch import publish_document
        return publish_document(session, doc_id, task_id, parse_mode=action["parse_mode"], operation=action["operation"])
    return PUBLISHERS[stage](session, doc_id, task_id)


def mark_failed(session, doc_id: UUID, task_id: UUID, code: str, cause_code=None) -> None:
    """独立记录编排失败并保留已验证的底层Router原因，不改原文档错误或新代次。"""
    document = session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update(nowait=True))
    if document is None or document.management_task_id != task_id:
        return
    config = config_for(document)
    if config["state"] != "running":
        return
    if code == "MANAGEMENT_STAGE_FAILED" and cause_code is None:
        from app.services.document_processing import router_failure_codes
        causes = router_failure_codes(document.error_message)
        cause_code = causes[0] if causes else None
    config.update(state="failed", error_code=ManagementError(code).code, cause_code=safe_cause_code(cause_code))
    document.management_config = config
    if config["operation"] == "delete":
        document.status, document.error_message = "FAILED", str(ManagementError(code))
