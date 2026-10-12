"""适配边界的安全错误和只读快照校验，不依赖 LlamaIndex 或模型推理框架。"""

import json
import math
from numbers import Real

from app.services.embedding.service import DIMENSION

ERRORS = {
    "RAG_ADAPTER_INPUT": "适配输入或证据字段无效",
    "RAG_ADAPTER_IDENTITY": "Chunk、Document 或 Node 标识不一致",
    "RAG_ADAPTER_UNLOADED": "ORM 必需字段未加载，请由调用方显式查询完整记录",
    "RAG_ADAPTER_VECTOR": "已有向量不是有效的 1024 维 L2 单位向量",
    "RAG_ADAPTER_NODE": "仅支持携带项目证据元数据的 TextNode 或 NodeWithScore",
    "RAG_DOCUMENT_EMBEDDING_FORBIDDEN": "框架不得重新生成文档向量，请使用项目 /embed 和 /index 流程",
    "RAG_ADAPTER_EMBEDDING": "项目 Query 向量化未完成，请检查本地模型部署",
}


class RagAdapterError(Exception):
    """只携带固定错误码与中文原因，禁止透传正文、向量、路径及验证错误原文。"""

    def __init__(self, code: str):
        """未知类别使用固定输入错误，不把调用方任意字符串转换为异常详情。"""

        self.code = code if code in ERRORS else "RAG_ADAPTER_INPUT"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def json_snapshot(value: dict) -> dict:
    """深复制 JSON 证据，不共享 ORM 可变 metadata；拒绝非 JSON 值或非有限数值。"""

    try:
        if not isinstance(value, dict):
            raise ValueError
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise RagAdapterError("RAG_ADAPTER_INPUT") from None


def vector_snapshot(value) -> list[float]:
    """复制已存储的 Dense 向量，兼容 pgvector 的 NumPy 返回值；不推理或重新归一化。"""

    try:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if (not isinstance(value, list) or len(value) != DIMENSION
                or any(isinstance(item, bool) or not isinstance(item, Real) or not math.isfinite(item)
                       for item in value)):
            raise ValueError
        result = [float(item) for item in value]
        norm = math.sqrt(math.fsum(item * item for item in result))
        if not math.isfinite(norm) or abs(norm - 1.0) > 1e-4:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise RagAdapterError("RAG_ADAPTER_VECTOR") from None
