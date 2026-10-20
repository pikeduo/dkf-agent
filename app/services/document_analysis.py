"""阶段35的确定性结构/复杂度/质量评估；只消费当前观测，不修改Block和检索链路。"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from uuid import UUID, uuid5

from app.schemas.document_analysis import (
    AnalysisSource, AssessmentFinding, BlockObservation, ComplexityAssessment, ComplexityFactor,
    DocumentAnalysisReport, OutlineAssessment, OutlineNode, QualityAssessment, TocEntry,
)
from app.services.specialty_errors import SpecialtyError

MAX_BLOCKS = 50000
MAX_HEADINGS = 2000
NUMBERING = re.compile(r"^([1-9]\d{0,2}(?:\.\d{1,3}){0,5})(?:[.．](?!\d)\s*|、\s*|\s+)(\S.*)$")
CHINESE_CHAPTER = re.compile(r"^第[一二三四五六七八九十百零〇\d]+([章节篇部])\s*\S*")
CHINESE_ITEM = re.compile(r"^(?:[一二三四五六七八九十百]+[、．.]|[（(][一二三四五六七八九十百]+[）)])\s*\S+")
TOC_LINE = re.compile(r"^(.+?)(?:\.{2,}|…{2,}|·{3,}|\t+)\s*([0-9ivxlcdmIVXLCDM]{1,8})\s*$")


@dataclass(frozen=True)
class DocumentSnapshot:
    """独立只读快照的文档身份及质量摘要；私有路径仅供明确选页的视觉检查。"""

    kb_id: UUID
    doc_id: UUID
    file_name: str
    file_type: str
    file_hash: str
    processing_task_id: UUID
    status: str
    quality: dict | None
    file_path: str | None = None


def _source(block, text, start=None):
    """保留前缀内真实字符区间及片段Hash，不把前缀Hash称为整Block校验。"""
    start = block.preview.index(text) if start is None else start
    return AnalysisSource(block_id=block.block_id, block_index=block.block_index, page=block.page,
        start_char=start, end_char=start + len(text), quote=text,
        quote_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def _heading(text, kind):
    """优先使用显式编号/Markdown，再用Parser标题；短编号正文仅作为启发式候选。"""
    if not text or len(text) > 200 or "\n" in text or TOC_LINE.fullmatch(text):
        return None
    markdown = re.match(r"^(#{1,6})\s+\S", text)
    if markdown:
        return len(markdown[1]), "markdown", ()
    chinese = CHINESE_CHAPTER.match(text)
    if chinese:
        return (2 if chinese[1] == "节" else 1), "numbering", ()
    chinese = CHINESE_ITEM.match(text)
    if chinese:
        return (2 if text.startswith(("（", "(")) else 1), "numbering", ()
    numbered = NUMBERING.fullmatch(text)
    if numbered and (kind == "title" or len(text) <= 100 and not re.search(r"[。；;！？!?]", text)):
        numbers = tuple(int(item) for item in numbered[1].split("."))
        return len(numbers), "numbering", numbers
    if kind == "title":
        return 1, "parser_title", ()
    return None


def _title_key(text):
    """目录与正文仅按规范空白和显式标题编号比较，不猜相似标题或印刷页映射。"""
    value = text.strip()
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"^第[一二三四五六七八九十百零〇\d]+[章节篇部]\s*", "", value)
    matched = NUMBERING.fullmatch(value)
    if matched:
        value = matched[2]
    value = re.sub(r"^(?:[一二三四五六七八九十百]+[、．.]|[（(][一二三四五六七八九十百]+[）)])\s*", "", value)
    return re.sub(r"\s+", "", value).casefold()


def recover_outline(candidates, toc, *, ordered):
    """原序构建真实父节点；缺级/错号只标记，不能补造缺失标题或连到错误父章。"""
    nodes, stack, previous_numbers = [], [], {}
    for block, text, (declared, basis, numbers) in candidates:
        issues = []
        if not ordered:
            issues.append("ORDER_UNKNOWN")
        if numbers and len(numbers) > 1:
            parent = next((entry for entry in reversed(stack) if entry[1] == numbers[:-1]), None)
            if parent is None:
                issues.append("MISSING_NUMBERED_PARENT")
        else:
            parent = next((entry for entry in reversed(stack) if entry[0].declared_level < declared), None)
        parent_node = parent[0] if parent is not None and ordered else None
        level = min(6, parent_node.level + 1) if parent_node else 1
        if declared > level:
            issues.append("LEVEL_GAP")
        if basis == "parser_title":
            issues.append("UNNUMBERED_LEVEL_UNCERTAIN")
        if numbers:
            key = parent_node.node_id if parent_node else None, numbers[:-1]
            previous = previous_numbers.get(key)
            if previous is not None and numbers[-1] != previous + 1:
                issues.append("DUPLICATE_OR_REVERSED_NUMBER" if numbers[-1] <= previous else "NUMBER_GAP")
            previous_numbers[key] = numbers[-1]
        node = OutlineNode(node_id=uuid5(block.block_id, "document-outline-v1"),
            parent_id=parent_node.node_id if parent_node else None, title=text, level=level,
            declared_level=declared, basis=basis,
            reliability="parser_supported" if block.block_type == "title" else "heuristic",
            issues=issues, source=_source(block, text))
        nodes.append(node)
        # 栈保存真实节点及编号，不为跳级创建占位章节。
        stack = [entry for entry in stack if entry[0].level < level] + [(node, numbers)]
    by_title = defaultdict(list)
    for node in nodes:
        by_title[_title_key(node.title)].append(node.node_id)
    entries = []
    for block, text, matched, start in toc:
        targets = by_title[_title_key(matched[1])]
        entries.append(TocEntry(title=matched[1].strip(), printed_page_hint=matched[2],
            matched_node_id=targets[0] if len(targets) == 1 else None,
            match_status="matched" if len(targets) == 1 else "ambiguous" if targets else "unmatched",
            source=_source(block, text, start)))
    return OutlineAssessment(status="recovered" if nodes else "insufficient_evidence", nodes=nodes,
        toc_entries=entries, limitations=["HEURISTIC_LEVELS_REQUIRE_REVIEW", "FONT_INFORMATION_NOT_STORED",
            "PRINTED_TOC_PAGES_ARE_NOT_SOURCE_PAGES"] + ([] if ordered else ["BLOCK_ORDER_UNKNOWN"]))


def _page_list(quality, name):
    """验证已存页数组；坏元数据不能被默默当成没有缺页。"""
    value = quality.get(name, [])
    if (not isinstance(value, list) or len(value) > 100000
            or any(type(page) is not int or page < 1 for page in value)):
        raise SpecialtyError("ANALYSIS_METADATA")
    return sorted(set(value))


def _column_candidate(boxes):
    """仅提示水平分离且纵向重叠的文本区，多栏/侧注/表格语义仍须人工确认。"""
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            vertical = min(first[3], second[3]) - max(first[1], second[1])
            minimum = min(first[3] - first[1], second[3] - second[1])
            if minimum > 0 and vertical / minimum > .5 and (
                    first[2] + 10 < second[0] or second[2] + 10 < first[0]):
                return True
    return False


def _quality(document, counts, *, order_unknown, regressions, low_confidence, replacement_count):
    """区分覆盖缺口、视觉待审和未知项，不用PARTIAL或视觉无字直接宣称解析失败。"""
    findings = []
    quality = document.quality
    not_assessed = ["OCR_SEMANTIC_ACCURACY", "TABLE_CELL_CORRECTNESS", "FORMULA_SEMANTICS",
                    "VISUAL_BUSINESS_INFORMATION", "SOURCE_BLUR", "SOURCE_SKEW"]
    content = None
    if quality is None:
        findings.append(AssessmentFinding(code="QUALITY_METADATA_ABSENT", severity="info",
            message="历史记录没有解析质量摘要，无法证明整份覆盖完整"))
        not_assessed.append("FULL_DOCUMENT_COVERAGE")
    else:
        content = quality.get("content")
        if content not in {"COMPLETE", "PARTIAL"}:
            raise SpecialtyError("ANALYSIS_METADATA")
        if quality.get("structure") not in {None, "BASE", "ENHANCED", "DEGRADED"} or quality.get("evidence") not in {
                None, "VERIFIED", "PARTIAL"}:
            raise SpecialtyError("ANALYSIS_METADATA")
        required, covered = _page_list(quality, "required_pages"), _page_list(quality, "covered_pages")
        missing = sorted(set(_page_list(quality, "missing_pages")) | (set(required) - set(covered)))
        if missing:
            findings.append(AssessmentFinding(code="MISSING_REQUIRED_PAGES", severity="attention",
                message="已存覆盖要求中存在缺页，不代表可自动重新解析", count=len(missing), pages=missing))
        review = _page_list(quality, "visual_review_pages")
        if review:
            findings.append(AssessmentFinding(code="VISUAL_REVIEW_REQUIRED", severity="review",
                message="这些页有待检查的视觉内容；没有文字不证明缺失业务信息或MinerU失败",
                count=len(review), pages=review))
        for key in ("quarantined_count", "visual_only_count"):
            if type(quality.get(key, 0)) is not int or quality.get(key, 0) < 0:
                raise SpecialtyError("ANALYSIS_METADATA")
        if quality.get("quarantined_count", 0):
            findings.append(AssessmentFinding(code="QUARANTINED_CONTENT", severity="attention",
                message="存在未进入可靠Block的隔离内容，覆盖仍有待核对", count=quality["quarantined_count"]))
        if quality.get("visual_only_count", 0) and not review:
            findings.append(AssessmentFinding(code="VISUAL_ONLY_CONTENT", severity="review",
                message="存在仅视觉对象的观测，是否含额外业务信息仍需检查", count=quality["visual_only_count"]))
        if quality.get("evidence") == "PARTIAL":
            findings.append(AssessmentFinding(code="SOURCE_TRACE_PARTIAL", severity="review",
                message="已存来源质量为部分覆盖，不能据此宣称证据链完整"))
        if quality.get("structure") == "DEGRADED":
            findings.append(AssessmentFinding(code="STRUCTURE_DEGRADED", severity="review",
                message="严格结构增强曾降级，可靠基础内容已保留，表格结构需核对"))
        if content == "PARTIAL":
            findings.append(AssessmentFinding(code="PARSE_PARTIAL", severity="review",
                message="解析保留了部分质量缺口；评估不改变PARTIAL_READY或补造内容"))
        warnings = quality.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
            raise SpecialtyError("ANALYSIS_METADATA")
        if "EXTERNAL_SOURCE_UNVERIFIED" in warnings:
            findings.append(AssessmentFinding(code="EXTERNAL_SOURCE_UNVERIFIED", severity="review",
                message="Office保存快照的外部来源未经完整验证，不自动抓取外部附件或计算公式"))
    for code, amount, message in (
        ("BLOCK_ORDER_UNKNOWN", order_unknown, "历史Block缺少序号，无法确认原阅读顺序"),
        ("PAGE_ORDER_REGRESSION", regressions, "按Block序号观察到页码回退，阅读顺序需核对"),
        ("LOW_REPORTED_CONFIDENCE", low_confidence, "部分上游置信度低于0.6，该数值不是实测OCR准确率"),
        ("REPLACEMENT_CHARACTER", replacement_count, "可见前缀出现替代字符，疑似编码或识别问题"),
    ):
        if amount:
            findings.append(AssessmentFinding(code=code, severity="review", message=message, count=amount))
    if counts["all"] == 0:
        findings.append(AssessmentFinding(code="NO_BLOCKS", severity="attention", message="当前文档没有可评估Block"))
    status = ("attention" if any(item.severity == "attention" for item in findings) else
              "review" if any(item.severity == "review" for item in findings) else
              "unknown" if quality is None else "no_observed_issue")
    return QualityAssessment(status=status, parse_content=content, findings=findings, not_assessed=not_assessed)


def analyze_observations(document, observations):
    """流式累计全部Block统计与有界标题候选；正文只读前缀，未知布局维度保持未知。"""
    if (document.status not in {"READY", "PARTIAL_READY"} or not document.processing_task_id
            or not document.processing_task_id.int):
        raise SpecialtyError("ANALYSIS_UNAVAILABLE")
    counts, pages, boxes, candidates, toc = Counter(), set(), defaultdict(list), [], []
    total_chars = order_unknown = regressions = low_confidence = replacement_count = 0
    previous_page, previous_index, seen_ids = None, None, set()
    observed_hash = hashlib.sha256()
    for raw in observations:
        try:
            block = BlockObservation.model_validate(raw.model_dump() if isinstance(raw, BlockObservation) else raw)
            if (block.block_id.int == 0 or block.text_chars < len(block.preview)
                    or block.block_id in seen_ids):
                raise ValueError
            block.preview.encode("utf-8", errors="strict")
        except Exception:
            raise SpecialtyError("ANALYSIS_METADATA") from None
        seen_ids.add(block.block_id)
        counts["all"] += 1
        if counts["all"] > MAX_BLOCKS:
            raise SpecialtyError("ANALYSIS_LIMIT")
        counts[block.block_type] += 1
        total_chars += block.text_chars
        pages.add(block.page)
        if block.block_index is None:
            order_unknown += 1
        else:
            if previous_index is not None and block.block_index <= previous_index:
                raise SpecialtyError("ANALYSIS_METADATA")
            previous_index = block.block_index
            regressions += int(previous_page is not None and block.page < previous_page)
            previous_page = block.page
        low_confidence += int(block.confidence is not None and block.confidence < .6)
        replacement_count += block.preview.count("\ufffd")
        if block.bbox is not None:
            if block.bbox[0] > block.bbox[2] or block.bbox[1] > block.bbox[3]:
                raise SpecialtyError("ANALYSIS_METADATA")
            counts["bbox"] += 1
            if block.block_type in {"text", "title"} and len(boxes[block.page]) < 32:
                boxes[block.page].append(block.bbox)
        if block.block_type in {"title", "text"}:
            # 不把被截断前缀误认为完整标题；多行Block仍允许逐条读取完整目录行。
            lines = block.preview.splitlines(keepends=True)
            offset = 0
            for index, line in enumerate(lines):
                stripped = line.strip()
                complete = index < len(lines) - 1 or block.text_chars == len(block.preview) or line.endswith(("\n", "\r"))
                if complete and stripped:
                    matched = TOC_LINE.fullmatch(stripped)
                    if matched:
                        toc.append((block, stripped, matched, offset + len(line) - len(line.lstrip())))
                offset += len(line)
            if block.text_chars == len(block.preview):
                heading = _heading(block.preview.strip(), block.block_type)
                if heading:
                    candidates.append((block, block.preview.strip(), heading))
        if len(candidates) + len(toc) > MAX_HEADINGS:
            raise SpecialtyError("ANALYSIS_LIMIT")
        observed_hash.update(json.dumps(block.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, allow_nan=False).encode("utf-8"))
    outline = recover_outline(candidates, toc, ordered=order_unknown == 0)
    columns = sum(_column_candidate(value) for value in boxes.values())
    factors = []
    for code, observed, points, explanation in (
        ("OBSERVED_PAGES", len(pages), 25 if len(pages) >= 200 else 15 if len(pages) >= 50 else 5 if len(pages) >= 10 else 0,
         "已观测页数量是下界，不是原件总物理页数"),
        ("TABLE_BLOCKS", counts["table"], 20 if counts["table"] >= 20 else 8 if counts["table"] else 0,
         "按完整table Block计数，不把一个Block等同于一个单元格或独立整表"),
        ("FORMULA_BLOCKS", counts["formula"], 15 if counts["formula"] >= 10 else 5 if counts["formula"] else 0,
         "仅统计已识别公式块，不含被遗漏公式或整表中的Excel单元格公式"),
        ("IMAGE_TEXT_BLOCKS", counts["image_text"], 10 if counts["image_text"] >= 5 else 3 if counts["image_text"] else 0,
         "图像文字块只是视觉处理信号，不能推断扫描件数量或OCR质量"),
        ("MULTICOLUMN_CANDIDATE_PAGES", columns, 15 if columns else 0,
         "每页最多32个文本bbox作横向分离观测，侧注也可能命中，需人工确认"),
        ("TEXT_VOLUME", total_chars, 15 if total_chars >= 1000000 else 8 if total_chars >= 100000 else 0,
         "全量Block字符数，不是模型Token数"),
    ):
        factors.append(ComplexityFactor(code=code, observed=observed, points=points, explanation=explanation))
    score = min(100, sum(factor.points for factor in factors))
    complexity = ComplexityAssessment(score=score, level="high" if score >= 50 else "medium" if score >= 20 else "low",
        factors=factors, unknown_dimensions=["SOURCE_TOTAL_PAGES", "FONTS", "UNEXTRACTED_OBJECTS"]
            + (["LAYOUT"] if counts["bbox"] < counts["all"] else []))
    quality = _quality(document, counts, order_unknown=order_unknown, regressions=regressions,
        low_confidence=low_confidence, replacement_count=replacement_count)
    semantics = "physical_pdf" if document.file_type == "pdf" else (
        "image_page" if document.file_type in {"png", "jpg", "jpeg"} else "logical")
    identity = json.dumps({"kb_id": str(document.kb_id), "file_name": document.file_name,
        "file_type": document.file_type, "status": document.status,
        "hash": document.file_hash, "parse": str(document.processing_task_id),
        "observations": observed_hash.hexdigest(), "quality": document.quality}, sort_keys=True,
        ensure_ascii=False, allow_nan=False)
    return DocumentAnalysisReport(assessment_id=uuid5(document.doc_id, "document-analysis-v1:" + identity),
        kb_id=document.kb_id, doc_id=document.doc_id, file_name=document.file_name, file_type=document.file_type,
        file_hash=document.file_hash, source_parse_task_id=document.processing_task_id,
        document_status=document.status, page_semantics=semantics, observed_pages=sorted(pages),
        block_count=counts["all"], text_chars=total_chars,
        block_types={kind: counts[kind] for kind in ("title", "text", "table", "formula", "image_text")},
        outline=outline, complexity=complexity, quality=quality)
