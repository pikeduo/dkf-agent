"""读取云端 ZIP 的 content_list，转换为现有统一契约，不实现持久化 Parse Cache。"""

import json
import math
from io import BytesIO
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
    """保留阅读顺序、实际标题章节、表格和公式；没有置信度时不伪造高分。"""

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
        for position, item in enumerate(items, start=1):
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
