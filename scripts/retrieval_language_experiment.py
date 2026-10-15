"""阶段26独立语言感知实验：只读现有证据及本地模型，不接入生产、不使用真值选择排序。"""

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import retrieval_quality as base

# 实验前固定的粗粒度对照，不做逐题网格搜索；上线仍需独立文档留出集。
POLICIES = ("language_preferred", "language_lexicographic", "language_weighted_075",
            "original_weighted_075", "original_fusion_ties")
LANGUAGE_VERSION = "script-count-conservative-v1"


def digest(value):
    """精确摘要只用于输入身份和重复检测，不把问题、正文或模型路径写入报告。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text_language(value, *, query=False):
    """仅按正文字符判断zh/en/mixed/unknown；排除HTML标记/数字，保守阈值不取测试集标签。"""
    plain = html.unescape(re.sub(r"<[^>]{1,2048}>", " ", value))
    chinese = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", plain))
    latin = len(re.findall(r"[A-Za-z]", plain))
    total = chinese + latin
    if total < (8 if query else 24):
        label = "unknown"
    elif chinese >= (4 if query else 12) and chinese / total >= (0.2 if query else 0.5):
        label = "zh"
    elif latin / total >= 0.8:
        label = "en"
    else:
        label = "mixed"
    return {"language": label, "cjk_chars": chinese, "latin_chars": latin, "version": LANGUAGE_VERSION}


def preferred_query(queries, node):
    """仅中文问题+明确英文候选时用唯一英文平行表达；英文原问题及不确定/多义情况保留原问。"""
    if text_language(queries[0], query=True)["language"] != "zh":
        return 0
    if text_language(node.node.text)["language"] != "en":
        return 0
    english = [index for index, query in enumerate(queries[1:], 1)
               if text_language(query, query=True)["language"] == "en"]
    return english[0] if len(english) == 1 else 0


def policy_orders(queries, rankings):
    """同候选池比较纯首选/词典序/固定加权策略；真值、答案、标签均不是函数输入。"""
    from app.services.rag.llama_index.node_adapter import node_to_result
    if not queries or len(queries) != len(rankings):
        raise base.QualityError("QUALITY_LIMIT")
    originals, identity = {}, {}
    positions, tied_positions = [], []
    for nodes in rankings:
        current, tied, score_first = {}, {}, {}
        for rank, hit in enumerate(nodes, 1):
            result = node_to_result(hit, log_success=False)
            key = str(result.chunk_id)
            if key in current:
                raise base.QualityError("QUALITY_DUPLICATE")
            source = result.model_dump(exclude={"retrieval_score", "fusion", "fusion_score", "rerank", "rerank_score"})
            if key in identity and identity[key] != source:
                raise base.QualityError("QUALITY_SOURCE_CONFLICT")
            identity[key] = source
            originals.setdefault(key, hit)
            current[key] = rank
            score_first.setdefault(hit.score, rank)
            tied[key] = score_first[hit.score]
        if positions and set(current) != set(positions[0]):
            raise base.QualityError("QUALITY_SOURCE_CONFLICT")
        positions.append(current)
        tied_positions.append(tied)
    chosen = {key: preferred_query(queries, hit) for key, hit in originals.items()}
    sorts = {name: {} for name in POLICIES}
    for key, hit in originals.items():
        selected, original = chosen[key], positions[0][key]
        preferred = positions[selected][key]
        sorts["language_preferred"][key] = (preferred, UUID(key).int)
        sorts["language_lexicographic"][key] = (tied_positions[selected][key], original, UUID(key).int)
        sorts["language_weighted_075"][key] = (
            -(0.75 / (60 + preferred) + 0.25 / (60 + original)), UUID(key).int)
        extra = math.fsum(1 / (60 + ranks[key]) for ranks in positions[1:]) / max(1, len(positions) - 1)
        score = (0.75 / (60 + original) + 0.25 * extra) if len(positions) > 1 else 1 / (60 + original)
        sorts["original_weighted_075"][key] = (-score, UUID(key).int)
        # 只改变确切同logit组的次序，不用近似阈值、文本长度或答案特征抬分。
        fusion_rank = hit.node.metadata.get("rerank", {}).get("input_rank", original)
        sorts["original_fusion_ties"][key] = (-hit.score, fusion_rank, UUID(key).int)
    return {name: [originals[key].model_copy(deep=True) for key in sorted(originals, key=sort_keys.get)]
            for name, sort_keys in sorts.items()}, chosen


def percentile(values, fraction):
    """对有限非空测量值做线性插值分位数，空集不伪造零耗时。"""
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] * (high - position) + values[high] * (position - low) if low != high else values[low]


def latency_summary(values):
    """汇总均值/P50/P95及最大值；组成估计与实际墙钟耗时在调用方明确区分。"""
    return {"count": len(values), "mean_ms": math.fsum(values) / len(values) if values else None,
            "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95), "max_ms": max(values) if values else None}


def suite_audit(suite):
    """仅用于评测审计，记录重复文档/目标和表达；这些真值统计绝不传入策略函数。"""
    documents, targets, expressions = Counter(), Counter(), defaultdict(list)
    for case in suite["cases"]:
        for doc_id in {target["doc_id"] for target in case["targets"]}:
            documents[doc_id] += 1
        for target in case["targets"]:
            targets[target["chunk_id"]] += 1
        for query in base.unique_queries(case):
            expressions[digest(" ".join(query.casefold().split()))].append(case["id"])
    return {"case_count": len(suite["cases"]), "answerable_count": sum(bool(c["targets"]) for c in suite["cases"]),
        "independent_target_documents": len(documents), "unique_target_chunks": len(targets),
        "document_case_counts": dict(documents), "target_reuse_counts": dict(targets),
        "repeated_expression_case_groups": [sorted(set(ids)) for ids in expressions.values() if len(set(ids)) > 1],
        "single_document_scope_count": sum(len(c.get("doc_ids") or []) == 1 for c in suite["cases"]),
        "semantic_equivalence": "manual_review_required_not_proved_by_matching_ranks",
        "independent_holdout": "not_established", "no_answer_accuracy": "not_applicable_no_generation"}


class MeasuredService:
    """只包装已有共享Reranker，缓存本案例完全相同输入的原logit；不缓存Node或创建第二个模型。"""

    def __init__(self, service):
        """每案例重置测量缓存，跨案例不靠复用结果虚报低延迟。"""
        self.service, self.cache = service, {}
        self.calls, self.model_calls, self.pairs = 0, 0, 0
        self.events = []

    def identity(self):
        """始终委托正式身份检查，模型指纹变化不能沿用旧分数。"""
        return self.service.identity()

    def score(self, query, passages):
        """沿用完整输入与现有精度/批处理，显式记录缓存命中及实际GPU计算成本。"""
        self.calls += 1
        fingerprint = json.dumps(self.identity(), sort_keys=True)
        key = digest(query), tuple(digest(text) for text in passages), fingerprint
        reused = key in self.cache
        if not reused:
            started = perf_counter()
            scores = self.service.score(query, passages)
            elapsed = (perf_counter() - started) * 1000
            token_counts, token_hashes = [], []
            # 仅实验读取已加载Tokenizer，不另载模型，不输出token序列/正文；成本单列。
            audit_started = perf_counter()
            tokenizer = self.service._backend[0]
            encoded = tokenizer([(query, text) for text in passages], add_special_tokens=True,
                                padding=False, truncation=False)["input_ids"]
            for tokens in encoded:
                token_counts.append(len(tokens))
                token_hashes.append(digest(json.dumps(tokens, separators=(",", ":"))))
            self.cache[key] = (list(scores), elapsed, token_counts, token_hashes,
                               (perf_counter() - audit_started) * 1000)
            self.model_calls += 1
            self.pairs += len(passages)
        scores, elapsed, token_counts, token_hashes, audit_ms = self.cache[key]
        self.events.append({"model_reused": reused, "pair_count": len(passages),
            "measured_model_ms": 0.0 if reused else elapsed, "standalone_model_ms": elapsed,
            "token_audit_ms": 0.0 if reused else audit_ms, "token_counts": token_counts,
            "token_hashes": token_hashes})
        return list(scores)


def call_key(query, nodes):
    """按原问题和有序Chunk/正文身份匹配本案例观察，不把节点排序来源与分数缓存混用。"""
    return digest(query), tuple((hit.node.node_id, digest(hit.node.text)) for hit in nodes)


def evaluate(case, retrieve, rerank, *, candidate_limit=30):
    """直接复用原三策略评测，记录同池观察后比较新策略；指标最后才读取真值。"""
    observations, retrievals = {}, {}
    calls = []

    def tracked_retrieve(query):
        """记录既有召回结果，不扩大范围/预算，也不二次编码文档。"""
        result = retrieve(query)
        retrievals[query] = result
        return result

    def tracked_rerank(nodes, query):
        """保留每个原始候选池的真实评分，排序策略共享观察而非反复加载模型。"""
        tick = perf_counter()
        ranked = rerank(nodes, query)
        observations[call_key(query, nodes)] = ranked
        calls.append({"key": call_key(query, nodes), "wall_ms": (perf_counter() - tick) * 1000})
        return ranked

    result = base.evaluate_case(case, tracked_retrieve, tracked_rerank, candidate_limit=candidate_limit)
    queries = base.unique_queries(case)
    baseline_pool = retrievals[queries[0]][2]
    pool, _ = base.reciprocal_merge([retrievals[q][2] for q in queries], limit=candidate_limit)
    ranked = [observations[call_key(q, pool)] for q in queries]
    tick = perf_counter()
    policies, chosen = policy_orders(queries, ranked)
    policy_ms = (perf_counter() - tick) * 1000
    same_pool_rankings = [observations[call_key(q, baseline_pool)] for q in queries]
    same_policies, _ = policy_orders(queries, same_pool_rankings)
    policies["language_preferred_same_baseline_pool"] = same_policies["language_preferred"]
    for name, nodes in policies.items():
        base.scoped(nodes, case)
        result["strategies"][name] = {"metrics": base.metrics(nodes, case),
            "target_ranks": base.target_ranks(nodes, case, "rerank"), "rows": base.safe_rows(nodes),
            "score_semantics": "representative_original_logit_not_fused_score"}
    result["same_candidate_set"] = {hit.node.node_id for hit in baseline_pool} == {hit.node.node_id for hit in pool}
    result["query_languages_inferred"] = [text_language(q, query=True) for q in queries]
    result["candidate_diagnostics"] = [{"chunk_id": hit.node.node_id,
        "language": text_language(hit.node.text), "preferred_query_index": chosen[hit.node.node_id],
        "characters": len(hit.node.text), "text_hash": digest(hit.node.text),
        "block_types": sorted({s.get("block_type", "unknown") for s in hit.node.metadata["chunk_metadata"].get("source_spans", [])})}
        for hit in pool]
    result["rerank_observations"] = [{"query_index": index, "rows": base.safe_rows(nodes)}
                                     for index, nodes in enumerate(ranked)]
    result["policy_wall_ms"] = policy_ms
    return result, calls


def aggregate(results):
    """分语言方向汇总真实指标及改善/退化题号；无答案不进入指标分母。"""
    groups = {"all": results}
    for result in results:
        groups.setdefault(result["query_language"] + "->" + result["document_language"], []).append(result)
    output = {}
    names = list(results[0]["strategies"]) if results else []
    for group, cases in groups.items():
        output[group] = {}
        for name in names:
            measured = [c for c in cases if c["strategies"][name]["metrics"]["status"] == "measured"]
            improved, regressed = [], []
            for case in measured:
                before = case["strategies"]["baseline"]["metrics"]["first_relevant_rank"] or math.inf
                after = case["strategies"][name]["metrics"]["first_relevant_rank"] or math.inf
                if after < before:
                    improved.append(case["id"])
                elif after > before:
                    regressed.append(case["id"])
            output[group][name] = {"count": len(measured), **{metric:
                math.fsum(c["strategies"][name]["metrics"][metric] for c in measured) / len(measured) if measured else None
                for metric in ("recall_at_5", "hit_at_1", "mrr")}, "rank_improved": improved, "rank_regressed": regressed,
                "rank_unchanged_count": len(measured) - len(improved) - len(regressed)}
    return output


def attach_measurements(result, calls, events, queries):
    """绑定真实评分/token观察，并重组独立策略成本估计；不将缓存命中墙钟冒充完整策略延迟。"""
    event_iter, costs, audits = iter(events), {}, {}
    for call in calls:
        key = call["key"]
        if not key[1]:
            costs[key] = call["wall_ms"]
            continue
        event = next(event_iter)
        # 同案例重复输入仅计算一次；独立运行成本用实测GPU耗时回填，不计额外token审计。
        costs[key] = max(0, call["wall_ms"] - event["measured_model_ms"] - event["token_audit_ms"]) + event["standalone_model_ms"]
        audits[key] = [{"chunk_id": identity[0], "pair_tokens": tokens, "token_hash": token_hash}
            for identity, tokens, token_hash in zip(key[1], event["token_counts"], event["token_hashes"])]
    if next(event_iter, None) is not None:
        raise base.QualityError("QUALITY_SOURCE_CONFLICT")
    first_key = calls[0]["key"]
    # evaluate_case调用顺序固定为baseline，然后各表达在扩展池；单表达直接复用baseline。
    expanded_keys = [first_key]
    # index=0只有expanded原问调用；index>=1此前已包含expanded原问，因此为2,4,...。
    if len(queries) > 1:
        expanded_keys = [calls[1]["key"], *[calls[2 * index]["key"] for index in range(1, len(queries))]]
    retrieval = result["timing_ms"]["retrieval_per_query"]
    cost = {"baseline": retrieval[0] + costs[first_key],
        "parallel_recall_original_rerank": sum(retrieval) + costs[expanded_keys[0]],
        "parallel_recall_parallel_rerank": sum(retrieval) + sum(costs[key] for key in expanded_keys)}
    selected = {row["preferred_query_index"] for row in result["candidate_diagnostics"]} or {0}
    for name in POLICIES:
        required = ({0, *selected} if name in {"language_lexicographic", "language_weighted_075"}
                    else selected if name == "language_preferred" else {0} if name == "original_fusion_ties"
                    else set(range(len(queries))))
        cost[name] = sum(retrieval) + sum(costs[expanded_keys[index]] for index in required)
    # 同baseline池仍需平行表达，但无需平行召回；用已实测的同池调用成本。
    same_keys = {0: first_key, **{index: calls[2 * index + 1]["key"] for index in range(1, len(queries))}}
    cost["language_preferred_same_baseline_pool"] = retrieval[0] + sum(costs[same_keys[index]] for index in selected)
    result["standalone_cost_estimate_ms"] = cost
    result["cost_semantics"] = "composition_of_observed_retrieval_and_full_rerank_not_independent_latency_benchmark"
    result["token_observations"] = [{"query_index": index, "rows": audits.get(key, [])}
                                     for index, key in enumerate(expanded_keys)]
    result["compute"] = {"logical_rerank_calls": len(calls), "actual_model_calls": sum(not e["model_reused"] for e in events),
        "actual_pairs": sum(e["pair_count"] for e in events if not e["model_reused"]),
        "actual_model_ms": sum(e["measured_model_ms"] for e in events),
        "extra_token_audit_ms": sum(e["token_audit_ms"] for e in events)}


def measurement_summary(results):
    """分别汇总实际实验开销、重组策略成本与显存；不混入模型冷启动或假称独立计时。"""
    names = list(results[0]["strategies"]) if results else []
    return {"standalone_cost_estimates_ms": {name: latency_summary([
        row["standalone_cost_estimate_ms"][name] for row in results]) for name in names},
        "actual_case_wall_ms": latency_summary([row["experiment_wall_ms"] for row in results]),
        "actual_model_calls": sum(row["compute"]["actual_model_calls"] for row in results),
        "actual_pairs": sum(row["compute"]["actual_pairs"] for row in results),
        "actual_model_ms": sum(row["compute"]["actual_model_ms"] for row in results),
        "token_audit_ms": sum(row["compute"]["extra_token_audit_ms"] for row in results),
        "peak_torch_allocated_mib": max((row["gpu"]["allocated_peak_mib"] for row in results), default=None),
        "peak_torch_reserved_mib": max((row["gpu"]["reserved_peak_mib"] for row in results), default=None)}


def markdown_report(report):
    """从本次真实报告生成短表，不导出正文、答案、连接串或模型私有路径。"""
    lines = ["# 阶段26独立语言策略实验", "", f"状态：{report['status']}；生产策略未修改；无云调用/数据库写入。", "",
        "## 总体（仅可靠真值）", "", "| 策略 | N | Recall@5 | Hit@1 | MRR |", "|---|---:|---:|---:|---:|"]
    for name, values in report["metrics"].get("all", {}).items():
        cells = [f"{values[key]:.4f}" if values[key] is not None else "N/A" for key in ("recall_at_5", "hit_at_1", "mrr")]
        lines.append(f"| {name} | {values['count']} | {' | '.join(cells)} |")
    for group, strategies in report["metrics"].items():
        if group == "all":
            continue
        lines += ["", f"## {group}", ""]
        for name, values in strategies.items():
            lines.append(f"- {name}：N={values['count']}，Hit@1={values['hit_at_1']}，MRR={values['mrr']}；"
                         f"改善={values['rank_improved']}；退化={values['rank_regressed']}。")
    lines += ["", "## 成本与边界", "", "```json", json.dumps(report["measurements"], ensure_ascii=False, indent=2), "```", "",
        "策略耗时是实测调用的组成估计，不是各策略独立运行的延迟基准；精确重复输入仅在同案例复用logit。",
        "语言由正文字符保守判断，未读取测试集document_language作为策略输入；混合/未知保留原问。",
        "平行表达来自人工测试集，生产没有自动翻译器；语义等价需人工复核。",
        "少量重复目标、无独立文档留出集，结果不足以上线；检索命中不是问答准确率，无答案项不计分。"]
    return "\n".join(lines) + "\n"


def run(suite, *, candidate_limit, branch_top_k, max_seconds):
    """显式执行现有本地模型和只读Retriever，出错有界停止，不改环境/精度或重试耗费任务。"""
    import torch
    from app.services.embedding.service import get_embedding_service
    from app.services.reranking import get_reranker_service
    startup = perf_counter()
    embedding, reranker = get_embedding_service(), get_reranker_service()
    identities = {"embedding": embedding.identity(), "reranker": reranker.identity()}
    device = reranker.options.device
    if not device.startswith("cuda:") or not torch.cuda.is_available():
        raise base.QualityError("QUALITY_FLAGS")
    startup_ms, results = (perf_counter() - startup) * 1000, []
    started = perf_counter()
    for case in suite["cases"]:
        tick = perf_counter()
        if tick - started > max_seconds:
            results.append({"id": case["id"], "status": "blocked", "code": "QUALITY_TIME_BUDGET"})
            continue
        try:
            truth = base.verify_truth(case)
            torch.cuda.synchronize(device)
            torch.cuda.reset_peak_memory_stats(device)
            measured_service = MeasuredService(reranker)
            retrieve, rerank = base.real_backends(case, candidate_limit, branch_top_k, reranker_service=measured_service)
            result, calls = evaluate(case, retrieve, rerank, candidate_limit=candidate_limit)
            attach_measurements(result, calls, measured_service.events, base.unique_queries(case))
            torch.cuda.synchronize(device)
            result["gpu"] = {"allocated_peak_mib": torch.cuda.max_memory_allocated(device) / 2**20,
                             "reserved_peak_mib": torch.cuda.max_memory_reserved(device) / 2**20}
            # 无长事务/锁；前后核对来源快照，发现并发维护则阻塞而不拼接旧证据。
            if base.verify_truth(case) != truth:
                raise base.QualityError("QUALITY_SOURCE_CONFLICT")
            result["ground_truth_snapshot"], result["status"] = truth, "measured"
            result["experiment_wall_ms"] = (perf_counter() - tick) * 1000
        except Exception as exc:
            result = {"id": case["id"], "status": "blocked", "code": base.failure_code(exc)}
        results.append(result)
        print(f"案例 {case['id']}：{result['status']}" + (f"；{result['code']}" if result["status"] == "blocked" else ""), flush=True)
        # 任何真实后端错误先停止，防止模型/数据库异常后重复推理；剩余案例显式阻塞。
        if result["status"] == "blocked":
            results += [{"id": item["id"], "status": "blocked", "code": "QUALITY_STOPPED_AFTER_ERROR"}
                        for item in suite["cases"][len(results):]]
            break
    if {"embedding": embedding.identity(), "reranker": reranker.identity()} != identities:
        raise base.QualityError("QUALITY_SOURCE_CONFLICT")
    measured = [row for row in results if row["status"] == "measured"]
    return {"version": 1, "status": "measured" if len(measured) == len(results) else "blocked",
        "real_models": True, "production_strategy_changed": False, "llm_called": False,
        "database_writes": False, "source_suite_sha256": digest(json.dumps(suite, sort_keys=True, ensure_ascii=False)),
        "branch_top_k": branch_top_k, "candidate_limit": candidate_limit, "model_identities": identities,
        "policy_parameters": {"language_detection": LANGUAGE_VERSION, "rrf_k": 60, "preferred_weight": 0.75,
            "tuning": "fixed_before_run_no_truth_or_per_case_weights", "parallel_queries": "human_supplied_not_auto_translation"},
        "model_startup_ms": startup_ms, "experiment_wall_ms": (perf_counter() - started) * 1000,
        "suite_audit": suite_audit(suite), "cases": results, "metrics": aggregate(measured),
        "measurements": measurement_summary(measured)}


def main(argv=None):
    """默认dry-run；--execute及显存确认后才读库/推理，独占发布新报告，不覆盖旧结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="docs/test/stage26_quality/stage26_quality_suite_full_real_20261015.json")
    parser.add_argument("--output", default="docs/test/stage26_quality/report-language-01.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--gpu-idle-confirmed", action="store_true")
    parser.add_argument("--candidate-limit", type=int, choices=range(1, 101), default=30, metavar="1..100")
    parser.add_argument("--branch-top-k", type=int, choices=range(1, 101), default=20, metavar="1..100")
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    suite_path, output = Path(args.suite), Path(args.output)
    suite_path = suite_path if suite_path.is_absolute() else ROOT / suite_path
    output = output if output.is_absolute() else ROOT / output
    try:
        suite = base.read_suite(suite_path)
        if not 1 <= args.max_seconds <= 3600:
            raise base.QualityError("QUALITY_LIMIT")
        if not args.execute:
            print(f"DRY-RUN：{len(suite['cases'])}案例；固定策略={','.join(POLICIES)}；不读库/加载模型。")
            return 0
        if not args.gpu_idle_confirmed:
            raise base.QualityError("QUALITY_FLAGS")
        if output.exists() or output.with_suffix(".md").exists():
            raise base.QualityError("QUALITY_OUTPUT_EXISTS")
        os.environ["HF_HUB_OFFLINE"], os.environ["TRANSFORMERS_OFFLINE"] = "1", "1"
        from app.core.business_logging import configure_logging
        from app.core.config import get_settings
        configure_logging(get_settings(), role="retrieval-acceptance")
        report = run(suite, candidate_limit=args.candidate_limit, branch_top_k=args.branch_top_k,
                     max_seconds=args.max_seconds)
        base.publish_json(output, report)
        with output.with_suffix(".md").open("x", encoding="utf-8") as stream:
            stream.write(markdown_report(report))
        print(f"报告已保存；状态={report['status']}；不自动接入生产。", flush=True)
        return 0 if report["status"] == "measured" else 2
    except Exception as exc:
        print(base.failure_code(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
