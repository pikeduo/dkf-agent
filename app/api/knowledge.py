"""Knowledge搜索/问答HTTP入口，只使用项目Schema和Service，不导入框架类型。"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.routing import APIRoute

from app.core.business_logging import log_event
from app.schemas.knowledge_query import KnowledgeErrorResponse, KnowledgeQueryRequest, KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.rag import RAGAnswer
from app.services.knowledge_query import KnowledgeService, KnowledgeServiceError


class SafeKnowledgeRoute(APIRoute):
    """仅本领域路由清理验证错误，避免FastAPI默认详情回显问题/正文或坏响应。"""

    def get_route_handler(self):
        """不读取/记录原始请求体，将输入/输出错误转换为固定安全HTTP详情。"""
        handler = super().get_route_handler()

        async def safe_handler(request):
            """保留正常响应，验证失败不输出input/ctx/异常原文或框架对象。"""
            try:
                return await handler(request)
            except RequestValidationError:
                error = KnowledgeServiceError("KNOWLEDGE_INPUT")
                log_event("knowledge.rejected", level=logging.WARNING, status_code=error.status_code, error_code=error.code)
                raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None
            except ResponseValidationError:
                error = KnowledgeServiceError("KNOWLEDGE_RESULT")
                log_event("knowledge.rejected", level=logging.WARNING, status_code=error.status_code, error_code=error.code)
                raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None
            except HTTPException:
                raise
            except Exception:
                error = KnowledgeServiceError("KNOWLEDGE_INTERNAL")
                log_event("knowledge.rejected", level=logging.ERROR, status_code=error.status_code, error_code=error.code)
                raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None

        return safe_handler


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], route_class=SafeKnowledgeRoute,
                   responses={code: {"model": KnowledgeErrorResponse} for code in (403, 409, 413, 422, 429, 500, 502, 503)})


def get_knowledge_service() -> KnowledgeService:
    """返回轻量服务门面供依赖注入；构造不连接数据库或加载任何模型。"""
    return KnowledgeService()


KnowledgeDependency = Annotated[KnowledgeService, Depends(get_knowledge_service)]


@router.post("/search", response_model=KnowledgeSearchResponse, summary="检索并重排知识库证据")
def search(payload: KnowledgeSearchRequest, service: KnowledgeDependency) -> KnowledgeSearchResponse:
    """在线同步服务在线程池执行，本地Hybrid→Top5，不返回Node或调用LLM。"""
    try:
        return service.search(payload)
    except KnowledgeServiceError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None


@router.post("/query", response_model=RAGAnswer, summary="根据最终证据生成带引用的回答")
def query(payload: KnowledgeQueryRequest, service: KnowledgeDependency) -> RAGAnswer:
    """每次请求显式确认外传/费用后合成，异常与真正证据不足严格区分。"""
    try:
        return service.query(payload)
    except KnowledgeServiceError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from None
