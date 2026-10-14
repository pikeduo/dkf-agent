"""LlamaIndex LLM 只包装项目 DeepSeekService，不引入第二个客户端或默认 LLM。"""

import asyncio
from typing import Any

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, LLMMetadata, MessageRole
from llama_index.core.callbacks import CallbackManager
from llama_index.core.llms.custom import CustomLLM
from pydantic import PrivateAttr

from app.services.deepseek import DeepSeekService
from app.services.rag.synthesis import RAGError, RAG_SYSTEM_PROMPT


class DKFDeepSeekAdapter(CustomLLM):
    """显式 LLM Adapter；不走携带正文的框架默认 callback、Completion 或流式路径。"""

    _service: Any = PrivateAttr()

    def __init__(self, service: DeepSeekService | None = None):
        """仅冻结服务及空回调管理器，不请求网络、不设置全局 Settings。"""
        super().__init__(callback_manager=CallbackManager([]))
        self._service = service if service is not None else DeepSeekService()

    @property
    def context_chars(self) -> int:
        """供项目合成层检查完整上下文，禁止自动重切片或裁剪。"""
        return self._service.context_chars

    @property
    def metadata(self) -> LLMMetadata:
        """提供框架描述信息；不用于 token 自动裁剪或供应商精确计量。"""
        return LLMMetadata(model_name=self._service.model_name, num_output=self._service.max_tokens,
                           is_chat_model=True, is_function_calling_model=False)

    @classmethod
    def class_name(cls) -> str:
        """固定类名，不序列化私有客户端、密钥或问题。"""
        return "DKFDeepSeekAdapter"

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """仅接受审查后的系统 Prompt 与单个 JSON 用户上下文，委托唯一项目 HTTP 服务。"""
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
        """同进程线程包装项目单次同步请求，不用框架含正文的异步回调。"""
        return await asyncio.to_thread(self.chat, messages, **kwargs)

    def complete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝自由 Completion，避免丢失系统/证据边界或旁路调用默认 Prompt。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def acomplete(self, prompt: str, formatted: bool = False, **kwargs):
        """异步自由文本同样拒绝，不隐式请求供应商。"""
        raise RAGError("RAG_UNSUPPORTED")

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        """引用必须全量验证后发布，第一版不提供部分流式回答。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def astream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        """拒绝异步流式 Completion，不泄漏尚未校验的生成内容。"""
        raise RAGError("RAG_UNSUPPORTED")

    def stream_chat(self, messages, **kwargs):
        """拒绝流式 Chat，必须先完成 Schema 与最终证据引用校验。"""
        raise RAGError("RAG_UNSUPPORTED")

    async def astream_chat(self, messages, **kwargs):
        """拒绝异步流式 Chat，不绕过项目最终结果边界。"""
        raise RAGError("RAG_UNSUPPORTED")
