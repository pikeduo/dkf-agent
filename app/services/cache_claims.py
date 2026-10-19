"""复用 PostgreSQL 事务锁和云任务检查点保护缓存 miss，不额外引入服务或后台续租。"""

from sqlalchemy import func, or_, select

from app.core.business_logging import log_event
from app.models import DocumentParseJob
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import ParseCache, key_for


def lock_keys(session, keys: list[str]) -> None:
    """按固定顺序获取事务级 advisory lock，串行检查跨文档整份/视觉缓存身份。"""

    for key in sorted(set(keys)):
        number = int(key[:16], 16)
        if number >= 2**63:
            number -= 2**64
        session.execute(select(func.pg_advisory_xact_lock(number)))


def claim_miss(session, document, value: dict, visual_keys: list[str], *, force_reparse: bool,
               authorized_office_source=None):
    """保留所有收费保护；仅排除已复核并显式授权的单个失败Office源Job，不泛化为Force。"""

    from app.services.mineru.pdf_split_jobs import has_submission_history

    key = key_for(value)
    lock_keys(session, [key, value["file_hash"], *visual_keys])
    identities = [DocumentParseJob.parse_options.contains({"_cache_key": key})]
    # 本地Normalizer升级不能使旧收费历史失踪；先恢复原Job的ZIP生成新缓存。
    identities.append((DocumentParseJob.file_hash == value["file_hash"]) &
                      (DocumentParseJob.provider == "mineru_cloud"))
    identities.extend(DocumentParseJob.parse_options.contains({"_cache_visual_keys": [v]}) for v in visual_keys)
    previous = session.scalars(select(DocumentParseJob).where(or_(*identities))).all()
    if any(job.is_active for job in previous):
        log_event("cache.busy", doc_id=document.doc_id, cache_key=key, error_code="CACHE_IN_PROGRESS")
        raise MinerUError("CACHE_IN_PROGRESS", "相同内容已有活动云任务，未重复提交；完成后重试即可复用缓存")
    # 同代次已有 batch/申请意图包括失败或 done 后本地结果缺失，普通重试不重复花费。
    if not force_reparse and any(has_submission_history(job) and job.job_id != authorized_office_source for job in previous):
        raise MinerUError("CACHE_REPARSE_REQUIRED", "相同内容已有云提交历史；请先用原Job的本地ZIP恢复，未自动上传或重新收费")
    legacy = session.scalars(select(DocumentParseJob).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.file_hash == document.file_hash,
        DocumentParseJob.provider == "mineru_cloud")).all()
    if not force_reparse and any(has_submission_history(job) and job.job_id != authorized_office_source for job in legacy):
        raise MinerUError("CACHE_REPARSE_REQUIRED", "该文档已有付费结果历史；请恢复原Job的本地ZIP，不应因Adapter错误强制重解析")
