"""用户手动执行的新PDF费用前置检查；只读Hash/缓存指针/SQL计数，不解析或调用云端。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def history_counts(session, file_hash):
    """全库检查同Hash原件和云历史，不选取Token、上传链接、正文或私有参数。"""
    from sqlalchemy import func, select
    from app.models import Document, DocumentParseJob
    documents = session.scalar(select(func.count()).select_from(Document).where(Document.file_hash == file_hash))
    jobs, batches, uncertain = session.execute(select(func.count(), func.count(func.distinct(DocumentParseJob.batch_id)),
        func.count().filter(DocumentParseJob.submission_attempted.is_(True) & DocumentParseJob.batch_id.is_(None)))
        .select_from(DocumentParseJob).where(DocumentParseJob.file_hash == file_hash)).one()
    return {"same_hash_documents": documents, "same_hash_jobs": jobs,
            "confirmed_batch_count": batches, "uncertain_submission_count": uncertain}


def inspect_candidate(path, settings, *, history=None):
    """只计算PDF字节身份并检查当前Whole缓存路径；已有任何历史/缓存残留均不算新样本。"""
    from app.services.parse_cache import ParseCache, key_for, recipe
    path = Path(path)
    limit = min(settings.max_upload_size_mb, 200) * 1024**2
    if not path.is_file() or path.is_symlink() or path.suffix.lower() != ".pdf" or not 0 < path.stat().st_size <= limit:
        raise ValueError("PREFLIGHT_FILE")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("PREFLIGHT_FILE")
        stream.seek(0)
        file_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    identity = recipe(SimpleNamespace(file_hash=file_hash, file_type="pdf"), settings, "mineru_whole")
    key = key_for(identity)
    # 不调用document()/job_document()，避免跨文档materialize和任何缓存写入。
    cache_present = ParseCache(settings).path(".cache", key).exists()
    counts = history(file_hash) if history is not None else None
    new_candidate = not cache_present and counts is not None and not any(counts.values())
    return {"file_hash": file_hash, "cache_key": key, "current_cache_path_present": cache_present,
        "history": counts, "status": "ELIGIBLE_SNAPSHOT" if new_candidate else "BLOCKED_OR_NOT_CHECKED",
        "submitted": False, "fee_authorized": False,
        "limits": "snapshot_only_pages_permissions_and_platform_budget_not_checked"}


def main(argv=None):
    """默认仅本地检查；--check-history显式开启只读SQL，始终不允许上传或新建Job。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--check-history", action="store_true")
    args = parser.parse_args(argv)
    try:
        from app.core.config import get_settings
        settings = get_settings()
        path = Path(args.file)
        path = path if path.is_absolute() else ROOT / path
        if args.check_history:
            from sqlalchemy import text
            from sqlalchemy.orm import Session
            from app.db.session import get_engine
            with Session(get_engine(), autoflush=False) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                result = inspect_candidate(path, settings, history=lambda value: history_counts(session, value))
        else:
            result = inspect_candidate(path, settings)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["status"] == "ELIGIBLE_SNAPSHOT" else 2
    except Exception:
        print("PREFLIGHT_BLOCKED：原件、配置、缓存路径或只读数据库检查未完成；未提交云任务", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
