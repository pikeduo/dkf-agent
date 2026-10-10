"""将 DOCX 正文段落及表格按阅读顺序转换为统一解析块。"""

import re
from pathlib import Path
from uuid import UUID
from zipfile import BadZipFile

from docx import Document as load_document
from docx.opc.exceptions import OpcError
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from app.parsers.base import BaseParser
from app.parsers.common import NoTextError, ParserError, make_block
from app.schemas.parsed_document import FileType, ParsedBlock, ParsedDocument


def _outline_level(element) -> int | None:
    """读取段落或样式的显式大纲级别，正文级别 9 返回空值。"""

    properties = element.find(qn("w:pPr"))
    if properties is None:
        return None
    outline = properties.find(qn("w:outlineLvl"))
    if outline is None:
        return None
    value = int(outline.get(qn("w:val"), "9"))
    if not 0 <= value <= 9:
        raise ValueError("DOCX 大纲级别超出范围")
    return value + 1 if value < 9 else None


def _heading_level(paragraph: Paragraph) -> int | None:
    """识别直接大纲级别、标准中英文标题及继承样式，不按字体大小猜测标题。"""

    # 显式设置为正文级别时不能再由继承的 Heading 样式误判为标题。
    direct_properties = paragraph._p.find(qn("w:pPr"))
    if direct_properties is not None:
        if direct_properties.find(qn("w:outlineLvl")) is not None:
            return _outline_level(paragraph._p)

    style = paragraph.style
    visited: set[str] = set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        name = style.name.strip().casefold()
        if name in {"title", "标题"}:
            return 0
        heading_match = re.fullmatch(r"(?:heading|标题)\s*([1-9])", name)
        if heading_match:
            return int(heading_match.group(1))
        properties = style.element.find(qn("w:pPr"))
        if properties is not None:
            if properties.find(qn("w:outlineLvl")) is not None:
                return _outline_level(style.element)
        style = style.base_style
    return None


def _cell_text(cell: _Cell) -> str:
    """保留单元格段落原文空白及嵌套表格次序，不读取图片或文本框。"""

    parts = []
    for content in cell.iter_inner_content():
        value = content.text if isinstance(content, Paragraph) else _table_text(content)
        if value.strip():
            parts.append(value)
    return "\n".join(parts)


def _table_text(table: Table) -> str:
    """以制表符分列、换行分行提取表格，避免水平合并单元格重复输出正文。"""

    lines = []
    for row in table.rows:
        seen_cells = set()
        values = []
        for cell in row.cells:
            if cell._tc in seen_cells:
                values.append("")
            else:
                seen_cells.add(cell._tc)
                values.append(_cell_text(cell))
        if any(values):
            lines.append("\t".join(values))
    return "\n".join(lines)


class DocxParser(BaseParser):
    """只解析 DOCX 原生正文；不推测排版页码、执行 OCR 或写入数据库。"""

    def _parse(
        self,
        *,
        doc_id: UUID,
        file_path: Path,
        file_name: str,
        file_type: FileType,
    ) -> ParsedDocument:
        """保留正文原文并产生稳定标识的块，格式损坏抛安全错误、I/O 错误向上传递。"""

        if file_type != "docx":
            raise ParserError("DOCX Parser 只支持 docx 文件")

        blocks: list[ParsedBlock] = []
        headings: dict[int, str] = {}
        title = ""
        try:
            # 自行打开流，避免依赖库把文件不存在或无权限错误包装成坏包错误。
            with file_path.open("rb") as source:
                document = load_document(source)
                if document.element.body is None:
                    raise ParserError("DOCX 正文结构不完整")
                for content in document.iter_inner_content():
                    if isinstance(content, Paragraph):
                        text = content.text
                        if not text.strip():
                            continue
                        level = _heading_level(content)
                        if level is not None:
                            headings = {
                                key: value
                                for key, value in headings.items()
                                if key < level
                            }
                            heading = text.strip()
                            headings[level] = heading
                            if not title:
                                title = heading
                        block_type = "title" if level is not None else "text"
                    else:
                        text = _table_text(content)
                        if not text:
                            continue
                        block_type = "table"
                    blocks.append(
                        make_block(
                            doc_id,
                            len(blocks) + 1,
                            page=1,
                            block_type=block_type,
                            text=text,
                            section=" / ".join(
                                headings[key] for key in sorted(headings)
                            ),
                        )
                    )
        except (
            BadZipFile,
            OpcError,
            XMLSyntaxError,
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            IndexError,
            RuntimeError,
            NotImplementedError,
        ):
            # 不把解析器底层异常（可能含本机路径或文档内容）传给 API/任务结果。
            raise ParserError("DOCX 文件损坏、格式无效或内容不受支持") from None
        if not blocks:
            raise NoTextError("DOCX 未提取到原生正文文字，可能为空文档或图片文档")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            title=title,
            blocks=blocks,
        )
