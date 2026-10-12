"""统一的本地 BGE-M3 Dense Embedding 入口，导入时不加载模型。"""

from app.services.embedding.service import EmbeddingService, embed_documents, embed_query, get_embedding_service

__all__ = ["EmbeddingService", "embed_documents", "embed_query", "get_embedding_service"]
