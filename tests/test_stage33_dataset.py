"""阶段33固定范围的元数据Mock；只在用户执行时创建占位字节，不生成真实业务文档。"""

import csv
import json
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

import pytest

from scripts import stage33_common as tool


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """构造200份纯字节哨兵和4087条Mock元数据，绝不访问真实阶段32目录。"""
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    root = tmp_path / "data/dataset/stage32_run_01"
    documents, questions, mapping = [], [], []
    for dataset, (file_count, qa_count) in tool.COUNTS.items():
        items = []
        for index in range(file_count):
            kind = "pdf" if dataset == "ohr" else ("docx", "xlsx", "pptx")[index % 3]
            identity = f"domain/mock-{index}" if dataset == "ohr" else f"mock-{index}.{kind}"
            path = root / "documents" / dataset / (f"mock-{index}.{kind}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"MOCK_ONLY_{dataset}_{index}".encode())
            sha = tool.sha_file(path)
            item = {"dataset": dataset, "source_identity": identity,
                "file_id": str(uuid5(NAMESPACE_URL, f"dkf:stage32:{dataset}:{identity}:{sha}")),
                "file_path": path.relative_to(tmp_path).as_posix(), "file_type": kind,
                "size_bytes": path.stat().st_size, "sha256": sha, "page_count": 400 if kind == "pdf" else ""}
            items.append(item)
        linked = Counter()
        for index in range(qa_count):
            docs = [items[index % file_count]]
            if dataset == "ocb" and index % 20 == 0:
                docs.append(items[(index + 1) % file_count])
            original = ({"ID": f"qa-{index}", "questions": f"MOCK_PRIVATE_QUESTION_{index}", "answers": "MOCK_ANSWER",
                "doc_name": docs[0]["source_identity"], "evidence_page_no": 0, "evidence_source": "text"}
                if dataset == "ohr" else {"question": f"MOCK_PRIVATE_QUESTION_{index}",
                    "reference_files": [d["source_identity"] for d in docs], "track": "File Fidelity Word",
                    "expected_assertions": ["MOCK_ASSERTION_NOT_REAL_GOLD"], "weights": [1.0]})
            qa = {"dataset": dataset, "qa_id": f"qa-{index}",
                "question": original.get("questions", original.get("question")),
                "expected_answer": original.get("answers"), "expected_assertions": original.get("expected_assertions"),
                "source_file_ids": [d["file_id"] for d in docs], "source_file_paths": [d["file_path"] for d in docs],
                "source_file_hashes": {d["file_id"]: d["sha256"] for d in docs},
                "original": original, "original_record_sha256": tool.digest(original),
                "qa_source": {"source_row_index": index, "repository": "mock_not_a_real_dataset"},
                "validation_status": "validated_dependencies_and_official_basis",
                "evidence_page": {"official_0based": [0], "system_1based": [1]} if dataset == "ohr" else None,
                **{k: original.get(k) for k in ("weights", "track", "reference_files", "evidence_source",
                                               "evidence_context", "evidence_page_no")}}
            questions.append(qa)
            for doc in docs:
                linked[doc["file_id"]] += 1
                mapping.append({"dataset": dataset, "qa_id": qa["qa_id"], "file_id": doc["file_id"],
                    "file_path": doc["file_path"], "sha256": doc["sha256"], "dependency_count": len(docs)})
        for doc in items:
            doc["qa_count"] = linked[doc["file_id"]]
        documents.extend(items)
        tool.write_csv(root / f"manifests/{dataset}_documents.csv", items, list(items[0]))
        target = root / f"qa/{dataset}_qa.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"".join(tool.encoded(q) + b"\n" for q in questions if q["dataset"] == dataset))
    tool.write_csv(root / "manifests/qa_file_mapping.csv", mapping, list(mapping[0]))
    return root


def test_frozen_full_scope_is_200_and_4087_without_sampling(frozen):
    """验证默认固定全部文件/题目，分母不是4078或旧小样本规模。"""
    data = tool.load_dataset(frozen, verify_files=False)
    assert len(data["documents"]) == 200 and len(data["questions"]) == 4087
    assert Counter(q["dataset"] for q in data["questions"]) == {"ohr": 3588, "ocb": 499}
    assert data["file_issues"] == {}


def test_missing_file_is_reported_but_all_qa_and_documents_remain_registered(frozen):
    """一个缺失原件保留完整范围和原因，不能静默删文件/关联QA。"""
    data = tool.load_dataset(frozen, verify_files=False)
    doc = data["documents"][0]
    (tool.ROOT / doc["file_path"]).unlink()
    result = tool.load_dataset(frozen, verify_files=False)
    assert result["file_issues"][doc["file_id"]] == "FILE_MISSING_OR_SIZE"
    assert len(result["documents"]) == 200 and len(result["questions"]) == 4087


def test_ohr_zero_based_page_projection_cannot_shift_silently(frozen):
    """官方0页必须映射业务1页，不能把局部分片偏移写回冻结QA。"""
    path = frozen / "qa/ohr_qa.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["evidence_page"]["system_1based"] = [0]
    path.write_bytes(b"".join(tool.encoded(r) + b"\n" for r in rows))
    with pytest.raises(tool.Stage33Error, match="OHR_PAGES_OR_FIELDS"):
        tool.load_dataset(frozen, verify_files=False)


def test_multifile_dependency_or_mapping_missing_is_a_hard_failure(frozen):
    """多文件QA不拆题，映射缺一条即明确失败，不降为单文件评分。"""
    path = frozen / "manifests/qa_file_mapping.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    tool.write_csv(path, rows[:-1], list(rows[0]))
    with pytest.raises(tool.Stage33Error, match="MAPPING"):
        tool.load_dataset(frozen, verify_files=False)


def test_official_assertions_remain_structured_and_are_not_answers(frozen):
    """OCB原始断言/权重/依赖不变，expected_answer始终null。"""
    rows = tool.load_dataset(frozen, verify_files=False)["questions"]
    qa = next(q for q in rows if q["dataset"] == "ocb")
    assert qa["expected_answer"] is None
    assert qa["expected_assertions"] == qa["original"]["expected_assertions"]
    assert len(qa["source_file_ids"]) == 2 and len(qa["reference_files"]) == 2


def test_fewer_files_or_questions_cannot_be_formal_input(frozen):
    """禁止通过减少正式文件/QA数量获得看似更好的结果。"""
    path = frozen / "qa/ocb_qa.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(tool.Stage33Error, match="FROZEN_COUNTS"):
        tool.load_dataset(frozen, verify_files=False)
