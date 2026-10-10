"""读取云端 ZIP 的 content_list，转换为现有统一契约，不实现持久化 Parse Cache。"""

import json
import math
from io import BytesIO
from itertools import pairwise
from operator import itemgetter
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5
from zipfile import BadZipFile, ZipFile

import pymupdf
from pydantic import ValidationError

from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.mineru.errors import MinerUError


def page_geometry(path: Path, file_type: str) -> dict[int, tuple[float, float]]:
    """获取原件的真实页面尺寸，用于归一化坐标转点数 / 像素，不猜测尺寸。"""

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


def ordered_content_items(archive: bytes, items: list[dict]) -> list[tuple[int, dict]]:
    """按页恢复经过验证的页边界项，并携带原始数组位置保证 Block ID 不变。

    不改变原有 page_idx 序列，交错页面也只替换该页自己的槽位；不做全局排序。
    """

    ordered = list(enumerate(items, start=1))
    pages = read_layout_pages(archive)
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


class MinerUResultAdapter:
    """保留正文原序并恢复有证据的页边界项；不改写表格、公式或伪造置信度。"""

    def normalize(
        self,
        archive: bytes,
        *,
        doc_id: UUID,
        file_name: str,
        file_type: str,
        geometry: dict[int, tuple[float, float]],
        identity: str,
    ) -> ParsedDocument:
        """将 V4 内容列表统一为 Block；页码转为 1 基，格式未知时安全失败而非丢正文。"""

        items = read_content_list(archive)
        blocks, headings, title = [], {}, ""
        # ID 使用原始 position，block_index 使用最终列表顺序；改序不能换掉既有块身份。
        for position, item in ordered_content_items(archive, items):
            page_idx = item.get("page_idx")
            if type(page_idx) is not int or page_idx not in geometry:
                raise MinerUError("PAGE", "MinerU 页码与原件物理页数不一致")
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
                        bbox=convert_bbox(item.get("bbox"), geometry[page_idx]),
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
