"""受限HTML表格结构核对与V1原字节行切片；不渲染、不执行或从layout生成正文。"""

from html import unescape
from html.parser import HTMLParser
from dataclasses import dataclass
import re

from app.services.mineru.adapter import merge_evidence_error


@dataclass(frozen=True)
class TableLimits:
    """资源预算与网格正确性分离；Adapter默认旧预算，仅新切片显式扩大有限规模。"""

    max_chars: int = 4 * 1024**2
    max_rows: int = 4096
    max_cells: int = 65536
    max_grid_cells: int = 262144


class TableHTML(HTMLParser):
    """校验表格网格及显式启用的行内标签；不推断或计算公式。"""

    def __init__(self, text: str, *, limits: TableLimits = TableLimits(), cell_paragraphs: bool = False,
                 cell_strong: bool = False):
        """读取原行区间；新切片显式接受单元格p/strong，默认Adapter与旧策略保持不变。"""
        super().__init__(convert_charrefs=False)
        self.limits, self.cell_paragraphs = limits, cell_paragraphs
        self.cell_strong = cell_strong
        if not isinstance(text, str) or not 0 < len(text) <= limits.max_chars:
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
        """校验嵌套/跨度；显式接纳单元格无属性strong，未知标签/属性与结构仍拒绝。"""
        if tag == "table" and not self.stack and not self.root_seen and not attrs:
            self.root_seen = True
        elif tag == "tbody" and self.stack == ["table"] and not self.rows and not self.tbody_seen and not attrs:
            self.tbody_seen = True
        elif tag == "tr" and self.stack in (["table"], ["table", "tbody"]) and not attrs:
            if len(self.rows) >= self.limits.max_rows:
                raise merge_evidence_error("TABLE_LIMIT")
            self.rows.append({"start": self.source_offset(), "cells": []})
        elif tag in {"td", "th"} and self.stack and self.stack[-1] == "tr" and self.cell is None:
            values = dict(attrs)
            self.cell_count += 1
            if self.cell_count > self.limits.max_cells:
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
            self.cell = {"tag": tag, "parts": [], "breaks": 0,
                         "content_start": self.text.find(">", self.source_offset()) + 1, **spans}
        elif (tag == "p" and self.cell_paragraphs and self.cell is not None
              and self.stack[-1] in {"td", "th"} and not attrs):
            # 段落边界只用于网格核对的文字摘要；实际Chunk始终引用原HTML，不改写字符。
            self.cell["parts"].append(" ")
        elif (tag == "strong" and self.cell_strong and self.cell is not None
              and self.stack[-1] in {"td", "th", "p"} and not attrs):
            # 加粗仅改变样式；摘要不添空格，Chunk仍按原字符区间保留标签及实体。
            pass
        elif (tag == "br" and self.cell is not None
              and self.stack[-1] in ({"td", "th", "p"} if self.cell_paragraphs else {"td", "th"}) and not attrs):
            self.cell["parts"].append(" ")
            self.cell["breaks"] += 1
            return
        elif (tag == "eq" and self.cell is not None
              and self.stack[-1] in ({"td", "th", "p"} if self.cell_paragraphs else {"td", "th"}) and not attrs):
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
        """关闭结构并固定原行区间；单元格p只保留摘要边界，失配仍拒绝不修复。"""
        if not self.stack or self.stack[-1] != tag:
            raise merge_evidence_error("TABLE_HTML")
        self.stack.pop()
        if tag == "eq":
            self.cell["parts"].append("$ ")
        elif tag == "p":
            self.cell["parts"].append(" ")
        elif tag in {"td", "th"}:
            self.cell["content_end"] = self.source_offset()
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
        """在显式网格预算内核对占用，仍拒绝重叠、缺口、越界rowspan及不完整行。"""
        occupied, width, cell_count = set(), 0, 0
        for row_index, row in enumerate(self.rows):
            column = 0
            for cell in row["cells"]:
                cell_count += 1
                while (row_index, column) in occupied:
                    column += 1
                bottom, right = row_index + cell["rowspan"], column + cell["colspan"]
                if bottom > len(self.rows) or right > 1024 or cell_count > self.limits.max_cells:
                    raise merge_evidence_error("TABLE_GRID")
                cell["column"], cell["right"] = column, right
                for r in range(row_index, bottom):
                    for c in range(column, right):
                        if (r, c) in occupied or len(occupied) >= self.limits.max_grid_cells:
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

    def split_boundary_row(self, index: int, left: dict, right: dict, *,
                           allow_column_refinement=False) -> tuple[str, str]:
        """切分V1续接行；网格细化仅供后续整页投影核对，原标签和数字不重写。

        默认仍要求列边界完全相同。显式细化时只推迟列边界检查，调用方必须对
        两份完整输出执行verify_projection，不能仅凭本行文字相同接受列归属。
        """
        row = self.rows[index]
        if len(left["cells"]) != len(right["cells"]) or len(left["cells"]) != len(row["cells"]):
            raise merge_evidence_error("TABLE_CELLS")
        fragments = [[], []]
        cursor = row["start"]
        for a, b, target in zip(left["cells"], right["cells"], row["cells"]):
            keys = ("tag",) if allow_column_refinement else ("tag", "column", "right")
            if (any(cell["rowspan"] != 1 for cell in (a, b, target))
                    or any(a[key] != b[key] or a[key] != target[key] for key in keys)):
                raise merge_evidence_error("TABLE_GRID")
            start, end = target["content_start"], target["content_end"]
            raw = self.text[start:end]
            if not a["text"] or not b["text"]:
                # 整个单元格只属于一页时保留其完整内联标记，不做不必要的内部切割。
                if "".join(target["text"].split()) != "".join((a["text"] + b["text"]).split()):
                    raise merge_evidence_error("TABLE_CELLS")
                parts = ("", raw) if not a["text"] else (raw, "")
            else:
                parts = split_cell_text(raw, a["text"], b["text"])
            for result, part in zip(fragments, parts):
                result.extend((self.text[cursor:start], part))
            cursor = end
        tail = self.text[cursor:row["end"]]
        return tuple("".join(parts) + tail for parts in fragments)


def split_cell_text(raw: str, left: str, right: str) -> tuple[str, str]:
    """依据两页非空白字符序列切V1原单元格，支持实体/br但不在实体或公式标签内部切割。

    空白仍保留在原V1切片中；从不从layout生成字词，两个返回切片必须原样拼回raw。
    未支持的内联标签安全拒绝，不尝试修复HTML或替换公式。
    """
    first, second = "".join(left.split()), "".join(right.split())
    expected = first + second
    cursor = count = previous_end = 0
    cut = None
    pattern = re.compile(r"&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);|<br\s*/?>", re.I)
    while cursor < len(raw):
        token = pattern.match(raw, cursor)
        stop = token.end() if token else cursor + 1
        if raw[cursor] == "<" and not token:
            raise merge_evidence_error("TABLE_CONTINUATION_MARKUP")
        text = " " if token and token.group().startswith("<") else unescape(raw[cursor:stop])
        for char in text:
            if char.isspace():
                continue
            if count >= len(expected) or char != expected[count]:
                raise merge_evidence_error("TABLE_CELLS")
            if count == len(first) and cut is None:
                if previous_end > cursor:
                    raise merge_evidence_error("TABLE_CONTINUATION_MARKUP")
                cut = cursor
            count += 1
            previous_end = stop
        cursor = stop
    if count != len(expected):
        raise merge_evidence_error("TABLE_CELLS")
    if not first:
        return "", raw
    if not second:
        return raw, ""
    # 多字符实体不可从中间分成两页；普通空白归前片，原HTML/字符仍无损保留。
    return raw[:cut], raw[cut:]


def verify_projection(source: TableHTML, piece: TableHTML, *, continuation_rows=frozenset()) -> None:
    """证明单元格及rowspan逐项一致，列网格只允许一致的严格单调细化映射。

    例如4列网格可映射为[0,3,6,9,10]，但任何单元格合并/交换/数值变化、
    不一致的列边界或跨分片rowspan均拒绝；仅已证明续接行允许空白序列化差异，
    非空白字符仍逐项一致。验证不会改写V1的colspan或任何正文。
    """
    # tbody只是可选HTML包装，不改变行列；br与空白的序列化差异由单元格文字核对。
    if len(source.rows) != len(piece.rows):
        raise merge_evidence_error("TABLE_ROWS")
    edges = {}
    for row_index, (before, after) in enumerate(zip(source.rows, piece.rows)):
        if len(before["cells"]) != len(after["cells"]):
            raise merge_evidence_error("TABLE_CELLS")
        for a, b in zip(before["cells"], after["cells"]):
            if any(a[key] != b[key] for key in ("tag", "rowspan")):
                raise merge_evidence_error("TABLE_CELLS")
            if (a["text"] != b["text"] and (row_index not in continuation_rows
                    or "".join(a["text"].split()) != "".join(b["text"].split()))):
                raise merge_evidence_error("TABLE_CELLS")
            for key in ("column", "right"):
                if a[key] in edges and edges[a[key]] != b[key]:
                    raise merge_evidence_error("TABLE_GRID")
                edges[a[key]] = b[key]
    ordered = sorted(edges)
    if (edges.get(0) != 0 or edges.get(source.width) != piece.width
            or any(edges[b] - edges[a] < b - a for a, b in zip(ordered, ordered[1:]))):
        raise merge_evidence_error("TABLE_GRID")
