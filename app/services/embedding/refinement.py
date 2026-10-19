"""真实Token预检及保留字符证据的本地重切；不解析原件、不调用云端、不编码向量。"""

from dataclasses import replace
from itertools import groupby
from uuid import uuid5

from sqlalchemy import delete, insert, select

from app.core.business_logging import log_event
from app.models import DocumentBlock, DocumentChunk
from app.services.chunk.processing import checkpoint_options, source_generation
from app.services.chunk.splitter import ChunkError, ChunkOptions, SourceBlock, Span, token_count
from app.services.chunk.streaming import iter_chunks, native_rows, source_identity
from app.services.embedding.artifacts import digest_json
from app.services.embedding.service import MAX_TEXT_CHARS, MAX_TOTAL_CHARS, EmbeddingError

POLICY = "embedding-token-refinement-v1"


def first_oversized(rows, service, max_length, *, batch_size=16):
    """编码前按冻结批大小/字符预算测量；巨片不送Tokenizer，返回首个需要重切的Chunk。"""
    batch, chars = [], 0

    def measure():
        """一次预检有界批次；保持全局原序，不以估算值替代真实Token。"""
        if batch:
            counts = service.token_counts([row.text for row in batch])
            if len(counts) != len(batch):
                raise EmbeddingError("EMBEDDING_OUTPUT")
            for row, count in zip(batch, counts):
                if count > max_length:
                    return row, "TOKEN_LIMIT", count, max_length
        return None

    for row in rows:
        if len(row.text) > MAX_TEXT_CHARS:
            earlier = measure()
            if earlier is not None:
                return earlier
            return row, "TEXT_CHAR_LIMIT", len(row.text), MAX_TEXT_CHARS
        if batch and (len(batch) >= batch_size or chars + len(row.text) > MAX_TOTAL_CHARS):
            oversized = measure()
            if oversized is not None:
                return oversized
            batch, chars = [], 0
        batch.append(row)
        chars += len(row.text)
    return measure()


def fits(text, service, max_length):
    """先限制单片字符量，再真实计Token；纯空白不能生成独立向量输入。"""
    return (bool(text.strip()) and len(text) <= MAX_TEXT_CHARS
            and service.token_counts([text])[0] <= max_length)


def child_draft(parent, fragments, index, recipe):
    """区间映射回原Block；纯重复上下文返回None，任何主内容及其来源Hash均不丢弃。"""
    parts, spans, offset = [], [], 0
    for start, end, role in fragments:
        if parts:
            offset += 2
        parts.append(parent.text[start:end])
        for original in parent.metadata["source_spans"]:
            left = max(start, original["chunk_start_char"])
            right = min(end, original["chunk_end_char"])
            if right <= left:
                continue
            source_start = original["start_char"] + left - original["chunk_start_char"]
            spans.append(original | {"start_char": source_start, "end_char": source_start + right - left,
                "chunk_start_char": offset + left - start, "chunk_end_char": offset + right - start,
                "role": original["role"] if role == "content" else "context"})
        offset += end - start
    text = "\n\n".join(parts)
    if not spans:
        raise EmbeddingError("EMBEDDING_SOURCE")
    if not any(span["role"] == "content" for span in spans):
        return None
    metadata = parent.metadata | {"chunk_index": index, "source_spans": spans,
        "token_refinement": recipe, "estimated_token_count": token_count([
            Span(SourceBlock(parent.chunk_id, 1, parent.page_start, parent.section, "text", text, ""), 0, len(text))])}
    metadata["oversized"] = metadata["estimated_token_count"] > metadata["config"]["size"]
    identity = {"parent": str(parent.chunk_id), "text": text, "metadata": metadata, "kb_id": str(parent.kb_id)}
    pages = [span["page"] for span in spans]
    return replace(parent, chunk_id=uuid5(parent.doc_id, digest_json(identity)), text=text,
                   page_start=min(pages), page_end=max(pages), metadata=metadata)


def fragments_for(parent, service, max_length):
    """超长正文二分至合规；HTML/TSV仅切完整行组，公式和无法证明的结构不猜切。"""
    for _, values in groupby(parent.metadata["source_spans"], key=lambda span: span["block_id"]):
        group = list(values)
        if not any(span["role"] == "content" for span in group):
            # 标题/重叠等纯重复上下文可省略；原始主内容仍在其所属切片中。
            continue
        first, last = group[0]["chunk_start_char"], group[-1]["chunk_end_char"]
        kind = group[0]["block_type"]
        block = SourceBlock(parent.chunk_id, 1, parent.page_start, parent.section,
                            kind, parent.text[first:last], group[0]["source"])
        if kind == "table":
            if block.source == "native_parser" and parent.metadata["page_semantics"] == "logical":
                units = native_rows(block, 1)
            else:
                from app.services.chunk.html_rows import html_rows
                units = html_rows(block, 1)
            for unit in units:
                yield [(first + span.start, first + span.end, span.role) for span in unit], "TABLE_ROW_GROUP"
        elif kind == "formula":
            yield [(first, last, "content")], "FORMULA"
        else:
            pending = [(first, last)]
            while pending:
                start, end = pending.pop()
                text = parent.text[start:end]
                if fits(text, service, max_length):
                    yield [(start, end, "content")], "TEXT_CHARACTER"
                elif end - start <= 1 or not text.strip():
                    yield [(start, end, "content")], "TEXT_CHARACTER"
                else:
                    middle = (start + end) // 2
                    # 不把空白单独成片；边界旁的空白归到包含原文的一侧，字符不丢失。
                    left_text = start + len(text) - len(text.lstrip())
                    right_text = start + len(text.rstrip())
                    middle = max(left_text + 1, min(middle, right_text - 1))
                    if middle >= end or not parent.text[start:middle].strip() or not parent.text[middle:end].strip():
                        yield [(start, end, "content")], "TEXT_CHARACTER"
                        continue
                    pending.extend([(middle, end), (start, middle)])


def refine_drafts(drafts, service, identity):
    """每个最终片均经真实Tokenizer复验；算法/模型身份进入稳定ID，不循环重算向量。"""
    recipe = {"version": POLICY, "model_fingerprint": identity["model_fingerprint"],
              "max_length": identity["max_length"]}
    index = 0
    for parent in drafts:
        if fits(parent.text, service, identity["max_length"]):
            units = [([(0, len(parent.text), "content")], None)]
        else:
            units = fragments_for(parent, service, identity["max_length"])
        for fragments, reason in units:
            child = child_draft(parent, fragments, index + 1, recipe)
            if child is None:
                continue
            if not fits(child.text, service, identity["max_length"]):
                log_event("embedding.rechunk_blocked", doc_id=parent.doc_id, chunk_id=parent.chunk_id,
                    stage="embedding.rechunk", step=reason or "TEXT_CHARACTER",
                    error_code="EMBEDDING_UNSPLITTABLE", limit_count=identity["max_length"],
                    page=child.page_start)
                raise EmbeddingError("EMBEDDING_UNSPLITTABLE")
            index += 1
            yield child


def rebuild_chunks(session, document, service, identity):
    """文档行锁内重建当前Block的切片；核对旧输入Hash，256片暂存，失败由外层全回滚。"""
    try:
        previous = checkpoint_options(document)
    except ChunkError:
        raise EmbeddingError("EMBEDDING_CHECKPOINT") from None
    options = ChunkOptions(previous.size, previous.overlap)

    def factory():
        """逐块读取当前文档，保留原Block/页码/公式与来源；不读原文件或缓存。"""
        query = select(DocumentBlock).where(DocumentBlock.doc_id == document.doc_id).order_by(
            DocumentBlock.block_index.asc().nulls_last()).execution_options(yield_per=1)
        for block in session.scalars(query):
            yield SourceBlock(block.block_id, block.block_index, block.page, block.section,
                block.block_type, block.text, block.source, block.bbox, getattr(block, "provenance", None))

    try:
        input_hash, _ = source_identity(factory)
        old_hashes = set(session.scalars(select(DocumentChunk.chunk_metadata["input_blocks_hash"].as_string())
            .where(DocumentChunk.doc_id == document.doc_id, DocumentChunk.kb_id == document.kb_id).distinct()))
        if old_hashes != {input_hash}:
            raise EmbeddingError("EMBEDDING_SOURCE")
        # 先证明新旧输入一致，再替换Chunk；原始Block和parse_quality始终不写。
        session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == document.doc_id,
                                                   DocumentChunk.kb_id == document.kb_id))
        batch, count = [], 0
        drafts = iter_chunks(document.doc_id, document.kb_id, document.file_type, factory, options)
        for draft in refine_drafts(drafts, service, identity):
            metadata = draft.metadata | {"source_parse_task_id": source_generation(document),
                                         "file_hash": document.file_hash}
            if getattr(document, "parse_quality", None):
                metadata["parse_quality"] = document.parse_quality
            batch.append({"chunk_id": draft.chunk_id, "doc_id": document.doc_id, "kb_id": document.kb_id,
                "text": draft.text, "page_start": draft.page_start, "page_end": draft.page_end,
                "section": draft.section, "chunk_metadata": metadata, "embedding": None})
            count += 1
            if len(batch) == 256:
                session.execute(insert(DocumentChunk), batch)
                batch = []
        if batch:
            session.execute(insert(DocumentChunk), batch)
    except ChunkError:
        raise EmbeddingError("EMBEDDING_SOURCE") from None
    if not count:
        raise EmbeddingError("EMBEDDING_EMPTY")
    document.chunk_config = {"options": options.snapshot(), "source_parse_task_id": source_generation(document)}
    return count
