"""v6逐块切片：批量落库而非以整份大小拒绝，原字符与来源保持完整。"""

import csv
import hashlib
import json
from io import StringIO
from itertools import islice
from uuid import uuid5

from app.core.business_logging import log_event
from app.services.chunk.splitter import (
    ChunkDraft, ChunkError, Span, TOKENIZER, VERSION, iter_token_ranges, table_units,
    tail_overlap, text_spans, token_count,
)


def source_record(block):
    """生成与旧算法相同的原块身份，不混入代次、时间或派生文本。"""
    value = {"id": str(block.block_id), "index": block.block_index, "page": block.page,
             "section": block.section, "type": block.block_type, "text": block.text,
             "source": block.source, "bbox": block.bbox}
    if block.provenance is not None:
        value["provenance"] = block.provenance
    return value


def source_identity(factory):
    """首遍逐块核对全量顺序并计算规范Hash，只保留块标识/摘要，不收集整份正文。"""
    digest, hashes, nonempty = hashlib.sha256(), {}, False
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True)
    digest.update(b"[")
    for index, block in enumerate(factory(), 1):
        if (block.block_id in hashes or type(block.block_index) is not int or block.block_index != index
                or block.page < 1 or block.block_type not in {"title", "text", "table", "formula", "image_text"}):
            raise ChunkError("CHUNK_SOURCE_ORDER：解析块身份或阅读序号不可用")
        if index > 1:
            digest.update(b", ")
        record_hash = hashlib.sha256()
        for part in encoder.iterencode(source_record(block)):
            encoded = part.encode()
            digest.update(encoded)
            record_hash.update(encoded)
        hashes[block.block_id] = (hashlib.sha256(block.text.encode()).hexdigest(), record_hash.hexdigest())
        nonempty = nonempty or bool(block.text.strip())
    if not nonempty:
        raise ChunkError("CHUNK_EMPTY：没有可切片正文")
    digest.update(b"]")
    return digest.hexdigest(), hashes


def native_rows(block, budget):
    """两遍验证带引号TSV后逐组输出，不保留全部行，也不因总行数/总字符数拒绝。"""
    width, count = None, 0
    try:
        for row in csv.reader(StringIO(block.text, newline=""), delimiter="\t", strict=True):
            width = len(row) if width is None else width
            if width < 2 or len(row) != width:
                raise csv.Error
            count += 1
    except csv.Error:
        # 无法证明TSV边界时不能猜切；整块保留，真实Tokenizer继续执行安全校验。
        yield [Span(block, 0, len(block.text))]
        return
    if not count:
        yield [Span(block, 0, len(block.text))]
        return
    stream = StringIO(block.text, newline="")
    first = last = cost = emitted = 0
    has_text = False
    for row in csv.reader(stream, delimiter="\t", strict=True):
        end = stream.tell()
        current = token_count([Span(block, last, end)])
        row_has_text = bool(block.text[last:end].strip())
        # 连续空行须归入相邻有内容的行组，不能制造不可向量化的空白Chunk或丢掉原空白。
        if last > first and has_text and row_has_text and (cost + current > budget or end - first > budget * 4):
            yield [Span(block, first, last)]
            emitted += 1
            first, cost, has_text = last, 0, False
        has_text = has_text or row_has_text
        last, cost = end, cost + current
    if last != len(block.text):
        raise ChunkError("CHUNK_SOURCE_ORDER：原生表格字符边界不完整")
    yield [Span(block, first, last)]
    if emitted:
        log_event("chunk.table_split", block_id=block.block_id, page=block.page,
                  stage="chunk.native_table_rows", chunk_count=emitted + 1)


def atoms(factory, file_type, budget, hashes):
    """次遍逐块消费并复核Hash；正文/原生表/HTML表仍使用各自的真实边界。"""
    seen = 0
    for block in factory():
        record_hash = hashlib.sha256(json.dumps(source_record(block), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if block.block_id not in hashes or hashes[block.block_id][1] != record_hash or block.block_index != seen + 1:
            raise ChunkError("CHUNK_SOURCE_ORDER：切片期间来源内容发生变化")
        seen += 1
        if not block.text.strip():
            continue
        if block.block_type == "table" and block.source == "native_parser" and file_type in {"xlsx", "csv"}:
            yield from native_rows(block, budget)
        elif block.block_type == "table":
            yield from table_units(block, budget, allow_surrounding=True, large_tables=True, cell_strong=True)
        elif block.block_type == "formula":
            yield [Span(block, 0, len(block.text))]
        else:
            for span in text_spans(block, budget):
                yield [span]
    if seen != len(hashes):
        raise ChunkError("CHUNK_SOURCE_ORDER：切片期间来源块数量发生变化")


def formula_groups(values, budget):
    """只保留一个前项和后项，将公式与邻近解释组合，不物化全篇原子列表。"""
    iterator, previous = iter(values), None
    current = next(iterator, None)
    while current is not None:
        unit = list(current)
        following = next(iterator, None)
        if unit[0].block.block_type == "formula":
            if (previous and len(previous) == 1 and previous[0].block.block_type in {"text", "image_text"}
                    and previous[0].block.section == unit[0].block.section
                    and token_count(previous + unit) <= budget):
                unit, previous = previous + unit, None
            if (following and following[0].block.block_type in {"text", "image_text"}
                    and following[0].block.section == unit[-1].block.section
                    and token_count(unit + following) <= budget):
                unit += following
                following = next(iterator, None)
        if previous is not None:
            yield previous
        previous, current = unit, following
    if previous is not None:
        yield previous


def draft_for(doc_id, kb_id, file_type, pending, section, index, config, input_hash, hashes):
    """为一片生成精确字符证据和稳定ID；完整来源Hash不随分批边界改变。"""
    parts, evidence, offset = [], [], 0
    for span in pending:
        if parts:
            offset += 2
        value = span.text
        parts.append(value)
        evidence.append({"block_id": str(span.block.block_id), "block_index": span.block.block_index,
            "page": span.block.page, "block_type": span.block.block_type, "source": span.block.source,
            "bbox": span.block.bbox, "start_char": span.start, "end_char": span.end,
            "chunk_start_char": offset, "chunk_end_char": offset + len(value), "role": span.role,
            "block_text_hash": hashes[span.block.block_id][0]})
        if span.block.provenance is not None:
            evidence[-1].update(provenance=span.block.provenance,
                                page_reliable=span.block.provenance.get("page_reliable", False))
        offset += len(value)
    text, estimated = "\n\n".join(parts), token_count(pending)
    metadata = {"chunk_index": index, "chunker_version": VERSION, "tokenizer": TOKENIZER,
        "config": config, "input_blocks_hash": input_hash, "estimated_token_count": estimated,
        "oversized": estimated > config["size"], "source_spans": evidence,
        "page_semantics": "physical_pdf" if file_type == "pdf" else
                          "image_page" if file_type in {"jpg", "jpeg", "png"} else "logical"}
    identity = json.dumps({"metadata": metadata, "text": text, "kb_id": str(kb_id)}, ensure_ascii=False, sort_keys=True)
    pages = [span.block.page for span in pending]
    return ChunkDraft(uuid5(doc_id, identity), kb_id, doc_id, text, min(pages), max(pages), section, metadata)


def iter_chunks(doc_id, kb_id, file_type, factory, options):
    """用可重复读取的原块工厂逐片输出；不截断总文档、不限制最终Chunk数，由调用方原子落库。"""
    config = options.snapshot()
    if options.version != VERSION:
        raise ChunkError("CHUNK_CONFIG_INVALID：流式入口仅支持v6")
    input_hash, hashes = source_identity(factory)
    pending, heading, section, index = [], None, None, 0
    units = formula_groups(atoms(factory, file_type, options.size - 100, hashes), options.size - 100)

    def flush():
        """只生成当前一片及小型重叠，调用方成功消费后立即释放，不累积全篇Chunk。"""
        nonlocal pending, index
        if not pending or not any(span.role == "content" for span in pending):
            pending = []
            return None, []
        index += 1
        draft = draft_for(doc_id, kb_id, file_type, pending, section, index, config, input_hash, hashes)
        overlap = tail_overlap(pending, options.overlap)
        pending = []
        return draft, overlap

    for unit in units:
        first = unit[0].block
        new_title = first.block_type == "title" and (heading is None or heading.block.block_id != first.block_id)
        if new_title or first.section != section:
            draft, _ = flush()
            if draft is not None:
                yield draft
            section, heading = first.section, None
        if new_title:
            next_token = next(islice(iter_token_ranges(first.text), 100, None), None)
            heading = Span(first, 0, next_token[0] if next_token is not None else len(first.text), "context")
        if not pending and heading is not None and first.block_id != heading.block.block_id:
            pending = [heading]
        if pending and token_count(pending + unit) > options.size:
            draft, overlap = flush()
            if draft is not None:
                yield draft
            context = [heading] if heading is not None and first.block_id != heading.block.block_id else []
            pending = context + overlap if token_count(context + overlap + unit) <= options.size else context
        pending += unit
        if token_count(pending) > options.size:
            draft, _ = flush()
            if draft is not None:
                yield draft
    draft, _ = flush()
    if draft is not None:
        yield draft
