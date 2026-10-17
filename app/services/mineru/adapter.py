"""读取 V1 正文并核对布局证据，恢复合并段落的原页/坐标，不承担缓存或落库。"""

import json
import logging
import math
from io import BytesIO
from itertools import pairwise
from operator import itemgetter
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5
from zipfile import BadZipFile, ZipFile

import pymupdf
from pydantic import ValidationError

from app.core.business_logging import log_event
from app.core.file_formats import MINERU_TYPES, OFFICE_TYPES
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.mineru.errors import MinerUError


def page_geometry(path: Path, file_type: str) -> dict[int, tuple[float, float]] | None:
    """PDF/图片取得真实尺寸；Office 无可靠原件分页与坐标映射，明确返回空值。"""

    if file_type in OFFICE_TYPES:
        return None
    if file_type not in MINERU_TYPES:
        raise MinerUError("FILE_TYPE", "该文件类型不支持 MinerU 结果转换")

    try:
        with pymupdf.open(path) as document:
            if file_type == "pdf":
                # 云端归一化坐标对应渲染视口，裁剪 / 旋转后的尺寸不能用 MediaBox 猜测。
                return {
                    index: (float(page.rect.width), float(page.rect.height))
                    for index, page in enumerate(document)
                }
            pixmap = pymupdf.Pixmap(str(path))
            return {0: (float(pixmap.width), float(pixmap.height))}
    except (OSError, ValueError, RuntimeError):
        raise MinerUError("GEOMETRY", "原件页码或页面尺寸无法读取") from None


def read_content_list(archive: bytes) -> list[dict]:
    """只在内存读取唯一内容列表；拒绝路径穿越、超量解压和无结构结果，不执行 ZIP 文件。"""

    try:
        with ZipFile(BytesIO(archive)) as zipped:
            infos = zipped.infolist()
            if (
                len(infos) > 10000
                or sum(info.file_size for info in infos) > 512 * 1024**2
            ):
                raise MinerUError("ZIP_LIMIT", "MinerU 结果 ZIP 解压规模超过安全限制")
            candidates = []
            for info in infos:
                path = PurePosixPath(info.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or ":" in info.filename:
                    raise MinerUError("ZIP_PATH", "MinerU ZIP 包含不安全路径")
                if path.name == "content_list.json" or path.name.endswith(
                    "_content_list.json"
                ):
                    candidates.append(info)
            if len(candidates) != 1 or candidates[0].file_size > 64 * 1024**2:
                raise MinerUError(
                    "CONTENT_LIST", "MinerU ZIP 缺少唯一有效的 content_list.json"
                )
            value = json.loads(zipped.read(candidates[0]).decode("utf-8-sig"))
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                raise MinerUError("CONTENT_LIST", "MinerU content_list 结构不受支持")
            return value
    except (BadZipFile, UnicodeError, ValueError, RuntimeError, NotImplementedError):
        raise MinerUError("ZIP_INVALID", "MinerU ZIP 或内容列表损坏") from None


def joined_text(value: object) -> str:
    """保留实际字符串及字符串列表，不将未知字典、图片路径或空内容当正文。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    raise MinerUError("CONTENT_LIST", "MinerU 文本字段格式不受支持")


def read_layout_pages(archive: bytes) -> dict[int, dict]:
    """内存读取可选的旧版 layout 顺序证据；缺失、歧义或未知结构时不用它改序。

    调用前必须由 read_content_list 完成 ZIP 路径和总解压规模校验。
    layout 不替代正文契约，不读取 V2、Markdown，也不持久化解析缓存。
    """

    try:
        with ZipFile(BytesIO(archive)) as zipped:
            candidates = [
                info
                for info in zipped.infolist()
                if PurePosixPath(info.filename.replace("\\", "/")).name == "layout.json"
            ]
            if len(candidates) != 1 or candidates[0].file_size > 64 * 1024**2:
                return {}
            value = json.loads(zipped.read(candidates[0]).decode("utf-8-sig"))
        pages = value.get("pdf_info") if isinstance(value, dict) else None
        if not isinstance(pages, list):
            return {}
        result = {}
        for page in pages:
            if not isinstance(page, dict):
                return {}
            index = page.get("page_idx")
            if type(index) is not int or index < 0 or index in result:
                return {}
            result[index] = page
        return result
    except (BadZipFile, UnicodeError, ValueError, RuntimeError, NotImplementedError):
        # 正文已经独立校验；可选顺序信息损坏只能退回原序，不能伪造正文或索引。
        return {}


def layout_bbox(block: dict, size: object) -> list[float] | None:
    """将 layout 的页面坐标归一化，仅用于唯一关联，不用于推断阅读顺序。"""

    bbox = block.get("bbox")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number <= 0
            for number in size
        )
        or not isinstance(bbox, list)
        or len(bbox) != 4
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not 0 <= number <= size[index % 2]
            for index, number in enumerate(bbox)
        )
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        return None
    return [number * 1000 / size[index % 2] for index, number in enumerate(bbox)]


def order_page_margins(
    entries: list[tuple[int, dict]], page: dict
) -> list[tuple[int, dict]]:
    """只恢复官方索引明确的页眉/页脚，任何正文重排、复杂版式或歧义均保留原序。

    页内所有原始项必须与 layout 父块按类型和 bbox 唯一对应；坐标容差 2/1000
    仅兼容官方整数取整，不构成按 y 排序。结构块作为整体参与校验，绝不拆分。
    """

    if not any(item.get("type") in {"header", "footer"} for _, item in entries):
        return entries
    groups = [page.get("para_blocks"), page.get("discarded_blocks")]
    if any(not isinstance(group, list) for group in groups):
        return entries
    layout = [block for group in groups for block in group]
    # 复杂或超量页不做启发式关联，限制最坏情况下的两两匹配成本。
    if len(layout) != len(entries) or not 2 <= len(entries) <= 512:
        return entries
    aliases = {"title": "text", "interline_equation": "equation", "formula": "equation"}
    candidates, indices = [], set()
    for block in layout:
        if not isinstance(block, dict):
            return entries
        index = block.get("index")
        bbox = layout_bbox(block, page.get("page_size"))
        kind = block.get("type")
        if (
            type(index) is not int
            or index < 0
            or index in indices
            or bbox is None
            or not isinstance(kind, str)
        ):
            return entries
        indices.add(index)
        candidates.append((index, aliases.get(kind, kind), bbox))
    matched, used, boxes = [], set(), {}
    for position, item in entries:
        try:
            bbox = convert_bbox(item.get("bbox"), (1000, 1000))
        except MinerUError:
            return entries
        kind = item.get("type")
        if bbox is None or not isinstance(kind, str):
            return entries
        matches = [
            index
            for index, candidate_kind, candidate_bbox in candidates
            if candidate_kind == aliases.get(kind, kind)
            and all(abs(a - b) <= 2 for a, b in zip(bbox, candidate_bbox))
        ]
        if len(matches) != 1 or matches[0] in used:
            return entries
        used.add(matches[0])
        matched.append((matches[0], position, item))
        boxes[position] = bbox
    body = [
        (position, item)
        for position, item in entries
        if item.get("type") not in {"header", "footer"}
    ]
    if not body:
        return entries
    # 空图片仍参与唯一关联和页边界检查；版式检测只看实际输出的正文，不能补 OCR。
    visible_boxes = [
        boxes[position]
        for position, item in body
        if item.get("type") not in {"image", "chart"}
        or item.get("content")
        or item.get("image_caption")
        or item.get("image_footnote")
    ]
    for offset, box in enumerate(visible_boxes):
        for other in visible_boxes[offset + 1 :]:
            vertical_overlap = min(box[3], other[3]) - max(box[1], other[1])
            horizontal_gap = max(box[0], other[0]) - min(box[2], other[2])
            if vertical_overlap > 0 and horizontal_gap >= 0:
                return entries
    # 正文发生纵向回跳可能是按栏阅读；即使存在 index，也不改其原始序列。
    if any(left[1] > right[1] for left, right in pairwise(visible_boxes)):
        return entries
    for position, item in entries:
        bbox = boxes[position]
        if item.get("type") == "header" and (
            bbox[3] > 150 or bbox[3] > min(boxes[p][1] for p, _ in body)
        ):
            return entries
        if item.get("type") == "footer" and (
            bbox[1] < 850 or bbox[1] < max(boxes[p][3] for p, _ in body)
        ):
            return entries
    # 使用供应商 index 而非几何位置排序；校验候选结果不能改变任何正文项的相对顺序。
    proposed = [
        (position, item) for _, position, item in sorted(matched, key=itemgetter(0))
    ]
    if [p for p, item in proposed if item.get("type") not in {"header", "footer"}] != [
        p for p, _ in body
    ]:
        return entries
    body_slots = [
        slot
        for slot, (_, item) in enumerate(proposed)
        if item.get("type") not in {"header", "footer"}
    ]
    if any(
        item.get("type") == "header"
        and slot > body_slots[0]
        or item.get("type") == "footer"
        and slot < body_slots[-1]
        for slot, (_, item) in enumerate(proposed)
    ):
        return entries
    return proposed


def ordered_content_items(
    archive: bytes, items: list[dict], *, pages: dict[int, dict] | None = None
) -> list[tuple[int, dict]]:
    """按页恢复经过验证的页边界项，并携带原始数组位置保证 Block ID 不变。

    不改变原有 page_idx 序列，交错页面也只替换该页自己的槽位；不做全局排序。
    """

    ordered = list(enumerate(items, start=1))
    pages = read_layout_pages(archive) if pages is None else pages
    slots = {}
    for slot, (_, item) in enumerate(ordered):
        index = item.get("page_idx")
        if type(index) is int and index in pages:
            slots.setdefault(index, []).append(slot)
    for index, positions in slots.items():
        entries = [ordered[position] for position in positions]
        proposed = order_page_margins(entries, pages[index])
        for slot, entry in zip(positions, proposed):
            ordered[slot] = entry
    return ordered


def convert_bbox(value: object, size: tuple[float, float]) -> list[float] | None:
    """将官方 content_list 的 0～1000 坐标转换为原件点数 / 像素；未知位置保持空值。"""

    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not 0 <= number <= 1000
            for number in value
        )
        or value[2] < value[0]
        or value[3] < value[1]
    ):
        raise MinerUError("BBOX", "MinerU 返回的页面坐标无效")
    return [
        float(number) * size[index % 2] / 1000 for index, number in enumerate(value)
    ]


# 只允许跨越有唯一layout证据的边界元素；正文/标题/表格及未知元素绝不跳过。
MERGE_BOUNDARY_TYPES = frozenset({"header", "footer", "page_number"})
# 诊断子码仅用于内部定位；对外错误码/重试策略/缓存版本和恢复判定均保持不变。
MERGE_REASONS = {
    "LAYOUT_PAGE": "matched_text_layout", "TEXT_LEVEL": "matched_text_layout",
    "ITEM_BBOX": "matched_text_layout", "PARENT_LIST": "matched_text_layout",
    "PARENT_LIMIT": "matched_text_layout", "PARENT_INDEX": "matched_text_layout",
    "PARENT_MATCH": "matched_text_layout", "PARENT_INDEX_MISMATCH": "matched_text_layout",
    "LINES_MISSING": "plain_layout_spans", "LINE_SCHEMA": "plain_layout_spans",
    "LINE_SPAN_COUNT": "plain_layout_spans", "SPAN_TYPE": "plain_layout_spans",
    "SPAN_TEXT": "plain_layout_spans", "TEXT_LIMIT": "merged_text_slices",
    "SOURCE_TEXT": "merged_text_slices", "TEXT_CHARACTER": "merged_text_slices",
    "TEXT_TRAILING": "merged_text_slices", "PAGE_PARENT_LIMIT": "restore_merged_text",
    "PLACEHOLDER_MATCH": "restore_merged_text", "GROUP_NO_TARGET": "restore_merged_text",
    "GROUP_LIMIT": "restore_merged_text", "TARGET_TEXT": "restore_merged_text",
    "TARGET_DELETED": "restore_merged_text", "SOURCE_NOT_DELETED": "restore_merged_text",
    "SOURCE_SPAN_COUNT": "restore_merged_text", "SOURCE_SPAN_BBOX": "restore_merged_text",
    "SOURCE_BBOX_COVERAGE": "restore_merged_text", "TARGET_SPAN_COUNT": "restore_merged_text",
    "SOURCE_PAGE": "restore_merged_text", "MOVED_TEXT": "restore_merged_text",
    "MOVED_CROSS_PAGE": "restore_merged_text", "SOURCE_CROSS_PAGE": "restore_merged_text",
    "MOVED_BBOX": "restore_merged_text",
    "BOUNDARY_LIMIT": "merge_group_indices", "TARGET_AMBIGUOUS": "restore_merged_text",
    "BOUNDARY_LAYOUT": "matched_merge_boundary", "BOUNDARY_MATCH": "matched_merge_boundary",
    "BOUNDARY_INDEX": "matched_merge_boundary", "BOUNDARY_POSITION": "matched_merge_boundary",
    "BOUNDARY_TEXT": "matched_merge_boundary", "BOUNDARY_REUSED": "restore_merged_text",
}


class MergeEvidenceError(MinerUError):
    """附带固定阶段/子码及受限整数定位，不携带正文、坐标、路径或底层异常。"""

    def __init__(self, reason: str, **counts) -> None:
        """只接受注册子码和受限计数，未知输入不进入日志或离线报告。"""
        super().__init__("MERGE_EVIDENCE", "MinerU 合并段落的正文或来源证据无法唯一核对，未保存不完整结果")
        self.diagnostic = {"reason": reason if reason in MERGE_REASONS else "UNCLASSIFIED",
                           "stage": "adapter." + MERGE_REASONS.get(reason, "restore_merged_text")}
        self.locate(**counts)

    def locate(self, **counts) -> None:
        """补充原数组位置/局部页、受限计数及固定结构角色；拒绝布尔值与外部字符串。"""
        for name in ("item_index", "page_idx", "observed", "expected"):
            value = counts.get(name)
            if type(value) is int and 0 <= value <= 1024**2 + 1:
                self.diagnostic.setdefault(name, value)
        for name, allowed in {"layout_group": {"para_blocks", "preproc_blocks"},
                              "span_role": {"source", "target"}}.items():
            if isinstance(counts.get(name), str) and counts[name] in allowed:
                self.diagnostic.setdefault(name, counts[name])


def merge_evidence_error(reason: str, **counts) -> MergeEvidenceError:
    """生成不可收费重试的安全子错误；不改变外部 MERGE_EVIDENCE 契约。"""
    return MergeEvidenceError(reason, **counts)


def matched_text_layout(item: dict, pages: dict[int, dict]) -> tuple[dict, dict, list]:
    """按同页类型/坐标唯一关联前后父块，索引须一致；坐标仅用于关联、不用于排序。"""

    index = item.get("page_idx")
    page = pages.get(index) if type(index) is int else None
    if not isinstance(page, dict):
        raise merge_evidence_error("LAYOUT_PAGE", page_idx=index)
    if type(item.get("text_level", 0)) is not int or item.get("text_level", 0) != 0:
        raise merge_evidence_error("TEXT_LEVEL", page_idx=index)
    box = convert_bbox(item.get("bbox"), (1000, 1000))
    if box is None:
        raise merge_evidence_error("ITEM_BBOX", page_idx=index)
    matches = []
    for group in ("para_blocks", "preproc_blocks"):
        blocks = page.get(group)
        if not isinstance(blocks, list) or not blocks:
            raise merge_evidence_error("PARENT_LIST", page_idx=index, layout_group=group)
        if len(blocks) > 512:
            raise merge_evidence_error("PARENT_LIMIT", page_idx=index, observed=len(blocks), expected=512,
                                       layout_group=group)
        indices = [b.get("index") for b in blocks if isinstance(b, dict)]
        if (len(indices) != len(blocks) or any(type(i) is not int or i < 0 for i in indices)
                or len(set(indices)) != len(indices)):
            raise merge_evidence_error("PARENT_INDEX", page_idx=index, layout_group=group)
        found = []
        for block in blocks:
            candidate = layout_bbox(block, page.get("page_size"))
            if (block.get("type") == "text" and candidate is not None
                    and all(abs(a - b) <= 2 for a, b in zip(box, candidate))):
                found.append(block)
        if len(found) != 1:
            raise merge_evidence_error("PARENT_MATCH", page_idx=index, observed=len(found), expected=1,
                                       layout_group=group)
        matches.append(found[0])
    if matches[0]["index"] != matches[1]["index"]:
        raise merge_evidence_error("PARENT_INDEX_MISMATCH", page_idx=index)
    return matches[0], matches[1], page["page_size"]


def plain_layout_spans(block: dict) -> list[dict]:
    """只接受每行一个非空纯文本 span；公式、混合 span 或未知布局不猜测拆分。"""

    lines = block.get("lines")
    if not isinstance(lines, list) or not lines:
        raise merge_evidence_error("LINES_MISSING")
    spans = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("spans"), list):
            raise merge_evidence_error("LINE_SCHEMA")
        if len(line["spans"]) != 1:
            raise merge_evidence_error("LINE_SPAN_COUNT", observed=len(line["spans"]), expected=1)
        span = line["spans"][0]
        if not isinstance(span, dict) or span.get("type") != "text":
            raise merge_evidence_error("SPAN_TYPE")
        if not isinstance(span.get("content"), str) or not span["content"].strip():
            raise merge_evidence_error("SPAN_TEXT")
        spans.append(span)
    return spans


def merged_text_slices(text: str, originals: list[str]) -> list[str]:
    """逐字符核对完整 V1 正文，返回其原始切片；只兼容空白分隔和 Markdown 字符转义。

    不用 layout 生成新正文，不去重、不模糊匹配；所有字符须被原序消费。分隔空白
    留在前一切片，切片拼接必须与 V1 原字符串完全相同，拒绝遗漏或额外文字。
    """

    if len(text) > 1024**2:
        raise merge_evidence_error("TEXT_LIMIT", observed=len(text), expected=1024**2)
    if not originals or any(not isinstance(part, str) or not part.strip() for part in originals):
        raise merge_evidence_error("SOURCE_TEXT")
    escapable = frozenset("\\`*_{}[]()#+-.!$<>|")
    cursor, cuts = 0, [0]
    for original in originals:
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        for char in original.strip():
            if cursor < len(text) and text[cursor] == char:
                cursor += 1
            elif (char in escapable and text[cursor:cursor + 2] == "\\" + char):
                cursor += 2
            else:
                raise merge_evidence_error("TEXT_CHARACTER")
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        cuts.append(cursor)
    if cursor != len(text):
        raise merge_evidence_error("TEXT_TRAILING")
    return [text[left:right] for left, right in pairwise(cuts)]


def merge_group_indices(items: list[dict], offset: int) -> tuple[list[int], list[int]]:
    """收集非连续正文/占位原索引及待核验边界索引；遇任何其他元素立即停止。

    最多64个占位与64个边界，不因跨页眉而越过另一正文寻找更早目标；本步骤
    仅收集索引，调用方必须验证每个边界的layout证据后才能恢复。
    """
    target, boundaries = offset - 1, []
    while target >= 0 and items[target].get("type") in MERGE_BOUNDARY_TYPES:
        boundaries.append(target)
        if len(boundaries) > 64:
            raise merge_evidence_error("BOUNDARY_LIMIT", item_index=offset + 1)
        target -= 1
    if target < 0:
        raise merge_evidence_error("GROUP_NO_TARGET", page_idx=items[offset].get("page_idx"),
                                   item_index=offset + 1)
    placeholders, cursor = [], offset
    while cursor < len(items) and items[cursor].get("type") == "text" and items[cursor].get("text") == "":
        placeholders.append(cursor)
        if len(placeholders) > 64:
            raise merge_evidence_error("GROUP_LIMIT", page_idx=items[offset].get("page_idx"),
                                       item_index=offset + 1, observed=len(placeholders), expected=64)
        next_index = cursor + 1
        while next_index < len(items) and items[next_index].get("type") in MERGE_BOUNDARY_TYPES:
            # 最后占位之后的无关页边界不属于本组，也不在本组验证/修改。
            if next_index - cursor > 64:
                break
            next_index += 1
        if (next_index < len(items) and items[next_index].get("type") == "text"
                and items[next_index].get("text") == ""):
            boundaries.extend(range(cursor + 1, next_index))
            if len(boundaries) > 64:
                raise merge_evidence_error("BOUNDARY_LIMIT", item_index=offset + 1)
            cursor = next_index
        else:
            break
    return [target, *placeholders], sorted(boundaries)


def matched_merge_boundary(item: dict, pages: dict[int, dict], allowed_pages: set[int]) -> tuple[int, int]:
    """唯一核对discarded页边界的类型/坐标/index/原文及边缘位置；只返回来源身份。

    不用边界位置推断正文顺序，不修改边界；任何缺失或冲突均拒绝跨越。
    """
    index, kind = item.get("page_idx"), item.get("type")
    location = {"page_idx": index}
    page = pages.get(index) if type(index) is int else None
    if not isinstance(page, dict) or index not in allowed_pages or kind not in MERGE_BOUNDARY_TYPES:
        raise merge_evidence_error("BOUNDARY_LAYOUT", **location)
    body, discarded = page.get("para_blocks"), page.get("discarded_blocks")
    if (not isinstance(body, list) or not body or not isinstance(discarded, list)
            or not 1 <= len(body) + len(discarded) <= 512):
        raise merge_evidence_error("BOUNDARY_LAYOUT", **location)
    blocks = body + discarded
    if any(not isinstance(block, dict) for block in blocks):
        raise merge_evidence_error("BOUNDARY_LAYOUT", **location)
    indices = [block.get("index") for block in blocks]
    if (any(type(value) is not int or value < 0 for value in indices)
            or len(set(indices)) != len(indices)):
        raise merge_evidence_error("BOUNDARY_INDEX", **location)
    boxes = [layout_bbox(block, page.get("page_size")) for block in blocks]
    if any(box is None for box in boxes):
        raise merge_evidence_error("BOUNDARY_LAYOUT", **location)
    try:
        box = convert_bbox(item.get("bbox"), (1000, 1000))
    except MinerUError:
        raise merge_evidence_error("BOUNDARY_MATCH", **location) from None
    matches = [position for position, block in enumerate(blocks)
               if box is not None and block.get("type") == kind
               and all(abs(a - b) <= 2 for a, b in zip(box, boxes[position]))]
    if len(matches) != 1 or matches[0] < len(body):
        raise merge_evidence_error("BOUNDARY_MATCH", observed=len(matches), expected=1, **location)
    boundary = blocks[matches[0]]
    top = box[3] <= 150 and box[3] <= min(value[1] for value in boxes[:len(body)])
    bottom = box[1] >= 850 and box[1] >= max(value[3] for value in boxes[:len(body)])
    top = top and boundary["index"] < min(indices[:len(body)])
    bottom = bottom and boundary["index"] > max(indices[:len(body)])
    if (kind == "header" and not top or kind == "footer" and not bottom
            or kind == "page_number" and not (top or bottom)):
        raise merge_evidence_error("BOUNDARY_POSITION", **location)
    if (boundary.get("lines_deleted", False) is not False or not isinstance(item.get("text"), str)
            or type(item.get("text_level", 0)) is not int or item.get("text_level", 0) != 0):
        raise merge_evidence_error("BOUNDARY_TEXT", **location)
    try:
        spans = plain_layout_spans(boundary)
        if any(span.get("cross_page", False) is not False
               or layout_bbox(span, page["page_size"]) is None for span in spans):
            raise merge_evidence_error("BOUNDARY_TEXT", **location)
        merged_text_slices(item["text"], [span["content"] for span in spans])
    except MinerUError:
        raise merge_evidence_error("BOUNDARY_TEXT", **location) from None
    return index, boundary["index"]


def restore_merged_text(items: list[dict], pages: dict[int, dict]) -> tuple[list[dict], int]:
    """恢复正文及有证据的非连续合并占位，保留所有原数组位置、页面与bbox。

    仅支持每个源段落一个纯文本 span、最多64个占位和64个经核对的边界。必须有
    lines_deleted=true、空后处理 lines、完整前处理来源及目标 span 顺序证据；
    跨页要求 cross_page=true 且只跨相邻后一页。歧义安全失败，不跳过真空正文。
    """

    if not pages:
        return items, 0
    restored, count, offset = list(items), 0, 0
    while offset < len(items):
        item = items[offset]
        if item.get("type") != "text" or item.get("text") != "":
            offset += 1
            continue
        # 无删除标记的空项仍交给原 EMPTY_BLOCK 检查；不把任意空正文当作合并占位。
        if type(item.get("page_idx")) is not int:
            offset += 1
            continue
        page = pages.get(item["page_idx"], {})
        candidates = page.get("para_blocks", [])
        if isinstance(candidates, list) and len(candidates) > 512:
            raise merge_evidence_error("PAGE_PARENT_LIMIT", page_idx=item["page_idx"], item_index=offset + 1,
                                       observed=len(candidates), expected=512)
        box = convert_bbox(item.get("bbox"), (1000, 1000))
        marked = []
        if isinstance(candidates, list) and box is not None:
            for block in candidates:
                candidate = layout_bbox(block, page.get("page_size")) if isinstance(block, dict) else None
                if (isinstance(block, dict) and block.get("type") == "text"
                        and block.get("lines_deleted") is True and candidate is not None
                        and all(abs(a - b) <= 2 for a, b in zip(box, candidate))):
                    marked.append(block)
        if not marked:
            offset += 1
            continue
        if len(marked) != 1:
            raise merge_evidence_error("PLACEHOLDER_MATCH", page_idx=item["page_idx"], item_index=offset + 1,
                                       observed=len(marked), expected=1)
        group_indices, boundary_indices = merge_group_indices(items, offset)
        group = [items[index] for index in group_indices]
        target_index = group_indices[0]
        target = group[0]
        if (target.get("type") != "text" or not isinstance(target.get("text"), str)
                or not target["text"].strip()):
            raise merge_evidence_error("TARGET_TEXT", page_idx=target.get("page_idx"), item_index=target_index + 1)
        evidence = []
        for position, value in zip(group_indices, group):
            try:
                evidence.append(matched_text_layout(value, pages))
            except MergeEvidenceError as error:
                error.locate(page_idx=value.get("page_idx"), item_index=position + 1)
                raise
        if boundary_indices:
            # 边界不能用于跨越别的正文；目标映射在V1中也必须唯一，避免重复目标歧义。
            target_box = convert_bbox(target.get("bbox"), (1000, 1000))
            target_matches = 0
            for value in items:
                if value.get("type") == "text" and value.get("page_idx") == target.get("page_idx"):
                    box = convert_bbox(value.get("bbox"), (1000, 1000))
                    if box is not None and all(abs(a - b) <= 2 for a, b in zip(box, target_box)):
                        target_matches += 1
            if target_matches != 1:
                raise merge_evidence_error("TARGET_AMBIGUOUS", page_idx=target.get("page_idx"),
                                           item_index=target_index + 1, observed=target_matches, expected=1)
            allowed_pages = {value.get("page_idx") for value in group}
            used_boundaries = set()
            for position in boundary_indices:
                try:
                    boundary_id = matched_merge_boundary(items[position], pages, allowed_pages)
                    if boundary_id in used_boundaries:
                        raise merge_evidence_error("BOUNDARY_REUSED", page_idx=items[position].get("page_idx"))
                    used_boundaries.add(boundary_id)
                except MergeEvidenceError as error:
                    error.locate(item_index=position + 1)
                    raise
        if evidence[0][0].get("lines_deleted", False) is not False:
            raise merge_evidence_error("TARGET_DELETED", page_idx=target.get("page_idx"), item_index=target_index + 1)
        source_spans = []
        for index, (parent, before, size) in enumerate(evidence):
            if index and (parent.get("lines_deleted") is not True or parent.get("lines") != []):
                raise merge_evidence_error("SOURCE_NOT_DELETED", page_idx=group[index].get("page_idx"),
                                           item_index=group_indices[index] + 1)
            try:
                spans = plain_layout_spans(before)
            except MergeEvidenceError as error:
                error.locate(page_idx=group[index].get("page_idx"), item_index=group_indices[index] + 1, span_role="source")
                raise
            if len(spans) != 1:
                raise merge_evidence_error("SOURCE_SPAN_COUNT", page_idx=group[index].get("page_idx"),
                                           item_index=group_indices[index] + 1, observed=len(spans), expected=1)
            if layout_bbox(spans[0], size) is None:
                raise merge_evidence_error("SOURCE_SPAN_BBOX", page_idx=group[index].get("page_idx"),
                                           item_index=group_indices[index] + 1)
            # 当前只处理源 span 覆盖整个段落的情形，不把局部 span 坐标冒充整个源块。
            span_box, parent_box = layout_bbox(spans[0], size), layout_bbox(before, size)
            if any(abs(a - b) > 2 for a, b in zip(span_box, parent_box)):
                raise merge_evidence_error("SOURCE_BBOX_COVERAGE", page_idx=group[index].get("page_idx"),
                                           item_index=group_indices[index] + 1)
            source_spans.append(spans[0])
        try:
            target_spans = plain_layout_spans(evidence[0][0])
        except MergeEvidenceError as error:
            error.locate(page_idx=target.get("page_idx"), item_index=target_index + 1, span_role="target")
            raise
        if len(target_spans) != len(source_spans):
            raise merge_evidence_error("TARGET_SPAN_COUNT", page_idx=target.get("page_idx"), item_index=target_index + 1,
                                       observed=len(target_spans), expected=len(source_spans))
        for position, value, original, moved, (_, _, size) in zip(
                group_indices, group, source_spans, target_spans, evidence):
            source_page, target_page = value.get("page_idx"), target.get("page_idx")
            location = {"page_idx": source_page, "item_index": position + 1}
            if (type(source_page) is not int or type(target_page) is not int
                    or source_page not in {target_page, target_page + 1}):
                raise merge_evidence_error("SOURCE_PAGE", **location)
            if moved.get("content") != original["content"]:
                raise merge_evidence_error("MOVED_TEXT", **location)
            if moved.get("cross_page", False) is not (source_page != target_page):
                raise merge_evidence_error("MOVED_CROSS_PAGE", **location)
            if original.get("cross_page", False) is not False:
                raise merge_evidence_error("SOURCE_CROSS_PAGE", **location)
            moved_box, original_box = layout_bbox(moved, size), layout_bbox(original, size)
            if moved_box is None or any(abs(a - b) > 2 for a, b in zip(moved_box, original_box)):
                raise merge_evidence_error("MOVED_BBOX", **location)
        try:
            parts = merged_text_slices(target["text"], [s["content"] for s in source_spans])
        except MergeEvidenceError as error:
            error.locate(page_idx=target.get("page_idx"), item_index=target_index + 1)
            raise
        # 只写回经验证的真实原索引；绝不连续覆盖夹在其中的页眉/页脚等内容。
        for index, value, text in zip(group_indices, group, parts):
            restored[index] = value | {"text": text}
        count += len(group_indices) - 1
        offset = group_indices[-1] + 1
    return restored, count


class MinerUResultAdapter:
    """保留正文原序并恢复有证据的页边界项；不改写表格、公式或伪造置信度。"""

    def normalize(
        self,
        archive: bytes,
        *,
        doc_id: UUID,
        file_name: str,
        file_type: str,
        geometry: dict[int, tuple[float, float]] | None,
        identity: str,
    ) -> ParsedDocument:
        """统一 V4 Block；PDF/图片核对合并来源，Office 保持原逻辑页和空坐标规则。"""

        if file_type not in MINERU_TYPES:
            raise MinerUError("FILE_TYPE", "该文件类型不支持 MinerU 结果转换")
        office = file_type in OFFICE_TYPES
        items = read_content_list(archive)
        pages = {} if office else read_layout_pages(archive)
        if not office:
            try:
                items, restored_count = restore_merged_text(items, pages)
            except MinerUError as exc:
                detail = exc.diagnostic if isinstance(exc, MergeEvidenceError) else {}
                log_event("mineru.merge_rejected", level=logging.WARNING,
                          doc_id=doc_id, error_code=exc.code,
                          stage=".".join(filter(None, (detail.get("stage", "adapter"),
                              detail.get("layout_group"), detail.get("span_role")))),
                          step=detail.get("reason"), item_index=detail.get("item_index"),
                          page=(detail["page_idx"] + 1) if "page_idx" in detail else None,
                          item_count=detail.get("observed"), error_type=type(exc).__name__)
                raise
            if restored_count:
                log_event("mineru.merge_restored", doc_id=doc_id,
                          item_count=restored_count, stage="adapter")
        blocks, headings, title = [], {}, ""
        # ID 使用原始 position，block_index 使用最终列表顺序；改序不能换掉既有块身份。
        ordered = (
            list(enumerate(items, start=1))
            if office
            else ordered_content_items(archive, items, pages=pages)
        )
        for position, item in ordered:
            page_idx = item.get("page_idx")
            if (
                type(page_idx) is not int
                or page_idx < 0
                or (office and page_idx >= 200)
                or (not office and (geometry is None or page_idx not in geometry))
            ):
                raise MinerUError("PAGE", "MinerU 页码无效或与已知原件页面范围不一致")
            kind = item.get("type")
            if kind in {
                "text",
                "title",
                "image_text",
                "header",
                "footer",
                "page_number",
                "aside_text",
                "page_footnote",
            }:
                content = joined_text(item.get("text"))
                level = item.get("text_level", 0)
                block_type = (
                    "title"
                    if kind == "title" or (type(level) is int and level > 0)
                    else ("image_text" if kind == "image_text" else "text")
                )
                if block_type == "title" and content.strip():
                    level = level if type(level) is int and 1 <= level <= 9 else 1
                    headings = {
                        key: value for key, value in headings.items() if key < level
                    }
                    headings[level] = content.strip()
                    title = title or content.strip()
            elif kind == "table":
                block_type = "table"
                content = "\n".join(
                    part
                    for part in (
                        joined_text(item.get("table_caption")),
                        joined_text(item.get("table_body")),
                        joined_text(item.get("table_footnote")),
                    )
                    if part
                )
            elif kind in {"list", "code"}:
                block_type = "text"
                content = (
                    joined_text(item.get("list_items"))
                    if kind == "list"
                    else "\n".join(
                        part
                        for part in (
                            joined_text(item.get("code_caption")),
                            joined_text(item.get("code_body")),
                            joined_text(item.get("code_footnote")),
                        )
                        if part
                    )
                )
            elif kind in {"equation", "formula"}:
                block_type = "formula"
                content = joined_text(item.get("text"))
            elif kind in {"image", "chart"}:
                block_type = "image_text"
                content = "\n".join(
                    part
                    for part in (
                        joined_text(item.get("content")),
                        joined_text(item.get("image_caption")),
                        joined_text(item.get("image_footnote")),
                        joined_text(item.get("img_caption")),
                        joined_text(item.get("img_footnote")),
                        joined_text(item.get("chart_caption")),
                        joined_text(item.get("chart_footnote")),
                    )
                    if part
                )
            else:
                raise MinerUError(
                    "CONTENT_TYPE", "MinerU 返回尚未支持的内容类型，请核对结果契约"
                )
            if not content.strip():
                if kind in {"image", "chart"}:
                    continue
                raise MinerUError(
                    "EMPTY_BLOCK", "MinerU 返回的正文、表格或公式没有可用文本"
                )
            try:
                blocks.append(
                    ParsedBlock(
                        block_id=uuid5(
                            doc_id,
                            f"mineru-v1:{identity}:{position}:{page_idx}:{block_type}",
                        ),
                        page=page_idx + 1,
                        block_type=block_type,
                        text=content,
                        section=" / ".join(headings[key] for key in sorted(headings)),
                        bbox=None
                        if office
                        else convert_bbox(item.get("bbox"), geometry[page_idx]),
                        source="mineru_cloud",
                        confidence=item.get("confidence"),
                    )
                )
            except ValidationError:
                raise MinerUError(
                    "CONTENT_LIST", "MinerU Block 字段不符合统一契约"
                ) from None
        if not blocks:
            raise MinerUError("NO_TEXT", "MinerU 未返回可用解析正文")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            title=title,
            blocks=blocks,
        )
