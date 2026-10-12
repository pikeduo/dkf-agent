"""用户执行的运行时 Adapter 测试，不生成原始文档、不连接数据库或调用真实模型。"""

import asyncio
import copy
from uuid import uuid4

import pytest

# 可选 core 尚未由用户安装时跳过，而非破坏阶段 1～15 的既有测试收集；跳过不算验收通过。
pytest.importorskip("llama_index.core.schema", reason="用户需先手动安装已确认的 llama-index-core")

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.schema import MetadataMode, NodeRelationship, NodeWithScore, RelatedNodeInfo, TextNode

from app.models import Document, DocumentChunk
from app.schemas.retrieval import RetrievalResult
from app.services.rag.llama_index import embedding_adapter
from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result
from app.services.rag.llama_index.validation import RagAdapterError


@pytest.fixture
def source():
    """只构造未绑定 Session 的已有字段 Stub，不读写原件、执行解析或保存数据库记录。"""

    doc_id, kb_id, chunk_id = uuid4(), uuid4(), uuid4()
    text = "  第一行原式 =A1+B1\n第二行解释  "
    metadata = {"chunk_index": 1, "page_semantics": "physical", "source_parse_task_id": str(uuid4()),
        "source_spans": [{"block_id": str(uuid4()), "block_index": 1, "page": 3,
            "section": "原章节", "start_char": 0, "end_char": len(text),
            "chunk_start_char": 0, "chunk_end_char": len(text), "role": "content", "bbox": None}],
        "file_hash": "a" * 64}
    model = {"model_name": "BAAI/bge-m3", "model_fingerprint": "b" * 64, "dimension": 1024,
             "normalized": True, "version": "bge-m3-dense-v1", "max_length": 8192, "precision": "fp16"}
    document = Document(doc_id=doc_id, kb_id=kb_id, file_name="已有资料.pdf", file_type="pdf",
                        file_path="PRIVATE_PATH", index_config={"result": {"model": model}})
    chunk = DocumentChunk(chunk_id=chunk_id, doc_id=doc_id, kb_id=kb_id, text=text,
        page_start=3, page_end=4, section="原章节", chunk_metadata=metadata,
        embedding=[1.0] + [0.0] * 1023)
    return chunk, document


def test_chunk_node_result_roundtrip_is_stable_and_preserves_all_evidence(source):
    """业务编号及全部页码/章节/字符证据往返不变，同一 Chunk 重复转换不生成随机 Node ID。"""

    chunk, document = source
    first, second = chunk_to_node(chunk, document), chunk_to_node(chunk, document)
    assert first.model_dump() == second.model_dump()
    assert first.node_id == str(chunk.chunk_id) and first.ref_doc_id == str(document.doc_id)
    assert first.text == chunk.text and first.embedding == chunk.embedding
    assert first.embedding is not chunk.embedding
    assert first.metadata["chunk_id"] == str(chunk.chunk_id)
    assert first.metadata["file_name"] == document.file_name
    assert first.metadata["page_start"] == 3 and first.metadata["page_end"] == 4
    assert first.metadata["section"] == "原章节"
    result = node_to_result(first)
    assert isinstance(result, RetrievalResult) and result.retrieval_score is None
    assert (result.kb_id, result.doc_id, result.chunk_id) == (chunk.kb_id, chunk.doc_id, chunk.chunk_id)
    assert result.text == chunk.text and result.metadata == chunk.chunk_metadata
    assert result.embedding_identity["model_fingerprint"] == "b" * 64
    assert "embedding" not in result.model_dump() and "PRIVATE_PATH" not in first.model_dump_json()
    assert first.get_content(metadata_mode=MetadataMode.EMBED) == chunk.text
    assert first.get_content(metadata_mode=MetadataMode.LLM) == chunk.text


@pytest.mark.parametrize("score", [None, 0.0, -0.25, 0.75])
def test_scored_node_keeps_unknown_zero_and_finite_scores(source, score):
    """未知分数不变为 0，真实 0/负值不被 truthiness 判断或概率化误改。"""

    result = node_to_result(NodeWithScore(node=chunk_to_node(*source), score=score))
    assert result.retrieval_score == score
    assert result.page_start == 3 and result.page_end == 4 and result.section == "原章节"


def test_metadata_and_vector_mutations_do_not_change_orm_objects(source):
    """所有运行时字段深复制，修改 Node/DTO 不能改变 ORM metadata 或既有向量。"""

    chunk, document = source
    original = copy.deepcopy(chunk.chunk_metadata)
    node = chunk_to_node(chunk, document)
    result = node_to_result(node)
    result.metadata["source_spans"][0]["page"] = 77
    node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 99
    node.embedding[0] = 0.0
    assert chunk.chunk_metadata == original and chunk.embedding[0] == 1.0
    assert document.index_config["result"]["model"]["dimension"] == 1024


def test_null_section_logical_page_and_missing_embedding_are_not_fabricated(source):
    """逻辑页/空章节/尚未生成向量原样保留；不调用 Embedding 补齐数据。"""

    chunk, document = source
    chunk.page_start = chunk.page_end = 1
    chunk.section, chunk.embedding = None, None
    chunk.chunk_metadata = {"page_semantics": "logical", "source_spans": []}
    document.file_type, document.index_config = "xlsx", None
    node = chunk_to_node(chunk, document)
    result = node_to_result(node)
    assert node.embedding is None and result.embedding_identity is None
    assert result.page_start == result.page_end == 1 and result.section is None
    assert result.metadata == chunk.chunk_metadata


@pytest.mark.parametrize("change", ["doc", "kb", "missing_field", "page", "text", "vector"])
def test_invalid_chunk_input_fails_safely_without_reading_private_source(source, change):
    """身份、页码、空文本、无效向量和未加载字段均安全拒绝，不泄漏正文或原件路径。"""

    chunk, document = source
    if change == "doc":
        document.doc_id = uuid4()
    elif change == "kb":
        document.kb_id = uuid4()
    elif change == "missing_field":
        del chunk.section
    elif change == "page":
        chunk.page_end = 0
    elif change == "text":
        chunk.text = " "
    else:
        chunk.embedding = [2.0] + [0.0] * 1023
    with pytest.raises(RagAdapterError) as error:
        chunk_to_node(chunk, document)
    assert "PRIVATE_PATH" not in str(error.value) and "第一行原式" not in str(error.value)


@pytest.mark.parametrize("change", ["id", "source", "kb", "missing", "version", "score", "page"])
def test_invalid_node_identity_metadata_and_score_are_not_silently_fixed(source, change):
    """Node ID、来源、契约、页码及分数异常直接拒绝，不修正或伪造 Citation 字段。"""

    node = chunk_to_node(*source)
    value = node
    if change == "id":
        node.id_ = str(uuid4())
    elif change == "source":
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=str(uuid4()))
    elif change == "kb":
        node.metadata["kb_id"] = str(uuid4())
    elif change == "missing":
        node.metadata.pop("section")
    elif change == "version":
        node.metadata["dkf_adapter_version"] = "PRIVATE_VERSION"
    elif change == "page":
        node.metadata["page_start"] = True
    else:
        value = NodeWithScore(node=node, score=float("nan"))
    with pytest.raises(RagAdapterError) as error:
        node_to_result(value)
    assert "PRIVATE_VERSION" not in str(error.value) and "第一行原式" not in str(error.value)


class EmbeddingStub:
    """只记录显式 Query 调用，不实例化真实模型，文档编码调用立即失败。"""

    def __init__(self):
        """初始化调用计数；测试只有内存数值，不消耗 GPU/云额度。"""

        self.queries = []

    def embed_query(self, query):
        """返回合法固定单位向量，仅验证委托而非真实检索质量。"""

        self.queries.append(query)
        return [1.0] + [0.0] * 1023

    def embed_documents(self, texts):
        """文档编码不得在此阶段由框架启动，任何调用都是测试失败。"""

        pytest.fail("框架不应重新生成文档向量")


def test_embedding_construction_is_lazy_query_reuses_shared_backend_and_private_state_is_not_serialized(monkeypatch):
    """构造不获取/加载模型，显式 Query 才使用现有服务，不添加前缀或框架缓存。"""

    stub, requests = EmbeddingStub(), []

    def shared_service():
        """模拟项目共享 getter，重复返回同一服务而非构造新模型。"""

        requests.append(True)
        return stub

    monkeypatch.setattr(embedding_adapter, "get_embedding_service", shared_service)
    adapter = DKFEmbeddingAdapter()
    assert isinstance(adapter, BaseEmbedding) and requests == [] and adapter.embeddings_cache is None
    query = "  PRIVATE_QUERY\n保留原文  "
    assert len(adapter.get_query_embedding(query)) == 1024
    assert len(asyncio.run(adapter.aget_query_embedding(query))) == 1024
    assert stub.queries == [query, query] and len(requests) == 2
    assert "PRIVATE_QUERY" not in adapter.model_dump_json() and "_service" not in adapter.model_dump()


def test_existing_node_vectors_are_returned_without_reencoding_or_modification(source):
    """Transform 只复用已有向量，同步/异步均不改变 Node，也不调用任何模型方法。"""

    stub, node = EmbeddingStub(), chunk_to_node(*source)
    adapter = DKFEmbeddingAdapter(stub)
    nodes, before = [node], node.model_dump()
    assert adapter(nodes) is nodes and asyncio.run(adapter.acall(nodes)) is nodes
    assert node.model_dump() == before and stub.queries == []
    node.embedding = None
    with pytest.raises(RagAdapterError, match="RAG_DOCUMENT_EMBEDDING_FORBIDDEN"):
        adapter(nodes)


def test_all_document_embedding_entrypoints_reject_framework_reingestion():
    """所有公开/私有文档编码入口拒绝，不意外进入框架二次文档 Embedding。"""

    adapter = DKFEmbeddingAdapter(EmbeddingStub())
    for method, value in ((adapter.get_text_embedding, "PRIVATE_TEXT"),
                           (adapter.get_text_embedding_batch, ["PRIVATE_TEXT"]),
                           (adapter._get_text_embedding, "PRIVATE_TEXT"),
                           (adapter._get_text_embeddings, ["PRIVATE_TEXT"])):
        with pytest.raises(RagAdapterError, match="RAG_DOCUMENT_EMBEDDING_FORBIDDEN") as error:
            method(value)
        assert "PRIVATE_TEXT" not in str(error.value)
    for method, value in ((adapter.aget_text_embedding, "PRIVATE_TEXT"),
                           (adapter.aget_text_embedding_batch, ["PRIVATE_TEXT"]),
                           (adapter._aget_text_embedding, "PRIVATE_TEXT"),
                           (adapter._aget_text_embeddings, ["PRIVATE_TEXT"])):
        with pytest.raises(RagAdapterError, match="RAG_DOCUMENT_EMBEDDING_FORBIDDEN"):
            asyncio.run(method(value))


def test_backend_failure_is_sanitized_and_never_falls_back(monkeypatch):
    """原始模型/连接错误固定脱敏，不自动调用其他模型或读取原件。"""

    def unavailable():
        """只模拟私有异常，不使本机依赖/服务发生故障。"""

        raise RuntimeError("PRIVATE_MODEL_PATH")

    monkeypatch.setattr(embedding_adapter, "get_embedding_service", unavailable)
    with pytest.raises(RagAdapterError, match="RAG_ADAPTER_EMBEDDING") as error:
        DKFEmbeddingAdapter().get_query_embedding("PRIVATE_QUERY")
    assert "PRIVATE_MODEL_PATH" not in str(error.value) and "PRIVATE_QUERY" not in str(error.value)


def test_transform_rejects_generators_and_invalid_existing_vectors(source):
    """不消费一次性生成器后丢失 Node，不把无效已有向量当作可复用结果。"""

    adapter, node = DKFEmbeddingAdapter(EmbeddingStub()), chunk_to_node(*source)
    with pytest.raises(RagAdapterError, match="RAG_ADAPTER_NODE"):
        adapter(iter([node]))
    node.embedding = [0.0] * 1024
    with pytest.raises(RagAdapterError, match="RAG_ADAPTER_VECTOR"):
        adapter([node])
