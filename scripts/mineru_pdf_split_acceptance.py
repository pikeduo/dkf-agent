"""只验收 PDF 分片→MinerU→原始 ZIP；默认只读，不连接业务数据库或调用 Adapter。"""

import argparse
import json
import math
import sys
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf

from app.core.business_logging import configure_logging, log_event
from app.core.config import get_settings
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import parse_options, result_failure
from app.services.mineru.pdf_split import TARGET_PAGES, file_digest, part_path, policy, prepare_manifest
from app.services.mineru.provider import LocalFile, MinerUCloudProvider, validate_local_file
from app.services.mineru.raw_results import obtain_raw_result
from app.services.parse_cache import CacheError, ParseCache, atomic_write, canonical


def inspect_source(path):
    """只读检查 PDF 页数与 Hash；默认预览不生成分片、不创建输出目录或云客户端。"""
    path = path if path.is_absolute() else ROOT / path
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise MinerUError("FILE_TYPE", "请指定已存在的本地 PDF")
    digest = file_digest(path)
    with pymupdf.open(path) as pdf:
        if not pdf.is_pdf or pdf.page_count < 1 or pdf.needs_pass or pdf.is_repaired:
            raise MinerUError("FILE_DAMAGED", "PDF 为空、加密或损坏，未提交云端")
        pages = pdf.page_count
    if file_digest(path) != digest:
        raise MinerUError("FILE_CHANGED", "检查期间原件已变化，未提交云端")
    return path, digest, pages


def write_report(directory, report):
    """仅更新本次独占新目录的进度报告，不含签名 URL、Token、正文或业务成功声明。"""
    atomic_write(directory / "report.json", canonical(report))


def acquire_piece(provider, source, row, report, directory, settings, *, timeout, poll_interval,
                  clock=monotonic, wait=sleep):
    """一个分片只申请一次 Batch；短 GET 之间有界等待，先保留原始 ZIP，完全不调用 Adapter。"""
    validate_local_file(source)
    row["submission_attempted"] = True
    row["upload_status"] = "requesting_batch"
    write_report(directory, report)  # POST前保存意图，响应不明确时绝不自动再POST。
    batch = provider.request_upload_urls([source], parse_options(settings))
    row["batch_id"], row["upload_status"] = batch.batch_id, "batch_created"
    write_report(directory, report)
    provider.upload(batch.urls[0], source)
    row["upload_status"] = "uploaded"
    write_report(directory, report)
    deadline = clock() + timeout
    while clock() < deadline:
        result = provider.query(batch.batch_id, source.data_id)
        row["parse_status"] = result["state"]
        write_report(directory, report)
        log_event("mineru.remote_state", doc_id=report["doc_id"], job_id=report["job_id"],
                  batch_id=batch.batch_id, file_index=row["ordinal"], state=result["state"])
        if result["state"] == "failed":
            raise result_failure(result)
        if result["state"] == "done":
            row["zip_status"] = "downloading"
            write_report(directory, report)
            _, receipt = obtain_raw_result(provider, result.get("full_zip_url"), settings,
                doc_id=report["doc_id"], job_id=report["job_id"], ordinal=row["ordinal"],
                batch_id=batch.batch_id, data_id=source.data_id, file_hash=source.file_hash)
            row.update(zip_status="validated", zip_path=str(ParseCache(settings).root / receipt["zip_path"]),
                       zip_sha256=receipt["sha256"], zip_size_bytes=receipt["size_bytes"],
                       content_items=receipt["content_items"])
            write_report(directory, report)
            return
        wait(min(poll_interval, max(0, deadline - clock())))
    # 本地停止观察不是云端任务失败；报告保留Batch，禁止靠重新运行创建另一收费任务。
    raise MinerUError("JOB_TIMEOUT", "本地等待已超时，云端结果未知；请按报告 Batch 核对，勿重复提交")


def execute(path, digest, pages, directory, settings, *, max_files, timeout, poll_interval,
            provider_factory=MinerUCloudProvider, clock=monotonic, wait=sleep):
    """在全新隔离目录验收原始结果；复用生产分片器/Provider，不读取或修改任何业务 Job。"""
    directory = directory if directory.is_absolute() else ROOT / directory
    # 必须是新目录；拒绝误重跑导致同一原件再次收费，不实现旧Job恢复或自动续费。
    directory.mkdir(parents=True, exist_ok=False)
    settings = settings.model_copy(update={"parsed_dir": directory / "artifacts"})
    document = SimpleNamespace(doc_id=uuid4(), file_hash=digest)
    job = SimpleNamespace(job_id=uuid4(), parse_options={
        "_pdf_split_request": {"policy": policy(), "page_count": pages}})
    report = {"scope": "cloud_zip_acceptance_only", "status": "RUNNING",
        "doc_id": str(document.doc_id), "job_id": str(job.job_id), "file_hash": digest,
        "page_count": pages, "parts": [], "adapter_status": "NOT_RUN",
        "business_database_modified": False, "business_parse_success": False}
    provider, row = None, None
    try:
        write_report(directory, report)
        manifest = prepare_manifest(document, job, settings, path)
        report["planned_submissions"] = len(manifest.parts)
        for part in manifest.parts:
            report["parts"].append({"ordinal": part.ordinal, "page_start": part.page_start,
                "page_end": part.page_end, "file_hash": part.file_hash, "size_bytes": part.size_bytes,
                "data_id": part.data_id, "batch_id": None, "submission_attempted": False,
                "upload_status": "not_started", "parse_status": "not_started", "zip_status": "not_downloaded"})
        write_report(directory, report)
        if len(manifest.parts) > max_files:
            raise MinerUError("SUBMISSION_BUDGET", "实际分片数超过显式文件预算，未提交云端")
        provider = provider_factory(settings)  # 所有本地分片/预算通过后才创建真实客户端。
        for part, row in zip(manifest.parts, report["parts"], strict=True):
            if file_digest(path) != digest:
                raise MinerUError("FILE_CHANGED", "原件Hash变化，未继续提交")
            source = LocalFile(part_path(ParseCache(settings), document.doc_id, job.job_id, part.ordinal),
                               f"part-{part.ordinal:04d}.pdf", part.data_id, part.file_hash)
            acquire_piece(provider, source, row, report, directory, settings,
                          timeout=timeout, poll_interval=poll_interval, clock=clock, wait=wait)
        report["status"] = "CLOUD_ZIP_PASS"
    except (MinerUError, CacheError, OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
        code = error.code if isinstance(error, MinerUError) else (
            "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "LOCAL_IO_OR_CONFIG")
        report.update(status="INCOMPLETE", error_code=code)
        if row is not None:
            row["error_code"] = code
            if row["zip_status"] == "downloading":
                row["zip_status"] = "download_or_validation_failed"
        # 不将本地超时/中断改写为远端failed，也不自动重试POST或另建Batch。
    finally:
        report["confirmed_submissions"] = sum(bool(p["batch_id"]) for p in report["parts"])
        report["submission_attempts"] = sum(p["submission_attempted"] for p in report["parts"])
        write_report(directory, report)
        if provider is not None:
            provider.close()
    return report


def main(argv=None):
    """默认按180页策略预览；双显式云许可和文件预算才执行，已有输出目录一律拒绝。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-cloud", action="store_true",
                        help="确认本原件外传MinerU及每个新分片可能收费；不是复用旧Batch")
    parser.add_argument("--max-files", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=7200, help="每片最多观察秒数")
    parser.add_argument("--poll-interval", type=int, default=15)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_files <= 128 or not 1 <= args.timeout <= 86400 or not 1 <= args.poll_interval <= 60:
            raise MinerUError("CONFIG", "文件预算/等待/轮询参数超出安全范围")
        if args.execute and not args.allow_cloud:
            raise MinerUError("CLOUD_NOT_ALLOWED", "真实执行须显式确认外传及新Batch费用")
        path, digest, pages = inspect_source(args.pdf)
        if not args.execute:
            print(json.dumps({"status": "DRY_RUN", "page_count": pages, "file_hash": digest,
                "minimum_submissions": max(1, math.ceil(pages / TARGET_PAGES)), "max_files": args.max_files,
                "note": "仅检查页数和Hash；按实际分片字节数最终确认数量。未生成分片/联网/操作数据库。"},
                ensure_ascii=False, indent=2))
            return 0
        settings = get_settings()
        configure_logging(settings, role="mineru-split-cli")
        report = execute(path, digest, pages, args.output, settings, max_files=args.max_files,
                         timeout=args.timeout, poll_interval=args.poll_interval)
        print(f"{report['status']}；已确认Batch数={report['confirmed_submissions']}；"
              f"报告：{args.output / 'report.json'}；Adapter未执行，业务状态未修改")
        return 0 if report["status"] == "CLOUD_ZIP_PASS" else 1
    except (MinerUError, CacheError, OSError, ValueError, RuntimeError) as error:
        # 不打印底层异常，避免私有路径、签名URL或上游响应泄漏。
        code = error.code if isinstance(error, MinerUError) else (
            "OUTPUT_EXISTS" if isinstance(error, FileExistsError) else "LOCAL_IO_OR_CONFIG")
        print(f"SPLIT_ACCEPTANCE_STOPPED / {code}：检查输入/参数及是否已有输出目录；未自动重试或覆盖")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
