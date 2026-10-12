"""用户手动执行的本地质量诊断；只消费已有导出与 GT，不调用数据库、Parser、LLM。"""

import argparse
import hashlib
import importlib.util
import math
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import benchmark_common as common

VERSION = "dkf-offline-diagnostic-v1-not-official-score"


def normalize(text):
    """NFC 与空白归一化，不改数字/公式名称；不冒充官方版本相关的归一化器。"""
    return " ".join(unicodedata.normalize("NFC", text).split())


def formula_normalize(text):
    """仅去常见 LaTeX 外包装与空白，不做代数等价、名称替换或渲染/CDM。"""
    text = unicodedata.normalize("NFC", text).strip()
    for left, right in (("$$", "$$"), ("\\[", "\\]"), ("$", "$"), ("\\(", "\\)")):
        if text.startswith(left) and text.endswith(right) and len(text) >= len(left) + len(right):
            text = text[len(left):-len(right)]
            break
    return re.sub(r"\s+", "", text)


def edit_distance(left, right):
    """有界字符级 Levenshtein；过长输入返回无法评测，而不是截断或编造零分。"""
    if len(left) > 10000 or len(right) > 10000 or len(left) * len(right) > 2_000_000:
        return None
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def conclusion(dataset, identity, reference_id, criterion, status="manual_pending", value=None, **details):
    """每项结论保留原件/官方参考/位置/方法；缺信息不补零，正文只留受控报告。"""
    return {"dataset": dataset, "document_identity": identity, "official_reference_id": reference_id,
            "criterion": criterion, "status": status, "value": value, **details}


def documents_index(export):
    """检查已有导出结构及稳定 Block 身份；只读不纠正未知顺序，不接管业务模型。"""
    documents = export.get("documents")
    if not isinstance(documents, list) or len(documents) > 100:
        raise common.BenchmarkError("SCHEMA")
    result = {}
    for document in documents:
        if (document.get("doc_id") in result or not isinstance(document.get("doc_id"), str)
                or document.get("file_type") not in common.FORMATS
                or re.fullmatch(r"[0-9a-f]{64}", document.get("file_hash", "")) is None
                or not isinstance(document.get("blocks"), list) or len(document["blocks"]) > 100000):
            raise common.BenchmarkError("SCHEMA")
        seen = set()
        for block in document["blocks"]:
            if (not isinstance(block.get("block_id"), str) or block["block_id"] in seen
                    or not isinstance(block.get("text"), str) or type(block.get("page")) is not int
                    or block["page"] < 1):
                raise common.BenchmarkError("SCHEMA")
            seen.add(block["block_id"])
        result[document["doc_id"]] = document
    return result


def document_by_hash(documents, sha):
    """SHA 必须唯一匹配真实导出，不能靠文件名或相似正文猜关联。"""
    matches = [document for document in documents.values() if document["file_hash"] == sha]
    return matches[0] if len(matches) == 1 else None


def chunk_evidence(document, dataset, identity, reference_id):
    """按既有 source_spans 的字符区间/Hash 验证证据链，不宣称问答证据召回率。"""
    result = []
    blocks = {block["block_id"]: block for block in document["blocks"]}
    chunks = document.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return [conclusion(dataset, identity, reference_id, "chunk_source_spans", "not_applicable", reason="chunks_not_exported")]
    for chunk in chunks:
        spans = chunk.get("metadata", {}).get("source_spans")
        if not isinstance(spans, list) or not spans:
            result.append(conclusion(dataset, identity, reference_id, "chunk_source_spans", "not_applicable",
                                     chunk_id=chunk.get("chunk_id"), reason="source_spans_missing"))
            continue
        valid = True
        for span in spans:
            try:
                block = blocks[span["block_id"]]
                start, end = span["start_char"], span["end_char"]
                a, b = span["chunk_start_char"], span["chunk_end_char"]
                valid = valid and (all(type(index) is int for index in (start, end, a, b))
                    and 0 <= start < end <= len(block["text"]) and 0 <= a < b <= len(chunk["text"])
                    and chunk["text"][a:b] == block["text"][start:end]
                    and span["page"] == block["page"] and chunk["page_start"] <= span["page"] <= chunk["page_end"]
                    and span["block_index"] == block["block_index"]
                    and span["block_text_hash"] == hashlib.sha256(block["text"].encode("utf-8")).hexdigest())
            except (KeyError, TypeError, IndexError):
                valid = False
        result.append(conclusion(dataset, identity, reference_id, "chunk_source_spans", "pass" if valid else "fail",
                                 chunk_id=chunk.get("chunk_id"), doc_id=document["doc_id"], method="exact_character_offsets_and_hash"))
    return result


def ohr_checks(references, documents):
    """OHR 仅检查证据字面保留及物理页；答案/表格/公式/顺序仍由人工核实。"""
    result = []
    for row in references:
        reference = row["_ohr_reference"]
        identity, rid = row["doc_name"], row["ID"]
        document = document_by_hash(documents, reference["document_sha256"])
        pages = reference["evidence_pages_1based"]
        context = row.get("evidence_context")
        if document is None:
            result.append(conclusion("ohr", identity, rid, "document_mapping", "not_applicable", reason="missing_or_ambiguous_sha"))
            continue
        selected = [block for block in document["blocks"] if block["page"] in pages]
        full_text = "\n".join(block["text"] for block in selected)
        if document["file_type"] != "pdf" or not isinstance(context, str) or not context.strip():
            status, value, reason = "not_applicable", None, "physical_pdf_or_literal_context_unavailable"
        else:
            value = normalize(context) in normalize(full_text)
            status, reason = "observed" if value else "review_required", "literal_retention_not_answer_accuracy"
        result.append(conclusion("ohr", identity, rid, "evidence_literal_on_official_pages", status, value,
                                 evidence_pages=pages, block_ids=[block["block_id"] for block in selected],
                                 doc_id=document["doc_id"], method="nfc_whitespace_substring", reason=reason))
        for criterion in ("answer_numbers_table_formula", "physical_page_and_reading_order", "important_evidence_in_chunks"):
            result.append(conclusion("ohr", identity, rid, criterion, evidence_pages=pages,
                                     doc_id=document["doc_id"], method="manual_against_original_and_official_qa"))
        result.extend(chunk_evidence(document, "ohr", identity, rid))
    return result


def ocb_checks(references, documents):
    """OCB 原始问答断言逐项人审，所有参考文件同时存在才组成完整检查上下文。"""
    result = []
    for row in references:
        matched = [document_by_hash(documents, item["sha256"]) for item in row["dependencies"]]
        complete = all(document is not None for document in matched)
        identity = row["reference_files"]
        for index, assertion in enumerate(row["expected_assertions"]):
            result.append(conclusion("ocb", identity, row["reference_id"], "expected_assertion",
                "manual_pending" if complete else "not_applicable", assertion_index=index, expected_assertion=assertion,
                doc_ids=[document["doc_id"] for document in matched if document is not None],
                feature=row.get("feature"), method="manual_all_reference_files_no_llm_judge",
                reason=None if complete else "missing_or_ambiguous_dependency"))
        if complete:
            for document in matched:
                result.extend(chunk_evidence(document, "ocb", identity, row["reference_id"]))
    return result


def teds_backend(path):
    """仅用户显式提供可信官方模块时加载 TEDS，记录代码 Hash；不安装依赖或调用全套 pipeline。"""
    if path is None:
        return None, None
    data = common.read_bytes(path, 2 * common.MIB)
    spec = importlib.util.spec_from_file_location("dkf_user_official_teds", common.ohr.plain_path(path))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module.TEDS(n_jobs=1), hashlib.sha256(data).hexdigest()
    except ImportError:
        return None, hashlib.sha256(data).hexdigest()


def table_html(text):
    """仅接受已有结构化单表 HTML；不将 TSV/普通文字猜成表，不补造合并单元格。"""
    if not isinstance(text, str) or len(text) > 100000 or "<!" in text:
        return None
    try:
        root = ET.fromstring(text)
        tables = [root] if root.tag == "table" else list(root.iter("table"))
        if len(tables) != 1 or len(list(tables[0].iter())) > 2000:
            return None
        if not any(cell.tag in {"td", "th"} for cell in tables[0].iter()):
            return None
        # 官方 TEDS 查找 body/table，仅加包装，不改变行列、文本或属性。
        return "<html><body>" + ET.tostring(tables[0], encoding="unicode") + "</body></html>"
    except ET.ParseError:
        return None


def region_checks(row, region, document, match, backend):
    """仅对人工确认的区域对应计算诊断；表格/公式复杂匹配缺失时标记不可评测。"""
    identity, rid = row["reference_id"], region["anno_id"]
    positions = {"gt_page_no": row["source_gt_page_no"], "project_page": 1, "poly": region["poly"]}
    base = {"doc_id": document["doc_id"], "evidence_position": positions, "anno_id": rid}
    blocks = {block["block_id"]: block for block in document["blocks"]}
    ids = match.get("block_ids", []) if match else []
    if (not match or match.get("confirmed") is not True or match.get("doc_id") != document["doc_id"]
            or not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or any(key not in blocks or blocks[key]["page"] != 1 for key in ids)):
        return [conclusion("omnidocbench", identity, rid, "region_mapping", "not_applicable",
                           reason="manual_matching_missing_or_invalid", **base)], None
    selected = [blocks[key] for key in ids]
    text = "\n".join(block["text"] for block in selected)
    result = []
    category = region["category_type"]
    if category in {"text_block", "title", "list_group", "reference", "figure_caption", "table_caption", "header", "footer", "page_number", "code_txt"}:
        gt = region.get("text")
        distance = edit_distance(normalize(gt), normalize(text)) if isinstance(gt, str) and gt else None
        result.append(conclusion("omnidocbench", identity, rid, "text_normalized_edit_distance",
            "measured" if distance is not None else "not_applicable",
            distance / max(len(normalize(gt)), len(normalize(text)), 1) if distance is not None else None,
            method="dkf_nfc_whitespace_levenshtein_not_official_normalizer", block_ids=ids, **base))
        if distance is not None:
            result.append(conclusion("omnidocbench", identity, rid, "text_cer", "measured",
                distance / max(len(normalize(gt)), 1), method="character_edits_over_gt_length", **base))
    elif category == "equation_isolated":
        gt = region.get("latex")
        valid = isinstance(gt, str) and bool(gt.strip()) and len(ids) == 1
        result.append(conclusion("omnidocbench", identity, rid, "formula_normalized_exact",
            "measured" if valid else "not_applicable", formula_normalize(gt) == formula_normalize(text) if valid else None,
            method="literal_latex_not_cdm_or_semantic_equivalence", block_ids=ids, **base))
    elif category == "table":
        gt, pred = table_html(region.get("html")), table_html(text)
        value, reason = None, "html_or_optional_official_teds_unavailable"
        if gt and pred and backend:
            try:
                value = float(backend.evaluate(pred, gt))
                if not math.isfinite(value) or not 0 <= value <= 1:
                    value = None
                reason = None if value is not None else "invalid_metric_result"
            except Exception:
                reason = "table_metric_cannot_evaluate"
        result.append(conclusion("omnidocbench", identity, rid, "table_teds",
            "measured" if value is not None else "not_applicable", value,
            method="official_teds_on_manually_matched_html_not_official_end_to_end_score", reason=reason, block_ids=ids, **base))
    else:
        result.append(conclusion("omnidocbench", identity, rid, "category_content", method="manual_region_category", **base))
    compatible = {"table": {"table"}, "equation_isolated": {"formula"}, "title": {"title", "text", "image_text"}}
    expected = compatible.get(category)
    result.append(conclusion("omnidocbench", identity, rid, "block_category_mapping",
        "observed" if expected else "manual_pending", all(block.get("block_type") in expected for block in selected) if expected else None,
        method="coarse_dkf_category_mapping_not_detection_ap", block_ids=ids, **base))
    # 坐标必须同一像素空间且用户核对；PDF 点数或变换不明时不能套比例计算。
    box = selected[0].get("bbox") if len(selected) == 1 else None
    iou = None
    if (match.get("bbox_verified") is True and match.get("coordinate_space") == "image_pixels"
            and document["file_type"] in {"png", "jpg", "jpeg"} and isinstance(box, list) and len(box) == 4
            and all(type(value) in {int, float} and math.isfinite(value) for value in box)):
        poly = region["poly"]
        gt_box = [min(poly[::2]), min(poly[1::2]), max(poly[::2]), max(poly[1::2])]
        overlap = max(0, min(gt_box[2], box[2]) - max(gt_box[0], box[0])) * max(0, min(gt_box[3], box[3]) - max(gt_box[1], box[1]))
        total = (gt_box[2] - gt_box[0]) * (gt_box[3] - gt_box[1]) + max(0, box[2] - box[0]) * max(0, box[3] - box[1]) - overlap
        iou = overlap / total if total > 0 else None
    result.append(conclusion("omnidocbench", identity, rid, "axis_aligned_bbox_iou",
        "measured" if iou is not None else "not_applicable", iou,
        method="verified_same_image_pixels_not_polygon_iou_or_coco_ap", **base))
    order = region.get("order")
    block_order = selected[0].get("block_index") if len(selected) == 1 else None
    pair = (order, block_order, ids[0]) if type(order) is int and order >= 0 and type(block_order) is int and block_order > 0 else None
    return result, pair


def omni_checks(references, documents, mapping, backend=None):
    """按完整原始页面身份逐区域检查，不根据 bbox 自动匹配或重排；关系复杂时保守跳过。"""
    result = []
    lookup = {}
    for match in mapping.get("matches", []):
        key = (match["reference_id"], str(match["anno_id"]))
        if key in lookup:
            raise common.BenchmarkError("SCHEMA")
        lookup[key] = match
    for row in references:
        identity = row["reference_id"]
        document = document_by_hash(documents, row["document"]["sha256"])
        if document is None:
            result.append(conclusion("omnidocbench", identity, identity, "document_mapping", "not_applicable", reason="missing_or_ambiguous_sha"))
            continue
        pairs, expected = [], 0
        relations = row["original"].get("extra", {}).get("relation", [])
        related = {str(relation[key]) for relation in relations for key in ("source_anno_id", "target_anno_id") if key in relation}
        for region in row["original"]["layout_dets"]:
            if region.get("ignore") or "mask" in region["category_type"] or region["category_type"] == "abandon":
                result.append(conclusion("omnidocbench", identity, region["anno_id"], "ignored_region", "not_applicable", reason="official_ignore_or_mask"))
                continue
            if str(region["anno_id"]) in related or region.get("merge_list"):
                result.append(conclusion("omnidocbench", identity, region["anno_id"], "complex_region_relation", method="manual_merge_or_truncated_region"))
                if type(region.get("order")) is int and region["order"] >= 0:
                    expected += 1
                continue
            checks, pair = region_checks(row, region, document, lookup.get((identity, str(region["anno_id"]))), backend)
            result.extend(checks)
            if type(region.get("order")) is int and region["order"] >= 0:
                expected += 1
            if pair is not None:
                pairs.append(pair)
        ratio = None
        if (len(pairs) == expected and 2 <= expected <= 2000
                and len({pair[0] for pair in pairs}) == expected and len({pair[1] for pair in pairs}) == expected
                and len({pair[2] for pair in pairs}) == expected):
            pairs.sort(key=order_key)
            inversions = sum(pairs[i][1] > pairs[j][1] for i in range(expected) for j in range(i + 1, expected))
            ratio = 1 - inversions / (expected * (expected - 1) / 2)
        result.append(conclusion("omnidocbench", identity, identity, "reading_order_pair_consistency",
            "measured" if ratio is not None else "not_applicable", ratio,
            method="manual_complete_one_to_one_mapping_not_official_read_order_edit_distance"))
        result.extend(chunk_evidence(document, "omnidocbench", identity, identity))
    return result


def order_key(pair):
    """仅为比较排序 GT order，不修改项目 Block 顺序或跨页重排正文。"""
    return pair[0]


def summary(items):
    """仅汇总各指标已测数量/均值及缺口，不混成官方总分，缺失项不进入分母。"""
    groups = defaultdict(list)
    for item in items:
        groups[item["criterion"]].append(item)
    result = {}
    for name, rows in groups.items():
        values = [row["value"] for row in rows if row["status"] == "measured" and isinstance(row["value"], (int, float))]
        result[name] = {"status_counts": dict(Counter(row["status"] for row in rows)),
                        "measured_count": len(values), "mean": sum(values) / len(values) if values else None}
    return result


def main(argv=None):
    """用户命令消费已有本地导出，各基准独立发布报告；不读取原件或加载业务服务。"""
    parser = argparse.ArgumentParser(description="离线 GT 诊断；不连接数据库/Parser/模型/网络")
    parser.add_argument("--dataset", choices=["ohr", "ocb", "omnidocbench"], required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--teds-module", type=Path, help="用户已核对的官方 table_metric.py，可选")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        test_root = common.ROOT / "docs/test"
        references = common.rows_file(common.ohr.plain_path(args.references, beneath=test_root))
        documents = documents_index(common.json_file(common.ohr.plain_path(args.export, beneath=test_root)))
        mapping = common.json_file(common.ohr.plain_path(args.mapping, beneath=test_root)) if args.mapping else {}
        backend, backend_hash = teds_backend(args.teds_module)
        if args.dataset == "ohr":
            items = ohr_checks(references, documents)
        elif args.dataset == "ocb":
            items = ocb_checks(references, documents)
        else:
            items = omni_checks(references, documents, mapping, backend)
        output = common.ohr.plain_path(args.output, beneath=test_root)
        report = {"version": VERSION, "dataset": args.dataset, "items": items, "summary": summary(items),
                  "input_sha256": {"references": hashlib.sha256(common.read_bytes(args.references)).hexdigest(),
                                   "export": hashlib.sha256(common.read_bytes(args.export)).hexdigest(),
                                   "mapping": hashlib.sha256(common.read_bytes(args.mapping)).hexdigest() if args.mapping else None},
                  "teds_module_sha256": backend_hash, "not_evaluated": ["Retrieval", "RAG", "official_benchmark_overall", "cloud_permissions"]}
        common.ohr.publish_exclusive(output, common.ohr.encoded(report) + b"\n")
        print(f"离线报告完成：{len(items)} 条；未执行解析或问答，不代表验收通过。")
        return 0
    except common.BenchmarkError as exc:
        print(exc)
    except Exception:
        print(common.BenchmarkError("IO"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
