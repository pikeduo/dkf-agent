"""阶段32固定本地数据提取；仅OHR/OCB，默认不写入，不连接业务服务或云端。"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import importlib.util
import io
import json
import math
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_common as common
from scripts import ocb_bulk_archive as bulk
from scripts import prepare_ohr_bench_preset as ohr
from scripts.stage16_ocb import official_reference

TARGET = 100
SEED = 42
MAX_BYTES = 200 * 1024**2


class Stage32Error(ValueError):
    """仅传播固定错误码，失败时不回显问题、答案、来源URL或底层异常。"""


def encoded(value):
    """严格UTF-8 JSON，保留全部官方字段，拒绝非有限数值。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def sha_file(path):
    """流式计算文件SHA256；不提取正文、不加载模型。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


def relative(path):
    """只保存项目相对路径，最终文件路径统一使用正斜杠。"""
    return path.relative_to(ROOT).as_posix()


def read_json(path):
    """安全只读元数据；路径不能经过链接或Windows Junction。"""
    path = ohr.plain_path(path, beneath=ROOT)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_new(path, payload):
    """仅在新运行目录独占发布文件；既有文件绝不覆盖，失败不自动删除。"""
    ohr.plain_path(path, beneath=ROOT / "data/dataset")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def csv_bytes(rows, fields):
    """生成UTF-8 CSV；按标准转义原始身份，不改写文件名或字段。"""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def rubric_status(original):
    """只认官方完整加权断言；复杂Domain任务单列待审，不算正式可评分题。"""
    assertions, weights = original.get("expected_assertions"), original.get("weights")
    if not isinstance(assertions, list) or not assertions or any(not isinstance(x, str) or not x.strip() for x in assertions):
        return "missing_assertion_basis"
    if (not isinstance(weights, list) or len(weights) != len(assertions)
            or any(type(w) not in (int, float) or not math.isfinite(w) or w < 0 for w in weights)
            or not math.isfinite(sum(weights)) or sum(weights) <= 0):
        return "invalid_assertion_weights"
    if original.get("track") == "Domain":
        return "domain_task_requires_manual_rag_suitability_review"
    return None


def source_record(dataset, identity, path, sha, size, *, source, pages=None, method=""):
    """冻结稳定文件标识和原始身份；source为唯一原件位置，不改变数据集原命名。"""
    return {"dataset": dataset, "file_id": str(uuid5(NAMESPACE_URL, f"dkf:stage32:{dataset}:{identity}:{sha}")),
            "source_identity": identity, "source_path": source, "file_path": relative(path),
            "file_type": path.suffix.lower().lstrip("."), "size_bytes": size, "sha256": sha,
            "page_count": pages, "checksum_method": method}


def ranked(identity):
    """QA数量相同才用固定种子和真实身份摘要裁决，不人为调整格式比例。"""
    return hashlib.sha256(encoded([SEED, identity])).hexdigest(), identity


def ohr_inputs():
    """先读完整官方QA，再精确匹配ZIP身份；核对原始QA和归档的固定下载回执。"""
    base = ROOT / "docs/test/ohr_bench_preset/source"
    qa_path, zip_path = base / "qas_v2.json", base / "pdfs.zip"
    raw = read_json(qa_path)
    groups, rejected = ohr.qa_rows(raw)
    info = read_json(base / "dataset_info.json")
    tree = read_json(base / ".download/tree.json")
    entry = next(x for x in tree["entries"] if x.get("path") == "pdfs.zip" and x.get("type") == "file")
    expected = (entry.get("lfs") or {}).get("sha256", (entry.get("lfs") or {}).get("oid", "")).removeprefix("sha256:")
    zip_sha = sha_file(zip_path)
    if (info.get("id") != "opendatalab/OHR-Bench" or tree.get("revision") != info.get("sha")
            or zip_path.stat().st_size != entry.get("size") or zip_sha != expected):
        raise Stage32Error("OHR_ARCHIVE_HASH")
    receipts = [read_json(p) for p in (base / ".download").glob("*.json")]
    receipt = next(x for x in receipts if x.get("spec", {}).get("local") == "qas_v2.json")
    spec, qa_bytes = receipt["spec"], qa_path.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(qa_bytes)).encode("ascii") + b"\0" + qa_bytes).hexdigest()
    if (spec.get("repo") != "github:opendatalab/OHR-Bench" or spec.get("git_oid") != blob
            or spec.get("size") != len(qa_bytes)):
        raise Stage32Error("OHR_QA_HASH")
    source = {"repository": info["id"], "dataset_revision": info["sha"], "qa_revision": spec["revision"],
              "metadata_file": relative(qa_path), "metadata_sha256": hashlib.sha256(qa_bytes).hexdigest(),
              "archive_sha256": zip_sha}
    return raw, groups, rejected, zip_path, source


def prepare_ohr(run, exclusions, reserves):
    """从有合格QA的身份开始验证成员CRC/真实PDF页树，再选100份不同内容的PDF。"""
    raw, groups, rejected, archive_path, provenance = ohr_inputs()
    exclusions.extend({"dataset": "ohr", "reason": reason, "count": count} for reason, count in rejected.items())
    documents, questions, hashes = [], [], set()
    row_indexes = {q["ID"]: i for i, q in enumerate(raw)}
    with ZipFile(archive_path) as archive:
        pdfs, _ = ohr.archive_index(archive)
        pool = []
        for identity, qas in groups.items():
            matches = [name for name in pdfs if name == identity + ".pdf" or name.endswith("/" + identity + ".pdf")]
            if len(matches) != 1:
                exclusions.append({"dataset": "ohr", "identity": identity, "reason": "missing_file" if not matches else "ambiguous_mapping", "count": len(qas)})
                continue
            pool.append((identity, matches[0], qas))
        pool.sort(key=lambda item: (-len(item[2]), ranked(item[0])))
        for identity, member, qas in pool:
            if len(documents) == TARGET:
                reserves.append({"dataset": "ohr", "source_identity": identity, "source_path": relative(archive_path) + "!/" + member,
                                 "qa_count": len(qas), "status": "metadata_candidate_not_copied_or_member_validated"})
                continue
            data = ohr.read_member(archive, member, MAX_BYTES)  # 完整读取触发成员CRC，不解压其他PDF。
            sha = hashlib.sha256(data).hexdigest()
            pages = ohr.pdf_metadata(data, 10000)  # 只查页树/页面几何，不抽正文、渲染或OCR。
            valid = [q for q in qas if all(page < pages for page in q["pages"])]
            for q in qas:
                if q not in valid:
                    exclusions.append({"dataset": "ohr", "qa_id": q["original"]["ID"], "identity": identity,
                                       "reason": "evidence_page_out_of_real_pdf_range", "count": 1})
            if not valid or sha in hashes:
                exclusions.append({"dataset": "ohr", "identity": identity, "reason": "no_valid_qa" if not valid else "duplicate_content", "count": len(valid)})
                continue
            destination = run / "documents/ohr" / ohr.safe_name(identity + ".pdf")
            doc = source_record("ohr", identity, destination, sha, len(data), source=relative(archive_path) + "!/" + member,
                                pages=pages, method="official_archive_sha256;member_crc32;local_sha256;pdf_page_tree")
            doc["zip_member"], doc["crc32"] = member, f"{pdfs[member].CRC:08x}"
            documents.append(doc)
            hashes.add(sha)
            for row in valid:
                original = row["original"]
                questions.append(qa_record("ohr", original["ID"], original["questions"], original["answers"], None,
                    [doc], {"official_0based": list(row["pages"]), "system_1based": [p + 1 for p in row["pages"]]},
                    provenance | {"source_row_index": row_indexes[original["ID"]]}, original))
            if len(documents) % 10 == 0:
                print(f"OHR完整性预检：{len(documents)}/100份；完整官方QA={len(questions)}。", flush=True)
    if len(documents) != TARGET:
        raise Stage32Error("OHR_DOCUMENT_SHORTFALL")
    return documents, questions, provenance


def ocb_inputs(exclusions):
    """复用归档身份/原始QA适配；完整依赖和评分依据检查先于文档选择。"""
    base = ROOT / "docs/test/ocb_preset/source"
    plan, candidates = bulk.load_plan(ROOT)
    bulk.validate_metadata(ROOT, plan)
    source = common.source_record(base, "ocb", ["hosted_files.json", "ocb_qna_data.parquet", "ocb_file_sources.parquet"])
    report = read_json(ROOT / "docs/test/ocb_preset/results/bulk_archive/archive_report.json")
    if report.get("revision") != source["revision"] or report.get("plan_sha256") != plan["plan_sha256"]:
        raise Stage32Error("OCB_REPORT_IDENTITY")
    receipts = {r["reference_file"]: r for r in report["records"]}
    if len(receipts) != len(report["records"]):
        raise Stage32Error("OCB_AMBIGUOUS_RECEIPT")
    assets, paths = {}, set()
    for candidate in candidates:
        identity = candidate["reference_file"]
        path = ohr.plain_path(bulk.safe_local_path(base, candidate), beneath=base)
        receipt = receipts.get(identity)
        if not path.is_file():
            continue
        if (not receipt or receipt.get("status") not in bulk.STATUS_OK or receipt.get("technical_source_verified") is not True
                or receipt.get("local_file") != candidate["local_file"] or receipt.get("source_url") != candidate["source_url"]):
            raise Stage32Error("OCB_RECEIPT_IDENTITY")
        key = str(path).casefold()
        if identity in assets or key in paths:
            raise Stage32Error("OCB_AMBIGUOUS_FILE")
        paths.add(key)
        assets[identity] = {"path": path, "candidate": candidate, "receipt": receipt}
    rows = common.rows_file(base / "ocb_qna_data.parquet")
    groups = {g["source_row_index"]: g for g in plan["qa_groups"]}
    if len(groups) != len(rows):
        raise Stage32Error("OCB_QA_GROUP_IDENTITY")
    valid, pending = [], []
    for index, original in enumerate(rows):
        group = groups[index]
        if group.get("reference_files") != original.get("reference_files") or group.get("track") != original.get("track"):
            raise Stage32Error("OCB_QA_ROW_IDENTITY")
        ref = official_reference(original, index, source["metadata_sha256"]["ocb_qna_data.parquet"])
        names = ref["reference_files"]
        if any(name not in assets for name in names):
            exclusions.append({"dataset": "ocb", "qa_id": ref["reference_id"], "reason": "missing_dependency", "reference_files": names, "count": 1})
            continue
        reason = rubric_status(original)
        if reason:
            pending.append({"qa_id": ref["reference_id"], "source_row_index": index, "reference_files": names,
                            "reason": reason, "assertion_count": len(original.get("expected_assertions") or []),
                            "status": "manual_pending_not_in_formal_qa"})
        else:
            valid.append(ref)
    provenance = {"repository": source["repo"], "dataset_revision": source["revision"],
                  "metadata_file": relative(base / "ocb_qna_data.parquet"),
                  "metadata_sha256": source["metadata_sha256"]["ocb_qna_data.parquet"],
                  "archive_report_sha256": sha_file(ROOT / "docs/test/ocb_preset/results/bulk_archive/archive_report.json")}
    return assets, valid, pending, provenance


def prepare_ocb(run, exclusions, reserves):
    """按完整官方评分QA选择Office原件，逐个验证容器CRC、回执和可用官方摘要。"""
    assets, valid, pending, provenance = ocb_inputs(exclusions)
    by_file = defaultdict(list)
    for ref in valid:
        for name in ref["reference_files"]:
            by_file[name].append(ref)
    # 优先单文件依赖；若后续数据包含多文件题，只按完整依赖组选择，绝不拆题。
    valid.sort(key=lambda q: (len(q["reference_files"]) != 1,
                             -sum(len(by_file[n]) for n in q["reference_files"]), ranked(q["reference_id"])))
    selected = {}
    for ref in valid:
        fresh = [name for name in ref["reference_files"] if name not in selected]
        if not fresh or len(selected) + len(fresh) > TARGET:
            continue
        for identity in fresh:
            asset = assets[identity]
            item, receipt, path = asset["candidate"], asset["receipt"], asset["path"]
            if not 0 < path.stat().st_size <= MAX_BYTES or path.stat().st_size != receipt.get("size_bytes"):
                raise Stage32Error("OCB_SIZE_MISMATCH")
            sha = bulk.inspect_office(path, item["file_type"], receipt["size_bytes"])
            if sha != receipt.get("sha256"):
                raise Stage32Error("OCB_RECEIPT_HASH")
            tree = item.get("official_tree_object")
            if item["source_kind"] == "hf":
                if not isinstance(tree, dict) or tree.get("path") != item["local_file"] or tree.get("size") != path.stat().st_size:
                    raise Stage32Error("OCB_TREE_IDENTITY")
                expected = (tree.get("lfs") or {}).get("sha256", (tree.get("lfs") or {}).get("oid"))
                if (expected.removeprefix("sha256:") != sha if expected else tree.get("oid") != bulk.git_blob_oid(path)):
                    raise Stage32Error("OCB_OFFICIAL_HASH")
            if sha in {d["sha256"] for d in selected.values()}:
                raise Stage32Error("OCB_DUPLICATE_CONTENT")
            destination = run / "documents/ocb" / ohr.safe_name(item["local_file"])
            selected[identity] = source_record("ocb", identity, destination, sha, path.stat().st_size,
                source=relative(path), method="office_zip_crc;official_hf_checksum" if item["source_kind"] == "hf" else "office_zip_crc;local_receipt_sha256_not_official_hash")
        if len(selected) % 10 == 0:
            print(f"OCB完整性预检：{len(selected)}/100份。", flush=True)
        if len(selected) == TARGET:
            break
    if len(selected) != TARGET:
        raise Stage32Error("OCB_DOCUMENT_SHORTFALL")
    questions = []
    for ref in valid:
        if not set(ref["reference_files"]) <= selected.keys():
            exclusions.append({"dataset": "ocb", "qa_id": ref["reference_id"], "reason": "dependencies_not_selected", "reference_files": ref["reference_files"], "count": 1})
            continue
        original = ref["original"]
        questions.append(qa_record("ocb", ref["reference_id"], original["question"], None, original["expected_assertions"],
            [selected[name] for name in ref["reference_files"]], None,
            provenance | {"source_row_index": ref["source_row_index"], "official_qa_id": None}, original))
    for identity in sorted(set(by_file) - selected.keys()):
        reserves.append({"dataset": "ocb", "source_identity": identity, "source_path": relative(assets[identity]["path"]),
                         "qa_count": len(by_file[identity]), "status": "qa_metadata_candidate_not_copied_or_revalidated"})
    return list(selected.values()), sorted(questions, key=lambda q: q["qa_source"]["source_row_index"]), pending, provenance


def qa_record(dataset, identity, question, answer, assertions, documents, evidence, source, original):
    """保留完整官方行及评分依据，Normalized字段只做无损投影，不拼断言或猜答案。"""
    return {"dataset": dataset, "qa_id": identity, "question": question, "expected_answer": answer,
            "expected_assertions": copy.deepcopy(assertions), "source_file_ids": [d["file_id"] for d in documents],
            "source_file_paths": [d["file_path"] for d in documents],
            "source_file_hashes": {d["file_id"]: d["sha256"] for d in documents}, "evidence_page": evidence,
            "qa_source": source, "validation_status": "validated_dependencies_and_official_basis",
            "source_row_index": source["source_row_index"],
            "weights": copy.deepcopy(original.get("weights")), "track": original.get("track"),
            "reference_files": copy.deepcopy(original.get("reference_files")),
            "evidence_source": original.get("evidence_source"), "evidence_context": copy.deepcopy(original.get("evidence_context")),
            "evidence_page_no": copy.deepcopy(original.get("evidence_page_no")),
            "original_record_sha256": hashlib.sha256(encoded(original)).hexdigest(),
            "scoring_mode": "official_answer" if dataset == "ohr" else "official_weighted_assertions",
            "requires_manual_assertion_judgment": dataset == "ocb", "formal_scoring_basis_available": True,
            "original": copy.deepcopy(original)}


def copy_documents(documents, run):
    """仅复制最终200份，独占新文件并对目标重算SHA；源变化/缺件立即失败，不继续冒充成功。"""
    archives = {}
    try:
        for index, doc in enumerate(documents, 1):
            target = ohr.plain_path(ROOT / doc["file_path"], beneath=run / "documents")
            if doc["dataset"] == "ohr":
                archive_path = ROOT / doc["source_path"].split("!/", 1)[0]
                if archive_path not in archives:
                    archives[archive_path] = ZipFile(archive_path)
                payload = ohr.read_member(archives[archive_path], doc["zip_member"], MAX_BYTES)
                if hashlib.sha256(payload).hexdigest() != doc["sha256"]:
                    raise Stage32Error("OHR_SOURCE_CHANGED")
                write_new(target, payload)
            else:
                source = ohr.plain_path(ROOT / doc["source_path"], beneath=ROOT / "docs/test/ocb_preset/source")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024**2)
            if target.stat().st_size != doc["size_bytes"] or sha_file(target) != doc["sha256"]:
                raise Stage32Error("COPIED_FILE_HASH")
            if index % 25 == 0:
                print(f"最终原件复制并核对：{index}/200份。", flush=True)
    finally:
        for archive in archives.values():
            archive.close()


def publish_metadata(run, documents, questions, exclusions, reserves, pending, provenance):
    """独占写出QA、双向映射和来源快照；所有QA路径均指向本次真实提取目录。"""
    counts = Counter(file_id for q in questions for file_id in q["source_file_ids"])
    fields = ["dataset", "file_id", "source_identity", "source_path", "file_path", "file_type", "size_bytes", "sha256", "page_count", "checksum_method", "qa_count"]
    for dataset in ("ohr", "ocb"):
        docs = [{k: d.get(k) for k in fields if k != "qa_count"} | {"qa_count": counts[d["file_id"]]} for d in documents if d["dataset"] == dataset]
        write_new(run / f"manifests/{dataset}_documents.csv", csv_bytes(docs, fields))
        rows = [q for q in questions if q["dataset"] == dataset]
        write_new(run / f"qa/{dataset}_qa.jsonl", b"".join(encoded(q) + b"\n" for q in rows))
    mappings = [{"dataset": q["dataset"], "qa_id": q["qa_id"], "file_id": fid, "file_path": path,
                 "sha256": q["source_file_hashes"][fid], "dependency_count": len(q["source_file_ids"])}
                for q in questions for fid, path in zip(q["source_file_ids"], q["source_file_paths"])]
    write_new(run / "manifests/qa_file_mapping.csv", csv_bytes(mappings, ["dataset", "qa_id", "file_id", "file_path", "sha256", "dependency_count"]))
    write_new(run / "reports/source_provenance.json", encoded(provenance))
    write_new(run / "reports/exclusions.json", encoded(exclusions))
    write_new(run / "reports/ocb_manual_review_candidates.json", encoded(pending))
    write_new(run / "reports/reserve_documents.csv", csv_bytes(reserves, ["dataset", "source_identity", "source_path", "qa_count", "status"]))


def validate_run(run, documents, questions):
    """重新读取磁盘JSONL/manifest，校验QA→文件、文件→QA、原字段、页码、唯一性和全量Hash。"""
    manifests, disk_questions = [], []
    for dataset in ("ohr", "ocb"):
        with (run / f"manifests/{dataset}_documents.csv").open(encoding="utf-8", newline="") as stream:
            manifests.extend(csv.DictReader(stream))
        with (run / f"qa/{dataset}_qa.jsonl").open(encoding="utf-8") as stream:
            disk_questions.extend(json.loads(line) for line in stream if line.strip())
    if disk_questions != questions or len(manifests) != len(documents):
        raise Stage32Error("MANIFEST_OR_JSONL_COUNT")
    by_id = {d["file_id"]: d for d in documents}
    if len(by_id) != len(documents) or len({d["sha256"] for d in documents}) != len(documents):
        raise Stage32Error("DUPLICATE_FILE_ID_OR_HASH")
    if len({(q["dataset"], q["qa_id"]) for q in questions}) != len(questions):
        raise Stage32Error("DUPLICATE_QA_ID")
    expected_files = {str((ROOT / d["file_path"]).resolve()).casefold() for d in documents}
    actual_files = {str(p.resolve()).casefold() for p in (run / "documents").rglob("*") if p.is_file()}
    if expected_files != actual_files:
        raise Stage32Error("DISK_MANIFEST_FILE_SET")
    for row in manifests:
        doc = by_id.get(row["file_id"])
        if doc is None or any(row[key] != str(doc[key]) for key in ("file_path", "source_identity", "sha256", "size_bytes", "dataset")):
            raise Stage32Error("MANIFEST_IDENTITY")
    if len({(d["dataset"], d["source_identity"]) for d in documents}) != len(documents):
        raise Stage32Error("DUPLICATE_SOURCE_IDENTITY")
    # 原始行重读比较，而非仅相信导出记录内部自洽；官方字段和来源摘要不能丢失/变化。
    ohr_source = ROOT / "docs/test/ohr_bench_preset/source/qas_v2.json"
    ocb_source = ROOT / "docs/test/ocb_preset/source/ocb_qna_data.parquet"
    original_rows = {"ohr": read_json(ohr_source), "ocb": common.rows_file(ocb_source)}
    original_hashes = {"ohr": sha_file(ohr_source), "ocb": sha_file(ocb_source)}
    seen = Counter()
    for q in questions:
        ids = q["source_file_ids"]
        if not ids or len(ids) != len(set(ids)) or len(ids) != len(q["source_file_paths"]):
            raise Stage32Error("QA_DEPENDENCY_SHAPE")
        matched = []
        for fid, path in zip(ids, q["source_file_paths"]):
            doc = by_id.get(fid)
            if (doc is None or doc["dataset"] != q["dataset"] or doc["file_path"] != path
                    or q["source_file_hashes"].get(fid) != doc["sha256"]):
                raise Stage32Error("QA_FILE_LINK")
            matched.append(doc["source_identity"])
            seen[fid] += 1
        original = q["original"]
        index = q["qa_source"]["source_row_index"]
        if (type(index) is not int or not 0 <= index < len(original_rows[q["dataset"]])
                or original != original_rows[q["dataset"]][index]
                or q["qa_source"]["metadata_sha256"] != original_hashes[q["dataset"]]
                or q["original_record_sha256"] != hashlib.sha256(encoded(original)).hexdigest()
                or q["source_row_index"] != index):
            raise Stage32Error("ORIGINAL_QA_FIELDS_OR_SOURCE_CHANGED")
        for key in ("weights", "track", "reference_files", "evidence_source", "evidence_context", "evidence_page_no"):
            if q[key] != original.get(key):
                raise Stage32Error("OFFICIAL_FIELD_LOST")
        if q["dataset"] == "ohr":
            if (original["ID"] != q["qa_id"] or original["questions"] != q["question"]
                    or original["answers"] != q["expected_answer"] or matched != [original["doc_name"]]):
                raise Stage32Error("OHR_OFFICIAL_FIELDS")
            pages = original["evidence_page_no"]
            pages = pages if isinstance(pages, list) else [pages]
            if (q["evidence_page"] != {"official_0based": pages, "system_1based": [p + 1 for p in pages]}
                    or any(not 0 <= p < by_id[ids[0]]["page_count"] for p in pages)):
                raise Stage32Error("OHR_REAL_PAGE_RANGE")
        elif (original["reference_files"] != matched or original["question"] != q["question"]
              or q["expected_assertions"] != original["expected_assertions"] or q["expected_answer"] is not None
              or rubric_status(original) is not None):
            raise Stage32Error("OCB_OFFICIAL_FIELDS_OR_RUBRIC")
    for doc in documents:
        path = ohr.plain_path(ROOT / doc["file_path"], beneath=run / "documents")
        if not seen[doc["file_id"]] or path.stat().st_size != doc["size_bytes"] or sha_file(path) != doc["sha256"]:
            raise Stage32Error("ORPHAN_OR_FINAL_HASH")
    for row in manifests:
        if int(row["qa_count"]) != seen[row["file_id"]]:
            raise Stage32Error("MANIFEST_QA_COUNT")
    with (run / "manifests/qa_file_mapping.csv").open(encoding="utf-8", newline="") as stream:
        mapping = list(csv.DictReader(stream))
    expected_mapping = [{"dataset": q["dataset"], "qa_id": q["qa_id"], "file_id": fid, "file_path": path,
                         "sha256": q["source_file_hashes"][fid], "dependency_count": str(len(q["source_file_ids"]))}
                        for q in questions for fid, path in zip(q["source_file_ids"], q["source_file_paths"])]
    if mapping != expected_mapping:
        raise Stage32Error("MAPPING_ROW_OR_CONTENT")
    return {"status": "PASS", "documents": len(documents), "qas": len(questions),
            "missing_dependency_qas": 0, "hash_failures": 0, "orphan_files": 0,
            "duplicate_qa_ids": 0, "duplicate_file_hashes": 0, "official_fields_preserved": True}


def reports(run, documents, questions, pending, exclusions, check):
    """输出明确区分完整性通过、官方断言人工判分和未执行RAG的报告。"""
    lines = ["# 阶段32 OHR/OCB固定数据提取", "", "仅本地研究使用；无云、模型、业务解析或数据库写入。未执行阶段33～36。", "",
             "| 数据集 | 文件数 | 有效官方QA | 平均QA/文件 | 正式集缺依赖QA | Hash失败 |", "|---|---:|---:|---:|---:|---:|"]
    for dataset in ("ohr", "ocb"):
        ds, qs = [d for d in documents if d["dataset"] == dataset], [q for q in questions if q["dataset"] == dataset]
        lines.append(f"| {dataset} | {len(ds)} | {len(qs)} | {len(qs) / len(ds):.2f} | 0 | 0 |")
    assertion_count = sum(len(q["expected_assertions"]) for q in questions if q["dataset"] == "ocb")
    long_pdfs = [d for d in documents if d["dataset"] == "ohr" and d["page_count"] > 200]
    lines.extend(["", f"入选OCB官方断言共{assertion_count}条，均须逐条人工判断或后续经审核的断言评分；不是本轮已回答正确。",
                  "", f"OHR中超过200页的原件{len(long_pdfs)}份；本地结构可读不等于可整份发送MinerU。保留原件不拆分，后续须另审云端页数/费用限制；本轮无云调用。",
                  "", "## 选择与排除", "", "固定seed=42；先读取完整官方QA及全部依赖，再按可用QA数量降序和稳定身份裁决；不按格式配额凑数。OHR仅保留真实页范围内全部合格QA，multi仍不拆题。候补只记录元数据，不额外复制或声称已经通过成员检查。",
                  "", "OCB保留官方完整expected_assertions/weights/track/reference_files/original，不创造expected_answer。File Fidelity的有效加权断言可供人工逐条正式评分，但自动断言判分尚未实现；不能用字符串包含代替。Domain复杂文件任务及坏评分依据单列待审核，不进正式QA分母。",
                  "", f"未纳入正式QA的OCB待审条目：{len(pending)}题/{sum(p['assertion_count'] for p in pending)}条断言；其原始行身份/原因/断言数见ocb_manual_review_candidates.json。原始QA总排除/候补事件见exclusions.json，不能与正式集0断链混作同一分母。",
                  "", "## 文件名单", ""])
    lines.extend(f"- {d['dataset']} | {d['source_identity']} | {d['file_path']} | {d['sha256']}" for d in documents)
    lines.extend(["", "## 后续读取契约", "", "以项目根目录解析所有source_file_paths；读取qa/ohr_qa.jsonl、qa/ocb_qa.jsonl及manifests中的稳定file_id/SHA。阶段33按这些文件身份、OHR真实页码评召回；阶段34分别用OHR官方答案及OCB官方加权断言评正确率，忠实度/引用单独审核；阶段35不改GT或原件；阶段36另行授权后计时，不从本次提取耗时冒充模型性能。",
                  "", "Omni完全不纳入；不写正式preset/manifest，不改变历史阶段16格式标准。公开数据不授予第三方外传权限；后续上传必须另行确认。", ""])
    write_new(run / "reports/extraction_report.md", "\n".join(lines).encode("utf-8"))
    verification = "# 双向完整性校验\n\n" + "\n".join(f"- {key}: {value}" for key, value in check.items())
    verification += "\n\n已重新读取最终JSONL/CSV；QA全依赖与原字段、真实页范围、200个磁盘文件、全量SHA和每文件至少一题双向一致。只表示数据提取完整，不表示回答正确、解析正确或OCB自动评判已实现。\n"
    write_new(run / "reports/validation_report.md", verification.encode("utf-8"))
    write_new(run / "reports/validation_result.json", encoded(check))


def main(argv=None):
    """必须显式执行和确认本地使用；固定至多100+100，已有run拒绝覆盖，失败保留现场。"""
    # Windows管道和编辑器统一UTF-8，只影响安全计数输出，不改官方数据编码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-local-use", action="store_true")
    parser.add_argument("--run", default="stage32_run_01")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：仅计划OHR/OCB各100份；显式--execute --confirm-local-use才读取成员并写新run，不使用Omni。")
        return 0
    if not args.confirm_local_use or re.fullmatch(r"stage32_run_[0-9]{2,}", args.run) is None:
        print("STAGE32_PERMISSION_OR_RUN")
        return 1
    if any(importlib.util.find_spec(name) is None for name in ("pymupdf", "pyarrow")):
        print("STAGE32_DEPENDENCY：缺PyMuPDF/PyArrow，请用户手动检查环境；未安装或写入。")
        return 1
    run = ohr.plain_path(ROOT / "data/dataset" / args.run, beneath=ROOT / "data/dataset")
    if run.exists():
        print("STAGE32_EXISTS：运行目录已存在，拒绝覆盖；不删除或续写。")
        return 1
    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        print("STAGE32_DISK：可用磁盘不足2GiB，停止，不删除用户数据。")
        return 1
    run.mkdir(parents=True, exist_ok=False)
    exclusions, reserves = [], []
    try:
        ohr_docs, ohr_qa, ohr_source = prepare_ohr(run, exclusions, reserves)
        ocb_docs, ocb_qa, pending, ocb_source = prepare_ocb(run, exclusions, reserves)
        documents, questions = ohr_docs + ocb_docs, ohr_qa + ocb_qa
        if shutil.disk_usage(ROOT).free < sum(d["size_bytes"] for d in documents) + 256 * 1024**2:
            raise Stage32Error("DISK_BUDGET")
        copy_documents(documents, run)
        publish_metadata(run, documents, questions, exclusions, reserves, pending, {"ohr": ohr_source, "ocb": ocb_source})
        check = validate_run(run, documents, questions)
        reports(run, documents, questions, pending, exclusions, check)
        print(f"STAGE32_PASS：文件={len(documents)}，OHR_QA={len(ohr_qa)}，OCB_QA={len(ocb_qa)}，待审OCB={len(pending)}；正式集断链/Hash失败=0；未执行评测。")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, Stage32Error) else "SOURCE_OR_IO_VALIDATION_FAILED"
        # 不自动清理失败目录，不把部分复制/损坏来源当作成功；用户可查看现场后选择新run。
        write_new(run / "reports/failure.json", encoded({"status": "FAIL", "code": code, "exclusions": exclusions,
                                                       "action": "keep_partial_outputs; do_not_use_as_formal_dataset"}))
        print(f"STAGE32_FAIL：{code}；保留现场，禁止将部分结果用于正式分母。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
