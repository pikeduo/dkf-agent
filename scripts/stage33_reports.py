"""从全范围输入及真实检查点生成阶段33报告；未执行项目不能伪造指标或费用。"""

from collections import Counter
from datetime import UTC, datetime
from tempfile import TemporaryFile

from scripts.stage33_common import STRATEGIES, digest, encoded, read_json, write_csv, write_json, publish_stream
from scripts.stage33_retrieval import aggregate, truth


def fee_summary(documents):
    """按Batch/Data去重文件任务；只报告可观测次数，真实金额必须由MinerU账单人工核对。"""
    confirmed, uncertain, cache = set(), set(), {"observed_hit": 0, "unknown": 0}
    piece_hits = 0
    for doc in documents:
        cache["observed_hit" if doc.get("parse_cache_hit") is True else "unknown"] += 1
        for job in doc.get("parse_jobs", []):
            piece_hits += job.get("cached_pieces_observed", 0)
            for unit in job["units"]:
                if unit.get("batch_id") and unit.get("data_id"):
                    confirmed.add((unit["batch_id"], unit["data_id"]))
                elif unit.get("submission_attempted"):
                    uncertain.add((job["job_id"], unit.get("data_id")))
    return {"confirmed_cloud_file_submissions_lifetime": len(confirmed),
        "confirmed_file_identities": sorted(confirmed),
        "submission_outcome_unknown": len(uncertain), "cache_hits": cache, "cached_pieces_observed": piece_hits,
        "confirmed_batches": sorted({batch for batch, _ in confirmed}),
        "actual_cost": None, "cost_status": "manual_provider_billing_required",
        "note": "仅无提交且done的云Job/片可证明回放；Native缓存命中无持久观测则unknown。超时可能收费，历史提交不是本run新增费用。"}


def generate(data, run, *, fatal_code=None):
    """全200文档、4087题全部列出；可重复汇总本run报告，不覆盖阶段32或其他run。"""
    from app.services.parse_cache import atomic_write
    checkpoint = run / "import_checkpoint.json"
    imported = read_json(checkpoint) if checkpoint.exists() else {}
    documents, questions, metric_rows, failures = [], [], [], []
    for doc in data["documents"]:
        state = imported.get(doc["file_id"], {})
        row = doc | state | {"execution_status": state.get("execution_status", "NOT_ATTEMPTED")}
        if doc["file_id"] in data.get("file_issues", {}):
            row.update(execution_status="INPUT_FAILED", error_code=data["file_issues"][doc["file_id"]])
        documents.append(row)
        if row["execution_status"] != "READY":
            failures.append({"kind": "document", "dataset": doc["dataset"], "identity": doc["file_id"],
                "status": row["execution_status"], "error_code": row.get("error_code"),
                "adapter_diagnostic": row.get("adapter_diagnostic", {}),
                "parser_reason_codes": row.get("parser_reason_codes", [])})
    validity = read_json(run / "evaluation_validity.json") if (run / "evaluation_validity.json").exists() else {}
    config = read_json(run / "evaluation_identity.json") if (run / "evaluation_identity.json").exists() else None
    executions, hits_buffer = [], TemporaryFile(dir=run)
    hit_csv = TemporaryFile(mode="w+", encoding="utf-8", newline="", dir=run)
    import csv
    hit_fields = ["dataset", "qa_id", "strategy", "rank", "chunk_id", "doc_id", "file_id", "file_name",
        "page_start", "page_end", "source_pages", "section", "score", "retrieval_score", "fusion_score", "rerank_score"]
    writer = csv.DictWriter(hit_csv, fieldnames=hit_fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for qa in data["questions"]:
        path = run / "cases" / (digest([qa["dataset"], qa["qa_id"]]) + ".json")
        result = read_json(path) if path.exists() else {"dataset": qa["dataset"], "qa_id": qa["qa_id"],
            "truth": truth(qa), "strategies": {s: {"status": "NOT_EXECUTED", "hits": [],
                "metrics": None, "document_proxy_metrics": None} for s in STRATEGIES}}
        if path.exists() and (result.get("qa_sha256") != digest(qa) or config is None
                             or result.get("evaluation_identity") != digest(config)):
            raise ValueError("STAGE33_CASE_IDENTITY")
        # 汇总仅保留小型指标；全部证据明细逐题流式写临时文件，不占用数GB内存。
        questions.append(result | {"strategies": {s: {k: v for k, v in r.items() if k != "hits"}
                                                   for s, r in result["strategies"].items()}})
        # 官方行/断言原结构完整保留，下一阶段不必重新猜测QA或文件关联。
        execution = qa | {"retrieval_status": {s: r["status"] for s, r in result["strategies"].items()},
            "scoring_eligibility": result["truth"], "doc_ids": [imported.get(fid, {}).get("doc_id")
                for fid in qa["source_file_ids"]], "dependency_status": {
                    fid: imported.get(fid, {}).get("execution_status", "NOT_ATTEMPTED") for fid in qa["source_file_ids"]}}
        executions.append(execution)
        for strategy, value in result["strategies"].items():
            metric_rows.append({"dataset": qa["dataset"], "qa_id": qa["qa_id"], "strategy": strategy,
                "source_file_ids": qa["source_file_ids"], "doc_ids": execution["doc_ids"], "status": value["status"],
                "scorable": result["truth"]["scorable"], "manual_pending": result["truth"]["manual_pending"],
                "error_code": value.get("error_code") or fatal_code, "duration_seconds": value.get("duration_seconds"),
                **(value.get("metrics") or {})})
            hits_buffer.write(encoded({"dataset": qa["dataset"], "qa_id": qa["qa_id"], "strategy": strategy,
                "status": value["status"], "hits": value["hits"]}) + b"\n")
            for hit in value["hits"]:
                writer.writerow({**{k: v for k, v in hit.items() if k in hit_fields},
                    "source_pages": encoded(hit["source_pages"]).decode(), "dataset": qa["dataset"],
                    "qa_id": qa["qa_id"], "strategy": strategy})
            if value["status"] != "SUCCESS" or not result["truth"]["scorable"]:
                failures.append({"kind": "qa", "dataset": qa["dataset"], "identity": qa["qa_id"],
                    "strategy": strategy, "status": value["status"],
                    "error_code": value.get("error_code") or result["truth"]["basis"]})
    metrics = {group: {s: aggregate([q for q in questions if group == "overall" or q["dataset"] == group], s)
                      for s in STRATEGIES} for group in ("ohr", "ocb", "overall")}
    doc_ready = sum(r["execution_status"] == "READY" for r in documents)
    attempted = sum(bool(r.get("doc_id") or r.get("upload_intent")) for r in documents)
    all_success = all(v["status"] == "SUCCESS" for q in questions for v in q["strategies"].values())
    report = {"version": "stage33-full-v1", "updated_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if doc_ready == len(documents) and all_success and validity.get("status") == "VALID"
                  and fatal_code is None else "INCOMPLETE", "fatal_code": fatal_code,
        "fixed_document_count": len(documents), "fixed_qa_count": len(questions),
        "qa_strategy_records": len(questions) * len(STRATEGIES),
        "import_attempted_or_reused": attempted, "ready": doc_ready,
        "dataset_formats": {d: dict(Counter(r["file_type"] for r in documents if r["dataset"] == d))
                            for d in ("ohr", "ocb")}, "evaluation_validity": validity,
        "metrics": metrics, "fees": fee_summary(documents),
        "limits": ["PASS仅指全范围流程/检索执行完成，不代表每题召回成功或阶段34问答准确率",
                   "OCB无官方Chunk/页相关性标签，证据Recall不可评分；单列官方reference_files文档代理指标",
                   "OCB expected_assertions全部保持人工待审，未调用LLM/未自动判定答案正确"]}
    baseline = read_json(run / "baseline.json") if (run / "baseline.json").exists() else None
    report["fees"]["newly_observed_cloud_file_submissions_this_run"] = (
        len({tuple(v) for v in report["fees"]["confirmed_file_identities"]}
            - {tuple(v) for v in baseline["confirmed_cloud_files"]}) if baseline is not None else None)
    # 变化或中断快照不能发布为有效正式指标；保留逐题事实便于审计/后续恢复。
    if validity.get("status") != "VALID":
        report["metrics_status"] = "NOT_CERTIFIED"
        for group in metrics.values():
            for value in group.values():
                value["diagnostic_uncertified_metrics"] = value["metrics"]
                value["metrics"] = {key: None for key in value["metrics"]}
                value["document_proxy_metrics"] = {key: None for key in value["document_proxy_metrics"]}
    else:
        report["metrics_status"] = "VALID_EXECUTION_RECORDS"
    write_json(run / "import_report.json", {"documents": documents})
    write_csv(run / "import_report.csv", documents, ["dataset", "file_id", "file_path", "file_type", "sha256",
        "doc_id", "execution_status", "status", "error_code", "adapter_diagnostic", "parser_reason_codes", "route_strategy", "block_count", "chunk_count", "vector_count",
        "invalid_vector_count", "management", "parse_jobs", "index_identity"])
    atomic_write(run / "qa_execution.jsonl", b"".join(encoded(r) + b"\n" for r in executions))
    write_csv(run / "qa_execution.csv", metric_rows, ["dataset", "qa_id", "strategy", "source_file_ids", "doc_ids", "status",
        "scorable", "manual_pending", "error_code", "duration_seconds", "recall_at_5", "recall_at_10", "mrr_at_20"])
    publish_stream(run / "retrieval_results.jsonl", hits_buffer)
    hit_csv.flush()
    publish_stream(run / "retrieval_results.csv", hit_csv)
    hits_buffer.close()
    hit_csv.close()
    write_json(run / "failures_and_unscorable.json", failures)
    write_csv(run / "failures_and_unscorable.csv", failures, ["kind", "dataset", "identity", "strategy", "status", "error_code", "adapter_diagnostic", "parser_reason_codes"])
    write_json(run / "metrics.json", metrics)
    for name in ("ohr", "ocb", "overall"):
        write_json(run / f"metrics_{name}.json", metrics[name])
    write_json(run / "costs_and_timing.json", {"fees": report["fees"], "strategies": metrics,
        "runtime": read_json(run / "timing.json") if (run / "timing.json").exists() else None})
    write_json(run / "summary.json", report)
    summary_rows = [{"dataset": d, "strategy": s, **r, **r["metrics"],
        **{"document_" + k: v for k, v in r["document_proxy_metrics"].items()}}
        for d, values in metrics.items() for s, r in values.items()]
    write_csv(run / "strategy_comparison.csv", summary_rows, ["dataset", "strategy", "total_questions",
        "executed_successfully", "failed", "not_executed", "scorable_denominator", "successfully_scored",
        "unscorable", "manual_pending", "recall_at_5", "recall_at_10", "mrr_at_20", "document_proxy_metrics",
        "document_recall_at_5", "document_recall_at_10", "document_mrr_at_20", "latency_seconds"])
    text = ["# 阶段33全量导入与检索", "", f"状态：{report['status']}；文档 {doc_ready}/{len(documents)} READY；QA范围 {len(questions)}。",
        "", "| 数据集 | 策略 | 成功执行 | 可评分总分母 | 成功评分 | 不可评分 | Recall@5 | Recall@10 | MRR@20 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in summary_rows:
        text.append("| " + " | ".join(str(row.get(k)) for k in ("dataset", "strategy", "executed_successfully",
            "scorable_denominator", "successfully_scored", "unscorable", "recall_at_5", "recall_at_10", "mrr_at_20")) + " |")
    text += ["", "指标分母不按成功子集缩小；未全量执行/快照无效时不可宣称正式评测通过。",
        "文档代理指标见JSON/CSV，不代表OCB断言正确或LLM问答准确率。金额须人工核对账单。"]
    atomic_write(run / "summary.md", ("\n".join(text) + "\n").encode("utf-8"))
    return report
