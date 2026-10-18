"""保留阅读顺序与字符级证据的确定性切片，不重排版面或计算公式。"""

import hashlib
import json
import re
from dataclasses import dataclass, replace
from itertools import islice
from collections.abc import Iterator
from uuid import UUID, uuid5

VERSION = "block-chunk-v1"
TOKENIZER = "unicode-estimate-v1"
MAX_SOURCE_CHARS = 10_000_000
MAX_BLOCKS = 100_000
MAX_CHUNKS = 20_000
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\s]", re.UNICODE)


class ChunkError(Exception):
    """固定的安全业务错误，不携带正文、本机路径或数据库异常原文。"""


@dataclass(frozen=True)
class ChunkOptions:
    """切片参数快照；单位是本地估算 token，不声称等于模型 tokenizer。"""

    size: int = 600
    overlap: int = 80
    version: str = VERSION
    tokenizer: str = TOKENIZER

    def validate(self) -> None:
        """拒绝未知算法版本及超出第一版允许范围的检查点参数。"""

        if (type(self.size) is not int or type(self.overlap) is not int
                or not 400 <= self.size <= 700 or not 50 <= self.overlap <= 100
                or self.version != VERSION or self.tokenizer != TOKENIZER):
            raise ChunkError("CHUNK_CONFIG_INVALID：切片参数或算法版本不可用")

    def snapshot(self) -> dict:
        """返回可冻结、可比较且不含原文的参数字典。"""

        self.validate()
        return {"size": self.size, "overlap": self.overlap,
                "version": self.version, "tokenizer": self.tokenizer}


@dataclass(frozen=True)
class SourceBlock:
    """切片的输入快照，与 ORM、Celery 和 ParsedDocument 相互独立。"""

    block_id: UUID
    block_index: int | None
    page: int
    section: str | None
    block_type: str
    text: str
    source: str
    bbox: list[float] | None = None
    provenance: dict | None = None


@dataclass(frozen=True)
class Span:
    """原 Block 的字符区间；结束偏移不包含自身，重复上下文单独标注角色。"""

    block: SourceBlock
    start: int
    end: int
    role: str = "content"

    @property
    def text(self) -> str:
        """按原始字符偏移取文本，不清洗公式、表格或 OCR 字符串。"""

        return self.block.text[self.start:self.end]


@dataclass(frozen=True)
class ChunkDraft:
    """供事务持久化的切片，保留身份、范围、顺序和完整证据 metadata。"""

    chunk_id: UUID
    kb_id: UUID
    doc_id: UUID
    text: str
    page_start: int
    page_end: int
    section: str | None
    metadata: dict


def iter_token_ranges(text: str) -> Iterator[tuple[int, int]]:
    """流式枚举估算 token 字符跨度，避免长正文一次分配数百万区间对象。"""

    for match in TOKEN_PATTERN.finditer(text):
        step = 4 if match.group().isascii() and re.fullmatch(r"[A-Za-z0-9_]+", match.group()) else 1
        for start in range(match.start(), match.end(), step):
            yield start, min(start + step, match.end())


def token_ranges(text: str) -> list[tuple[int, int]]:
    """物化短正文/重叠片段的跨度；中文逐字、ASCII 词每四字符、空白不计。"""

    return list(iter_token_ranges(text))


def token_count(spans: list[Span]) -> int:
    """计量源文本的估算 token，拼接分隔符不视为原文证据。"""

    # 长结构块只计数、不分配所有 token 跨度；仍采用同一确定性计量规则。
    count = 0
    for span in spans:
        for match in TOKEN_PATTERN.finditer(span.text):
            token = match.group()
            count += (len(token) + 3) // 4 if token.isascii() and re.fullmatch(r"[A-Za-z0-9_]+", token) else 1
    return count


def text_spans(block: SourceBlock, budget: int) -> list[Span]:
    """优先保留空行分隔的段落，超长段落再按 token 区间切分，绝不截断尾部。"""

    boundaries = [0, *(match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n+", block.text)), len(block.text)]
    result = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        cuts = [start, *(start + left for index, (left, _) in enumerate(iter_token_ranges(block.text[start:end]))
                        if index > 0 and index % budget == 0), end]
        result.extend(Span(block, left, right) for left, right in zip(cuts, cuts[1:]) if right > left)
    return result


def tail_overlap(spans: list[Span], budget: int) -> list[Span]:
    """只复用普通正文尾部，不重复表格、公式、标题或之前的 overlap。"""

    result = []
    for span in reversed(spans):
        if span.role != "content" or span.block.block_type not in {"text", "image_text"}:
            break
        ranges = token_ranges(span.text)
        if not ranges:
            continue
        take = min(budget, len(ranges))
        start = span.start if take == len(ranges) else span.start + ranges[-take][0]
        result.insert(0, replace(span, start=start, role="overlap"))
        budget -= take
        if budget <= 0:
            break
    return result


def formula_units(atoms: list[list[Span]], budget: int) -> list[list[Span]]:
    """公式优先与紧邻的同章节正文组成不可拆单元；过长时仍保留公式自身完整。"""

    result = []
    index = 0
    while index < len(atoms):
        unit = list(atoms[index])
        if unit[0].block.block_type == "formula":
            if (result and len(result[-1]) == 1
                    and result[-1][0].block.block_type in {"text", "image_text"}
                    and result[-1][0].block.section == unit[0].block.section
                    and token_count(result[-1] + unit) <= budget):
                unit = result.pop() + unit
            if (index + 1 < len(atoms)
                    and atoms[index + 1][0].block.block_type in {"text", "image_text"}
                    and atoms[index + 1][0].block.section == unit[-1].block.section
                    and token_count(unit + atoms[index + 1]) <= budget):
                index += 1
                unit += atoms[index]
        result.append(unit)
        index += 1
    return result


def build_chunks(doc_id: UUID, kb_id: UUID, file_type: str,
                 blocks: list[SourceBlock], options: ChunkOptions | None = None) -> list[ChunkDraft]:
    """按可信 block_index 切片并生成稳定 ID；不执行文件读取、分页推断或外部调用。"""

    options = options or ChunkOptions()
    config = options.snapshot()
    if not blocks or not any(block.text.strip() for block in blocks):
        raise ChunkError("CHUNK_EMPTY：没有可切片正文")
    if len(blocks) > MAX_BLOCKS or sum(len(block.text) for block in blocks) > MAX_SOURCE_CHARS:
        raise ChunkError("CHUNK_LIMIT：切片源文本超过本地资源上限")
    if (len({block.block_id for block in blocks}) != len(blocks)
            or any(type(block.block_index) is not int or block.block_index != index
                   or block.page < 1 or block.block_type not in {"title", "text", "table", "formula", "image_text"}
                   for index, block in enumerate(blocks, 1))):
        raise ChunkError("CHUNK_SOURCE_ORDER：解析块身份或阅读序号不可用，请明确重新解析")
    payload = [{"id": str(block.block_id), "index": block.block_index, "page": block.page,
                "section": block.section, "type": block.block_type, "text": block.text,
                "source": block.source, "bbox": block.bbox} for block in blocks]
    for value, block in zip(payload, blocks):
        if block.provenance is not None:
            value["provenance"] = block.provenance
    input_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    # 同一个长 Block 可贡献很多片段，整块 Hash 只计算一次，避免随 Chunk 数量重复扫描全文。
    text_hashes = {block.block_id: hashlib.sha256(block.text.encode()).hexdigest() for block in blocks}
    atoms = []
    for block in blocks:
        if block.text.strip():
            atoms.extend([[span] for span in text_spans(block, options.size - 100)]
                         if block.block_type not in {"table", "formula"}
                         else [[Span(block, 0, len(block.text))]])
    units = formula_units(atoms, options.size - 100)
    result: list[ChunkDraft] = []
    pending: list[Span] = []
    heading: Span | None = None
    section: str | None = None

    def flush() -> list[Span]:
        """输出一个完整切片并返回普通正文重叠，metadata 同时保存源与拼接后的字符位置。"""

        nonlocal pending
        if not pending or not any(span.role == "content" for span in pending):
            pending = []
            return []
        text_parts, evidence = [], []
        offset = 0
        for span in pending:
            if text_parts:
                offset += 2
            text_parts.append(span.text)
            evidence.append({"block_id": str(span.block.block_id), "block_index": span.block.block_index,
                "page": span.block.page, "block_type": span.block.block_type, "source": span.block.source,
                "bbox": span.block.bbox, "start_char": span.start, "end_char": span.end,
                "chunk_start_char": offset, "chunk_end_char": offset + len(span.text), "role": span.role,
                "block_text_hash": text_hashes[span.block.block_id]})
            if span.block.provenance is not None:
                evidence[-1]["provenance"] = span.block.provenance
                evidence[-1]["page_reliable"] = span.block.provenance.get("page_reliable", False)
            offset += len(span.text)
        text = "\n\n".join(text_parts)
        index = len(result) + 1
        metadata = {"chunk_index": index, "chunker_version": VERSION, "tokenizer": TOKENIZER,
            "config": config, "input_blocks_hash": input_hash, "estimated_token_count": token_count(pending),
            "oversized": token_count(pending) > options.size,
            "page_semantics": "physical_pdf" if file_type == "pdf" else "image_page" if file_type in {"jpg", "jpeg", "png"} else "logical",
            "source_spans": evidence}
        identity = json.dumps({"metadata": metadata, "text": text, "kb_id": str(kb_id)}, ensure_ascii=False, sort_keys=True)
        pages = [span.block.page for span in pending]
        result.append(ChunkDraft(uuid5(doc_id, identity), kb_id, doc_id, text,
                                 min(pages), max(pages), section, metadata))
        if len(result) > MAX_CHUNKS:
            raise ChunkError("CHUNK_LIMIT：切片数量超过本地资源上限")
        overlap = tail_overlap(pending, options.overlap)
        pending = []
        return overlap

    for unit in units:
        first = unit[0].block
        new_title = first.block_type == "title" and (heading is None or heading.block.block_id != first.block_id)
        if new_title or first.section != section:
            flush()
            section, heading = first.section, None
        if new_title:
            next_token = next(islice(iter_token_ranges(first.text), 100, None), None)
            end = next_token[0] if next_token is not None else len(first.text)
            heading = Span(first, 0, end, "context")
        if not pending and heading is not None and first.block_id != heading.block.block_id:
            pending = [heading]
        if pending and token_count(pending + unit) > options.size:
            overlap = flush()
            context = [heading] if heading is not None and first.block_id != heading.block.block_id else []
            # 结构块和标题上下文优先；overlap 是软目标，不挤占完整表格/公式或跨标题复用。
            pending = context + overlap if token_count(context + overlap + unit) <= options.size else context
        pending += unit
        if token_count(pending) > options.size:
            flush()
    flush()
    return result
