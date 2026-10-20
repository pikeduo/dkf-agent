"""专项薄门面：公式发现复用检索/来源服务，提取/绑定/计算复用阶段27～29。"""

import json
import logging
import math
from time import perf_counter

from app.core.business_logging import log_event
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_binding import FormulaBindingRequest, FormulaBindingResult
from app.schemas.formula_calculation import FormulaCalculationResult
from app.schemas.knowledge_query import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.knowledge_specialty import FormulaCandidate, FormulaDiscoveryResult, FormulaExtractRequest
from app.services.formula import FormulaError, FormulaService, select_formula_source
from app.services.formula_binding import FormulaBindingError, FormulaBindingService
from app.services.formula_calculation import FormulaCalculationError, FormulaCalculationService
from app.services.knowledge_query import KnowledgeService, KnowledgeServiceError
from app.services.llm import LLMError
from app.services.rag.evidence import EvidenceError, EvidenceService, make_citation
from app.services.specialty_errors import SpecialtyError


def _finite(value):
    """转换JSON前递归拒绝非有限数，避免Pydantic将非法浮点数隐式变成null。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise SpecialtyError("SPECIALTY_RESULT")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _finite(item)


def checked_output(value, schema):
    """复制并重验项目响应，既不返回框架对象，也不发布非法/半份结果。"""
    if not isinstance(value, schema):
        raise SpecialtyError("SPECIALTY_RESULT")
    _finite(value.model_dump(mode="python"))
    try:
        serialized = json.dumps(value.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
        serialized.encode("utf-8", errors="strict")
        return schema.model_validate_json(serialized)
    except Exception:
        raise SpecialtyError("SPECIALTY_RESULT") from None


class KnowledgeSpecialtyService:
    """构造无IO；不增加Agent、检索器、求值器、模型或业务持久化。"""

    def __init__(self, *, search_service=None, evidence_service=None, formula_service=None,
                 binding_service=None, calculation_service=None):
        """保留可注入的既有服务，允许用户用内存桩验证门面行为而不连接外部资源。"""
        self._search = search_service if search_service is not None else KnowledgeService()
        self._evidence = evidence_service if evidence_service is not None else EvidenceService()
        self._formula = formula_service if formula_service is not None else FormulaService(evidence_service=self._evidence)
        self._binding = binding_service if binding_service is not None else FormulaBindingService(evidence_service=self._evidence)
        self._calculation = calculation_service if calculation_service is not None else FormulaCalculationService(binding_service=self._binding)

    def _invoke(self, operation, payload, action, schema):
        """只记录固定操作和安全终态，保留已知原始错误码，无自动重试或收费重放。"""
        started = perf_counter()
        scope = {"kb_id": payload.kb_id} if hasattr(payload, "kb_id") else {
            "kb_id": payload.formula.source.kb_id, "doc_id": payload.formula.source.doc_id}
        if hasattr(payload, "doc_id"):
            scope["doc_id"] = payload.doc_id
        try:
            log_event("specialty.started", **scope, operation=operation)
            result = checked_output(action(), schema)
            log_event("specialty.finished", **scope, operation=operation, status=result.status,
                duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            known = (SpecialtyError, FormulaError, FormulaBindingError, FormulaCalculationError,
                     EvidenceError, KnowledgeServiceError, LLMError)
            error = SpecialtyError(exc.code) if isinstance(exc, known) else SpecialtyError("SPECIALTY_INTERNAL")
            log_event("specialty.failed", level=logging.WARNING, **scope, operation=operation,
                error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    def discover_formulas(self, payload):
        """只在本次Top5证据内筛选完整MinerU公式并复核来源，不提取含义或调用LLM。"""
        if not isinstance(payload, KnowledgeSearchRequest):
            raise SpecialtyError("ANALYSIS_INPUT")
        payload = KnowledgeSearchRequest.model_validate(payload.model_dump())

        def discover():
            """候选按检索原序去重；片段不完整正常计数，来源失效则整批失败。"""
            result = checked_output(self._search.search(payload), KnowledgeSearchResponse)
            candidates, seen, rejected, total = [], set(), 0, 0
            generations = {}
            for rank, item in enumerate(result.evidence, 1):
                if item.kb_id != payload.kb_id or payload.doc_ids is not None and item.doc_id not in payload.doc_ids:
                    raise SpecialtyError("SPECIALTY_RESULT")
                trace = self._evidence.resolve_citation(make_citation(item, rank))
                if (trace.kb_id, trace.doc_id, trace.chunk_id) != (item.kb_id, item.doc_id, item.chunk_id):
                    raise SpecialtyError("SPECIALTY_RESULT")
                generation = trace.file_hash, trace.source_parse_task_id, trace.source_chunk_task_id
                if trace.doc_id in generations and generations[trace.doc_id] != generation:
                    raise SpecialtyError("EVIDENCE_STALE")
                generations[trace.doc_id] = generation
                for span in trace.source_spans:
                    if span.block_type != "formula" or span.block_id in seen:
                        continue
                    try:
                        target = select_formula_source(trace, span.block_id)
                    except FormulaError as error:
                        if error.code != "FORMULA_SOURCE":
                            raise
                        rejected += 1
                        continue
                    seen.add(span.block_id)
                    total += 1
                    if len(candidates) < 100:
                        candidates.append(FormulaCandidate(kb_id=trace.kb_id, doc_id=trace.doc_id,
                            chunk_id=trace.chunk_id, block_id=target.block_id, file_name=trace.file_name,
                            file_hash=trace.file_hash, source_parse_task_id=trace.source_parse_task_id,
                            source_chunk_task_id=trace.source_chunk_task_id, page=target.page,
                            page_semantics=trace.page_semantics, section=target.section,
                            raw_formula=target.text, block_text_hash=target.block_text_hash,
                            retrieval_rank=rank, document_status=trace.document_status))
            return FormulaDiscoveryResult(status="candidates" if candidates else "no_candidate_in_retrieved_chunks",
                candidates=candidates, retrieved_chunk_count=len(result.evidence),
                rejected_formula_span_count=rejected, omitted_candidate_count=total - len(candidates))

        return self._invoke("formula_discovery", payload, discover, FormulaDiscoveryResult)

    def extract_formula(self, payload):
        """每次准确选择与云许可先检查，服务侧再次核对当前完整原式和上下文。"""
        if not isinstance(payload, FormulaExtractRequest):
            raise SpecialtyError("FORMULA_INPUT")
        payload = FormulaExtractRequest.model_validate(payload.model_dump())
        if not payload.allow_cloud:
            raise SpecialtyError("FORMULA_CLOUD_PERMISSION")

        def extract():
            """只执行一次既有提取，防止服务异常返回跨范围公式。"""
            result = self._formula.extract_formula(payload.kb_id, payload.chunk_id, payload.block_id,
                doc_id=payload.doc_id, allow_cloud=True)
            if (not isinstance(result, FormulaSemanticResult) or result.formula_block_id != payload.block_id
                    or (result.source.kb_id, result.source.doc_id, result.source.chunk_id)
                    != (payload.kb_id, payload.doc_id, payload.chunk_id)):
                raise SpecialtyError("SPECIALTY_RESULT")
            return result

        return self._invoke("formula_extract", payload, extract, FormulaSemanticResult)

    def bind_formula(self, payload):
        """复用已有定义绑定，缺参正常返回，不重提取、付费或计算。"""
        if not isinstance(payload, FormulaBindingRequest):
            raise SpecialtyError("FORMULA_BINDING_INPUT")

        def bind():
            """保留完整原公式和绑定来源，不允许底层更换公式定义。"""
            result = self._binding.bind_formula_variables(payload.formula,
                user_inputs=payload.user_inputs, evidence_inputs=payload.evidence_inputs)
            if not isinstance(result, FormulaBindingResult) or result.formula != payload.formula:
                raise SpecialtyError("SPECIALTY_RESULT")
            return result

        return self._invoke("formula_bind", payload, bind, FormulaBindingResult)

    def calculate_formula(self, payload):
        """消费原绑定请求，每次复核来源后机械计算，缺参不猜值、不调用LLM。"""
        if not isinstance(payload, FormulaBindingRequest):
            raise SpecialtyError("FORMULA_CALC_INPUT")

        def calculate():
            """原定义和参数交给既有安全边界，返回范围仍必须与请求一致。"""
            result = self._calculation.calculate_formula(payload)
            if not isinstance(result, FormulaCalculationResult) or result.formula != payload.formula:
                raise SpecialtyError("SPECIALTY_RESULT")
            return result

        return self._invoke("formula_calculate", payload, calculate, FormulaCalculationResult)
