"""阶段35公式门面与HTTP内存用例；只编写，不执行云服务、模型、真实数据库或解析。"""

import asyncio
import copy
import io
import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import knowledge_specialty as api
from app.core import business_logging as logs
from app.schemas.formula_binding import FormulaBindingRequest
from app.services.document_analysis import analyze_observations
from app.schemas.knowledge_query import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.knowledge_specialty import FormulaExtractRequest
from app.services.formula import FormulaService
from app.services.knowledge_specialty import KnowledgeSpecialtyService
from app.services.rag.evidence import EvidenceError
from app.services.specialty_errors import SpecialtyError
from test_evidence import result_for
from test_formula import LLMStub, source
from test_document_analysis import block, snapshot


def request(app, path, payload):
    """只通过ASGI内存调用路由，不监听端口或访问网络。"""
    async def invoke():
        """每次独立客户端使用显式内存transport。"""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/knowledge/" + path, json=payload)
    return asyncio.run(invoke())


@pytest.fixture
def context():
    """复用现有来源Stub与真实公式服务，只替换LLM和搜索，不改变来源校验。"""
    evidence, session, chunk, document, blocks = source()
    llm, calls = LLMStub(), []

    def search(payload):
        """返回同一内存来源证据，不运行检索模型或数据库。"""
        calls.append(payload)
        return KnowledgeSearchResponse(status="success", evidence=[result_for(chunk, document)])

    service = KnowledgeSpecialtyService(search_service=SimpleNamespace(search=search), evidence_service=evidence,
        formula_service=FormulaService(evidence_service=evidence, llm_service=llm))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_specialty_service] = lambda: service
    return SimpleNamespace(app=app, service=service, llm=llm, evidence=evidence, session=session, calls=calls,
        chunk=chunk, document=document, blocks=blocks,
        selection=dict(kb_id=str(chunk.kb_id), doc_id=str(document.doc_id), chunk_id=str(chunk.chunk_id),
            block_id=str(blocks[0].block_id)))


def test_discovery_returns_verified_full_candidate_without_calling_llm(context):
    """发现只读原检索证据并复核，不将候选当成公式语义或自动收费提取。"""
    body = {"kb_id": str(context.chunk.kb_id), "question": "PRIVATE_QUESTION",
            "doc_ids": [str(context.document.doc_id)]}
    response = request(context.app, "formulas/discover", body)
    assert response.status_code == 200
    result = response.json()
    assert result["scope"] == "retrieved_chunks_only" and result["status"] == "candidates"
    [candidate] = result["candidates"]
    assert candidate["raw_formula"] == context.blocks[0].text and candidate["block_id"] == str(context.blocks[0].block_id)
    assert candidate["page_semantics"] == "physical_pdf" and candidate["source_parse_task_id"] == str(context.document.processing_task_id)
    assert len(context.calls) == 1 and not context.llm.calls and context.session.closed


def test_document_analysis_http_returns_original_identity_and_derived_report_without_models(context):
    """HTTP评估只委托一次现有报告服务，原范围/页语义/未知质量保持。"""
    doc = snapshot(file_type="docx")
    report = analyze_observations(doc, [block(1, "第一章 总论", kind="title")])
    calls = []

    def analyze(payload):
        """只返回内存派生报告并保留显式请求供核对。"""
        calls.append(payload)
        return report

    context.app.dependency_overrides[api.get_assessment_service] = lambda: SimpleNamespace(analyze=analyze)
    response = request(context.app, "documents/analyze", {"kb_id": str(doc.kb_id), "doc_id": str(doc.doc_id)})
    assert response.status_code == 200 and response.json() == report.model_dump(mode="json")
    assert len(calls) == 1 and calls[0].visual_pages == [] and not context.llm.calls
    assert response.json()["page_semantics"] == "logical" and response.json()["quality"]["status"] == "unknown"


def test_discovery_rejects_stale_original_citation_instead_of_silently_returning_empty(context):
    """当前来源失效仍保留EVIDENCE错误，不洗成文档无公式或证据不足。"""
    def stale(citation):
        """只模拟来源代次失败，不访问真实数据库。"""
        raise EvidenceError("EVIDENCE_STALE")

    context.service._evidence = SimpleNamespace(resolve_citation=stale)
    response = request(context.app, "formulas/discover", {"kb_id": str(context.chunk.kb_id), "question": "公式"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "EVIDENCE_STALE"
    assert not context.llm.calls


def test_partial_formula_span_is_explicitly_rejected_as_candidate_without_weakening_hash_checks(context):
    """完整来源核验后，公式的局部切片仍不可作为完整原式使用。"""
    trace = context.evidence.resolve(context.chunk.kb_id, context.chunk.chunk_id, doc_id=context.document.doc_id)
    trace.source_spans[0].end_char -= 1
    trace.source_spans[0].text = trace.source_spans[0].text[:-1]
    context.service._evidence = SimpleNamespace(resolve_citation=lambda citation: trace)
    result = context.service.discover_formulas(KnowledgeSearchRequest(kb_id=context.chunk.kb_id, question="公式"))
    assert result.status == "no_candidate_in_retrieved_chunks"
    assert result.rejected_formula_span_count == 1 and not result.candidates and not context.llm.calls


@pytest.mark.parametrize("allow", [None, False, "true", 1])
def test_extract_requires_actual_explicit_bool_before_any_source_or_llm_call(context, allow):
    """缺许可/false为403，字符串和数字为422，均不执行来源或收费调用。"""
    payload = context.selection.copy()
    if allow is not None:
        payload["allow_cloud"] = allow
    response = request(context.app, "formulas/extract", payload)
    assert response.status_code == (403 if allow is None or allow is False else 422)
    assert not context.session.statements and not context.llm.calls


def test_extraction_binding_and_calculation_share_definition_and_never_reextract(context):
    """一次结构化之后，缺参/补参/安全计算全部复用定义与Evidence，不追加LLM请求。"""
    pytest.importorskip("sympy", reason="缺少已声明SymPy；skip不是通过")
    original = copy.deepcopy(context.chunk.chunk_metadata)
    response = request(context.app, "formulas/extract", context.selection | {"allow_cloud": True})
    assert response.status_code == 200
    formula = response.json()
    assert formula["semantic_validation"] == "manual_pending" and len(context.llm.calls) == 1
    binding = {"formula": formula, "user_inputs": [], "evidence_inputs": []}
    missing = request(context.app, "formulas/bind", binding)
    assert missing.status_code == 200 and missing.json()["required_variables"] == ["profit", "revenue"]
    binding["user_inputs"] = [{"name": "profit", "value": "20", "input_id": "scenario-1"},
                              {"name": "revenue", "value": "100", "input_id": "scenario-1"}]
    calculation = request(context.app, "formulas/calculate", binding)
    assert calculation.status_code == 200 and calculation.json()["result"]["exact"] == "20"
    assert calculation.json()["formula"] == formula and calculation.json()["result_unit"] is None
    assert calculation.json()["calculation_scope"] == "arithmetic_only" and len(context.llm.calls) == 1
    assert context.chunk.chunk_metadata == original and context.document.status == "READY"


def test_missing_values_and_zero_division_are_distinct_and_do_not_retry(context):
    """缺参是正常状态，真实零分母是明确安全失败，不猜数值或再次请求LLM。"""
    pytest.importorskip("sympy", reason="缺少已声明SymPy；skip不是通过")
    formula = context.service.extract_formula(FormulaExtractRequest(**context.selection, allow_cloud=True))
    payload = FormulaBindingRequest(formula=formula)
    missing = context.service.calculate_formula(payload)
    assert missing.status == "need_variables" and missing.result is None
    payload = FormulaBindingRequest(formula=formula, user_inputs=[
        {"name": "profit", "value": "20", "input_id": "case"},
        {"name": "revenue", "value": "0", "input_id": "case"}])
    with pytest.raises(SpecialtyError) as caught:
        context.service.calculate_formula(payload)
    assert caught.value.code == "FORMULA_CALC_ZERO_DIVISION" and len(context.llm.calls) == 1


def test_cross_scope_search_is_rejected_before_source_lookup(context):
    """底层即使误返其他库证据，发现门面也不得将其发布或扩大查询范围。"""
    with pytest.raises(SpecialtyError) as caught:
        context.service.discover_formulas(KnowledgeSearchRequest(kb_id=uuid4(), question="公式"))
    assert caught.value.code == "SPECIALTY_RESULT" and not context.session.statements


def test_nonfinite_nested_metadata_is_rejected_before_source_lookup(context):
    """嵌套NaN不能在JSON转换时被洗成null并发布成合法候选。"""
    item = result_for(context.chunk, context.document)
    item.metadata["PRIVATE_VALUE"] = float("nan")
    context.service._search = SimpleNamespace(search=lambda payload: KnowledgeSearchResponse(
        status="success", evidence=[item]))
    with pytest.raises(SpecialtyError) as caught:
        context.service.discover_formulas(KnowledgeSearchRequest(kb_id=context.chunk.kb_id, question="公式"))
    assert caught.value.code == "SPECIALTY_RESULT" and not context.session.statements


@pytest.mark.parametrize("path,payload", [("documents/analyze", {"kb_id": str(UUID(int=0)), "doc_id": str(uuid4())}),
    ("documents/analyze", {"kb_id": str(uuid4()), "doc_id": str(uuid4()), "visual_pages": [True]}),
    ("formulas/extract", {"kb_id": "PRIVATE_SECRET"}), ("formulas/bind", {"formula": "PRIVATE_FORMULA"})])
def test_invalid_http_payload_has_safe_validation_details_without_echo(context, path, payload):
    """沿用安全路由屏蔽Pydantic原输入，错误与日志不能回显正文或密钥。"""
    response = request(context.app, path, payload)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "KNOWLEDGE_INPUT"
    assert "PRIVATE" not in response.text


def test_specialty_logs_never_include_formula_values_question_or_source_path(context):
    """复用每日业务日志，仅允许ID/操作/状态，不打印Prompt、参数、原式或源路径。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter("api"))
    logger = logs.LOGGER
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    try:
        context.service.discover_formulas(KnowledgeSearchRequest(kb_id=context.chunk.kb_id, question="PRIVATE_QUESTION"))
        context.service.extract_formula(FormulaExtractRequest(**context.selection, allow_cloud=True))
        output = stream.getvalue()
        assert "specialty.finished" in output and context.blocks[0].text not in output
        assert "PRIVATE" not in output and "利润率用于" not in output
    finally:
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        handler.close()
