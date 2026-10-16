"""阶段32纯契约/元数据用例；不生成文档、提取原件、连接服务或加载模型。"""

import copy
from pathlib import Path

import pytest

from scripts import prepare_stage32_dataset as tool


def official_row(**changes):
    """构造内存官方行字段，不声称来自真实数据集，也不生成Office测试样本。"""
    return {"question": "SCHEMA_QUESTION", "expected_assertions": ["SCHEMA_ASSERTION"],
            "weights": [1.0], "track": "File Fidelity Word", "reference_files": ["schema.docx"]} | changes


@pytest.mark.parametrize("changes,reason", [
    ({}, None),
    ({"track": "Domain"}, "domain_task_requires_manual_rag_suitability_review"),
    ({"expected_assertions": []}, "missing_assertion_basis"),
    ({"weights": []}, "invalid_assertion_weights"),
    ({"weights": [float("nan")]}, "invalid_assertion_weights"),
    ({"weights": [-1]}, "invalid_assertion_weights"),
    ({"weights": [0]}, "invalid_assertion_weights"),
])
def test_official_rubric_is_not_flattened_or_assumed_reliable(changes, reason):
    """空断言、坏权重和复杂任务待审，不伪装为正式可评分题；不调用模型判分。"""
    assert tool.rubric_status(official_row(**changes)) == reason


def test_complete_multi_file_qa_keeps_dependencies_and_original_fields():
    """一条多文件题保留整组顺序、原始断言/权重和全部字段，expected_answer保持null。"""
    original = official_row(reference_files=["a.docx", "b.xlsx"], feature="SCHEMA_FEATURE")
    before = copy.deepcopy(original)
    docs = [{"file_id": "a", "file_path": "data/dataset/stage32_run_01/documents/ocb/a.docx", "sha256": "a" * 64},
            {"file_id": "b", "file_path": "data/dataset/stage32_run_01/documents/ocb/b.xlsx", "sha256": "b" * 64}]
    result = tool.qa_record("ocb", "parquet:row:1", original["question"], None,
                            original["expected_assertions"], docs, None, {"source_row_index": 1}, original)
    assert result["source_file_ids"] == ["a", "b"]
    assert result["source_file_paths"] == [d["file_path"] for d in docs]
    assert result["source_file_hashes"] == {"a": "a" * 64, "b": "b" * 64}
    assert result["expected_answer"] is None and result["expected_assertions"] == ["SCHEMA_ASSERTION"]
    assert result["original"] == before and original == before
    result["original"]["weights"][0] = 9
    assert original == before


def test_file_identity_and_seed_ranking_are_stable():
    """稳定标识绑定官方身份与内容Hash，不以QA条数或随机UUID替代原件身份。"""
    path = tool.ROOT / "data/dataset/stage32_run_01/documents/ohr/news/schema.pdf"
    kwargs = {"source": "docs/test/ohr_bench_preset/source/pdfs.zip!/news/schema.pdf", "pages": 3}
    first = tool.source_record("ohr", "news/schema", path, "a" * 64, 128, **kwargs)
    assert first == tool.source_record("ohr", "news/schema", path, "a" * 64, 128, **kwargs)
    assert first["file_id"] != tool.source_record("ohr", "news/schema", path, "b" * 64, 128, **kwargs)["file_id"]
    assert "\\" not in first["file_path"] and not Path(first["file_path"]).is_absolute()
    assert tool.ranked("news/schema") == tool.ranked("news/schema")
