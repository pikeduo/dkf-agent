"""MinerU基础内容与可选结构增强分层；只读原ZIP，不调用网络、模型或数据库。"""

import hashlib
import json
import logging
import math
import posixpath
from collections import Counter, defaultdict
from io import BytesIO
from uuid import uuid5
from zipfile import ZipFile

from app.core.business_logging import log_event
from app.core.file_formats import OFFICE_TYPES
from app.schemas.parsed_document import BlockProvenance, ParsedBlock, ParsedDocument, ParseQuality, VisualAsset
from app.services.mineru.errors import MinerUError

NONFATAL = frozenset({"MERGE_EVIDENCE", "EMPTY_BLOCK", "CONTENT_TYPE", "CONTENT_LIST", "BBOX", "PAGE", "NO_TEXT"})
TEXT_TYPES = frozenset({"text", "title", "header", "footer", "page_number", "ref_text", "aside_text", "page_footnote",
                        "list", "code", "equation", "formula", "interline_equation", "image_text"})


def canonical_hash(value):
    """计算原始结构摘要；局部坏坐标仍可留摘要，但绝不进入有效bbox。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=True).encode()).hexdigest()


class BasicContent:
    """先获得逐页基础内容；局部结构问题记录质量，不阻断其他可靠块。"""

    def __init__(self, archive, doc_id, file_name, file_type, geometry, identity):
        """保存一次本地转换的身份和有界输出，不修改原ZIP。"""
        from app.services.mineru.adapter import read_content_list
        from app.services.mineru.raw_results import validate_archive
        validate_archive(archive, 512 * 1024**2)
        self.items = read_content_list(archive, allow_invalid_items=True)
        self.archive, self.doc_id, self.file_name, self.file_type = archive, doc_id, file_name, file_type
        self.geometry, self.identity = geometry, identity
        self.office = file_type in OFFICE_TYPES
        self.blocks, self.quarantine, self.pages_used = [], [], set()
        self.quality, self.characters = ParseQuality(), 0
        self.headings, self.title = {}, ""
        self.visual_seen, self.visual_hashes, self.body_pages = set(), {}, set()
        with ZipFile(BytesIO(archive)) as zipped:
            self.members = {info.filename: info for info in zipped.infolist() if not info.is_dir()}
            self.result_roots = {posixpath.dirname(name) for name in self.members
                                 if posixpath.basename(name) == "layout.json"
                                 or name.endswith("content_list.json")}
        self.uncertain_targets = set()
        previous = None
        for position, value in enumerate(self.items):
            if not isinstance(value, dict):
                previous = None
                continue
            kind = value.get("type") if isinstance(value.get("type"), str) else "unknown"
            if kind in {"text", "table"}:
                if self.v1_text(value).strip():
                    previous = (position, kind, value.get("page_idx"))
                elif previous is not None and previous[1] == kind and previous[2] != value.get("page_idx"):
                    self.uncertain_targets.add(previous[0])
            elif kind not in {"header", "footer", "page_number", "aside_text", "page_footnote", "image", "chart", "code"}:
                previous = None

    def warn(self, reason, *, partial=False):
        """仅接收内部固定原因；重复告警聚合，不记录正文或异常原文。"""
        if reason not in self.quality.warnings and len(self.quality.warnings) < 128:
            self.quality.warnings.append(reason)
        self.quality.structure = "DEGRADED"
        if partial:
            self.quality.content = "PARTIAL"

    def text(self, value):
        """只接受明确文字字段，不把路径、字典或数值元数据强转成正文。"""
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return "\n".join(value)
        self.warn("LOCAL_TEXT_SCHEMA", partial=True)
        return ""

    def quarantine_content(self, text, pointer, origin, reason):
        """未知来源内容有界隔离，不伪造页码或进入检索证据，也不绕过总量限制。"""
        self.characters += len(text)
        if len(self.blocks) + len(self.quarantine) >= 100000 or self.characters > 32_000_000:
            raise MinerUError("CONTENT_LIMIT", "基础内容超过安全处理预算，未发布不完整结果")
        self.quarantine.append({"text": text, "page": None, "pointer": pointer,
                                "origin": origin, "reason": reason})
        self.quality.evidence = "PARTIAL"
        self.warn(reason, partial=True)

    def layout_text(self, node, depth=0):
        """按原子块/行/Span原序读取文字、HTML和原式；不执行公式或严格重建跨页关系。"""
        if not isinstance(node, dict) or depth > 16:
            self.warn("LOCAL_LAYOUT_SCHEMA", partial=True)
            return ""
        children = node.get("blocks")
        if isinstance(children, list) and children:
            return "\n".join(filter(None, (self.layout_text(child, depth + 1) for child in children)))
        if children is not None and not isinstance(children, list):
            self.warn("LOCAL_LAYOUT_SCHEMA", partial=True)
        lines = node.get("lines", [])
        if not isinstance(lines, list):
            self.warn("LOCAL_LAYOUT_SCHEMA", partial=True)
            return ""
        result = []
        for line in lines:
            spans = line.get("spans", []) if isinstance(line, dict) else []
            if not isinstance(spans, list):
                self.warn("LOCAL_SPAN_SCHEMA", partial=True)
                continue
            parts = []
            for span in spans:
                if not isinstance(span, dict):
                    self.warn("LOCAL_SPAN_SCHEMA", partial=True)
                    continue
                kind = span.get("type") if isinstance(span.get("type"), str) else "unknown"
                if kind not in {"text", "table", "image", "chart", "inline_equation", "interline_equation", "equation", "formula"}:
                    self.warn("UNKNOWN_SPAN_TYPE")
                content = self.text(span.get("html") if kind == "table" else span.get("content"))
                if kind in {"inline_equation", "interline_equation", "equation", "formula"} and content:
                    content = "$" + content + "$" if kind == "inline_equation" else content
                if content:
                    parts.append(content)
            result.append("".join(parts))
        return "\n".join(result)

    def record_visuals(self, raw, page_idx, pointer):
        """核对同ZIP原图并记录语义待审页；普通图片不再自动判缺文，图表/缺原图仍保守处理。"""
        candidates = []
        pending = [(raw, 0, pointer)]
        while pending:
            node, depth, node_pointer = pending.pop()
            if not isinstance(node, dict) or depth > 16:
                continue
            kind = node.get("type") if isinstance(node.get("type"), str) else "unknown"
            if kind in {"image", "chart"}:
                ref = node.get("image_path") or node.get("img_path")
                if ref:
                    candidates.append((kind, ref, node, node_pointer))
            children = node.get("blocks", [])
            if isinstance(children, list):
                pending.extend((child, depth + 1, f"{node_pointer}/blocks/{i}")
                               for i, child in reversed(list(enumerate(children))))
            lines = node.get("lines", [])
            if isinstance(lines, list):
                for i, line in reversed(list(enumerate(lines))):
                    spans = line.get("spans", []) if isinstance(line, dict) else []
                    if isinstance(spans, list):
                        pending.extend((span, depth + 1, f"{node_pointer}/lines/{i}/spans/{j}")
                                       for j, span in reversed(list(enumerate(spans))))
        if not candidates and isinstance(raw.get("type"), str) and raw["type"] in {"image", "chart"}:
            candidates.append((raw["type"], None, raw, pointer))
        for kind, ref, node, visual_pointer in candidates:
            if isinstance(node.get("content"), str) and node["content"].strip():
                continue
            marker = (page_idx if type(page_idx) is int else None, visual_pointer)
            if marker in self.visual_seen:
                continue
            self.visual_seen.add(marker)
            self.quality.visual_only_count += 1
            valid_page = type(page_idx) is int and page_idx >= 0 and (
                page_idx < 200 if self.office else isinstance(self.geometry, dict) and page_idx in self.geometry)
            if valid_page and page_idx + 1 not in self.quality.visual_review_pages:
                self.quality.visual_review_pages.append(page_idx + 1)
            matches = set()
            if isinstance(ref, str) and ref and not any(c in ref for c in ("\\", ":", "\x00")):
                if not ref.startswith("/") and ".." not in ref.split("/"):
                    for root in self.result_roots:
                        # layout常用纯图片名，V1常用images/路径；不按任意目录basename模糊匹配。
                        for relative in (ref, "images/" + ref if "/" not in ref else ref):
                            member = posixpath.join(root, relative)
                            if member in self.members:
                                matches.add(member)
            member = next(iter(matches)) if len(matches) == 1 else None
            if member is not None and valid_page and 0 < self.members[member].file_size <= 64 * 1024**2:
                if member not in self.visual_hashes:
                    with ZipFile(BytesIO(self.archive)) as zipped:
                        self.visual_hashes[member] = hashlib.sha256(zipped.read(member)).hexdigest()
                self.quality.visual_assets.append(VisualAsset(page=page_idx + 1, kind=kind,
                    pointer=visual_pointer, member=member, sha256=self.visual_hashes[member]))
                self.warn("VISUAL_SEMANTICS_NOT_EVALUATED", partial=kind == "chart")
            else:
                self.warn("VISUAL_ASSET_UNAVAILABLE", partial=True)

    def v1_text(self, item):
        """按V1已知正文键保留原HTML、公式、附注，不依赖类型枚举完美覆盖。"""
        kind = item.get("type")
        if kind == "table":
            fields = ("table_caption", "table_body", "table_footnote")
        elif kind in ("image", "chart"):
            fields = ("image_caption", "img_caption", "chart_caption", "content", "image_footnote", "img_footnote", "chart_footnote")
        elif kind == "code":
            fields = ("code_caption", "code_body", "code_footnote")
        elif kind == "list":
            fields = ("list_items",)
        else:
            fields = ("text", "content", "html", "latex")
        return "\n".join(filter(None, (self.text(item.get(field)) for field in fields)))

    def emit(self, raw, page_idx, pointer, origin, *, layout=False, size=None):
        """只发布页码可确认的块；局部页码异常隔离，坐标异常设NULL并保留可靠正文。"""
        content = self.layout_text(raw) if layout else self.v1_text(raw)
        kind = raw.get("type") if isinstance(raw.get("type"), str) else "unknown"
        self.record_visuals(raw, page_idx, pointer)
        if not content.strip():
            if kind in {"image", "chart"}:
                self.warn("VISUAL_WITHOUT_TEXT")  # 不生成假文字；整页缺正文在extract/Guard中单独判定。
            elif not (raw.get("lines_deleted") is True or layout and raw.get("lines") == []):
                self.warn("EMPTY_LOCAL_CONTENT", partial=kind in {"table", "equation", "formula", "interline_equation"})
            return
        valid_page = type(page_idx) is int and page_idx >= 0 and (
            page_idx < 200 if self.office else isinstance(self.geometry, dict) and page_idx in self.geometry)
        if not valid_page:
            self.quarantine_content(content, pointer, origin, "UNMAPPED_PAGE")
            return
        self.characters += len(content)
        if len(self.blocks) + len(self.quarantine) >= 100000 or self.characters > 32_000_000:
            raise MinerUError("CONTENT_LIMIT", "基础内容超过安全处理预算，未发布不完整结果")
        from app.services.mineru.adapter import convert_bbox, layout_bbox
        bbox = None
        if not self.office:
            try:
                normalized = layout_bbox(raw, size) if layout else raw.get("bbox")
                bbox = convert_bbox(normalized, self.geometry[page_idx])
            except MinerUError:
                self.warn("BBOX_UNAVAILABLE")  # 不改变已确认的原页和正文。
            if bbox is None:
                self.warn("BBOX_UNAVAILABLE")
                self.quality.evidence = "PARTIAL"
        block_type = ("table" if kind == "table" else "formula" if kind in {"equation", "formula", "interline_equation"}
                      else "image_text" if kind in {"image", "chart", "image_text"} else "text")
        if kind == "table":
            children = raw.get("blocks") if isinstance(raw.get("blocks"), list) else []
            has_body = (any(isinstance(child, dict) and child.get("type") == "table_body"
                            and self.layout_text(child).strip() for child in children) if layout
                        else bool(self.text(raw.get("table_body")).strip()))
            if layout and not children:
                has_body = any(isinstance(span, dict) and span.get("type") == "table" and
                    isinstance(span.get("html"), str) and span["html"].strip()
                    for line in raw.get("lines", []) if isinstance(line, dict)
                    for span in (line.get("spans") if isinstance(line.get("spans"), list) else []))
            if not has_body:
                block_type = "text"
                self.warn("TABLE_BODY", partial=True)
        if kind == "title" or type(raw.get("text_level")) is int and 1 <= raw["text_level"] <= 9:
            block_type = "title"
            level = raw.get("text_level") if type(raw.get("text_level")) is int and 1 <= raw["text_level"] <= 9 else 1
            self.headings = {key: value for key, value in self.headings.items() if key < level}
            self.headings[level] = content
            if not self.title:
                self.title = content
        elif raw.get("text_level") not in (None, 0):
            self.warn("TITLE_LEVEL_UNAVAILABLE")
        elif kind not in TEXT_TYPES | {"table", "image", "chart"}:
            self.warn("UNKNOWN_LOCAL_TYPE")
        confidence = raw.get("confidence")
        if confidence is not None and not (type(confidence) in {int, float} and
                math.isfinite(confidence) and 0 <= confidence <= 1):
            confidence = None
            self.warn("CONFIDENCE_UNAVAILABLE")
        self.blocks.append(ParsedBlock(block_id=uuid5(self.doc_id, f"mineru-v4:{self.identity}:{pointer}"),
            page=page_idx + 1, text=content, block_type=block_type, bbox=bbox, source="mineru_cloud",
            confidence=confidence,
            section=" / ".join(self.headings[key] for key in sorted(self.headings)),
            provenance=BlockProvenance(origin=origin, pointer=pointer, raw_sha256=canonical_hash(raw),
                                       bbox_reliable=bbox is not None)))
        if kind in {"text", "table", "equation", "formula", "interline_equation", "code", "list", "image_text"}:
            self.body_pages.add(page_idx + 1)
        elif kind in {"image", "chart"} and isinstance(raw.get("content"), str) and raw["content"].strip():
            self.body_pages.add(page_idx + 1)

    def extract(self, *, allow_empty_part=False):
        """优先逐页preproc，缺页使用V1；只允许PDF中间片保留无可靠块的部分结果。"""
        if not self.office:
            with ZipFile(BytesIO(self.archive)) as zipped:
                layouts = [info for info in zipped.infolist() if info.filename.replace("\\", "/").split("/")[-1] == "layout.json"]
                pages = []
                if len(layouts) == 1 and layouts[0].file_size <= 64 * 1024**2:
                    try:
                        data = json.loads(zipped.read(layouts[0]).decode("utf-8-sig"))
                        pages = data.get("pdf_info", []) if isinstance(data, dict) else []
                    except (ValueError, UnicodeError):
                        self.warn("LAYOUT_UNAVAILABLE")
                if not isinstance(pages, list):
                    pages = []
                    self.warn("LAYOUT_UNAVAILABLE")
                counts = Counter(p.get("page_idx") for p in pages if isinstance(p, dict) and type(p.get("page_idx")) is int)
                duplicates = {p for p, count in counts.items() if count > 1}
                if duplicates:
                    self.warn("AMBIGUOUS_LAYOUT_PAGE", partial=True)
                for offset, page in enumerate(pages):
                    if not isinstance(page, dict):
                        self.warn("LOCAL_LAYOUT_SCHEMA", partial=True)
                        continue
                    idx, body = page.get("page_idx"), page.get("preproc_blocks")
                    if type(idx) is not int or idx in duplicates or not isinstance(body, list):
                        if body or type(idx) is not int:
                            self.warn("LAYOUT_PAGE_UNAVAILABLE")
                        continue
                    entries = [("preproc_blocks", i, b) for i, b in enumerate(body)]
                    discarded = page.get("discarded_blocks", [])
                    if isinstance(discarded, list):
                        entries += [("discarded_blocks", i, b) for i, b in enumerate(discarded)]
                    indices = [b.get("index") if isinstance(b, dict) else None for _, _, b in entries]
                    if all(type(i) is int and i >= 0 for i in indices) and len(set(indices)) == len(indices):
                        entries.sort(key=lambda e: e[2]["index"])
                    else:
                        self.warn("ORDER_UNVERIFIED")
                    before = len(self.blocks)
                    for name, position, block in entries:
                        if not isinstance(block, dict):
                            self.warn("LOCAL_LAYOUT_SCHEMA", partial=True)
                            continue
                        self.emit(block, idx, f"layout/page/{idx}/{name}/{position}",
                                  "layout_preproc" if name == "preproc_blocks" else "layout_discarded",
                                  layout=True, size=page.get("page_size"))
                    if len(self.blocks) > before or any(isinstance(b, dict) and b.get("type") in {"image", "chart"} for b in body):
                        self.pages_used.add(idx)
        for position, item in enumerate(self.items):
            if not isinstance(item, dict):
                self.warn("LOCAL_ITEM_SCHEMA", partial=True)
                if isinstance(item, str) and item.strip():
                    self.quarantine_content(item, f"content_list/{position}", "content_list", "LOCAL_ITEM_SCHEMA")
                continue
            idx = item.get("page_idx")
            if type(idx) is int and idx in self.pages_used:
                continue
            # 有空占位而无逐页原始来源时，合并正文的跨页归属不可靠，不能伪造精确页命中。
            if not self.office and position in self.uncertain_targets:
                if item.get("type") in {"text", "table"}:
                    text = self.v1_text(item)
                    if text.strip():
                        self.quarantine_content(text, f"content_list/{position}", "content_list", "MERGED_PAGE_UNVERIFIED")
                    continue
            self.emit(item, idx, f"content_list/{position}", "content_list")
        if not self.blocks and not (allow_empty_part and self.file_type == "pdf" and
                (self.quarantine or self.quality.visual_only_count)):
            raise MinerUError("PAGE" if self.quarantine else "NO_TEXT", "没有来源可确认的基础内容，未伪造页码或发布空结果")
        self.quality.covered_pages = sorted({b.page for b in self.blocks})
        self.quality.visual_review_pages.sort()
        # 页码/页眉/图注不能替整页图片证明文字已识别；只有可靠正文页可仅记视觉待审。
        visual_only_pages = sorted(set(self.quality.visual_review_pages) - self.body_pages)
        if visual_only_pages:
            self.warn("VISUAL_ONLY_PAGES", partial=True)
            for page in visual_only_pages[:128]:
                if len(self.quality.diagnostics) < 128:
                    self.quality.diagnostics.append({"reason": "VISUAL_ONLY_PAGE", "stage": "adapter.v4", "page": page})
        self.quality.base_block_count, self.quality.quarantined_count = len(self.blocks), len(self.quarantine)
        return ParsedDocument(doc_id=self.doc_id, file_name=self.file_name, file_type=self.file_type,
            title=self.title, blocks=self.blocks, quality=self.quality, quarantined_content=self.quarantine)


def normalize_content(archive, *, strict, doc_id, file_name, file_type, geometry, identity, allow_empty_part=False):
    """安全基础提取先完成；增强失败返回基础块和降级摘要，不吞掉ZIP/身份致命错误。"""
    basic = BasicContent(archive, doc_id, file_name, file_type, geometry, identity)
    parsed = basic.extract(allow_empty_part=allow_empty_part)
    try:
        enhanced = strict(archive, doc_id=doc_id, file_name=file_name, file_type=file_type,
                          geometry=geometry, identity=identity)
    except MinerUError as error:
        if error.code not in NONFATAL:
            raise
        reason = getattr(error, "diagnostic", {}).get("reason", error.code)
        basic.warn(reason)
        if getattr(error, "diagnostic", None):
            basic.quality.diagnostics.append(dict(error.diagnostic))  # 固定阶段/子码/有界整数，无正文坐标。
    else:
        # 仅在每页完整字符序列相同的情况下使用增强；不能让增强吞掉基础附注或公式。
        before, after = defaultdict(str), defaultdict(str)
        for block in parsed.blocks:
            before[block.page] += "".join(block.text.split())
        for block in enhanced.blocks:
            after[block.page] += "".join(block.text.split())
        if before == after and basic.quality.structure != "DEGRADED":
            parsed = enhanced.model_copy(update={"quality": basic.quality, "quarantined_content": basic.quarantine})
            basic.quality.structure, basic.quality.enhanced = "ENHANCED", True
            parsed.blocks = [b.model_copy(update={"provenance": BlockProvenance(origin="verified_enhancement",
                pointer=f"enhanced/{i}", raw_sha256=hashlib.sha256(archive).hexdigest(),
                bbox_reliable=b.bbox is not None)}) for i, b in enumerate(parsed.blocks)]
        else:
            basic.warn("BASE_CONTENT_PRESERVED")
    log_event("mineru.adapter_content", level=logging.WARNING if basic.quality.warnings else logging.INFO,
              doc_id=doc_id, block_count=len(parsed.blocks), item_count=len(basic.quality.warnings),
              status="PARTIAL" if basic.quality.content == "PARTIAL" else "COMPLETE", stage="adapter.v4")
    return parsed
