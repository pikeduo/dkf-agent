"""显式将已成功Office Job的本地旧ZIP转换为新缓存，不访问云端或修改业务数据库。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.business_logging import configure_logging, log_event
from app.parsers.routing.contracts import RouteDecision
from app.parsers.routing.guard import guard_result
from app.schemas.parsed_document import ParsedDocument
from app.services.mineru.adapter import MinerUResultAdapter
from app.services.mineru.errors import MinerUError, internal_failure
from app.services.mineru.raw_results import validate_archive
from app.services.parse_cache import (NORMALIZER_VERSION, ParseCache, ReadyRecord,
                                      cloud_options, key_for, recipe)
from scripts.mineru_merge_diagnostic import file_hash, output_path


def require(check: str, valid: bool) -> None:
    """拒绝不匹配的固定身份条件，只暴露检查名称，不打印正文、路径或异常原文。"""
    if not valid:
        error = MinerUError("CACHE_UPGRADE_IDENTITY", "旧缓存身份无法核对，未发布或请求云端")
        error.diagnostic = {"stage": "cache.upgrade.identity", "failed_check": check}
        raise error


def verify_context(source, target, job, record, settings):
    """严格绑定原成功Job/缓存及同Hash目标；仅支持整份DOCX/PPTX，不套用Hybrid/分片。"""
    old = record.recipe
    require("TARGET_FAILED_IDLE", target.status == "FAILED"
            and (target.management_config or {}).get("state") != "running")
    require("SAME_SOURCE_FILE", source.doc_id != target.doc_id
            and source.file_hash == target.file_hash == job.file_hash
            and source.file_type == target.file_type and target.file_type in {"docx", "pptx"})
    require("SUCCESSFUL_SOURCE_JOB", job.doc_id == source.doc_id and job.provider == "mineru_cloud"
            and job.state == "done" and not job.is_active and job.upload_complete
            and job.submission_attempted and bool(job.batch_id) and bool(job.data_id))
    require("WHOLE_SOURCE_ONLY", not any(name in job.parse_options
            for name in ("_hybrid", "_xlsx_primary", "_pdf_split", "_pdf_split_request")))
    require("JOB_CACHE_BINDING", job.parse_options.get("_cache_recipe") == old
            and job.parse_options.get("_cache_key") == record.key == key_for(old))
    require("LEGACY_NORMALIZER", old.get("normalizer_version") in {
        "mineru-adapter-v2", "mineru-adapter-v3"})
    current = recipe(target, settings, "mineru_whole")
    # 只显式改变本地Normalizer/内容策略；云参数、覆盖范围及预检配置必须完全一致。
    expected_old = current | {"normalizer_version": old["normalizer_version"]}
    expected_old.pop("content_policy_version", None)
    require("FROZEN_RECIPE", old == expected_old)
    options = cloud_options(settings)
    require("CLOUD_OPTIONS", all(job.parse_options.get(name) == value for name, value in options.items())
            and job.model_version == options["model_version"]
            and job.options_hash == hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest())
    route = job.parse_options.get("_route_decision")
    guard = job.parse_options.get("_cache_guard")
    frozen = route or guard
    require("FROZEN_GUARD_PRESENT", isinstance(frozen, dict))
    decision = RouteDecision.model_validate(frozen)
    # 正常整份Job的两份快照相同；冲突时不能任意挑一份来发布新缓存。
    if route is not None and guard is not None:
        require("FROZEN_GUARD_AGREEMENT", RouteDecision.model_validate(route)
                == RouteDecision.model_validate(guard))
    require("FROZEN_GUARD_SCOPE", decision.file_type == target.file_type
            and decision.route_strategy == "mineru_whole" and old.get("coverage_scope") == "complete")
    return current, decision


def upgrade_cache(source, target, job, settings, *, execute=False, publish=False):
    """验证旧Job不可变缓存；显式execute才转换，publish才建立目标的新版本缓存指针。

    不修改原Job、原缓存或文档状态，不投递任务。用户须先停止相关API/Worker，避免
    文件发布与其他维护操作交错；后续入库仍由原文档级Reindex执行。
    """
    require("PUBLISH_REQUIRES_EXECUTE", not publish or execute)
    cache = ParseCache(settings)
    require("TARGET_CACHE_ABSENT", not cache.path(str(target.doc_id), "latest.json").exists())
    pointer = cache.path(".jobs", str(job.job_id) + ".json")
    require("JOB_CACHE_RECEIPT_PRESENT", pointer.is_file())
    record = ReadyRecord.model_validate_json(cache.read_bytes(pointer, 65536))
    current, decision = verify_context(source, target, job, record, settings)
    loaded = cache.load(record.recipe, record)
    require("JOB_CACHE_PRESENT", loaded is not None)
    _, files = loaded
    previous = ParsedDocument.model_validate_json(files["normalized_document.json"])
    require("CACHED_DOCUMENT_IDENTITY", previous.doc_id == source.doc_id
            and previous.file_type == target.file_type and bool(previous.blocks))
    require("SOURCE_ORIGINAL_HASH", file_hash_source(source) == source.file_hash)
    require("TARGET_ORIGINAL_HASH", file_hash_source(target) == target.file_hash)
    archive = files.get("raw/mineru_result.zip")
    require("RAW_ZIP_PRESENT", isinstance(archive, bytes) and bool(archive))
    validate_archive(archive, settings.mineru_result_max_mb * 1024**2)
    report = {"source_doc_id": str(source.doc_id), "doc_id": str(target.doc_id),
        "source_job_id": str(job.job_id), "batch_id": job.batch_id, "kb_id": str(target.kb_id),
        "status": "IDENTITY_VALIDATED", "error_code": None,
        "source_cache_key": record.key, "target_cache_key": key_for(current),
        "frozen_normalizer_version": record.recipe["normalizer_version"],
        "normalizer_version": NORMALIZER_VERSION, "zip_sha256": hashlib.sha256(archive).hexdigest(),
        "zip_batch_provenance": "validated_successful_job_cache",
        "database_modified": False, "publication_attempted": False,
        "cache_published": False, "cloud_submitted": False}
    log_event("cache.upgrade_validated", doc_id=target.doc_id, job_id=job.job_id, batch_id=job.batch_id)
    if not execute:
        return report
    parsed = MinerUResultAdapter().normalize(archive, doc_id=target.doc_id,
        file_name=target.file_name, file_type=target.file_type, geometry=None,
        identity=job.file_hash + ":" + job.options_hash)
    guard_result(parsed, decision)
    require("CONVERTED_TARGET_IDENTITY", (parsed.doc_id, parsed.file_name, parsed.file_type)
            == (target.doc_id, target.file_name, target.file_type) and parsed.quality is not None)
    # 转换前后复核两份原件，防止处理期间改动被发布到错误的内容身份。
    require("SOURCE_HASH_AFTER_CONVERSION", file_hash_source(source) == source.file_hash)
    require("TARGET_HASH_AFTER_CONVERSION", file_hash_source(target) == target.file_hash)
    report.update(status="ADAPTER_VALIDATED", block_count=len(parsed.blocks),
                  parse_quality=parsed.quality.model_dump(mode="json"))
    if publish:
        require("TARGET_CACHE_STILL_ABSENT", not cache.path(str(target.doc_id), "latest.json").exists())
        # 沿用原缓存发布器；旧key、原Job指针和原文档latest均保留，业务SQL始终只读。
        try:
            cache.save_document(target, current, parsed, files)
        except Exception as exc:
            # 全局ready与目标latest分步发布；中途失败可能已写部分文件，不能误报未写入。
            exc.cache_publication_attempted = True
            raise
        report.update(status="CACHE_PUBLISHED_NOT_RECOVERED", publication_attempted=True,
                      cache_published=True)
        log_event("cache.upgrade_published", doc_id=target.doc_id, job_id=job.job_id,
                  batch_id=job.batch_id, block_count=len(parsed.blocks), state=parsed.quality.content)
    return report


def file_hash_source(document):
    """复用已有安全路径/Hash检查，不解析原Office或访问外部依赖。"""
    from app.services.document_processing import source_path
    return file_hash(source_path(document))


def inspect_database(source_id, target_id, job_id, kb_id, settings, *, execute=False, publish=False):
    """只读快照核对原/目标文档及无活动云Job；显式回滚，不改状态或提交SQL。"""
    from sqlalchemy import func, select, text
    from sqlalchemy.orm import Session, defer
    from app.db.session import get_engine
    from app.models import Document, DocumentParseJob

    with Session(get_engine(), autoflush=False) as session:
        try:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            source, target = session.get(Document, source_id), session.get(Document, target_id)
            job = session.scalar(select(DocumentParseJob).where(DocumentParseJob.job_id == job_id)
                                 .options(defer(DocumentParseJob.upload_url), defer(DocumentParseJob.error_message)))
            require("DATABASE_ROWS_PRESENT", source is not None and target is not None and job is not None)
            require("TARGET_KNOWLEDGE_BASE", target.kb_id == kb_id)
            active = session.scalar(select(func.count()).select_from(DocumentParseJob).where(
                DocumentParseJob.file_hash == target.file_hash, DocumentParseJob.is_active.is_(True)))
            require("NO_ACTIVE_CLOUD_JOB", active == 0)
            return upgrade_cache(source, target, job, settings, execute=execute, publish=publish)
        finally:
            session.rollback()


def failure_report(exc):
    """保留受控身份条件/项目栈及发布不确定性，不输出异常原文或任意diagnostic字段。"""
    from app.services.parse_cache import CacheError
    detail = internal_failure(exc, "CACHE_UPGRADE_LOCAL_ERROR", "本地缓存升级失败").internal_diagnostic
    attempted = getattr(exc, "cache_publication_attempted", False) is True
    identity = isinstance(exc, MinerUError) and exc.code == "CACHE_UPGRADE_IDENTITY"
    return {"status": "BLOCKED",
        "error_code": exc.code if isinstance(exc, (CacheError, MinerUError)) else "CACHE_UPGRADE_LOCAL_ERROR",
        "error_type": detail["error_type"], "internal_diagnostic": detail,
        "identity_diagnostic": exc.diagnostic if identity else None,
        "database_modified": False, "publication_attempted": attempted,
        "cache_published": None if attempted else False, "cloud_submitted": False}


def main(argv=None):
    """默认只核验；显式执行/发布均无云访问，报告不覆盖，失败保留具体安全检查名。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-doc-id", required=True, type=UUID)
    parser.add_argument("--source-job-id", required=True, type=UUID)
    parser.add_argument("--target-doc-id", required=True, type=UUID)
    parser.add_argument("--kb-id", required=True, type=UUID)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true", help="仅执行本地Adapter转换，不发布缓存")
    parser.add_argument("--publish", action="store_true", help="同时显式发布目标新缓存，须配合--execute")
    args = parser.parse_args(argv)
    if args.publish and not args.execute:
        parser.error("--publish 必须同时指定 --execute")
    try:
        target_path = output_path(args.output)
    except (OSError, ValueError):
        print("CACHE_UPGRADE_OUTPUT：必须使用results下新的报告路径", file=sys.stderr)
        return 2
    try:
        from app.core.config import get_settings
        settings = get_settings()
        configure_logging(settings, role="mineru-cache-upgrade-cli")
        report = inspect_database(args.source_doc_id, args.target_doc_id, args.source_job_id,
            args.kb_id, settings, execute=args.execute, publish=args.publish)
    except Exception as exc:
        # 包括SQL/配置/磁盘错误也不输出异常原文、连接串、上游正文或本机路径。
        report = failure_report(exc)
        log_event("cache.upgrade_rejected", doc_id=args.target_doc_id, job_id=args.source_job_id,
                  error_code=report["error_code"], error_type=report["error_type"])
    try:
        with target_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
    except OSError:
        print("CACHE_UPGRADE_OUTPUT：报告写入失败；请核对目标缓存指针，不盲目重复执行", file=sys.stderr)
        return 2
    print(f"{report['status']}；数据库只读、无云请求；缓存发布={report['cache_published']}")
    return 2 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
