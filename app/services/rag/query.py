"""项目自有问答入口，显式调用时才导入可选 LlamaIndex，不影响原 API/Worker。"""

from uuid import UUID

from app.schemas.rag import RAGAnswer
from app.services.rag.synthesis import RAGError


def query_knowledge_base(kb_id: UUID | str, question: str, *, doc_ids=None,
                         top_k: int = 30, allow_cloud: bool = False) -> RAGAnswer:
    """复用既有检索/重排服务后合成答案；默认拒绝云外传，不写库、不重建索引。"""
    # 未授权时连可选框架也不导入，避免其首次 NLP 缓存检查产生额外副作用。
    if allow_cloud is not True:
        raise RAGError("RAG_CLOUD_PERMISSION")
    from app.services.rag.llama_index.query_engine import DKFQueryEngine

    return DKFQueryEngine(kb_id, doc_ids=doc_ids, top_k=top_k).query(question, allow_cloud=allow_cloud)
