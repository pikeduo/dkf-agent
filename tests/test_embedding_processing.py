"""向量长度失败的纯Mock回归；不连接数据库、加载模型或写入向量文件。"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.embedding import processing
from app.services.embedding.service import DIMENSION, EmbeddingError, EmbeddingOptions


def test_later_batch_token_limit_identifies_chunk_without_publishing_partial_vectors(monkeypatch, tmp_path):
    """第二批超限映射到全局Chunk序号，保留原异常且不发布manifest或推进INDEXING。"""
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
    error = EmbeddingError("EMBEDDING_TOO_LONG", item_index=2, token_count=9001, limit_count=8192)
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
    [failure] = [call.kwargs for call in log.call_args_list if call.args == ("embedding.chunk_rejected",)]
    assert failure["doc_id"] == document.doc_id and failure["task_id"] == document.embedding_task_id
    assert failure["chunk_id"] == rows[3].chunk_id and failure["item_index"] == 4
    assert failure["token_count"] == 9001 and failure["limit_count"] == 8192
    assert failure["error_code"] == "EMBEDDING_TOO_LONG" and failure["stage"] == "embedding.token_limit"
