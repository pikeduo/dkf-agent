"""阶段33固定QA抽样的内存回归；仅供用户执行，不读取原件或运行模型/数据库。"""

from collections import Counter
from copy import deepcopy
from uuid import UUID

import pytest

from scripts import stage33_common as common
from scripts import stage33_reports as reports


@pytest.fixture
def population():
    """构造200份身份和两组120题，不生成文档样本，保留多文件QA的完整依赖。"""
    documents, questions = [], []
    for dataset in ("ohr", "ocb"):
        documents.extend({"file_id": f"{dataset}-{index}", "dataset": dataset,
                          "file_type": "pdf" if dataset == "ohr" else ("docx", "xlsx", "pptx")[index % 3]}
                         for index in range(100))
        for index in range(120):
            sources = [f"{dataset}-{index % 100}"]
            if dataset == "ocb" and index % 2 == 0:
                sources.append(f"{dataset}-{(index + 1) % 100}")
            questions.append({"dataset": dataset, "qa_id": f"qa-{index}", "question": f"MOCK_QUERY_{index}",
                "source_file_ids": sources, "expected_answer": "MOCK_ANSWER",
                "expected_assertions": ["MOCK_ASSERTION"],
                "evidence_page": {"system_1based": [1]} if dataset == "ohr" else None})
    return {"fingerprint": "mock-parent", "documents": documents, "questions": questions, "file_issues": {}}


def test_default_keeps_full_population_and_legacy_identity(population):
    """未指定抽样时维持原完整输入及指纹，不改变既有全量run的恢复契约。"""
    assert common.select_questions(population) is population
    assert "qa_selection" not in population


def test_selects_50_per_dataset_and_preserves_all_documents_and_dependencies(population):
    """每集恰好50题，全部200份仍可检索；不拆多文件题或修改答案、依赖和原始池。"""
    before = deepcopy(population)
    selected = common.select_questions(population, per_dataset=50)
    assert Counter(qa["dataset"] for qa in selected["questions"]) == {"ohr": 50, "ocb": 50}
    assert len(selected["documents"]) == 200 and selected["documents"] is population["documents"]
    originals = {(qa["dataset"], qa["qa_id"]): qa for qa in population["questions"]}
    assert all(qa == originals[qa["dataset"], qa["qa_id"]] for qa in selected["questions"])
    assert population == before and selected["fingerprint"] != population["fingerprint"]
    assert selected["qa_selection"]["population_counts"] == {"ohr": 120, "ocb": 120}
    assert selected["qa_selection"]["selected_counts"] == {"ohr": 50, "ocb": 50}


def test_selection_is_stable_under_reordering_and_changes_with_seed(population):
    """同种子与官方身份固定结果，不依赖输入行序；不同种子隔离选题及断点身份。"""
    first = common.select_questions(population, per_dataset=50, seed=33)
    reordered = population | {"questions": list(reversed(population["questions"]))}
    second = common.select_questions(reordered, per_dataset=50, seed=33)
    third = common.select_questions(population, per_dataset=50, seed=34)
    assert first["qa_selection"] == second["qa_selection"] and first["fingerprint"] == second["fingerprint"]
    assert first["qa_selection"]["qa_ids"] != third["qa_selection"]["qa_ids"]
    assert first["fingerprint"] != third["fingerprint"]


def test_answers_questions_and_quality_do_not_choose_the_sample(population):
    """选择排序不读取答案、问题正文或文档质量，不按已知容易题或成功文件挑选。"""
    first = common.select_questions(population, per_dataset=50)
    changed = deepcopy(population)
    for qa in changed["questions"]:
        qa.update(question="DIFFERENT_QUERY", expected_answer="DIFFERENT_ANSWER", expected_assertions=[])
    for document in changed["documents"]:
        document["parse_quality"] = {"content": "PARTIAL"}
    changed["fingerprint"] = "changed-parent"
    second = common.select_questions(changed, per_dataset=50)
    assert first["qa_selection"]["qa_ids"] == second["qa_selection"]["qa_ids"]
    assert first["fingerprint"] != second["fingerprint"]


@pytest.mark.parametrize("count,seed,code", [(0, 33, "QA_SAMPLE_ARGUMENTS"),
    (-1, 33, "QA_SAMPLE_ARGUMENTS"), (True, 33, "QA_SAMPLE_ARGUMENTS"),
    (50, -1, "QA_SAMPLE_ARGUMENTS"), (50, True, "QA_SAMPLE_ARGUMENTS"),
    (121, 33, "QA_SAMPLE_TOO_LARGE")])
def test_invalid_sample_is_rejected_without_silently_reducing_quota(population, count, seed, code):
    """错误参数或任一数据集题量不足时明确拒绝，不能悄悄取不足50条或重复凑数。"""
    with pytest.raises(common.Stage33Error, match=code):
        common.select_questions(population, per_dataset=count, seed=seed)


def test_sample_manifest_is_frozen_and_cannot_resume_full_or_other_seed(population, tmp_path):
    """抽样清单独立保存，重复相同run可恢复，不能与全量或另一种子混算。"""
    selected = common.select_questions(population, per_dataset=50)
    kb = UUID(int=999)
    common.initialize(tmp_path, selected, kb)
    assert common.read_json(tmp_path / "qa_selection.json") == selected["qa_selection"]
    assert common.initialize(tmp_path, selected, kb)["qa_count"] == 100
    for other in (population, common.select_questions(population, per_dataset=50, seed=34)):
        with pytest.raises(common.Stage33Error, match="RUN_IDENTITY_CHANGED"):
            common.initialize(tmp_path, other, kb)


def test_sample_report_registers_100_questions_and_400_records(population, tmp_path):
    """报告标明抽样并保留200份，未执行不假报成绩或称为全量QA验收。"""
    selected = common.select_questions(population, per_dataset=50)
    report = reports.generate(selected, tmp_path)
    assert report["fixed_document_count"] == 200 and report["fixed_qa_count"] == 100
    assert report["qa_strategy_records"] == 400 and report["population_qa_count"] == 240
    assert report["qa_scope"] == "sampled" and report["full_qa_evaluation"] is False
    assert report["status"] == "INCOMPLETE" and report["metrics_status"] == "NOT_CERTIFIED"
    assert report["metrics"]["ohr"]["dense"]["total_questions"] == 50
    assert report["metrics"]["ocb"]["dense"]["total_questions"] == 50
    assert report["metrics"]["ocb"]["dense"]["metrics"]["recall_at_5"] is None
    assert "抽样检索评测" in (tmp_path / "summary.md").read_text(encoding="utf-8")
