"""有界、只读的预检；检查覆盖风险，不下载附件或推断 OCR 真值。"""

import re
import posixpath
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pymupdf
from lxml import etree

from app.core.config import Settings
from app.core.business_logging import log_event
from app.core.file_formats import FileFormatError, validate_file_content
from app.parsers.common import ParserError
from app.parsers.routing.contracts import PDF_COVERAGE_VERSION, Inspection, PageSignals, StructuralSignals
from app.parsers.routing.pdf_coverage import margin_rule_only
from app.parsers.text import FENCE, read_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
RELATIONSHIP_ROOT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
# 图片面积仍需逐项计算；绘图细查超预算时只保留计数并要求云端覆盖。
PDF_MAX_IMAGES_PER_PAGE = 5000
PDF_GEOMETRY_DETAIL_LIMIT = 5000


class InspectionLimitError(ParserError):
    """资源上限不是格式损坏，路由必须安全失败而不是完成部分预检。"""


def missing_external_assets(archive: ZipFile, root: str, limit: int) -> bool:
    """检查内容附件；DOCX外部排版模板不当正文依赖，视觉/对象外链仍阻塞且不抓取。"""

    names = set(archive.namelist())
    parts = [name for name in names if name.startswith(root + "/") and name.endswith(".rels")]
    if sum(archive.getinfo(name).file_size for name in parts) > 64 * 1024**2:
        raise InspectionLimitError("Office 关系规模超出预检安全上限")
    external = False
    for name in parts:
        parent = posixpath.dirname(posixpath.dirname(name))
        tree = xml_part(archive, name, limit)
        if etree.QName(tree).localname != "Relationships":
            raise ParserError("Office 附件关系结构无法识别")
        identifiers = set()
        for node in tree:
            identifier = node.get("Id", "")
            if (etree.QName(node).localname != "Relationship" or not identifier
                or identifier in identifiers or not node.get("Type") or not node.get("Target")):
                raise ParserError("Office 附件关系损坏或存在重复标识")
            identifiers.add(identifier)
            if node.get("Type", "").rsplit("/", 1)[-1] == "hyperlink":
                continue
            if node.get("TargetMode", "Internal") != "Internal":
                if (root == "word" and node.get("TargetMode") == "External"
                        and node.get("Type") == RELATIONSHIP_ROOT + "attachedTemplate"):
                    # 模板只影响外部排版，不补正文；精确类型例外不放行图片/OLE或内部缺件。
                    continue
                external = True
                continue
            target = node.get("Target", "")
            part = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                     else posixpath.join(parent, target))
            if (not target or "\\" in target or ":" in target or "?" in target or "#" in target
                or part.startswith("../") or part not in names):
                raise ParserError("Office 附件关系无效或内部部件缺失")
    return external


def xml_part(archive: ZipFile, name: str, limit: int):
    """读取受限 OOXML 部件并禁止 DTD、实体与网络，错误只返回安全说明。"""

    if archive.getinfo(name).file_size > limit:
        log_event("router.inspection_rejected", stage="router.xml_part", step="XML_PART_SIZE",
                  size_bytes=archive.getinfo(name).file_size, limit_count=limit, error_code="PREFLIGHT_LIMIT")
        raise InspectionLimitError("文档结构部件超出预检安全限制")
    content = archive.read(name)
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ParserError("文档结构包含不支持的 XML 声明")
    tree = etree.fromstring(
        content,
        parser=etree.XMLParser(
            resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False
        ),
    )
    if tree.getroottree().docinfo.doctype:
        raise ParserError("文档结构包含不支持的 XML 声明")
    return tree


def cell_has_content(cell, shared_strings: list[str]) -> bool:
    """区分格式化空格与有效单元格，解析 sharedStrings 引用但不把值放入结构摘要。"""

    if cell.find("{*}f") is not None:
        return True
    value = cell.find("{*}v")
    if cell.get("t") == "s" and value is not None:
        try:
            index = int(value.text or "")
            if index < 0 or index >= len(shared_strings):
                raise ValueError
        except ValueError:
            raise ParserError("XLSX 共享字符串引用无效") from None
        return bool(shared_strings[index].strip())
    return any(
        (node.text or "").strip()
        for node in cell.iter()
        if etree.QName(node).localname in {"v", "t"}
    )


def inspect_office(path: Path, kind: str, settings: Settings) -> Inspection:
    """检查 DOCX/XLSX 覆盖结构和附件关系；不渲染 Office，也不计算打印页。"""

    root = "word" if kind == "docx" else "xl"
    limit = settings.router_xml_part_max_mb * 1024 * 1024
    with ZipFile(path) as archive:
        names = archive.namelist()
        signals = {
            "image_count": sum(
                n.startswith(root + "/media/") and not n.endswith("/") for n in names
            ),
            "drawing_count": sum(
                n.startswith(root + "/drawings/") and n.endswith(".xml") for n in names
            ),
            "chart_count": sum(
                n.startswith(root + "/charts/") and n.endswith(".xml") for n in names
            ),
            "embedded_count": sum(
                n.startswith(root + "/embeddings/") and not n.endswith("/")
                for n in names
            ),
        }
        risks = []
        if missing_external_assets(archive, root, limit):
            risks.append("EXTERNAL_ATTACHMENT_MISSING")
        for field, code in (
            ("image_count", "HAS_EMBEDDED_IMAGE"),
            ("drawing_count", "HAS_DRAWING"),
            ("chart_count", "HAS_CHART"),
            ("embedded_count", "HAS_EMBEDDED_OBJECT"),
        ):
            if signals[field]:
                risks.append(f"{kind.upper()}_{code}")
        if kind == "docx":
            tree = xml_part(archive, "word/document.xml", limit)
            if tree.tag != f"{{{W}}}document":
                raise ParserError("DOCX 正文 XML 结构无法识别")
            tags = {element.tag for element in tree.iter()}
            for tag, code in (
                (f"{{{W}}}drawing", "DOCX_HAS_DRAWING"),
                (f"{{{W}}}pict", "DOCX_HAS_DRAWING"),
                (f"{{{W}}}txbxContent", "DOCX_HAS_TEXTBOX"),
                (f"{{{W}}}object", "DOCX_HAS_EMBEDDED_OBJECT"),
                (f"{{{M}}}oMath", "DOCX_HAS_FORMULA"),
                (f"{{{M}}}oMathPara", "DOCX_HAS_FORMULA"),
            ):
                if tag in tags:
                    risks.append(code)
            # Native 只遍历普通段落和表格；字段、修订、内容控件等不能当作已完整覆盖。
            if any(
                f"{{{W}}}{tag}" in tags
                for tag in (
                    "sdt",
                    "altChunk",
                    "ins",
                    "del",
                    "fldSimple",
                    "fldChar",
                    "numPr",
                )
            ):
                risks.append("DOCX_HAS_UNCOVERED_STRUCTURE")
            for name in names:
                if name.startswith("word/diagrams/"):
                    risks.append("DOCX_HAS_DIAGRAM")
                if re.fullmatch(
                    r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name
                ):
                    auxiliary = xml_part(archive, name, limit)
                    if any(
                        (node.text or "").strip()
                        for node in auxiliary.iter(f"{{{W}}}t")
                    ) or any(
                        node.tag in {f"{{{W}}}drawing", f"{{{W}}}pict", f"{{{M}}}oMath"}
                        for node in auxiliary.iter()
                    ):
                        risks.append("DOCX_HAS_AUXILIARY_CONTENT")
        else:
            # Sheet 次序由 Native 保留；预检只保存数量，不记录 Sheet 名或单元格值。
            workbook = xml_part(archive, "xl/workbook.xml", limit)
            if etree.QName(workbook).localname != "workbook":
                raise ParserError("XLSX 工作簿 XML 结构无法识别")
            signals["sheet_count"] = len(workbook.findall(".//{*}sheet"))
            sheet_names = [
                n for n in names if re.fullmatch(r"xl/worksheets/[^/]+\.xml", n)
            ]
            inspected_size = sum(archive.getinfo(n).file_size for n in sheet_names)
            if inspected_size > 64 * 1024 * 1024:
                return Inspection(
                    signals=StructuralSignals(**signals),
                    risks=("PREFLIGHT_LIMIT",),
                    uncertain=True,
                )
            signals["nonempty_sheet_count"] = 0
            signals["formula_count"] = 0
            shared_strings = []
            if "xl/sharedStrings.xml" in names:
                strings = xml_part(archive, "xl/sharedStrings.xml", limit)
                shared_strings = [
                    "".join(node.text or "" for node in item.iterfind(".//{*}t"))
                    for item in strings.findall("{*}si")
                ]
            for name in sheet_names:
                sheet = xml_part(archive, name, limit)
                if any(
                    etree.QName(node).localname
                    in {
                        "drawing",
                        "legacyDrawing",
                        "legacyDrawingHF",
                        "oleObjects",
                        "picture",
                        "controls",
                    }
                    for node in sheet.iter()
                ):
                    risks.append("XLSX_HAS_DRAWING")
                cells = sheet.findall(".//{*}sheetData/{*}row/{*}c")
                signals["formula_count"] += sum(
                    cell.find("{*}f") is not None for cell in cells
                )
                if any(cell_has_content(cell, shared_strings) for cell in cells):
                    signals["nonempty_sheet_count"] += 1
            if any(n.lower().endswith(".vml") for n in names):
                risks.append("XLSX_HAS_VML")
            if any(n.startswith("xl/externalLinks/") for n in names):
                risks.append("XLSX_HAS_EXTERNAL_LINK")
            if signals["sheet_count"] != len(sheet_names):
                risks.append("XLSX_UNSUPPORTED_SHEET_STRUCTURE")
    return Inspection(
        signals=StructuralSignals(**signals), risks=tuple(dict.fromkeys(risks))
    )


def inspect_markdown(path: Path) -> Inspection:
    """识别正文图片/HTML 视觉引用，忽略围栏与行内代码；不获取任何 URL 或附件。"""

    visible = []
    fence = ""
    for line in read_text(path).splitlines():
        marker = FENCE.match(line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
            continue
        if marker:
            fence = marker[1]
            continue
        visible.append(re.sub(r"(`+).*?\1", "", line))
    count = len(
        re.findall(
            r"(?<!\\)!\[[^\]]*\]|<(?:img|svg|picture|object)\b",
            "\n".join(visible),
            re.IGNORECASE,
        )
    )
    return Inspection(
        signals=StructuralSignals(image_reference_count=count),
        risks=("MD_ASSET_NOT_UPLOADED",) if count else (),
    )


def has_columns(lines: list[tuple], width: float) -> bool:
    """仅检测重叠行间明显横向间隔；有疑似多栏就走整份解析，不用于排序。"""

    active = []
    for rect in sorted(lines, key=lambda item: item[1]):
        active = [other for other in active if other[3] > rect[1]]
        if len(active) > 128:
            return True
        for other in active:
            overlap = min(rect[3], other[3]) - max(rect[1], other[1])
            if (
                overlap > min(rect[3] - rect[1], other[3] - other[1]) * 0.5
                and max(rect[0], other[0]) - min(rect[2], other[2]) > width * 0.05
            ):
                return True
        active.append(rect)
    return False


def inspect_pdf(path: Path, settings: Settings) -> Inspection:
    """逐页冻结覆盖 v2；密集绘图改用计数判云，页数/行数/图片硬预算仍拒绝。"""

    pages, risks = [], []
    with pymupdf.open(path) as document:
        if document.needs_pass or document.is_repaired or not document.page_count:
            raise ParserError("PDF 已加密、损坏或没有可读页面")
        if document.page_count > settings.router_pdf_max_pages:
            log_event("router.preflight_rejected", stage="preflight.pdf", step="PDF_PAGE_LIMIT",
                error_code="PREFLIGHT_LIMIT", page_count=document.page_count,
                item_count=settings.router_pdf_max_pages, limit_count=settings.router_pdf_max_pages)
            return Inspection(
                signals=StructuralSignals(page_count=document.page_count),
                risks=("PREFLIGHT_LIMIT",),
                uncertain=True,
            )
        for page in document:
            area = max(page.rect.width * page.rect.height, 1)
            images = page.get_image_info()
            drawings = page.get_drawings()
            annotations = sum(1 for _ in page.annots())
            text_data = page.get_text(
                "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            )
            lines = [
                line
                for block in text_data["blocks"]
                if block.get("type") == 0
                for line in block.get("lines", [])
            ]
            if (
                len(lines) > settings.router_pdf_max_lines_per_page
                or len(images) > PDF_MAX_IMAGES_PER_PAGE
            ):
                log_event("router.preflight_rejected", stage="preflight.pdf",
                    step="PDF_LINE_LIMIT" if len(lines) > settings.router_pdf_max_lines_per_page else "PDF_OBJECT_LIMIT",
                    error_code="PREFLIGHT_LIMIT", page=page.number + 1,
                    item_count=len(lines), line_count=len(lines), image_count=len(images),
                    drawing_count=len(drawings), object_count=len(images) + len(drawings),
                    limit_count=(settings.router_pdf_max_lines_per_page
                                 if len(lines) > settings.router_pdf_max_lines_per_page else PDF_MAX_IMAGES_PER_PAGE))
                return Inspection(
                    signals=StructuralSignals(page_count=document.page_count),
                    risks=("PREFLIGHT_LIMIT",),
                    uncertain=True,
                )
            geometry_limited = len(images) + len(drawings) > PDF_GEOMETRY_DETAIL_LIMIT
            if geometry_limited:
                # 已取到路径计数，不再遍历几何或判断装饰；仍扫描后续页并冻结完整覆盖要求。
                log_event("router.preflight_geometry_limited", stage="preflight.pdf", step="PDF_DRAWING_DETAIL_LIMIT",
                    page=page.number + 1, line_count=len(lines), image_count=len(images),
                    drawing_count=len(drawings), object_count=len(images) + len(drawings),
                    limit_count=PDF_GEOMETRY_DETAIL_LIMIT)
            spans = [span for line in lines for span in line["spans"]]
            text = "".join(span["text"] for span in spans).strip()
            ratio = min(
                1.0,
                sum(
                    (pymupdf.Rect(image["bbox"]) & page.rect).get_area() / area
                    for image in images
                ),
            )
            visual = ratio >= settings.router_pdf_image_area_ratio
            required = bool(text or images or drawings or annotations)
            if not required:
                # shading/不可提取的绘制可能不在 get_drawings 中，未知绘制不能冒充空白页。
                required = bool(page.get_bboxlog())
            decorative = (not geometry_limited and not (text or images or annotations) and bool(drawings)
                          and margin_rule_only(page, drawings))
            if decorative:
                required = False
            pages.append(
                PageSignals(
                    page=page.number + 1,
                    text_chars=len(text),
                    image_count=len(images),
                    image_area_ratio=ratio,
                    drawing_count=len(drawings),
                    annotation_count=annotations,
                    required=required,
                    decorative_drawing_count=len(drawings) if decorative else 0,
                    coverage_reason=("margin_rule_only" if decorative else
                                     "content_or_uncertain" if required else "empty"),
                )
            )
            if not text and required:
                risks.append("PDF_SCAN_OR_NON_TEXT_PAGE")
            if text and visual:
                risks.append("PDF_TEXT_IMAGE_MIXED")
            if visual and len(text) < settings.router_pdf_min_text_chars:
                risks.append("PDF_INSUFFICIENT_TEXT")
            if not decorative and (geometry_limited or len(drawings) >= settings.router_pdf_drawing_count or any(
                (pymupdf.Rect(item["rect"]) & page.rect).get_area() / area
                >= settings.router_pdf_drawing_area_ratio
                for item in drawings
            )):
                risks.append("PDF_COMPLEX_DRAWING")
            if annotations:
                risks.append("PDF_HAS_ANNOTATION")
            if has_columns([line["bbox"] for line in lines], page.rect.width):
                risks.append("PDF_MULTICOLUMN_RISK")
            if any(character in text for character in "∫∑√∏≠≤≥") or any(
                re.search(r"math|symbol", span.get("font", ""), re.IGNORECASE)
                or span.get("flags", 0) & 1
                for span in spans
            ):
                risks.append("PDF_FORMULA_RISK")
            if any(tuple(line.get("dir", (1, 0))) != (1, 0) for line in lines):
                risks.append("PDF_ROTATED_TEXT")
        return Inspection(
            signals=StructuralSignals(
                page_count=document.page_count, pages=tuple(pages), pdf_coverage_version=PDF_COVERAGE_VERSION
            ),
            risks=tuple(dict.fromkeys(risks)),
        )


def inspect_document(path: Path, kind: str, settings: Settings) -> Inspection:
    """先验证真实容器再做只读预检，损坏输入安全失败而不是盲目消耗云端额度。"""

    try:
        with path.open("rb") as stream:
            validate_file_content(stream, kind)
        if kind in {"docx", "xlsx"}:
            return inspect_office(path, kind, settings)
        if kind == "pptx":
            with ZipFile(path) as archive:
                missing = missing_external_assets(archive, "ppt", settings.router_xml_part_max_mb * 1024**2)
            return Inspection(risks=("EXTERNAL_ATTACHMENT_MISSING",) if missing else ())
        if kind == "pdf":
            return inspect_pdf(path, settings)
        if kind == "md":
            return inspect_markdown(path)
        return Inspection()
    except InspectionLimitError:
        return Inspection(risks=("PREFLIGHT_LIMIT",), uncertain=True)
    except (
        FileFormatError,
        BadZipFile,
        etree.XMLSyntaxError,
        pymupdf.FileDataError,
        KeyError,
        ValueError,
    ):
        raise ParserError("文档格式损坏或结构无法安全预检，请人工核对原件") from None
