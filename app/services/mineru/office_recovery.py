"""管理员显式授权把失败Office整份任务改为新的PDF分片输入；不重投旧Batch、不使用Force。"""

from uuid import UUID

from app.core.file_formats import OFFICE_TYPES
from app.models import DocumentParseJob
from app.services.document_processing import check_source_hash, DocumentSourceError
from app.services.mineru.errors import MinerUError
from app.services.mineru.pdf_split_jobs import has_submission_history
from app.services.mineru.raw_results import load_raw_result


def source_permission(document, source, settings, maximum):
    """只接受同Hash的已上传远端失败Whole；原ZIP可恢复、未知提交及已转换任务均拒绝。"""
    from app.services.document_management import ManagementError

    if (type(maximum) is not int or not 1 <= maximum <= 128 or document.file_type not in OFFICE_TYPES
            or source is None or source.doc_id != document.doc_id or source.file_hash != document.file_hash
            or source.provider != "mineru_cloud" or source.is_active or source.state != "failed"
            or source.error_code not in {"ROUTED_REMOTE_FAILED", "REMOTE_FAILED", "-60006"}
            or not source.batch_id or not source.upload_complete or not source.submission_attempted
            or not isinstance(source.parse_options, dict)
            or any(key in source.parse_options for key in ("_pdf_split", "_hybrid", "_office_pdf_request"))):
        raise ManagementError("OFFICE_PDF_RECOVERY")
    try:
        check_source_hash(document)
        raw = load_raw_result(settings, doc_id=document.doc_id, job_id=source.job_id, ordinal=1,
            batch_id=source.batch_id, data_id=source.data_id, file_hash=source.file_hash)
    except (OSError, DocumentSourceError, MinerUError):
        raise ManagementError("OFFICE_PDF_RECOVERY") from None
    if raw is not None:
        raise ManagementError("OFFICE_PDF_RAW_AVAILABLE")
    return {"source_job_id": str(source.job_id), "source_task_id": str(source.task_id),
            "source_hash": source.file_hash, "source_batch_id": source.batch_id,
            "source_data_id": source.data_id, "source_options_hash": source.options_hash,
            "max_new_parts": maximum}


def checked_authorization(session, document, settings):
    """CPU创建新Job前重验本次管理许可，不能把旧文档/其他代次许可当作通用收费绕过。"""
    from app.services.document_management import config_for, ManagementError

    if getattr(document, "management_config", None) is None and getattr(document, "management_task_id", None) is None:
        return None
    try:
        config = config_for(document)
        permission = config.get("office_pdf_recovery") if config else None
        if permission is None:
            return None
        if config["operation"] != "retry" or config["state"] != "running" or config["parse_operation"] != "ingest":
            raise ManagementError("OFFICE_PDF_RECOVERY")
        source = session.get(DocumentParseJob, UUID(permission["source_job_id"]))
        current = source_permission(document, source, settings, permission["max_new_parts"])
        if permission != current:
            raise ValueError
    except (ManagementError, KeyError, TypeError, ValueError):
        # CPU解析入口消费MinerUError，避免管理许可错误被最外层折叠成UNKNOWN。
        raise MinerUError("OFFICE_PDF_IDENTITY", "本次Office分片许可与原Job或当前管理代次不一致，未提交云任务") from None
    return permission


def unsubmitted_job(session, document):
    """仅定位当前从未提交任何PDF片的新转换Job；普通Retry可续本地准备而不创建另一代。"""
    from sqlalchemy import select

    job = session.scalar(select(DocumentParseJob).where(DocumentParseJob.doc_id == document.doc_id,
        DocumentParseJob.task_id == document.processing_task_id,
        DocumentParseJob.state == "failed", DocumentParseJob.is_active.is_(False),
        DocumentParseJob.parse_options.has_key("_office_pdf_request")).with_for_update())
    if (isinstance(job, DocumentParseJob) and job.file_hash == document.file_hash and job.provider == "mineru_cloud"
            and not has_submission_history(job)):
        return job
    return None
