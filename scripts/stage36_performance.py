"""阶段36冻结规模/题单、请求隔离采集和本地报告；不自动调用收费服务。"""

import math
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from time import perf_counter, process_time
from uuid import UUID

from app.core.query_performance import capture_query_performance
from scripts.stage33_common import ROOT, digest, plain_path, read_json, sha_file, write_json

VERSION = "stage36-performance-v1"


class Stage36Error(ValueError):
    """仅传递内部固定错误码，不携带问题、路径、连接信息或异常原文。"""

    def __init__(self, code):
        """保存性能工具的安全失败类别。"""
        self.code = code
        super().__init__("STAGE36_" + code)


def run_directory(name):
    """将所有结果限定在阶段36的独立运行目录，拒绝路径跳转和链接。"""
    if not isinstance(name, str) or not re.fullmatch(r"stage36_run_[0-9]+", name):
        raise Stage36Error("RUN_NAME")
    return plain_path(ROOT / "results/stage36" / name, ROOT / "results/stage36")


def make_plan(source_run, *, sizes=(10, 50, 200), concurrency=(1, 2), questions=3,
              repeats=2, warmup=1, seed=36):
    """复用阶段34已冻结题单与索引；规模按身份排序，不用答案或检索成绩选范围。"""
    from scripts.stage33_runtime import is_indexed, scope_identity
    from scripts.stage34_evaluation import load_plan, run_directory as source_directory
    if not isinstance(source_run, str) or not re.fullmatch(r"stage34_run_[0-9]+", source_run):
        raise Stage36Error("SOURCE_RUN")
    if (type(questions) is not int or not 1 <= questions <= 20
            or type(repeats) is not int or not 1 <= repeats <= 10
            or type(warmup) is not int or not 0 <= warmup <= 3 or type(seed) is not int):
        raise Stage36Error("PLAN_ARGUMENTS")
    source = load_plan(source_directory(source_run))
    rows = list(source["index_snapshot"]["documents"].values())
    if (not rows or any(not is_indexed(row) for row in rows)
            or len({row["doc_id"] for row in rows}) != len(rows)
            or any(not row.get("model_identity") for row in rows)
            or len({digest(row["model_identity"]) for row in rows}) != 1
            or scope_identity({row["file_hash"]: row for row in rows}) != source["source_scope"]
            or str(UUID(source["kb_id"])) != source["index_snapshot"]["kb_id"]):
        raise Stage36Error("SOURCE_IDENTITY")
    if (not sizes or len(sizes) > 5 or any(type(n) is not int or not 1 <= n <= len(rows) for n in sizes)
            or len(set(sizes)) != len(sizes) or not concurrency or len(concurrency) > 3
            or any(type(n) is not int or not 1 <= n <= 4 for n in concurrency)
            or len(set(concurrency)) != len(concurrency)):
        raise Stage36Error("PLAN_ARGUMENTS")
    # 指定范围沿用生产100文档上限；全库用None，并在运行前后核对完整冻结集合。
    if any(n > 100 and n != len(rows) for n in sizes):
        raise Stage36Error("SCOPE_LIMIT")
    candidates, seen = [], set()
    for case in sorted(source["cases"], key=lambda case: digest([seed, case["case_id"]])):
        key = (case["dataset"], case["qa_id"])
        if case["purpose"] == "official_question" and key not in seen:
            candidates.append({k: case[k] for k in ("case_id", "dataset", "qa_id", "question")})
            seen.add(key)
    if len(candidates) < questions:
        raise Stage36Error("SAMPLE_TOO_SMALL")
    ordered = sorted(rows, key=lambda row: digest([seed, row["file_hash"], row["doc_id"]]))
    scopes = []
    for size in sorted(sizes):
        selected = ordered[:size]
        scopes.append({"documents": size, "doc_ids": None if size == len(rows) else sorted(
            row["doc_id"] for row in selected), "scope_sha256": digest(sorted(row["doc_id"] for row in selected)),
            "chunks": sum(row["chunk_count"] for row in selected),
            "status_counts": dict(Counter(row["status"] for row in selected))})
    return {"version": VERSION, "source_run": source_run, "source_plan_sha256": digest(source),
            "kb_id": source["kb_id"], "source_scope": source["source_scope"],
            "indexed_embedding": rows[0]["model_identity"],
            "dataset_fingerprint": source["dataset_fingerprint"],
            "bm25_corpus_limits": source["bm25_corpus_limits"],
            "scopes": scopes, "concurrency": sorted(concurrency), "cases": candidates[:questions],
            "repeats": repeats, "warmup": warmup, "seed": seed, "cloud_calls": 0,
            "scope_kind": "nested_document_subsets_in_one_kb", "quality_evaluation": False}


def load_plan(run):
    """核对整份计划摘要，禁止修改题单、预算或并发后继续混写旧结果。"""
    envelope = read_json(plain_path(run / "plan.json", run))
    plan = envelope["plan"]
    if envelope.get("sha256") != digest(plan) or plan.get("version") != VERSION or plan.get("cloud_calls") != 0:
        raise Stage36Error("PLAN_CHANGED")
    return plan


def distribution(values):
    """按最近秩计算P50/P95；无观测返回None，失败不从执行分母删除。"""
    values = sorted(value for value in values if type(value) in {int, float}
                    and math.isfinite(value) and value >= 0)
    return {"count": len(values), "mean": mean(values) if values else None,
            "p50": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None,
            "max": values[-1] if values else None}


def process_resources():
    """只读本CLI的RSS及进程生命周期峰值；不启动采样器，也不将其冒充GPU/整机内存。"""
    result = {"rss_bytes": None, "lifetime_peak_rss_bytes": None}
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                """对应Windows进程内存计数，不加载外部监控依赖。"""

                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            current = ctypes.windll.kernel32.GetCurrentProcess
            current.restype = wintypes.HANDLE
            reader = ctypes.windll.psapi.GetProcessMemoryInfo
            reader.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            reader.restype = wintypes.BOOL
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if reader(current(), ctypes.byref(counters), counters.cb):
                result.update(rss_bytes=counters.WorkingSetSize,
                              lifetime_peak_rss_bytes=counters.PeakWorkingSetSize)
        else:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result["lifetime_peak_rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
            path = Path("/proc/self/statm")
            if path.is_file():
                result["rss_bytes"] = int(path.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    return result


def safe_error(exc):
    """复用已有服务错误白名单，任意异常或伪造code不会把异常原文写入报告。"""
    if isinstance(exc, Stage36Error):
        return "STAGE36_" + exc.code
    from scripts.stage34_evaluation import safe_error as existing_error
    return existing_error(exc)


class Runtime:
    """仅显式执行时导入项目组件；单进程共享模型，每个规模只保留一个BM25索引。"""

    def __init__(self, plan):
        """仅保存空状态；快照与未开始批次核对通过后才允许加载本地模型。"""
        self.identity, self.setup_ms = None, None
        self._retriever, self._reranker = None, None

    def prepare(self, plan):
        """实际部署身份和初始化单列计时；不构造LLM、不请求云端、不建立模型副本。"""
        from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
        from app.services.embedding import get_embedding_service
        from app.services.reranking import get_reranker_service
        started = perf_counter()
        embedding, reranker = get_embedding_service(), get_reranker_service()
        self.identity = {"embedding": DKFEmbeddingAdapter().query_model_identity(),
                         "reranker": reranker.identity(),
                         "execution": {"embedding_device": embedding.options.device,
                             "embedding_batch_size": embedding.options.batch_size,
                             "reranker_device": reranker.options.device,
                             "reranker_batch_size": reranker.options.batch_size},
                         "bm25_corpus_limits": plan["bm25_corpus_limits"],
                         "python": list(sys.version_info[:3]), "platform": sys.platform,
                         "source_sha256": {path: sha_file(ROOT / path) for path in (
                             "app/core/query_performance.py", "app/core/business_logging.py",
                             "app/services/rag/search.py", "app/services/rag/database.py",
                             "app/services/rag/bm25.py", "app/services/rag/dense.py",
                             "app/services/embedding/service.py", "app/services/reranking.py",
                             "app/services/rag/llama_index/hybrid_retriever.py",
                             "app/services/rag/llama_index/dense_retriever.py",
                             "app/services/rag/llama_index/bm25_retriever.py",
                             "app/services/rag/llama_index/embedding_adapter.py",
                             "app/services/rag/llama_index/reranker_adapter.py",
                             "app/services/rag/llama_index/node_adapter.py",
                             "scripts/stage36_performance.py", "scripts/evaluate_stage36.py")}}
        self.setup_ms = (perf_counter() - started) * 1000
        if self.identity["embedding"] != plan["indexed_embedding"]:
            raise Stage36Error("INDEX_MODEL_IDENTITY")

    def validate_snapshot(self, plan):
        """测量批次前后只读核对全部索引代次，检查本身不进入查询延迟分布。"""
        from scripts.stage33_runtime import read_snapshot, scope_identity
        if scope_identity(read_snapshot(plan["kb_id"], detailed=False)) != plan["source_scope"]:
            raise Stage36Error("INDEX_CHANGED")

    def select_scope(self, plan, scope):
        """替换范围前释放上一份派生索引；所有并发请求共享此范围实例和原服务锁。"""
        from app.services.rag.bm25 import CorpusLimits
        from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
        from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
        self._retriever = None
        self._retriever = HybridRetriever(plan["kb_id"], doc_ids=scope["doc_ids"], top_k=30,
                                          corpus_limits=CorpusLimits(**plan["bm25_corpus_limits"]))
        self._reranker = BGERerankerAdapter(top_n=5)

    def search(self, plan, scope, case):
        """委托原搜索编排与完整来源校验，只把命中数返回给性能报告。"""
        from app.services.rag.search import search_knowledge_base
        results = search_knowledge_base(plan["kb_id"], case["question"], doc_ids=scope["doc_ids"],
            top_k=30, retriever=self._retriever, reranker=self._reranker)
        return len(results)


def measure_request(runtime, plan, scope, case, ordinal):
    """每个工作线程独享计时上下文；失败记录阶段及安全码，不返回正文或自动重试。"""
    row = {"case_id": case["case_id"], "ordinal": ordinal, "status": "SUCCESS"}
    with capture_query_performance() as trace:
        try:
            row["result_count"] = runtime.search(plan, scope, case)
        except Exception as exc:
            row.update(status="FAILED", error_code=safe_error(exc))
    row.update(trace.snapshot())
    if row["status"] == "FAILED" and row["failure_stage"] is None:
        row["failure_stage"] = "search"
    return row


def group_paths(run, plan):
    """生成固定批次及安全结果路径，供执行前检查与恢复共用。"""
    return [(scope, concurrency, plain_path(run / "groups" / (
        f"docs_{scope['documents']}_concurrency_{concurrency}.json"), run))
        for scope in plan["scopes"] for concurrency in plan["concurrency"]]


def check_snapshot(run, plan, runtime):
    """索引观察一旦失效就保留失效原因，不用后续成功观察覆盖旧失败。"""
    path = plain_path(run / "validity.json", run)
    if path.exists() and read_json(path).get("status") != "MATCHED":
        raise Stage36Error("PREVIOUS_INDEX_INVALID")
    try:
        runtime.validate_snapshot(plan)
    except Exception as exc:
        write_json(path, {"status": "CHANGED_OR_UNAVAILABLE", "error_code": safe_error(exc)})
        raise
    write_json(path, {"status": "MATCHED"})


def execute(run, plan, runtime, *, resume=False, limit_groups=1):
    """有界并发、逐条落盘；旧失败/中断批次只保留，续跑仅执行尚未开始的批次。"""
    from app.core.business_logging import log_event
    if type(limit_groups) is not int or not 1 <= limit_groups <= 15:
        raise Stage36Error("GROUP_LIMIT")
    pending_groups = []
    for scope, concurrency, path in group_paths(run, plan):
        if path.exists():
            if read_json(path).get("plan_sha256") != digest(plan):
                raise Stage36Error("RESULT_IDENTITY")
            if not resume:
                raise Stage36Error("RESUME_REQUIRED")
        else:
            pending_groups.append((scope, concurrency, path))
    if not pending_groups:
        return
    check_snapshot(run, plan, runtime)
    runtime.prepare(plan)
    envelope = {"identity": runtime.identity, "sha256": digest(runtime.identity)}
    identity_path = plain_path(run / "runtime.json", run)
    if identity_path.exists():
        if not resume or read_json(identity_path) != envelope:
            raise Stage36Error("RUNTIME_CHANGED_OR_RESUME_REQUIRED")
    else:
        write_json(identity_path, envelope)
    (run / "groups").mkdir(exist_ok=True)
    selected = None
    for index, (scope, concurrency, path) in enumerate(pending_groups[:limit_groups]):
        if index:
            check_snapshot(run, plan, runtime)
        value = {"group_id": path.stem, "plan_sha256": digest(plan), "status": "STARTED",
                 "scope": scope, "concurrency": concurrency, "rows": [], "warmup": [],
                 "setup_ms": runtime.setup_ms, "resources_before": process_resources(),
                 "started_at": datetime.now(UTC).isoformat(), "index_check": "NOT_RUN"}
        write_json(path, value)
        log_event("performance.started", kb_id=plan["kb_id"], stage="search",
                  source_document_count=scope["documents"], batch_size=concurrency)
        stage = "setup"
        try:
            if selected != scope["documents"]:
                runtime.select_scope(plan, scope)
                selected = scope["documents"]
            stage = "warmup"
            for ordinal in range(plan["warmup"]):
                row = measure_request(runtime, plan, scope, plan["cases"][ordinal % len(plan["cases"])], ordinal)
                value["warmup"].append(row)
                write_json(path, value)
                if row["status"] != "SUCCESS":
                    value["status"] = "WARMUP_FAILED"
                    break
            if value["status"] == "STARTED":
                stage = "measurement"
                started, cpu_started = perf_counter(), process_time()
                # 同时最多concurrency个任务；不把整轮问题及证据复制到多个进程。
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    pending = {}
                    jobs = iter((ordinal, case) for ordinal in range(plan["repeats"]) for case in plan["cases"])
                    while True:
                        while len(pending) < concurrency:
                            job = next(jobs, None)
                            if job is None:
                                break
                            ordinal, case = job
                            pending[pool.submit(measure_request, runtime, plan, scope, case, ordinal)] = True
                        if not pending:
                            break
                        future = next(as_completed(pending))
                        del pending[future]
                        value["rows"].append(future.result())
                        write_json(path, value)
                value.update(wall_seconds=perf_counter() - started,
                             cpu_seconds=process_time() - cpu_started)
                value["status"] = "FAILED" if any(r["status"] == "FAILED" for r in value["rows"]) else "COMPLETED"
        except Exception as exc:
            value.update(status="GROUP_FAILED", failure_stage=stage, error_code=safe_error(exc))
        try:
            check_snapshot(run, plan, runtime)
            value["index_check"] = "MATCHED"
        except Exception as exc:
            value.update(index_check="CHANGED_OR_UNAVAILABLE", status="INVALID_INDEX",
                         index_error_code=safe_error(exc))
        value["resources_after"] = process_resources()
        write_json(path, value)
        log_event("performance.finished", kb_id=plan["kb_id"], status=value["status"],
                  item_count=len(value["rows"]), duration_ms=value.get("wall_seconds", 0) * 1000)
        if value["status"] != "COMPLETED":
            return


def historical_rag_timings(plan):
    """只汇总阶段34已有成功记录，不调用LLM，也不混入本轮并发检索的样本。"""
    from scripts.stage34_evaluation import load_plan as source_plan, run_directory as source_directory
    source = source_directory(plan["source_run"])
    if digest(source_plan(source)) != plan["source_plan_sha256"]:
        raise Stage36Error("SOURCE_PLAN_CHANGED")
    samples = {name: [] for name in ("retrieval", "rerank", "synthesis", "llm", "total")}
    statuses = Counter()
    for path in sorted((source / "cases").glob("*.json")):
        record = read_json(plain_path(path, source))
        if record.get("plan_sha256") != plan["source_plan_sha256"]:
            raise Stage36Error("SOURCE_RESULT_CHANGED")
        statuses[record.get("status", "UNKNOWN")] += 1
        if record.get("status") != "COMPLETED":
            continue
        for name in ("retrieval", "rerank", "synthesis", "total"):
            seconds = record.get("timings_seconds", {}).get(name)
            if type(seconds) in {int, float}:
                samples[name].append(seconds * 1000)
        attempts = record.get("llm_attempts", [])
        durations = [item.get("duration_seconds") for item in attempts]
        if durations and all(type(n) in {int, float} and math.isfinite(n) and n >= 0 for n in durations):
            samples["llm"].append(sum(durations) * 1000)
    return {"source_run": plan["source_run"], "new_cloud_calls": 0,
            "historical_only": True, "status_counts": dict(statuses),
            "timings_ms": {name: distribution(values) for name, values in samples.items()}}


def report(run, plan):
    """保留未测/失败分母，分开冷启动和暖态延迟；只在自己的运行目录写摘要。"""
    groups = []
    for scope in plan["scopes"]:
        for concurrency in plan["concurrency"]:
            path = run / "groups" / f"docs_{scope['documents']}_concurrency_{concurrency}.json"
            value = read_json(plain_path(path, run)) if path.exists() else {}
            if value and value.get("plan_sha256") != digest(plan):
                raise Stage36Error("RESULT_IDENTITY")
            rows = value.get("rows", [])
            success = [row for row in rows if row["status"] == "SUCCESS"]
            failures = [row for row in rows + value.get("warmup", []) if row["status"] == "FAILED"]
            stages = sorted({key for row in rows for key in row.get("timings_ms", {})})
            wall = value.get("wall_seconds")
            groups.append({"documents": scope["documents"], "concurrency": concurrency,
                "chunks": scope["chunks"], "status_counts": scope["status_counts"],
                "status": value.get("status", "NOT_RUN"), "index_check": value.get("index_check", "NOT_RUN"),
                "planned": len(plan["cases"]) * plan["repeats"], "attempted": len(rows),
                "successful": len(success), "failed": len(rows) - len(success),
                "empty_successful": sum(row.get("result_count") == 0 for row in success),
                "all_attempt_latency_ms": distribution([row.get("total_ms") for row in rows]),
                "success_latency_ms": distribution([row.get("total_ms") for row in success]),
                "successful_queries_per_second": len(success) / wall if wall and wall > 0 else None,
                "stage_timings_ms": {stage: distribution([row.get("timings_ms", {}).get(stage)
                                          for row in success]) for stage in stages},
                "failure_stages": dict(Counter(row.get("failure_stage") for row in failures)),
                "error_codes": dict(Counter(row.get("error_code") for row in failures)),
                "group_failure_stage": value.get("failure_stage"), "group_error_code": value.get("error_code"),
                "index_error_code": value.get("index_error_code"),
                "warmup": value.get("warmup", []), "setup_ms": value.get("setup_ms"),
                "cpu_seconds": value.get("cpu_seconds"), "resources_before": value.get("resources_before"),
                "resources_after": value.get("resources_after")})
    validity_path = run / "validity.json"
    validity = read_json(plain_path(validity_path, run)) if validity_path.exists() else {"status": "NOT_RUN"}
    valid = validity["status"]
    result = {"version": VERSION, "plan_sha256": digest(plan), "groups": groups,
              "status": "MEASURED" if valid == "MATCHED" and all(g["status"] == "COMPLETED" for g in groups)
                        else "INCOMPLETE_OR_INVALID", "validity": valid,
              "index_error_code": validity.get("error_code"),
              "historical_rag": historical_rag_timings(plan), "new_cloud_calls": 0,
              "gpu_memory": "NOT_MEASURED", "ingestion_performance": "NOT_MEASURED"}
    write_json(run / "report.json", result)
    lines = ["# 阶段36性能观察", "", f"状态：{result['status']}；索引观察：{valid}；新增云调用：0。", "",
             "这是同一知识库内的嵌套文档范围对比，不是三个物理数据库的容量测试。", "",
             "| 文档数 | Chunk数 | 并发 | 状态 | 成功/计划 | 成功P50 ms | 成功P95 ms | 成功QPS |",
             "|---:|---:|---:|---|---:|---:|---:|---:|"]
    for group in groups:
        latency = group["success_latency_ms"]
        lines.append(f"| {group['documents']} | {group['chunks']} | {group['concurrency']} | {group['status']} | "
                     f"{group['successful']}/{group['planned']} | {latency['p50']} | {latency['p95']} | "
                     f"{group['successful_queries_per_second']} |")
    lines += ["", "各阶段为成功测量请求的观测分布；count不同表示该阶段未在所有请求中出现。", "",
              "| 文档数 | 并发 | 阶段 | count | P50 ms | P95 ms |", "|---:|---:|---|---:|---:|---:|"]
    for group in groups:
        for stage, values in group["stage_timings_ms"].items():
            lines.append(f"| {group['documents']} | {group['concurrency']} | {stage} | {values['count']} | "
                         f"{values['p50']} | {values['p95']} |")
    lines += ["", "失败摘要（包含预热失败；完整明细与资源快照见report.json）：", ""]
    for group in groups:
        if group["error_codes"] or group["group_error_code"] or group["index_error_code"]:
            lines.append(f"- {group['documents']}份/并发{group['concurrency']}："
                         f"阶段{group['failure_stages']}；错误{group['error_codes']}；"
                         f"批次{group['group_failure_stage']}/{group['group_error_code']}；"
                         f"索引{group['index_error_code']}。")
    lines += ["", "预热请求及模型初始化单列，不纳入暖态分位数。并发包含原共享模型锁等待；没有HTTP网络/线程池排队计时。",
              "Dense包含Query编码，fusion包含Dense/BM25，重排包含模型评分，合成包含LLM；父子阶段不能相加。",
              "RSS仅为进程批次前后快照；峰值为进程生命周期峰值，不是本批峰值或显存。",
              "各规模复用同一固定题单，未按参考答案缩小查询范围；小范围可能缺少答案，空结果和阶段覆盖需结合明细看。",
              "阶段34的历史RAG耗时单列于report.json，问题/模型/代码不同，不能与本轮并发检索相加或宣称同次完整RAG测量。",
              "不评估答案正确率，不代表阶段35已验收；未运行入库/故障注入、独立库规模或GPU显存测试。", ""]
    from app.services.parse_cache import atomic_write
    atomic_write(run / "summary.md", "\n".join(lines).encode("utf-8"))
    return result
