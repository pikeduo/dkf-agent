"""联合冒烟的 OCB 离线适配；只读取本地原件/官方 QA，不下载、不授予云外传权限。"""

import hashlib
from collections import Counter
from pathlib import Path

from scripts import benchmark_common as common
from scripts import ocb_bulk_archive as bulk
from scripts import prepare_ocb_preset as ocb


def official_reference(row, index, source_hash):
    """复用 File Fidelity 契约，并保留完整 Domain QA；不合并或改写原始断言。"""
    if not isinstance(row, dict):
        raise common.BenchmarkError("SCHEMA")
    if ocb.fidelity_subtype(row.get("track")) is not None:
        return ocb.qa_record(row, index, "hf", source_hash)
    if row.get("track") != "Domain":
        raise common.BenchmarkError("SOURCE")
    names, question, assertions = row.get("reference_files"), row.get("question"), row.get("expected_assertions")
    if (not isinstance(names, list) or not 1 <= len(names) <= 5
            or not all(isinstance(name, str) for name in names) or len(set(names)) != len(names)
            or not isinstance(question, str) or not question.strip()
            or not isinstance(assertions, list) or not assertions
            or not all(isinstance(value, str) and value.strip() for value in assertions)):
        raise common.BenchmarkError("SCHEMA")
    for name in names:
        common.ohr.safe_name(name)
    return {"reference_id": source_hash + ":hf_row:" + str(index), "source_row_index": index,
            "source_schema": "hf", "reference_files": names, "question": question,
            "expected_assertions": assertions, "track": row["track"], "track_subtype": "Domain",
            "feature": row.get("feature"), "original": row}


def load_local_archive(project):
    """核对固定版本、下载回执及 QA 导出；缺导出直接读已有 Parquet，不改 bulk 报告。"""
    project = common.ohr.plain_path(project)
    root = common.ohr.plain_path(project / "docs/test/ocb_preset/source", beneath=project)
    results = common.ohr.plain_path(project / "docs/test/ocb_preset/results/bulk_archive", beneath=project)
    common.ohr.plain_path(project / "docs/test/ocb_preset/results/native_download_plan.json", beneath=project)
    source = common.source_record(root, "ocb", ["hosted_files.json", "ocb_qna_data.parquet", "ocb_file_sources.parquet"])
    try:
        plan, candidates = bulk.load_plan(project)
        bulk.validate_metadata(project, plan)
    except (ValueError, OSError, KeyError, TypeError):
        raise common.BenchmarkError("SOURCE") from None
    if plan["source"]["revision"] != source["revision"]:
        raise common.BenchmarkError("SOURCE")
    report_path = results / "archive_report.json"
    report = common.json_file(report_path, project) if report_path.exists() else None
    if report is not None and (not isinstance(report, dict) or report.get("dataset") != "OCB"
            or report.get("revision") != source["revision"] or report.get("plan_sha256") != plan["plan_sha256"]
            or not isinstance(report.get("records"), list)):
        raise common.BenchmarkError("SOURCE")
    receipts = {}
    for record in (report or {}).get("records", []):
        if not isinstance(record, dict) or not isinstance(record.get("reference_file"), str):
            raise common.BenchmarkError("SCHEMA")
        if record["reference_file"] in receipts:
            raise common.BenchmarkError("SOURCE")
        receipts[record["reference_file"]] = record
    assets, excluded = {}, Counter()
    for item in candidates:
        name = item["reference_file"]
        receipt = receipts.get(name)
        try:
            path = common.ohr.plain_path(bulk.safe_local_path(root, item), beneath=root)
            if not path.is_file():
                excluded["not_downloaded"] += 1
                continue
            # 外部原件没有官方文件摘要时必须有下载回执；HF 可直接核对固定 Tree。
            if item["source_kind"] == "external" and (not receipt or receipt.get("status") not in bulk.STATUS_OK):
                excluded["external_receipt_missing"] += 1
                continue
            if receipt and (receipt.get("status") not in bulk.STATUS_OK
                    or receipt.get("local_file") != item["local_file"]
                    or receipt.get("source_url") != item["source_url"]
                    or receipt.get("technical_source_verified") is not True):
                excluded["receipt_conflict"] += 1
                continue
            size = path.stat().st_size
            if not 0 < size <= 20 * common.MIB or (receipt and receipt.get("size_bytes") != size):
                excluded["size_or_receipt_mismatch"] += 1
                continue
            assets[name] = {"candidate": item, "receipt": receipt, "size_bytes": size,
                            "file_type": item["file_type"], "path": path}
        except (common.ohr.PreparationError, ValueError, OSError, KeyError, TypeError):
            excluded["unsafe_or_missing_file"] += 1
    # Parquet 是官方真值，JSON 导出只是可复用副本；逐行核对，不能相信被修改的 QA。
    rows = common.rows_file(root / "ocb_qna_data.parquet")
    groups = plan["qa_groups"]
    if len(groups) != len(rows):
        raise common.BenchmarkError("SOURCE")
    by_index = {}
    for group in groups:
        if not isinstance(group, dict):
            raise common.BenchmarkError("SCHEMA")
        index = group.get("source_row_index")
        if type(index) is not int or not 0 <= index < len(rows) or index in by_index:
            raise common.BenchmarkError("SCHEMA")
        original = rows[index]
        if (not isinstance(original, dict) or group.get("reference_files") != original.get("reference_files")
                or group.get("track") != original.get("track")):
            raise common.BenchmarkError("SOURCE")
        by_index[index] = group
    export_state, export_hashes = {}, {}
    for name in ("qa_full.json", "qa_smoke.json"):
        path = results / name
        if not path.exists():
            export_state[name] = "missing_rebuilt_from_local_parquet_in_smoke_run"
            continue
        values = common.json_file(path, project)
        if not isinstance(values, list):
            raise common.BenchmarkError("SCHEMA")
        seen = set()
        for value in values:
            index = value.get("source_row_index") if isinstance(value, dict) else None
            if type(index) is not int or index not in by_index or index in seen:
                raise common.BenchmarkError("SCHEMA")
            seen.add(index)
            original, group = rows[index], by_index[index]
            if (value.get("original_qa") != original or value.get("qa_id") != group["qa_id"]
                    or any(value.get(key) != original.get(key) for key in
                           ("question", "expected_assertions", "reference_files", "track"))):
                raise common.BenchmarkError("SOURCE")
        export_state[name] = "validated_against_official_parquet"
        export_hashes[name] = hashlib.sha256(common.read_bytes(path, beneath=project)).hexdigest()
    references = []
    for index, original in enumerate(rows):
        names = original.get("reference_files")
        if (not isinstance(names, list) or not names or not all(isinstance(name, str) and name in assets for name in names)):
            excluded["qa_dependencies_incomplete"] += 1
            continue
        try:
            row = official_reference(original, index, source["metadata_sha256"]["ocb_qna_data.parquet"])
        except common.BenchmarkError:
            excluded["qa_schema_or_track"] += 1
            continue
        references.append(row | {"qa_id": by_index[index]["qa_id"], "source_row_index": index})
    return root, assets, references, source | {"archive_report_present": report is not None,
        "qa_exports": export_state, "qa_export_sha256": export_hashes,
        "official_qa_count": len(rows), "locally_available_qa_count": len(references),
        "local_file_counts": dict(Counter(a["file_type"] for a in assets.values())),
        "exclusions": dict(excluded), "cloud_permission": "not_granted"}


def verified_asset(root, name, metadata):
    """重检入选原件容器/CRC/官方摘要及下载回执；SHA 不冒充版权或云外传授权。"""
    item, receipt = metadata["candidate"], metadata["receipt"]
    tree = item.get("official_tree_object") if item["source_kind"] == "hf" else None
    if item["source_kind"] == "hf":
        checked_tree = ocb.hosted_index([tree])
        if (name not in checked_tree or checked_tree[name]["path"] != item["local_file"]
                or checked_tree[name]["size"] != item.get("size_bytes")):
            raise common.BenchmarkError("SOURCE")
    try:
        sha = bulk.inspect_office(metadata["path"], item["file_type"], item.get("size_bytes"))
    except (ValueError, OSError, KeyError, TypeError):
        raise common.BenchmarkError("FORMAT") from None
    if receipt and receipt.get("sha256") != sha:
        raise common.BenchmarkError("SOURCE")
    if tree is not None:
        lfs = tree.get("lfs") or {}
        expected = lfs.get("sha256", lfs.get("oid"))
        actual = sha if expected else bulk.git_blob_oid(metadata["path"])
        if (expected or tree["oid"]).removeprefix("sha256:") != actual:
            raise common.BenchmarkError("SOURCE")
    asset = common.asset(root, item["local_file"], "ocb", 20 * common.MIB)
    if asset["sha256"] != sha:
        raise common.BenchmarkError("SOURCE")
    return asset | {"source_identity": name, "source_kind": item["source_kind"],
        "checksum_method": "official_hf_tree" if item["source_kind"] == "hf" else "local_receipt_sha256_not_official_hash",
        "cloud_transfer_allowed": False}


def choose_local_samples(root, assets, references, *, per_format, seed, qa_per_file, existing_hashes=()):
    """固定种子先选单文件 QA，再尝试完整多文件组；不超三格式配额、不拆 QA 依赖。"""
    selected, verified, rejected, features = {}, {}, Counter(), set()
    remaining = list(references)
    hashes = set(existing_hashes)
    while remaining and any(sum(a["file_type"] == kind for a in selected.values()) < per_format for kind in bulk.FORMATS):
        counts = Counter(a["file_type"] for a in selected.values())
        ranked = []
        for row in remaining:
            fresh = [name for name in row["reference_files"] if name not in selected]
            after = counts + Counter(assets[name]["file_type"] for name in fresh)
            if not fresh or any(after[kind] > per_format for kind in bulk.FORMATS):
                continue
            ranked.append(((len(row["reference_files"]) > 1,
                ocb.fidelity_subtype(row["track"]) is None, -len(fresh),
                str(row.get("feature")) in features, sum(assets[name]["size_bytes"] for name in fresh),
                common.ranked(seed, row["reference_id"])), row))
        if not ranked:
            break
        _, row = min(ranked, key=ocb.score_key)
        remaining.remove(row)
        fresh = [name for name in row["reference_files"] if name not in selected]
        try:
            checked = {}
            for name in fresh:
                if name not in verified:
                    verified[name] = verified_asset(root, name, assets[name])
                checked[name] = verified[name]
            new_hashes = [a["sha256"] for a in checked.values()]
            if len(new_hashes) != len(set(new_hashes)) or hashes.intersection(new_hashes):
                raise common.BenchmarkError("SOURCE")
        except common.BenchmarkError as exc:
            if exc.code == "DEPENDENCY":
                raise
            rejected["file_integrity_or_duplicate"] += 1
            bad = set(fresh)
            remaining = [r for r in remaining if not bad.intersection(r["reference_files"])]
            continue
        selected.update(checked)
        hashes.update(new_hashes)
        features.add(str(row.get("feature")))
    eligible = [r for r in references if set(r["reference_files"]).issubset(selected)]
    chosen, used = [], set()
    for name in sorted(selected):
        rows = sorted((r for r in eligible if name in r["reference_files"]),
            key=lambda r: (len(r["reference_files"]) > 1, ocb.fidelity_subtype(r["track"]) is None,
                           common.ranked(seed, r["reference_id"])))
        for row in rows[:qa_per_file]:
            if row["reference_id"] not in used:
                chosen.append(row | {"dependencies": [selected[n] for n in row["reference_files"]]})
                used.add(row["reference_id"])
    return selected, chosen, {"format_shortfall": {kind: max(0, per_format - sum(
        a["file_type"] == kind for a in selected.values())) for kind in bulk.FORMATS},
        "exclusions": dict(rejected)}


def prepare(project, *, per_format=2, qa_per_file=4, seed=42, existing_hashes=()):
    """返回六份左右 Office 元数据、完整 QA 与来源报告；由主工具独占发布到新 run。"""
    root, assets, references, source = load_local_archive(project)
    selected, chosen, gaps = choose_local_samples(root, assets, references,
        per_format=per_format, qa_per_file=qa_per_file, seed=seed, existing_hashes=existing_hashes)
    return root, selected, chosen, source, gaps, references
