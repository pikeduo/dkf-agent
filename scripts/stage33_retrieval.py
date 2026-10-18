"""阶段33四策略全量检索及分母透明的指标；不调用LLM或重新编码任何文档。"""

import hashlib
import math
from statistics import mean
from time import perf_counter

from scripts.stage33_common import STRATEGIES, Stage33Error, digest, read_json, write_json
from scripts.stage33_runtime import check_scope, scope_identity


class Backends:
    """薄组合现有Retriever/Fusion/Reranker，一次Dense查询供四路公平对照。"""

    def __init__(self, kb_id, *, corpus_limits=None):
        """显式评测时构造；全独立库200份，doc_ids=None而非突破原100个ID限制。"""
        from app.services.rag.llama_index.dense_retriever import DenseRetriever
        from app.services.rag.llama_index.bm25_retriever import BM25Retriever
        from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
        from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
        from app.services.rag.bm25 import CorpusLimits
        from app.services.reranking import get_reranker_service
        self.embedding = DKFEmbeddingAdapter()
        self.reranker_service = get_reranker_service()
        self.dense = DenseRetriever(kb_id, top_k=20, embedding_adapter=self.embedding)
        self.corpus_limits = corpus_limits or CorpusLimits(100_000, 256 * 1024**2, 32_000_000)
        self.bm25 = BM25Retriever(kb_id, top_k=20, corpus_limits=self.corpus_limits)
        # 正式API仍Top5；仅评测请求Top20才能计算Recall@10/MRR@20。
        self.reranker = BGERerankerAdapter(top_n=20, service=self.reranker_service)
        self.kb_id = kb_id

    def identity(self):
        """记录实际共享模型身份及候选预算；首次显式评测才可能加载本地模型。"""
        return {"embedding": self.embedding.query_model_identity(), "reranker": self.reranker_service.identity(),
                "branch_top_k": 20, "fusion_candidates": 30, "output_top_k": 20, "rrf_k": 60,
                "bm25_corpus_limits": {"chunks": self.corpus_limits.chunks,
                    "bytes": self.corpus_limits.bytes, "tokens": self.corpus_limits.tokens}}

    def retrieve_dense(self, question):
        """只对原始官方question执行已有Dense Query编码，不把答案传给模型。"""
        return self.dense.retrieve(question)

    def retrieve_bm25(self, question):
        """复用实例内可重建BM25缓存及当前READY快照，不持久化第二套业务索引。"""
        return self.bm25.retrieve(question)

    def fuse(self, dense, bm25):
        """使用生产等权RRF及原chunk_id去重，不实现实验性语言重写。"""
        from app.services.rag.llama_index.hybrid_retriever import fuse_nodes
        return fuse_nodes(dense, bm25, kb_id=self.kb_id, top_k=30, branch_top_k=20, rrf_k=60)

    def rerank(self, candidates, question):
        """同一Hybrid候选池原问题重排，不执行RAG合成。"""
        return self.reranker.postprocess_nodes(candidates, query_str=question)


def hit_rows(nodes, documents, kb_id):
    """复用Node校验，导出业务ID/原始证据页/字符来源/分数，不导出正文或模型路径。"""
    from app.services.rag.llama_index.node_adapter import node_to_result
    output, seen = [], set()
    for rank, hit in enumerate(nodes, 1):
        value = node_to_result(hit, log_success=False)
        doc = documents.get(str(value.doc_id))
        if (str(value.kb_id) != str(kb_id) or doc is None or str(value.chunk_id) in seen
                or hit.score is None or not math.isfinite(hit.score)):
            raise Stage33Error("RETRIEVAL_SCOPE_OR_SCORE")
        seen.add(str(value.chunk_id))
        spans = value.metadata.get("source_spans")
        if not isinstance(spans, list) or not spans:
            raise Stage33Error("RETRIEVAL_EVIDENCE")
        pages = set()
        for span in spans:
            page = span.get("page") if isinstance(span, dict) else None
            if (type(page) is not int or not value.page_start <= page <= value.page_end
                    or not span.get("block_id") or not span.get("block_text_hash")):
                raise Stage33Error("RETRIEVAL_EVIDENCE")
            if span.get("page_reliable", True) is True and (
                    span.get("provenance") is None or span["provenance"].get("page_reliable") is True):
                pages.add(page)
        semantics = value.metadata.get("page_semantics")
        if value.file_type != doc["file_type"] or (value.file_type == "pdf" and (
                semantics != "physical_pdf" or pages and max(pages) > int(doc["page_count"]))):
            raise Stage33Error("RETRIEVAL_PAGE_SEMANTICS")
        output.append({"rank": rank, "chunk_id": str(value.chunk_id), "doc_id": str(value.doc_id),
            "kb_id": str(value.kb_id), "file_id": doc["file_id"], "file_name": value.file_name,
            "file_hash": doc["sha256"], "page_start": value.page_start, "page_end": value.page_end,
            "source_pages": sorted(pages), "page_semantics": semantics, "section": value.section,
            "source_spans": spans, "text_sha256": hashlib.sha256(value.text.encode("utf-8")).hexdigest(), "score": hit.score,
            "retrieval_score": value.retrieval_score, "fusion_score": value.fusion_score,
            "rerank_score": value.rerank_score,
            "fusion": value.fusion.model_dump(mode="json") if value.fusion else None})
    return output


def truth(qa):
    """官方页标注可评证据；OCB文件依赖仅作单独文档代理，不把断言当相关Chunk标签。"""
    files = qa["source_file_ids"]
    pages = (qa.get("evidence_page") or {}).get("system_1based")
    if qa["dataset"] == "ohr" and pages and len(files) == 1:
        return {"scorable": True, "basis": "official_document_and_physical_page",
                "units": [(files[0], page) for page in sorted(set(pages))], "manual_pending": False}
    return {"scorable": False, "basis": "no_official_chunk_or_page_relevance",
            "units": [], "manual_pending": qa["dataset"] == "ocb"}


def score_units(hits, units, *, document_only=False):
    """按不同官方来源单位计Recall；重复Chunk不重复计数，MRR截断在20。"""
    gold = set(units)
    if not gold:
        return None
    covered, first = set(), None
    recalls = {}
    for rank in range(1, 21):
        if rank <= len(hits):
            hit = hits[rank - 1]
            observed = {hit["file_id"]} if document_only else {
                (hit["file_id"], page) for page in hit["source_pages"] if hit["page_semantics"] == "physical_pdf"}
            matches = gold & observed
            if matches and first is None:
                first = rank
            covered.update(matches)
        if rank in {5, 10}:
            recalls[f"recall_at_{rank}"] = len(covered) / len(gold)
    return recalls | {"mrr_at_20": 1 / first if first else 0.0, "first_relevant_rank": first}


def safe_service_error(error):
    """只复用项目固定错误注册表，保留BM25预算/模型/SQL根因，不回显异常字符串。"""
    if isinstance(error, Stage33Error):
        return error.code
    from app.services.rag.bm25 import ERRORS as bm25_errors
    from app.services.rag.dense import ERRORS as dense_errors
    from app.services.reranking import ERRORS as rerank_errors
    from app.services.embedding.service import ERRORS as embedding_errors
    from app.services.rag.llama_index.hybrid_retriever import ERRORS as fusion_errors
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code in set(bm25_errors) | set(dense_errors) | set(
        rerank_errors) | set(embedding_errors) | set(fusion_errors) else "RETRIEVAL_SERVICE_ERROR"


def evaluate_question(qa, documents, kb_id, backend):
    """四种策略同一原问题/同一池；分支失败如实记错误，不扩大范围或按答案挑策略。"""
    results, values, durations = {}, {}, {}
    question = qa["question"]
    for name in STRATEGIES:
        started = perf_counter()
        try:
            if name == "dense":
                values[name] = backend.retrieve_dense(question)
            elif name == "bm25":
                values[name] = backend.retrieve_bm25(question)
            elif name == "hybrid":
                if "dense" not in values or "bm25" not in values:
                    raise Stage33Error("BRANCH_FAILED")
                values[name] = backend.fuse(values["dense"], values["bm25"])
            else:
                if "hybrid" not in values:
                    raise Stage33Error("BRANCH_FAILED")
                values[name] = backend.rerank(values["hybrid"], question)
            rows = hit_rows(values[name][:20], documents, kb_id)
            elapsed = perf_counter() - started
            durations[name] = elapsed + (durations["dense"] + durations["bm25"] if name == "hybrid"
                else durations["hybrid"] if name == "hybrid_reranker" else 0)
            basis = truth(qa)
            results[name] = {"status": "SUCCESS", "duration_seconds": durations[name], "hits": rows,
                "metrics": score_units(rows, basis["units"]) if basis["scorable"] else None,
                "document_proxy_metrics": score_units(rows, qa["source_file_ids"], document_only=True)}
        except Exception as error:
            # 任意上游异常原文均不进入评测输出；具体业务错误由原脱敏日志定位。
            values.pop(name, None)
            results[name] = {"status": "FAILED", "error_code": safe_service_error(error),
                "duration_seconds": perf_counter() - started, "hits": [],
                "metrics": None, "document_proxy_metrics": None}
    return {"dataset": qa["dataset"], "qa_id": qa["qa_id"], "truth": truth(qa), "strategies": results}


def run_evaluation(data, run, kb_id, reader, *, backend_factory=Backends, retry_failed=False):
    """遍历全部QA；每题原子检查点可恢复，索引/模型/输入变化拒绝混算，不加载模型直至全库READY。"""
    # 预检/模型身份失败也必须撤销本次正式指标资格，不能沿用上次VALID冒充当前通过。
    write_json(run / "evaluation_validity.json", {"status": "CHECKING"})
    rows = reader(kb_id)
    check_scope(data, rows, kb_id, ready=True)
    scope = scope_identity(rows)
    documents = {row["doc_id"]: doc for doc in data["documents"] for row in [rows[doc["sha256"]]]}
    backend = backend_factory(kb_id)
    identity = backend.identity()
    if identity["embedding"] != next(iter(rows.values()))["model_identity"]:
        raise Stage33Error("QUERY_INDEX_MODEL_MISMATCH")
    config = {"dataset": data["fingerprint"], "scope": scope, "backend": identity}
    config_path = run / "evaluation_identity.json"
    if config_path.exists() and read_json(config_path) != config:
        raise Stage33Error("EVALUATION_IDENTITY_CHANGED")
    write_json(config_path, config)
    try:
        for index, qa in enumerate(data["questions"], 1):
            path = run / "cases" / (digest([qa["dataset"], qa["qa_id"]]) + ".json")
            if path.exists():
                previous = read_json(path)
                if previous.get("qa_sha256") != digest(qa) or previous.get("evaluation_identity") != digest(config):
                    raise Stage33Error("CASE_IDENTITY")
                if not retry_failed or all(v["status"] == "SUCCESS" for v in previous["strategies"].values()):
                    continue
            # 每题前后只读核对集合；不长期持有数据库事务或锁住整个知识库。
            if scope_identity(reader(kb_id)) != scope:
                raise Stage33Error("INDEX_SNAPSHOT_CHANGED")
            result = evaluate_question(qa, documents, kb_id, backend)
            if scope_identity(reader(kb_id)) != scope or backend.identity() != identity:
                raise Stage33Error("INDEX_OR_MODEL_CHANGED")
            result.update(qa_sha256=digest(qa), evaluation_identity=digest(config))
            write_json(path, result)
            print(f"QA已登记：{index}/{len(data['questions'])}；数据集={qa['dataset']}")
        write_json(run / "evaluation_validity.json", {"status": "VALID", "scope": scope})
    except BaseException:
        write_json(run / "evaluation_validity.json", {"status": "BLOCKED_OR_INTERRUPTED", "scope": scope})
        raise


def aggregate(items, strategy):
    """分母始终展示全范围；失败按零计入保守端到端指标，同时单列成功执行分母。"""
    scorable = [r for r in items if r["truth"]["scorable"]]
    successful = [r for r in items if r["strategies"][strategy]["status"] == "SUCCESS"]
    measured = [r["strategies"][strategy]["metrics"] for r in successful if r["truth"]["scorable"]]
    proxy = [r["strategies"][strategy]["document_proxy_metrics"] for r in successful]
    durations = sorted(r["strategies"][strategy]["duration_seconds"] for r in successful)
    result = {"total_questions": len(items), "executed_successfully": len(successful),
        "failed": sum(r["strategies"][strategy]["status"] == "FAILED" for r in items),
        "not_executed": sum(r["strategies"][strategy]["status"] not in {"SUCCESS", "FAILED"} for r in items),
        "scorable_denominator": len(scorable), "successfully_scored": len(measured),
        "unscorable": len(items) - len(scorable), "manual_pending": sum(r["truth"]["manual_pending"] for r in items),
        "metrics": {}, "successful_execution_metrics": {}, "document_proxy_metrics": {}}
    complete = result["not_executed"] == 0
    for metric in ("recall_at_5", "recall_at_10", "mrr_at_20"):
        result["metrics"][metric] = sum(m[metric] for m in measured) / len(scorable) if scorable and complete else None
        result["successful_execution_metrics"][metric] = mean(m[metric] for m in measured) if measured else None
        result["document_proxy_metrics"][metric] = sum(m[metric] for m in proxy) / len(items) if items and complete else None
    result["latency_seconds"] = {"mean": mean(durations) if durations else None,
        "p50": durations[(len(durations) - 1) // 2] if durations else None,
        "p95": durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None,
        "total_successful": sum(durations)}
    return result
