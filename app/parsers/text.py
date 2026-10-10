"""TXT 和 Markdown 原生解析：严格解码、逻辑页和原文段落，不调用外部服务。"""

import codecs
import re
from pathlib import Path
from uuid import UUID

from app.parsers.base import BaseParser
from app.parsers.common import NoTextError, ParserError, make_block
from app.schemas.parsed_document import FileType, ParsedBlock, ParsedDocument

ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def read_text(file_path: Path) -> str:
    """严格读取 UTF-8、带 BOM 的 UTF-16/32 或 GB18030，不用替换字符掩盖乱码。"""

    content = file_path.read_bytes()
    # UTF-32 BOM 包含 UTF-16 前缀，必须先判断最长 BOM，避免误识别。
    if content.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encodings = ["utf-32"]
    elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ["utf-16"]
    elif content.startswith(codecs.BOM_UTF8):
        encodings = ["utf-8-sig"]
    else:
        encodings = ["utf-8", "gb18030"]
    for encoding in encodings:
        try:
            value = content.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if any(ord(char) < 32 and char not in "\t\n\r\f" for char in value):
            raise ParserError("文本包含不支持的控制字符，请检查是否误上传二进制文件")
        return value
    raise ParserError("文本编码无法识别，请转换为 UTF-8 后重新上传")


def text_paragraphs(text: str) -> list[str]:
    """按空行分段，保留段内换行和缩进，仅去除段落末尾的分隔换行。"""

    paragraphs = []
    lines = []
    for line in text.splitlines(keepends=True):
        if line.strip():
            lines.append(line)
        elif lines:
            paragraphs.append("".join(lines).rstrip("\r\n"))
            lines = []
    if lines:
        paragraphs.append("".join(lines).rstrip("\r\n"))
    return paragraphs


class TextParser(BaseParser):
    """TXT 按原文段落产生 text 块，没有固定排版时使用逻辑页 1。"""

    def _parse(
        self, *, doc_id: UUID, file_path: Path, file_name: str, file_type: FileType
    ) -> ParsedDocument:
        """返回有序 TXT 段落；空白文件立即失败，不伪造标题或物理页码。"""

        if file_type != "txt":
            raise ParserError("TXT 解析器仅接受 txt 文件")
        paragraphs = text_paragraphs(read_text(file_path))
        if not paragraphs:
            raise NoTextError("文档没有可用原生文本")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            blocks=[
                make_block(doc_id, index, page=1, block_type="text", text=paragraph)
                for index, paragraph in enumerate(paragraphs, start=1)
            ],
        )


class MarkdownParser(BaseParser):
    """识别常用标题和章节，代码围栏及未处理的 Markdown 语法保留原文。"""

    def _parse(
        self, *, doc_id: UUID, file_path: Path, file_name: str, file_type: FileType
    ) -> ParsedDocument:
        """按阅读顺序输出标题与段落，不把代码围栏中的井号识别为标题。"""

        if file_type != "md":
            raise ParserError("Markdown 解析器仅接受 md 文件")
        lines = read_text(file_path).splitlines(keepends=True)
        blocks: list[ParsedBlock] = []
        paragraph: list[str] = []
        headings: list[str] = []
        title = ""
        fence = ""
        position = 0

        def flush_paragraph() -> None:
            """把当前段落写为正文块，保留原文格式和所属章节，不产生空块。"""

            content = "".join(paragraph).rstrip("\r\n")
            if content.strip():
                blocks.append(
                    make_block(
                        doc_id,
                        len(blocks) + 1,
                        page=1,
                        block_type="text",
                        text=content,
                        section=" / ".join(part for part in headings if part),
                    )
                )
            paragraph.clear()

        while position < len(lines):
            line = lines[position]
            plain = line.rstrip("\r\n")
            marker = FENCE.match(plain)
            if fence:
                paragraph.append(line)
                if (
                    marker
                    and marker[1][0] == fence[0]
                    and len(marker[1]) >= len(fence)
                    and not marker[2].strip()
                ):
                    fence = ""
                position += 1
                continue
            if marker:
                flush_paragraph()
                fence = marker[1]
                paragraph.append(line)
                position += 1
                continue
            heading = ATX_HEADING.match(plain)
            underline = (
                SETEXT_HEADING.match(lines[position + 1].rstrip("\r\n"))
                if position + 1 < len(lines) and plain.strip()
                else None
            )
            if heading or underline:
                flush_paragraph()
                if heading:
                    level = len(heading[1])
                    content = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
                else:
                    level = 1 if underline[1].startswith("=") else 2
                    content = plain.strip()
                    position += 1
                if content:
                    headings = headings[: level - 1]
                    headings.extend([""] * (level - 1 - len(headings)))
                    headings.append(content)
                    title = title or content
                    blocks.append(
                        make_block(
                            doc_id,
                            len(blocks) + 1,
                            page=1,
                            block_type="title",
                            text=content,
                            section=" / ".join(part for part in headings if part),
                        )
                    )
            elif not plain.strip():
                flush_paragraph()
            else:
                paragraph.append(line)
            position += 1
        flush_paragraph()
        if not blocks:
            raise NoTextError("文档没有可用原生文本")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            title=title,
            blocks=blocks,
        )
