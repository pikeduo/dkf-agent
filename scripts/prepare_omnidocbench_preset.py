"""OmniDocBench 原始页面图像与区域 GT 的离线准备；不把图像转换为伪原始 PDF。"""

import math
import sys
from collections import Counter
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import benchmark_common as common

TARGETS = {"text", "table", "formula", "multi_column", "chinese"}


def page_record(row):
    """严格保留 image_path/page_no/anno_id/poly 与关系；不从文件名猜原 PDF 页码。"""
    info, regions = row.get("page_info"), row.get("layout_dets")
    if (not isinstance(info, dict) or not isinstance(regions, list) or not regions
            or type(info.get("page_no")) is not int or info["page_no"] < 0
            or type(info.get("width")) is not int or type(info.get("height")) is not int
            or not 0 < info["width"] * info["height"] <= 40_000_000):
        raise common.BenchmarkError("SCHEMA")
    name = common.ohr.safe_name(info["image_path"]).as_posix()
    ids, kinds = set(), set()
    for region in regions:
        identity, poly = region.get("anno_id"), region.get("poly")
        if (not isinstance(identity, (str, int)) or isinstance(identity, bool) or str(identity) in ids
                or not isinstance(region.get("category_type"), str) or not isinstance(poly, list) or len(poly) != 8
                or any(type(value) not in {int, float} or not math.isfinite(value) for value in poly)
                or any(not 0 <= poly[index] <= info["width" if index % 2 == 0 else "height"] for index in range(8))):
            raise common.BenchmarkError("SCHEMA")
        ids.add(str(identity))
        if not region.get("ignore", False):
            kinds.add(region["category_type"])
    labels = set()
    if kinds & {"text_block", "title", "list_group", "reference"}:
        labels.add("text")
    if "table" in kinds:
        labels.add("table")
    if "equation_isolated" in kinds:
        labels.add("formula")
    attributes = info.get("page_attribute", {})
    if attributes.get("layout") in {"double_column", "three_column", "1andmore_column"}:
        labels.add("multi_column")
    if attributes.get("language") in {"simplified_chinese", "traditional_chinese", "en_ch_mixed", "ch", "chinese", "en_ch"}:
        labels.add("chinese")
    return name, labels


def choose(candidates, count=5, seed=42):
    """优先补齐真实 GT 分类；不同页面不冒充不同原文档，原始归属未知时如实保留。"""
    selected, covered = [], set()
    remaining = list(candidates)
    while remaining and len(selected) < count:
        scored = [((-len(set(item["labels"]) - covered), item["asset"]["size_bytes"],
                    common.ranked(seed, item["identity"])), item) for item in remaining]
        scored.sort(key=score_key)
        item = scored[0][1]
        remaining.remove(item)
        selected.append(item)
        covered.update(item["labels"])
    return selected


def score_key(item):
    """按元数据覆盖、大小和固定种子稳定排序，不改变 GT 区域阅读顺序。"""
    return item[0]


def attach_original_pdfs(root, references, documents, mappings, revision, max_bytes):
    """仅接受用户从官方原件/映射逐项核对的 PDF，整份复用；不拆页、造 PDF 或猜页码。"""
    indexed, verified, unavailable = {}, {}, []
    if not isinstance(mappings, list):
        raise common.BenchmarkError("SCHEMA")
    for mapping in mappings:
        identity = common.ohr.safe_name(mapping["image_path"]).as_posix()
        if identity in indexed:
            raise common.BenchmarkError("SOURCE")
        indexed[identity] = mapping
    for reference in references:
        mapping = indexed.get(reference["reference_id"])
        if mapping is None:
            unavailable.append(reference["reference_id"])
            continue
        if (mapping.get("local_use_confirmed") is not True or mapping.get("official_mapping_confirmed") is not True
                or mapping.get("source_revision") != revision
                or not isinstance(mapping.get("source_mapping_reference"), str) or not mapping["source_mapping_reference"]
                or not isinstance(mapping.get("source_url"), str)
                or not mapping["source_url"].startswith(("https://huggingface.co/datasets/opendatalab/OmniDocBench/",
                                                        "https://github.com/opendatalab/OmniDocBench/"))
                or type(mapping.get("page_number_1based")) is not int or mapping["page_number_1based"] < 1):
            raise common.BenchmarkError("SOURCE")
        path = common.ohr.safe_name(mapping["pdf_file"]).as_posix()
        if not path.startswith("original_pdfs/") or not path.endswith(".pdf"):
            raise common.BenchmarkError("PATH")
        if path not in verified:
            checked = common.asset(root, path, "omnidocbench", max_bytes)
            if checked["sha256"] != mapping.get("pdf_sha256"):
                raise common.BenchmarkError("SOURCE")
            identity = mapping.get("original_pdf_identity")
            if not isinstance(identity, str) or not identity:
                raise common.BenchmarkError("SOURCE")
            if any(item["sha256"] == checked["sha256"] or item["source_identity"] == identity for item in verified.values()):
                raise common.BenchmarkError("SOURCE")
            checked.update(source_identity=identity, provenance=mapping)
            verified[path] = checked
        checked = verified[path]
        if (checked["sha256"] != mapping.get("pdf_sha256")
                or checked["source_identity"] != mapping.get("original_pdf_identity")):
            raise common.BenchmarkError("SOURCE")
        if mapping["page_number_1based"] > checked["page_count"]:
            raise common.BenchmarkError("SOURCE")
        reference["original_pdf"] = checked
        reference["original_pdf_physical_page"] = mapping["page_number_1based"]
        reference["original_pdf_mapping_provenance"] = mapping
        # 此映射仅由用户核对，不是根据原图尺寸推断 PDF 坐标的变换矩阵。
        reference["document"]["original_pdf_identity"] = checked["source_identity"]
        reference["document"]["original_pdf_page"] = mapping["page_number_1based"]
    documents.extend(verified[path] for path in sorted(verified))
    if sum(item["size_bytes"] for item in documents) > 128 * common.MIB:
        raise common.BenchmarkError("LIMIT")
    return unavailable


def prepare(args):
    """实际只检查本地少量原始图像；未提供可验证原 PDF 映射则报告 PDF 缺口。"""
    root = common.ROOT / "docs/test/omnidocbench_preset/source"
    metadata = ["OmniDocBench.json"]
    if (root / "page_sources.json").exists():
        metadata.append("page_sources.json")
    source = common.source_record(root, "omnidocbench", metadata)
    rows = common.json_file(root / "OmniDocBench.json", root)
    if not isinstance(rows, list) or not 0 < len(rows) <= 10000:
        raise common.BenchmarkError("SCHEMA")
    candidates, rejected, seen, hashes = [], [], set(), set()
    ordered = []
    identities = Counter(str(row.get("page_info", {}).get("image_path", "")).casefold()
                         for row in rows if isinstance(row, dict) and isinstance(row.get("page_info"), dict))
    for row in rows:
        try:
            name, labels = page_record(row)
            if name in seen or identities[name.casefold()] != 1:
                raise common.BenchmarkError("SOURCE")
            seen.add(name)
            ordered.append({"identity": name, "labels": sorted(labels), "original": row})
        except Exception:
            rejected.append({"reason": "invalid_or_ambiguous_page_annotation"})
    ordered.sort(key=identity_key)
    # 最多检查 64 个/256 MiB（按最大读取预算计），避免默认读取整个图像集。
    attempted = 0
    while ordered and attempted < min(64, 256 // args.max_mb):
        ranked_items = [((-len(set(item["labels"]) - {label for candidate in candidates for label in candidate["labels"]}),
                         common.ranked(args.seed, item["identity"])), item) for item in ordered]
        ranked_items.sort(key=score_key)
        item = ranked_items[0][1]
        ordered.remove(item)
        attempted += 1
        name = item["identity"]
        relative = name if name.startswith("images/") else "images/" + name
        try:
            checked = common.asset(root, relative, "omnidocbench", args.max_mb * common.MIB)
            info = item["original"]["page_info"]
            if (checked.get("width") != info["width"] or checked.get("height") != info["height"]
                    or checked["sha256"] in hashes):
                raise common.BenchmarkError("SOURCE")
            hashes.add(checked["sha256"])
            checked.update(source_identity=name, project_page=1, gt_page_no=info["page_no"],
                           original_pdf_identity=None, original_pdf_page=None)
            candidates.append(item | {"asset": checked})
        except common.BenchmarkError as exc:
            if exc.code == "DEPENDENCY":
                raise
            rejected.append({"identity": name, "reason": "BENCH_" + exc.code})
        except Exception:
            rejected.append({"identity": name, "reason": "invalid_or_missing_image"})
        if len(candidates) >= args.count and TARGETS <= {label for candidate in candidates for label in candidate["labels"]}:
            break
    selected = choose(candidates, args.count, args.seed)
    references = [{"reference_id": item["identity"], "original": item["original"],
                   "document": item["asset"], "source": source, "labels": item["labels"],
                   "coordinate_space": "image_pixels", "source_gt_page_no": item["original"]["page_info"]["page_no"],
                   "project_document_page": 1} for item in selected]
    documents = [item["asset"] for item in selected]
    unavailable = [reference["reference_id"] for reference in references]
    if "page_sources.json" in metadata:
        unavailable = attach_original_pdfs(root, references, documents, common.json_file(root / "page_sources.json", root),
                                           source["revision"], args.max_mb * common.MIB)
    gaps = {"page_count_shortfall": max(0, args.count - len(selected)),
            "missing_gt_categories": sorted(TARGETS - {label for item in selected for label in item["labels"]}),
            "original_pdf_mapping_unavailable": unavailable,
            "original_document_independence_unverified": bool(unavailable),
            "manual_content_quality_pending": True}
    inventory = [{"identity": item["identity"], "labels": item["labels"], "inspection": "verified"} for item in candidates]
    inventory += [{"identity": item["identity"], "labels": item["labels"], "inspection": "not_inspected", "sha256": None}
                  for item in ordered]
    inventory += rejected
    return common.artifacts("omnidocbench", source, documents, references, inventory, gaps,
                            {"seed": args.seed, "count": args.count, "max_bytes": args.max_mb * common.MIB})


def identity_key(item):
    """按完整 image_path 排序，不通过拆文件名构造 PDF 或页码身份。"""
    return item["identity"]


if __name__ == "__main__":
    sys.exit(common.run("omnidocbench", prepare))
