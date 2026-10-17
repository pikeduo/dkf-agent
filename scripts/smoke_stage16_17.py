"""用户执行的阶段 16/17 小样本验收；默认只说明操作，不下载或启动业务服务。"""

import argparse
import hashlib
import io
import json
import math
import os
import re
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_common as common
from scripts import evaluate_benchmark_parsing as quality
from scripts import prepare_ohr_bench_preset as ohr
from scripts import prepare_omnidocbench_preset as omni

TEST_ROOT = ROOT / "docs/test/stage16_smoke"
VERSION = "stage16-17-smoke-v1"
STAGES = {"CHUNKING": ("chunk", "chunk_task_id"),
          "EMBEDDING": ("embed", "embedding_task_id"),
          "INDEXING": ("index", "index_task_id")}
TASK_FIELDS = ("processing_task_id", "chunk_task_id", "embedding_task_id", "index_task_id")
# 只公开当前 MinerU 边界的固定代码；未知字符串不得当成错误码输出到报告。
MINERU_ERROR_CODES = frozenset({
    "ACTIVE_JOB", "BAD_RESPONSE", "BAD_URL", "BATCH_LIMIT", "BBOX", "CACHE_CONFIG_CHANGED", "CACHE_MISS",
    "COMPLETENESS", "CONFIG", "CONTENT_LIST", "CONTENT_TYPE", "DAILY_LIMIT", "DATA_ID",
    "DATABASE", "DATABASE_SCHEMA", "DOCUMENT_STATE", "DOWNLOAD_NETWORK", "EMPTY_BLOCK",
    "EMPTY_FILE", "EXTERNAL_ATTACHMENT_MISSING", "FILE_CHANGED", "FILE_DAMAGED", "FILE_FORMAT",
    "FILE_HASH", "FILE_IO", "FILE_MISSING", "FILE_SIZE", "FILE_STRUCTURE", "FILE_TYPE",
    "GEOMETRY", "HYBRID_CHECKPOINT", "JOB_TIMEOUT", "MERGE_EVIDENCE", "NETWORK", "NO_TEXT", "PAGE",
    "PAGE_LIMIT", "PREFLIGHT_LIMIT", "QUEUE", "QUOTA_UNAVAILABLE", "RATE_LIMIT",
    "REMOTE_FAILED", "ROUTE_INVALID", "ROUTED_REMOTE_FAILED", "SUBMIT_UNCERTAIN",
    "TOKEN_MISSING", "UNKNOWN", "UPLOAD_EXPIRED", "UPLOAD_NETWORK", "UPSTREAM_ERROR",
    "ZIP_INVALID", "ZIP_LIMIT", "ZIP_PATH", "ZIP_SIZE",
})
ERRORS = {
    "PERMISSION": "需要显式执行及本地使用/独立测试库/云外传确认；默认未执行",
    "INPUT": "参数、官方参考或样本身份无效；不改写输入",
    "EXISTS": "运行目录已有内容；准备请换 --run，后续步骤复用原 --run",
    "LOCK": "该运行目录正在使用或存在中断锁；确认旧进程已退出后人工处理锁",
    "PLAN": "计划、QA/GT 或样本摘要已改变；不能用修改后的材料继续同一运行",
    "TARGET": "测试库不存在、非 ACTIVE、是默认库或含非本计划文件，已停止写操作",
    "HTTP": "API 请求失败/响应不确定；不打印响应原文，不盲目重复提交",
    "UNCERTAIN": "存在提交意图但无持久化检查点；请核对数据库与日志，不自动补投",
    "DATABASE": "只读数据库检查失败；请核对连接、迁移、扩展及安全日志",
    "PRECHECK": "基础服务、Worker、本地模型文件或 MinerU 配置检查未通过",
    "LIMIT": "样本、导出数量/大小或等待预算超限",
    "RETRIEVAL": "Dense 检索未完成；请核对本地 GPU/模型身份和 Dense 安全日志",
    "RETRIEVAL_PREREQUISITE": "READY 或证据/向量前置校验未通过；Dense 未执行，未加载 Query 模型，请先查看文档失败阶段与错误码",
    "IO": "本地步骤未完成；已发布文件保留，不覆盖原件或已有报告",
}


class SmokeError(ValueError):
    """仅传递固定安全错误，不输出连接串、正文、API 响应或底层异常。"""

    def __init__(self, code):
        """固定错误类别供终端及独立报告追踪。"""
        self.code = code if code in ERRORS else "IO"
        super().__init__(f"SMOKE_{self.code}：{ERRORS[self.code]}")


def stamp():
    """生成 UTC 时间及随机后缀，用于独占发布报告而不覆盖历史结果。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]


def publish(path, value):
    """复用安全发布，只写独立冒烟目录；已有文件包括同内容均不覆盖。"""
    path = ohr.plain_path(path, beneath=TEST_ROOT)
    data = value if isinstance(value, bytes) else ohr.encoded(value) + b"\n"
    if len(data) > 96 * common.MIB:
        raise SmokeError("LIMIT")
    ohr.publish_exclusive(path, data)


@contextmanager
def run_lock(run):
    """仅锁本次运行的辅助文件；不锁数据库，不删除他人或中断进程的锁。"""
    run = ohr.plain_path(run, beneath=TEST_ROOT)
    run.mkdir(parents=True, exist_ok=True)
    lock = run / "run.lock"
    try:
        stream = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise SmokeError("LOCK") from None
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def pick_questions(selected, count, seed):
    """每份 PDF 至少保留一题，再平衡五类证据；原始 QA 完整保留，不合成问题。"""
    chosen, counts = [], Counter()
    for item in selected:
        rows = sorted(item["qas"], key=lambda row: (ohr.digest([seed, row["original"]["ID"]]), row["original"]["ID"]))
        if rows:
            chosen.append((item, rows[0]))
            counts[rows[0]["category"]] += 1
    remaining = [(item, row) for item in selected for row in item["qas"]
                 if row["original"]["ID"] not in {pair[1]["original"]["ID"] for pair in chosen}]
    while remaining and len(chosen) < count:
        pair = min(remaining, key=lambda pair: (counts[pair[1]["category"]],
                   ohr.digest([seed, pair[1]["original"]["ID"]]), pair[1]["original"]["ID"]))
        remaining.remove(pair)
        chosen.append(pair)
        counts[pair[1]["category"]] += 1
    return chosen


def prepare_samples(args, run):
    """离线准备 OHR/Omni/OCB；只验证本地容器及官方关联，不运行 Parser 或授权云外传。"""
    if not args.confirm_local_use:
        raise SmokeError("PERMISSION")
    if any(path.name != "run.lock" for path in run.iterdir()):
        raise SmokeError("EXISTS")
    buffers, documents, references = [], [], []
    source = ROOT / "docs/test/ohr_bench_preset/source"
    inventory = {}
    groups, rejected_qa, provenance = ohr.load_source(source / "qas_v2.json", source / "pdfs.zip", inventory=inventory)
    # 仅复用选择函数；正式 prepare 的 6～8 份限制和正式导入覆盖标准均不改变。
    limits = ohr.Limits(count=args.pdf_count, seed=args.seed, max_pages=args.max_pages,
                        max_total_pages=args.max_total_pages, qas_per_type=args.qa_count)
    with ZipFile(ohr.plain_path(source / "pdfs.zip")) as archive:
        pdfs, directory_sha = ohr.archive_index(archive)
        pool, missing = ohr.candidates(groups, pdfs, limits, ohr.DOMAINS)
        selected, failures, inspection = ohr.select_documents(pool, archive, limits)
        for item in selected:
            relative = "samples/ohr/" + item["doc_name"] + ".pdf"
            data = ohr.read_member(archive, item["zip_member"], limits.max_file_bytes)
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise SmokeError("PLAN")
            buffers.append((relative, data))
            documents.append({"file": relative, "file_type": "pdf", "source": "ohr",
                "identity": item["doc_name"], "sha256": item["sha256"], "size_bytes": len(data),
                "page_count": item["page_count"], "zip_member": item["zip_member"]})
        for item, row in pick_questions(selected, args.qa_count, args.seed):
            references.append(row["original"] | {"_ohr_reference": {
                "source_qa_sha256": provenance["qa_sha256"], "document_sha256": item["sha256"],
                "zip_member": item["zip_member"], "preset_file": "samples/ohr/" + item["doc_name"] + ".pdf",
                "evidence_page_idx_0based": list(row["pages"]),
                "evidence_pages_1based": [page + 1 for page in row["pages"]], "category": row["category"]}})
    omni_refs, omni_rejected = [], Counter()
    if args.image_count:
        source = ROOT / "docs/test/omnidocbench_preset/source"
        gt_source = common.source_record(source, "omnidocbench", ["OmniDocBench.json"])
        rows = common.json_file(source / "OmniDocBench.json", source)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise SmokeError("INPUT")
        ids = Counter(str(row.get("page_info", {}).get("image_path", "")).casefold()
                      for row in rows if isinstance(row, dict) and isinstance(row.get("page_info"), dict))
        candidates, hashes = [], {item["sha256"] for item in documents}
        inspected = 0
        for row in sorted(rows, key=lambda row: str(row.get("page_info", {}).get("image_path", "")) if isinstance(row, dict) else ""):
            try:
                name, labels = omni.page_record(row)
                if ids[name.casefold()] != 1:
                    raise SmokeError("INPUT")
                relative = name if name.startswith("images/") else "images/" + name
                path = ohr.plain_path(source / relative, beneath=source)
                if not path.is_file():
                    omni_rejected["not_downloaded"] += 1
                    continue
                if inspected >= 64:
                    omni_rejected["inspection_limit"] += 1
                    continue
                inspected += 1
                asset = common.asset(source, relative, "omnidocbench", 20 * common.MIB)
                info = row["page_info"]
                if (asset["width"] != info["width"] or asset["height"] != info["height"]
                        or asset["sha256"] in hashes):
                    raise SmokeError("INPUT")
                hashes.add(asset["sha256"])
                candidates.append({"identity": name, "labels": sorted(labels), "asset": asset, "original": row})
            except common.BenchmarkError as exc:
                if exc.code == "DEPENDENCY":
                    raise
                omni_rejected["invalid_image"] += 1
            except (SmokeError, ohr.PreparationError, KeyError, TypeError, ValueError, OSError):
                omni_rejected["invalid_annotation_or_image"] += 1
        for item in omni.choose(candidates, args.image_count, args.seed):
            asset = item["asset"] | {"project_page": 1, "gt_page_no": item["original"]["page_info"]["page_no"],
                "original_pdf_identity": None, "original_pdf_page": None, "source_identity": item["identity"]}
            relative = "samples/omni/" + asset["source_file"]
            data = common.read_bytes(source / asset["source_file"], 20 * common.MIB, source)
            if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise SmokeError("PLAN")
            buffers.append((relative, data))
            documents.append({"file": relative, "file_type": asset["file_type"], "source": "omni",
                "identity": item["identity"], "sha256": asset["sha256"], "size_bytes": len(data),
                "width": asset["width"], "height": asset["height"]})
            omni_refs.append({"reference_id": item["identity"], "original": item["original"], "document": asset,
                "source": gt_source, "labels": item["labels"], "coordinate_space": "image_pixels",
                "source_gt_page_no": asset["gt_page_no"], "project_document_page": 1})
    from scripts import stage16_ocb
    ocb_root, ocb_assets, ocb_refs, ocb_source, ocb_gaps, ocb_available = stage16_ocb.prepare(ROOT,
        per_format=args.ocb_per_format, qa_per_file=args.ocb_qa_per_file, seed=args.seed,
        existing_hashes={item["sha256"] for item in documents})
    print(f"OCB 离线关联：本地可用QA={ocb_source['locally_available_qa_count']}；"
          f"入选原件={len(ocb_assets)}；入选QA={len(ocb_refs)}；未授予云外传。")
    for name, asset in sorted(ocb_assets.items()):
        relative = "samples/ocb/" + asset["source_file"]
        data = common.read_bytes(ocb_root / asset["source_file"], 20 * common.MIB, ocb_root)
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise SmokeError("PLAN")
        buffers.append((relative, data))
        documents.append({"file": relative, "file_type": asset["file_type"], "source": "ocb",
            "identity": name, "sha256": asset["sha256"], "size_bytes": len(data),
            "dataset_revision": ocb_source["revision"], "source_file": asset["source_file"],
            "source_kind": asset["source_kind"], "checksum_method": asset["checksum_method"],
            "cloud_permission": "not_granted_by_download"})
    for index, path in enumerate(args.extra):
        path = ohr.plain_path(path)
        if path.suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
            raise SmokeError("INPUT")
        asset = common.asset(path.parent, path.name, "user", 20 * common.MIB)
        if asset["sha256"] in {item["sha256"] for item in documents}:
            raise SmokeError("INPUT")
        relative = f"samples/user/{index + 1}/" + path.name
        data = common.read_bytes(path, 20 * common.MIB)
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise SmokeError("PLAN")
        buffers.append((relative, data))
        documents.append({"file": relative, "file_type": asset["file_type"], "source": "user",
            "identity": path.name, "sha256": asset["sha256"], "size_bytes": len(data), "content_tags": "manual_pending"})
    qa_bytes = b"".join(ohr.encoded(row) + b"\n" for row in references)
    gt_bytes = b"".join(ohr.encoded(row) + b"\n" for row in omni_refs)
    ocb_bytes = b"".join(ohr.encoded(row) + b"\n" for row in ocb_refs)
    plan = {"version": VERSION, "parameters": {"seed": args.seed, "pdf_count": args.pdf_count,
        "image_count": args.image_count, "qa_count": args.qa_count, "max_pages": args.max_pages,
        "max_total_pages": args.max_total_pages, "ocb_per_format": args.ocb_per_format,
        "ocb_qa_per_file": args.ocb_qa_per_file}, "documents": documents,
        "official_qa_count": len(references), "omni_gt_count": len(omni_refs),
        "ocb_qa_count": len(ocb_refs), "total_official_qa_count": len(references) + len(ocb_refs),
        "ocb_qa_sha256": hashlib.sha256(ocb_bytes).hexdigest(), "ocb_source": ocb_source,
        "ocb_selection": ocb_gaps, "datasets_requested": ["ohr", "omni", "ocb"],
        "qa_sha256": hashlib.sha256(qa_bytes).hexdigest(), "gt_sha256": hashlib.sha256(gt_bytes).hexdigest(),
        "ohr_source": provenance | {"zip_central_directory_sha256": directory_sha},
        "source_qa_inventory": inventory, "excluded_qa": rejected_qa,
        "preparation_gaps": {"pdf_shortfall": max(0, args.pdf_count - len(selected)),
            "qa_shortfall": max(0, args.qa_count - len(references)),
            "image_shortfall": max(0, args.image_count - len(omni_refs)),
            "ocb_format_shortfall": sum(ocb_gaps["format_shortfall"].values()),
            "ocb_files_without_qa": sum(not any(name in r["reference_files"] for r in ocb_refs) for name in ocb_assets),
            "missing_evidence_categories": sorted(ohr.EVIDENCE - {row["_ohr_reference"]["category"] for row in references})},
        "rejected_pdfs": missing + failures, "inspection": inspection,
        "omni_exclusions": dict(omni_rejected), "local_use_confirmed_by_user": True,
        "cloud_permission": "not_granted_by_preparation", "official_quality_evaluated": False}
    if sum(len(data) for _, data in buffers) > 256 * common.MIB:
        raise SmokeError("LIMIT")
    plan["plan_sha256"] = ohr.digest(plan)
    # 计划最后发布：部分磁盘故障不会把不完整素材集合冒充可执行计划。
    for relative, data in buffers:
        publish(run / relative, data)
    publish(run / "official_qa.jsonl", qa_bytes)
    publish(run / "omni_gt.jsonl", gt_bytes)
    publish(run / "ocb_qa.jsonl", ocb_bytes)
    # 缺 bulk QA 导出时，新 run 已生成官方关联副本；不覆盖下载报告或原有 QA 文件。
    for name, rows in (("qa_full.json", ocb_available), ("qa_smoke.json", ocb_refs)):
        if ocb_source["qa_exports"][name].startswith("missing_"):
            publish(run / "ocb" / name, [{"qa_id": row["qa_id"],
                "source_row_index": row["source_row_index"], "original_qa": row["original"],
                **{key: row[key] for key in ("question", "expected_assertions", "reference_files", "track")}}
                for row in rows])
    publish(run / "ocb_source_summary.json", ocb_source)
    publish(run / "plan.json", plan)
    return plan


def load_plan(run):
    """核对三数据集及冻结 QA/GT；旧双数据集计划保持只读兼容，不回填或改写。"""
    plan = common.json_file(ohr.plain_path(run / "plan.json", beneath=TEST_ROOT))
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("version") != VERSION or ohr.digest(payload) != plan.get("plan_sha256"):
        raise SmokeError("PLAN")
    documents = plan.get("documents")
    if not isinstance(documents, list) or not 0 < len(documents) <= 20:
        raise SmokeError("PLAN")
    seen, hashes = set(), set()
    for item in documents:
        name = ohr.safe_name(item["file"]).as_posix()
        if not name.startswith("samples/") or name.casefold() in seen or item["sha256"] in hashes:
            raise SmokeError("PLAN")
        sample_bytes(run, item)
        seen.add(name.casefold())
        hashes.add(item["sha256"])
    reference_files = [("official_qa.jsonl", "qa_sha256"), ("omni_gt.jsonl", "gt_sha256")]
    if "ocb_qa_sha256" in plan:
        reference_files.append(("ocb_qa.jsonl", "ocb_qa_sha256"))
    for name, key in reference_files:
        path = ohr.plain_path(run / name, beneath=run)
        data = common.read_bytes(path, 96 * common.MIB, run) if path.stat().st_size else b""
        if hashlib.sha256(data).hexdigest() != plan[key]:
            raise SmokeError("PLAN")
    return plan


def sample_bytes(run, item):
    """限定目录/大小并冻结单文件上传字节，防止检查后文件被改动而额外提交错误原件。"""
    data = common.read_bytes(run / item["file"], 20 * common.MIB, run)
    if len(data) != item["size_bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise SmokeError("PLAN")
    return data


def references_file(path):
    """允许零参考的显式缺口报告；不把空 GT/QA 伪造为有效参考。"""
    if path.stat().st_size == 0:
        return []
    return common.rows_file(path)


def save_artifact(run, kind, payload):
    """每次执行发布新快照，保留旧报告；正文/GT 只进被忽略的受控本地目录。"""
    target = run / "reports" / f"{stamp()}-{kind}.json"
    publish(target, payload)
    return target


def latest_artifact(run, kind):
    """只消费同一运行目录的最新独立快照，不跨运行拼接测试结果。"""
    paths = sorted((run / "reports").glob(f"*-{kind}.json"))
    return common.json_file(ohr.plain_path(paths[-1], beneath=run)) if paths else None


def journal(run, operation, sha, phase, **summary):
    """API 调用前保存意图、确认后保存摘要；不保存上传正文、URL 或响应原文。"""
    publish(run / "events" / f"{stamp()}.json", {"operation": operation, "sha256": sha, "phase": phase, **summary})


def attempted(run, operation, sha):
    """有任何该文件提交意图即禁止自动重复 POST，响应丢失时只读恢复。"""
    for path in sorted((run / "events").glob("*.json")):
        row = common.json_file(ohr.plain_path(path, beneath=run))
        if row.get("operation") == operation and row.get("sha256") == sha:
            return True
    return False


def bind_target(run, plan, kb_id):
    """固定运行与测试库关系；禁止同一运行被重新绑定造成重复上传或跨库报告。"""
    path = run / "execution.json"
    value = {"plan_sha256": plan["plan_sha256"], "kb_id": str(kb_id)}
    if path.exists():
        if common.json_file(path) != value:
            raise SmokeError("TARGET")
    else:
        publish(path, value)


def checked_api_url(value):
    """只允许本机 API；支持 root_path 前缀，拒绝凭据、查询串与远程测试库上传。"""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise SmokeError("INPUT")
    return value.rstrip("/")


def api_request(client, method, path, **kwargs):
    """一次 HTTP 请求，不自动重试 POST、不跟随重定向，错误只暴露状态码。"""
    try:
        response = client.request(method, path.lstrip("/"), **kwargs)
        if response.status_code not in {200, 201, 202, 409}:
            print(f"API 操作未确认：HTTP {response.status_code}；请核对安全日志。")
            raise SmokeError("HTTP")
        body = response.json()
        if not isinstance(body, dict):
            raise SmokeError("HTTP")
        return response.status_code, body
    except SmokeError:
        raise
    except Exception:
        raise SmokeError("HTTP") from None


@contextmanager
def readonly_session():
    """只在用户执行真实验证时连接现有 PostgreSQL；所有 SQL 均在只读一致快照中。"""
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db.session import get_engine

    try:
        with Session(get_engine(), autoflush=False, expire_on_commit=False) as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            yield session
    except SmokeError:
        raise
    except Exception:
        raise SmokeError("DATABASE") from None


def read_target(kb_id, plan, *, exclusive=False):
    """只读小测试库状态、当前错误和编排标识；不把旧Job错误当新失败，不写业务状态。"""
    from sqlalchemy import select
    from app.db.seed import DEFAULT_KB_ID
    from app.models import Document, DocumentParseJob, KnowledgeBase

    with readonly_session() as session:
        kb = session.get(KnowledgeBase, kb_id)
        if kb is None or kb.status != "ACTIVE" or kb_id == DEFAULT_KB_ID:
            raise SmokeError("TARGET")
        rows = list(session.scalars(select(Document).where(Document.kb_id == kb_id).limit(101)))
        hashes = {item["sha256"] for item in plan["documents"]}
        if len(rows) > 100 or (exclusive and any(row.file_hash not in hashes for row in rows)):
            raise SmokeError("TARGET")
        # 一次限定库/Hash/当前解析代次查询，避免逐文档额外查询或混入历史失败。
        current_jobs = session.execute(select(DocumentParseJob.doc_id, DocumentParseJob.task_id,
            DocumentParseJob.error_code).join(Document, Document.doc_id == DocumentParseJob.doc_id)
            .where(Document.kb_id == kb_id, Document.file_hash.in_(hashes),
                   DocumentParseJob.task_id == Document.processing_task_id)).all()
        jobs_by_doc = {}
        for job in current_jobs:
            jobs_by_doc.setdefault(job.doc_id, []).append(job)
        result = {}
        for row in rows:
            if row.file_hash not in hashes:
                continue
            if row.file_hash in result:
                raise SmokeError("TARGET")
            result[row.file_hash] = {"doc_id": str(row.doc_id), "kb_id": str(row.kb_id), "file_type": row.file_type,
                "status": row.status, "error_code": document_error_code(row, jobs_by_doc.get(row.doc_id, [])),
                "management_task_id": str(row.management_task_id) if row.management_task_id else None,
                **{field: str(getattr(row, field)) if getattr(row, field) else None for field in TASK_FIELDS}}
        return result


def safe_code(message):
    """仅从 Document 安全文案提取应用代码；空值无错误，不猜供应商中文消息的错误码。"""
    if message is None or (isinstance(message, str) and not message.strip()):
        return None
    if not isinstance(message, str):
        return "DOCUMENT_FAILED"
    match = re.match(r"^((?:CHUNK|EMBEDDING|INDEX|MINERU|PARSER|ROUTE|CACHE)_[A-Z_]{1,64})[：:]", message or "")
    return match[1] if match else ("DOCUMENT_FAILED" if message else None)


def safe_job_code(code):
    """校验 Job/分片安全代码，不拼接冒号；未知值不透传，也不加载Provider/模型。"""
    from app.services.mineru.errors import PDF_SPLIT_ERROR_CODES
    if code is None or (isinstance(code, str) and not code.strip()):
        return None
    if not isinstance(code, str):
        return "UNKNOWN_ERROR_CODE"
    value = code.strip()
    if value in MINERU_ERROR_CODES | PDF_SPLIT_ERROR_CODES or re.fullmatch(r"HTTP_[1-5][0-9]{2}", value):
        return value
    # 复用供应商的纯错误映射，不导入 Provider/Parser，不发请求或加载配置/模型。
    from app.services.mineru.errors import api_error

    if api_error(value).code == value:
        return value
    return "UNKNOWN_ERROR_CODE"


def document_error_code(document, jobs):
    """优先使用 Document 的阶段代码，其次绑定当前解析代次的 Job 代码；历史失败不传播。"""
    code = safe_code(document.error_message)
    if code is not None and code != "DOCUMENT_FAILED":
        return code
    current_codes = {safe_job_code(job.error_code) for job in jobs
                     if job.task_id == document.processing_task_id and job.error_code}
    current_codes.discard(None)
    return next(iter(current_codes)) if len(current_codes) == 1 else code


def precheck(args, run, plan, client):
    """提交前检查已有基础设施和部署配置，不迁移、启动服务、加载模型或探测云 API。"""
    from sqlalchemy import text
    from app.core.config import get_settings
    from app.services.indexing import require_vector_index
    from app.services.embedding.service import model_files

    settings = get_settings()
    checks = {}
    read_target(args.kb_id, plan, exclusive=True)
    with readonly_session() as session:
        checks["postgresql"] = session.scalar(text("SELECT 1")) == 1
        checks["vector_extension"] = bool(session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")))
        require_vector_index(session)
        checks["migration"] = session.scalar(text("SELECT version_num FROM alembic_version")) == "0007_pgvector_index"
    code, health = api_request(client, "GET", "/health")
    checks["api"] = code == 200 and health.get("status") == "ok" and health.get("service") == "knowledge-service"
    code, kb = api_request(client, "GET", f"/api/admin/knowledge-bases/{args.kb_id}")
    checks["api_kb"] = code == 200 and kb.get("kb_id") == str(args.kb_id) and kb.get("status") == "ACTIVE"
    from redis import Redis
    for name, url in (("redis", settings.redis_url), ("broker", settings.celery_broker_url or settings.redis_url),
                      ("result_backend", settings.celery_result_backend or settings.redis_url)):
        try:
            with Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3) as redis:
                checks[name] = redis.ping() is True
        except Exception:
            checks[name] = False
    try:
        from app.core.celery_app import celery_app
        inspect = celery_app.control.inspect(timeout=3)
        queues, stats = inspect.active_queues() or {}, inspect.stats() or {}
        checks["cpu_worker"] = any(any(queue.get("name") == "default_queue" for queue in items)
                                   and worker in stats for worker, items in queues.items())
        checks["gpu_worker"] = any(any(queue.get("name") == "gpu_queue" for queue in items)
            and stats.get(worker, {}).get("pool", {}).get("max-concurrency") == 1 for worker, items in queues.items())
    except Exception:
        checks["cpu_worker"] = checks["gpu_worker"] = False
    model = settings.embedding_model_dir
    model = model if model.is_absolute() else ROOT / model
    try:
        # 复用正式服务的本地配置/Tokenizer/权重目录检查；不加载或 Hash 整份权重。
        checks["local_model_structure"] = bool(model_files(model))
    except Exception:
        checks["local_model_structure"] = False
    try:
        import torch
        checks["cuda"] = (settings.embedding_device.startswith("cuda:") and torch.cuda.is_available()
                          and int(settings.embedding_device.split(":")[1]) < torch.cuda.device_count())
    except Exception:
        checks["cuda"] = False
    checks["mineru_token_configured"] = bool(settings.mineru_api_token.get_secret_value())
    checks["upload_size"] = all(item["size_bytes"] <= settings.max_upload_size_mb * common.MIB for item in plan["documents"])
    result = {"checks": checks, "status": "PASS" if all(checks.values()) else "BLOCKED",
        "not_proved": ["API/Worker 与 CLI 的全部配置一致", "Worker 已成功加载 BGE-M3", "MinerU Token 真实有效", "云外传版权", "平台剩余额度"]}
    save_artifact(run, "precheck", result)
    if not all(checks.values()):
        print("部署检查未通过：" + ", ".join(name for name, ok in checks.items() if not ok))
        raise SmokeError("PRECHECK")


def match_document(item, row, kb_id):
    """要求 API/SQL 文档与样本类型、知识库和稳定 UUID 相符，不根据文件名猜身份。"""
    if row.get("kb_id") != str(kb_id) or row.get("file_type") != item["file_type"]:
        raise SmokeError("TARGET")
    try:
        UUID(row["doc_id"])
    except (ValueError, KeyError, TypeError):
        raise SmokeError("TARGET") from None


def drive_pipeline(args, run, plan, client, *, reader=read_target, clock=monotonic, pause=sleep):
    """原API上传后只观察增量编排；历史无编排文档沿用逐阶段POST，已有任务不补投。"""
    deadline = clock() + args.timeout
    for item in plan["documents"]:
        if clock() >= deadline:
            return {"status": "BLOCKED", "reason": "TIMEOUT"}
        rows = reader(args.kb_id, plan, exclusive=True)
        row = rows.get(item["sha256"])
        if row is not None:
            match_document(item, row, args.kb_id)
            continue
        if attempted(run, "upload", item["sha256"]):
            raise SmokeError("UNCERTAIN")
        data = sample_bytes(run, item)
        journal(run, "upload", item["sha256"], "intent")
        with io.BytesIO(data) as stream:
            code, body = api_request(client, "POST", f"/api/admin/knowledge-bases/{args.kb_id}/documents",
                                    files={"file": (Path(item["file"]).name, stream)}, data={"parse_mode": "auto"})
        # 409 只能从同库同 Hash 的 SQL 记录恢复，不自动再次上传或调用 /process。
        if code == 409:
            row = reader(args.kb_id, plan, exclusive=True).get(item["sha256"])
            if row is None:
                raise SmokeError("HTTP")
        else:
            if body.get("file_hash") != item["sha256"]:
                raise SmokeError("TARGET")
            row = body
        match_document(item, row, args.kb_id)
        persisted = reader(args.kb_id, plan, exclusive=True).get(item["sha256"])
        if persisted is None or persisted["doc_id"] != row["doc_id"]:
            raise SmokeError("UNCERTAIN")
        journal(run, "upload", item["sha256"], "confirmed", doc_id=row["doc_id"], http_status=code)
        print(f"上传身份已确认：doc_id={row['doc_id']}；HTTP={code}")
    observed = {}
    last_progress = clock()
    while clock() < deadline:
        rows = reader(args.kb_id, plan, exclusive=True)
        finished = 0
        for item in plan["documents"]:
            row = rows.get(item["sha256"])
            if row is None:
                raise SmokeError("UNCERTAIN")
            match_document(item, row, args.kb_id)
            if observed.get(row["doc_id"]) != row["status"]:
                print(f"状态：doc_id={row['doc_id']}；{row['status']}；错误码={row['error_code'] or '-'}")
                observed[row["doc_id"]] = row["status"]
            code, body = api_request(client, "GET", f"/api/admin/knowledge-bases/{args.kb_id}/documents/{row['doc_id']}")
            if (code != 200 or body.get("doc_id") != row["doc_id"] or body.get("file_hash") != item["sha256"]
                    or body.get("kb_id") != str(args.kb_id)):
                raise SmokeError("TARGET")
            if row["status"] == "FAILED":
                finished += 1
                continue
            if row["status"] == "READY":
                finished += 1
                continue
            if row.get("management_task_id") is not None:
                # 新上传已有服务端逐文档编排；只观察，不和后台管理任务争抢旧逐阶段POST。
                code, status = api_request(client, "GET", f"/api/admin/documents/{row['doc_id']}/status")
                management = status.get("management")
                if (code != 200 or status.get("doc_id") != row["doc_id"] or status.get("kb_id") != str(args.kb_id)
                        or not isinstance(management, dict)
                        or management.get("management_task_id") != row["management_task_id"]
                        or management.get("state") not in {"running", "failed", "done"}):
                    raise SmokeError("TARGET")
                if management["state"] == "failed":
                    return {"status": "FAIL", "reason": "MANAGEMENT_FAILED", "doc_id": row["doc_id"]}
                continue
            if row["status"] in STAGES:
                operation, field = STAGES[row["status"]]
                if row[field] is not None:
                    continue
                if attempted(run, operation, item["sha256"]):
                    raise SmokeError("UNCERTAIN")
                journal(run, operation, item["sha256"], "intent", doc_id=row["doc_id"])
                code, body = api_request(client, "POST", f"/api/admin/knowledge-bases/{args.kb_id}/documents/{row['doc_id']}/{operation}")
                if code != 202 or body.get("doc_id") != row["doc_id"] or not body.get(field):
                    raise SmokeError("HTTP")
                try:
                    UUID(body[field])
                except (ValueError, TypeError):
                    raise SmokeError("HTTP") from None
                journal(run, operation, item["sha256"], "confirmed", doc_id=row["doc_id"], task_id=body[field])
                print(f"阶段已提交：doc_id={row['doc_id']}；stage={operation}")
        if finished == len(plan["documents"]):
            return {"status": "FAIL" if any(row["status"] == "FAILED" for row in rows.values()) else "PASS"}
        if clock() - last_progress >= 30:
            print(f"等待已有任务：完成={finished}/{len(plan['documents'])}；剩余预算约 {max(0, int(deadline - clock()))} 秒。")
            last_progress = clock()
        pause(min(args.poll_interval, max(0, deadline - clock())))
    return {"status": "BLOCKED", "reason": "TIMEOUT", "background_tasks_cancelled": False}


def validate_export(document, item, kb_id):
    """校验实际 Block 序号与字符证据，不推断视觉阅读顺序正确或补写解析结果。"""
    issues = []
    if document["kb_id"] != str(kb_id) or document["file_hash"] != item["sha256"] or document["file_type"] != item["file_type"]:
        issues.append("DOCUMENT_IDENTITY")
    blocks, chunks = document["blocks"], document["chunks"]
    if not blocks or [block["block_index"] for block in blocks] != list(range(1, len(blocks) + 1)):
        issues.append("BLOCK_ORDER")
    if len({block["block_id"] for block in blocks}) != len(blocks):
        issues.append("DUPLICATE_BLOCK")
    for block in blocks:
        if (block.get("doc_id") != document["doc_id"] or not isinstance(block["text"], str) or not block["text"].strip()
                or not isinstance(block["source"], str) or not block["source"].strip()
                or type(block["page"]) is not int or block["page"] < 1
                or (item["file_type"] == "pdf" and block["page"] > item["page_count"])
                or (item["file_type"] in {"jpg", "jpeg", "png"} and block["page"] != 1)
                or (item["file_type"] in {"docx", "xlsx", "pptx"} and block["bbox"] is not None)):
            issues.append("BLOCK_EVIDENCE")
    if not chunks or len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        issues.append("CHUNK_COUNT_OR_ID")
    for chunk in chunks:
        metadata = chunk["metadata"]
        semantics = "physical_pdf" if item["file_type"] == "pdf" else "image_page" if item["file_type"] in {"jpg", "jpeg", "png"} else "logical"
        if (chunk["kb_id"] != str(kb_id) or chunk["doc_id"] != document["doc_id"]
                or not metadata.get("source_spans")
                or metadata.get("page_semantics") != semantics
                or metadata.get("source_parse_task_id") != document["processing_task_id"]):
            issues.append("CHUNK_EVIDENCE")
        block_map = {block["block_id"]: block for block in blocks}
        spans = metadata.get("source_spans")
        if not isinstance(spans, list):
            issues.append("SPAN_SOURCE_FIELDS")
        else:
            for span in spans:
                block = block_map.get(span.get("block_id")) if isinstance(span, dict) else None
                if block is None or any(key not in span or span[key] != block[key] for key in ("source", "block_type", "bbox")):
                    issues.append("SPAN_SOURCE_FIELDS")
    if [chunk["metadata"].get("chunk_index") for chunk in chunks] != list(range(1, len(chunks) + 1)):
        issues.append("CHUNK_ORDER")
    checks = quality.chunk_evidence(document, item["source"], item["identity"], "smoke")
    if not checks or any(check["status"] != "pass" for check in checks):
        issues.append("SOURCE_SPANS")
    return sorted(set(issues))


def export_snapshot(plan, kb_id):
    """只读导出同库 Block/Chunk/分片收费摘要及向量统计，不导出向量/私有配置。"""
    from sqlalchemy import func, or_, select
    from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob, KnowledgeBase
    from app.services.indexing import IndexingError, checked_index_config, require_complete_vectors
    from app.services.rag.dense import DenseRetrievalError, model_snapshot
    from app.services.embedding.processing import chunks_hash

    exported, validations, missing = [], [], []
    with readonly_session() as session:
        kb = session.get(KnowledgeBase, kb_id)
        if kb is None or kb.status != "ACTIVE":
            raise SmokeError("TARGET")
        for item in plan["documents"]:
            d = session.scalar(select(Document).where(Document.kb_id == kb_id, Document.file_hash == item["sha256"]))
            if d is None:
                missing.append(item["file"])
                continue
            blocks = list(session.scalars(select(DocumentBlock).where(DocumentBlock.doc_id == d.doc_id)
                .order_by(DocumentBlock.block_index.asc().nulls_last(), DocumentBlock.block_id).limit(100001)))
            chunks = session.execute(select(DocumentChunk.chunk_id, DocumentChunk.doc_id, DocumentChunk.kb_id,
                DocumentChunk.text, DocumentChunk.page_start, DocumentChunk.page_end, DocumentChunk.section,
                DocumentChunk.chunk_metadata).where(DocumentChunk.doc_id == d.doc_id, DocumentChunk.kb_id == kb_id)
                .order_by(DocumentChunk.chunk_metadata["chunk_index"].as_integer(), DocumentChunk.chunk_id).limit(100001)).all()
            if len(blocks) > 100000 or len(chunks) > 100000:
                raise SmokeError("LIMIT")
            invalid = or_(func.vector_dims(DocumentChunk.embedding) != 1024,
                          func.abs(func.vector_norm(DocumentChunk.embedding) - 1) > 0.0001)
            total, vector_count, invalid_count = session.execute(select(func.count(), func.count(DocumentChunk.embedding),
                func.count().filter(invalid)).select_from(DocumentChunk).where(DocumentChunk.doc_id == d.doc_id,
                DocumentChunk.kb_id == kb_id)).one()
            jobs = session.execute(select(DocumentParseJob.job_id, DocumentParseJob.task_id, DocumentParseJob.state,
                DocumentParseJob.batch_id, DocumentParseJob.upload_complete, DocumentParseJob.submission_attempted,
                DocumentParseJob.parse_options["_pdf_split"]["parts"].label("pdf_parts"),
                DocumentParseJob.error_code, DocumentParseJob.is_active).where(DocumentParseJob.doc_id == d.doc_id)
                .order_by(DocumentParseJob.created_at, DocumentParseJob.job_id)).all()
            document = {"doc_id": str(d.doc_id), "kb_id": str(d.kb_id), "file_name": d.file_name,
                "file_type": d.file_type, "file_hash": d.file_hash, "status": d.status,
                "source_type": d.source_type, "sample_source": item["source"], "sample_identity": item["identity"],
                "error_code": document_error_code(d, jobs),
                **{field: str(getattr(d, field)) if getattr(d, field) else None for field in TASK_FIELDS},
                "model_identity": (d.index_config or {}).get("result", {}).get("model"),
                "blocks": [{"block_id": str(b.block_id), "doc_id": str(b.doc_id), "block_index": b.block_index,
                    "page": b.page, "section": b.section, "block_type": b.block_type, "text": b.text,
                    "bbox": b.bbox, "source": b.source, "confidence": b.confidence} for b in blocks],
                "chunks": [{"chunk_id": str(c.chunk_id), "doc_id": str(c.doc_id), "kb_id": str(c.kb_id),
                    "text": c.text, "page_start": c.page_start, "page_end": c.page_end,
                    "section": c.section, "metadata": c.chunk_metadata} for c in chunks],
                "parse_jobs": [{"job_id": str(j.job_id), "task_id": str(j.task_id), "state": j.state,
                    "batch_id": j.batch_id, "upload_complete": j.upload_complete,
                    "is_active": j.is_active,
                    # 仅导出收费/原页摘要，不导出私有 URL、缓存配方或正文。
                    "pdf_parts": [{key: part.get(key) for key in ("ordinal", "data_id", "task_id", "page_start",
                        "page_end", "file_hash", "size_bytes", "batch_id", "state", "upload_complete",
                        "submission_attempted", "error_code")} for part in (getattr(j, "pdf_parts", None) or [])],
                    "submission_attempted": j.submission_attempted, "error_code": safe_job_code(j.error_code)}
                    for j in jobs]}
            issues = validate_export(document, item, kb_id)
            if d.status != "READY":
                issues.append("NOT_READY")
            if total == 0 or total != vector_count or invalid_count != 0:
                issues.append("VECTOR_INCOMPLETE")
            try:
                config = checked_index_config(d)
                expected = config["embedding_result"]["chunk_count"]
                require_complete_vectors(session, d, expected)
                if config["result"]["chunk_count"] != expected or config["result"]["dimension"] != 1024:
                    raise ValueError
                if any(c.chunk_metadata.get("source_parse_task_id") != config["source_parse_task_id"] for c in chunks):
                    raise ValueError
                model = model_snapshot(config["result"]["model"])
                if (any(model[key] != config["embedding_result"][key]
                        for key in ("model_name", "model_fingerprint", "dimension"))
                        or config["result"]["vectors_sha256"] != config["embedding_result"]["vectors_sha256"]
                        or model["max_length"] != d.embedding_config["options"]["max_length"]
                        or model["precision"] != ("fp16" if d.embedding_config["options"]["use_fp16"] else "fp32")
                        or config["source_hash"] != chunks_hash([SimpleNamespace(chunk_id=c.chunk_id, text=c.text,
                            chunk_index=c.chunk_metadata.get("chunk_index")) for c in chunks])):
                    raise ValueError
            except (ValueError, KeyError, TypeError, IndexingError, DenseRetrievalError):
                issues.append("INDEX_CHECKPOINT")
            if sum(j.task_id == d.processing_task_id for j in jobs) > 1:
                issues.append("MULTIPLE_JOBS_SAME_PARSE_GENERATION")
            if any(getattr(d, field) is None for field in TASK_FIELDS):
                issues.append("TASK_CHECKPOINT_MISSING")
            if any(j.is_active for j in jobs):
                issues.append("ACTIVE_PARSE_JOB")
            exported.append(document)
            validations.append({"file": item["file"], "doc_id": str(d.doc_id), "document_status": d.status,
                "status": "PASS" if not issues else ("FAIL" if d.status in {"READY", "FAILED"} else "BLOCKED"),
                "failure_stage": next((name for name, field in reversed(list(STAGES.values())) if getattr(d, field)), "parse") if d.status == "FAILED" else None,
                "error_code": document["error_code"],
                "issues": sorted(set(issues)), "block_count": len(blocks), "chunk_count": total,
                "vector_count": vector_count, "invalid_vector_count": invalid_count})
    return {"documents": exported}, {"validations": validations, "missing_documents": missing,
        "status": "FAIL" if any(row["status"] == "FAIL" for row in validations) else
                  "BLOCKED" if missing or any(row["status"] == "BLOCKED" for row in validations) else "PASS"}


def verify_run(run, plan, kb_id):
    """复用三数据集质量诊断；OCB 原始断言及 Office 结构人工核对，不做字符串正确性判定。"""
    export, checks = export_snapshot(plan, kb_id)
    documents = quality.documents_index(export)
    qa = references_file(run / "official_qa.jsonl")
    gt = references_file(run / "omni_gt.jsonl")
    diagnostics = quality.ohr_checks(qa, documents) + quality.omni_checks(gt, documents, {})
    if plan.get("ocb_qa_count", 0):
        ocb_refs = references_file(run / "ocb_qa.jsonl")
        diagnostics += quality.ocb_checks(ocb_refs, documents)
        criteria = {"docx": "paragraph_heading_table", "xlsx": "sheet_cells_native_formula",
                    "pptx": "text_visual_relationship_logical_page_order"}
        for item in plan["documents"]:
            if item["source"] == "ocb":
                document = quality.document_by_hash(documents, item["sha256"])
                diagnostics.append(quality.conclusion("ocb", item["identity"], "office_structure",
                    criteria[item["file_type"]], "manual_pending" if document else "not_applicable",
                    doc_id=document["doc_id"] if document else None,
                    method="manual_original_office_no_print_page_fabrication"))
    path = save_artifact(run, "export", export)
    result = checks | {"kb_id": str(kb_id), "export_file": path.relative_to(run).as_posix(),
        "export_sha256": ohr.digest(export), "parsing_diagnostics": diagnostics,
        "parsing_summary": quality.summary(diagnostics), "visual_order_validation": "manual_pending"}
    save_artifact(run, "verify", result)
    print(f"只读验证：{result['status']}；已找到={len(documents)}；READY={sum(d['status'] == 'READY' for d in documents.values())}")
    return export, result


def score_retrieval(qa, expected_doc, hits):
    """文档级 Hit 与截断 MRR；页命中只用物理 PDF 的实际 source_spans，不用宽页范围猜测。"""
    pages = set(qa["_ohr_reference"]["evidence_pages_1based"])
    ranks = [index + 1 for index, hit in enumerate(hits) if hit["doc_id"] == expected_doc["doc_id"]]
    rank = ranks[0] if ranks else None
    reliable = (expected_doc["file_type"] == "pdf" and bool(expected_doc["chunks"])
                and all(c["metadata"].get("page_semantics") == "physical_pdf" for c in expected_doc["chunks"]))
    result = {"qa_id": qa["ID"], "doc_name": qa["doc_name"], "expected_doc_id": expected_doc["doc_id"],
        "first_document_rank": rank, "reciprocal_rank_at20": 1 / rank if rank else 0.0,
        "page_status": "evaluable" if reliable else "not_applicable", "official_pages_1based": sorted(pages)}
    for k in (1, 5, 10):
        result[f"hit_at{k}"] = rank is not None and rank <= k
        result[f"page_hit_at{k}"] = (any(hit["doc_id"] == expected_doc["doc_id"] and pages.intersection(hit["span_pages"])
            for hit in hits[:k]) if reliable else None)
    return result


def score_ocb_retrieval(qa, documents, hits):
    """仅按官方完整文件依赖计算召回；断言/答案证据人工核对，不做字符串答案裁判。"""
    expected = [quality.document_by_hash(documents, item["sha256"]) for item in qa["dependencies"]]
    if not expected or any(doc is None for doc in expected):
        raise SmokeError("PLAN")
    expected_ids = {doc["doc_id"] for doc in expected}
    if len(expected_ids) != len(qa["reference_files"]):
        raise SmokeError("PLAN")
    ranks = [i + 1 for i, hit in enumerate(hits) if hit["doc_id"] in expected_ids]
    rank = ranks[0] if ranks else None
    result = {"qa_id": qa.get("qa_id", qa["reference_id"]), "reference_id": qa["reference_id"],
        "source_row_index": qa.get("source_row_index"), "reference_files": qa["reference_files"],
        "track": qa["track"], "question": qa["question"], "expected_assertions": qa["expected_assertions"],
        "expected_doc_ids": sorted(expected_ids), "first_document_rank": rank,
        "reciprocal_rank_at20": 1 / rank if rank else 0.0, "page_status": "not_applicable",
        "page_reason": "office_logical_page_has_no_official_physical_page_truth",
        "answer_evidence_status": "manual_pending", "assertion_validation": "manual_pending_no_string_answer_judge"}
    for k in (1, 5, 10):
        retrieved = {hit["doc_id"] for hit in hits[:k]}
        result[f"hit_at{k}"] = bool(retrieved.intersection(expected_ids))
        result[f"all_dependencies_hit_at{k}"] = expected_ids.issubset(retrieved)
        result[f"page_hit_at{k}"] = None
    return result


def retrieval_metrics(rows):
    """只统计已完成且有文档真值的查询；未运行/异常不填零，明确显示统计分母。"""
    measured = [row for row in rows if row["status"] in {"PASS", "FAIL"}]
    pages = [row for row in measured if row.get("page_status") == "evaluable"]
    return {"qa_count": len(rows), "measured_qa_count": len(measured),
        "blocked_qa_count": len(rows) - len(measured), "page_evaluable_count": len(pages),
        "mrr_at20": sum(row["reciprocal_rank_at20"] for row in measured) / len(measured) if measured else None,
        **{f"hit_at{k}": sum(row[f"hit_at{k}"] for row in measured) / len(measured) if measured else None for k in (1, 5, 10)},
        **{f"page_hit_at{k}": sum(row[f"page_hit_at{k}"] for row in pages) / len(pages) if pages else None for k in (1, 5, 10)}}


def retrieval_tasks(run, plan):
    """保留两套官方 QA 原字段，以各自原问题查询；Omni GT 永不进入 QA 或指标分母。"""
    tasks = [{"dataset": "ohr", "original": row, "query": row["questions"], "qa_id": row["ID"]}
             for row in references_file(run / "official_qa.jsonl")]
    if plan.get("ocb_qa_count", 0):
        tasks += [{"dataset": "ocb", "original": row, "query": row["question"], "qa_id": row.get("qa_id", row["reference_id"])}
                  for row in references_file(run / "ocb_qa.jsonl")]
    return tasks


def dense_run(run, plan, kb_id):
    """共同READY前置通过后以检索验收角色测OHR/OCB，复用Dense，不重编码或混算QA/GT。"""
    before, verification = verify_run(run, plan, kb_id)
    if verification["status"] != "PASS":
        # 必须在导入框架/配置日志/加载 BGE-M3 之前停止；未执行不能填零分冒充检索结果。
        save_artifact(run, "dense", {"status": "BLOCKED", "executed": False,
            "kb_id": str(kb_id), "qa_count": plan["official_qa_count"] + plan.get("ocb_qa_count", 0), "executed_qa_count": 0,
            "error_code": "SMOKE_RETRIEVAL_PREREQUISITE", "verification_status": verification["status"],
            "export_sha256": ohr.digest(before), "items": [],
            "hit_at1": None, "hit_at5": None, "hit_at10": None, "mrr_at20": None,
            "page_hit_at1": None, "page_hit_at5": None, "page_hit_at10": None,
            "page_evaluable_count": 0, "same_query_ids_stable": None, "no_data_or_task_change": None})
        raise SmokeError("RETRIEVAL_PREREQUISITE")
    queries = retrieval_tasks(run, plan)
    if not queries:
        raise SmokeError("INPUT")
    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.rag.llama_index.dense_retriever import DenseRetriever

    configure_logging(get_settings(), role="retrieval-acceptance")
    retriever = DenseRetriever(kb_id, top_k=20)
    docs = quality.documents_index(before)
    chunks = {chunk["chunk_id"]: chunk for document in docs.values() for chunk in document["chunks"]}
    results, stability, repeated_datasets = [], True, set()
    for index, entry in enumerate(queries):
        qa, dataset = entry["original"], entry["dataset"]
        started = monotonic()
        try:
            hits = retriever.retrieve_results(entry["query"])
            if len(hits) > 20:
                raise SmokeError("RETRIEVAL")
            summaries = []
            for hit in hits:
                original = chunks.get(str(hit.chunk_id))
                if (original is None or str(hit.kb_id) != str(kb_id) or str(hit.doc_id) != original["doc_id"]
                        or hit.text != original["text"] or hit.page_start != original["page_start"]
                        or hit.page_end != original["page_end"] or hit.section != original["section"]
                        or hit.metadata.get("source_spans") != original["metadata"].get("source_spans")
                        or hit.file_name != docs[original["doc_id"]]["file_name"]
                        or hit.file_type != docs[original["doc_id"]]["file_type"]
                        or hit.embedding_identity != docs[original["doc_id"]]["model_identity"]
                        or hit.retrieval_score is None or not math.isfinite(hit.retrieval_score)):
                    raise SmokeError("RETRIEVAL")
                summaries.append({"chunk_id": str(hit.chunk_id), "doc_id": str(hit.doc_id),
                    "score": hit.retrieval_score, "page_start": hit.page_start, "page_end": hit.page_end,
                    "file_name": hit.file_name, "section": hit.section, "source_spans": hit.metadata["source_spans"],
                    "span_pages": sorted({span["page"] for span in hit.metadata["source_spans"]})})
            if len({hit["chunk_id"] for hit in summaries}) != len(summaries) or any(
                    summaries[i]["score"] < summaries[i + 1]["score"] or
                    (summaries[i]["score"] == summaries[i + 1]["score"] and
                     summaries[i]["chunk_id"] > summaries[i + 1]["chunk_id"])
                    for i in range(len(summaries) - 1)):
                raise SmokeError("RETRIEVAL")
            if dataset not in repeated_datasets:
                repeated = retriever.retrieve_results(entry["query"])
                stability = stability and [str(hit.chunk_id) for hit in repeated] == [hit["chunk_id"] for hit in summaries]
                repeated_datasets.add(dataset)
            if dataset == "ohr":
                document = quality.document_by_hash(docs, qa["_ohr_reference"]["document_sha256"])
                if document is None:
                    raise SmokeError("PLAN")
                result = score_retrieval(qa, document, summaries)
            else:
                result = score_ocb_retrieval(qa, docs, summaries)
            # 冒烟的严格通过条件：命中文档，且可判定的物理证据页也须在前十项命中。
            result.update(status="PASS" if result["hit_at10"] and result["page_hit_at10"] is not False
                          and result.get("all_dependencies_hit_at10", True) else "FAIL", hits=summaries)
        except Exception as exc:
            # 只保留已有 Dense 白名单代码，不输出异常原文/查询。
            from app.services.rag.dense import ERRORS as dense_errors
            code = getattr(exc, "code", "")
            result = {"qa_id": entry["qa_id"], "status": "BLOCKED",
                "error_code": code if code in dense_errors else "SMOKE_RETRIEVAL",
                "hit_at1": None, "hit_at5": None, "hit_at10": None, "reciprocal_rank_at20": None,
                "page_status": "not_applicable"}
        result["dataset"] = dataset
        result["duration_ms"] = round((monotonic() - started) * 1000, 2)
        results.append(result)
        print(f"Dense：dataset={dataset}；{index + 1}/{len(queries)}；{result['status']}")
        if result["status"] == "BLOCKED":
            # 模型/SQL 失败不重试；未运行题保留数量与状态，但不伪造命中率的零分。
            results.extend({"qa_id": row["qa_id"], "dataset": row["dataset"], "status": "BLOCKED",
                "error_code": "NOT_RUN_AFTER_FAILURE", "hit_at1": None, "hit_at5": None, "hit_at10": None,
                "reciprocal_rank_at20": None, "page_status": "not_applicable"} for row in queries[index + 1:])
            break
    after, _ = export_snapshot(plan, kb_id)
    unchanged = ohr.digest(before) == ohr.digest(after)
    status = "BLOCKED" if any(row["status"] == "BLOCKED" for row in results) else "PASS"
    if not unchanged or not stability or any(row["status"] == "FAIL" for row in results):
        status = "FAIL"
    datasets = {}
    for name in ("ohr", "ocb"):
        rows = [row for row in results if row["dataset"] == name]
        datasets[name] = retrieval_metrics(rows) | {"status": "NOT_APPLICABLE" if not rows else
            "FAIL" if not unchanged or not stability or any(r["status"] == "FAIL" for r in rows) else
            "BLOCKED" if any(r["status"] == "BLOCKED" for r in rows) else "PASS"}
    datasets["omni"] = {"status": "NOT_APPLICABLE", "reason": "parsing_gt_not_question_answer_truth"}
    result = {"status": status, "executed": True, "kb_id": str(kb_id), "qa_count": len(results), "top_k": 20,
        "executed_qa_count": sum(row.get("error_code") != "NOT_RUN_AFTER_FAILURE" for row in results),
        "items": results, "same_query_ids_stable": stability, "no_data_or_task_change": unchanged,
        "export_sha256": ohr.digest(before), "datasets": datasets,
        # 保留旧 OHR 指标字段供旧报告读取，绝不把 OCB 断言或 Omni GT 混入该分母。
        **{key: datasets["ohr"][key] for key in ("hit_at1", "hit_at5", "hit_at10", "mrr_at20",
             "page_evaluable_count", "page_hit_at1", "page_hit_at5", "page_hit_at10")},
        "legacy_metric_scope": "ohr_only",
        "criterion": "OHR document+physical_page_hit10; OCB all_dependencies_hit10; assertions_manual; not_answer_accuracy"}
    save_artifact(run, "dense", result)
    return result


def formal_gaps(plan):
    """仅说明小样本与正式标准的差距，不修改正式 manifest 或猜七类语义标签。"""
    kinds = Counter(item["file_type"] for item in plan["documents"])
    modern = {"image" if kind in {"jpg", "jpeg", "png"} else kind for kind in kinds
              if kind in {"txt", "md", "csv", "docx", "xlsx", "pptx", "pdf", "jpg", "jpeg", "png"}}
    return {"status": "BLOCKED", "scope": "smoke_samples_only_not_formal_preset_inventory",
        "unique_smoke_hashes": len({item["sha256"] for item in plan["documents"]}),
        "missing_to_ten_smoke_hashes": max(0, 10 - len(plan["documents"])),
        "missing_legacy_formats": sorted({"doc", "xls", "ppt"} - set(kinds)),
        "modern_format_categories": sorted(modern), "missing_to_three_modern_categories": max(0, 3 - len(modern)),
        "tags_pending": ["text_pdf", "scan_pdf", "mixed", "table", "formula", "chart", "reading_order"],
        "not_verified": ["原始来源独立性（Omni 页面不等于独立文档）", "正式 source_type=preset 默认库导入",
            "正式清单覆盖预检", "全批内容质量人工对照", "阶段16正式重复导入幂等性", "平台实际费用及外传授权"]}


def dataset_reports(plan, verification, dense, export):
    """按数据集拆分文件链路、检索分母和人工质量边界；旧双数据集运行不回填 OCB。"""
    reports = {}
    current_dense = bool(dense and verification and verification.get("status") == "PASS" and dense.get("executed", True)
                         and dense.get("export_sha256") == verification["export_sha256"])
    gaps = plan["preparation_gaps"]
    for name in ("ohr", "omni", "ocb"):
        files = [item for item in plan["documents"] if item["source"] == name]
        names, hashes = {item["file"] for item in files}, {item["sha256"] for item in files}
        requested = name in plan.get("datasets_requested", []) or bool(files)
        if not requested:
            reports[name] = {"status": "NOT_APPLICABLE", "file_count": 0,
                             "reason": "not_in_frozen_legacy_plan"}
            continue
        rows = [r for r in (verification or {}).get("validations", []) if r["file"] in names]
        documents = [d for d in export["documents"] if d["file_hash"] in hashes]
        pipeline = ("FAIL" if any(row["status"] == "FAIL" for row in rows) else
                    "BLOCKED" if not verification or not files or len(rows) != len(files)
                    or any(row["status"] != "PASS" for row in rows) else "PASS")
        metrics = (dense.get("datasets", {}).get(name) if current_dense else None)
        if name == "ohr" and current_dense and metrics is None:
            # 旧报告中的指标只属于 OHR，不用于伪造新增 OCB 的检索结论。
            metrics = {key: dense.get(key) for key in ("hit_at1", "hit_at5", "hit_at10", "mrr_at20")}
            metrics["status"] = dense["status"]
        retrieval = "NOT_APPLICABLE" if name == "omni" else (metrics or {}).get("status", "BLOCKED")
        missing = (gaps.get("pdf_shortfall") or gaps.get("qa_shortfall") or gaps.get("missing_evidence_categories")
                   if name == "ohr" else gaps.get("image_shortfall") if name == "omni" else
                   gaps.get("ocb_format_shortfall") or gaps.get("ocb_files_without_qa"))
        statuses = [pipeline, retrieval]
        if missing:
            statuses.append("BLOCKED")
        reports[name] = {"status": "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS",
            "file_count": len(files), "ready_count": sum(d["status"] == "READY" for d in documents),
            "formats": dict(Counter(item["file_type"] for item in files)),
            "pipeline_status": pipeline, "retrieval_status": retrieval, "retrieval_metrics": metrics,
            "qa_count": plan["official_qa_count"] if name == "ohr" else plan.get("ocb_qa_count", 0) if name == "ocb" else 0,
            "gt_count_not_qa": plan["omni_gt_count"] if name == "omni" else 0,
            "quality_status": "manual_pending" if verification else "BLOCKED",
            "automatic_pass_is_not_quality_or_answer_accuracy": True}
    return reports


def cloud_fee_summary(jobs):
    """按分片/普通文件计数确认Batch与不确定提交；不由父行数或重复日志推算收费。"""
    units = [part for job in jobs for part in (job.get("pdf_parts") or [job])]
    batches = sorted({unit["batch_id"] for unit in units if unit.get("batch_id")})
    return {"confirmed_batch_ids": batches, "distinct_confirmed_batches": len(batches),
            "file_jobs_with_batch": sum(bool(unit.get("batch_id")) for unit in units),
            "submission_outcome_unknown": sum(bool(unit.get("submission_attempted") and not unit.get("batch_id"))
                                              for unit in units)}


def generate_report(run, plan, *, error=None, error_step=None):
    """分数据集报告链路/质量并按实际分片统计收费；旧报告保留，不冒充问答准确率。"""
    verification = latest_artifact(run, "verify")
    dense = latest_artifact(run, "dense")
    process = latest_artifact(run, "process")
    export = common.json_file(ohr.plain_path(run / verification["export_file"], beneath=run)) if verification else {"documents": []}
    pipeline = verification["status"] if verification else "BLOCKED"
    dense_status = dense["status"] if dense else "BLOCKED"
    if dense and verification and dense["export_sha256"] != verification["export_sha256"]:
        dense_status = "BLOCKED"
    datasets = dataset_reports(plan, verification, dense, export)
    if plan.get("ocb_qa_count", 0) and datasets["ocb"]["retrieval_status"] == "BLOCKED":
        dense_status = "BLOCKED"
    gaps = plan["preparation_gaps"]
    statuses = [pipeline, dense_status]
    statuses += [row["status"] for row in datasets.values() if row["status"] != "NOT_APPLICABLE"]
    failures = sorted((run / "reports").glob("*-failure.json"))
    if error:
        # 失败单独冻结，后续纯 report 不能以旧的成功快照掩盖刚发生的故障。
        failure = {"error_code": error, "recovery_kind": "dense" if error_step == "retrieve"
                   and error != "SMOKE_RETRIEVAL_PREREQUISITE" else "verify"}
        failures.append(save_artifact(run, "failure", failure))
    unresolved = None
    if failures:
        failure = common.json_file(failures[-1])
        successes = sorted((run / "reports").glob(f"*-{failure['recovery_kind']}.json"))
        if not successes or successes[-1].name < failures[-1].name:
            unresolved = failure["error_code"]
    if any(gaps.values()) or unresolved:
        statuses.append("BLOCKED")
    if process and process["status"] == "FAIL":
        statuses.append("FAIL")
    status = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    jobs = [job for document in export["documents"] for job in document["parse_jobs"]]
    fees = cloud_fee_summary(jobs)
    events = [common.json_file(path) for path in sorted((run / "events").glob("*.json"))]
    upload_confirmed = {row["sha256"] for row in events if row["operation"] == "upload"
                        and row["phase"] == "confirmed" and row.get("http_status") in {201, 202}}
    unreconciled_uploads = {row["sha256"] for row in events if row["operation"] == "upload" and row["phase"] == "intent"}
    unreconciled_uploads -= {d["file_hash"] for d in export["documents"]}
    report = {"version": VERSION, "plan_sha256": plan["plan_sha256"], "small_sample_smoke": status,
        "stage16_formal_acceptance": formal_gaps(plan), "official_benchmark_full_evaluation": "NOT_APPLICABLE",
        "sources": dict(Counter(item["source"] for item in plan["documents"])), "file_count": len(plan["documents"]),
        "formats": dict(Counter(item["file_type"] for item in plan["documents"])),
        "upload_confirmed_count": len(upload_confirmed), "existing_document_count": len(export["documents"]),
        "ready_count": sum(d["status"] == "READY" for d in export["documents"]),
        "official_qa_count": plan["official_qa_count"], "ocb_qa_count": plan.get("ocb_qa_count", 0),
        "total_official_qa_count": plan["official_qa_count"] + plan.get("ocb_qa_count", 0),
        "datasets": datasets, "omni_gt_count_not_qa": plan["omni_gt_count"],
        "pipeline_status": pipeline, "verification": verification, "dense_status": dense_status, "dense": dense,
        "dense_block_reason": "VERIFICATION_NOT_PASS" if pipeline != "PASS" else None,
        "preparation_gaps": gaps, "process": process, "error_code": unresolved,
        "task_and_batch_summary": [{"doc_id": d["doc_id"], **{field: d[field] for field in TASK_FIELDS},
                                   "parse_jobs": d["parse_jobs"]} for d in export["documents"]],
        "potential_cost": {**fees,
            "upload_intents_not_reconciled": len(unreconciled_uploads),
            "money_amount": None, "billing_verification": "manual_pending; check MinerU account, not log line counts"},
        "quality_boundary": "仅自动结构/字符证据检查；视觉区域/阅读顺序/公式语义仍人工待核；无 LLM 或问答准确率",
        "parsing_quality_status": "BLOCKED" if verification else "NOT_APPLICABLE",
        "automatic_structure_status": pipeline,
        "reports_contain_source_text": True}
    if verification and ohr.digest(export) != verification["export_sha256"]:
        raise SmokeError("PLAN")
    report_name = stamp() + "-report"
    publish(run / "reports" / (report_name + ".json"), report)
    # 兼容旧成功快照；前置失败、未执行或旧数据身份不一致时不展示旧/虚构的零分指标。
    show_dense_metrics = (dense is not None and dense.get("executed", True)
        and pipeline == "PASS" and dense["export_sha256"] == verification["export_sha256"]
        and all(type(dense.get(key)) in {int, float} for key in ("hit_at1", "hit_at5", "hit_at10", "mrr_at20")))
    markdown = (f"# 阶段16/17 小样本验收\n\n小样本真实冒烟：**{status}**\n\n"
        f"文件 {report['file_count']}；本次 API 确认上传 {len(upload_confirmed)}；已有文档 {len(export['documents'])}；READY {report['ready_count']}。\n\n"
        f"链路：{pipeline}；Dense：{dense_status}；OHR QA {plan['official_qa_count']}；OCB QA {plan.get('ocb_qa_count', 0)}；Omni GT 图像 {plan['omni_gt_count']}（不是 QA）。\n\n"
        + (f"OHR Hit@1={dense['hit_at1']:.3f}，Hit@5={dense['hit_at5']:.3f}，Hit@10={dense['hit_at10']:.3f}，MRR@20={dense['mrr_at20']:.3f}。\n\n" if show_dense_metrics
           else "当前无可报告的 Dense 检索指标。\n\n")
        + ("READY/证据/向量前置校验未通过，当前阻塞来自文档链路，不能据此判断 GPU 或 Query 模型失败。\n\n" if pipeline != "PASS" else "")
        + ("本次 Dense 未执行，未加载 Query 模型；统计项为 null，不是零命中率。\n\n" if dense and dense.get("executed") is False else "")
        + f"准备缺口：`{json.dumps(gaps, ensure_ascii=False)}`。\n\n"
        + (f"阻塞错误：`{unresolved}`。\n\n" if unresolved else "")
        + f"确认云批次 {fees['distinct_confirmed_batches']}；按实际分片计数，金额仍需人工核对 MinerU 平台。\n\n"
        + "正式阶段16：**BLOCKED / 待验收**。仍需 ≥10 份真实独立内容、DOC/XLS/PPT、≥3 种现代格式、七类真实标签、正式 preset 导入与幂等/质量/费用确认；不降低原预检标准。\n\n"
        + "官方完整基准质量评测：**NOT_APPLICABLE**。READY、文档命中、GT 标签都不等于回答准确；无人工区域映射的 Omni 指标为 not_applicable/manual_pending。\n\n"
        + "Block 序号通过不代表视觉阅读顺序正确。OHR 字面证据未保留为 review_required，需对照原 PDF；表格/公式语义不自动裁判。\n\n"
        + "详见同名 JSON 的 verification.validations、parsing_diagnostics、Dense 逐题结果、任务与批次。报告/导出含原文，勿提交 Git 或公开分享。\n")
    markdown += "\n| 数据集 | 小样本状态 | 文件 / READY | 链路 | 检索 | QA / GT | 解析质量 |\n|---|---|---|---|---|---|---|\n"
    metric_lines = []
    for name, row in datasets.items():
        if row["status"] == "NOT_APPLICABLE":
            markdown += f"| {name} | NOT_APPLICABLE | 0 / 0 | - | - | - | 旧计划未纳入 |\n"
            continue
        markdown += (f"| {name} | {row['status']} | {row['file_count']} / {row['ready_count']} | {row['pipeline_status']} | "
                     f"{row['retrieval_status']} | {row['qa_count']} / {row['gt_count_not_qa']} | {row['quality_status']} |\n")
        metrics = row["retrieval_metrics"]
        if metrics and all(type(metrics.get(key)) in {int, float} for key in ("hit_at1", "hit_at5", "hit_at10", "mrr_at20")):
            metric_lines.append(f"\n{name}：Hit@1={metrics['hit_at1']:.3f}，Hit@5={metrics['hit_at5']:.3f}，"
                         f"Hit@10={metrics['hit_at10']:.3f}，MRR@20={metrics['mrr_at20']:.3f}；"
                         f"有效查询分母={metrics.get('measured_qa_count', '旧快照未记录')}。\n\n")
    markdown += "".join(metric_lines)
    markdown += "\nOCB 文件命中不证明答案证据正确；原始 expected_assertions 保留并为 manual_pending。Office 逻辑页不参与物理页命中率。\n"
    if verification:
        markdown += "\n| 文件 | 状态 | 失败阶段 / 错误码 | Block | Chunk | 向量 | 问题 |\n|---|---|---|---:|---:|---:|---|\n"
        for row in verification["validations"]:
            label = row["file"].replace("|", "\\|").replace("`", "")
            markdown += (f"| {label} | {row['document_status']}/{row['status']} | {row.get('failure_stage') or '-'} / {row.get('error_code') or '-'} | {row['block_count']} | "
                         f"{row['chunk_count']} | {row['vector_count']} | {','.join(row['issues']) or '-'} |\n")
        if verification["missing_documents"]:
            markdown += "\n尚未找到文档：" + ", ".join(verification["missing_documents"]) + "。\n"
    publish(run / "reports" / (report_name + ".md"), markdown.encode("utf-8"))
    print(f"报告已生成：docs/test/stage16_smoke/{run.name}/reports/{report_name}.md；{status}")
    return report


def build_parser():
    """默认三数据集抽样与独立显式步骤；云授权覆盖所选全部文件，不由下载状态推断。"""
    parser = argparse.ArgumentParser(description="阶段16/17 小样本真实冒烟；默认 dry-run，无自动部署/下载")
    parser.add_argument("--step", choices=("prepare", "process", "verify", "retrieve", "report"), default="prepare")
    parser.add_argument("--run", default="run-01", help="独立运行目录名；准备不覆盖旧目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="用户显式执行所选步骤")
    mode.add_argument("--dry-run", action="store_true", help="默认仅说明，无业务连接或文件写入")
    parser.add_argument("--confirm-local-use", action="store_true", help="确认本地官方来源及素材研究使用权限，不授予云外传")
    parser.add_argument("--confirm-test-kb", action="store_true", help="确认 UUID 为独立测试知识库，不是业务库")
    parser.add_argument("--allow-cloud", action="store_true", help="确认允许全部样本外传 MinerU 及产生费用")
    parser.add_argument("--kb-id", type=UUID)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="本机 API，允许 root_path 前缀")
    parser.add_argument("--pdf-count", type=int, default=5)
    parser.add_argument("--image-count", type=int, default=3)
    parser.add_argument("--qa-count", type=int, default=20)
    parser.add_argument("--ocb-per-format", type=int, default=2, help="OCB 每种 DOCX/XLSX/PPTX 选1～2份，默认2")
    parser.add_argument("--ocb-qa-per-file", type=int, default=4, help="每份 OCB 文件保留1～4条完整官方QA，默认4")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-total-pages", type=int, default=150)
    parser.add_argument("--extra", type=Path, action="append", default=[], help="可选用户自有 DOCX/XLSX/PPTX，最多3份；无自定义QA")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-interval", type=int, default=5)
    return parser


def main(argv=None):
    """用户显式运行三数据集；旧计划只读兼容，默认不创建样本、连接服务或授予外传。"""
    args = build_parser().parse_args(argv)
    plan, run = None, None
    try:
        if (re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.run) is None
                or not 1 <= args.pdf_count <= 8 or not 0 <= args.image_count <= 3
                or not args.pdf_count <= args.qa_count <= 50 or len(args.extra) > 3
                or not 1 <= args.ocb_per_format <= 2 or not 1 <= args.ocb_qa_per_file <= 4
                or not 1 <= args.max_pages <= 50 or not 1 <= args.max_total_pages <= 200
                or not 1 <= args.timeout <= 86400 or not 1 <= args.poll_interval <= 60):
            raise SmokeError("INPUT")
        run = ohr.plain_path(TEST_ROOT / args.run, beneath=TEST_ROOT)
        if not args.execute:
            print(f"DRY-RUN：step={args.step}；run={args.run}。未创建素材/报告，未连接API/数据库/Redis/模型或云端。")
            print("准备：--step prepare --execute --confirm-local-use；处理：--step process --execute --kb-id UUID --confirm-test-kb --allow-cloud。")
            print("只读验证：--step verify --execute --kb-id UUID；Dense：停止空闲 GPU Worker 后 --step retrieve --execute --kb-id UUID。")
            return 0
        if args.step in {"process", "verify", "retrieve"} and args.kb_id is None:
            raise SmokeError("INPUT")
        if args.step == "process" and (not args.confirm_test_kb or not args.allow_cloud):
            raise SmokeError("PERMISSION")
        if args.step == "prepare":
            # 不在准备锁内检查空目录，否则自己的锁会被误判为旧用户文件。
            if run.exists() and any(run.iterdir()):
                raise SmokeError("EXISTS")
            if not args.confirm_local_use:
                raise SmokeError("PERMISSION")
            run.mkdir(parents=True, exist_ok=True)
            with run_lock(run):
                plan = prepare_samples(args, run)
                generate_report(run, plan)
            print(f"离线准备结束：真实文件={len(plan['documents'])}；OHR QA={plan['official_qa_count']}；"
                  f"OCB QA={plan.get('ocb_qa_count', 0)}；Omni GT={plan['omni_gt_count']}；未上传。")
            return 0 if not any(plan["preparation_gaps"].values()) else 2
        with run_lock(run):
            plan = load_plan(run)
            if args.step in {"process", "verify", "retrieve"}:
                bind_target(run, plan, args.kb_id)
            if args.step == "process":
                if any(plan["preparation_gaps"].values()):
                    raise SmokeError("INPUT")
                import httpx
                # base_url 尾斜杠 + 相对路径保留反向代理 root_path；所有请求直连本机。
                with httpx.Client(base_url=checked_api_url(args.api_url) + "/", trust_env=False,
                                  follow_redirects=False, timeout=httpx.Timeout(60, connect=5)) as client:
                    precheck(args, run, plan, client)
                    before, _ = export_snapshot(plan, args.kb_id)
                    result = drive_pipeline(args, run, plan, client)
                after, verification = verify_run(run, plan, args.kb_id)
                # 已 READY 的重复操作须保持完整证据/任务/批次不变；新文档正常推进不算重复。
                prior_ready = {d["doc_id"]: d for d in before["documents"] if d["status"] == "READY"}
                current = {d["doc_id"]: d for d in after["documents"]}
                stable = all(doc_id in current and ohr.digest(d) == ohr.digest(current[doc_id]) for doc_id, d in prior_ready.items())
                result.update(existing_ready_count=len(prior_ready), existing_ready_unchanged=stable)
                if not stable:
                    result["status"] = "FAIL"
                save_artifact(run, "process", result)
            elif args.step == "verify":
                read_target(args.kb_id, plan)
                verify_run(run, plan, args.kb_id)
            elif args.step == "retrieve":
                read_target(args.kb_id, plan)
                dense_run(run, plan, args.kb_id)
            report = generate_report(run, plan)
            if args.step in {"process", "verify"}:
                return 0 if report["pipeline_status"] == "PASS" and not report["error_code"] and not (
                    report["process"] and report["process"]["status"] == "FAIL") else 1
            return 0 if report["small_sample_smoke"] == "PASS" else 1
    except KeyboardInterrupt:
        print("验收已中断；后台任务不取消，重新执行同一 process 命令只恢复已有身份/检查点。")
        return 130
    except Exception as exc:
        error = exc if isinstance(exc, SmokeError) else SmokeError("INPUT" if isinstance(exc, (common.BenchmarkError, ohr.PreparationError)) else "IO")
        print(error)
        if plan is not None and run is not None:
            try:
                generate_report(run, plan, error="SMOKE_" + error.code, error_step=args.step)
            except Exception:
                print("报告发布未完成；已有文件保留，请核对独立运行目录。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
