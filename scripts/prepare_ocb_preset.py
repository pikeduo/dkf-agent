"""OCB 离线分层准备；完整保留多文件 QA，拒绝转换文件及未经审核的原件。"""

import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import benchmark_common as common

TYPES = {"docx", "xlsx", "pptx"}
FIDELITY_TRACKS = {"filefidelity": "legacy", "filefidelityexcel": "Excel",
                   "filefidelitypowerpoint": "PowerPoint", "filefidelityword": "Word"}


def fidelity_subtype(track):
    """显式识别已确认的三种官方子类及旧通用名，拒绝 Domain/未知子类，不猜后缀。"""
    if not isinstance(track, str):
        return None
    return FIDELITY_TRACKS.get(re.sub(r"[ _-]", "", track.casefold()))


def qa_record(row, index, schema, source_hash):
    """只读适配三类官方 File Fidelity/旧通用名；保留原 Track/完整 QA，不改写 Parquet。

    分类先于格式字段校验，Domain/未知分类稳定报告 qa_track；不按后缀猜测子类型。
    """
    if not isinstance(row, dict) or schema not in {"hf", "ndjson"}:
        raise common.BenchmarkError("SCHEMA", stage="qa_rows_schema")
    track = row.get("track")
    subtype = fidelity_subtype(track)
    if subtype is None:
        raise common.BenchmarkError("SOURCE", stage="qa_track")
    if schema == "hf":
        names, question, assertions = row.get("reference_files"), row.get("question"), row.get("expected_assertions")
        identity = "hf_row:" + str(index)
    else:
        names, question, assertions = row.get("filepath"), row.get("query"), row.get("gold")
        identity = row.get("id")
        if not isinstance(identity, (str, int)) or isinstance(identity, bool):
            raise common.BenchmarkError("SCHEMA")
        identity = "ndjson_id:" + str(identity)
    names = [names] if isinstance(names, str) else names
    assertions = [assertions] if isinstance(assertions, str) else assertions
    if (not isinstance(names, list) or not 1 <= len(names) <= 5
            or not all(isinstance(name, str) for name in names) or len(set(names)) != len(names)
            or not isinstance(question, str) or not question.strip()
            or not isinstance(assertions, list) or not assertions
            or not all(isinstance(item, str) and item.strip() for item in assertions)):
        raise common.BenchmarkError("SCHEMA")
    for name in names:
        common.ohr.safe_name(name)
    return {"reference_id": source_hash + ":" + identity, "source_schema": schema,
            "source_row_index": index, "reference_files": names, "question": question,
            "expected_assertions": assertions, "file_format": row.get("file_format", row.get("original_format")),
            "track": track, "track_subtype": subtype, "feature": row.get("feature"), "version": row.get("version"),
            "domain": row.get("domain"), "weights": row.get("weights"), "original": row}


def hosted_index(entries):
    """拒绝字符串文件列表，校验官方对象/大小/OID/LFS，不为旧列表捏造缺失字段。"""
    if not isinstance(entries, list) or len(entries) > 10000:
        raise common.BenchmarkError("SCHEMA", stage="hosted_tree")
    result, folded = {}, set()
    for item in entries:
        if not isinstance(item, dict) or item.get("type") not in {"file", "directory"}:
            raise common.BenchmarkError("SCHEMA", stage="hosted_tree")
        if item["type"] == "directory":
            continue
        if (not isinstance(item.get("path"), str) or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("oid"), str) or re.fullmatch(r"[0-9a-f]{40}", item["oid"]) is None):
            raise common.BenchmarkError("SCHEMA", stage="hosted_tree")
        lfs = item.get("lfs")
        if lfs is not None:
            if (not isinstance(lfs, dict) or lfs.get("size") != item["size"]
                    or not isinstance(lfs.get("sha256", lfs.get("oid")), str)
                    or re.fullmatch(r"[0-9a-f]{64}", lfs.get("sha256", lfs.get("oid")).removeprefix("sha256:")) is None):
                raise common.BenchmarkError("SCHEMA", stage="hosted_tree")
        path = common.ohr.safe_name(item["path"]).as_posix()
        if not path.startswith("reference_files/"):
            continue
        name = path[len("reference_files/"):]
        if name.casefold() in folded:
            raise common.BenchmarkError("SOURCE", stage="hosted_tree")
        folded.add(name.casefold())
        result[name] = item
    return result


def json_input(root, name):
    """读取具名元数据，保留严格编码失败步骤；只显示白名单技术文件名。"""
    try:
        return common.json_file(root / name, root)
    except common.BenchmarkError as exc:
        raise common.BenchmarkError(exc.code, input_id="ocb:" + name, stage=exc.stage) from None


def load_inputs(args, *, verbose=False):
    """逐步校验 source_record/审核表/文件树/QA；不读文档原件，不改官方 JSON 或报告。"""
    root = common.ROOT / "docs/test/ocb_preset/source"
    common.ohr.safe_name(args.qa_file)
    hosted_file = getattr(args, "hosted_file", "hosted_files.json")
    if hosted_file not in {"hosted_files.json", "hosted_files.structured.json"}:
        raise common.BenchmarkError("PATH", stage="hosted_tree")
    names = [args.qa_file, hosted_file, "source_review.json"]
    if (root / "ocb_file_sources.parquet").exists():
        names.append("ocb_file_sources.parquet")
    source = common.source_record(root, "ocb", names)
    if verbose:
        print("source_record=OK：JSON/身份/cardData/输入摘要通过；此步骤尚未校验文件树和 QA 行。")
    reviews = json_input(root, "source_review.json")
    by_name = {}
    if not isinstance(reviews, list):
        raise common.BenchmarkError("SCHEMA", input_id="ocb:source_review.json", stage="review_schema")
    for row in reviews:
        if not isinstance(row, dict) or not isinstance(row.get("reference_file"), str):
            raise common.BenchmarkError("SCHEMA", input_id="ocb:source_review.json", stage="review_schema")
        name = common.ohr.safe_name(row["reference_file"]).as_posix()
        if name in by_name:
            raise common.BenchmarkError("SOURCE", input_id="ocb:source_review.json", stage="review_schema")
        by_name[name] = row
    if verbose:
        print(f"review_schema=OK：审核记录数={len(by_name)}；不代表自动批准许可。")
    try:
        hosted = hosted_index(json_input(root, hosted_file))
    except common.BenchmarkError as exc:
        raise common.BenchmarkError(exc.code, input_id="ocb:" + hosted_file, stage=exc.stage or "hosted_tree") from None
    if verbose:
        print(f"hosted_tree=OK：真实托管文件对象数={len(hosted)}。")
    try:
        rows = common.rows_file(root / args.qa_file)
    except common.BenchmarkError as exc:
        raise common.BenchmarkError(exc.code, input_id="ocb:" + args.qa_file, stage=exc.stage or "qa_read") from None
    if verbose:
        print(f"qa_read=OK：官方 QA 行数={len(rows)}。")
    return root, source, by_name, hosted, rows


def diagnose_inputs(args):
    """只读显示失败发生在哪一步及真实 Track 数量，不生成抽样计划或运行文档解析。"""
    _, source, _, _, rows = load_inputs(args, verbose=True)
    tracks, failures = Counter(), Counter()
    for index, row in enumerate(rows):
        track = row.get("track")
        key = track if fidelity_subtype(track) is not None or isinstance(track, str) and track in {"Domain", "Domain Q&A"} else "unknown_track"
        tracks[key] += 1
        if fidelity_subtype(track) is not None:
            try:
                qa_record(row, index, args.schema, source["metadata_sha256"][args.qa_file])
            except common.BenchmarkError as exc:
                failures[exc.stage or "qa_rows_schema"] += 1
    for track, count in sorted(tracks.items()):
        print(f"QA Track：{track}；数量={count}。")
    if failures:
        print(f"已识别 File Fidelity 中结构失败行数={sum(failures.values())}；未输出 QA 正文。")
        raise common.BenchmarkError("SCHEMA", stage="qa_rows_schema")
    print("输入诊断完成；Domain 未进入候选，不证明原件/许可/解析质量验收通过。")


def manifest_record(row):
    """按官方下载器公开的列别名读取 URL manifest；缺失原格式/转换状态仍安全拒绝。"""
    lowered = {str(key).casefold(): value for key, value in row.items()}
    aliases = {"filename": ("filename", "file_name", "name"), "url": ("url", "source_url", "download_url"),
               "original_format": ("original_format", "source_format", "src_format"),
               "final_format": ("final_format", "required_format", "target_format", "format"),
               "conversion_required": ("conversion_required", "convert", "needs_conversion")}
    return {key: next((lowered[name] for name in options if name in lowered), None) for key, options in aliases.items()}


def reviewed_asset(name, reviews, hosted, root, max_bytes):
    """按用户核对的官方来源取原生 Office，声明不等于自动取得版权或云授权。"""
    kind = Path(name).suffix.lower().lstrip(".")
    review = reviews.get(name)
    if kind not in TYPES:
        raise common.BenchmarkError("FORMAT")
    if (not review or review.get("original_native") is not True or review.get("local_use_confirmed") is not True
            or review.get("conversion_required") is not False or not isinstance(review.get("license"), str)
            or not review["license"].strip() or not isinstance(review.get("source_url"), str)):
        raise common.BenchmarkError("PERMISSION")
    if name in hosted:
        relative = hosted[name]["path"]
        expected_url = "https://huggingface.co/datasets/" + common.REPOS["ocb"] + "/resolve/"
        if not review["source_url"].startswith(expected_url):
            raise common.BenchmarkError("SOURCE")
    else:
        # 未托管记录必须由用户从官方 URL manifest 核对，禁止工具自行下载/转换。
        manifest = [manifest_record(row) for row in common.rows_file(root / "ocb_file_sources.parquet")]
        matches = [row for row in manifest if row["filename"] == name]
        if len(matches) != 1:
            raise common.BenchmarkError("SOURCE")
        source = matches[0]
        original = source["original_format"]
        final = source["final_format"]
        conversion = source.get("conversion_required")
        if (str(original).casefold().lstrip(".") != kind or str(final).casefold().lstrip(".") != kind
                or conversion not in (False, "false", "False", "no", "0", 0)
                or review["source_url"] != source["url"]):
            raise common.BenchmarkError("SOURCE")
        relative = "external_files/" + name
    checked = common.asset(root, relative, "ocb", max_bytes)
    if name in hosted:
        entry = hosted[name]
        if entry.get("size") != checked["size_bytes"]:
            raise common.BenchmarkError("SOURCE")
        lfs = entry.get("lfs") or {}
        official_sha = lfs.get("sha256", lfs.get("oid"))
        if official_sha and official_sha.removeprefix("sha256:") != checked["sha256"]:
            raise common.BenchmarkError("SOURCE")
        if not official_sha:
            data = common.read_bytes(root / relative, max_bytes, root)
            git_blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\x00" + data).hexdigest()
            if entry.get("oid") != git_blob:
                raise common.BenchmarkError("SOURCE")
    checked.update(source_identity=name, provenance=review)
    return checked


def choose(groups, assets, per_format=2, seed=42):
    """贪心覆盖真实三类 Office，完整纳入依赖；重复来源只复用，不能拆 QA 凑六份。"""
    selected, references, features = {}, [], set()
    remaining = list(groups)
    while remaining:
        counts = Counter(item["file_type"] for item in selected.values())
        if all(counts[kind] >= per_format for kind in TYPES):
            break
        scores = []
        for row in remaining:
            fresh = [assets[name] for name in row["reference_files"] if name not in selected]
            gain = sum(counts[item["file_type"]] < per_format for item in fresh)
            scores.append(((-gain, -(str(row.get("feature")) not in features),
                            sum(item["size_bytes"] for item in fresh), common.ranked(seed, row["reference_id"])), row))
        scores.sort(key=score_key)
        row = scores[0][1]
        remaining.remove(row)
        if scores[0][0][0] == 0:
            break
        fresh = {name: assets[name] for name in row["reference_files"] if name not in selected}
        combined = list(selected.values()) + list(fresh.values())
        if len(combined) > 12 or sum(item["size_bytes"] for item in combined) > 128 * common.MIB:
            continue
        selected.update(fresh)
        references.append(row | {"dependencies": [assets[name] for name in row["reference_files"]]})
        features.add(str(row.get("feature")))
    return [selected[name] for name in sorted(selected)], sorted(references, key=reference_key)


def score_key(item):
    """仅排序元数据得分，不按题目文字或随机进程 Hash 选样。"""
    return item[0]


def reference_key(row):
    """参考输出按来源行身份稳定排序，不拆分多文件依赖。"""
    return row["reference_id"]


def prepare(args):
    """读取本地官方 QA/文件表及逐文件许可审核，输出真实可用样本与拒绝清单。"""
    root, source, by_name, hosted, rows = load_inputs(args)
    qa_hash = source["metadata_sha256"][args.qa_file]
    groups, assets, failures, inventory, seen, hashes = [], {}, {}, [], set(), {}
    inspected, byte_budget = 0, 0
    for index, row in enumerate(rows):
        try:
            qa = qa_record(row, index, args.schema, qa_hash)
            if qa["reference_id"] in seen:
                raise common.BenchmarkError("SCHEMA")
            seen.add(qa["reference_id"])
            reason = None
            for name in qa["reference_files"]:
                if name not in assets and name not in failures:
                    if name not in by_name:
                        failures[name] = "source_or_license_review_missing"
                        reason = failures[name]
                        continue
                    if inspected >= 64 or byte_budget + args.max_mb * common.MIB > 256 * common.MIB:
                        failures[name] = "inspection_budget"
                        reason = failures[name]
                        continue
                    inspected += 1
                    byte_budget += args.max_mb * common.MIB
                    try:
                        checked = reviewed_asset(name, by_name, hosted, root, args.max_mb * common.MIB)
                        if checked["sha256"] in hashes and hashes[checked["sha256"]] != name:
                            raise common.BenchmarkError("SOURCE")
                        hashes[checked["sha256"]] = name
                        assets[name] = checked
                    except common.BenchmarkError as exc:
                        if exc.code == "DEPENDENCY":
                            raise
                        failures[name] = "BENCH_" + exc.code
                    except Exception:
                        failures[name] = "invalid_or_missing_asset"
                if name in failures:
                    reason = failures[name]
            inventory.append({"reference_id": qa["reference_id"], "reference_files": qa["reference_files"],
                              "track": qa["track"], "feature": qa["feature"], "reason": reason})
            if reason is None:
                groups.append(qa)
        except common.BenchmarkError as exc:
            if exc.code == "DEPENDENCY":
                raise
            inventory.append({"source_row_index": index, "reason": "not_file_fidelity_or_invalid_schema"})
    documents, references = choose(groups, assets, args.per_format, args.seed)
    counts = Counter(item["file_type"] for item in documents)
    gaps = {"format_shortfall": {kind: args.per_format - counts[kind] for kind in sorted(TYPES)
                                 if counts[kind] < args.per_format},
            "missing_or_rejected_assets": failures, "manual_content_quality_pending": True}
    return common.artifacts("ocb", source, documents, references, inventory, gaps,
                            {"seed": args.seed, "per_format": args.per_format, "max_bytes": args.max_mb * common.MIB})


if __name__ == "__main__":
    sys.exit(common.run("ocb", prepare, diagnose=diagnose_inputs))
