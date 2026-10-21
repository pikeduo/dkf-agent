"""项目自有搜索入口，惰性组合既有Hybrid/Reranker，仅返回框架无关Evidence。"""

from uuid import UUID

from app.schemas.retrieval import RetrievalResult
from app.services.rag.dense import validate_doc_ids, validate_query, validate_scope
from app.services.rag.synthesis import RAGError


def search_knowledge_base(kb_id: UUID | str, question: str, *, doc_ids=None,
                          top_k: int = 30, retriever=None, reranker=None) -> list[RetrievalResult]:
    """只读Hybrid→Top5；可注入同范围组件供性能测量复用，HTTP默认行为不变。"""
    stage = "input"
    try:
        kb_id, doc_ids = validate_scope(kb_id, top_k), validate_doc_ids(doc_ids)
        question = validate_query(question)
        stage = "retrieval"
        from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
        from app.services.rag.llama_index.node_adapter import node_to_result
        from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter

        if retriever is not None and (retriever.kb_id != kb_id or retriever.doc_ids != doc_ids
                                      or retriever.top_k != top_k):
            raise RAGError("RAG_INPUT")
        if reranker is not None and reranker.top_n != 5:
            raise RAGError("RAG_INPUT")
        active_retriever = retriever if retriever is not None else HybridRetriever(
            kb_id, doc_ids=doc_ids, top_k=top_k)
        candidates = active_retriever.retrieve(question)
        if not isinstance(candidates, list) or len(candidates) > top_k:
            raise RAGError("RAG_EVIDENCE")
        original = {}
        for node in candidates:
            item = node_to_result(node, log_success=False)
            if (item.kb_id != kb_id or item.chunk_id in original or item.fusion is None
                    or item.rerank is not None or doc_ids is not None and item.doc_id not in doc_ids):
                raise RAGError("RAG_EVIDENCE")
            original[item.chunk_id] = item
        # 空集无需构造重排服务/加载模型，也不调用LLM补齐。
        if not candidates:
            return []
        stage = "rerank"
        active_reranker = reranker if reranker is not None else BGERerankerAdapter(top_n=5)
        final = active_reranker.postprocess_nodes(candidates, query_str=question)
        if not isinstance(final, list) or len(final) != min(5, len(candidates)):
            raise RAGError("RAG_EVIDENCE")
        results, seen = [], set()
        for node in final:
            item = node_to_result(node, log_success=False)
            if (item.chunk_id not in original or item.chunk_id in seen or item.rerank is None
                    or item.model_dump(exclude={"rerank", "rerank_score"}) != original[item.chunk_id].model_dump(
                        exclude={"rerank", "rerank_score"})):
                raise RAGError("RAG_EVIDENCE")
            seen.add(item.chunk_id)
            results.append(item)
        return results
    except Exception as exc:
        error = exc if isinstance(exc, RAGError) else RAGError({
            "input": "RAG_INPUT", "retrieval": "RAG_RETRIEVAL", "rerank": "RAG_RERANK"}[stage])
        raise error from None
