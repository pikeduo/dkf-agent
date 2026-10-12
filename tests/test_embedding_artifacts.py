"""用户运行时才生成临时向量结果，验证原子发布和校验，不创建文档样本或加载模型。"""

import json
from uuid import uuid4
import pytest

from app.services.embedding.artifacts import ArtifactStore
from app.services.embedding.service import DIMENSION, MODEL_ID, VERSION, EmbeddingError


def publish(store, doc_id, task_id, ids):
    """写入小型纯数值 Stub，完整 manifest 的模型身份不代表真实 BGE-M3 已验证。"""

    with store.writer(doc_id, task_id) as writer:
        for identifier in ids:
            writer.append(identifier, [1.0] + [0.0] * (DIMENSION - 1))
    manifest = {"format": "dense-jsonl-v1", "doc_id": str(doc_id), "task_id": str(task_id),
        "source_hash": "a" * 64, "options_hash": "b" * 64, "chunk_count": len(ids),
        "model": {"model_name": MODEL_ID, "dimension": DIMENSION, "normalized": True,
                  "model_fingerprint": "c" * 64, "version": VERSION},
        "size_bytes": writer.size_bytes, "vectors_sha256": writer.digest.hexdigest()}
    store.publish(doc_id, task_id, manifest)
    return manifest


def test_all_vectors_and_manifest_publish_atomically(tmp_path):
    """仅在完整结果发布后命中，流式读取按输入 ID/序号校验，不乱序、不丢尾部。"""

    store, doc_id, task_id = ArtifactStore(tmp_path), uuid4(), uuid4()
    ids = [str(uuid4()), str(uuid4())]
    assert store.ready(doc_id, task_id, "a" * 64, "b" * 64, ids) is None
    expected = publish(store, doc_id, task_id, ids)
    assert store.ready(doc_id, task_id, "a" * 64, "b" * 64, ids) == expected
    directory = store.directory(doc_id, task_id)
    assert {file.name for file in directory.iterdir()} == {"vectors.jsonl", "manifest.json"}
    rows = [json.loads(line) for line in (directory / "vectors.jsonl").read_text().splitlines()]
    assert [row["chunk_id"] for row in rows] == ids


def test_failed_batch_never_publishes_complete_result(tmp_path):
    """第二批失败保留原始异常供安全上层处理，移除临时文件，不留下成功 manifest。"""

    store, doc_id, task_id = ArtifactStore(tmp_path), uuid4(), uuid4()
    with pytest.raises(EmbeddingError), store.writer(doc_id, task_id) as writer:
        writer.append(str(uuid4()), [1.0] + [0.0] * (DIMENSION - 1))
        raise EmbeddingError("EMBEDDING_TOO_LONG")
    assert store.ready(doc_id, task_id, "a" * 64, "b" * 64, []) is None
    assert list(store.directory(doc_id, task_id).iterdir()) == []


@pytest.mark.parametrize("change", ["file", "order", "source", "options", "dimension", "missing"])
def test_corrupted_or_mismatched_artifact_is_not_reencoded_silently(tmp_path, change):
    """校验和、顺序、来源、参数、维度或文件缺失明确报错，不能当作普通 miss。"""

    store, doc_id, task_id = ArtifactStore(tmp_path), uuid4(), uuid4()
    ids = [str(uuid4()), str(uuid4())]
    manifest = publish(store, doc_id, task_id, ids)
    directory = store.directory(doc_id, task_id)
    source, options = "a" * 64, "b" * 64
    if change == "file":
        with (directory / "vectors.jsonl").open("ab") as stream:
            stream.write(b"broken\n")
    elif change == "order":
        ids.reverse()
    elif change == "source":
        source = "d" * 64
    elif change == "options":
        options = "d" * 64
    elif change == "dimension":
        manifest["model"]["dimension"] = 768
        store.publish(doc_id, task_id, manifest)
    elif change == "missing":
        (directory / "vectors.jsonl").unlink()
    with pytest.raises(EmbeddingError, match="EMBEDDING_ARTIFACT"):
        store.ready(doc_id, task_id, source, options, ids)
