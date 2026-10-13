"""已有 Chunk 与 TextNode/NodeWithScore 的只读互转，不查询、落库或重新处理原件。"""

import logging
from time import perf_counter

from llama_index.core.schema import NodeRelationship, NodeWithScore, RelatedNodeInfo, TextNode
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import NO_VALUE

from app.core.business_logging import log_event
from app.models import Document, DocumentChunk
from app.schemas.retrieval import RetrievalResult
from app.services.rag.llama_index.validation import RagAdapterError, json_snapshot, vector_snapshot

ADAPTER_VERSION = "chunk-node-v1"
CHUNK_FIELDS = ("chunk_id", "kb_id", "doc_id", "text", "page_start", "page_end", "section", "chunk_metadata")
DOCUMENT_FIELDS = ("doc_id", "kb_id", "file_name", "file_type", "index_config")
METADATA_FIELDS = {"dkf_adapter_version", "kb_id", "doc_id", "chunk_id", "file_name", "file_type",
                   "page_start", "page_end", "section", "chunk_metadata", "embedding_identity"}


def loaded_snapshot(instance, fields: tuple[str, ...]) -> dict:
    """只读取已加载的 ORM 属性，未加载时失败；禁止隐式 SELECT/autoflush 或猜缺失证据。"""

    state = inspect(instance)
    values = {field: state.attrs[field].loaded_value for field in fields}
    if any(value is NO_VALUE for value in values.values()):
        raise RagAdapterError("RAG_ADAPTER_UNLOADED")
    return values


def chunk_to_node(chunk: DocumentChunk, document: Document, *, include_embedding: bool = True,
                  log_success: bool = True) -> TextNode:
    """用原 chunk_id/证据构建 Node，不重编码；批量调用可关闭逐项成功日志，错误仍记录。"""

    started = perf_counter()
    try:
        if (not isinstance(chunk, DocumentChunk) or not isinstance(document, Document)
                or type(include_embedding) is not bool or type(log_success) is not bool):
            raise RagAdapterError("RAG_ADAPTER_INPUT")
        source = loaded_snapshot(chunk, CHUNK_FIELDS + (("embedding",) if include_embedding else ()))
        owner = loaded_snapshot(document, DOCUMENT_FIELDS)
        if source["doc_id"] != owner["doc_id"] or source["kb_id"] != owner["kb_id"]:
            raise RagAdapterError("RAG_ADAPTER_IDENTITY")
        identity = None
        config = owner["index_config"]
        if config is not None:
            config = json_snapshot(config)
            completed = config.get("result")
            if completed is not None:
                identity = json_snapshot(completed["model"])
        result = RetrievalResult(kb_id=source["kb_id"], doc_id=source["doc_id"], chunk_id=source["chunk_id"],
            file_name=owner["file_name"], file_type=owner["file_type"], text=source["text"],
            page_start=source["page_start"], page_end=source["page_end"], section=source["section"],
            metadata=json_snapshot(source["chunk_metadata"]), embedding_identity=identity)
        metadata = {"dkf_adapter_version": ADAPTER_VERSION, "kb_id": str(result.kb_id),
                    "doc_id": str(result.doc_id), "chunk_id": str(result.chunk_id),
                    "file_name": result.file_name, "file_type": result.file_type,
                    "page_start": result.page_start, "page_end": result.page_end, "section": result.section,
                    "chunk_metadata": result.metadata, "embedding_identity": result.embedding_identity}
        embedding = None
        if include_embedding and source["embedding"] is not None:
            embedding = vector_snapshot(source["embedding"])
        node = TextNode(id_=str(result.chunk_id), text=result.text, metadata=metadata, embedding=embedding,
            relationships={NodeRelationship.SOURCE: RelatedNodeInfo(node_id=str(result.doc_id),
                metadata={"kb_id": str(result.kb_id), "file_name": result.file_name, "file_type": result.file_type})},
            # 保留 Citation 元数据，但不把 ID/页码/原始 metadata 自动混入正文编码或 LLM 内容。
            excluded_embed_metadata_keys=sorted(metadata), excluded_llm_metadata_keys=sorted(metadata))
    except Exception as exc:
        error = exc if isinstance(exc, RagAdapterError) else RagAdapterError("RAG_ADAPTER_INPUT")
        log_event("rag.adapter_failed", level=logging.WARNING, stage="chunk_to_node", error_code=error.code)
        raise error from None
    if log_success:
        log_event("rag.node_converted", kb_id=result.kb_id, doc_id=result.doc_id, chunk_id=result.chunk_id,
                  artifact_reused=embedding is not None, duration_ms=(perf_counter() - started) * 1000)
    return node


def node_to_result(value: TextNode | NodeWithScore) -> RetrievalResult:
    """从 Node 还原项目 DTO；分数 None/0 原样保留，读取原 text 而非拼接 metadata 的内容。"""

    started = perf_counter()
    try:
        score = value.score if isinstance(value, NodeWithScore) else None
        node = value.node if isinstance(value, NodeWithScore) else value
        if not isinstance(node, TextNode) or set(node.metadata) != METADATA_FIELDS:
            raise RagAdapterError("RAG_ADAPTER_NODE")
        metadata = json_snapshot(node.metadata)
        if metadata["dkf_adapter_version"] != ADAPTER_VERSION:
            raise RagAdapterError("RAG_ADAPTER_NODE")
        result = RetrievalResult(kb_id=metadata["kb_id"], doc_id=metadata["doc_id"], chunk_id=metadata["chunk_id"],
            file_name=metadata["file_name"], file_type=metadata["file_type"], text=node.text,
            page_start=metadata["page_start"], page_end=metadata["page_end"], section=metadata["section"],
            metadata=metadata["chunk_metadata"], embedding_identity=metadata["embedding_identity"],
            retrieval_score=score)
        if (node.node_id != str(result.chunk_id) or node.ref_doc_id != str(result.doc_id)
                or node.source_node is None
                or node.source_node.metadata != {"kb_id": str(result.kb_id), "file_name": result.file_name,
                                                 "file_type": result.file_type}):
            raise RagAdapterError("RAG_ADAPTER_IDENTITY")
    except (RagAdapterError, ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
        error = exc if isinstance(exc, RagAdapterError) else RagAdapterError("RAG_ADAPTER_INPUT")
        log_event("rag.adapter_failed", level=logging.WARNING, stage="node_to_result", error_code=error.code)
        raise error from None
    log_event("rag.result_converted", kb_id=result.kb_id, doc_id=result.doc_id, chunk_id=result.chunk_id,
              duration_ms=(perf_counter() - started) * 1000)
    return result
