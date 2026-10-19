"""只读数据库的Office本地分页预览；显式--prepare才转换/生成预览片，永不发云或恢复业务。"""

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.core.file_formats import OFFICE_TYPES
from app.services.mineru.errors import MinerUError
from app.services.mineru import office_pdf


def report_target(value):
    """报告仅写项目results内的新JSON，拒绝越界/链接/覆盖已有用户结果。"""
    path = value if value.is_absolute() else ROOT / value
    path = office_pdf.safe_path(path, ROOT / "results")
    if path.suffix.lower() != ".json" or path.exists():
        raise MinerUError("OFFICE_PDF_PATH", "请指定results中尚不存在的JSON报告路径")
    return path


def load_document(doc_id):
    """在只读事务复制必要元数据后立即回滚；转换期间不持有数据库事务/文档行锁。"""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session
    from types import SimpleNamespace
    from app.db.session import get_engine
    from app.models import Document

    with Session(get_engine(), autoflush=False) as session:
        try:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            row = session.execute(select(Document.doc_id, Document.file_hash, Document.file_type,
                Document.file_path, Document.file_name).where(Document.doc_id == doc_id)).one_or_none()
            if row is None or row.file_type not in OFFICE_TYPES:
                raise MinerUError("OFFICE_PDF_IDENTITY", "找不到指定Office原件，未转换或访问云端")
            return SimpleNamespace(**dict(row._mapping))
        finally:
            session.rollback()


def plan_document(document, settings, folder, *, prepare=False):
    """默认仅检查转换依赖；显式准备才发布独立转换缓存/预览分片，不修改任何原Job。"""
    request = office_pdf.conversion_request(document, settings)
    result = {"doc_id": str(document.doc_id), "original_sha256": document.file_hash,
        "status": "DRY_RUN", "target_pages": 180, "cloud_submitted": False,
        "database_modified": False, "business_cache_published": False, "business_recovered": False,
        "conversion_prepared": False}
    if not prepare:
        return result
    import pymupdf
    from app.services.mineru.pdf_split import build_parts, render_range

    path, receipt = office_pdf.ensure_conversion(document, request, settings)
    folder.mkdir(parents=True, exist_ok=False)
    with pymupdf.open(path) as pdf:
        def render(first, last, target):
            """只生成新预览目录中的分片；正式Job稍后仍按同一冻结转换PDF规划。"""
            render_range(pdf, first, last, target)

        parts = build_parts(pdf.page_count, folder, render)
    result.update(status="LOCAL_PLAN_READY", conversion_prepared=True, page_count=receipt["pdf_signals"]["page_count"],
        converted_pdf_sha256=receipt["pdf_sha256"], converted_pdf_path=str(path),
        page_semantics="converted_office", planned_max_new_files=len(parts), parts=parts)
    return result


def main(argv=None):
    """用户手动运行；不构造Provider/任务，不执行GPU；报告路径仅留本地而不进入业务日志。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", required=True, type=UUID)
    parser.add_argument("--prepare", action="store_true", help="显式允许本地LibreOffice转换和预览分片，不允许云端")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = report_target(args.output)
        settings = get_settings()
        configure_logging(settings, role="office-pdf-cli")
        document = load_document(args.doc_id)
        report = plan_document(document, settings, output.parent / f"office-preview-{uuid4()}", prepare=args.prepare)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"{report['status']}；文件={args.doc_id}；预览片数={report.get('planned_max_new_files', '-')}；未请求云端/恢复业务")
        return 0
    except MinerUError as error:
        print(f"BLOCKED：{error.code}；未请求云端/恢复业务")
    except Exception:
        print("BLOCKED：OFFICE_PDF_LOCAL_IO_OR_DB；未请求云端/恢复业务")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
