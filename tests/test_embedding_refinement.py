"""Token自适应重切的定向回归；仅编写，由用户运行，无真实模型/云端或业务数据库。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.chunk.splitter import ChunkOptions, build_chunks
from app.services.embedding import processing, refinement
from app.services.embedding.service import EmbeddingError, EmbeddingOptions
from test_chunk_splitter import assert_evidence, block


class TokenStub:
    """以字符数加特殊Token模拟真实计量；不加载模型或执行任何向量计算。"""

    def token_counts(self, texts):
        """包含两个特殊Token，令用例可独立检查边界和输入原序。"""
        return [len(text) + 2 for text in texts]


def identity(limit=96):
    """返回固定本地模型摘要，不包含模型路径或秘密信息。"""
    return {"model_fingerprint": "f" * 64, "max_length": limit}


@pytest.mark.parametrize("file_type", ["pdf", "xlsx"])
def test_plain_text_refines_to_exact_limit_without_losing_source_or_quality(file_type):
    """正文连续细分且特殊Token计入上限；ID稳定、页码/来源/原字完全可反查。"""
    sources = [block(1, "标题", "title", page=2), block(2, "甲乙丙丁" * 180 + "\n最后一字", page=3)]
    sources[1] = replace(sources[1], provenance={"origin": "preproc", "pointer": "layout/1",
        "raw_sha256": "a" * 64, "page_reliable": True, "bbox_reliable": False})
    original = deepcopy(sources)
    drafts = build_chunks(uuid4(), uuid4(), file_type, sources)
    repaired = list(refinement.refine_drafts(drafts, TokenStub(), identity()))
    assert all(len(draft.text) + 2 <= 96 for draft in repaired)
    assert [draft.metadata["chunk_index"] for draft in repaired] == list(range(1, len(repaired) + 1))
    assert all(draft.metadata["token_refinement"]["version"] == refinement.POLICY for draft in repaired)
    assert repaired == list(refinement.refine_drafts(drafts, TokenStub(), identity()))
    assert sources == original
    assert all(span["provenance"] == sources[1].provenance for draft in repaired
        for span in draft.metadata["source_spans"] if span["block_id"] == str(sources[1].block_id))
    assert_evidence(repaired, sources)


@pytest.mark.parametrize("native", [False, True])
def test_complete_table_rows_keep_cells_quotes_and_original_character_coverage(native):
    """HTML/带引号TSV按完整行组拆分；表壳上下文和原始单元格字符保持完整。"""
    text = ("字段\t原值\n" + "".join(f'项目{i}\t"原式{i}\n第二行"\n' for i in range(15)) if native else
            "表题<table>" + "".join(f"<tr><td>项目{i}</td><td>原值{i}</td></tr>" for i in range(15)) + "</table>表注")
    sources = [block(1, text, "table", page=7)]
    drafts = build_chunks(uuid4(), uuid4(), "xlsx" if native else "pdf", sources)
    repaired = list(refinement.refine_drafts(drafts, TokenStub(), identity(128)))
    assert len(repaired) > 1 and all(len(draft.text) + 2 <= 128 for draft in repaired)
    if native:
        assert all('"原式' not in draft.text or '第二行"' in draft.text for draft in repaired)
    else:
        assert any("<table>" in draft.text and "</table>" in draft.text for draft in repaired)
    assert_evidence(repaired, sources)


def test_rowspan_transitive_group_remains_in_one_child():
    """跨行合并单元格关联组整体保留，不能把第二行切到独立表格中。"""
    linked = '<tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td></tr>'
    sources = [block(1, "<table>" + linked + "<tr><td>D</td><td>E</td></tr>" * 8 + "</table>", "table")]
    repaired = list(refinement.refine_drafts(build_chunks(uuid4(), uuid4(), "pdf", sources), TokenStub(), identity(128)))
    assert sum(linked in child.text for child in repaired) == 1
    assert_evidence(repaired, sources)


@pytest.mark.parametrize("kind,text", [
    ("formula", "=SUM(" + "A1+" * 100 + "A2)"),
    ("table", "<table><tr><td>" + "甲" * 200 + "</td></tr></table>"),
    ("table", '<table class="unknown"><tr><td>' + "甲" * 200 + "</td></tr></table>"),
])
def test_irreducible_formula_row_and_unknown_structure_return_specific_error(kind, text):
    """不可拆公式/单行/未知HTML给明确原因；不截断、不拼造新表格或伪造向量。"""
    drafts = build_chunks(uuid4(), uuid4(), "pdf", [block(1, text, kind)])
    with pytest.raises(EmbeddingError, match="EMBEDDING_UNSPLITTABLE"):
        list(refinement.refine_drafts(drafts, TokenStub(), identity()))


def test_character_budget_is_checked_before_tokenizing_giant_chunk(monkeypatch):
    """字符超限先转重切，不把巨片直接交给Tokenizer，首个超限位置保持可诊断。"""
    monkeypatch.setattr(refinement, "MAX_TEXT_CHARS", 10)
    row = SimpleNamespace(text="甲" * 11, chunk_id=uuid4(), chunk_index=5)
    service = MagicMock()
    assert refinement.first_oversized([row], service, 8192) == (row, "TEXT_CHAR_LIMIT", 11, 10)
    service.token_counts.assert_not_called()


def test_preflight_batches_keep_first_failure_and_never_encode():
    """按批计Token仍返回第一个超长片，不把后批故障拖到向量计算时才发现。"""
    rows = [SimpleNamespace(text=value, chunk_id=uuid4(), chunk_index=index)
            for index, value in enumerate(["短文", "甲" * 30, "乙" * 32], 1)]
    service = MagicMock()
    service.token_counts.side_effect = [[4, 32], [34]]
    assert refinement.first_oversized(rows, service, 32, batch_size=2) == (rows[2], "TOKEN_LIMIT", 34, 32)
    assert [call.args[0] for call in service.token_counts.call_args_list] == [[rows[0].text, rows[1].text], [rows[2].text]]
    service.embed_documents.assert_not_called()


@pytest.mark.parametrize("damage", ["none", "wrong_hash"])
def test_rebuild_verifies_original_block_hash_before_replacing_chunks(monkeypatch, damage):
    """原输入Hash缺失或变化时，在删除旧Chunk之前拒绝；不能用重切绕过来源检查。"""
    parse_id = uuid4()
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), file_type="pdf", processing_task_id=parse_id,
        chunk_config={"options": ChunkOptions().snapshot(), "source_parse_task_id": str(parse_id)})
    session = MagicMock()
    session.scalars.return_value = iter([] if damage == "none" else ["b" * 64])
    monkeypatch.setattr(refinement, "source_identity", lambda factory: ("a" * 64, {}))
    with pytest.raises(EmbeddingError, match="EMBEDDING_SOURCE"):
        refinement.rebuild_chunks(session, document, TokenStub(), identity())
    session.execute.assert_not_called()


@pytest.mark.parametrize("failure", ["EMBEDDING_TOO_LONG", "EMBEDDING_LIMIT"])
def test_failed_old_embedding_retry_enables_preflight_without_changing_frozen_model(monkeypatch, tmp_path, failure):
    """旧向量长度失败Retry自动启用重切配方，仍校验旧输入Hash并保留模型/批大小。"""
    options = EmbeddingOptions(str(tmp_path / "model"), batch_size=2)
    row = SimpleNamespace(chunk_id=uuid4(), chunk_index=1, text="旧超长片")
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), status="FAILED", processing_task_id=uuid4(),
        chunk_task_id=uuid4(), embedding_task_id=uuid4(), index_task_id=None,
        error_message=failure + "：安全错误")
    config = {"options": options.snapshot(), "source_hash": processing.chunks_hash([row]),
        "source_parse_task_id": str(document.processing_task_id), "source_chunk_task_id": str(document.chunk_task_id),
        "output_dir": str(tmp_path / "vectors")}
    document.embedding_config = config
    read = MagicMock(return_value=[row])
    monkeypatch.setattr(processing, "read_chunks", read)
    session = MagicMock()
    session.scalar.return_value = None
    task_id, dispatch = processing.prepare_embedding(session, document, MagicMock())
    assert dispatch and task_id == document.embedding_task_id
    read.assert_called_once_with(session, document, allow_refinement=True)
    assert document.embedding_config == config | {"input_policy": refinement.POLICY}
    assert document.status == "EMBEDDING" and document.error_message is None


def test_rechunk_precedes_vector_writer_and_hands_off_new_generation(monkeypatch, tmp_path):
    """超长预检只提交新Chunk/检查点并交接，不在原任务写入或发布半份向量。"""
    options = EmbeddingOptions(str(tmp_path / "model"), max_length=64)
    old = [SimpleNamespace(chunk_id=uuid4(), chunk_index=1, text="甲" * 100)]
    new = [SimpleNamespace(chunk_id=uuid4(), chunk_index=i + 1, text="甲" * 50) for i in range(2)]
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), status="EMBEDDING", processing_task_id=uuid4(),
        chunk_task_id=uuid4(), embedding_task_id=uuid4(), index_task_id=None,
        chunk_config={"options": ChunkOptions().snapshot()})
    old_task, old_chunk_task = document.embedding_task_id, document.chunk_task_id
    document.management_config = {"state": "running", "stage": "embed", "stage_task_id": str(old_task)}
    document.embedding_config = {"options": options.snapshot(), "source_hash": processing.chunks_hash(old),
        "source_parse_task_id": str(document.processing_task_id), "source_chunk_task_id": str(old_chunk_task),
        "output_dir": str(tmp_path / "vectors"), "input_policy": refinement.POLICY}
    session, store, service = MagicMock(), MagicMock(), MagicMock()
    session.scalar.return_value = document
    store.ready.return_value = None
    service.identity.return_value = identity(64)
    service.token_counts.return_value = [102]
    monkeypatch.setattr(processing, "read_chunks", MagicMock(side_effect=[old, new]))
    monkeypatch.setattr(processing, "rebuild_chunks", MagicMock(return_value=2))
    monkeypatch.setattr(processing, "get_embedding_service", MagicMock(return_value=service))
    monkeypatch.setattr(processing, "ArtifactStore", MagicMock(return_value=store))
    result = processing.run_embedding_step(session, document.doc_id, old_task)
    assert result["status"] == "rechunked" and result["next_task_id"] == str(document.embedding_task_id)
    assert document.embedding_task_id != old_task and document.chunk_task_id != old_chunk_task
    assert document.embedding_config["source_hash"] == processing.chunks_hash(new)
    assert document.embedding_config["source_chunk_task_id"] == str(document.chunk_task_id)
    assert document.management_config["stage_task_id"] == str(document.embedding_task_id)
    assert document.status == "EMBEDDING" and "result" not in document.embedding_config
    service.embed_documents.assert_not_called()
    store.writer.assert_not_called()
    store.publish.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("changed_model", [False, True])
def test_repaired_input_does_not_enter_unbounded_rechunk_loop(monkeypatch, tmp_path, changed_model):
    """同模型重切后超长或模型指纹变化均明确拒绝，不混用或创建无限队列代次。"""
    options = EmbeddingOptions(str(tmp_path / "model"), max_length=64)
    rows = [SimpleNamespace(chunk_id=uuid4(), chunk_index=1, text="甲" * 100)]
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), status="EMBEDDING", processing_task_id=uuid4(),
        chunk_task_id=uuid4(), embedding_task_id=uuid4())
    document.embedding_config = {"options": options.snapshot(), "source_hash": processing.chunks_hash(rows),
        "source_parse_task_id": str(document.processing_task_id), "source_chunk_task_id": str(document.chunk_task_id),
        "output_dir": str(tmp_path / "vectors"), "input_policy": refinement.POLICY,
        "token_refinement": identity(64) | {"version": refinement.POLICY}}
    service, store = MagicMock(), MagicMock()
    service.identity.return_value = identity(64) | ({"model_fingerprint": "c" * 64} if changed_model else {})
    service.token_counts.return_value = [102]
    store.ready.return_value = None
    session = MagicMock()
    session.scalar.return_value = document
    monkeypatch.setattr(processing, "read_chunks", MagicMock(return_value=rows))
    rebuild = MagicMock()
    monkeypatch.setattr(processing, "rebuild_chunks", rebuild)
    monkeypatch.setattr(processing, "get_embedding_service", MagicMock(return_value=service))
    monkeypatch.setattr(processing, "ArtifactStore", MagicMock(return_value=store))
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL_CHANGED" if changed_model else "EMBEDDING_SOURCE"):
        processing.run_embedding_step(session, document.doc_id, document.embedding_task_id)
    rebuild.assert_not_called()
    store.writer.assert_not_called()
