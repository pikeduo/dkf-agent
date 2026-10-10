"""统一 Parser 契约入口；原生格式解析器由 registry 按文件类型选择。"""

from app.parsers.base import BaseParser

__all__ = ["BaseParser"]
