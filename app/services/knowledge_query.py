"""HTTP/未来Agent的DKF服务边界；不暴露框架对象，不创建LLM客户端或模型。"""

import json
import logging
import math
from time import perf_counter

from pydantic import ValidationError

from app.core.business_logging import log_event
from app.schemas.knowledge_query import KnowledgeQueryRequest, KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.rag import RAGAnswer
from app.services.deepseek import ERRORS as DEEPSEEK_ERRORS
from app.services.llm import LLMError, ERRORS as LLM_ERRORS
from app.services.rag.query import query_knowledge_base
from app.services.rag.search import search_knowledge_base
from app.services.rag.synthesis import RAGError, ERRORS as RAG_ERRORS

LOCAL_ERRORS = {
    "KNOWLEDGE_INPUT": (422, "知识库、问题、文档范围或Top-K参数无效"),
    "KNOWLEDGE_RESULT": (502, "返回证据或回答不符合DKF契约，未发布部分结果"),
    "KNOWLEDGE_INTERNAL": (500, "知识库服务未完成，请查看安全日志"),
}
HTTP_CODES = {"RAG_INPUT": 422, "RAG_CLOUD_PERMISSION": 403, "RAG_CONTEXT_LIMIT": 413,
              "RAG_EVIDENCE": 409, "RAG_OUTPUT": 502, "RAG_SYNTHESIS": 502,
              "DEEPSEEK_RATE_LIMIT": 429, "DEEPSEEK_RESPONSE": 502, "DEEPSEEK_TRUNCATED": 502,
              "DEEPSEEK_HTTP": 502, "DEEPSEEK_NETWORK": 503,
              "LLM_RATE_LIMIT": 429, "LLM_RESPONSE": 502, "LLM_TRUNCATED": 502,
              "LLM_HTTP": 502, "LLM_NETWORK": 503}


class KnowledgeServiceError(Exception):
    """只接受集中白名单中的安全错误类别，不从任意异常.code或str生成HTTP详情。"""

    def __init__(self, code):
        """固定HTTP状态与中文说明；未注册类别归为内部错误。"""
        if not isinstance(code, str):
            code = "KNOWLEDGE_INTERNAL"
        if code in RAG_ERRORS:
            self.status_code, self.message = HTTP_CODES.get(code, 503), RAG_ERRORS[code]
        elif code in DEEPSEEK_ERRORS:
            self.status_code, self.message = HTTP_CODES.get(code, 503), DEEPSEEK_ERRORS[code]
        elif code in LLM_ERRORS:
            self.status_code, self.message = HTTP_CODES.get(code, 503), LLM_ERRORS[code]
        else:
            code = code if code in LOCAL_ERRORS else "KNOWLEDGE_INTERNAL"
            self.status_code, self.message = LOCAL_ERRORS[code]
        self.code = code
        super().__init__(self.message)


def _reject_nonfinite(value):
    """递归检查原Python字段，拒绝非有限浮点数，避免JSON转换将其掩盖为None。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("响应含非有限数值")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_nonfinite(item)


def _checked_response(value, schema):
    """转换前检查原字段，再复制/校验DKF响应；非法数值不能被静默改为未知值。"""
    try:
        if not isinstance(value, schema):
            raise ValueError
        # JSON模式可能把NaN/Infinity转换为null，必须先核对保留原数值的Python快照。
        _reject_nonfinite(value.model_dump(mode="python"))
        raw = json.dumps(value.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
        raw.encode("utf-8", errors="strict")
        return schema.model_validate(json.loads(raw))
    except Exception:
        raise KnowledgeServiceError("KNOWLEDGE_RESULT") from None


class KnowledgeService:
    """同步在线服务；构造不取配置/数据库/模型，共享模型仍由原项目服务管理。"""

    def search(self, payload: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        """复用只读Hybrid/Top5重排，搜索不授予云外传，也不使用LLM。"""
        return self._execute(payload, querying=False)

    def query(self, payload: KnowledgeQueryRequest) -> RAGAnswer:
        """默认拒绝真实云请求，有明确许可才委托已有单次问答流程。"""
        return self._execute(payload, querying=True)

    def _execute(self, payload, *, querying):
        """核对请求/返回范围并记录安全摘要；失败不伪装空集/不足，不自动重试收费。"""
        started, scope, stage = perf_counter(), {}, "input"
        operation = "query" if querying else "search"
        try:
            schema = KnowledgeQueryRequest if querying else KnowledgeSearchRequest
            if not isinstance(payload, schema):
                raise KnowledgeServiceError("KNOWLEDGE_INPUT")
            payload = schema.model_validate(payload.model_dump())
            scope = {"kb_id": payload.kb_id, "top_k": payload.top_k,
                     "source_doc_count": len(payload.doc_ids) if payload.doc_ids is not None else 0}
            if payload.doc_ids is not None and len(payload.doc_ids) == 1:
                scope["doc_id"] = payload.doc_ids[0]
            log_event("knowledge.started", **scope, operation=operation)
            stage = "service"
            if querying:
                if payload.allow_cloud is not True:
                    raise RAGError("RAG_CLOUD_PERMISSION")
                value = query_knowledge_base(payload.kb_id, payload.question, doc_ids=payload.doc_ids,
                                             top_k=payload.top_k, allow_cloud=True)
                result = _checked_response(value, RAGAnswer)
                items = result.citations
            else:
                items = search_knowledge_base(payload.kb_id, payload.question,
                                              doc_ids=payload.doc_ids, top_k=payload.top_k)
                if not isinstance(items, list) or len(items) > min(5, payload.top_k):
                    raise KnowledgeServiceError("KNOWLEDGE_RESULT")
                result = _checked_response(KnowledgeSearchResponse(
                    status="success" if items else "empty", evidence=items), KnowledgeSearchResponse)
                items = result.evidence
            if len(items) > min(5, payload.top_k) or any(item.kb_id != payload.kb_id or item.chunk_id.int == 0 or item.doc_id.int == 0
                   or payload.doc_ids is not None and item.doc_id not in payload.doc_ids for item in items):
                raise KnowledgeServiceError("KNOWLEDGE_RESULT")
            log_event("knowledge.finished", **scope, operation=operation, status=result.status,
                      item_count=len(items), duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            if isinstance(exc, KnowledgeServiceError):
                error = exc
            elif isinstance(exc, (RAGError, LLMError)):
                error = KnowledgeServiceError(exc.code)
            elif isinstance(exc, ValidationError):
                error = KnowledgeServiceError("KNOWLEDGE_INPUT" if stage == "input" else "KNOWLEDGE_RESULT")
            else:
                error = KnowledgeServiceError("KNOWLEDGE_INTERNAL")
            log_event("knowledge.failed", level=logging.WARNING, **scope, operation=operation,
                      error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None
