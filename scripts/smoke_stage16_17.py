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
    """用户显式离线准备：只验证容器/页数/图像完整性，不提取正文、不运行 Parser。"""
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
    plan = {"version": VERSION, "parameters": {"seed": args.seed, "pdf_count": args.pdf_count,
        "image_count": args.image_count, "qa_count": args.qa_count, "max_pages": args.max_pages,
        "max_total_pages": args.max_total_pages}, "documents": documents,
        "official_qa_count": len(references), "omni_gt_count": len(omni_refs),
        "qa_sha256": hashlib.sha256(qa_bytes).hexdigest(), "gt_sha256": hashlib.sha256(gt_bytes).hexdigest(),
        "ohr_source": provenance | {"zip_central_directory_sha256": directory_sha},
        "source_qa_inventory": inventory, "excluded_qa": rejected_qa,
        "preparation_gaps": {"pdf_shortfall": max(0, args.pdf_count - len(selected)),
            "qa_shortfall": max(0, args.qa_count - len(references)),
            "image_shortfall": max(0, args.image_count - len(omni_refs)),
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
    publish(run / "plan.json", plan)
    return plan


def load_plan(run):
    """在任何运行步骤前复核冻结计划、原始 QA/GT 和每份普通文件，不解析正文。"""
    plan = common.json_file(ohr.plain_path(run / "plan.json", beneath=TEST_ROOT))
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("version") != VERSION or ohr.digest(payload) != plan.get("plan_sha256"):
        raise SmokeError("PLAN")
    documents = plan.get("documents")
    if not isinstance(documents, list) or not 0 < len(documents) <= 14:
        raise SmokeError("PLAN")
    seen, hashes = set(), set()
    for item in documents:
        name = ohr.safe_name(item["file"]).as_posix()
        if not name.startswith("samples/") or name.casefold() in seen or item["sha256"] in hashes:
            raise SmokeError("PLAN")
        sample_bytes(run, item)
        seen.add(name.casefold())
        hashes.add(item["sha256"])
    for name, key in (("official_qa.jsonl", "qa_sha256"), ("omni_gt.jsonl", "gt_sha256")):
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
    """读取小测试库的状态与任务标识，不读取向量/正文，不 flush/commit 或修改业务状态。"""
    from sqlalchemy import select
    from app.db.seed import DEFAULT_KB_ID
    from app.models import Document, KnowledgeBase

    with readonly_session() as session:
        kb = session.get(KnowledgeBase, kb_id)
        if kb is None or kb.status != "ACTIVE" or kb_id == DEFAULT_KB_ID:
            raise SmokeError("TARGET")
        rows = list(session.scalars(select(Document).where(Document.kb_id == kb_id).limit(101)))
        hashes = {item["sha256"] for item in plan["documents"]}
        if len(rows) > 100 or (exclusive and any(row.file_hash not in hashes for row in rows)):
            raise SmokeError("TARGET")
        result = {}
        for row in rows:
            if row.file_hash not in hashes:
                continue
            if row.file_hash in result:
                raise SmokeError("TARGET")
            result[row.file_hash] = {"doc_id": str(row.doc_id), "kb_id": str(row.kb_id), "file_type": row.file_type,
                "status": row.status, "error_code": safe_code(row.error_message),
                **{field: str(getattr(row, field)) if getattr(row, field) else None for field in TASK_FIELDS}}
        return result


def safe_code(message):
    """仅识别应用安全代码前缀；未知错误正文/连接串/上游文本一律不写报告。"""
    match = re.match(r"^((?:CHUNK|EMBEDDING|INDEX|MINERU|PARSER|ROUTE|CACHE)_[A-Z_]{1,64})[：:]", message or "")
    return match[1] if match else ("DOCUMENT_FAILED" if message else None)


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
    """仅通过原 API 上传及提交后续阶段；每个 POST 有意图记录，已有任务只等待、不补投。"""
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
    """只导出同库、同 SHA 的必要 Block/Chunk，SQL 核对全量向量，不导出向量及私有配置。"""
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
                DocumentParseJob.error_code, DocumentParseJob.is_active).where(DocumentParseJob.doc_id == d.doc_id)
                .order_by(DocumentParseJob.created_at, DocumentParseJob.job_id)).all()
            document = {"doc_id": str(d.doc_id), "kb_id": str(d.kb_id), "file_name": d.file_name,
                "file_type": d.file_type, "file_hash": d.file_hash, "status": d.status,
                "source_type": d.source_type, "sample_source": item["source"], "sample_identity": item["identity"],
                "error_code": safe_code(d.error_message),
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
                    "submission_attempted": j.submission_attempted, "error_code": safe_code((j.error_code or "") + "：")}
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
                "issues": sorted(set(issues)), "block_count": len(blocks), "chunk_count": total,
                "vector_count": vector_count, "invalid_vector_count": invalid_count})
    return {"documents": exported}, {"validations": validations, "missing_documents": missing,
        "status": "FAIL" if any(row["status"] == "FAIL" for row in validations) else
                  "BLOCKED" if missing or any(row["status"] == "BLOCKED" for row in validations) else "PASS"}


def verify_run(run, plan, kb_id):
    """只读验证并复用已有质量诊断；无人工区域映射时 Omni 不伪算 OCR 或阅读顺序分数。"""
    export, checks = export_snapshot(plan, kb_id)
    documents = quality.documents_index(export)
    qa = references_file(run / "official_qa.jsonl")
    gt = references_file(run / "omni_gt.jsonl")
    diagnostics = quality.ohr_checks(qa, documents) + quality.omni_checks(gt, documents, {})
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


def dense_run(run, plan, kb_id):
    """用户显式运行已有 DenseRetriever；仅 Query 向量化，无 LLM，无文档重算或新任务。"""
    before, verification = verify_run(run, plan, kb_id)
    if verification["status"] != "PASS":
        raise SmokeError("RETRIEVAL")
    qa_rows = references_file(run / "official_qa.jsonl")
    if not qa_rows:
        raise SmokeError("INPUT")
    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.rag.llama_index.dense_retriever import DenseRetriever

    configure_logging(get_settings(), role="dense-cli")
    retriever = DenseRetriever(kb_id, top_k=20)
    docs = quality.documents_index(before)
    chunks = {chunk["chunk_id"]: chunk for document in docs.values() for chunk in document["chunks"]}
    results, stability, first_ids = [], True, None
    for index, qa in enumerate(qa_rows):
        document = quality.document_by_hash(docs, qa["_ohr_reference"]["document_sha256"])
        started = monotonic()
        try:
            hits = retriever.retrieve_results(qa["questions"])
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
                    "span_pages": sorted({span["page"] for span in hit.metadata["source_spans"]})})
            if len({hit["chunk_id"] for hit in summaries}) != len(summaries) or any(
                    summaries[i]["score"] < summaries[i + 1]["score"] for i in range(len(summaries) - 1)):
                raise SmokeError("RETRIEVAL")
            if index == 0:
                first_ids = [hit["chunk_id"] for hit in summaries]
                repeated = retriever.retrieve_results(qa["questions"])
                stability = [str(hit.chunk_id) for hit in repeated] == first_ids
            result = score_retrieval(qa, document, summaries)
            # 冒烟的严格通过条件：命中文档，且可判定的物理证据页也须在前十项命中。
            result.update(status="PASS" if result["hit_at10"] and result["page_hit_at10"] is not False else "FAIL", hits=summaries)
        except Exception as exc:
            # 只保留已有 Dense 白名单代码，不输出异常原文/查询。
            from app.services.rag.dense import ERRORS as dense_errors
            code = getattr(exc, "code", "")
            result = {"qa_id": qa["ID"], "doc_name": qa["doc_name"], "status": "BLOCKED",
                "error_code": code if code in dense_errors else "SMOKE_RETRIEVAL",
                "hit_at1": False, "hit_at5": False, "hit_at10": False, "reciprocal_rank_at20": 0.0,
                "page_status": "not_applicable"}
        result["duration_ms"] = round((monotonic() - started) * 1000, 2)
        results.append(result)
        print(f"Dense：{index + 1}/{len(qa_rows)}；{result['status']}")
        if result["status"] == "BLOCKED":
            # 模型/SQL 失败不循环加载和重试；未执行的题仍保留在分母和报告中。
            results.extend({"qa_id": row["ID"], "doc_name": row["doc_name"], "status": "BLOCKED",
                "error_code": "NOT_RUN_AFTER_FAILURE", "hit_at1": False, "hit_at5": False, "hit_at10": False,
                "reciprocal_rank_at20": 0.0, "page_status": "not_applicable"} for row in qa_rows[index + 1:])
            break
    after, _ = export_snapshot(plan, kb_id)
    unchanged = ohr.digest(before) == ohr.digest(after)
    status = "BLOCKED" if any(row["status"] == "BLOCKED" for row in results) else "PASS"
    if not unchanged or not stability or any(row["status"] == "FAIL" for row in results):
        status = "FAIL"
    measured_pages = [row for row in results if row["page_status"] == "evaluable"]
    result = {"status": status, "kb_id": str(kb_id), "qa_count": len(results), "top_k": 20,
        "items": results, "same_query_ids_stable": stability, "no_data_or_task_change": unchanged,
        "export_sha256": ohr.digest(before), "mrr_at20": sum(row["reciprocal_rank_at20"] for row in results) / len(results),
        "page_evaluable_count": len(measured_pages),
        **{f"hit_at{k}": sum(row[f"hit_at{k}"] for row in results) / len(results) for k in (1, 5, 10)},
        **{f"page_hit_at{k}": sum(row[f"page_hit_at{k}"] for row in measured_pages) / len(measured_pages)
           if measured_pages else None for k in (1, 5, 10)},
        "criterion": "all_official_questions_document_hit_at10_and_evaluable_page_hit_at10; not_answer_accuracy"}
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


def generate_report(run, plan, *, error=None, error_step=None):
    """区分小样本、正式验收、官方总评测；不覆盖旧 JSON/Markdown，不输出原始正文。"""
    verification = latest_artifact(run, "verify")
    dense = latest_artifact(run, "dense")
    process = latest_artifact(run, "process")
    export = common.json_file(ohr.plain_path(run / verification["export_file"], beneath=run)) if verification else {"documents": []}
    pipeline = verification["status"] if verification else "BLOCKED"
    dense_status = dense["status"] if dense else "BLOCKED"
    if dense and verification and dense["export_sha256"] != verification["export_sha256"]:
        dense_status = "BLOCKED"
    gaps = plan["preparation_gaps"]
    statuses = [pipeline, dense_status]
    failures = sorted((run / "reports").glob("*-failure.json"))
    if error:
        # 失败单独冻结，后续纯 report 不能以旧的成功快照掩盖刚发生的故障。
        failure = {"error_code": error, "recovery_kind": "dense" if error_step == "retrieve" else "verify"}
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
    batches = sorted({job["batch_id"] for job in jobs if job["batch_id"]})
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
        "official_qa_count": plan["official_qa_count"], "omni_gt_count_not_qa": plan["omni_gt_count"],
        "pipeline_status": pipeline, "verification": verification, "dense_status": dense_status, "dense": dense,
        "preparation_gaps": gaps, "process": process, "error_code": unresolved,
        "task_and_batch_summary": [{"doc_id": d["doc_id"], **{field: d[field] for field in TASK_FIELDS},
                                   "parse_jobs": d["parse_jobs"]} for d in export["documents"]],
        "potential_cost": {"confirmed_batch_ids": batches, "distinct_confirmed_batches": len(batches),
            "file_jobs_with_batch": sum(bool(job["batch_id"]) for job in jobs),
            "submission_outcome_unknown": sum(job["submission_attempted"] and not job["batch_id"] for job in jobs),
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
    markdown = (f"# 阶段16/17 小样本验收\n\n小样本真实冒烟：**{status}**\n\n"
        f"文件 {report['file_count']}；本次 API 确认上传 {len(upload_confirmed)}；已有文档 {len(export['documents'])}；READY {report['ready_count']}。\n\n"
        f"链路：{pipeline}；Dense：{dense_status}；官方 QA {plan['official_qa_count']}；Omni GT 图像 {plan['omni_gt_count']}（不是 QA）。\n\n"
        + (f"Hit@1={dense['hit_at1']:.3f}，Hit@5={dense['hit_at5']:.3f}，Hit@10={dense['hit_at10']:.3f}，MRR@20={dense['mrr_at20']:.3f}。\n\n" if dense else "Dense 尚未运行。\n\n")
        + f"准备缺口：`{json.dumps(gaps, ensure_ascii=False)}`。\n\n"
        + (f"阻塞错误：`{unresolved}`。\n\n" if unresolved else "")
        + f"确认云批次 {len(batches)}；金额未知，需人工核对 MinerU 平台；同文件多个 Job 要人工排查。\n\n"
        + "正式阶段16：**BLOCKED / 待验收**。仍需 ≥10 份真实独立内容、DOC/XLS/PPT、≥3 种现代格式、七类真实标签、正式 preset 导入与幂等/质量/费用确认；不降低原预检标准。\n\n"
        + "官方完整基准质量评测：**NOT_APPLICABLE**。READY、文档命中、GT 标签都不等于回答准确；无人工区域映射的 Omni 指标为 not_applicable/manual_pending。\n\n"
        + "Block 序号通过不代表视觉阅读顺序正确。OHR 字面证据未保留为 review_required，需对照原 PDF；表格/公式语义不自动裁判。\n\n"
        + "详见同名 JSON 的 verification.validations、parsing_diagnostics、Dense 逐题结果、任务与批次。报告/导出含原文，勿提交 Git 或公开分享。\n")
    if verification:
        markdown += "\n| 文件 | 状态 | Block | Chunk | 向量 | 问题 |\n|---|---|---:|---:|---:|---|\n"
        for row in verification["validations"]:
            label = row["file"].replace("|", "\\|").replace("`", "")
            markdown += (f"| {label} | {row['document_status']}/{row['status']} | {row['block_count']} | "
                         f"{row['chunk_count']} | {row['vector_count']} | {','.join(row['issues']) or '-'} |\n")
        if verification["missing_documents"]:
            markdown += "\n尚未找到文档：" + ", ".join(verification["missing_documents"]) + "。\n"
    publish(run / "reports" / (report_name + ".md"), markdown.encode("utf-8"))
    print(f"报告已生成：docs/test/stage16_smoke/{run.name}/reports/{report_name}.md；{status}")
    return report


def build_parser():
    """提供独立步骤和显式授权；所有步骤省略 --execute 都只打印计划，不创建数据目录。"""
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
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-total-pages", type=int, default=150)
    parser.add_argument("--extra", type=Path, action="append", default=[], help="可选用户自有 DOCX/XLSX/PPTX，最多3份；无自定义QA")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-interval", type=int, default=5)
    return parser


def main(argv=None):
    """延迟导入真实依赖；仅用户显式步骤会访问 API/数据库/GPU，错误非零退出并保留材料。"""
    args = build_parser().parse_args(argv)
    plan, run = None, None
    try:
        if (re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.run) is None
                or not 1 <= args.pdf_count <= 8 or not 0 <= args.image_count <= 3
                or not args.pdf_count <= args.qa_count <= 50 or len(args.extra) > 3
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
            print(f"离线准备结束：真实文件={len(plan['documents'])}；官方QA={plan['official_qa_count']}；未上传。")
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
