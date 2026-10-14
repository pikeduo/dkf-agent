"""用户运行的Knowledge API Mock；不启动服务、连接数据库、运行模型或请求云端。"""

import asyncio
import copy
import io
import json
import logging
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import knowledge as api
from app.core import business_logging as logs
from app.schemas.rag import RAGAnswer
from app.schemas.retrieval import FusionProvenance, RerankProvenance, RetrievalResult
from app.services import knowledge_query as gateway
from app.services.deepseek import DeepSeekError
from app.services.llm import LLMError
from app.services.rag.evidence import make_citation
from app.services.rag.synthesis import RAGError, insufficient_answer


def evidence_item(kb_id, doc_id):
    """构造仅用于响应契约的内存证据/分数，不伪造真实数据库验收。"""
    score = 2 / 61
    fusion = FusionProvenance(rrf_k=60, sources=["dense", "bm25"], dense_rank=1,
        dense_score=.5, bm25_rank=1, bm25_score=1., fusion_score=score)
    rerank = RerankProvenance(model_name="BAAI/bge-reranker-v2-m3", model_fingerprint="a" * 64,
        version="bge-reranker-v2-m3-logit-v1", max_length=8192, precision="fp16", score_type="logit",
        input_rank=1, retrieval_score=score, fusion_score=score, rerank_score=1.5)
    return RetrievalResult(kb_id=kb_id, doc_id=doc_id, chunk_id=uuid4(), file_name="已有文档.pptx",
        file_type="pptx", text="PRIVATE_DOCUMENT_TEXT", page_start=95, page_end=95, section="原章节",
        metadata={"page_semantics": "logical", "source_spans": [{"block_id": str(uuid4()), "page": 95}]},
        retrieval_score=score, fusion=fusion, fusion_score=score, rerank=rerank, rerank_score=1.5)


def request(app, path, *, payload=None, content=None):
    """仅通过ASGI内存请求真实路由，不监听端口或发网络请求。"""
    async def invoke():
        """每次独立HTTP客户端，无真实网络transport或API启动。"""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            if content is not None:
                return await client.post(path, content=content, headers={"Content-Type": "application/json"})
            return await client.post(path, json=payload)
    return asyncio.run(invoke())


@pytest.fixture
def context(monkeypatch):
    """注入内存检索/问答边界，实际Schema/Service/路由及HTTP日志仍执行，不使用真实资源。"""
    kb_id, doc_id = uuid4(), uuid4()
    item = evidence_item(kb_id, doc_id)
    answer = RAGAnswer(status="success", answer="PRIVATE_ANSWER [1]",
        citations=[make_citation(item, 1)], insufficient_evidence=False)
    state = {"search_calls": [], "query_calls": [], "items": [item], "answer": answer,
             "error": None, "kb_id": kb_id, "doc_id": doc_id,
             "body": {"kb_id": str(kb_id), "question": "PRIVATE_QUERY", "doc_ids": [str(doc_id)]}}

    def search(kb, question, **options):
        """模拟只读项目搜索边界，记录是否扩大范围或调用次数。"""
        state["search_calls"].append((kb, question, options))
        if state["error"] is not None:
            raise state["error"]
        return state["items"]

    def query(kb, question, **options):
        """模拟单次项目问答，无DeepSeek客户端或任何收费调用。"""
        state["query_calls"].append((kb, question, options))
        if state["error"] is not None:
            raise state["error"]
        return state["answer"]

    monkeypatch.setattr(gateway, "search_knowledge_base", search)
    monkeypatch.setattr(gateway, "query_knowledge_base", query)
    app = FastAPI()
    app.include_router(api.router)
    app.add_middleware(logs.RequestLoggingMiddleware)
    state["app"] = app
    stream, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter("api"))
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    state["logs"] = stream
    try:
        yield state
    finally:
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
        handler.close()


def test_search_returns_original_dkf_evidence_and_never_invokes_query(context):
    """搜索返回原业务字段/三类分数/逻辑页；不返回Node或发起问答。"""
    result = request(context["app"], "/api/knowledge/search", payload=context["body"])
    assert result.status_code == 200 and result.headers["X-Request-ID"]
    assert result.json() == {"status": "success", "evidence": [context["items"][0].model_dump(mode="json")]}
    kb, question, options = context["search_calls"][0]
    assert kb == context["kb_id"] and question == "PRIVATE_QUERY"
    assert options == {"doc_ids": [context["doc_id"]], "top_k": 30} and not context["query_calls"]
    output = context["logs"].getvalue()
    assert "knowledge.finished" in output and "API" in output and "PRIVATE" not in output


@pytest.mark.parametrize("allow", [None, False])
def test_query_permission_is_required_before_calling_any_backend(context, allow):
    """省略/false许可时返回403，不检索、不加载模型或调LLM。"""
    payload = copy.deepcopy(context["body"])
    if allow is not None:
        payload["allow_cloud"] = allow
    response = request(context["app"], "/api/knowledge/query", payload=payload)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "RAG_CLOUD_PERMISSION"
    assert not context["query_calls"] and not context["search_calls"]


def test_query_preserves_answer_citations_and_single_explicit_authorized_call(context):
    """显式请求才返回原RAGAnswer，无新增合成/引用重建或框架对象。"""
    result = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": True, "top_k": 20})
    assert result.status_code == 200 and result.json() == context["answer"].model_dump(mode="json")
    assert len(context["query_calls"]) == 1 and not context["search_calls"]
    assert context["query_calls"][0][2] == {"doc_ids": [context["doc_id"]], "top_k": 20, "allow_cloud": True}
    assert "PRIVATE" not in context["logs"].getvalue()


def test_empty_search_and_true_insufficiency_are_success_not_service_failures(context):
    """正常空结果/证据不足保持200，异常不得走这两个分支。"""
    context["items"], context["answer"] = [], insufficient_answer()
    search = request(context["app"], "/api/knowledge/search", payload=context["body"])
    query = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": True})
    assert search.status_code == query.status_code == 200
    assert search.json() == {"status": "empty", "evidence": []}
    assert query.json()["insufficient_evidence"] and query.json()["citations"] == []


@pytest.mark.parametrize("invalid", [
    {"kb_id": "invalid"}, {"kb_id": str(UUID(int=0))}, {"question": " "}, {"question": 1},
    {"question": "PRIVATE\x00"}, {"question": "x" * 32769}, {"question": "\ud800"},
    {"doc_ids": []}, {"doc_ids": ["invalid"]}, {"doc_ids": [str(UUID(int=0))]},
    {"doc_ids": [str(uuid4())] * 101}, {"top_k": True}, {"top_k": 0}, {"top_k": 101},
    {"top_k": "20"}, {"top_k": None}, {"PRIVATE_EXTRA": "PRIVATE_TOKEN"},
])
def test_invalid_input_is_safe_422_and_does_not_reach_services(context, invalid):
    """错误输入/控制符/非法Unicode不回显input/ctx，更不触发实际推理。"""
    raw = json.dumps(context["body"] | invalid, ensure_ascii=True).encode()
    response = request(context["app"], "/api/knowledge/search", content=raw)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "KNOWLEDGE_INPUT"
    assert "PRIVATE" not in response.text and "PRIVATE" not in context["logs"].getvalue()
    assert not context["query_calls"] and not context["search_calls"]


def test_malformed_json_and_nonboolean_cloud_permission_are_safe(context):
    """解析错误及字符串许可不能转换为true或泄露原始请求体。"""
    broken = request(context["app"], "/api/knowledge/search", content=b'{"PRIVATE_TOKEN":')
    permission = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": "true"})
    assert broken.status_code == permission.status_code == 422
    assert "PRIVATE" not in broken.text and not context["query_calls"]


def test_document_scope_is_deduplicated_but_never_widened(context):
    """重复UUID冻结去重，None才表示全库；有指定范围时后端与返回必须同范围。"""
    payload = context["body"] | {"doc_ids": [str(context["doc_id"])] * 2}
    assert request(context["app"], "/api/knowledge/search", payload=payload).status_code == 200
    assert context["search_calls"][0][2]["doc_ids"] == [context["doc_id"]]
    context["items"][0].doc_id = uuid4()
    response = request(context["app"], "/api/knowledge/search", payload=payload)
    assert response.status_code == 502 and "evidence" not in response.json()


@pytest.mark.parametrize("omit", [True, False])
def test_omitted_or_null_doc_scope_keeps_explicit_whole_kb_semantics(context, omit):
    """只有省略/null才传None，仍固定原kb_id，不把正常全库请求误当成空范围。"""
    payload = copy.deepcopy(context["body"])
    if omit:
        payload.pop("doc_ids")
    else:
        payload["doc_ids"] = None
    response = request(context["app"], "/api/knowledge/search", payload=payload)
    assert response.status_code == 200
    assert context["search_calls"][0][0] == context["kb_id"]
    assert context["search_calls"][0][2]["doc_ids"] is None


def test_query_cross_document_citation_is_rejected_as_invalid_response(context):
    """问答引用同样检查请求文档范围，不因Schema类型合法而发布其他文档来源。"""
    context["answer"].citations[0].doc_id = uuid4()
    response = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": True})
    assert response.status_code == 502 and "answer" not in response.json()
    assert "PRIVATE" not in response.text and len(context["query_calls"]) == 1


@pytest.mark.parametrize("error,code", [
    (RAGError("RAG_RETRIEVAL"), 503), (RAGError("RAG_RERANK"), 503), (RAGError("RAG_OUTPUT"), 502),
    (RAGError("RAG_CONTEXT_LIMIT"), 413), (DeepSeekError("DEEPSEEK_AUTH"), 503),
    (DeepSeekError("DEEPSEEK_RATE_LIMIT"), 429), (DeepSeekError("DEEPSEEK_NETWORK"), 503),
    (LLMError("LLM_AUTH", provider="custom"), 503), (LLMError("LLM_QUOTA", provider="custom"), 503),
    (LLMError("LLM_RATE_LIMIT", provider="custom"), 429), (LLMError("LLM_NETWORK", provider="custom"), 503),
    (LLMError("LLM_RESPONSE", provider="custom"), 502), (LLMError("LLM_TRUNCATED", provider="custom"), 502),
    (RuntimeError("PRIVATE_TOKEN PRIVATE_PATH PRIVATE_QUERY"), 500),
])
def test_failures_do_not_become_insufficiency_or_retry(context, error, code):
    """部署/上游错误分别返回安全状态码；一个请求只调用一次，不伪装不足或重试收费。"""
    context["error"] = error
    response = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": True})
    assert response.status_code == code and set(response.json()) == {"detail"}
    assert len(context["query_calls"]) == 1 and "PRIVATE" not in response.text
    output = context["logs"].getvalue()
    assert "knowledge.failed" in output and "knowledge.finished" not in output and "PRIVATE" not in output


@pytest.mark.parametrize("corruption", ["cross_kb", "duplicate", "nan", "framework"])
def test_invalid_backend_evidence_is_rejected_not_partially_published(context, corruption):
    """坏证据全批拒绝并记录失败；非有限数不能转为空值后发布200或完成事件。"""
    if corruption == "cross_kb":
        context["items"][0].kb_id = uuid4()
    elif corruption == "duplicate":
        context["items"].append(context["items"][0])
    elif corruption == "nan":
        context["items"][0].metadata["invalid"] = float("nan")
    else:
        context["items"] = [object()]
    response = request(context["app"], "/api/knowledge/search", payload=context["body"])
    assert response.status_code == 502 and response.json()["detail"]["code"] == "KNOWLEDGE_RESULT"
    assert "PRIVATE" not in response.text
    output = context["logs"].getvalue()
    assert "knowledge.failed" in output and "knowledge.finished" not in output and "PRIVATE" not in output


@pytest.mark.parametrize("endpoint", ["search", "query"])
@pytest.mark.parametrize("field", ["metadata", "embedding_identity"])
@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "negative_inf"])
def test_nested_nonfinite_values_fail_before_json_conversion(context, endpoint, field, invalid_number):
    """两条路径的嵌套来源/身份数值须转换前拒绝，不伪装None、不重试或修改原证据。"""
    item = context["items"][0] if endpoint == "search" else context["answer"].citations[0]
    value = {"nested": [{"PRIVATE_KEY": invalid_number}]}
    if field == "metadata":
        item.metadata["invalid"] = value
    else:
        item.embedding_identity = value
    response = request(context["app"], f"/api/knowledge/{endpoint}",
                       payload=context["body"] | ({"allow_cloud": True} if endpoint == "query" else {}))
    assert response.status_code == 502 and response.json()["detail"]["code"] == "KNOWLEDGE_RESULT"
    calls = context["search_calls"] if endpoint == "search" else context["query_calls"]
    assert len(calls) == 1
    original = item.metadata["invalid"] if field == "metadata" else item.embedding_identity
    assert original["nested"][0]["PRIVATE_KEY"] is invalid_number
    output = context["logs"].getvalue()
    assert "knowledge.failed" in output and "knowledge.finished" not in output
    assert "PRIVATE" not in response.text and "PRIVATE" not in output


@pytest.mark.parametrize("endpoint", ["search", "query"])
def test_valid_unknown_values_and_non_numeric_nan_text_remain_unchanged(context, endpoint):
    """合法None、有限数与字面文本NaN/Infinity保持原样，不做字符串乱码或数值猜测。"""
    item = context["items"][0] if endpoint == "search" else context["answer"].citations[0]
    values = {"nested": [None, 0.0, -1.25, 100, "NaN", "Infinity"]}
    item.metadata["valid_values"] = copy.deepcopy(values)
    response = request(context["app"], f"/api/knowledge/{endpoint}",
                       payload=context["body"] | ({"allow_cloud": True} if endpoint == "query" else {}))
    assert response.status_code == 200
    items = response.json()["evidence"] if endpoint == "search" else response.json()["citations"]
    assert items[0]["metadata"]["valid_values"] == values
    assert item.metadata["valid_values"] == values


def test_unknown_query_response_cannot_leak_framework_or_raw_content(context):
    """服务不接受自由dict/框架Response代替RAGAnswer，也不返回其私有内容。"""
    context["answer"] = {"answer": "PRIVATE_RAW_RESPONSE"}
    response = request(context["app"], "/api/knowledge/query", payload=context["body"] | {"allow_cloud": True})
    assert response.status_code == 502 and "PRIVATE" not in response.text


def test_openapi_uses_only_project_schemas(context):
    """公开接口只有固定DKF请求/响应模型，不暴露TextNode/NodeWithScore或框架Response。"""
    specification = context["app"].openapi()
    assert {"/api/knowledge/search", "/api/knowledge/query"} <= set(specification["paths"])
    schemas = specification["components"]["schemas"]
    assert schemas["KnowledgeSearchRequest"]["properties"]["top_k"]["default"] == 30
    assert schemas["KnowledgeQueryRequest"]["properties"]["allow_cloud"]["default"] is False
    assert "RAGAnswer" in schemas and "KnowledgeSearchResponse" in schemas
    assert not ({"TextNode", "NodeWithScore", "QueryBundle", "Response"} & set(schemas))
