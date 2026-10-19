"""用户执行的RRF/两路组合用例；只写内存Stub，不生成文档或访问数据库/模型/云端。"""

import asyncio
import copy
import io
import logging
from uuid import UUID, uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="需已有core/NLP缓存；跳过不等于验收通过")

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle
from pydantic import ValidationError

from app.core import business_logging as logs
from app.schemas.retrieval import FusionProvenance
from app.services.rag.bm25 import BM25RetrievalError, MAX_QUERY_TERMS
from app.services.rag.dense import DenseRetrievalError
from app.services.rag.llama_index import hybrid_retriever as tool
from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result
from app.services.rag.llama_index.validation import RagAdapterError
from test_dense_retriever import QueryServiceStub, ReadonlySessionStub
from test_dense_sql import stored_pair


def source_nodes(count=3):
    """构造相同库的已有ORM字段Stub；不保存记录、生成文件或声称真实素材验收。"""
    kb_id = uuid4()
    nodes = [chunk_to_node(*stored_pair(kb_id, number), include_embedding=False, log_success=False)
             for number in range(1, count + 1)]
    return kb_id, nodes


def scored(node, score):
    """为已有Node深复制并附模拟分数，避免测试输入之间共享可变证据。"""
    return NodeWithScore(node=node.model_copy(deep=True), score=score)


class BranchStub:
    """只记录原Query并返回预设排名；不模拟文档解析或触发真实模型/SQL。"""

    def __init__(self, kb_id, *, doc_ids, top_k, result, **kwargs):
        """保留调用参数及固定结果，用于核对两路范围和调用次数。"""
        self.kb_id, self.doc_ids, self.top_k = kb_id, doc_ids, top_k
        self.result, self.queries = result, []

    def retrieve(self, query):
        """返回预设Node或异常；不改排序、不调用任何下游服务。"""
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def install_branches(monkeypatch, dense, bm25):
    """替换构造器以测试组合边界；对象仍由生产HybridRetriever统一构造和调用。"""
    branches = []

    def dense_factory(kb_id, **kwargs):
        """仅创建Dense调用记录器，不实例化Embedding服务。"""
        branch = BranchStub(kb_id, result=dense, **kwargs)
        branches.append(branch)
        return branch

    def bm25_factory(kb_id, **kwargs):
        """仅创建BM25调用记录器，不构建真实词频索引。"""
        branch = BranchStub(kb_id, result=bm25, **kwargs)
        branches.append(branch)
        return branch

    monkeypatch.setattr(tool, "DenseRetriever", dense_factory)
    monkeypatch.setattr(tool, "BM25Retriever", bm25_factory)
    return branches


def test_rrf_ranks_scores_provenance_and_top_k():
    """同Chunk两路贡献相加后仅返回一次，来源和原排名/分数保留，不混加原始分值。"""
    kb_id, nodes = source_nodes()
    dense = [scored(nodes[0], 0.9), scored(nodes[1], 0.8)]
    bm25 = [scored(nodes[1], 50.0), scored(nodes[2], 4.0)]
    result = tool.fuse_nodes(dense, bm25, kb_id=kb_id, top_k=3)
    assert [hit.node.node_id for hit in result] == [nodes[1].node_id, nodes[0].node_id, nodes[2].node_id]
    assert len({hit.node.node_id for hit in result}) == 3
    provenance = result[0].node.metadata["fusion"]
    assert provenance == {"strategy": "rrf-v1", "rrf_k": 60, "sources": ["dense", "bm25"],
        "dense_rank": 2, "dense_score": 0.8, "bm25_rank": 1, "bm25_score": 50.0,
        "fusion_score": 1.0 / 62 + 1.0 / 61}
    assert result[0].score == pytest.approx(1.0 / 62 + 1.0 / 61)
    assert tool.fuse_nodes(dense, bm25, kb_id=kb_id, top_k=1)[0].node.node_id == nodes[1].node_id
    # 修改原分数尺度但不改排名，RRF分数和排序不变，展示分数仍反映原值。
    bm25[0].score = 5000.0
    again = tool.fuse_nodes(dense, bm25, kb_id=kb_id, top_k=3)
    assert [hit.score for hit in again] == [hit.score for hit in result]


def test_equal_fusion_scores_use_uuid_tie_break_and_preserve_same_text_chunks():
    """同分按UUID稳定排序；不同Chunk即使正文完全相同也不能按文本hash错误合并。"""
    kb_id, nodes = source_nodes(2)
    assert nodes[0].text == nodes[1].text
    dense = [scored(nodes[1], 0.9), scored(nodes[0], 0.8)]
    bm25 = [scored(nodes[0], 2.0), scored(nodes[1], 1.0)]
    first = tool.fuse_nodes(dense, bm25, kb_id=kb_id)
    second = tool.fuse_nodes(dense, bm25, kb_id=kb_id)
    assert first[0].score == first[1].score
    assert [hit.node.node_id for hit in first] == [str(UUID(int=1)), str(UUID(int=2))]
    assert [hit.model_dump() for hit in first] == [hit.model_dump() for hit in second]


@pytest.mark.parametrize("branch", ["dense", "bm25", "both_empty"])
def test_normal_empty_branches_do_not_expand_scope(branch):
    """正常空支允许剩余支排名，但两支均空只返回空集，不补候选或伪造分数。"""
    kb_id, nodes = source_nodes(1)
    dense = [scored(nodes[0], -0.25)] if branch == "dense" else []
    bm25 = [scored(nodes[0], 2.0)] if branch == "bm25" else []
    result = tool.fuse_nodes(dense, bm25, kb_id=kb_id, doc_ids=[nodes[0].metadata["doc_id"]])
    if branch == "both_empty":
        assert result == []
    else:
        assert len(result) == 1 and result[0].node.metadata["fusion"]["sources"] == [branch]
        assert result[0].score == 1.0 / 61
        assert result[0].node.metadata["fusion"][f"{branch}_score"] == (-0.25 if branch == "dense" else 2.0)


def test_evidence_dto_exclusions_and_input_objects_remain_unchanged():
    """页码、章节、SOURCE、source_spans完整保留；只追加运行时来源，深复制不污染原Node。"""
    kb_id, nodes = source_nodes(1)
    dense, bm25 = [scored(nodes[0], 0.0)], [scored(nodes[0], 2.0)]
    original = copy.deepcopy([hit.model_dump() for hit in dense + bm25])
    fused = tool.fuse_nodes(dense, bm25, kb_id=kb_id)[0]
    result = node_to_result(fused)
    assert result.fusion.sources == ["dense", "bm25"] and result.fusion.dense_score == 0.0
    assert result.retrieval_score == fused.score == result.fusion.fusion_score
    assert result.metadata == nodes[0].metadata["chunk_metadata"]
    assert result.text == nodes[0].text and (result.page_start, result.page_end, result.section) == (3, 4, "原章节")
    assert result.embedding_identity == nodes[0].metadata["embedding_identity"]
    assert fused.node.ref_doc_id == nodes[0].ref_doc_id and fused.node.node_id == str(result.chunk_id)
    assert fused.node.get_content(metadata_mode=MetadataMode.EMBED) == nodes[0].text
    assert fused.node.get_content(metadata_mode=MetadataMode.LLM) == nodes[0].text
    assert "fusion" not in result.metadata and node_to_result(nodes[0]).fusion is None
    assert node_to_result(fused.node).fusion == result.fusion
    fused.node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 99
    result.fusion.sources.clear()
    assert [hit.model_dump() for hit in dense + bm25] == original


@pytest.mark.parametrize("change", ["text", "section", "spans", "model"])
def test_same_chunk_with_conflicting_snapshot_is_rejected(change):
    """两路独立快照中同Chunk证据不一致时全批拒绝，不选一份旧正文掩盖并发变化。"""
    kb_id, nodes = source_nodes(1)
    dense, bm25 = [scored(nodes[0], 0.9)], [scored(nodes[0], 3.0)]
    if change == "text":
        bm25[0].node.text = "PRIVATE_CHANGED_BODY"
    elif change == "section":
        bm25[0].node.metadata["section"] = "新章节"
    elif change == "spans":
        bm25[0].node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 99
    else:
        bm25[0].node.metadata["embedding_identity"]["model_fingerprint"] = "c" * 64
    with pytest.raises(tool.HybridRetrievalError, match="FUSION_CONFLICT") as error:
        tool.fuse_nodes(dense, bm25, kb_id=kb_id)
    assert "PRIVATE_CHANGED_BODY" not in str(error.value)


@pytest.mark.parametrize("change", ["kb", "doc", "id", "source", "order", "duplicate", "score", "unknown", "oversize"])
def test_bad_branch_results_fail_atomically(change):
    """越界、错身份、乱序、重复、未知分数或超量分支均不返回半份候选，不靠过滤掩盖错误。"""
    kb_id, nodes = source_nodes(2)
    hits = [scored(nodes[0], 0.9), scored(nodes[1], 0.8)]
    kwargs = {"kb_id": kb_id}
    if change == "kb":
        kwargs["kb_id"] = uuid4()
    elif change == "doc":
        kwargs["doc_ids"] = [uuid4()]
    elif change == "id":
        hits[0].node.id_ = str(uuid4())
    elif change == "source":
        hits[0].node.relationships.clear()
    elif change == "order":
        hits.reverse()
    elif change == "duplicate":
        hits[1] = scored(nodes[0], 0.8)
    elif change == "score":
        hits[0].score = float("nan")
    elif change == "unknown":
        hits[0].score = None
    else:
        kwargs["branch_top_k"] = 1
    with pytest.raises(tool.HybridRetrievalError, match="FUSION_RESULT"):
        tool.fuse_nodes(hits, [], **kwargs)


def test_no_nested_fusion_or_invalid_provenance_can_be_hidden_in_adapter():
    """禁止重复融合抬高排名；Adapter只接受完整可校验来源，未知metadata仍拒绝。"""
    kb_id, nodes = source_nodes(1)
    fused = tool.fuse_nodes([scored(nodes[0], 0.9)], [], kb_id=kb_id)[0]
    with pytest.raises(tool.HybridRetrievalError, match="FUSION_RESULT"):
        tool.fuse_nodes([fused], [], kb_id=kb_id)
    fields = copy.deepcopy(fused.node.metadata["fusion"])
    for update in ({"sources": ["bm25"]}, {"dense_rank": None}, {"fusion_score": 0.9}, {"rrf_k": True}):
        with pytest.raises(ValidationError):
            FusionProvenance.model_validate(fields | update)
    fused.score = 0.9
    with pytest.raises(RagAdapterError):
        node_to_result(fused)
    nodes[0].metadata["unknown_private_field"] = "PRIVATE_VALUE"
    with pytest.raises(RagAdapterError):
        node_to_result(nodes[0])


def test_lazy_constructor_scope_freezing_and_exactly_one_call_per_branch(monkeypatch):
    """两路共享冻结范围并各调用一次原Query；构造不读取数据库、不加载模型或改写问题。"""
    kb_id, nodes = source_nodes(1)
    branches = install_branches(monkeypatch, [scored(nodes[0], 0.9)], [scored(nodes[0], 2.0)])
    ids = [nodes[0].metadata["doc_id"]]
    retriever = tool.HybridRetriever(kb_id, doc_ids=ids, top_k=30)
    ids.clear()
    assert isinstance(retriever, BaseRetriever) and retriever.top_k == 30 and retriever.rrf_k == 60
    assert all(branch.top_k == 20 and branch.queries == [] for branch in branches)
    assert all(branch.doc_ids == retriever.doc_ids and branch.kb_id == kb_id for branch in branches)
    result = retriever.retrieve(QueryBundle(query_str="PRIVATE_QUERY NASA"))
    assert len(result) == 1 and all(branch.queries == ["PRIVATE_QUERY NASA"] for branch in branches)
    assert retriever.retrieve_results("NASA")[0].fusion.sources == ["dense", "bm25"]


@pytest.mark.parametrize("kwargs", [{"top_k": True}, {"top_k": 0}, {"top_k": 101},
    {"branch_top_k": 101}, {"rrf_k": 0}, {"rrf_k": True}, {"rrf_k": 1001},
    {"doc_ids": []}, {"doc_ids": ["PRIVATE_ID"]}, {"session_factory": 3}, {"embedding_adapter": object()}])
def test_invalid_parameters_do_not_construct_branches(monkeypatch, kwargs):
    """参数边界沿用既有上限，输入错误不能启动两路/模型/SQL。"""
    branches = install_branches(monkeypatch, [], [])
    with pytest.raises(tool.HybridRetrievalError):
        tool.HybridRetriever(uuid4(), **kwargs)
    assert branches == []


@pytest.mark.parametrize("query", ["", None, "PRIVATE_QUERY\x00",
    QueryBundle(query_str="PRIVATE_QUERY", embedding=[1.0]),
    QueryBundle(query_str="PRIVATE_QUERY", custom_embedding_strs=[]),
    QueryBundle(query_str="PRIVATE_QUERY", image_path="PRIVATE_PATH")])
def test_invalid_query_bundle_is_rejected_before_either_branch(monkeypatch, query):
    """无效Query及外部向量/变换文本/图片不消耗Query编码或读库，错误不泄露输入。"""
    branches = install_branches(monkeypatch, [], [])
    retriever = tool.HybridRetriever(uuid4())
    with pytest.raises(tool.HybridRetrievalError, match="FUSION_INPUT") as error:
        retriever.retrieve(query)
    assert all(branch.queries == [] for branch in branches)
    assert "PRIVATE_QUERY" not in str(error.value) and "PRIVATE_PATH" not in str(error.value)


def test_query_term_limit_is_checked_before_dense_gpu_work(monkeypatch):
    """BM25已知超限提前拒绝，不先消耗Dense模型资源，也不静默跳过关键词支路。"""
    branches = install_branches(monkeypatch, [], [])
    retriever = tool.HybridRetriever(uuid4())
    with pytest.raises(tool.HybridRetrievalError, match="FUSION_LIMIT"):
        retriever.retrieve(" ".join(f"term{number}" for number in range(MAX_QUERY_TERMS + 1)))
    assert all(branch.queries == [] for branch in branches)


def test_both_empty_stays_empty_and_repeated_query_order_is_stable(monkeypatch):
    """重复Query使用同样的两路排名，均空不扩库或生成Evidence，正常结果重复顺序稳定。"""
    kb_id, nodes = source_nodes(1)
    branches = install_branches(monkeypatch, [], [])
    retriever = tool.HybridRetriever(kb_id)
    assert retriever.retrieve("NASA") == []
    branches[0].result = [scored(nodes[0], 0.9)]
    first, second = retriever.retrieve("NASA"), retriever.retrieve("NASA")
    assert [hit.model_dump() for hit in first] == [hit.model_dump() for hit in second]
    assert all(branch.queries == ["NASA", "NASA", "NASA"] for branch in branches)


@pytest.mark.parametrize("branch", ["dense", "bm25"])
def test_branch_failure_is_not_a_normal_empty_or_silent_single_route_success(monkeypatch, branch):
    """任一路异常均整体失败；即使另一支已有命中也不冒充Hybrid成功。"""
    kb_id, nodes = source_nodes(1)
    dense = DenseRetrievalError("DENSE_DATABASE") if branch == "dense" else [scored(nodes[0], 0.9)]
    bm25 = BM25RetrievalError("BM25_LIMIT") if branch == "bm25" else []
    branches = install_branches(monkeypatch, dense, bm25)
    with pytest.raises(tool.HybridRetrievalError, match=f"FUSION_{branch.upper()}"):
        tool.HybridRetriever(kb_id).retrieve("NASA")
    assert branches[0].queries == ["NASA"]
    assert branches[1].queries == ([] if branch == "dense" else ["NASA"])


def test_async_boundary_does_not_use_base_callbacks_or_global_llm(monkeypatch):
    """异步仍走受保护入口，不调用含原文payload的基类retrieve或QueryFusion默认LLM。"""
    def forbidden(*args, **kwargs):
        """一旦尝试基类公共检索或LLM配置即报测试错误，不实际启动服务。"""
        pytest.fail("不得使用含正文回调或默认LLM")

    monkeypatch.setattr(BaseRetriever, "retrieve", forbidden)
    from llama_index.core.settings import _Settings
    monkeypatch.setattr(_Settings, "llm", property(forbidden))
    kb_id, nodes = source_nodes(1)
    branches = install_branches(monkeypatch, [scored(nodes[0], 0.9)], [])
    retriever = tool.HybridRetriever(kb_id)
    result = asyncio.run(retriever.aretrieve("NASA"))
    assert len(result) == 1 and all(branch.queries == ["NASA"] for branch in branches)
    assert retriever.callback_manager.handlers == []


def test_existing_real_retriever_classes_share_query_service_and_readonly_sessions():
    """两路共享一次Query编码；BM25分批扫描和命中取回在同一只读快照且保留SQL范围。"""
    from bm25_stubs import StreamingSessionStub

    pytest.importorskip("rank_bm25", reason="缺少已声明rank-bm25；跳过不算通过")
    chunk, document = stored_pair()
    chunk.text = "NASA 既有证据"
    sessions = []

    def factory():
        """分别提供Dense/BM25已加载字段；不连接真实引擎、执行文档解析或写库。"""
        rows = [(chunk, document, 0.25)] if not sessions else [(chunk, document)]
        session = ReadonlySessionStub(rows) if not sessions else StreamingSessionStub(rows)
        sessions.append(session)
        return session

    service = QueryServiceStub()
    retriever = tool.HybridRetriever(chunk.kb_id, doc_ids=[document.doc_id],
        embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    assert sessions == [] and service.queries == []
    result = retriever.retrieve("NASA")
    assert len(result) == 1 and result[0].node.metadata["fusion"]["sources"] == ["dense", "bm25"]
    assert service.queries == ["NASA"] and len(sessions) == 2
    assert all(session.closed and session.settings[0] == "SET TRANSACTION READ ONLY" for session in sessions)
    assert all("document_chunks.doc_id IN" in str(session.statements[0]) for session in sessions)
    assert all(session.new == () and session.dirty == () for session in sessions)
    assert ["READY", "PARTIAL_READY"] in sessions[1].statements[0].compile().params.values()
    assert len(sessions[1].statements) == 3 and all(result.closed for result in sessions[1].results)


def test_summary_logs_are_chinese_safe_and_keep_upstream_failure_code(monkeypatch):
    """汇总日志不输出Query/正文/分数/异常原文；分支失败保留固定安全错误分类。"""
    kb_id, nodes = source_nodes(1)
    nodes[0].text = "PRIVATE_BODY"
    branches = install_branches(monkeypatch, [scored(nodes[0], 0.9)], [])
    stream, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter())
    before = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
    token = logs.CONTEXT.set(None)
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    try:
        retriever = tool.HybridRetriever(kb_id)
        retriever.retrieve("PRIVATE_QUERY")
        branches[1].result = BM25RetrievalError("BM25_DATABASE")
        with pytest.raises(tool.HybridRetrievalError, match="FUSION_BM25"):
            retriever.retrieve("PRIVATE_QUERY")
        branches[0].result = RuntimeError("PRIVATE_SQL SECRET_URL")
        with pytest.raises(tool.HybridRetrievalError):
            retriever.retrieve("PRIVATE_QUERY")
        output = stream.getvalue()
        assert "event=fusion.finished" in output and "event=fusion.failed" in output
        assert "BM25_DATABASE" in output and "FUSION_BM25" in output and "RRF 融合完成" in output
        assert all(value not in output for value in ("PRIVATE_QUERY", "PRIVATE_BODY", "PRIVATE_SQL", "SECRET_URL"))
        assert "event=rag.result_converted" not in output
    finally:
        handler.close()
        logger.handlers, logger.level, logger.propagate, logger.disabled = before
        logs.CONTEXT.reset(token)
