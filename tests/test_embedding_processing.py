"""向量长度失败的纯Mock回归；不连接数据库、加载模型或写入向量文件。"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.embedding import processing
from app.services.embedding.service import DIMENSION, EmbeddingError, EmbeddingOptions


@pytest.mark.parametrize("count,chars", [(15160, 11273580), (12992, 14965690), (20000, 16000000)])
def test_document_budget_accepts_large_aggregate_before_reading_current_chunk_rows(monkeypatch, count, chars):
    """复现两份大表的SQL聚合计数；文档预算区别于单批预算，不读取实际文档或加载模型。"""
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), processing_task_id=uuid4())
    row = SimpleNamespace(chunk_index=1, source_parse_task_id=str(document.processing_task_id), text="内存切片")
    aggregate, ordered = MagicMock(), MagicMock()
    aggregate.one.return_value = (count, chars)
    ordered.all.return_value = [row]
    session = MagicMock()
    session.execute.side_effect = [aggregate, ordered]
    log = MagicMock()
    monkeypatch.setattr(processing, "log_event", log)
    assert processing.read_chunks(session, document) == [row]
    assert session.execute.call_count == 2
    log.assert_not_called()


@pytest.mark.parametrize("count,chars,step,limit", [
    (1, 16000001, "TOTAL_CHAR_LIMIT", 16000000),
    (20001, 1, "CHUNK_COUNT_LIMIT", 20000),
])
def test_document_budget_rejects_before_fetching_text_and_logs_only_safe_counts(monkeypatch, count, chars, step, limit):
    """文档边界仍有限；越界不加载正文/模型，只记录实际计数和固定失败阶段。"""
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4())
    session, log = MagicMock(), MagicMock()
    session.execute.return_value.one.return_value = (count, chars)
    monkeypatch.setattr(processing, "log_event", log)
    with pytest.raises(EmbeddingError) as failure:
        processing.read_chunks(session, document)
    assert failure.value.code == "EMBEDDING_LIMIT" and session.execute.call_count == 1
    assert log.call_args.args == ("embedding.input_rejected",)
    assert log.call_args.kwargs["stage"] == "embedding.document_budget"
    assert log.call_args.kwargs["step"] == step and log.call_args.kwargs["limit_count"] == limit


def test_large_document_budget_keeps_source_generation_and_order_checks():
    """扩大聚合预算仍不放行其他解析代次的Chunk，不因字符量合规跳过来源检查。"""
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), processing_task_id=uuid4())
    row = SimpleNamespace(chunk_index=1, source_parse_task_id=str(uuid4()), text="内存切片")
    aggregate, ordered = MagicMock(), MagicMock()
    aggregate.one.return_value = (12992, 14965690)
    ordered.all.return_value = [row]
    session = MagicMock()
    session.execute.side_effect = [aggregate, ordered]
    with pytest.raises(EmbeddingError) as failure:
        processing.read_chunks(session, document)
    assert failure.value.code == "EMBEDDING_SOURCE"


@pytest.mark.parametrize("code", ["EMBEDDING_TOO_LONG", "EMBEDDING_LIMIT"])
def test_later_batch_limit_identifies_chunk_without_publishing_partial_vectors(monkeypatch, tmp_path, code):
    """第二批Token或字符超限映射全局Chunk序号，不发布manifest或推进INDEXING。"""
    options = EmbeddingOptions(str(tmp_path / "unused-model"), batch_size=2)
    rows = [SimpleNamespace(chunk_id=uuid4(), chunk_index=index, text=f"已有切片{index}")
            for index in range(1, 5)]
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), status="EMBEDDING",
                               processing_task_id=uuid4(), chunk_task_id=uuid4(), embedding_task_id=uuid4())
    config = {"options": options.snapshot(), "source_hash": processing.chunks_hash(rows),
              "source_parse_task_id": str(document.processing_task_id),
              "source_chunk_task_id": str(document.chunk_task_id), "output_dir": str(tmp_path / "unused-output")}
    document.embedding_config = config
    session, store, service, log = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    session.scalar.return_value = document
    store.ready.return_value = None
    writer = store.writer.return_value.__enter__.return_value
    error = (EmbeddingError(code, item_index=2, token_count=9001, limit_count=8192)
             if code == "EMBEDDING_TOO_LONG" else
             EmbeddingError(code, item_index=2, item_count=1_000_001, limit_count=1_000_000, step="TEXT_CHAR_LIMIT"))
    service.embed_documents.side_effect = [[[1.0] + [0.0] * (DIMENSION - 1)] * 2, error]

    def memory_rows(*args):
        """仅返回原序内存Chunk，避免读取业务数据库。"""
        return rows

    monkeypatch.setattr(processing, "read_chunks", memory_rows)
    monkeypatch.setattr(processing, "ArtifactStore", MagicMock(return_value=store))
    monkeypatch.setattr(processing, "get_embedding_service", MagicMock(return_value=service))
    monkeypatch.setattr(processing, "log_event", log)

    with pytest.raises(EmbeddingError) as caught:
        processing.run_embedding_step(session, document.doc_id, document.embedding_task_id)

    assert caught.value is error
    assert writer.append.call_count == 2 and service.embed_documents.call_count == 2
    store.publish.assert_not_called()
    assert store.ready.call_count == 1 and document.status == "EMBEDDING"
    assert document.embedding_config is config and "result" not in config
    session.flush.assert_not_called()
    event = "embedding.chunk_rejected" if code == "EMBEDDING_TOO_LONG" else "embedding.chunk_resource_rejected"
    [failure] = [call.kwargs for call in log.call_args_list if call.args == (event,)]
    assert failure["doc_id"] == document.doc_id and failure["task_id"] == document.embedding_task_id
    assert failure["chunk_id"] == rows[3].chunk_id and failure["item_index"] == 4
    assert failure["error_code"] == code
    assert failure["limit_count"] == (8192 if code == "EMBEDDING_TOO_LONG" else 1_000_000)
    if code == "EMBEDDING_TOO_LONG":
        assert failure["token_count"] == 9001 and failure["stage"] == "embedding.token_limit"
    else:
        assert failure["item_count"] == 1_000_001 and failure["step"] == "TEXT_CHAR_LIMIT"
        assert failure["stage"] == "embedding.input_budget" and "token_count" not in failure


@pytest.mark.parametrize("version", ["block-chunk-v6", "block-chunk-v7"])
def test_late_giant_chunk_is_rejected_during_input_hash_before_any_gpu_or_artifact(monkeypatch, tmp_path, version):
    """复现前5608片正常、随后巨片的控制流；首次全量Hash预检即定位，不先计算数千批。"""
    options = EmbeddingOptions(str(tmp_path / "unused-model"), batch_size=2)
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), status="EMBEDDING",
        processing_task_id=uuid4(), chunk_task_id=uuid4(), embedding_task_id=uuid4(),
        chunk_config={"options": {"version": version}})
    document.embedding_config = {"options": options.snapshot(), "source_hash": "a" * 64,
        "source_parse_task_id": str(document.processing_task_id), "source_chunk_task_id": str(document.chunk_task_id),
        "output_dir": str(tmp_path / "unused-vectors")}
    rows = [SimpleNamespace(chunk_id=uuid4(), chunk_index=i, source_parse_task_id=str(document.processing_task_id),
                            text="正常片" if i < 4 else "甲" * 11) for i in range(1, 5)]
    session, aggregate, service, store, log = MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
    session.scalar.return_value = document
    aggregate.one.return_value = (4, 36_009_492)
    session.execute.side_effect = [aggregate, iter(rows)]
    monkeypatch.setattr(processing, "MAX_TEXT_CHARS", 10)
    monkeypatch.setattr(processing, "get_embedding_service", service)
    monkeypatch.setattr(processing, "ArtifactStore", store)
    monkeypatch.setattr(processing, "log_event", log)
    with pytest.raises(EmbeddingError, match="EMBEDDING_LIMIT") as failure:
        processing.run_embedding_step(session, document.doc_id, document.embedding_task_id)
    service.assert_not_called()
    store.assert_not_called()
    session.flush.assert_not_called()
    assert document.status == "EMBEDDING" and "result" not in document.embedding_config
    assert failure.value.diagnostic == {"item_index": 4, "item_count": 11, "limit_count": 10, "step": "TEXT_CHAR_LIMIT"}
    assert log.call_args.args == ("embedding.chunk_resource_rejected",)
    assert log.call_args.kwargs["chunk_id"] == rows[-1].chunk_id
    assert log.call_args.kwargs["stage"] == "embedding.input_budget"
