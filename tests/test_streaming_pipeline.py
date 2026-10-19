"""大文档分批切片/向量/索引回归，仅由用户执行；无真实文档、模型、数据库或云请求。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services import indexing
from app.services.chunk import processing as chunking, splitter, streaming
from app.services.embedding import artifacts, processing as embedding
from app.services.embedding.service import DIMENSION, MODEL_ID, VERSION as MODEL_VERSION, EmbeddingError, EmbeddingOptions
from test_chunk_splitter import assert_evidence, block


def test_v6_continues_past_aggregate_and_chunk_count_limits_without_losing_source(monkeypatch):
    """用缩小旧预算复现3601万字符的控制流；新版本继续分片而不是扩大整份上限。"""
    monkeypatch.setattr(splitter, "MAX_V4_SOURCE_CHARS", 100)
    monkeypatch.setattr(splitter, "MAX_CHUNKS", 2)
    sources = [block(1, "".join(f'项目{i}\t="公式{i}"\t' + "甲" * 100 + "\n" for i in range(60)), "table")]
    snapshot, doc_id, kb_id = deepcopy(sources), uuid4(), uuid4()
    drafts = list(streaming.iter_chunks(doc_id, kb_id, "xlsx", lambda: iter(sources), splitter.ChunkOptions()))
    assert len(drafts) > 2 and sources == snapshot
    assert [draft.metadata["chunk_index"] for draft in drafts] == list(range(1, len(drafts) + 1))
    assert all(draft.metadata["chunker_version"] == "block-chunk-v6" for draft in drafts)
    assert all(draft.metadata["estimated_token_count"] <= 600 for draft in drafts)
    assert_evidence(drafts, sources)
    assert drafts == list(streaming.iter_chunks(doc_id, kb_id, "xlsx", lambda: iter(sources), splitter.ChunkOptions()))
    with pytest.raises(splitter.ChunkError, match="CHUNK_LIMIT"):
        splitter.build_chunks(doc_id, kb_id, "xlsx", sources, splitter.ChunkOptions(version=splitter.STRONG_VERSION))


def test_v6_keeps_v5_text_formula_and_table_evidence_with_separate_version_identity():
    """新版本保留旧文本/公式/表行证据和输入Hash，仅配置版本与对应稳定Chunk ID变化。"""
    sources = [block(1, "章节", "title"), block(2, "甲" * 1300), block(3, "公式前解释"),
               block(4, "=SUM(A1:A10)", "formula"), block(5, "公式后解释"),
               block(6, "".join(f"项目{i}\t原值{i}\n" for i in range(80)), "table")]
    doc_id, kb_id = uuid4(), uuid4()
    old = splitter.build_chunks(doc_id, kb_id, "xlsx", sources, splitter.ChunkOptions(version=splitter.STRONG_VERSION))
    new = splitter.build_chunks(doc_id, kb_id, "xlsx", sources)
    assert [draft.text for draft in new] == [draft.text for draft in old]
    assert [draft.metadata["source_spans"] for draft in new] == [draft.metadata["source_spans"] for draft in old]
    assert new[0].metadata["input_blocks_hash"] == old[0].metadata["input_blocks_hash"]
    assert new[0].chunk_id != old[0].chunk_id
    assert_evidence(new, sources)


def test_v6_detects_source_change_between_hash_and_emission():
    """重复读取时任一原块内容改变都中止，不让分批策略绕过来源校验。"""
    original = [block(1, "甲" * 700)]
    calls = 0

    def factory():
        """仅第二遍更改内容，模拟不符合来源冻结约束的输入。"""
        nonlocal calls
        calls += 1
        yield original[0] if calls == 1 else block(1, "另一个来源")

    with pytest.raises(splitter.ChunkError, match="CHUNK_SOURCE_ORDER"):
        list(streaming.iter_chunks(uuid4(), uuid4(), "pdf", factory, splitter.ChunkOptions()))


def test_v6_preserves_blank_table_rows_without_emitting_empty_chunks():
    """原生表连续空白行仍逐字符保留，但不被单独生成为空向量输入。"""
    source = block(1, "项目\t原式\n" + "\t\n" * 1500 + "结尾\t=SUM(A1:A2)\n", "table")
    drafts = splitter.build_chunks(uuid4(), uuid4(), "xlsx", [source])
    assert all(draft.text.strip() for draft in drafts)
    assert_evidence(drafts, [source])


@pytest.mark.parametrize("fail_late", [False, True])
def test_chunk_persistence_uses_bounded_insert_batches_and_does_not_commit_partial_result(monkeypatch, fail_late):
    """模拟513片分三批写入；后批失败不推进EMBEDDING/提交，真实回滚另由DB用例验证。"""
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), file_type="xlsx", file_hash="a" * 64,
        status="CHUNKING", chunk_task_id=uuid4(), processing_task_id=uuid4(),
        parse_quality={"content": "PARTIAL", "missing_pages": [12]})
    session, batches = MagicMock(), []

    def drafts(*args):
        """只产生内存切片，按序验证消费者没有提前执行完整状态推进。"""
        for index in range(513):
            assert document.status == "CHUNKING"
            if fail_late and index == 257:
                raise splitter.ChunkError("CHUNK_SOURCE_ORDER：测试后批错误")
            yield SimpleNamespace(chunk_id=uuid4(), text="原字符", page_start=1, page_end=1,
                                  section="Sheet", metadata={"chunk_index": index + 1, "oversized": False})

    def execute(statement, values=None):
        """收集批次而不执行SQL；记录每批都是独立参数列表，质量元数据不丢失。"""
        if values is not None:
            batches.append(values)

    monkeypatch.setattr(streaming, "iter_chunks", drafts)
    session.execute.side_effect = execute
    if fail_late:
        with pytest.raises(splitter.ChunkError):
            chunking.stream_chunk_step(session, document, splitter.ChunkOptions(), 12, 36_009_492)
        assert [len(batch) for batch in batches] == [256] and document.status == "CHUNKING"
        session.flush.assert_not_called()
    else:
        result = chunking.stream_chunk_step(session, document, splitter.ChunkOptions(), 12, 36_009_492)
        assert [len(batch) for batch in batches] == [256, 256, 1]
        assert result["chunk_count"] == 513 and document.status == "EMBEDDING"
        assert all(row["chunk_metadata"]["parse_quality"] == document.parse_quality for batch in batches for row in batch)
    session.commit.assert_not_called()


def test_streamed_chunk_reader_does_not_fetch_all_text_or_reject_large_document():
    """复现36,009,492字符聚合；新版本直到迭代才按256行读取正文，顺序/代次每遍核对。"""
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), processing_task_id=uuid4(),
        chunk_config={"options": {"version": "block-chunk-v6"}})
    rows = [SimpleNamespace(chunk_id=uuid4(), text=f"原片{i}", chunk_index=i,
            source_parse_task_id=str(document.processing_task_id)) for i in range(1, 4)]
    aggregate, session = MagicMock(), MagicMock()
    aggregate.one.return_value = (3, 36_009_492)
    session.execute.side_effect = [aggregate, iter(rows), iter(rows)]
    current = embedding.read_chunks(session, document)
    assert isinstance(current, embedding.ChunkRows) and len(current) == 3 and session.execute.call_count == 1
    assert list(current) == rows and list(current) == rows
    assert session.execute.call_args.args[0].get_execution_options()["yield_per"] == 256


@pytest.mark.parametrize("damage", ["order", "generation", "missing", "empty"])
def test_streamed_chunk_reader_rejects_late_source_damage(damage):
    """分批读取不能放过乱序、旧代次、后批缺行或空正文，也不能伪装完整输入。"""
    document = SimpleNamespace(processing_task_id=uuid4())
    rows = [SimpleNamespace(text="原片", chunk_index=i, source_parse_task_id=str(document.processing_task_id))
            for i in (1, 2)]
    if damage == "order":
        rows[1].chunk_index = 1
    elif damage == "generation":
        rows[1].source_parse_task_id = str(uuid4())
    elif damage == "missing":
        rows.pop()
    else:
        rows[1].text = " "
    session = MagicMock()
    session.execute.return_value = iter(rows)
    with pytest.raises(EmbeddingError, match="EMBEDDING_SOURCE"):
        list(embedding.ChunkRows(session, document, MagicMock(), 2))


def test_streaming_hash_matches_frozen_json_and_batches_obey_each_call_budget(monkeypatch):
    """增量Hash与旧代次一致；达到单批字符限制就换批，而不是以整份累计字符拒绝。"""
    rows = [SimpleNamespace(chunk_id=uuid4(), chunk_index=i, text=value)
            for i, value in enumerate(("甲" * 7, "乙" * 4, "丙" * 3), 1)]
    expected = artifacts.digest_json([{"id": str(row.chunk_id), "index": row.chunk_index,
                                      "text_hash": artifacts.digest_json(row.text)} for row in rows])
    assert embedding.chunks_hash(iter(rows)) == expected
    monkeypatch.setattr(embedding, "MAX_TOTAL_CHARS", 10)
    batches = list(embedding.embedding_batches(iter(rows), 2))
    assert [len(batch) for batch in batches] == [1, 2]
    assert [row for batch in batches for row in batch] == rows


def test_mock_streamed_embedding_and_index_complete_all_batches_without_total_limits(monkeypatch, tmp_path):
    """纯Mock贯通分批GPU/本地产物/pgvector写入，缩小旧上限后仍逐片完整且保留PARTIAL。"""
    options = EmbeddingOptions(str(tmp_path / "unused-model"), batch_size=2)
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), processing_task_id=uuid4(), chunk_task_id=uuid4(),
        embedding_task_id=uuid4(), index_task_id=None, status="EMBEDDING", error_message=None,
        chunk_config={"options": {"version": "block-chunk-v6"}}, parse_quality={"content": "PARTIAL"})
    rows = [SimpleNamespace(chunk_id=uuid4(), chunk_index=i, text=f"原片{i}") for i in range(1, 6)]
    document.embedding_config = {"options": options.snapshot(), "source_hash": embedding.chunks_hash(rows),
        "source_parse_task_id": str(document.processing_task_id), "source_chunk_task_id": str(document.chunk_task_id),
        "output_dir": str(tmp_path / "vectors")}
    service, session, written = MagicMock(), MagicMock(), []
    session.scalar.return_value = document
    service.identity.return_value = {"model_name": MODEL_ID, "dimension": DIMENSION, "version": MODEL_VERSION,
        "model_fingerprint": "c" * 64, "normalized": True, "max_length": 8192, "precision": "fp16"}
    service.embed_documents.side_effect = lambda texts: [[1.0] + [0.0] * (DIMENSION - 1) for _ in texts]
    monkeypatch.setattr(embedding, "read_chunks", lambda *args: rows)
    monkeypatch.setattr(embedding, "get_embedding_service", lambda *args: service)
    monkeypatch.setattr(artifacts, "MAX_BYTES", 100)
    monkeypatch.setattr(indexing, "MAX_BYTES", 100)
    monkeypatch.setattr(indexing, "MAX_ITEMS", 2)
    result = embedding.run_embedding_step(session, document.doc_id, document.embedding_task_id)
    assert result["status"] == "awaiting_index" and document.status == "INDEXING"
    assert [len(call.args[0]) for call in service.embed_documents.call_args_list] == [2, 2, 1]
    document.index_task_id = uuid4()
    document.index_config = indexing.input_identity(document)
    monkeypatch.setattr(indexing, "read_chunks", lambda *args: rows)
    monkeypatch.setattr(indexing, "require_vector_index", MagicMock())
    complete = MagicMock()
    monkeypatch.setattr(indexing, "require_complete_vectors", complete)
    monkeypatch.setattr(indexing, "WRITE_BATCH_SIZE", 2)
    monkeypatch.setattr(indexing, "write_batch", lambda session, doc, batch: written.extend(batch))
    final = indexing.run_index_step(session, document.doc_id, document.index_task_id)
    assert final["status"] == "partial_ready" and document.status == "PARTIAL_READY"
    assert [str(value["_chunk_id"]) for value in written] == [str(row.chunk_id) for row in rows]
    complete.assert_called_once_with(session, document, len(rows))
    session.commit.assert_not_called()


@pytest.mark.parametrize("damage", ["checksum", "truncated", "order", "extra", "oversized"])
def test_v2_large_file_policy_still_validates_every_row_and_never_accepts_corruption(tmp_path, monkeypatch, damage):
    """总文件可超过旧上限不等于信任文件；每行、末尾、全量Hash和顺序仍必须匹配。"""
    import json

    store, doc_id, task_id = artifacts.ArtifactStore(tmp_path), uuid4(), uuid4()
    ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setattr(artifacts, "MAX_BYTES", 100)
    with store.writer(doc_id, task_id, streaming=True) as writer:
        for identifier in ids:
            writer.append(identifier, [1.0] + [0.0] * (DIMENSION - 1))
    manifest = {"format": "dense-jsonl-stream-v2", "doc_id": str(doc_id), "task_id": str(task_id),
        "source_hash": "a" * 64, "options_hash": "b" * 64, "chunk_count": len(ids),
        "model": {"model_name": MODEL_ID, "dimension": DIMENSION, "normalized": True,
                  "model_fingerprint": "c" * 64, "version": MODEL_VERSION},
        "size_bytes": writer.size_bytes, "vectors_sha256": writer.digest.hexdigest()}
    store.publish(doc_id, task_id, manifest)
    assert store.ready(doc_id, task_id, "a" * 64, "b" * 64, ids) == manifest
    path = store.directory(doc_id, task_id) / "vectors.jsonl"
    values = path.read_bytes().splitlines(keepends=True)
    if damage == "checksum":
        value = json.loads(values[-1])
        value["vector"] = [0.0, 1.0] + [0.0] * (DIMENSION - 2)
        values[-1] = json.dumps(value).encode() + b"\n"
    elif damage == "truncated":
        values.pop()
    elif damage == "order":
        values.reverse()
    elif damage == "oversized":
        values[-1] = b"x" * (artifacts.MAX_ROW_BYTES + 1)
    else:
        values.append(values[-1])
    path.write_bytes(b"".join(values))
    with pytest.raises(EmbeddingError, match="EMBEDDING_ARTIFACT"):
        store.ready(doc_id, task_id, "a" * 64, "b" * 64, ids)


def test_v2_late_failure_never_publishes_manifest_or_completed_vectors(tmp_path):
    """流式文件在后批失败时仅清理本次暂存，不留下成功标记或可被误用的半份产物。"""
    store, doc_id, task_id = artifacts.ArtifactStore(tmp_path), uuid4(), uuid4()
    with pytest.raises(EmbeddingError, match="EMBEDDING_TOO_LONG"):
        with store.writer(doc_id, task_id, streaming=True) as writer:
            writer.append(str(uuid4()), [1.0] + [0.0] * (DIMENSION - 1))
            raise EmbeddingError("EMBEDDING_TOO_LONG")
    assert store.ready(doc_id, task_id, "a" * 64, "b" * 64, []) is None
    assert list(store.directory(doc_id, task_id).iterdir()) == []
