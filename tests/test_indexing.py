"""用户执行的本地数值结果校验，不生成原始文档、不加载模型或访问数据库/云 API。"""

import json
from uuid import uuid4

import pytest

from app.services import indexing
from app.services.embedding.artifacts import ArtifactStore
from app.services.embedding.service import DIMENSION
from test_embedding_artifacts import publish


def test_valid_vectors_keep_values_and_order_in_bounded_batches(tmp_path, monkeypatch):
    """原向量不再归一化改写，批次有界且 Chunk 标识/顺序与文件保持一致。"""

    store, doc_id, task_id = ArtifactStore(tmp_path), uuid4(), uuid4()
    ids = [str(uuid4()) for index in range(5)]
    manifest = publish(store, doc_id, task_id, ids)
    monkeypatch.setattr(indexing, "WRITE_BATCH_SIZE", 2)
    batches = list(indexing.vector_batches(store, doc_id, task_id, manifest, ids))
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert [str(row["_chunk_id"]) for batch in batches for row in batch] == ids
    assert all(row["_vector"] == [1.0] + [0.0] * (DIMENSION - 1) for batch in batches for row in batch)


@pytest.mark.parametrize("vector", [None, [], [1.0], [0.0] * DIMENSION,
    [2.0] + [0.0] * (DIMENSION - 1), [float("nan")] + [0.0] * (DIMENSION - 1),
    [float("inf")] + [0.0] * (DIMENSION - 1), [True] + [0.0] * (DIMENSION - 1),
    ["PRIVATE"] + [0.0] * (DIMENSION - 1)])
def test_invalid_vectors_fail_without_normalizing_or_leaking_input(vector):
    """拒绝维度/数值/布尔值/零向量/非单位向量；安全错误不输出原始数值或文本。"""

    with pytest.raises(indexing.IndexingError, match="INDEX_VECTOR") as error:
        indexing.checked_vector(vector)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("change", ["checksum", "order", "count", "missing", "extra", "oversized"])
def test_changed_file_between_validation_and_ingestion_fails_safely(tmp_path, change):
    """预检后文件损坏/缺失/顺序改变必须失败，不能靠已验证 manifest 跳过读取校验。"""

    store, doc_id, task_id = ArtifactStore(tmp_path), uuid4(), uuid4()
    ids = [str(uuid4()), str(uuid4())]
    manifest = publish(store, doc_id, task_id, ids)
    path = store.directory(doc_id, task_id) / "vectors.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if change == "missing":
        path.unlink()
    elif change == "oversized":
        path.write_bytes(b"x" * (128 * 1024 + 1))
    else:
        if change == "checksum":
            rows[0]["vector"] = [0.0, 1.0] + [0.0] * (DIMENSION - 2)
        elif change == "order":
            rows.reverse()
        elif change == "count":
            rows.pop()
        elif change == "extra":
            rows[0]["unexpected"] = "PRIVATE"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(indexing.IndexingError, match="INDEX_ARTIFACT") as error:
        list(indexing.vector_batches(store, doc_id, task_id, manifest, ids))
    assert "PRIVATE" not in str(error.value) and str(tmp_path) not in str(error.value)
