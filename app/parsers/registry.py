"""按数据库中的格式标识选择原生解析器，图片必须等待后续 OCR 接入。"""

from app.parsers.base import BaseParser
from app.parsers.common import OCRRequiredError, ParserError
from app.parsers.docx import DocxParser
from app.parsers.pdf import PdfParser
from app.parsers.text import MarkdownParser, TextParser


def get_parser(file_type: str) -> BaseParser:
    """只分派四种已实现格式，其他输入明确失败，不绕过 OCR 或伪造空结果。"""

    parsers = {
        "txt": TextParser,
        "md": MarkdownParser,
        "docx": DocxParser,
        "pdf": PdfParser,
    }
    if file_type in {"jpg", "jpeg", "png"}:
        raise OCRRequiredError("图片文件需要 OCR，当前尚未接入 OCR")
    parser = parsers.get(file_type)
    if parser is None:
        raise ParserError("当前原生解析仅支持 TXT、Markdown、DOCX 和文本型 PDF")
    return parser()
