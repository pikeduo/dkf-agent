"""按固定 Native 能力选择解析器；图片和旧 Office 明确要求显式 MinerU，不实现 Router。"""

from app.parsers.base import BaseParser
from app.parsers.common import OCRRequiredError, ParserError
from app.parsers.csv import CsvParser
from app.parsers.docx import DocxParser
from app.parsers.pdf import PdfParser
from app.parsers.text import MarkdownParser, TextParser
from app.parsers.xlsx import XlsxParser


def get_parser(file_type: str) -> BaseParser:
    """只分派六种 Native 格式；旧 Office、演示文稿及图片需显式 MinerU，不自动路由。"""

    parsers = {
        "txt": TextParser,
        "md": MarkdownParser,
        "docx": DocxParser,
        "pdf": PdfParser,
        "csv": CsvParser,
        "xlsx": XlsxParser,
    }
    if file_type in {"jpg", "jpeg", "png"}:
        raise OCRRequiredError("图片文件需要 OCR，请使用显式 MinerU 云端解析入口")
    if file_type in {"doc", "xls", "ppt", "pptx"}:
        raise OCRRequiredError(
            "旧 Office 或演示文稿不支持 Native，请使用显式 MinerU 云端解析入口"
        )
    parser = parsers.get(file_type)
    if parser is None:
        raise ParserError(
            "当前原生解析仅支持 TXT、Markdown、CSV、DOCX、XLSX 和文本型 PDF"
        )
    return parser()
