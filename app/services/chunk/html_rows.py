"""切片专用的两遍HTML行扫描；不建立整表DOM或展开全表网格，不修改Adapter。"""

from collections import deque
from html.parser import HTMLParser
from itertools import islice
import re

from app.core.business_logging import log_event
from app.services.chunk.splitter import Span, token_count

FEED_CHARS = 64 * 1024


class UnsupportedRows(ValueError):
    """仅表示不能证明安全行边界，不包含原HTML或解析器异常文本。"""


class RowScanner(HTMLParser):
    """只维护当前标签栈及最多1024列的rowspan终点，工作内存不随总行数增长。"""

    def __init__(self, text, start, end, emit=None):
        """绑定原Block区间；emit仅接收完整rowspan关联组的原字符偏移，不接收正文。"""
        super().__init__(convert_charrefs=False)
        self.text, self.start, self.end, self.emit = text, start, end, emit
        self.absolute_offset, self.stack, self.columns = start, [], []
        self.root_seen = self.root_closed = self.tbody_seen = self.in_cell = False
        self.rows = self.cells = self.width = self.groups = 0
        self.first_row = self.last_row_end = self.group_start = self.ready_group = None

    def updatepos(self, i, j):
        """增量跟踪绝对字符偏移，避免为数十万行保存整篇换行索引。"""
        self.absolute_offset += j - i
        return super().updatepos(i, j)

    def handle_starttag(self, tag, attrs):
        """验证原有有限标签/属性和当前行网格；不提取、改写或执行单元格内容。"""
        if tag == "table" and not self.stack and not self.root_seen and not attrs:
            self.root_seen = True
        elif (tag == "tbody" and self.stack == ["table"] and not self.rows
              and not self.tbody_seen and not attrs):
            self.tbody_seen = True
        elif tag == "tr" and self.stack in (["table"], ["table", "tbody"]) and not attrs:
            if self.ready_group is not None:
                # 行间原空白归前组，不能丢失或在相邻组重复计作content。
                if self.emit is not None:
                    self.emit((self.ready_group, self.absolute_offset))
                self.ready_group = None
            if self.first_row is None:
                self.first_row = self.absolute_offset
            if self.group_start is None:
                self.group_start = self.absolute_offset
            self.cells = self.column = 0
        elif tag in {"td", "th"} and self.stack and self.stack[-1] == "tr" and not self.in_cell:
            values = dict(attrs)
            if len(values) != len(attrs) or set(values) - {"rowspan", "colspan"}:
                raise UnsupportedRows
            spans = []
            for name in ("rowspan", "colspan"):
                raw = values.get(name, "1")
                if (not isinstance(raw, str) or len(raw) > 3 or not raw.isascii()
                        or not raw.isdigit() or not 1 <= int(raw) <= 512):
                    raise UnsupportedRows
                spans.append(int(raw))
            rowspan, colspan = spans
            while self.column < len(self.columns) and self.columns[self.column] > self.rows:
                self.column += 1
            right = self.column + colspan
            if right > 1024:
                raise UnsupportedRows
            self.columns.extend([0] * (right - len(self.columns)))
            if any(self.columns[index] > self.rows for index in range(self.column, right)):
                raise UnsupportedRows
            for index in range(self.column, right):
                self.columns[index] = self.rows + rowspan
            self.column, self.in_cell, self.cells = right, True, self.cells + 1
        elif tag == "p" and self.in_cell and self.stack[-1] in {"td", "th"} and not attrs:
            pass
        elif tag == "strong" and self.in_cell and self.stack[-1] in {"td", "th", "p"} and not attrs:
            pass
        elif tag in {"eq", "br"} and self.in_cell and self.stack[-1] in {"td", "th", "p"} and not attrs:
            if tag == "br":
                return
        else:
            raise UnsupportedRows
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        """只接受已有契约的单元格br自闭合，其他隐式闭合不猜测修复。"""
        if tag != "br":
            raise UnsupportedRows
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        """每行核对完整列占用，所有跨行单元格结束后才开放组边界。"""
        if not self.stack or self.stack[-1] != tag:
            raise UnsupportedRows
        self.stack.pop()
        if tag in {"td", "th"}:
            self.in_cell = False
        elif tag == "tr":
            self.width = self.width or len(self.columns)
            if (not self.cells or len(self.columns) != self.width
                    or any(bottom <= self.rows for bottom in self.columns)):
                raise UnsupportedRows
            end = self.text.find(">", self.absolute_offset, self.end)
            if end < 0:
                raise UnsupportedRows
            self.last_row_end = end + 1
            self.rows += 1
            if max(self.columns) == self.rows:
                self.ready_group, self.group_start = self.group_start, None
                self.groups += 1
        elif tag == "table":
            self.root_closed = True

    def handle_data(self, data):
        """单元格正文只验证位置；不保存文本副本或改写实体。"""
        if not self.in_cell and data.strip():
            raise UnsupportedRows

    def handle_entityref(self, name):
        """实体仅允许在单元格内，实际输出始终引用原字符区间。"""
        if not self.in_cell:
            raise UnsupportedRows

    def handle_charref(self, name):
        """数值实体执行同一位置校验，不解码或重序列化原表。"""
        self.handle_entityref(name)

    def handle_comment(self, data):
        """不接受可能隐藏结构或改变分片含义的注释。"""
        raise UnsupportedRows

    def handle_decl(self, decl):
        """不执行或接受DOCTYPE及外部声明。"""
        raise UnsupportedRows

    def handle_pi(self, data):
        """禁止处理指令。"""
        raise UnsupportedRows

    def unknown_decl(self, data):
        """未知声明不能由底层解析器静默吞掉。"""
        raise UnsupportedRows

    def finish(self):
        """核对完整闭合和末尾rowspan，没有完整矩形或尾部越界则不能安全切行。"""
        self.close()
        if (not self.root_closed or self.stack or self.in_cell or not self.rows
                or self.group_start is not None or self.ready_group is None):
            raise UnsupportedRows
        if self.emit is not None:
            self.emit((self.ready_group, self.last_row_end))


def scan(text, start, end, emit=None):
    """按有界输入喂给扫描器；拒绝无限增长的不完整标签缓冲，不按总行数拒绝。"""
    parser = RowScanner(text, start, end, emit)
    for offset in range(start, end, FEED_CHARS):
        parser.feed(text[offset:min(offset + FEED_CHARS, end)])
        if len(parser.rawdata) > FEED_CHARS:
            raise UnsupportedRows
        yield parser
    parser.finish()
    yield parser


def html_rows(block, budget):
    """先完整验证再逐组输出；大表不受整表行/单元格预算限制，坏表只保留一次全块。"""
    whole = [Span(block, 0, len(block.text))]
    if len(block.text) <= budget * 4 and token_count(whole) <= budget:
        yield whole
        return
    openings = list(islice(re.finditer(r"<table(?:\s|>)", block.text, re.I), 2))
    closings = list(islice(re.finditer(r"</table\s*>", block.text, re.I), 2))
    if len(openings) != 1 or len(closings) != 1 or openings[0].start() >= closings[0].start():
        log_event("chunk.table_split_skipped", block_id=block.block_id, page=block.page,
                  stage="chunk.table_rows", step="AMBIGUOUS_TABLE_BODY")
        yield whole
        return
    start, end = openings[0].start(), closings[0].end()
    if not block.text[:start].strip():
        start = 0
    if not block.text[end:].strip():
        end = len(block.text)
    try:
        for validated in scan(block.text, start, end):
            pass
    except (ValueError, AssertionError):
        # 第一遍未通过前绝不yield任何子表，避免坏尾部与全表回退造成重复或半份来源。
        log_event("chunk.table_split_skipped", block_id=block.block_id, page=block.page,
                  stage="chunk.table_rows", step="UNSUPPORTED_TABLE_ROWS")
        yield whole
        return
    if validated.groups == 1:
        yield whole
        return

    def spans_for(first, last):
        """壳只在首/末片记主内容，重复壳记context；行间空白保留精确原偏移。"""
        spans = [Span(block, start, validated.first_row, "content" if first == validated.first_row else "context"),
                 Span(block, first, last),
                 Span(block, validated.last_row_end, end, "content" if last == validated.last_row_end else "context")]
        return [span for span in spans if span.end > span.start]

    if start:
        yield [Span(block, 0, start)]
    shell_cost = token_count([Span(block, start, validated.first_row), Span(block, validated.last_row_end, end)])
    shell_chars = validated.first_row - start + end - validated.last_row_end
    queue, first, last, cost, emitted = deque(), None, None, 0, 0
    for _ in scan(block.text, start, end, queue.append):
        while queue:
            next_first, next_last = queue.popleft()
            current_cost = token_count([Span(block, next_first, next_last)])
            if first is None:
                first, last, cost = next_first, next_last, current_cost
                continue
            # 组边界只在tr之间，估算词不会跨标签延续；增量计数避免反复扫描已累计的行。
            if cost + current_cost + shell_cost > budget or next_last - first + shell_chars > budget * 4:
                yield spans_for(first, last)
                emitted += 1
                first, cost = next_first, 0
            last, cost = next_last, cost + current_cost
    yield spans_for(first, last)
    if emitted:
        log_event("chunk.table_split", block_id=block.block_id, page=block.page,
                  stage="chunk.html_stream_rows", chunk_count=emitted + 1)
    if end < len(block.text):
        yield [Span(block, end, len(block.text))]
