"""只读核对原MinerU整份Job；显式许可后仅GET原Batch，不上传、下载结果、重投或修改业务数据。"""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import result_failure
from app.services.mineru.provider import EXTERNAL_STATES, MinerUCloudProvider, safe_identifier


def inspect_job(document, job, *, query=None):
    """核对当前同Hash整份Job；可选查询函数仅接收原Batch/Data ID，返回固定安全摘要。"""
    if (document.doc_id != job.doc_id or document.file_hash != job.file_hash
            or document.processing_task_id != job.task_id or job.provider != "mineru_cloud"
            or not isinstance(job.parse_options, dict)
            or any(key in job.parse_options for key in ("_pdf_split", "_hybrid"))):
        raise MinerUError("DIAGNOSTIC_IDENTITY", "原Job身份不符或不是整份任务，未查询云端")
    batch_id = safe_identifier(job.batch_id, optional=True)
    data_id = safe_identifier(job.data_id)
    stored = job.parse_options.get("_remote_failure")
    stored_cause = (result_failure({"err_code": stored.get("cause_code")}).code
                    if isinstance(stored, dict) else None)
    report = {"doc_id": str(document.doc_id), "job_id": str(job.job_id),
              "batch_id": batch_id, "data_id": data_id,
              "status": "LOCAL_CHECKPOINT_ONLY", "stored_cause_code": stored_cause,
              "cloud_queries": 0, "cloud_submitted": False, "result_downloaded": False,
              "database_modified": False, "cache_published": False, "business_recovered": False}
    if query is None:
        return report
    if not batch_id or job.upload_complete is not True:
        raise MinerUError("DIAGNOSTIC_CHECKPOINT", "原批次或已上传检查点缺失，未查询云端")
    result = query(batch_id, data_id)
    state = result.get("state")
    if not isinstance(state, str) or state not in EXTERNAL_STATES:
        raise MinerUError("BAD_RESPONSE", "MinerU 返回的状态无效")
    report.update(status="REMOTE_STATUS_ONLY", remote_state=state, cloud_queries=1,
                  trace_id=safe_identifier(result.get("trace_id"), optional=True))
    if state == "failed":
        error = result_failure(result)
        report.update(cause_code=error.code, message=str(error))
    # done只是供应商状态；没有取得ZIP或执行入库，绝不将诊断当作恢复成功。
    return report


def inspect_database(doc_id, settings, *, query_existing=False):
    """在只读SQL事务中选择唯一当前Job；只有显式开关才构造Provider并GET一次原批次。"""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session, defer
    from app.db.session import get_engine
    from app.models import Document, DocumentParseJob

    with Session(get_engine(), autoflush=False) as session:
        provider = None
        try:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            rows = session.execute(select(Document, DocumentParseJob).join(
                DocumentParseJob, DocumentParseJob.doc_id == Document.doc_id).where(
                Document.doc_id == doc_id,
                DocumentParseJob.task_id == Document.processing_task_id).options(
                defer(Document.parse_quality), defer(DocumentParseJob.upload_url),
                defer(DocumentParseJob.error_message))).all()
            if len(rows) != 1:
                raise MinerUError("DIAGNOSTIC_JOB", "无法唯一定位当前原Job，未查询云端")
            # 先验证身份，不能因身份阻塞而初始化HTTP或触碰共享限流。
            report = inspect_job(*rows[0])
            if query_existing:
                if not report["batch_id"] or rows[0][1].upload_complete is not True:
                    raise MinerUError("DIAGNOSTIC_CHECKPOINT", "原批次或已上传检查点缺失，未查询云端")
                provider = MinerUCloudProvider(settings)
                report = inspect_job(*rows[0], query=provider.query)
            return report
        finally:
            try:
                if provider is not None:
                    provider.close()
            finally:
                session.rollback()


def main(argv=None):
    """默认不访问云端；查询既有批次也不修改Job，所有异常只输出安全代码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", required=True, type=UUID)
    parser.add_argument("--query-existing", action="store_true", help="显式只GET一次原Batch，不提交新解析")
    args = parser.parse_args(argv)
    try:
        from app.core.business_logging import configure_logging
        from app.core.config import get_settings
        settings = get_settings()
        configure_logging(settings, role="mineru-diagnostic-cli")
        report = inspect_database(args.doc_id, settings, query_existing=args.query_existing)
    except MinerUError as error:
        report = {"status": "BLOCKED", "error_code": error.code,
                  "cloud_submitted": False, "database_modified": False, "business_recovered": False}
    except Exception:
        report = {"status": "BLOCKED", "error_code": "DIAGNOSTIC_IO_OR_DB",
                  "cloud_submitted": False, "database_modified": False, "business_recovered": False}
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 2 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
