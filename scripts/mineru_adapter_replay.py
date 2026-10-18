"""阶段33已有失败原ZIP的只读批量回放；不访问MinerU、不恢复任务、不发布缓存。"""

import argparse
import json
from pathlib import Path
import sys
from time import monotonic
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mineru.errors import MinerUError
from scripts.mineru_merge_diagnostic import inspect_database, output_path


def replay_records(records, settings, *, inspect=inspect_database, timeout=600):
    """遍历全部登记项；超时/无Job/失败保留逐项状态，绝不截掉难例或补收费任务。"""
    started, results = monotonic(), []
    for record in records:
        identity = {key: record.get(key) for key in ("file_id", "doc_id", "job_id", "batch_id")}
        report = identity | {"status": "BLOCKED", "error_code": record.get("error_code", "DIAGNOSTIC_JOB"),
                             "cloud_submitted": False, "database_modified": False, "cache_published": False}
        if monotonic() - started >= timeout:
            report.update(status="NOT_EXECUTED_BUDGET", error_code="LOCAL_TIME_BUDGET")
        elif record.get("job_id") and record.get("batch_id"):
            try:
                report.update(inspect(UUID(record["job_id"]), settings, record["batch_id"], None,
                                      allow_normalizer_upgrade=True))
            except MinerUError as error:
                report["error_code"] = error.code
                if error.code == "DIAGNOSTIC_IDENTITY" and getattr(error, "diagnostic", None):
                    report["identity_diagnostic"] = error.diagnostic
            except Exception:
                report["error_code"] = "DIAGNOSTIC_IO_OR_DB"
        results.append(report)
    return {"status": "OFFLINE_REPLAY_REPORT_NOT_RECOVERY", "registered_count": len(records),
        "complete_count": sum(r["status"] == "ADAPTER_PASS_NOT_RECOVERED" for r in results),
        "partial_count": sum(r["status"] == "ADAPTER_PARTIAL_NOT_RECOVERED" for r in results),
        "base_extracted_document_count": sum(r.get("adapter_normalize") == "PASS" for r in results),
        "enhanced_count": sum(r.get("enhancement_status") == "ENHANCED" for r in results),
        "degraded_count": sum(r.get("enhancement_status") == "DEGRADED" for r in results),
        "zip_validated_count": sum(r.get("zip_integrity") == "PASS" for r in results),
        "failed_or_blocked_count": sum(r["status"] not in {"ADAPTER_PASS_NOT_RECOVERED", "ADAPTER_PARTIAL_NOT_RECOVERED"} for r in results),
        "cloud_requests": 0, "database_modified": False, "cache_published": False,
        "duration_seconds": monotonic() - started, "documents": results}


def checkpoint_records(path, kb_id, settings):
    """只读原checkpoint与当前Job映射；保留所有历史MERGE失败项，包括已恢复/无当前Job项。"""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session, defer
    from app.db.session import get_engine
    from app.models import Document, DocumentParseJob
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("CHECKPOINT_SCHEMA")
    selected = [value | {"file_id": key} for key, value in raw.items()
                if isinstance(value, dict) and value.get("error_code") == "MERGE_EVIDENCE"]
    records = []
    with Session(get_engine(), autoflush=False) as session:
        try:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            for item in selected:
                record = {"file_id": item["file_id"], "doc_id": item.get("doc_id")}
                if not item.get("doc_id"):
                    records.append(record)
                    continue
                try:
                    doc_id = UUID(item["doc_id"])
                except (ValueError, TypeError):
                    records.append(record | {"error_code": "CHECKPOINT_IDENTITY"})
                    continue
                rows = session.execute(select(Document, DocumentParseJob).join(DocumentParseJob,
                    DocumentParseJob.doc_id == Document.doc_id).where(Document.doc_id == doc_id,
                        Document.kb_id == kb_id, Document.processing_task_id == DocumentParseJob.task_id)
                    .options(defer(Document.parse_quality), defer(DocumentParseJob.upload_url),
                             defer(DocumentParseJob.error_message)).limit(2)).all()
                if len(rows) == 1:
                    document, job = rows[0]
                    expected = item.get("file_hash") or item.get("sha256")
                    if expected != document.file_hash:
                        record["error_code"] = "CHECKPOINT_HASH"
                    else:
                        record.update(job_id=str(job.job_id), batch_id=job.batch_id)
                elif len(rows) > 1:
                    record["error_code"] = "AMBIGUOUS_JOB"
                records.append(record)
        finally:
            session.rollback()
    return records


def main(argv=None):
    """用户显式执行完整登记范围；报告独占新建，正文/签名URL/Token不进入输出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--kb-id", type=UUID, required=True)
    parser.add_argument("--output", required=True, help="results下全新报告JSON")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout <= 3600:
            raise ValueError
        target = output_path(args.output)
        from app.core.config import get_settings
        from app.core.business_logging import configure_logging
        settings = get_settings()
        configure_logging(settings, role="mineru-diagnostic-cli")
        checkpoint = args.checkpoint if args.checkpoint.is_absolute() else ROOT / args.checkpoint
        records = checkpoint_records(checkpoint, args.kb_id, settings)
        report = replay_records(records, settings, timeout=args.timeout)
        with target.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
        print(f"登记={report['registered_count']}；完整={report['complete_count']}；部分={report['partial_count']}；失败/阻塞={report['failed_or_blocked_count']}；云请求=0；未恢复")
        return 0 if records and not report["failed_or_blocked_count"] else 2
    except Exception:
        print("LOCAL_REPLAY_BLOCKED：检查新报告路径、checkpoint和只读数据库配置；未调用MinerU或恢复任务", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
