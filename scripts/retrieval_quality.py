"""阶段26跨语言检索离线/只读对比；默认不读库、不加载模型，不请求任何云API。"""

import argparse
import copy
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUALITY_CODES = {"QUALITY_SUITE_INVALID", "QUALITY_LIMIT", "QUALITY_DUPLICATE", "QUALITY_SOURCE_CONFLICT",
    "QUALITY_SCOPE", "QUALITY_SOURCE_PAGE", "QUALITY_SOURCE_HASH", "QUALITY_FLAGS", "QUALITY_OUTPUT_EXISTS", "QUALITY_LOCAL_IO"}


class QualityError(Exception):
    """只输出固定错误码，不透传问题、正文、路径、连接串或模型异常。"""

    def __init__(self, code):
        """未注册类别安全归为本地输入/输出错误。"""
        self.code = code if code in QUALITY_CODES else "QUALITY_LOCAL_IO"
        super().__init__(self.code)


def failure_code(error):
    """只保留已知服务错误类别，阻塞报告可定位模型/SQL/来源，而不回显上游原文。"""
    from app.services.rag.bm25 import BM25RetrievalError
    from app.services.rag.dense import DenseRetrievalError
    from app.services.rag.evidence import EvidenceError
    from app.services.rag.llama_index.hybrid_retriever import HybridRetrievalError
    from app.services.rag.llama_index.validation import RagAdapterError
    from app.services.reranking import RerankError
    known = (QualityError, BM25RetrievalError, DenseRetrievalError, EvidenceError,
             HybridRetrievalError, RagAdapterError, RerankError)
    return error.code if isinstance(error, known) else "QUALITY_BACKEND_OR_SOURCE"


def read_suite(path):
    """校验人工真值与最多两条平行检索表达；不自动翻译、补答案或推断相关Chunk。"""
    from app.services.rag.dense import validate_doc_ids, validate_query, validate_scope
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if payload.get("version") != 1 or not isinstance(payload.get("cases"), list) or not 1 <= len(payload["cases"]) <= 100:
            raise ValueError
        identifiers = set()
        for case in payload["cases"]:
            if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                    or not case["id"].isascii() or not case["id"].replace("-", "").replace("_", "").isalnum()
                    or case["id"] in identifiers or len(case["id"]) > 64):
                raise ValueError
            identifiers.add(case["id"])
            case["kb_id"] = str(validate_scope(case["kb_id"], 20))
            scope = validate_doc_ids(case.get("doc_ids"))
            if scope is not None:
                case["doc_ids"] = [str(value) for value in scope]
            validate_query(case["question"])
            variants = case.get("variants", [])
            if not isinstance(variants, list) or len(variants) > 2:
                raise ValueError
            for query in variants:
                validate_query(query)
            queries = [case["question"], *variants]
            if len(set(query.strip().casefold() for query in queries)) != len(queries):
                raise ValueError
            if (case["query_language"] not in {"zh", "en"} or case["document_language"] not in {"zh", "en"}
                    or type(case.get("no_answer", False)) is not bool or not isinstance(case["targets"], list)):
                raise ValueError
            if bool(case.get("no_answer")) == bool(case["targets"]):
                raise ValueError
            seen = set()
            for target in case["targets"]:
                chunk, doc = UUID(target["chunk_id"]), UUID(target["doc_id"])
                if (not chunk.int or not doc.int or chunk in seen or (scope is not None and doc not in scope)
                        or not isinstance(target["pages"], list) or not target["pages"]
                        or any(type(page) is not int or page < 1 for page in target["pages"])
                        or not isinstance(target["answer"], str) or not target["answer"].strip()):
                    raise ValueError
                seen.add(chunk)
                target["chunk_id"], target["doc_id"] = str(chunk), str(doc)
    except Exception:
        raise QualityError("QUALITY_SUITE_INVALID") from None
    return payload


def unique_queries(case):
    """返回已校验的原问题和平行表达；变体仅来自人工真值集，不用问题外答案扩展。"""
    return [case["question"], *case.get("variants", [])]


def verify_truth(case, resolver=None):
    """先复用EvidenceService只读核对真值当前来源，避免已删除/过期Chunk被误算为检索退化。"""
    if case.get("no_answer"):
        return {"status": "not_applicable"}
    if resolver is None:
        from app.services.rag.evidence import EvidenceService
        resolver = EvidenceService()
    snapshots = {}
    for target in case["targets"]:
        trace = resolver.resolve(case["kb_id"], target["chunk_id"], doc_id=target["doc_id"])
        if not set(target["pages"]).issubset({span.page for span in trace.source_spans}):
            raise QualityError("QUALITY_SOURCE_PAGE")
        if target.get("file_hash") is not None and target["file_hash"] != trace.file_hash:
            raise QualityError("QUALITY_SOURCE_HASH")
        snapshots[target["chunk_id"]] = {"file_hash": trace.file_hash,
            "source_parse_task_id": str(trace.source_parse_task_id),
            "source_chunk_task_id": str(trace.source_chunk_task_id), "page_semantics": trace.page_semantics}
    return {"status": "source_verified_not_answer_semantics", "chunks": snapshots}


def reciprocal_merge(lists, *, limit=30, rrf_k=60):
    """实验级等权RRF按Chunk身份去重；分数留在报告，不伪装为项目的双支FusionProvenance。"""
    from app.services.rag.llama_index.node_adapter import node_to_result
    if type(limit) is not int or not 1 <= limit <= 100 or not lists:
        raise QualityError("QUALITY_LIMIT")
    records, votes, ranks = {}, {}, {}
    for branch, nodes in enumerate(lists):
        seen = set()
        for rank, node in enumerate(nodes, 1):
            result = node_to_result(node, log_success=False)
            key = str(result.chunk_id)
            if key in seen:
                raise QualityError("QUALITY_DUPLICATE")
            seen.add(key)
            # 不比较来自不同Query的评分，但全部原始证据必须一致；并发变更全批拒绝。
            identity = result.model_dump(exclude={"retrieval_score", "fusion", "fusion_score", "rerank", "rerank_score"})
            if key in records and records[key][1] != identity:
                raise QualityError("QUALITY_SOURCE_CONFLICT")
            if key not in records:
                records[key] = (node.model_copy(deep=True), identity)
            votes.setdefault(key, []).append(1.0 / (rrf_k + rank))
            ranks.setdefault(key, {})[str(branch)] = rank
    scores = {key: math.fsum(values) / len(lists) for key, values in votes.items()}
    order = sorted(scores, key=lambda key: (-scores[key], UUID(key).int))[:limit]
    return [records[key][0] for key in order], {
        key: {"parallel_rrf_score": scores[key], "query_ranks": ranks[key]} for key in order}


def scoped(nodes, case):
    """每次跨查询合并前防御性核对固定kb/doc范围；不在Python中删除越界Evidence。"""
    from app.services.rag.llama_index.node_adapter import node_to_result
    kb_id = UUID(case["kb_id"])
    doc_ids = None if case.get("doc_ids") is None else {UUID(value) for value in case["doc_ids"]}
    for node in nodes:
        value = node_to_result(node, log_success=False)
        if value.kb_id != kb_id or (doc_ids is not None and value.doc_id not in doc_ids):
            raise QualityError("QUALITY_SCOPE")
    return nodes


def evidence_hits(nodes, targets):
    """仅按已核对Chunk/Document/来源页命中，不用答案字符串包含判断正确性。"""
    by_id = {target["chunk_id"]: target for target in targets}
    matches, seen = [], set()
    for rank, hit in enumerate(nodes, 1):
        meta, key = hit.node.metadata, hit.node.node_id
        if key in seen:
            raise QualityError("QUALITY_DUPLICATE")
        seen.add(key)
        target = by_id.get(key)
        if target is None or meta["doc_id"] != target["doc_id"]:
            continue
        spans = meta["chunk_metadata"].get("source_spans", [])
        pages = {span.get("page") for span in spans}
        if not set(target["pages"]).issubset(pages):
            raise QualityError("QUALITY_SOURCE_PAGE")
        matches.append((rank, key))
    return matches


def metrics(nodes, case):
    """计算证据Recall@5/Hit@1/MRR；无答案问题不伪造可检索真值或拒答成功。"""
    if case.get("no_answer"):
        return {"status": "not_applicable", "reason": "retrieval_is_not_answerability",
                "returned_count": len(nodes)}
    hits = evidence_hits(nodes, case["targets"])
    return {"status": "measured", "recall_at_5": sum(rank <= 5 for rank, _ in hits) / len(case["targets"]),
            "hit_at_1": int(any(rank == 1 for rank, _ in hits)),
            "mrr": 1.0 / hits[0][0] if hits else 0.0,
            "first_relevant_rank": hits[0][0] if hits else None}


def safe_rows(nodes):
    """导出排序和结构诊断，不导出问题、答案、正文、完整Node或私有模型路径。"""
    frequencies = Counter(hit.score for hit in nodes)
    rows = []
    for rank, hit in enumerate(nodes, 1):
        metadata = hit.node.metadata
        chunk = metadata["chunk_metadata"]
        rows.append({"rank": rank, "chunk_id": hit.node.node_id, "doc_id": metadata["doc_id"],
            "page_start": metadata["page_start"], "page_end": metadata["page_end"],
            "score": hit.score, "equal_score_count": frequencies[hit.score],
            "oversized": chunk.get("oversized"), "estimated_tokens": chunk.get("estimated_token_count"),
            "fusion": metadata.get("fusion"), "rerank": metadata.get("rerank")})
    return rows


def target_ranks(nodes, case, stage):
    """记录目标在原始Dense/BM25/Fusion/Rerank各阶段的名次，缺失不推测原因。"""
    output = {}
    for target in case["targets"]:
        key = target["chunk_id"]
        match = next((hit for hit in nodes if hit.node.node_id == key), None)
        if stage in {"dense", "bm25"}:
            rank = match.node.metadata.get("fusion", {}).get(f"{stage}_rank") if match else None
        else:
            rank = next((index for index, hit in enumerate(nodes, 1) if hit.node.node_id == key), None)
        output[key] = rank
    return output


def evaluate_case(case, retriever, reranker, *, candidate_limit=30):
    """固定范围比较平行召回及同候选重排；不修改业务排序策略或Node原证据。"""
    started, retrieved, timings, branches = perf_counter(), [], [], []
    for query in unique_queries(case):
        tick = perf_counter()
        # 独立检索器返回融合前完整分支排名，避免目标被Fusion截断后失去诊断信息。
        dense, bm25, fused = retriever(query)
        for nodes in (dense, bm25, fused):
            scoped(nodes, case)
        timings.append((perf_counter() - tick) * 1000)
        retrieved.append(fused)
        branches.append({"dense": target_ranks(dense, case, "fusion"),
            "bm25": target_ranks(bm25, case, "fusion"), "fusion": target_ranks(fused, case, "fusion"),
            "dense_count": len(dense), "bm25_count": len(bm25), "fusion_count": len(fused)})
    tick = perf_counter()
    baseline = reranker(retrieved[0], case["question"])
    scoped(baseline, case)
    baseline_ms = (perf_counter() - tick) * 1000
    expanded, parallel_scores = reciprocal_merge(retrieved, limit=candidate_limit)
    original_on_pool, parallel_on_pool, original_on_baseline = [], [], []
    rerank_ms = []
    for index, query in enumerate(unique_queries(case)):
        tick = perf_counter()
        ranked = baseline if len(retrieved) == 1 else reranker(copy.deepcopy(expanded), query)
        same = baseline if index == 0 else reranker(copy.deepcopy(retrieved[0]), query)
        scoped(ranked, case)
        scoped(same, case)
        parallel_on_pool.append(ranked)
        original_on_baseline.append({"query_index": index, "metrics": metrics(same, case),
                                     "target_ranks": target_ranks(same, case, "rerank")})
        rerank_ms.append((perf_counter() - tick) * 1000)
        if index == 0:
            original_on_pool = ranked
    combined, rerank_votes = reciprocal_merge(parallel_on_pool, limit=candidate_limit)
    variants = {"baseline": baseline, "parallel_recall_original_rerank": original_on_pool,
                "parallel_recall_parallel_rerank": combined}
    return {"id": case["id"], "query_language": case["query_language"],
        "document_language": case["document_language"], "category": case.get("category", "unspecified"),
        "ground_truth_chunk_ids": [target["chunk_id"] for target in case["targets"]],
        "branches": branches, "same_baseline_candidates": original_on_baseline,
        "strategies": {key: {"metrics": metrics(nodes, case), "target_ranks": target_ranks(nodes, case, "rerank"),
                             "rows": safe_rows(nodes)} for key, nodes in variants.items()},
        "parallel_recall": parallel_scores, "parallel_rerank": rerank_votes,
        "timing_ms": {"retrieval_per_query": timings, "baseline_rerank": baseline_ms,
                      "comparison_rerank_per_query": rerank_ms, "total": (perf_counter() - started) * 1000}}


def summarize(results):
    """按总体及语言方向分别做宏平均；只统计可靠真值，不把无答案项记为命中。"""
    output = {}
    groups = {"all": results}
    for result in results:
        groups.setdefault(result["query_language"] + "->" + result["document_language"], []).append(result)
    for group, items in groups.items():
        output[group] = {}
        for strategy in ("baseline", "parallel_recall_original_rerank", "parallel_recall_parallel_rerank"):
            measured = [item["strategies"][strategy]["metrics"] for item in items
                        if item["strategies"][strategy]["metrics"]["status"] == "measured"]
            output[group][strategy] = {"count": len(measured), **{
                metric: math.fsum(item[metric] for item in measured) / len(measured) if measured else None
                for metric in ("recall_at_5", "hit_at_1", "mrr")}}
    return output


def initial_suite():
    """只引用用户提供的真实案例，额外通用真实真值仍待补充，不虚构文档/Chunk。"""
    target = {"chunk_id": "292dfdb4-b7be-5bda-b32a-8b2e0edf1439",
        "doc_id": "fd154f01-6f14-4042-8a6e-a01b228f2d89", "pages": [1], "answer": "54,470"}
    questions = [("mdoc-zh-total", "监管对象总数是多少", "zh"),
        ("mdoc-en-total", "total number of inmates, parolees, and probationers", "en"),
        ("mdoc-zh-expanded", "在押人员、假释人员和缓刑监管人员合计有多少人", "zh")]
    return {"version": 1, "provenance": "user_supplied_stage26_case_not_official_benchmark",
        "pending_real_coverage": ["zh->zh", "en->zh", "multiple_similar_statistics", "no_answer", "additional_documents"],
        "cases": [{"id": key, "kb_id": "8ba11d93-f008-4eca-82c2-0ddb72fdd53d",
            "doc_ids": [target["doc_id"]], "question": question, "query_language": language,
            "document_language": "en", "category": "table_number", "targets": [dict(target)],
            "variants": [] if language == "en" else [questions[1][1]]} for key, question, language in questions]}


def publish_json(path, payload):
    """独占创建新的受限本地报告，拒绝覆盖用户既有测试集/报告。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)


def real_backends(case, candidate_limit, branch_top_k, *, reranker_service=None):
    """复用只读Retriever/本地模型；实验可注入共享服务的测量代理，默认行为不变。"""
    from app.services.rag.llama_index.dense_retriever import DenseRetriever
    from app.services.rag.llama_index.bm25_retriever import BM25Retriever
    from app.services.rag.llama_index.hybrid_retriever import fuse_nodes
    from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
    dense = DenseRetriever(case["kb_id"], doc_ids=case.get("doc_ids"), top_k=branch_top_k)
    bm25 = BM25Retriever(case["kb_id"], doc_ids=case.get("doc_ids"), top_k=branch_top_k)
    adapter = BGERerankerAdapter(top_n=100, service=reranker_service)

    def retrieve(query):
        """每表达各调用一次原两支，保存Fusion截断前排名；同实例复用BM25派生词频。"""
        dense_nodes, bm25_nodes = dense.retrieve(query), bm25.retrieve(query)
        return dense_nodes, bm25_nodes, fuse_nodes(dense_nodes, bm25_nodes, kb_id=case["kb_id"],
            doc_ids=case.get("doc_ids"), top_k=candidate_limit, branch_top_k=branch_top_k)

    def rerank(nodes, query):
        """保留完整原文/公式/表格，以相同本地服务评分；不截断或改写原Node。"""
        return adapter.postprocess_nodes(nodes, query_str=query)

    return retrieve, rerank


def main(argv=None):
    """默认只检查本地真值；--execute才显式读库/本地推理，所有输出均不覆盖旧文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="docs/test/stage26_quality/suite.json")
    parser.add_argument("--init-suite", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default="docs/test/stage26_quality/report-01.json")
    parser.add_argument("--candidate-limit", type=int, choices=range(1, 101), default=30, metavar="1..100")
    parser.add_argument("--branch-top-k", type=int, choices=range(1, 101), default=20, metavar="1..100")
    args = parser.parse_args(argv)
    suite_path, output = Path(args.suite), Path(args.output)
    suite_path = suite_path if suite_path.is_absolute() else ROOT / suite_path
    output = output if output.is_absolute() else ROOT / output
    try:
        if args.init_suite:
            if args.execute:
                raise QualityError("QUALITY_FLAGS")
            publish_json(suite_path, initial_suite())
            print("已生成用户已知案例；请审核KB/Doc/Chunk/页码并补充通用真实真值，不代表任何测试通过。")
            return 0
        suite = read_suite(suite_path)
        if not args.execute:
            print(f"DRY-RUN：{len(suite['cases'])}个案例；不连接数据库、不加载模型、不调用云端。")
            print("人工确认真值后加--execute；双语表达不是自动翻译，比较结果不自动修改生产策略。")
            return 0
        if output.exists():
            raise QualityError("QUALITY_OUTPUT_EXISTS")
        # 双重离线约束：服务自身仅本地加载，这里也禁止依赖隐式下载资源。
        os.environ["HF_HUB_OFFLINE"], os.environ["TRANSFORMERS_OFFLINE"] = "1", "1"
        from app.core.business_logging import configure_logging
        from app.core.config import get_settings
        configure_logging(get_settings(), role="retrieval-acceptance")
        results = []
        for case in suite["cases"]:
            try:
                truth = verify_truth(case)
                retrieve, rerank = real_backends(case, args.candidate_limit, args.branch_top_k)
                result = evaluate_case(case, retrieve, rerank, candidate_limit=args.candidate_limit)
                result["status"] = "measured"
                result["ground_truth_snapshot"] = truth
            except Exception as exc:
                result = {"id": case["id"], "status": "blocked", "code": failure_code(exc)}
            results.append(result)
        measured = [result for result in results if result["status"] == "measured"]
        report = {"version": 1, "status": "measured" if len(measured) == len(results) else "blocked",
            "real_models": True, "production_strategy_changed": False, "llm_called": False,
            "branch_top_k": args.branch_top_k, "candidate_limit": args.candidate_limit,
            "scope": "evidence_retrieval_not_answer_accuracy", "cases": results,
            "metrics": summarize(measured), "pending_real_coverage": suite.get("pending_real_coverage", [])}
        publish_json(output, report)
        print(f"检索证据对比已保存；完成={len(measured)}，阻塞={len(results)-len(measured)}；未修改线上策略。")
        return 0 if report["status"] == "measured" else 2
    except Exception as exc:
        print(str(exc) if isinstance(exc, QualityError) else "QUALITY_LOCAL_IO", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
