"""约束所有格式解析器的返回结构，不承担存储、任务调度或检索。"""

from abc import ABC, abstractmethod
from pathlib import Path
from uuid import UUID

from app.schemas.parsed_document import FileType, ParsedDocument


class BaseParser(ABC):
    """子类只实现解析钩子，调用方统一通过 parse 获取经过校验的结果。"""

    def parse(
        self,
        *,
        doc_id: UUID,
        file_path: Path,
        file_name: str,
        file_type: FileType,
    ) -> ParsedDocument:
        """解析原件并校验结构和归属；解析或校验异常交由调用方处理。"""

        result = self._parse(
            doc_id=doc_id,
            file_path=file_path,
            file_name=file_name,
            file_type=file_type,
        )
        # 即便子类返回模型实例，也重新校验，防止构造后修改绕过边界检查。
        parsed = ParsedDocument.model_validate(result)
        if (parsed.doc_id, parsed.file_name, parsed.file_type) != (
            doc_id,
            file_name,
            file_type,
        ):
            raise ValueError("Parser 输出的文档标识、文件名或类型与输入不一致")
        return parsed

    @abstractmethod
    def _parse(
        self,
        *,
        doc_id: UUID,
        file_path: Path,
        file_name: str,
        file_type: FileType,
    ) -> ParsedDocument:
        """由具体 Parser 读取文件并返回统一模型，不写数据库或调用 RAG。"""

        raise NotImplementedError
