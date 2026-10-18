"""已有Whole/分片Batch只读离线诊断：核对Hash并转换原ZIP，不恢复或提交任务。"""

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mineru.adapter import (MinerUResultAdapter, MergeEvidenceError, EmptyBlockError,
    page_geometry, read_content_list, read_layout_pages, restore_merged_text)
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


def compare_pdf_coverage(document, job, settings, manifest):
    """只在诊断内存副本重检覆盖 v1→v2；不得改原 Job、Normalizer、缓存或收费身份。"""
    from pydantic import ValidationError
    from app.parsers.common import ParserError
    from app.parsers.routing.contracts import PDF_COVERAGE_VERSION, RouteDecision
    from app.parsers.routing.inspector import inspect_pdf
    from app.services.document_processing import source_path

    try:
        original = job.parse_options.get("_route_decision") or job.parse_options["_cache_guard"]
        frozen = RouteDecision.model_validate(original)
        if (frozen.file_type != "pdf" or frozen.route_strategy != "mineru_whole"
                or frozen.signals.pdf_coverage_version != "pdf-coverage-v1"
                or frozen.signals.page_count != manifest.page_count
                or [p.page for p in frozen.signals.pages] != list(range(1, manifest.page_count + 1))):
            raise ValueError("frozen_coverage")
        path = regular_path(source_path(document))
        if file_hash(path) != document.file_hash:
            raise MinerUError("PDF_SPLIT_CHANGED", "原件Hash不符，未比较覆盖规则")
        inspected = inspect_pdf(path, settings)
        if file_hash(path) != document.file_hash:
            raise MinerUError("PDF_SPLIT_CHANGED", "预检期间原件Hash变化，未比较覆盖规则")
        signals = inspected.signals
        if (inspected.uncertain or signals.pdf_coverage_version != PDF_COVERAGE_VERSION
                or signals.page_count != manifest.page_count
                or [p.page for p in signals.pages] != list(range(1, manifest.page_count + 1))):
            raise ValueError("new_coverage")
        changes = []
        raw_fields = ("page", "text_chars", "image_count", "image_area_ratio", "drawing_count", "annotation_count")
        for old, new in zip(frozen.signals.pages, signals.pages):
            # 新旧原始结构必须完全相同；只允许已证明的 margin_rule_only 解除旧绘图覆盖要求。
            if (any(getattr(old, name) != getattr(new, name) for name in raw_fields)
                    or old.required != bool(old.text_chars or old.image_count or old.drawing_count or old.annotation_count)):
                raise ValueError("raw_signals_changed")
            if old.required != new.required:
                if not old.required or new.required or new.coverage_reason != "margin_rule_only":
                    raise ValueError("unexplained_coverage_change")
                changes.append({"page": old.page, "frozen_required": True, "proposed_required": False,
                    "reason": new.coverage_reason, "drawing_count": old.drawing_count,
                    "decorative_drawing_count": new.decorative_drawing_count})
        proposed = frozen.model_copy(update={"signals": signals})
        options = dict(job.parse_options)
        for key in ("_route_decision", "_cache_guard"):
            if options.get(key) is not None:
                if RouteDecision.model_validate(options[key]) != frozen:
                    raise ValueError("conflicting_frozen_guards")
                options[key] = proposed.model_dump(mode="json")
        return SimpleNamespace(parse_options=options), {
            "coverage_upgrade_explicit": True, "frozen_pdf_coverage_version": frozen.signals.pdf_coverage_version,
            "proposed_pdf_coverage_version": signals.pdf_coverage_version,
            "frozen_guard_decision_sha256": hashlib.sha256(canonical(original)).hexdigest(),
            "proposed_guard_decision_sha256": hashlib.sha256(canonical(proposed.model_dump(mode="json"))).hexdigest(),
            "coverage_change_pages": changes, "normalizer_changed_by_coverage_comparison": False}
    except (KeyError, ValueError, ParserError, ValidationError):
        raise MinerUError("DIAGNOSTIC_COVERAGE", "覆盖版本或原始结构无法严格对应，未放宽冻结Guard") from None


def inspect_job(document, job, settings, batch_id: str, archive_path: Path | None,
                *, allow_normalizer_upgrade=False, allow_coverage_upgrade=False) -> dict:
    """验证原件/分片/Batch/版本并优先读本地归档；成功也不写缓存、SQL或投递任务。

    ZIP本身通常不包含可验证的Batch签名；覆盖升级也只比较内存副本，不是导入或恢复授权。
    后续恢复必须另行确认下载来源并通过正常事务/Guard，禁止据此直接改SQL状态。
    """
    from app.services.document_processing import source_path
    from app.services.mineru.jobs import check_normalizer_recipe
    from app.services.mineru.pdf_split import checked_manifest, part_path, piece_recipe
    from app.services.mineru.pdf_split_jobs import current_part, validate_piece

    if "_pdf_split" not in job.parse_options:
        if allow_normalizer_upgrade or allow_coverage_upgrade:
            raise MinerUError("DIAGNOSTIC_OPTIONS", "Whole诊断不自动改变冻结Normalizer或覆盖规则")
        return inspect_whole_job(document, job, settings, batch_id, archive_path)
    manifest = checked_manifest(document, job)
    part = current_part(manifest)
    if (job.provider != "mineru_cloud" or document.file_type != "pdf"
            or document.processing_task_id != job.task_id or part is None
            or part.batch_id != batch_id or job.batch_id != batch_id or job.data_id != part.data_id
            or not part.upload_complete or not job.upload_complete or not part.submission_attempted):
        raise MinerUError("DIAGNOSTIC_IDENTITY", "原文档、当前片或原Batch身份无法核对")
    frozen = job.parse_options.get("_cache_recipe")
    if not isinstance(frozen, dict):
        raise MinerUError("CACHE_CONFIG_CHANGED", "缺少一致的冻结归一化版本，未恢复任务")
    frozen_version = frozen.get("normalizer_version")
    upgrade = frozen_version != NORMALIZER_VERSION
    if upgrade and not (allow_normalizer_upgrade and frozen_version == "mineru-adapter-v2"
                        and NORMALIZER_VERSION == "mineru-adapter-v3"):
        raise MinerUError("CACHE_CONFIG_CHANGED", "旧Job版本不一致，须显式允许仅诊断的v2到v3升级")
    # 只在内存副本校验拟用版本；绝不修改原Job、原缓存键或冻结参数。
    check_normalizer_recipe(SimpleNamespace(parse_options=job.parse_options | {
        "_cache_recipe": frozen | {"normalizer_version": NORMALIZER_VERSION}}))
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
    old_recipe = piece_recipe(job, part)
    identity = key_for(old_recipe | {"normalizer_version": NORMALIZER_VERSION})
    report = {"doc_id": str(document.doc_id), "job_id": str(job.job_id), "batch_id": batch_id,
        "data_id": part.data_id, "document_status": document.status, "job_state": job.state,
        "part_state": part.state, "part_ordinal": part.ordinal,
        "original_page_start": part.page_start, "original_page_end": part.page_end,
        "original_sha256": document.file_hash, "part_sha256": part.file_hash,
        "part_size_bytes": part.size_bytes, "normalizer_version": NORMALIZER_VERSION,
        "frozen_normalizer_version": frozen_version, "normalizer_upgrade_explicit": upgrade,
        "frozen_adapter_identity": key_for(old_recipe),
        "coverage_upgrade_requested": allow_coverage_upgrade, "coverage_upgrade_explicit": False,
        "frozen_options_verified": True,
        "adapter_identity": identity, "source_hashes_verified": True,
        "part_result_cached": part.result is not None,
        "unsubmitted_part_count": sum(not p.submission_attempted for p in manifest.parts),
        "status": "BLOCKED", "error_code": "ZIP_REQUIRED",
        "zip_batch_provenance": "manual_confirmation_required",
        "database_modified": False, "cache_published": False, "cloud_submitted": False}
    from app.services.mineru.raw_results import load_raw_result
    saved = load_raw_result(settings, doc_id=document.doc_id, job_id=job.job_id, ordinal=part.ordinal,
        batch_id=part.batch_id, data_id=part.data_id, file_hash=part.file_hash)
    if saved is not None:
        archive, record = saved
        if archive_path is not None and file_hash(regular_path(archive_path)) != record["sha256"]:
            raise MinerUError("DIAGNOSTIC_IDENTITY", "指定ZIP与原Batch归档Hash不一致")
        report["zip_batch_provenance"] = "validated_local_receipt"
    elif archive_path is None:
        return report
    else:
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
        from io import BytesIO
        from zipfile import ZipFile
        from app.services.mineru.table_merge import restore_merged_tables
        items, restored_count = restore_merged_text(read_content_list(archive), read_layout_pages(archive))
        with ZipFile(BytesIO(archive)) as zipped:
            members = zipped.namelist()
        _, table_groups = restore_merged_tables(items, read_layout_pages(archive), archive_members=members)
        report.update(text_restored_count=restored_count, table_restoration=table_groups)
        parsed = MinerUResultAdapter().normalize(archive, doc_id=document.doc_id,
            file_name=document.file_name, file_type="pdf", geometry=geometry, identity=identity)
        report.update(adapter_normalize="PASS", block_count=len(parsed.blocks),
                      adapter_unexplained_empty_text_or_tables=0,
                      original_pages=sorted({part.page_start + block.page - 1 for block in parsed.blocks}))
        guard_error = None
        report["guard_evaluation_scope"] = "frozen"
        try:
            validate_piece(document, job, part, parsed)
        except MinerUError as error:
            guard_error = error
            report["completeness_guard"] = "FAIL"
            if error.code == "COMPLETENESS":
                from app.parsers.routing.contracts import RouteDecision
                decision = RouteDecision.model_validate(job.parse_options.get("_route_decision")
                                                       or job.parse_options["_cache_guard"])
                required = {p.page for p in decision.signals.pages
                            if p.required and part.page_start <= p.page <= part.page_end}
                report["missing_required_original_pages"] = sorted(required - set(report["original_pages"]))
                report["missing_required_page_signals"] = [{
                    "page": p.page, "text_chars": p.text_chars, "image_count": p.image_count,
                    "drawing_count": p.drawing_count, "annotation_count": p.annotation_count}
                    for p in decision.signals.pages if p.page in report["missing_required_original_pages"]]
            else:
                raise
        report["frozen_completeness_guard"] = "FAIL" if guard_error else "PASS"
        report["completeness_guard"] = report["frozen_completeness_guard"]
        report["frozen_missing_required_original_pages"] = report.get("missing_required_original_pages", [])
        if allow_coverage_upgrade:
            proposed_job, comparison = compare_pdf_coverage(document, job, settings, manifest)
            report.update(comparison, guard_evaluation_scope="proposed_coverage_only")
            # 使用未修改的生产分片 Guard；只替换副本的覆盖摘要，不改 Block/页码/ID/Normalizer。
            report["completeness_guard"] = "FAIL"
            validate_piece(document, proposed_job, part, parsed)
            report["missing_required_original_pages"] = []
            report["missing_required_page_signals"] = []
        elif guard_error is not None:
            raise guard_error
        report["completeness_guard"] = "PASS"
    except MinerUError as error:
        report.update(status="FAIL", error_code=error.code)
        if isinstance(error, MergeEvidenceError):
            detail = dict(error.diagnostic)
            if "page_idx" in detail:
                detail["original_page"] = part.page_start + detail["page_idx"]
            report["merge_diagnostic"] = detail
        elif isinstance(error, EmptyBlockError):
            report["empty_diagnostic"] = dict(error.diagnostic) | {
                "original_page": part.page_start + error.diagnostic["page"] - 1}
        return report
    report.update(status="ADAPTER_PASS_NOT_RECOVERED", error_code=None, block_count=len(parsed.blocks),
                  unexplained_empty_text_or_tables=0,
                  original_pages=sorted({part.page_start + block.page - 1 for block in parsed.blocks}))
    return report


def inspect_whole_job(document, job, settings, batch_id, archive_path):
    """核对Whole原ZIP/冻结身份及缺页信号；不请求云端、不放宽Guard、不写库或缓存。"""
    from app.parsers.routing.contracts import RouteDecision
    from app.parsers.routing.guard import CompletenessError, guard_result
    from app.services.document_processing import source_path
    from app.services.mineru.jobs import check_normalizer_recipe
    from app.services.mineru.raw_results import load_raw_result
    if (job.provider != "mineru_cloud" or document.file_type != "pdf"
            or document.processing_task_id != job.task_id or document.file_hash != job.file_hash
            or batch_id != job.batch_id or not job.upload_complete or not job.submission_attempted
            or "_hybrid" in job.parse_options or "_pdf_split_request" in job.parse_options):
        raise MinerUError("DIAGNOSTIC_IDENTITY", "当前Whole文档/批次身份无法核对，未转换")
    check_normalizer_recipe(job)
    frozen = job.parse_options.get("_cache_recipe", {})
    if (frozen.get("normalizer_version") != NORMALIZER_VERSION
            or frozen.get("file_hash") != document.file_hash or frozen.get("file_type") != "pdf"
            or frozen.get("provider") != job.provider):
        raise MinerUError("DIAGNOSTIC_OPTIONS", "Whole冻结解析身份无法核对")
    names = ("model_version", "language", "is_ocr", "enable_table", "enable_formula")
    options = {name: job.parse_options.get(name) for name in names}
    if (any(name not in frozen or frozen[name] != options[name] for name in names)
            or options["model_version"] != job.model_version
            or hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest() != job.options_hash):
        raise MinerUError("DIAGNOSTIC_OPTIONS", "Whole模型或参数Hash不匹配")
    path = regular_path(source_path(document))
    if file_hash(path) != document.file_hash:
        raise MinerUError("FILE_HASH", "原件Hash不符，未转换原ZIP")
    saved = load_raw_result(settings, doc_id=document.doc_id, job_id=job.job_id, ordinal=1,
        batch_id=job.batch_id, data_id=job.data_id, file_hash=document.file_hash)
    if saved is None:
        raise MinerUError("ZIP_REQUIRED", "原Batch归档不存在，未下载或重新提交")
    archive, record = saved
    if archive_path is not None and file_hash(regular_path(archive_path)) != record["sha256"]:
        raise MinerUError("DIAGNOSTIC_IDENTITY", "指定ZIP与原Batch归档Hash不一致")
    report = {"doc_id": str(document.doc_id), "job_id": str(job.job_id), "batch_id": batch_id,
        "data_id": job.data_id, "status": "FAIL", "job_state": job.state,
        "original_sha256": document.file_hash, "zip_sha256": record["sha256"],
        "zip_size_bytes": record["size_bytes"], "normalizer_version": NORMALIZER_VERSION,
        "source_hashes_verified": True, "zip_batch_provenance": "validated_local_receipt",
        "adapter_normalize": "NOT_EXECUTED", "completeness_guard": "NOT_EXECUTED",
        "database_modified": False, "cache_published": False, "cloud_submitted": False}
    try:
        report["adapter_normalize"] = "RUNNING"
        parsed = MinerUResultAdapter().normalize(archive, doc_id=document.doc_id,
            file_name=document.file_name, file_type="pdf", geometry=page_geometry(path, "pdf"),
            identity=job.file_hash + ":" + job.options_hash)
        report.update(adapter_normalize="PASS", block_count=len(parsed.blocks))
        decision = RouteDecision.model_validate(job.parse_options.get("_route_decision")
                                               or job.parse_options["_cache_guard"])
        guard_result(parsed, decision)
        if file_hash(path) != document.file_hash:
            raise MinerUError("FILE_HASH", "转换期间原件Hash变化，未接受结果")
        report.update(status="ADAPTER_PASS_NOT_RECOVERED", completeness_guard="PASS", error_code=None)
    except (MinerUError, CompletenessError) as error:
        if report["adapter_normalize"] == "RUNNING":
            report["adapter_normalize"] = "FAIL"
        report["error_code"] = getattr(error, "code", "COMPLETENESS")
        if isinstance(error, MergeEvidenceError):
            report["merge_diagnostic"] = dict(error.diagnostic)
        elif isinstance(error, EmptyBlockError):
            report["empty_diagnostic"] = dict(error.diagnostic)
        elif isinstance(error, CompletenessError):
            report["completeness_guard"] = "FAIL"
            covered = {block.page for block in parsed.blocks if block.text.strip()}
            required = {page.page for page in decision.signals.pages if page.required}
            report["missing_required_original_pages"] = sorted(required - covered)
            report["missing_required_page_signals"] = [{
                "page": page.page, "text_chars": page.text_chars, "image_count": page.image_count,
                "drawing_count": page.drawing_count, "annotation_count": page.annotation_count}
                for page in decision.signals.pages if page.page in required - covered]
    return report


def inspect_database(job_id: UUID, settings, batch_id: str, archive_path: Path | None,
                     *, allow_normalizer_upgrade=False, allow_coverage_upgrade=False) -> dict:
    """只读取得原检查点及显式比较许可；始终回滚，不调用带恢复/提交副作用的入口。"""
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
            return inspect_job(*row, settings, batch_id, archive_path,
                               allow_normalizer_upgrade=allow_normalizer_upgrade,
                               allow_coverage_upgrade=allow_coverage_upgrade)
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
    """独占写只读报告；版本/覆盖须分别显式许可，均不授予网络请求、SQL写入或恢复权限。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, type=UUID)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--output", required=True, help="必须是results下尚不存在的JSON文件")
    parser.add_argument("--allow-normalizer-upgrade", action="store_true",
                        help="显式仅在离线诊断副本允许冻结v2使用v3，不修改或恢复原Job")
    parser.add_argument("--allow-coverage-upgrade", action="store_true",
                        help="显式对Hash一致原件重新预检，在内存副本比较PDF覆盖v1到v2，不恢复Job")
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
        report = inspect_database(args.job_id, settings, args.batch_id, args.zip,
                                  allow_normalizer_upgrade=args.allow_normalizer_upgrade,
                                  allow_coverage_upgrade=args.allow_coverage_upgrade)
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
