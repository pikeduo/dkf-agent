"""阶段34固定题单、逐题检查点和人工质量评分；不另建检索或问答实现。"""

import math
import re
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from statistics import mean
from time import perf_counter
from uuid import UUID

from scripts.stage33_common import ROOT, digest, plain_path, read_json, write_json

VERSION = "stage34-rag-v1"
METRICS = ("answer_accuracy", "faithfulness", "citation_accuracy")
TAGS = {"fact", "numeric", "unit", "table", "formula", "ambiguity", "insufficient_evidence"}


class Stage34Error(ValueError):
    """只向终端传递固定类别，不附带问题、证据或上游异常原文。"""

    def __init__(self, code):
        """保存本工具内部安全错误类别。"""
        self.code = code
        super().__init__("STAGE34_" + code)


def run_directory(name):
    """将运行名限定在独立结果根，拒绝路径跳转及链接。"""
    if not re.fullmatch(r"stage34_run_[0-9]+", name):
        raise Stage34Error("RUN_NAME")
    return plain_path(ROOT / "results/stage34" / name, ROOT / "results/stage34")


@contextmanager
def run_lock(run):
    """同一运行只允许一个写入者；仅复用已有文件锁，不操作业务服务。"""
    from app.core.business_logging import lock_file
    run.mkdir(parents=True, exist_ok=True)
    with (run / "run.lock").open("a+b") as stream:
        try:
            lock_file(stream.fileno())
        except OSError:
            raise Stage34Error("RUN_BUSY") from None
        try:
            yield
        finally:
            lock_file(stream.fileno(), unlock=True)


def llm_policy(settings=None):
    """只读当前主备开关以冻结单题请求上界；不创建服务、校验密钥或联网。"""
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    fallback = settings.llm_fallback_enabled
    return {"fallback": fallback, "llm_calls_per_case_max": 2 if fallback else 1}


def make_plan(source_run, *, single_count=10, office_count=5, seed=34, settings=None):
    """从阶段33按身份固定选题，冻结现有主备策略；不读取检索成绩或发送请求。"""
    from scripts.stage33_common import load_dataset, select_questions
    from scripts.stage33_runtime import check_scope, scope_identity
    if not re.fullmatch(r"stage33_run_[0-9]+", source_run) or not 10 <= single_count <= 50:
        raise Stage34Error("PLAN_ARGUMENTS")
    if type(office_count) is not int or not 1 <= office_count <= 50:
        raise Stage34Error("PLAN_ARGUMENTS")
    source = plain_path(ROOT / "results/stage33" / source_run, ROOT / "results/stage33")
    identity, selection = read_json(source / "run.json"), read_json(source / "qa_selection.json")
    # 只核对已有文件身份/大小和QA元数据，不解析、转换原件或调用数据库。
    data = select_questions(load_dataset(verify_files=False), per_dataset=selection["per_dataset"],
                            seed=selection["seed"])
    snapshot = read_json(source / "index_snapshot.json")
    rows = {row["file_hash"]: row for row in snapshot["documents"].values()}
    if (selection != data["qa_selection"] or identity["dataset_fingerprint"] != data["fingerprint"]
            or snapshot["dataset_fingerprint"] != data["fingerprint"]
            or snapshot["kb_id"] != identity["kb_id"] or scope_identity(rows) != snapshot["scope"]):
        raise Stage34Error("SOURCE_IDENTITY")
    check_scope(data, rows, identity["kb_id"], ready=True, allow_partial_ready=True)
    documents = {d["file_id"]: dict(d, doc_id=rows[d["sha256"]]["doc_id"]) for d in data["documents"]}

    def rank(qa):
        """只用种子及官方身份排序，避免按答案、格式或成功案例挑题。"""
        return digest([VERSION, seed, qa["dataset"], qa["qa_id"]])

    ordered = sorted(data["questions"], key=rank)
    singles, seen = [], set()
    for qa in ordered:
        ids = qa["source_file_ids"]
        if qa["dataset"] == "ohr" and len(ids) == 1 and ids[0] not in seen:
            singles.append(qa)
            seen.add(ids[0])
        if len(singles) == single_count:
            break
    # 多文档是显式范围分层，不依据检索结果选题；保留Office单文档作为不足时的后续样本。
    office_pool = [qa for qa in ordered if qa["dataset"] == "ocb"]
    offices = ([qa for qa in office_pool if len(qa["source_file_ids"]) > 1]
               + [qa for qa in office_pool if len(qa["source_file_ids"]) == 1])[:office_count]
    if len(singles) != single_count or len(offices) != office_count:
        raise Stage34Error("SAMPLE_TOO_SMALL")
    cases = []

    def add(qa, scope, ids, purpose):
        """冻结官方依据与执行范围；参考答案只留本地，运行时只传问题和范围。"""
        control = purpose == "scope_exclusion_control"
        cases.append({"case_id": digest([VERSION, qa["dataset"], qa["qa_id"], scope, purpose])[:24],
            "dataset": qa["dataset"], "qa_id": qa["qa_id"], "question": qa["question"],
            "scope": scope, "doc_ids": ids, "purpose": purpose,
            "expected_behavior": "unknown" if control or qa["dataset"] == "ocb" else "answerable",
            "reference_basis": "scope_control_requires_manual_review" if control else (
                "official_answer" if qa["dataset"] == "ohr" else "official_assertions_require_manual_review"),
            "reference_answer": None if control else qa.get("expected_answer"),
            "reference_assertions": None if control else qa.get("expected_assertions"),
            "official_pages": qa.get("evidence_page"), "source_file_ids": qa["source_file_ids"],
            "qa_sha256": digest(qa)})

    for qa in singles + offices:
        ids = [documents[fid]["doc_id"] for fid in qa["source_file_ids"]]
        add(qa, "single" if len(ids) == 1 else "multi", ids, "official_question")
    for qa in (singles + offices)[:3]:
        add(qa, "knowledge_base", None, "scope_comparison")
    for qa in singles[:2]:
        # 排除官方依赖只是拒答对照，不证明其他文档绝无答案，必须人工判断。
        other = next(d for fid, d in sorted(documents.items()) if fid not in qa["source_file_ids"])
        add(qa, "single", [other["doc_id"]], "scope_exclusion_control")
    backend = read_json(source / "evaluation_identity.json")["backend"]
    return {"version": VERSION, "source_run": source_run, "kb_id": identity["kb_id"], "seed": seed,
        "dataset_fingerprint": data["fingerprint"], "source_scope": snapshot["scope"],
        "bm25_corpus_limits": backend["bm25_corpus_limits"],
        "documents": documents, "index_snapshot": snapshot, "cases": cases,
        "policy": {"single_count": single_count, "office_count": office_count,
            **llm_policy(settings), "automatic_judge": False,
            "selection": "identity_hash_without_retrieval_scores"}}


def plan_id(plan):
    """对完整冻结计划生成身份，包括问题、参考依据、范围和索引质量。"""
    return digest(plan)


def load_plan(run):
    """读取有完整身份封装的计划，避免编辑题目后混用旧回答。"""
    envelope = read_json(run / "plan.json")
    if envelope.get("sha256") != plan_id(envelope["plan"]) or envelope["plan"]["version"] != VERSION:
        raise Stage34Error("PLAN_CHANGED")
    return envelope["plan"]


def safe_error(exc):
    """只接受已定义服务的错误码，不记录异常消息、SQL或提供方响应。"""
    if isinstance(exc, Stage34Error):
        return "STAGE34_" + exc.code
    from scripts.stage33_common import Stage33Error
    if isinstance(exc, Stage33Error):
        return "STAGE33_" + exc.code
    from app.services.llm import ERRORS as llm_errors
    from app.services.deepseek import ERRORS as deepseek_errors
    from app.services.rag.synthesis import ERRORS as rag_errors
    from app.services.rag.evidence import ERRORS as evidence_errors
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in set(llm_errors) | set(deepseek_errors) | set(rag_errors) | set(evidence_errors) else "EVALUATION_RUNTIME"


def case_path(run, case):
    """只接受固定题目Hash作文件名，不把外部QA标识拼进路径。"""
    if not re.fullmatch(r"[0-9a-f]{24}", case["case_id"]):
        raise Stage34Error("CASE_ID")
    return plain_path(run / "cases" / (case["case_id"] + ".json"), run)


def load_records(run, plan):
    """读取各题原子结果并核对计划绑定；中断题保持STARTED，绝不自动再次付费。"""
    records = {}
    for case in plan["cases"]:
        path = case_path(run, case)
        if path.exists():
            value = read_json(path)
            if value.get("plan_sha256") != plan_id(plan) or value.get("case_id") != case["case_id"]:
                raise Stage34Error("RESULT_IDENTITY")
            records[case["case_id"]] = value
    return records


def make_retry_plan(source_run):
    """只读提取尚未发起LLM的检索失败题到新计划，保留原问题、范围、策略及索引身份。"""
    source = run_directory(source_run)
    plan = load_plan(source)
    records = load_records(source, plan)
    cases, failures = [], {}
    for case in plan["cases"]:
        record = records.get(case["case_id"], {})
        # 缺请求账本、已发/未知请求、后置索引变化均不在安全补跑范围内。
        if (record.get("status") == "FAILED" and record.get("error_code") == "RAG_RETRIEVAL"
                and record.get("failure_stage") == "retrieval" and record.get("index_check") == "MATCHED"
                and record.get("llm_attempted") is False and record.get("llm_attempts") == []
                and record.get("actual_provider") is None and record.get("actual_model") is None
                and record.get("raw_llm_output") is None and record.get("final_evidence") == []):
            cases.append(case)
            failures[case["case_id"]] = digest(record)
    if not cases:
        raise Stage34Error("NO_UNPAID_RETRIEVAL_FAILURES")
    return {**plan, "cases": cases, "retry_source": {"run": source_run,
        "plan_sha256": plan_id(plan), "planned": len(plan["cases"]), "failed_results_sha256": failures}}


def request_count(record):
    """按逐请求意图计数；旧单次请求记录仍可离线报告，不改写原结果。"""
    if "llm_attempts" in record:
        return len(record["llm_attempts"])
    return int(bool(record.get("llm_attempted", False)))


def reserved_calls(records, per_case):
    """中断题保守占完整单题上界，其他题按已保存意图计数，不把预算当成实际收费。"""
    return sum(max(per_case, request_count(r)) if r["status"] == "STARTED" else request_count(r)
               for r in records.values())


def execute(run, plan, runtime, *, max_cloud_calls, limit):
    """串行执行新题；主备分别落盘意图及计预算，失败/中断题永不自动重发。"""
    records = load_records(run, plan)
    pending = [c for c in plan["cases"] if c["case_id"] not in records][:limit]
    policy = {name: plan["policy"][name] for name in ("fallback", "llm_calls_per_case_max")}
    per_case = policy["llm_calls_per_case_max"]
    if (type(policy["fallback"]) is not bool or type(per_case) is not int
            or per_case != (2 if policy["fallback"] else 1)):
        raise Stage34Error("LLM_POLICY_CHANGED")
    reserved = reserved_calls(records, per_case)
    if max_cloud_calls < reserved + len(pending) * per_case:
        raise Stage34Error("CLOUD_BUDGET")
    if not pending:
        return records
    if runtime.cloud_policy() != policy:
        raise Stage34Error("LLM_POLICY_CHANGED")
    runtime.validate_snapshot(plan)
    identity = runtime.identity()
    identity_path = run / "runtime.json"
    if identity_path.exists() and read_json(identity_path) != identity:
        raise Stage34Error("RUNTIME_CHANGED")
    write_json(identity_path, identity)
    for case in pending:
        runtime.validate_snapshot(plan)
        path = case_path(run, case)
        value = {"case_id": case["case_id"], "plan_sha256": plan_id(plan), "status": "STARTED",
            "started_at": datetime.now(UTC).isoformat(), "llm_attempted": False,
            "llm_attempts": [], "runtime_sha256": digest(identity)}
        write_json(path, value)

        def before_cloud(*, provider, model):
            """每方最多一次且先落盘；新增意图写入失败不计已发送，也不得发出HTTP。"""
            nonlocal reserved
            ordinal = len(value["llm_attempts"])
            providers = [identity["provider"]] + (["deepseek"] if policy["fallback"] else [])
            if ordinal >= per_case or provider != providers[ordinal]:
                raise Stage34Error("DUPLICATE_CLOUD_CALL")
            if reserved >= max_cloud_calls:
                raise Stage34Error("CLOUD_BUDGET")
            attempt = {"ordinal": ordinal + 1, "provider": provider, "model": model,
                "status": "STARTED", "started_at": datetime.now(UTC).isoformat()}
            updated = dict(value, llm_attempted=True, llm_attempts=[*value["llm_attempts"], attempt])
            write_json(path, updated)
            value.update(updated)
            reserved += 1
            return attempt

        started = perf_counter()
        try:
            payload = runtime.query(plan["kb_id"], case["question"], doc_ids=case["doc_ids"],
                                    before_cloud=before_cloud)
            value.update(payload, status="COMPLETED")
        except Exception as exc:
            value.update(status="FAILED", error_code=safe_error(exc),
                failure_stage=runtime.stage, timings_seconds=dict(runtime.timings),
                final_evidence=list(runtime.final_evidence), raw_llm_output=runtime.raw_output)
        responses = [a for a in value["llm_attempts"] if a["status"] == "COMPLETED"]
        value["actual_provider"] = responses[-1]["provider"] if responses else None
        value["actual_model"] = responses[-1]["model"] if responses else None
        value["timings_seconds"]["total"] = perf_counter() - started
        # 不覆盖已取得的答案；后置来源/快照失败单独标记，不通过重问修复。
        try:
            runtime.validate_snapshot(plan)
            value["index_check"] = "MATCHED"
        except Exception as exc:
            value["index_check"] = "CHANGED_OR_UNAVAILABLE"
            value["index_error_code"] = safe_error(exc)
        write_json(path, value)
        records[case["case_id"]] = value
        print(f"CASE：{len(records)}/{len(plan['cases'])}；{value['status']}")
        if (value["index_check"] != "MATCHED" or value.get("error_code", "").startswith(("LLM_", "DEEPSEEK_"))
                or value.get("status") == "FAILED" and value.get("failure_stage") == "llm"
                or value.get("source_check", {}).get("status") == "FAILED"):
            break
    return records


def make_review(case, record):
    """生成未评分表；每项保留分子/分母，空值不是零分或通过。"""
    return {"case_id": case["case_id"], "result_sha256": digest(record), "reviewer": "",
        "expected_behavior": case["expected_behavior"], "tags": [], "notes": "",
        **{name: {"passed": None, "total": None} for name in METRICS}}


def validate_review(review, case, record):
    """人工判断绑定完整原结果；拒答不计忠实度或引用准确率，防止空答案拉高指标。"""
    if (review.get("case_id") != case["case_id"] or review.get("result_sha256") != digest(record)
            or review.get("expected_behavior") not in {"answerable", "unanswerable", "unknown"}
            or not isinstance(review.get("tags"), list) or set(review["tags"]) - TAGS):
        raise Stage34Error("REVIEW_IDENTITY")
    answer = record.get("answer") or {}
    for name in METRICS:
        metric = review.get(name, {})
        if not isinstance(metric, dict) or set(metric) != {"passed", "total"}:
            raise Stage34Error("REVIEW_SCORE")
        passed, total = metric.get("passed"), metric.get("total")
        if passed is None and total is None:
            continue
        if (record["status"] != "COMPLETED" or type(passed) is not int or type(total) is not int
                or not 0 <= passed <= total or total < 1 or not review.get("reviewer", "").strip()
                or not review.get("notes", "").strip()):
            raise Stage34Error("REVIEW_SCORE")
        if name == "answer_accuracy" and (total != 1 or review["expected_behavior"] == "unknown"):
            raise Stage34Error("REVIEW_ANSWER_BASIS")
        if name != "answer_accuracy" and answer.get("insufficient_evidence"):
            raise Stage34Error("REFUSAL_NOT_FAITHFULNESS_SAMPLE")


def quality_metrics(reviews):
    """只累计已人工评分的分子分母；支持数据集/范围分组，不用成功执行自动补分。"""
    result = {}
    for name in METRICS:
        scored = [r[name] for r in reviews if r[name]["total"] is not None]
        passed, total = sum(r["passed"] for r in scored), sum(r["total"] for r in scored)
        result[name] = {"passed": passed, "total": total, "rate": passed / total if total else None,
                        "reviewed_cases": len(scored)}
    return result


def summarize(plan, records, reviews):
    """分别报告执行、人工指标及来源检查；补跑保留原计划关联，不混算不同代码的成绩。"""
    cases = {c["case_id"]: c for c in plan["cases"]}
    if len({r["case_id"] for r in reviews}) != len(reviews) or any(r["case_id"] not in records for r in reviews):
        raise Stage34Error("REVIEW_SET")
    for review in reviews:
        validate_review(review, cases[review["case_id"]], records[review["case_id"]])
    completed = [r for r in records.values() if r["status"] == "COMPLETED"]
    metrics = quality_metrics(reviews)
    grouped = {}
    for field in ("dataset", "scope"):
        grouped[field] = {value: {"planned": sum(c[field] == value for c in cases.values()),
            "metrics": quality_metrics([r for r in reviews if cases[r["case_id"]][field] == value])}
            for value in sorted({c[field] for c in cases.values()})}
    timings = {}
    for stage in ("retrieval", "rerank", "synthesis", "evidence_verification", "total"):
        values = sorted(r.get("timings_seconds", {}).get(stage) for r in completed
                        if r.get("timings_seconds", {}).get(stage) is not None)
        timings[stage] = {"count": len(values), "mean": mean(values) if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None}
    states = Counter(r["status"] for r in records.values())
    refused = sum(bool((r.get("answer") or {}).get("insufficient_evidence")) for r in completed)
    matched = sum(r.get("index_check") == "MATCHED" for r in records.values())
    verified = sum(r.get("source_check", {}).get("status") == "VERIFIED" for r in completed)
    pending = {name: (len(completed) if name == "answer_accuracy" else len(completed) - refused)
               - metrics[name]["reviewed_cases"] for name in METRICS}
    source_failed = sum(r.get("source_check", {}).get("status") == "FAILED" for r in completed)
    status = "EXECUTION_INCOMPLETE" if len(completed) != len(cases) else (
        "MANUAL_REVIEW_PENDING" if any(pending.values()) else "REVIEWED_WITH_LIMITATIONS")
    return {"status": status, "planned": len(cases), "completed": len(completed),
        "failed": states["FAILED"], "interrupted": states["STARTED"], "not_attempted": len(cases) - len(records),
        "execution_complete": len(completed) == len(cases), "refusals": refused,
        "llm_attempts_recorded": sum(request_count(r) for r in records.values()),
        "llm_attempts_uncertain": sum(sum(a["status"] == "STARTED" for a in r["llm_attempts"])
            if "llm_attempts" in r else int(r["status"] == "STARTED") for r in records.values()),
        "llm_budget_reserved": reserved_calls(records, plan["policy"]["llm_calls_per_case_max"]),
        "llm_requests_by_provider": dict(Counter(a["provider"] for r in records.values()
                                                 for a in r.get("llm_attempts", []))),
        "llm_response_cases_by_provider": dict(Counter(r["actual_provider"] for r in records.values()
                                                       if r.get("actual_provider"))),
        "fallback_cases": sum(request_count(r) > 1 for r in records.values()), "actual_cost": None,
        "post_index_matched": matched, "source_verified_answers": verified,
        "source_failed_answers": source_failed, "manual_pending_cases": pending,
        "snapshot_limitations": any(r.get("index_check") != "MATCHED" for r in records.values()),
        "manual_metrics": metrics, "timings_seconds_completed_cases": timings,
        **({"retry_source": plan["retry_source"]} if "retry_source" in plan else {}),
        "manual_metrics_by_group": grouped,
        "scope_counts": dict(Counter(c["scope"] for c in cases.values())),
        "tag_review_counts": dict(Counter(t for r in reviews for t in r["tags"])),
        "partial_ready_documents": sum(d["status"] == "PARTIAL_READY"
            for d in plan["index_snapshot"]["documents"].values()),
        "unknown_parse_quality_documents": sum(not d.get("parse_quality")
            for d in plan["index_snapshot"]["documents"].values()),
        "notes": ["人工指标仅针对已评分样本，不代表全部计划或原4087题。",
            "来源校验不代表语义正确；未评分为null，拒答不进入忠实度/引用率分母。",
            "受限范围对照并不保证不可回答，OCB断言不当成标准答案；金额人工核对账单。"]}


def report(run, plan):
    """离线汇总请求、来源及评分；补跑显示原运行与独立分母，不改旧记录或访问云端。"""
    records = load_records(run, plan)
    review_path = run / "reviews.json"
    reviews = read_json(review_path) if review_path.exists() else []
    known = {r["case_id"] for r in reviews}
    reviews += [make_review(c, records[c["case_id"]]) for c in plan["cases"]
                if c["case_id"] in records and c["case_id"] not in known
                and records[c["case_id"]]["status"] != "STARTED"]
    result = summarize(plan, records, reviews)
    write_json(review_path, reviews)
    write_json(run / "summary.json", result)
    lines = ["# 阶段34 RAG质量评测", "", f"状态：{result['status']}。",
        f"计划 {result['planned']}；完成 {result['completed']}；失败 {result['failed']}；"
        f"中断 {result['interrupted']}；未执行 {result['not_attempted']}；拒答 {result['refusals']}。", "",
        f"请求意图 {result['llm_attempts_recorded']}；预算占额 {result['llm_budget_reserved']}；"
        f"备用题数 {result['fallback_cases']}；逐方请求 {result['llm_requests_by_provider']}。", "",
        "| 人工指标 | 通过/总数 | 比率 | 已评题数 |", "|---|---:|---:|---:|"]
    if "retry_source" in plan:
        source = plan["retry_source"]
        lines[2:2] = [f"补跑来源：{source['run']}（原计划{source['planned']}题）；"
            f"本报告只含{len(plan['cases'])}道未发起LLM的检索失败题，原运行结果不变。", ""]
    for name, metric in result["manual_metrics"].items():
        rate = f"{metric['rate']:.2%}" if metric["rate"] is not None else "待评分"
        lines.append(f"| {name} | {metric['passed']}/{metric['total']} | {rate} | {metric['reviewed_cases']} |")
    lines += ["", *result["notes"], "", "完整范围/耗时/来源核对见summary.json；逐题答案与最终证据见cases。"]
    from app.services.parse_cache import atomic_write
    atomic_write(run / "summary.md", ("\n".join(lines) + "\n").encode("utf-8"))
    packet = ["# 本地人工评审材料（含问题、答案与原文，请勿公开）", "",
        "逐题核对原件、引用和最终证据，在reviews.json填写评分；不得只凭结构通过给满分。", ""]
    for case in plan["cases"]:
        record = records.get(case["case_id"], {})
        reference = case.get("reference_answer")
        if reference is None:
            reference = case.get("reference_assertions")
        packet += [f"## {case['case_id']} / {case['scope']} / {record.get('status', 'NOT_ATTEMPTED')}",
            "", case["question"], "", f"依据：{case['reference_basis']}；目的：{case['purpose']}",
            "", "参考答案/原始断言（OCB须人工解释）：", "",
            str(reference) if reference is not None else "待人工确定",
            "", "生成回答：", "", (record.get("answer") or {}).get("answer", "未取得有效回答"),
            "", f"来源核对：{record.get('source_check', {}).get('status', '未执行')}；"
            f"索引核对：{record.get('index_check', '未执行')}", ""]
        cited = {c["chunk_id"]: c["citation_id"] for c in (record.get("answer") or {}).get("citations", [])}
        for evidence in record.get("final_evidence", []):
            marker = cited.get(evidence["chunk_id"], "未引用")
            semantics = evidence.get("metadata", {}).get("page_semantics", "未知")
            packet += [f"### 引用 {marker} / {evidence['file_name']}",
                f"Chunk：{evidence['chunk_id']}；页：{evidence['page_start']}–{evidence['page_end']}；"
                f"页语义：{semantics}；章节：{evidence.get('section')}", "", evidence["text"], ""]
    atomic_write(run / "review_packet.md", ("\n".join(packet) + "\n").encode("utf-8"))
    return result
