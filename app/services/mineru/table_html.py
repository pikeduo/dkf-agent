"""受限HTML表格结构核对与V1原字节行切片；不渲染、不执行或从layout生成正文。"""

from html import unescape
from html.parser import HTMLParser

from app.services.mineru.adapter import merge_evidence_error


class TableHTML(HTMLParser):
    """仅接受表格网格及已验证的br/eq序列化；不推断或计算公式。"""

    def __init__(self, text: str):
        """有界解析原HTML并记录每行原字符区间、单元格内容与跨度。"""
        super().__init__(convert_charrefs=False)
        if not isinstance(text, str) or not 0 < len(text) <= 4 * 1024**2:
            raise merge_evidence_error("TABLE_HTML")
        self.text, self.rows, self.stack, self.cell = text, [], [], None
        self.starts = [0]
        self.starts.extend(index + 1 for index, char in enumerate(text) if char == "\n")
        self.root_seen = self.root_closed = self.tbody_seen = False
        self.cell_count = 0
        self.feed(text)
        self.close()
        if not self.root_closed or self.stack or not self.rows or self.cell is not None:
            raise merge_evidence_error("TABLE_HTML")
        self.width = self.validate_grid()

    def source_offset(self) -> int:
        """把HTMLParser位置映射回输入原字符，保留实体和所有数字的原始写法。"""
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        """校验严格嵌套和跨度，记录原始tr边界；不忽略未知标签或属性。"""
        if tag == "table" and not self.stack and not self.root_seen and not attrs:
            self.root_seen = True
        elif tag == "tbody" and self.stack == ["table"] and not self.rows and not self.tbody_seen and not attrs:
            self.tbody_seen = True
        elif tag == "tr" and self.stack in (["table"], ["table", "tbody"]) and not attrs:
            if len(self.rows) >= 4096:
                raise merge_evidence_error("TABLE_LIMIT")
            self.rows.append({"start": self.source_offset(), "cells": []})
        elif tag in {"td", "th"} and self.stack and self.stack[-1] == "tr" and self.cell is None:
            values = dict(attrs)
            self.cell_count += 1
            if self.cell_count > 65536:
                raise merge_evidence_error("TABLE_LIMIT")
            if len(values) != len(attrs) or set(values) - {"rowspan", "colspan"}:
                raise merge_evidence_error("TABLE_HTML")
            spans = {}
            for name in ("rowspan", "colspan"):
                raw = values.get(name, "1")
                if (not isinstance(raw, str) or len(raw) > 3 or not raw.isascii()
                        or not raw.isdigit() or not 1 <= int(raw) <= 512):
                    raise merge_evidence_error("TABLE_GRID")
                spans[name] = int(raw)
            self.cell = {"tag": tag, "parts": [], "breaks": 0, **spans}
        elif tag == "br" and self.cell is not None and self.stack[-1] in {"td", "th"} and not attrs:
            self.cell["parts"].append(" ")
            self.cell["breaks"] += 1
            return
        elif tag == "eq" and self.cell is not None and self.stack[-1] in {"td", "th"} and not attrs:
            # layout 的 <eq>x</eq> 在 V1 中为 ` $x$ `；仅作为来源比较，不输出layout文字。
            self.cell["parts"].append(" $")
        else:
            raise merge_evidence_error("TABLE_HTML")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        """只允许单元格内的自闭合br，不推测其他隐式HTML闭合。"""
        if tag != "br":
            raise merge_evidence_error("TABLE_HTML")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        """关闭当前结构并固定行区间；任何失配均拒绝，不自动修复HTML。"""
        if not self.stack or self.stack[-1] != tag:
            raise merge_evidence_error("TABLE_HTML")
        self.stack.pop()
        if tag == "eq":
            self.cell["parts"].append("$ ")
        elif tag in {"td", "th"}:
            self.cell["text"] = " ".join(unescape("".join(self.cell.pop("parts"))).split())
            self.rows[-1]["cells"].append(self.cell)
            self.cell = None
        elif tag == "tr":
            if not self.rows[-1]["cells"]:
                raise merge_evidence_error("TABLE_GRID")
            end = self.text.find(">", self.source_offset())
            if end < 0:
                raise merge_evidence_error("TABLE_HTML")
            self.rows[-1]["end"] = end + 1
        elif tag == "table":
            self.root_closed = True

    def handle_data(self, data):
        """只有单元格可含正文；结构外非空白内容不偷偷丢弃。"""
        if self.cell is not None:
            self.cell["parts"].append(data)
        elif data.strip():
            raise merge_evidence_error("TABLE_HTML")

    def handle_entityref(self, name):
        """把实体原写法交给单元格核对，输出仍使用V1原字符。"""
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        """保留数值实体身份，不转成新的输出文本。"""
        self.handle_data("&#" + name + ";")

    def handle_comment(self, data):
        """不接受可能改变切分语义的注释或隐藏数据。"""
        raise merge_evidence_error("TABLE_HTML")

    def handle_decl(self, decl):
        """禁止DOCTYPE和外部声明。"""
        raise merge_evidence_error("TABLE_HTML")

    def handle_pi(self, data):
        """禁止处理指令。"""
        raise merge_evidence_error("TABLE_HTML")

    def unknown_decl(self, data):
        """未知声明不能被解析器静默吞掉。"""
        raise merge_evidence_error("TABLE_HTML")

    def validate_grid(self) -> int:
        """展开有限行列占用图，拒绝重叠、缺口、越界rowspan及不完整行。"""
        occupied, width, cell_count = set(), 0, 0
        for row_index, row in enumerate(self.rows):
            column = 0
            for cell in row["cells"]:
                cell_count += 1
                while (row_index, column) in occupied:
                    column += 1
                bottom, right = row_index + cell["rowspan"], column + cell["colspan"]
                if bottom > len(self.rows) or right > 1024 or cell_count > 65536:
                    raise merge_evidence_error("TABLE_GRID")
                cell["column"], cell["right"] = column, right
                for r in range(row_index, bottom):
                    for c in range(column, right):
                        if (r, c) in occupied or len(occupied) >= 262144:
                            raise merge_evidence_error("TABLE_GRID")
                        occupied.add((r, c))
                column, width = right, max(width, right)
        if len(occupied) != len(self.rows) * width:
            raise merge_evidence_error("TABLE_GRID")
        return width

    def slice_rows(self, start: int, end: int) -> str:
        """仅切取V1原行区间并复用其外壳；不复制任何单元格、不跨rowspan截断。"""
        if not 0 <= start < end <= len(self.rows):
            raise merge_evidence_error("TABLE_ROWS")
        stop = self.rows[end]["start"] if end < len(self.rows) else self.rows[-1]["end"]
        return (self.text[:self.rows[0]["start"]] + self.text[self.rows[start]["start"]:stop]
                + self.text[self.rows[-1]["end"]:])

    def select_rows(self, indices: list[int]) -> str:
        """按已证明的来源映射取V1原行；可复用真实重复页首行，不生成单元格文字。

        调用方须证明重复行确实存在于各原页。返回HTML仍需重新校验网格，禁止跨
        rowspan断开；索引必须严格递增，不能把任意行复制成一个新表格。
        """
        if (not indices or any(type(index) is not int or not 0 <= index < len(self.rows) for index in indices)
                or any(left >= right for left, right in zip(indices, indices[1:]))):
            raise merge_evidence_error("TABLE_ROWS")
        fragments = []
        for index in indices:
            end = self.rows[index + 1]["start"] if index + 1 < len(self.rows) else self.rows[index]["end"]
            fragments.append(self.text[self.rows[index]["start"]:end])
        return self.text[:self.rows[0]["start"]] + "".join(fragments) + self.text[self.rows[-1]["end"]:]


def verify_projection(source: TableHTML, piece: TableHTML) -> None:
    """证明单元格及rowspan逐项一致，列网格只允许一致的严格单调细化映射。

    例如4列网格可映射为[0,3,6,9,10]，但任何单元格合并/交换/数值变化、
    不一致的列边界或跨分片rowspan均拒绝；验证不会改写V1的colspan。
    """
    # tbody只是可选HTML包装，不改变行列；br与空白的序列化差异由单元格文字核对。
    if len(source.rows) != len(piece.rows):
        raise merge_evidence_error("TABLE_ROWS")
    edges = {}
    for before, after in zip(source.rows, piece.rows):
        if len(before["cells"]) != len(after["cells"]):
            raise merge_evidence_error("TABLE_CELLS")
        for a, b in zip(before["cells"], after["cells"]):
            if any(a[key] != b[key] for key in ("tag", "text", "rowspan")):
                raise merge_evidence_error("TABLE_CELLS")
            for key in ("column", "right"):
                if a[key] in edges and edges[a[key]] != b[key]:
                    raise merge_evidence_error("TABLE_GRID")
                edges[a[key]] = b[key]
    ordered = sorted(edges)
    if (edges.get(0) != 0 or edges.get(source.width) != piece.width
            or any(edges[b] - edges[a] < b - a for a, b in zip(ordered, ordered[1:]))):
        raise merge_evidence_error("TABLE_GRID")
