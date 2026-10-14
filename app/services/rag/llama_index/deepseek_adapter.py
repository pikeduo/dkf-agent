"""旧导入路径的兼容入口；实际适配行为统一由提供方无关的 Adapter 管理。"""

from app.services.rag.llama_index.llm_adapter import DKFLLMAdapter


class DKFDeepSeekAdapter(DKFLLMAdapter):
    """兼容已有注入服务的调用者；未注入时同样尊重 LLM_PROVIDER，不强制官方。"""

    @classmethod
    def class_name(cls) -> str:
        """保留历史适配器类标识，不维护第二套请求实现。"""
        return "DKFDeepSeekAdapter"
