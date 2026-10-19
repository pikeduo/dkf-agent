"""从全范围输入及真实检查点生成阶段33报告；未执行项目不能伪造指标或费用。"""

from collections import Counter
from datetime import UTC, datetime
from tempfile import TemporaryFile

from scripts.stage33_common import (STRATEGIES, Stage33Error, digest, encoded, read_json, write_csv,
                                   write_json, publish_stream)
from scripts.stage33_analysis import diagnose_retrieval, gate_metrics, grouped_metrics, question_context
from scripts.stage33_retrieval import aggregate, truth
from scripts.stage33_runtime import is_indexed


def report_snapshot(data, run, imported):
    """报告优先采用显式数据库观察，旧导入意图只作历史保留，不写回原检查点。"""
    path = run / "index_snapshot.json"
    if not path.exists():
        return imported, None
    snapshot = read_json(path)
    if snapshot.get("superseded_by_import") is True:
        return imported, None
    expected = {doc["file_id"] for doc in data["documents"]}
    if (snapshot.get("dataset_fingerprint") != data["fingerprint"]
            or set(snapshot.get("documents", {})) != expected):
        raise Stage33Error("INDEX_SNAPSHOT_IDENTITY")
    run_path = run / "run.json"
    if run_path.exists() and read_json(run_path).get("kb_id") != snapshot.get("kb_id"):
        raise Stage33Error("INDEX_SNAPSHOT_IDENTITY")
    # 当前库已不存在的记录不能继续继承旧doc_id、旧成功计数或旧请求错误。
    return snapshot["documents"], snapshot


def fee_summary(documents):
    """按Batch/Data去重已观察任务；快速索引视图未查询费用历史时明确标未知，不能当零收费。"""
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
        "documents_with_unobserved_job_history": sum(doc.get("parse_job_history_observed") is False
                                                     for doc in documents),
        "note": "仅无提交且done的云Job/片可证明回放；Native缓存命中无持久观测则unknown。超时可能收费，历史提交不是本run新增费用。"}


def generate(data, run, *, fatal_code=None):
    """保留完整文档库及本run选题，区分抽样/全量成绩；不访问数据库、模型或云端。"""
    from app.services.parse_cache import atomic_write
    checkpoint = run / "import_checkpoint.json"
    imported = read_json(checkpoint) if checkpoint.exists() else {}
    imported, snapshot = report_snapshot(data, run, imported)
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
                "retry_http_status": row.get("retry_http_status"),
                "retry_request_error": row.get("retry_request_error"),
                "adapter_diagnostic": row.get("adapter_diagnostic", {}),
                "parser_reason_codes": row.get("parser_reason_codes", [])})
    validity = read_json(run / "evaluation_validity.json") if (run / "evaluation_validity.json").exists() else {}
    config = read_json(run / "evaluation_identity.json") if (run / "evaluation_identity.json").exists() else None
    valid = (fatal_code is None and validity.get("status") == "VALID" and config is not None
        and validity.get("scope") == config.get("scope")
        and (snapshot is None or snapshot["scope"] == config.get("scope")))
    by_file = {doc["file_id"]: doc for doc in documents}
    diagnostics, diagnostic_counts = [], Counter()
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
        context = question_context(qa, by_file)
        questions.append(result | {"context": context, "strategies": {s: {k: v for k, v in r.items() if k != "hits"}
                                                   for s, r in result["strategies"].items()}})
        # 官方行/断言原结构完整保留，下一阶段不必重新猜测QA或文件关联。
        execution = qa | {"retrieval_status": {s: r["status"] for s, r in result["strategies"].items()},
            "scoring_eligibility": result["truth"], "retrieval_context": context,
            "doc_ids": [imported.get(fid, {}).get("doc_id")
                for fid in qa["source_file_ids"]], "dependency_status": {
                    fid: imported.get(fid, {}).get("execution_status", "NOT_ATTEMPTED") for fid in qa["source_file_ids"]}}
        executions.append(execution)
        for strategy, value in result["strategies"].items():
            diagnostic = diagnose_retrieval(result, strategy)
            diagnostic_counts[(qa["dataset"], strategy, diagnostic["category"])] += 1
            if diagnostic["category"] != "HIT_AT_5":
                diagnostics.append({"dataset": qa["dataset"], "qa_id": qa["qa_id"], "strategy": strategy,
                    "source_file_ids": qa["source_file_ids"], "doc_ids": execution["doc_ids"],
                    "status": value["status"], **context, **diagnostic})
            metric_rows.append({"dataset": qa["dataset"], "qa_id": qa["qa_id"], "strategy": strategy,
                "source_file_ids": qa["source_file_ids"], "doc_ids": execution["doc_ids"], "status": value["status"],
                **context, "diagnosis": diagnostic["category"],
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
    breakdown = grouped_metrics(questions, STRATEGIES)
    doc_ready = sum(r["execution_status"] == "READY" for r in documents)
    indexed = sum(r["execution_status"] in {"READY", "PARTIAL_READY"} and is_indexed(r)
                  for r in documents)
    attempted = sum(bool(r.get("doc_id") or r.get("upload_intent")) for r in documents)
    all_success = all(v["status"] == "SUCCESS" for q in questions for v in q["strategies"].values())
    complete = bool(questions) and all_success and valid and fatal_code is None
    policy = (config or {}).get("index_policy", "complete")
    status = "PASS" if doc_ready == len(documents) and complete else (
        "COMPLETED_WITH_QUALITY_LIMITS" if policy == "indexed" and indexed == len(documents)
        and complete else "INCOMPLETE")
    selection = data.get("qa_selection")
    if selection is not None:
        status = {"PASS": "SAMPLE_COMPLETED",
                  "COMPLETED_WITH_QUALITY_LIMITS": "SAMPLE_COMPLETED_WITH_QUALITY_LIMITS"}.get(status, status)
    report = {"version": "stage33-full-v1", "updated_at": datetime.now(UTC).isoformat(),
        "status": status, "fatal_code": fatal_code, "index_policy": policy,
        "qa_scope": "sampled" if selection is not None else "full", "qa_selection": selection,
        "population_qa_count": sum(selection["population_counts"].values()) if selection else len(questions),
        "full_qa_evaluation": selection is None,
        "indexed": indexed, "full_ready_acceptance": doc_ready == len(documents),
        "state_source": "database_snapshot" if snapshot is not None else "import_checkpoint",
        "index_snapshot_at": snapshot.get("captured_at") if snapshot is not None else None,
        "fixed_document_count": len(documents), "fixed_qa_count": len(questions),
        "qa_strategy_records": len(questions) * len(STRATEGIES),
        "import_attempted_or_reused": attempted, "ready": doc_ready,
        "partial_ready": sum(r["execution_status"] == "PARTIAL_READY" for r in documents),
        "quality_not_recorded": sum(not (r.get("parse_quality") or {}).get("content") for r in documents),
        "degraded_documents": sum(bool((r.get("parse_quality") or {}).get("warnings")) for r in documents),
        "dataset_formats": {d: dict(Counter(r["file_type"] for r in documents if r["dataset"] == d))
                            for d in ("ohr", "ocb")}, "evaluation_validity": validity,
        "metrics": metrics, "metrics_breakdown": breakdown, "fees": fee_summary(documents),
        "limits": ["PASS仅指全范围流程/检索执行完成，不代表每题召回成功或阶段34问答准确率",
                   "OCB无官方Chunk/页相关性标签，证据Recall不可评分；单列官方reference_files文档代理指标",
                   "OCB expected_assertions全部保持人工待审，未调用LLM/未自动判定答案正确"]}
    report["limits"].append("COMPLETED_WITH_QUALITY_LIMITS仅表示当前可靠索引的全题检索执行完成，不能升级PARTIAL或宣称完整解析")
    if selection is not None:
        report["limits"].append(f"本run仅为固定QA抽样成绩，不代表全部{report['population_qa_count']}题或全部题型/文档均已验证")
    baseline = read_json(run / "baseline.json") if (run / "baseline.json").exists() else None
    report["fees"]["newly_observed_cloud_file_submissions_this_run"] = (
        len({tuple(v) for v in report["fees"]["confirmed_file_identities"]}
            - {tuple(v) for v in baseline["confirmed_cloud_files"]}) if baseline is not None else None)
    # 变化或中断快照不能发布为有效正式指标；保留逐题事实便于审计/后续恢复。
    report["metrics_status"] = "VALID_EXECUTION_RECORDS" if valid else "NOT_CERTIFIED"
    gate_metrics(metrics, valid=valid)
    for groups in breakdown.values():
        gate_metrics(groups, valid=valid)
    write_json(run / "import_report.json", {"documents": documents})
    quality_rows = [{"dataset": r["dataset"], "file_id": r["file_id"], "doc_id": r.get("doc_id"),
        "execution_status": r["execution_status"], "parse_quality": r.get("parse_quality")}
        for r in documents]
    write_json(run / "parse_quality_report.json", {"fixed_document_count": len(documents),
        "partial_ready": report["partial_ready"], "degraded_documents": report["degraded_documents"],
        "documents": quality_rows})
    write_csv(run / "parse_quality_report.csv", quality_rows,
        ["dataset", "file_id", "doc_id", "execution_status", "parse_quality"])
    write_csv(run / "import_report.csv", documents, ["dataset", "file_id", "file_path", "file_type", "sha256",
        "doc_id", "execution_status", "status", "error_code", "adapter_diagnostic", "parser_reason_codes", "route_strategy", "block_count", "chunk_count", "vector_count",
        "invalid_vector_count", "parse_quality", "management", "parse_jobs", "index_identity", "retry_http_status", "retry_request_error"])
    atomic_write(run / "qa_execution.jsonl", b"".join(encoded(r) + b"\n" for r in executions))
    write_csv(run / "qa_execution.csv", metric_rows, ["dataset", "qa_id", "strategy", "source_file_ids", "doc_ids", "status",
        "source_quality", "source_file_types", "file_type_group", "required_evidence_pages_missing", "diagnosis",
        "scorable", "manual_pending", "error_code", "duration_seconds", "recall_at_5", "recall_at_10", "mrr_at_20"])
    publish_stream(run / "retrieval_results.jsonl", hits_buffer)
    hit_csv.flush()
    publish_stream(run / "retrieval_results.csv", hit_csv)
    hits_buffer.close()
    hit_csv.close()
    write_json(run / "failures_and_unscorable.json", failures)
    write_csv(run / "failures_and_unscorable.csv", failures, ["kind", "dataset", "identity", "strategy", "status", "error_code", "adapter_diagnostic", "parser_reason_codes", "retry_http_status", "retry_request_error"])
    write_json(run / "metrics.json", metrics)
    write_json(run / "metrics_breakdown.json", breakdown)
    write_json(run / "retrieval_diagnostics.json", {"metrics_status": report["metrics_status"],
        "note": "未达到完整Recall@5的事后定位，包含晚命中/不可评分项；非解析失败清单，不作自动修复或重投。",
        "counts": [{"dataset": dataset, "strategy": strategy, "category": category, "count": count}
            for (dataset, strategy, category), count in sorted(diagnostic_counts.items())], "cases": diagnostics})
    write_csv(run / "retrieval_diagnostics.csv", diagnostics, ["dataset", "qa_id", "strategy", "status",
        "category", "basis", "source_file_ids", "doc_ids", "source_quality", "file_type_group",
        "required_evidence_pages_missing", "error_code"])
    for name in ("ohr", "ocb", "overall"):
        write_json(run / f"metrics_{name}.json", metrics[name])
    write_json(run / "costs_and_timing.json", {"fees": report["fees"], "strategies": metrics,
        "evaluation_setup": read_json(run / "evaluation_setup.json") if (run / "evaluation_setup.json").exists() else None,
        "runtime": read_json(run / "timing.json") if (run / "timing.json").exists() else None})
    write_json(run / "summary.json", report)
    summary_rows = [{"dataset": d, "strategy": s, "qa_scope": report["qa_scope"], **r, **r["metrics"],
        **{"document_" + k: v for k, v in r["document_proxy_metrics"].items()}}
        for d, values in metrics.items() for s, r in values.items()]
    write_csv(run / "strategy_comparison.csv", summary_rows, ["dataset", "strategy", "qa_scope", "total_questions",
        "executed_successfully", "failed", "not_executed", "scorable_denominator", "successfully_scored",
        "unscorable", "manual_pending", "recall_at_5", "recall_at_10", "mrr_at_20", "document_proxy_metrics",
        "document_recall_at_5", "document_recall_at_10", "document_mrr_at_20", "latency_seconds"])
    breakdown_rows = [{"dimension": dimension, "group": group, "strategy": strategy, **value,
        **value["metrics"], **{"document_" + key: score for key, score in value["document_proxy_metrics"].items()}}
        for dimension, groups in breakdown.items() for group, strategies in groups.items()
        for strategy, value in strategies.items()]
    write_csv(run / "metrics_breakdown.csv", breakdown_rows, ["dimension", "group", "strategy", "total_questions",
        "executed_successfully", "failed", "not_executed", "scorable_denominator", "unscorable",
        "recall_at_5", "recall_at_10", "mrr_at_20", "document_recall_at_5", "document_recall_at_10",
        "document_mrr_at_20", "latency_seconds"])
    title = "# 阶段33抽样检索评测" if selection is not None else "# 阶段33全量导入与检索"
    text = [title, "", f"状态：{report['status']}；文档 {doc_ready}/{len(documents)} READY；QA范围 {len(questions)}/{report['population_qa_count']}。",
        f"部分索引 PARTIAL_READY：{report['partial_ready']}；存在质量告警：{report['degraded_documents']}。部分索引不计入完整READY。",
        f"索引校验通过：{indexed}/{len(documents)}；评测政策：{policy}；指标状态：{report['metrics_status']}。",
        f"未记录解析质量元数据：{report['quality_not_recorded']}份；不补造COMPLETE。",
        "", "| 数据集 | 策略 | 成功执行 | 可评分总分母 | 成功评分 | 不可评分 | Recall@5 | Recall@10 | MRR@20 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in summary_rows:
        text.append("| " + " | ".join(str(row.get(k)) for k in ("dataset", "strategy", "executed_successfully",
            "scorable_denominator", "successfully_scored", "unscorable", "recall_at_5", "recall_at_10", "mrr_at_20")) + " |")
    text += ["", "以下仅为依赖文档代理指标，不能当作OCB答案或证据正确率。", "",
        "| 数据集 | 策略 | 文档Recall@5 | 文档Recall@10 | 文档MRR@20 | 平均秒 | P95秒 |",
        "|---|---|---:|---:|---:|---:|---:|"]
    for row in summary_rows:
        text.append("| " + " | ".join(str(value) for value in (row["dataset"], row["strategy"],
            row["document_recall_at_5"], row["document_recall_at_10"], row["document_mrr_at_20"],
            row["latency_seconds"]["mean"], row["latency_seconds"]["p95"])) + " |")
    text += ["", "指标分母不按成功子集缩小；本run选定QA未完整执行或快照无效时，不可宣称评测通过。",
        "质量/格式分组见metrics_breakdown.csv；漏召回及低排名定位见retrieval_diagnostics.csv。",
        "文档代理指标见JSON/CSV，不代表OCB断言正确或LLM问答准确率。金额须人工核对账单。"]
    if selection is not None:
        text += ["", f"固定QA抽样：每数据集{selection['per_dataset']}题；种子{selection['seed']}；"
                 "选题清单见qa_selection.json。文档库保持完整，不能将抽样成绩称为全量QA成绩。"]
    atomic_write(run / "summary.md", ("\n".join(text) + "\n").encode("utf-8"))
    return report
