"""阶段35同步HTTP入口，只暴露项目契约，不启动Worker或隐式付费处理。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.knowledge import SafeKnowledgeRoute
from app.schemas.document_analysis import DocumentAnalysisReport, DocumentAnalysisRequest
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_binding import FormulaBindingRequest, FormulaBindingResult
from app.schemas.formula_calculation import FormulaCalculationResult
from app.schemas.knowledge_query import KnowledgeErrorResponse, KnowledgeSearchRequest
from app.schemas.knowledge_specialty import FormulaDiscoveryResult, FormulaExtractRequest
from app.services.document_assessment import DocumentAssessmentService
from app.services.knowledge_specialty import KnowledgeSpecialtyService
from app.services.specialty_errors import SpecialtyError

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-specialty"], route_class=SafeKnowledgeRoute,
    responses={code: {"model": KnowledgeErrorResponse} for code in (403, 404, 409, 413, 422, 429, 500, 502, 503)})


def get_assessment_service():
    """轻量构造只读评估服务，访问Swagger时不连接数据库或读取原件。"""
    return DocumentAssessmentService()


def get_specialty_service():
    """轻量构造公式门面，访问Swagger时不加载模型、查询来源或请求LLM。"""
    return KnowledgeSpecialtyService()


AssessmentDependency = Annotated[DocumentAssessmentService, Depends(get_assessment_service)]
SpecialtyDependency = Annotated[KnowledgeSpecialtyService, Depends(get_specialty_service)]


@router.post("/documents/analyze", response_model=DocumentAnalysisReport, summary="只读评估目录、复杂度与质量")
def analyze_document(payload: DocumentAnalysisRequest, service: AssessmentDependency):
    """默认现有Block观测；明确visual_pages才检查原PDF/图片，不修改原始解析或索引。"""
    try:
        return service.analyze(payload)
    except SpecialtyError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None


@router.post("/formulas/discover", response_model=FormulaDiscoveryResult, summary="从检索证据中发现完整公式候选")
def discover_formulas(payload: KnowledgeSearchRequest, service: SpecialtyDependency):
    """沿用原Hybrid/重排与Evidence，返回候选来源，不请求LLM或自动选中公式。"""
    try:
        return service.discover_formulas(payload)
    except SpecialtyError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None


@router.post("/formulas/extract", response_model=FormulaSemanticResult, summary="显式结构化指定公式及变量含义")
def extract_formula(payload: FormulaExtractRequest, service: SpecialtyDependency):
    """准确选择公式并逐次明确云许可；可能收费，失败不自动重放。"""
    try:
        return service.extract_formula(payload)
    except SpecialtyError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None


@router.post("/formulas/bind", response_model=FormulaBindingResult, summary="绑定已有公式的显式参数")
def bind_formula(payload: FormulaBindingRequest, service: SpecialtyDependency):
    """只读核对来源，缺参返回need_variables，不重复收费提取或推断数值。"""
    try:
        return service.bind_formula(payload)
    except SpecialtyError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None


@router.post("/formulas/calculate", response_model=FormulaCalculationResult, summary="使用已有公式和参数安全计算")
def calculate_formula(payload: FormulaBindingRequest, service: SpecialtyDependency):
    """复用原SymPy四则白名单和来源核对，结果不冒充业务语义或单位验真。"""
    try:
        return service.calculate_formula(payload)
    except SpecialtyError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None
