"""OCB 联合冒烟离线适配的元数据 Stub；只编写，用户运行，无真实 Office 或网络。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import stage16_ocb as tool


def original(index, names, track="File Fidelity Word"):
    """单元专用 QA 元数据，不冒充公开数据集的真实问题或答案。"""
    return {"question": f" 单元问题{index}\n保留空白 ", "expected_assertions": ["单元断言甲", "单元断言乙"],
            "reference_files": names, "track": track, "feature": f"unit-feature-{index}"}


def local_archive_stub(monkeypatch, *, missing_exports=False, missing_file=False):
    """所有 IO/官方来源校验由内存 Stub 替代，不下载、生成或读取 Office 文件。"""
    rows = [original(0, ["unit.docx"]), original(1, ["unit.xlsx"], "File Fidelity Excel"),
            original(2, ["unit.pptx"], "File Fidelity PowerPoint"),
            original(3, ["unit.docx"], "Domain"), original(4, ["unit.docx", "missing.docx"])]
    candidates = [{"reference_file": name, "file_type": name.rsplit(".", 1)[1],
        "source_kind": "hf", "local_file": "reference_files/" + name,
        "source_url": "https://example.invalid/unit-only", "size_bytes": 100}
        for name in ("unit.docx", "unit.xlsx", "unit.pptx")]
    plan = {"source": {"revision": "a" * 40}, "plan_sha256": "b" * 64,
        "qa_groups": [{"source_row_index": i, "qa_id": f"unit-{i}",
                       "reference_files": r["reference_files"], "track": r["track"]} for i, r in enumerate(rows)]}
    source = {"revision": "a" * 40, "metadata_sha256": {"ocb_qna_data.parquet": "f" * 64}}
    records = [{"reference_file": c["reference_file"], "status": "verified_existing",
                "local_file": c["local_file"], "source_url": c["source_url"], "size_bytes": 100,
                "technical_source_verified": True, "sha256": "c" * 64} for c in candidates]
    report = {"dataset": "OCB", "revision": "a" * 40, "plan_sha256": "b" * 64, "records": records}
    exports = [{"source_row_index": i, "qa_id": f"unit-{i}", "original_qa": r,
                **{k: r[k] for k in ("reference_files", "question", "expected_assertions", "track")}}
               for i, r in enumerate(rows[:4])]
    reads = []
    real_exists, real_is_file, real_stat = Path.exists, Path.is_file, Path.stat

    def virtual(path):
        """只接管单元元数据的虚拟根，不影响 pytest 自身的路径检查。"""
        return bool(path.parts and path.parts[0] == "virtual-project")

    def json_file(path, *args, **kwargs):
        """返回官方 JSON 形状 Stub，不访问本地真实报告。"""
        return report if path.name == "archive_report.json" else exports

    def parquet(path):
        """记录本地 Parquet 的唯一读取入口，返回原始元数据，不安装 PyArrow。"""
        reads.append(path.name)
        return deepcopy(rows)

    def exists(path):
        """只模拟 QA 导出是否缺失，不创建目录或占位文件。"""
        return (not (missing_exports and path.name in {"qa_full.json", "qa_smoke.json"})
                if virtual(path) else real_exists(path))

    def is_file(path):
        """控制一个模拟原件缺失，不生成虚假的 Office 容器。"""
        return not (missing_file and path.name == "unit.xlsx") if virtual(path) else real_is_file(path)

    def stat(path, *args, **kwargs):
        """虚拟输入只暴露大小，其余路径保持宿主原有行为。"""
        return SimpleNamespace(st_size=100) if virtual(path) else real_stat(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        """联网或原下载器的报告写入即失败。"""
        pytest.fail("离线准备不能下载或覆盖报告")

    monkeypatch.setattr(tool.common.ohr, "plain_path", lambda path, **kwargs: Path(path))
    monkeypatch.setattr(tool.common, "source_record", lambda *args: deepcopy(source))
    monkeypatch.setattr(tool.bulk, "load_plan", lambda *args: (deepcopy(plan), deepcopy(candidates)))
    monkeypatch.setattr(tool.bulk, "validate_metadata", lambda *args: None)
    monkeypatch.setattr(tool.bulk, "safe_local_path", lambda root, item: root / item["local_file"])
    monkeypatch.setattr(tool.common, "json_file", json_file)
    monkeypatch.setattr(tool.common, "rows_file", parquet)
    monkeypatch.setattr(tool.common, "read_bytes", lambda *args, **kwargs: b"metadata-only")
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "stat", stat)
    for name in ("archive_one", "get_with_redirect_checks", "atomic_json", "export_qa"):
        monkeypatch.setattr(tool.bulk, name, forbidden)
    return rows, report, exports, reads


def test_existing_exports_validated_against_parquet_and_domain_preserved(monkeypatch):
    """已有 QA 导出逐字段对照官方 Parquet；Domain 保留原 track，不编辑断言。"""
    rows, report, exports, reads = local_archive_stub(monkeypatch)
    before = deepcopy(exports)
    _, assets, references, source = tool.load_local_archive(Path("virtual-project"))
    assert len(assets) == 3 and len(references) == 4
    assert {r["track"] for r in references} == {"Domain", "File Fidelity Word", "File Fidelity Excel", "File Fidelity PowerPoint"}
    assert references[0]["question"] == rows[0]["question"]
    assert references[0]["expected_assertions"] == rows[0]["expected_assertions"]
    assert source["cloud_permission"] == "not_granted" and exports == before
    assert source["exclusions"]["qa_dependencies_incomplete"] == 1
    assert reads == ["ocb_qna_data.parquet"]


def test_missing_qa_exports_rebuilt_from_local_parquet_without_download(monkeypatch):
    """缺 QA JSON 时只从本地 Parquet 形成关联，原 bulk 报告不写入或覆盖。"""
    _, _, _, reads = local_archive_stub(monkeypatch, missing_exports=True)
    _, _, refs, source = tool.load_local_archive(Path("virtual-project"))
    assert len(refs) == 4 and reads == ["ocb_qna_data.parquet"]
    assert all(v.startswith("missing_") for v in source["qa_exports"].values())


def test_missing_dependency_is_excluded_without_fabricating_file(monkeypatch):
    """未取得的原件和依赖被排除，不要求补齐整库或把缺失文件计为可测试。"""
    local_archive_stub(monkeypatch, missing_file=True)
    _, assets, refs, source = tool.load_local_archive(Path("virtual-project"))
    assert "unit.xlsx" not in assets
    assert all("unit.xlsx" not in r["reference_files"] and "missing.docx" not in r["reference_files"] for r in refs)
    assert source["exclusions"]["not_downloaded"] == 1


@pytest.mark.parametrize("field", ["question", "expected_assertions", "reference_files", "track", "original_qa", "qa_id"])
def test_modified_export_is_rejected_not_overwritten(monkeypatch, field):
    """已有导出与官方原行不一致时安全停止，不修改官方 QA 或覆盖 JSON。"""
    _, _, exports, _ = local_archive_stub(monkeypatch)
    exports[0][field] = "unit-tampering"
    with pytest.raises(tool.common.BenchmarkError):
        tool.load_local_archive(Path("virtual-project"))


def selection_stub(monkeypatch):
    """六个文件身份与每个两题；验证器返回元数据，不读取任何原件。"""
    assets, refs = {}, []
    for i, kind in enumerate(tool.bulk.FORMATS):
        for offset in range(2):
            name = f"unit-{i}-{offset}.{kind}"
            assets[name] = {"file_type": kind, "size_bytes": 100}
            for question in range(2):
                row = original(question, [name], "File Fidelity " + {"docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint"}[kind])
                refs.append(tool.official_reference(row, i * 10 + offset * 2 + question, "a" * 64))

    def verified(root, name, metadata):
        """用文件身份生成 Stub 摘要，明确不代表真实文件完整性验证。"""
        return {"file_type": metadata["file_type"], "sha256": tool.common.ranked(0, name),
                "source_file": "reference_files/" + name, "cloud_transfer_allowed": False}

    monkeypatch.setattr(tool, "verified_asset", verified)
    return assets, refs


def test_six_files_deterministic_balanced_selection_and_original_assertions(monkeypatch):
    """六份按三格式各二份选择，反转输入不改变结果，断言维持列表及原始字段。"""
    assets, refs = selection_stub(monkeypatch)
    before = deepcopy(refs)
    selected, qa, gaps = tool.choose_local_samples(Path("virtual"), assets, refs,
        per_format=2, seed=42, qa_per_file=4)
    repeat = tool.choose_local_samples(Path("virtual"), assets, list(reversed(refs)),
        per_format=2, seed=42, qa_per_file=4)
    assert (selected, qa, gaps) == repeat
    assert len(selected) == 6 and len(qa) == 12
    assert gaps["format_shortfall"] == {"docx": 0, "xlsx": 0, "pptx": 0}
    assert all(r["original"]["expected_assertions"] == r["expected_assertions"] for r in qa)
    assert refs == before and all(not a["cloud_transfer_allowed"] for a in selected.values())


def test_multi_file_qa_keeps_all_selected_dependencies(monkeypatch):
    """多文件参考完整纳入且仅一条 QA，不拆题、不遗漏其中一个文档。"""
    assets, refs = selection_stub(monkeypatch)
    names = sorted(assets)[:2]
    multi = tool.official_reference(original(99, names), 999, "a" * 64)
    refs.append(multi)
    selected, qa, _ = tool.choose_local_samples(Path("virtual"), assets, refs,
        per_format=2, seed=42, qa_per_file=4)
    row = next(r for r in qa if r["reference_id"] == multi["reference_id"])
    assert row["reference_files"] == names and len(row["dependencies"]) == 2
    assert all(name in selected for name in names)


def test_file_integrity_failure_reports_shortfall_without_replacing_file(monkeypatch):
    """某格式缺真实有效原件时如实少于二份，不凑数、不写临时 Office。"""
    assets, refs = selection_stub(monkeypatch)
    good = tool.verified_asset

    def verified(root, name, metadata):
        """拒绝全部 PPTX Stub，其余保持元数据校验接口。"""
        if metadata["file_type"] == "pptx":
            raise tool.common.BenchmarkError("FORMAT")
        return good(root, name, metadata)

    monkeypatch.setattr(tool, "verified_asset", verified)
    selected, _, gaps = tool.choose_local_samples(Path("virtual"), assets, refs,
        per_format=2, seed=42, qa_per_file=4)
    assert len(selected) == 4 and gaps["format_shortfall"]["pptx"] == 2


@pytest.mark.parametrize("lfs_field", [None, "oid", "sha256"])
@pytest.mark.parametrize("mismatch", [None, "receipt", "official"])
def test_selected_hf_asset_checks_official_hash_and_receipt(monkeypatch, lfs_field, mismatch):
    """入选时检查 Git OID/两种 LFS 摘要及回执；技术通过也不自动获得外传权限。"""
    sha, oid = "c" * 64, "a" * 40
    tree = {"type": "file", "path": "reference_files/unit.docx", "size": 100,
            "oid": "b" * 40 if mismatch == "official" and lfs_field is None else oid}
    if lfs_field:
        tree["lfs"] = {lfs_field: "d" * 64 if mismatch == "official" else sha, "size": 100}
    item = {"source_kind": "hf", "local_file": tree["path"], "file_type": "docx",
            "official_tree_object": tree, "size_bytes": 100}
    metadata = {"candidate": item, "receipt": {"sha256": "e" * 64 if mismatch == "receipt" else sha},
                "path": Path("virtual-source/reference_files/unit.docx")}
    calls = []

    def inspect(path, kind, expected_size):
        """只返回容器检查摘要 Stub，不创建或打开真实 Office 文件。"""
        assert path == metadata["path"] and kind == "docx" and expected_size == 100
        calls.append("container")
        return sha

    def blob(path):
        """未使用 LFS 时返回 Git blob 摘要 Stub，不读取文件。"""
        calls.append("git_blob")
        return oid

    def asset(root, relative, dataset, max_bytes):
        """匹配通过后才形成上传契约元数据，仍不授予版权或外传权限。"""
        calls.append("asset")
        return {"sha256": sha, "source_file": relative, "file_type": "docx"}

    monkeypatch.setattr(tool.bulk, "inspect_office", inspect)
    monkeypatch.setattr(tool.bulk, "git_blob_oid", blob)
    monkeypatch.setattr(tool.common, "asset", asset)
    if mismatch:
        with pytest.raises(tool.common.BenchmarkError):
            tool.verified_asset(Path("virtual-source"), "unit.docx", metadata)
        assert "asset" not in calls
    else:
        result = tool.verified_asset(Path("virtual-source"), "unit.docx", metadata)
        assert result["sha256"] == sha and result["cloud_transfer_allowed"] is False
        assert ("git_blob" in calls) == (lfs_field is None)
