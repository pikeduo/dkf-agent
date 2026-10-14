"""受保护的 LlamaIndex Response Synthesis，完整最终 Node 一次合成，不 refine/map-reduce。"""

import asyncio
import logging
from time import perf_counter

from llama_index.core.base.llms.types import ChatMessage, MessageRole
from llama_index.core.callbacks import CallbackManager
from llama_index.core.response_synthesizers.base import BaseSynthesizer
from llama_index.core.schema import NodeWithScore, QueryBundle

from app.core.business_logging import log_event
from app.schemas.rag import RAGAnswer
from app.services.deepseek import DeepSeekError
from app.services.rag.dense import validate_query
from app.services.rag.llama_index.deepseek_adapter import DKFDeepSeekAdapter
from app.services.rag.llama_index.node_adapter import node_to_result
from app.services.rag.llama_index.validation import RagAdapterError
from app.services.rag.synthesis import RAGError, RAG_SYSTEM_PROMPT, bind_answer, evidence_payload, insufficient_answer


def query_text(query: str | QueryBundle) -> str:
    """复用原问题校验，拒绝外部向量、变换 Query 或图像，不添加指令/改写问题。"""
    try:
        if isinstance(query, QueryBundle):
            if query.embedding is not None or query.custom_embedding_strs is not None or query.image_path is not None:
                raise ValueError
            query = query.query_str
        return validate_query(query)
    except Exception:
        raise RAGError("RAG_INPUT") from None


class DKFResponseSynthesizer(BaseSynthesizer):
    """Node-aware 合成边界；只输出项目 RAGAnswer，不返回框架 Response 对象。"""

    def __init__(self, llm: DKFDeepSeekAdapter | None = None):
        """显式初始化必要状态，避免 BaseSynthesizer 隐式默认 LLM/Tokenizer/PromptHelper。

        不调用默认构造与 synthesize/get_response 实现：它们会处理自由文本、裁剪及发送正文回调。
        本类只接受最终业务 Node，完整校验和一次 Chat；不提供框架文本切分/Refine 能力。
        """
        self._llm = llm if llm is not None else DKFDeepSeekAdapter()
        self._callback_manager = CallbackManager([])
        self._llm.callback_manager = self._callback_manager
        self._streaming, self._output_cls = False, RAGAnswer
        self._empty_response, self._multimodal = "", False
        self._prompt_helper, self._chat_prompt_helper = None, None

    def _get_prompts(self):
        """生产 Prompt 固定由项目维护，不暴露默认模板供动态覆盖。"""
        return {}

    def _update_prompts(self, prompts):
        """拒绝运行时更换 Prompt，以免绕过只使用最终 Evidence 的规则。"""
        if prompts:
            raise RAGError("RAG_UNSUPPORTED")

    def get_response(self, query_str: str, text_chunks: list[str], **kwargs):
        """自由文本没有稳定业务身份，禁止绕过最终 Node 合成边界。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def aget_response(self, query_str: str, text_chunks: list[str], **kwargs):
        """异步自由文本路径同样拒绝，不触发默认 Splitter 或多次 LLM 调用。"""
        raise RAGError("RAG_UNSUPPORTED")

    def synthesize(self, query: str | QueryBundle, nodes: list[NodeWithScore], *,
                   allow_cloud: bool = False, **kwargs) -> RAGAnswer:
        """只用最终至多五个已重排原Node；区分空集与模型拒答，不强制改写模型判断或追加请求。"""
        started = perf_counter()
        try:
            if allow_cloud is not True:
                raise RAGError("RAG_CLOUD_PERMISSION")
            question = query_text(query)
            if kwargs:
                raise RAGError("RAG_UNSUPPORTED")
            if not isinstance(nodes, list) or len(nodes) > 5:
                raise RAGError("RAG_EVIDENCE")
            evidence = []
            for node in nodes:
                item = node_to_result(node, log_success=False)
                if (item.rerank is None or item.fusion is None
                        or any(previous.kb_id != item.kb_id or previous.chunk_id == item.chunk_id
                               for previous in evidence)):
                    raise RAGError("RAG_EVIDENCE")
                evidence.append(item)
            if not evidence:
                log_event("rag.insufficient", stage="empty_final_evidence", chunk_count=0, item_count=0)
                return insufficient_answer()
            payload = evidence_payload(question, evidence, self._llm.context_chars)
            # 只记录最终证据数量；正文和模型原始拒答内容仍不得进入业务日志。
            log_event("rag.synthesis_started", kb_id=evidence[0].kb_id, chunk_count=len(evidence))
            response = self._llm.chat([
                ChatMessage(role=MessageRole.SYSTEM, content=RAG_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=payload)])
            result = bind_answer(response.message.content, evidence)
            log_event("rag.insufficient" if result.insufficient_evidence else "rag.synthesized",
                      kb_id=evidence[0].kb_id, item_count=len(result.citations),
                      stage="model_judged_insufficient" if result.insufficient_evidence else "supported_claims",
                      chunk_count=len(evidence),
                      duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            error = exc if isinstance(exc, (RAGError, DeepSeekError)) else RAGError(
                "RAG_EVIDENCE" if isinstance(exc, RagAdapterError) else "RAG_SYNTHESIS")
            log_event("rag.failed", level=logging.WARNING, stage="synthesis", error_code=error.code,
                      duration_ms=(perf_counter() - started) * 1000)
            raise error from None

    async def asynthesize(self, query, nodes, *, allow_cloud: bool = False, **kwargs) -> RAGAnswer:
        """显式异步只在线程中执行同一保护路径，不启用流式/额外模型或全局追踪。"""
        return await asyncio.to_thread(self.synthesize, query, nodes, allow_cloud=allow_cloud, **kwargs)
