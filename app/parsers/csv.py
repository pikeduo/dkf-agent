"""CSV 原生表格解析，保留字段顺序、引号与空字段，不猜测分隔符或执行公式。"""

import csv
from io import StringIO
from pathlib import Path
from uuid import UUID

from app.parsers.base import BaseParser
from app.parsers.common import NoTextError, ParserError, make_block
from app.parsers.text import read_text
from app.schemas.parsed_document import FileType, ParsedDocument


def table_text(rows: list[list[str]], *, delimiter: str = "\t") -> str:
    """用标准 CSV writer 序列化表格，字段内换行和分隔符仍可被正确还原。"""

    buffer = StringIO(newline="")
    csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buffer.getvalue().removesuffix("\n")


class CsvParser(BaseParser):
    """逗号分隔 CSV 输出一个 table 块，逻辑页 1，无原件布局坐标。"""

    def _parse(
        self, *, doc_id: UUID, file_path: Path, file_name: str, file_type: FileType
    ) -> ParsedDocument:
        """严格解码和读取字段；空内容、引号异常、列数不一致及超量表格安全失败。"""

        if file_type != "csv":
            raise ParserError("CSV 解析器仅接受 csv 文件")
        rows = []
        width = None
        cells = 0
        try:
            reader = csv.reader(StringIO(read_text(file_path), newline=""), strict=True)
            for row in reader:
                if not row:
                    continue
                width = len(row) if width is None else width
                if len(row) != width:
                    raise ParserError("CSV 各行列数不一致，请检查分隔符和引号")
                cells += len(row)
                if len(rows) >= 100000 or width > 1024 or cells > 1000000:
                    raise ParserError("CSV 行列规模超过原生解析安全上限")
                rows.append(row)
        except csv.Error:
            raise ParserError("CSV 引号或字段结构无效，或单字段超过读取上限") from None
        if not any(cell.strip() for row in rows for cell in row):
            raise NoTextError("CSV 没有可用单元格内容")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            blocks=[
                make_block(doc_id, 1, page=1, block_type="table", text=table_text(rows))
            ],
        )
