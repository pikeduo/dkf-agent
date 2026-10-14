"""用户执行的Node重排/DTO往返用例；无真实模型、数据库、文件或云调用。"""

import asyncio
import copy
import io
import logging
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.schema", reason="需已有core/NLP缓存；跳过不是通过")

from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import MetadataMode, QueryBundle

from app.core import business_logging as logs
from app.services.rag.llama_index import reranker_adapter as tool
from app.services.rag.llama_index.hybrid_retriever import fuse_nodes
from app.services.rag.llama_index.node_adapter import node_to_result, result_to_node
from app.services.rag.llama_index.validation import RagAdapterError
from app.services.reranking import MODEL_ID, VERSION, RerankError
from test_hybrid_retriever import source_nodes, scored


class ServiceStub:
    """模拟单个项目服务；只提供对应分数/身份，不重新Embedding或加载模型。"""

    def __init__(self, scores):
        """保存可控logit与调用记录，可注入输出及身份变化。"""
        self.scores, self.calls, self.identities = scores, [], 0
        self.change = False

    def identity(self):
        """返回有效内存身份，第二次可模拟模型在推理期间变化。"""
        self.identities += 1
        return {"model_name": MODEL_ID, "version": VERSION, "model_fingerprint":
                ("c" if self.change and self.identities > 1 else "b") * 64,
                "max_length": 8192, "precision": "fp16", "score_type": "logit"}

    def score(self, query, passages):
        """记录原Query和正文顺序，返回模拟logit或私有异常。"""
        self.calls.append((query, list(passages)))
        if isinstance(self.scores, Exception):
            raise self.scores
        return self.scores


def candidates(count=6):
    """用既有Node Stub产生合法RRF候选，不生成文档或真实检索结果。"""
    kb_id, nodes = source_nodes(count)
    dense = [scored(node, 0.9 - number * 0.05) for number, node in enumerate(nodes)]
    return fuse_nodes(dense, [], kb_id=kb_id, top_k=count)


def test_top5_keeps_original_evidence_and_all_scores_without_mutation():
    """Top5只改变当前排序分和运行时重排字段；原ID/正文/页/章节/检索分全部保留。"""
    incoming, service = candidates(), ServiceStub([-2.0, 0.0, 5.0, 3.0, 1.0, 4.0])
    before = copy.deepcopy([hit.model_dump() for hit in incoming])
    adapter = tool.BGERerankerAdapter(service=service)
    assert isinstance(adapter, BaseNodePostprocessor)
    ranked = adapter.postprocess_nodes(incoming, query_str="PRIVATE_QUERY")
    assert [node.score for node in ranked] == [5.0, 4.0, 3.0, 1.0, 0.0]
    assert len(service.calls) == 1 and service.calls[0] == ("PRIVATE_QUERY", [hit.node.text for hit in incoming])
    originals = {hit.node.node_id: hit for hit in incoming}
    for hit in ranked:
        source, result = originals[hit.node.node_id], node_to_result(hit)
        assert result.retrieval_score == source.score and result.fusion_score == source.score
        assert result.rerank_score == hit.score and result.fusion.model_dump() == source.node.metadata["fusion"]
        assert hit.node.text == source.node.text and hit.node.relationships == source.node.relationships
        assert result.page_start == source.node.metadata["page_start"] and result.section == source.node.metadata["section"]
        assert result.metadata == source.node.metadata["chunk_metadata"]
        assert hit.node.get_content(metadata_mode=MetadataMode.LLM) == source.node.text
        assert hit.node.get_content(metadata_mode=MetadataMode.EMBED) == source.node.text
        again = node_to_result(result_to_node(result))
        assert again.model_dump() == result.model_dump()
    ranked[0].node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 99
    assert [hit.model_dump() for hit in incoming] == before


def test_ties_are_stable_and_negative_scores_are_not_probabilities():
    """同logit按业务UUID排序，原负分/零分不被丢弃、sigmoid或阈值化。"""
    incoming, service = candidates(3), ServiceStub([-1.0, -1.0, -1.0])
    adapter = tool.BGERerankerAdapter(service=service)
    first = adapter.postprocess_nodes(incoming, query_str="question")
    second = adapter.postprocess_nodes(incoming, query_str="question")
    assert [hit.node.node_id for hit in first] == sorted(hit.node.node_id for hit in incoming)
    assert [hit.model_dump() for hit in first] == [hit.model_dump() for hit in second]
    assert all(hit.score == -1.0 for hit in first)


def test_default_getter_is_shared_and_construct_empty_are_lazy(monkeypatch):
    """多个Adapter只取得同一既有服务；构造及空候选不访问服务/模型。"""
    service, requests = ServiceStub([1.0]), []

    def shared():
        """模拟项目统一getter，不构造第三方Reranker。"""
        requests.append(True)
        return service

    monkeypatch.setattr(tool, "get_reranker_service", shared)
    one, two = tool.BGERerankerAdapter(), tool.BGERerankerAdapter()
    assert requests == [] and one.postprocess_nodes([], query_str="question") == []
    assert requests == [] and service.identities == 0
    incoming = candidates(1)
    one.postprocess_nodes(incoming, query_str="question")
    two.postprocess_nodes(incoming, query_str="question")
    assert requests == [True, True] and len(service.calls) == 2
    assert "_service" not in one.model_dump()


def test_project_dto_input_and_plain_dense_keep_distinct_scores():
    """非融合的原Dense DTO同样可重排，fusion_score保持None且正文证据能往返。"""
    _, nodes = source_nodes(1)
    original = node_to_result(scored(nodes[0], -0.25))
    result = tool.BGERerankerAdapter(service=ServiceStub([0.0])).postprocess_results([original], query="question")[0]
    assert result.retrieval_score == -0.25 and result.fusion_score is None and result.rerank_score == 0.0
    assert result.metadata == original.metadata and result.text == original.text
    assert node_to_result(result_to_node(result)).model_dump() == result.model_dump()


@pytest.mark.parametrize("query", ["", None, "PRIVATE_QUERY\x00"])
def test_invalid_query_fails_before_shared_service(query):
    """空/非法Query不得触发本地模型或泄露原文。"""
    service = ServiceStub([1.0])
    with pytest.raises(RerankError) as error:
        tool.BGERerankerAdapter(service=service).postprocess_nodes(candidates(1), query_str=query)
    assert service.calls == [] and service.identities == 0 and "PRIVATE_QUERY" not in str(error.value)


@pytest.mark.parametrize("kind", ["duplicate", "cross_kb", "unknown_score", "id", "already_ranked", "oversize"])
def test_invalid_candidates_fail_before_scoring(kind):
    """重复/跨库/坏身份/无原分/重复重排/超量全批拒绝，不用模型掩盖证据问题。"""
    incoming, service = candidates(2), ServiceStub([1.0, 2.0])
    if kind == "duplicate":
        incoming[1] = incoming[0].model_copy(deep=True)
    elif kind == "cross_kb":
        _, other = source_nodes(1)
        incoming[1] = scored(other[0], 0.8)
    elif kind == "unknown_score":
        incoming[0].score = None
    elif kind == "id":
        incoming[0].node.id_ = str(uuid4())
    elif kind == "already_ranked":
        incoming = tool.BGERerankerAdapter(service=ServiceStub([1.0, 2.0])).postprocess_nodes(incoming, query_str="question")
    else:
        incoming = incoming * 51
    with pytest.raises(RerankError):
        tool.BGERerankerAdapter(service=service).postprocess_nodes(incoming, query_str="question")
    assert service.calls == [] and service.identities == 0


@pytest.mark.parametrize("output", [[float("nan")], [], [True], RuntimeError("PRIVATE_BODY PRIVATE_PATH")])
def test_model_failure_or_bad_scores_never_fall_back(output):
    """模型异常/非有限/错误数量不能返回原候选伪装成功，异常脱敏且输入不变。"""
    incoming, service = candidates(1), ServiceStub(output)
    before = incoming[0].model_dump()
    with pytest.raises(RerankError) as error:
        tool.BGERerankerAdapter(service=service).postprocess_nodes(incoming, query_str="question")
    assert incoming[0].model_dump() == before and "PRIVATE" not in str(error.value)


def test_model_identity_change_is_an_atomic_failure():
    """推理期间身份变化拒绝全批，不把同维度/同名称误当成相同Reranker。"""
    service = ServiceStub([1.0])
    service.change = True
    with pytest.raises(RerankError, match="RERANK_MODEL_CHANGED"):
        tool.BGERerankerAdapter(service=service).postprocess_nodes(candidates(1), query_str="question")


def test_async_private_entries_and_callback_boundary(monkeypatch):
    """同步默认/显式异步及私有入口行为一致，不调用框架含正文公共回调或设置LLM。"""
    def forbidden(*args, **kwargs):
        """基类公共入口若被调用即失败，不执行真实模型。"""
        pytest.fail("禁止绕过项目安全入口")

    monkeypatch.setattr(BaseNodePostprocessor, "postprocess_nodes", forbidden)
    incoming, service = candidates(1), ServiceStub([1.0])
    adapter = tool.BGERerankerAdapter(service=service)
    first = adapter._postprocess_nodes(incoming, QueryBundle(query_str="question"))
    second = asyncio.run(adapter.apostprocess_nodes(incoming, query_str="question"))
    assert first[0].model_dump() == second[0].model_dump() and adapter.callback_manager.handlers == []
    with pytest.raises(RerankError, match="RERANK_INPUT"):
        adapter.postprocess_nodes(incoming, QueryBundle(query_str="question"), query_str="question")
    with pytest.raises(RerankError, match="RERANK_INPUT"):
        adapter.postprocess_nodes(incoming, QueryBundle(query_str="question", embedding=[1.0]))


def test_ranked_node_score_tampering_is_rejected():
    """当前排序分、原检索分及fusion来源互相校验，不能改分后仍冒充可信往返。"""
    hit = tool.BGERerankerAdapter(service=ServiceStub([2.0])).postprocess_nodes(candidates(1), query_str="question")[0]
    hit.score = 3.0
    with pytest.raises(RagAdapterError):
        node_to_result(hit)


def test_ranked_candidates_cannot_reenter_rrf_and_original_scores_cannot_be_forged():
    """已重排Dense候选不能回到RRF冒充原分；融合来源也约束被保留的原检索分。"""
    kb_id, nodes = source_nodes(1)
    adapter = tool.BGERerankerAdapter(service=ServiceStub([2.0]))
    ranked = adapter.postprocess_nodes([scored(nodes[0], 0.5)], query_str="question")
    from app.services.rag.llama_index.hybrid_retriever import HybridRetrievalError
    with pytest.raises(HybridRetrievalError, match="FUSION_RESULT"):
        fuse_nodes(ranked, [], kb_id=kb_id)
    fused_ranked = adapter.postprocess_nodes(candidates(1), query_str="question")[0]
    fused_ranked.node.metadata["rerank"]["retrieval_score"] = 999.0
    with pytest.raises(RagAdapterError):
        node_to_result(fused_ranked)


def test_safe_chinese_summary_logs_do_not_expose_payloads():
    """日志仅汇总数量/步骤/错误码，不写Query、正文、模型路径或上游异常。"""
    incoming, service = candidates(1), ServiceStub([1.0])
    incoming[0].node.text = "PRIVATE_BODY"
    before = incoming[0].model_dump()
    stream, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter())
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    try:
        adapter = tool.BGERerankerAdapter(service=service)
        adapter.postprocess_nodes(incoming, query_str="PRIVATE_QUERY")
        service.scores = RuntimeError("PRIVATE_PATH SECRET")
        with pytest.raises(RerankError):
            adapter.postprocess_nodes(incoming, query_str="PRIVATE_QUERY")
        output = stream.getvalue()
        assert "event=rerank.finished" in output and "event=rerank.failed" in output
        assert "原检索与融合分已保留" in output
        assert all(value not in output for value in ("PRIVATE_BODY", "PRIVATE_QUERY", "PRIVATE_PATH", "SECRET"))
        assert incoming[0].model_dump() == before
    finally:
        handler.close()
        logger.handlers, logger.level, logger.propagate, logger.disabled = previous
        logs.CONTEXT.reset(token)
