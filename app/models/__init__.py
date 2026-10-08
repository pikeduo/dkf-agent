"""显式导入模型，使 Alembic 能发现所有知识库表。"""

from app.models.knowledge import Document, DocumentBlock, DocumentChunk, KnowledgeBase

__all__ = ["Document", "DocumentBlock", "DocumentChunk", "KnowledgeBase"]
