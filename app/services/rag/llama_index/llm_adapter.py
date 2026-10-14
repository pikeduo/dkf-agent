"""提供方无关的 LlamaIndex LLM Adapter，只有项目工厂可以选择正式 HTTP 服务。"""

import asyncio
from typing import Any

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, LLMMetadata, MessageRole
from llama_index.core.callbacks import CallbackManager
from llama_index.core.llms.custom import CustomLLM
from pydantic import PrivateAttr

from app.services.llm import ChatCompletionService, create_llm_service
from app.services.rag.synthesis import RAGError, RAG_SYSTEM_PROMPT


class DKFLLMAdapter(CustomLLM):
    """保持原系统/证据边界，不走含正文 callback、自由 Completion 或流式入口。"""

    _service: Any = PrivateAttr()

    def __init__(self, service: ChatCompletionService | None = None):
        """显式注入服务或由工厂读取选择器，构造不发请求、不设置全局 LLM。"""
        super().__init__(callback_manager=CallbackManager([]))
        self._service = service if service is not None else create_llm_service()

    @property
    def context_chars(self) -> int:
        """提供原有完整上下文预算，不自动重切片或裁剪。"""
        return self._service.context_chars

    @property
    def metadata(self) -> LLMMetadata:
        """框架只获得所选模型标识和输出预算，不获得凭据或端点。"""
        return LLMMetadata(model_name=self._service.model_name, num_output=self._service.max_tokens,
                           is_chat_model=True, is_function_calling_model=False)

    @classmethod
    def class_name(cls) -> str:
        """提供固定适配器名称，不序列化私有服务配置。"""
        return "DKFLLMAdapter"

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """仅委托审查后的系统 Prompt 与最终证据上下文，保持单次请求限制。"""
        if (kwargs or not isinstance(messages, list) or len(messages) != 2
                or not all(isinstance(message, ChatMessage) for message in messages)
                or messages[0].role != MessageRole.SYSTEM or messages[1].role != MessageRole.USER
                or messages[0].content != RAG_SYSTEM_PROMPT
                or any(message.additional_kwargs for message in messages)
                or any(len(message.blocks) != 1 or message.blocks[0].block_type != "text" for message in messages)):
            raise RAGError("RAG_UNSUPPORTED")
        content = self._service.complete(RAG_SYSTEM_PROMPT, messages[1].content)
        return ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=content))

    async def achat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """同进程线程包装单次调用，不使用携带正文的框架异步回调。"""
        return await asyncio.to_thread(self.chat, messages, **kwargs)

    def complete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝自由 Completion，避免旁路系统/证据边界。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def acomplete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝异步自由文本，不隐式请求提供方。"""
        raise RAGError("RAG_UNSUPPORTED")

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝未完整校验的流式回答。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def astream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝异步流式 Completion，不泄漏部分输出。"""
        raise RAGError("RAG_UNSUPPORTED")

    def stream_chat(self, messages, **kwargs):
        """拒绝流式 Chat，先完成严格 JSON 和引用校验才能发布回答。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def astream_chat(self, messages, **kwargs):
        """拒绝异步流式 Chat，不绕过最终结果边界。"""
        raise RAGError("RAG_UNSUPPORTED")
