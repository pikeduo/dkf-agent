"""已有分片 Batch 的只读离线诊断：仅查询检查点/核对 Hash/转换已有 ZIP，不恢复或提交任务。"""

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mineru.adapter import MinerUResultAdapter, MergeEvidenceError, page_geometry
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import NORMALIZER_VERSION, ParseCache, canonical, key_for


def regular_path(path: Path) -> Path:
    """拒绝符号链接/junction和非普通文件；检查前不resolve以免隐藏重解析点。"""
    path = path if path.is_absolute() else ROOT / path
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise MinerUError("DIAGNOSTIC_PATH", "诊断路径包含不安全链接")
    if not stat.S_ISREG(path.stat().st_mode):
        raise MinerUError("DIAGNOSTIC_FILE", "诊断输入不是普通文件")
    return path


def file_hash(path: Path) -> str:
    """流式核对只读原件身份，不解析文档内容或改写原件。"""
    with regular_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_job(document, job, settings, batch_id: str, archive_path: Path | None) -> dict:
    """验证原文档/分片/Batch/冻结版本；只返回脱敏摘要，成功也不写缓存、SQL或投递任务。

    ZIP本身通常不包含可验证的Batch签名，离线通过不是导入授权或供应商身份认证。
    后续恢复必须另行确认下载来源并通过正常事务/Guard，禁止据此直接改SQL状态。
    """
    from app.services.document_processing import source_path
    from app.services.mineru.jobs import check_normalizer_recipe
    from app.services.mineru.pdf_split import checked_manifest, part_path, piece_recipe
    from app.services.mineru.pdf_split_jobs import current_part, validate_piece

    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if (job.provider != "mineru_cloud" or document.file_type != "pdf"
            or document.processing_task_id != job.task_id or part is None
            or part.batch_id != batch_id or job.batch_id != batch_id or job.data_id != part.data_id
            or not part.upload_complete or not job.upload_complete or not part.submission_attempted):
        raise MinerUError("DIAGNOSTIC_IDENTITY", "原文档、当前片或原Batch身份无法核对")
    check_normalizer_recipe(job)
    frozen = job.parse_options.get("_cache_recipe")
    if not isinstance(frozen, dict) or frozen.get("normalizer_version") != NORMALIZER_VERSION:
        raise MinerUError("CACHE_CONFIG_CHANGED", "缺少一致的冻结归一化版本，未恢复任务")
    option_names = ("model_version", "language", "is_ocr", "enable_table", "enable_formula")
    options = {name: job.parse_options.get(name) for name in option_names}
    if (any(name not in frozen or name not in job.parse_options or frozen[name] != options[name]
            for name in option_names) or frozen.get("file_hash") != document.file_hash
            or frozen.get("file_type") != "pdf" or frozen.get("provider") != job.provider
            or options["model_version"] != job.model_version
            # 分片Job的options_hash使用规范JSON并包含分片policy，不是普通Whole的Hash。
            or hashlib.sha256(canonical(options | {"pdf_split": manifest.policy})).hexdigest() != job.options_hash):
        raise MinerUError("DIAGNOSTIC_OPTIONS", "原解析参数、模型或缓存身份不一致，未恢复任务")
    cache = ParseCache(settings)
    path = regular_path(part_path(cache, document.doc_id, job.job_id, part.ordinal))
    if (file_hash(source_path(document)) != document.file_hash
            or path.stat().st_size != part.size_bytes or file_hash(path) != part.file_hash):
        raise MinerUError("PDF_SPLIT_CHANGED", "原件或分片Hash不一致，未恢复任务")
    identity = key_for(piece_recipe(job, part))
    report = {"doc_id": str(document.doc_id), "job_id": str(job.job_id), "batch_id": batch_id,
        "data_id": part.data_id, "document_status": document.status, "job_state": job.state,
        "part_state": part.state, "part_ordinal": part.ordinal,
        "original_page_start": part.page_start, "original_page_end": part.page_end,
        "original_sha256": document.file_hash, "part_sha256": part.file_hash,
        "part_size_bytes": part.size_bytes, "normalizer_version": NORMALIZER_VERSION,
        "frozen_options_verified": True,
        "adapter_identity": identity, "source_hashes_verified": True,
        "part_result_cached": part.result is not None,
        "unsubmitted_part_count": sum(not p.submission_attempted for p in manifest.parts),
        "status": "BLOCKED", "error_code": "ZIP_REQUIRED",
        "zip_batch_provenance": "manual_confirmation_required",
        "database_modified": False, "cache_published": False, "cloud_submitted": False}
    if archive_path is None:
        return report
    archive_path = regular_path(archive_path)
    if not 0 < archive_path.stat().st_size <= 512 * 1024**2:
        raise MinerUError("ZIP_LIMIT", "诊断ZIP超过安全大小限制")
    with archive_path.open("rb") as stream:
        archive = stream.read(512 * 1024**2 + 1)
    if len(archive) > 512 * 1024**2:
        raise MinerUError("ZIP_LIMIT", "诊断ZIP读取过程中超过安全大小限制")
    report.update(zip_sha256=hashlib.sha256(archive).hexdigest(), zip_size_bytes=len(archive))
    try:
        geometry = page_geometry(path, "pdf")
        if len(geometry) != part.page_end - part.page_start + 1:
            raise MinerUError("PDF_SPLIT_GEOMETRY", "分片真实页数与原账本不一致")
        parsed = MinerUResultAdapter().normalize(archive, doc_id=document.doc_id,
            file_name=document.file_name, file_type="pdf", geometry=geometry, identity=identity)
        validate_piece(document, job, part, parsed)
    except MinerUError as error:
        report.update(status="FAIL", error_code=error.code)
        if isinstance(error, MergeEvidenceError):
            detail = dict(error.diagnostic)
            if "page_idx" in detail:
                detail["original_page"] = part.page_start + detail["page_idx"]
            report["merge_diagnostic"] = detail
        return report
    report.update(status="ADAPTER_PASS_NOT_RECOVERED", error_code=None, block_count=len(parsed.blocks),
                  original_pages=sorted({part.page_start + block.page - 1 for block in parsed.blocks}))
    return report


def inspect_database(job_id: UUID, settings, batch_id: str, archive_path: Path | None) -> dict:
    """在有界只读事务中取当前检查点并退出；不调用带行锁/提交/恢复副作用的业务入口。"""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session, defer
    from app.db.session import get_engine
    from app.models import Document, DocumentParseJob

    with Session(get_engine(), autoflush=False) as session:
        try:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            row = session.execute(select(Document, DocumentParseJob).join(
                DocumentParseJob, DocumentParseJob.doc_id == Document.doc_id).where(
                DocumentParseJob.job_id == job_id).options(
                defer(DocumentParseJob.upload_url), defer(DocumentParseJob.error_message))).one_or_none()
            if row is None:
                raise MinerUError("DIAGNOSTIC_JOB", "未找到原解析Job")
            return inspect_job(*row, settings, batch_id, archive_path)
        finally:
            # 即使离线Adapter失败也显式回滚；无flush/commit/状态修改。
            session.rollback()


def output_path(value: str) -> Path:
    """仅在项目results下新建报告，拒绝既有报告和重解析点，不触及业务缓存目录。"""
    path = Path(value)
    path = path if path.is_absolute() else ROOT / path
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise ValueError("DIAGNOSTIC_PATH")
    path = path.resolve()
    if not path.is_relative_to(ROOT / "results") or path.exists():
        raise ValueError("DIAGNOSTIC_OUTPUT")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv=None) -> int:
    """用户显式读取原Job/已有ZIP，独占写新报告；无网络请求/SQL写入/云恢复选项。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, type=UUID)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--output", required=True, help="必须是results下尚不存在的JSON文件")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", args.batch_id):
            raise ValueError("DIAGNOSTIC_IDENTITY")
        target = output_path(args.output)
    except (ValueError, OSError):
        print("DIAGNOSTIC_INPUT：检查Batch编号/新报告路径；不覆盖已有报告", file=sys.stderr)
        return 2
    try:
        from app.core.config import get_settings
        from app.core.business_logging import configure_logging
        settings = get_settings()
        configure_logging(settings, role="mineru-diagnostic-cli")
        report = inspect_database(args.job_id, settings, args.batch_id, args.zip)
    except MinerUError as error:
        report = {"status": "BLOCKED", "error_code": error.code,
                  "cloud_submitted": False, "database_modified": False, "cache_published": False}
    except Exception:
        # 配置/驱动/文件异常可能带密码或路径；不打印异常原文或SQL。
        report = {"status": "BLOCKED", "error_code": "DIAGNOSTIC_IO_OR_DB",
                  "cloud_submitted": False, "database_modified": False, "cache_published": False}
    try:
        with target.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
    except OSError:
        print("DIAGNOSTIC_OUTPUT：报告未能独占写入，未覆盖已有文件", file=sys.stderr)
        return 2
    print(f"{report['status']}：{report['error_code'] or 'ADAPTER_VALIDATED'}；仅诊断，未恢复/提交任务")
    return 0 if report["status"] == "ADAPTER_PASS_NOT_RECOVERED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
