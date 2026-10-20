"""阶段34只读RAG运行薄层：沿用生产编排，计时并保存同次最终证据。"""

from time import perf_counter

from scripts.stage33_common import ROOT, digest, sha_file
from scripts.stage34_evaluation import Stage34Error, safe_error


class TimedComponent:
    """透明委托既有组件；仅在评测本地收集阶段耗时及最终证据，不改候选或Prompt。"""

    def __init__(self, component, runtime, stage):
        """保存本次调用组件，不修改生产服务或共享实例的方法。"""
        self.component, self.runtime, self.stage = component, runtime, stage

    def __getattr__(self, name):
        """属性原样转交；只为既有三个同步业务方法增加计时。"""
        target = getattr(self.component, name)
        if name not in {"retrieve", "postprocess_nodes", "synthesize"}:
            return target

        def invoke(*args, **kwargs):
            """记录同次调用，不重检索、不裁剪证据、不隐式重试。"""
            self.runtime.stage = self.stage
            started = perf_counter()
            try:
                if self.stage == "synthesis":
                    from app.services.rag.llama_index.node_adapter import node_to_result
                    self.runtime.final_evidence = [node_to_result(node, log_success=False).model_dump(mode="json")
                                                   for node in args[1]]
                return target(*args, **kwargs)
            finally:
                self.runtime.timings[self.stage] = perf_counter() - started

        return invoke


class MeteredLLM:
    """单提供方服务的薄代理；请求意图先保存，且最多一次complete，不增加备用或重试。"""

    def __init__(self, service, runtime, before_cloud):
        """使用原服务元数据并注入本地检查点回调。"""
        self.service, self.runtime, self.before_cloud = service, runtime, before_cloud

    def __getattr__(self, name):
        """所有配置及校验继续由原服务负责，密钥不拷贝进结果。"""
        return getattr(self.service, name)

    def complete(self, system_prompt, user_content):
        """落盘失败不得发HTTP；响应仅保存在受限本地报告，绝不写业务日志。"""
        self.runtime.stage = "llm"
        self.before_cloud()
        result = self.service.complete(system_prompt, user_content)
        self.runtime.raw_output = result
        return result


class Runtime:
    """默认构造不加载模型或联网；只有用户显式evaluate才读取模型身份及执行问答。"""

    def __init__(self, settings):
        """评测固定关闭Fallback，不修改用户配置；一次题目最多一个收费POST。"""
        from app.services.llm import create_llm_service
        self.settings = settings.model_copy(update={"llm_fallback_enabled": False})
        self.llm = create_llm_service(self.settings)
        self.llm.validate_config()
        self.stage, self.timings, self.final_evidence, self.raw_output = "input", {}, [], None
        self._retriever = None
        self._retriever_scope = None
        self.corpus_limits = None

    def identity(self):
        """冻结实际本地模型、提供方及代码/Prompt指纹，不导出密钥或私有路径。"""
        from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
        from app.services.reranking import get_reranker_service
        from app.services.rag.synthesis import RAG_SYSTEM_PROMPT
        provider = self.llm.provider
        endpoint = (self.settings.deepseek_api_base_url if provider == "deepseek"
                    else self.settings.custom_llm_api_base_url)
        files = ("app/services/rag/llama_index/query_engine.py",
            "app/services/rag/llama_index/response_synthesizer.py", "app/services/rag/llama_index/hybrid_retriever.py",
            "app/services/rag/bm25.py", "app/services/rag/evidence.py", "app/services/llm.py",
            "scripts/stage34_runtime.py", "scripts/stage34_evaluation.py")
        return {"embedding": DKFEmbeddingAdapter().query_model_identity(),
            "reranker": get_reranker_service().identity(), "provider": provider,
            "model": self.llm.model_name, "endpoint_sha256": digest(endpoint),
            "context_chars": self.llm.context_chars, "max_tokens": self.llm.max_tokens,
            "fallback": False, "prompt_sha256": digest(RAG_SYSTEM_PROMPT),
            "bm25_corpus_limits": self.corpus_limits,
            "source_sha256": {path: sha_file(ROOT / path) for path in files},
            "retrieval": {"branch_top_k": 20, "fusion_top_k": 30, "final_top_k": 5}}

    def validate_snapshot(self, plan):
        """每题前后核对全部原索引代次与质量；短只读事务，不持有业务锁。"""
        from scripts.stage33_runtime import read_snapshot, scope_identity
        self.corpus_limits = plan["bm25_corpus_limits"]
        if scope_identity(read_snapshot(plan["kb_id"], detailed=False)) != plan["source_scope"]:
            raise Stage34Error("INDEX_CHANGED")

    def query(self, kb_id, question, *, doc_ids, before_cloud):
        """调用原QueryEngine一次；同次最终证据/回答与显式来源反查分开记录。"""
        from app.services.rag.evidence import EvidenceService
        from app.services.rag.llama_index.query_engine import DKFQueryEngine
        from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
        from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
        from app.services.rag.llama_index.llm_adapter import DKFLLMAdapter
        from app.services.rag.llama_index.response_synthesizer import DKFResponseSynthesizer
        from app.services.rag.bm25 import CorpusLimits
        self.stage, self.timings, self.final_evidence, self.raw_output = "input", {}, [], None
        scope = (str(kb_id), tuple(doc_ids) if doc_ids is not None else None)
        if scope != self._retriever_scope:
            # 同时至多保留一个范围的BM25派生索引，避免每题重建全库或缓存多个全库副本。
            self._retriever = HybridRetriever(kb_id, doc_ids=doc_ids, top_k=30,
                                              corpus_limits=CorpusLimits(**self.corpus_limits))
            self._retriever_scope = scope
        engine = DKFQueryEngine(kb_id, doc_ids=doc_ids, top_k=30,
            retriever=TimedComponent(self._retriever, self, "retrieval"),
            reranker=TimedComponent(BGERerankerAdapter(top_n=5), self, "rerank"),
            synthesizer=TimedComponent(DKFResponseSynthesizer(
                DKFLLMAdapter(MeteredLLM(self.llm, self, before_cloud))), self, "synthesis"))
        answer = engine.query(question, allow_cloud=True)
        self.stage = "evidence_verification"
        started = perf_counter()
        traces = []
        if answer.citations:
            try:
                traces = [trace.model_dump(mode="json") for trace in EvidenceService().resolve_answer(answer)]
                source_check = {"status": "VERIFIED"}
            except Exception as exc:
                source_check = {"status": "FAILED", "error_code": safe_error(exc)}
        else:
            source_check = {"status": "NOT_APPLICABLE"}
        self.timings["evidence_verification"] = perf_counter() - started
        return {"answer": answer.model_dump(mode="json"), "final_evidence": self.final_evidence,
            "source_check": source_check, "source_traces": traces,
            "raw_llm_output": self.raw_output, "timings_seconds": dict(self.timings)}
