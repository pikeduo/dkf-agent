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
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
EXTERNAL_LINKS = "http://schemas.microsoft.com/office/spreadsheetml/2021/extlinks2021"
# 图片面积仍需逐项计算；绘图细查超预算时只保留计数并要求云端覆盖。
PDF_MAX_IMAGES_PER_PAGE = 5000
PDF_GEOMETRY_DETAIL_LIMIT = 5000


class InspectionLimitError(ParserError):
    """资源上限不是格式损坏，路由必须安全失败而不是完成部分预检。"""


def missing_external_assets(archive: ZipFile, root: str, limit: int) -> bool:
    """保留旧严格检查入口；新路由显式读取保存快照摘要，不静默修改旧调用语义。"""

    return external_asset_summary(archive, root, limit)[0]


def internal_relationship_part(owner: str, node, names: set[str]) -> str:
    """验证包内目标且不读取外部地址；缺件/路径越界仍是原件错误。"""
    target = node.get("Target", "")
    part = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                             else posixpath.join(posixpath.dirname(owner), target))
    if (not target or "\\" in target or ":" in target or "?" in target or "#" in target
            or part.startswith("../") or part not in names):
        raise ParserError("Office 附件关系无效或内部部件缺失")
    return part


def saved_chart_available(tree) -> bool:
    """只证明原图表有保存数据；允许原有稀疏空点，不补零、不计算或重建外部工作簿。"""
    if tree.tag != f"{{{C}}}chartSpace" or not tree.findall(f".//{{{C}}}ser"):
        return False
    points_present = False
    for reference in tree.iter():
        cache_tag = {f"{{{C}}}numRef": "numCache", f"{{{C}}}strRef": "strCache"}.get(reference.tag)
        if cache_tag is None:
            if reference.tag == f"{{{C}}}multiLvlStrRef":
                return False
            continue
        cache = reference.find(f"{{{C}}}{cache_tag}")
        count = cache.find(f"{{{C}}}ptCount") if cache is not None else None
        raw_count = count.get("val", "") if count is not None else ""
        if not raw_count.isascii() or not raw_count.isdigit() or len(raw_count) > 7:
            return False
        size, indices = int(raw_count), set()
        for point in cache.findall(f"{{{C}}}pt"):
            raw_index = point.get("idx", "")
            if (not raw_index.isascii() or not raw_index.isdigit() or len(raw_index) > 7
                    or int(raw_index) >= size or int(raw_index) in indices
                    or point.find(f"{{{C}}}v") is None):
                return False
            indices.add(int(raw_index))
        points_present |= cache_tag == "numCache" and any(
            (point.find(f"{{{C}}}v").text or "").strip() for point in cache.findall(f"{{{C}}}pt"))
    return points_present


def saved_external_relationship(archive: ZipFile, root: str, owner: str, tree, nodes: dict,
                                node, names: set[str]) -> bool:
    """识别保存快照的更新关系；仅接受精确OOXML类型及同元素已存在的包内图片副本。"""
    identifier, kind = node.get("Id"), node.get("Type")
    relation_ns = RELATIONSHIP_ROOT.rstrip("/")
    references = [element for element in tree.iter() if any(
        key.startswith("{" + relation_ns + "}") and value == identifier
        for key, value in element.attrib.items())]
    if not references:
        # 不以“没找到引用”为由忽略未知部件，避免漏掉其他命名空间的内容关系。
        return False
    if (kind in {RELATIONSHIP_ROOT + "oleObject", RELATIONSHIP_ROOT + "package"}
            and re.fullmatch(re.escape(root) + r"/charts/chart\d+\.xml", owner)
            and all(element.tag == f"{{{C}}}externalData"
                    and element.get(f"{{{relation_ns}}}id") == identifier for element in references)):
        return saved_chart_available(tree)
    if (root == "xl" and kind == RELATIONSHIP_ROOT + "externalLinkPath"
            and re.fullmatch(r"xl/externalLinks/externalLink\d+\.xml", owner)
            and tree.tag == f"{{{S}}}externalLink"
            and tree.find(f"{{{S}}}externalBook") is not None):
        # externalBook及其备用地址均为更新入口；公式仍从原单元格保留，不拿缓存值代替。
        return all(element.get(f"{{{relation_ns}}}id") == identifier and (
            element.tag == f"{{{S}}}externalBook" or (
            element.tag in {f"{{{EXTERNAL_LINKS}}}absoluteUrl", f"{{{EXTERNAL_LINKS}}}relativeUrl"}
            and element.getparent() is not None
            and element.getparent().tag == f"{{{EXTERNAL_LINKS}}}alternateUrls"
            and element.getparent().getparent() is tree.find(f"{{{S}}}externalBook")
        )) for element in references)
    if kind != RELATIONSHIP_ROOT + "image":
        return False
    for element in references:
        embedded = nodes.get(element.get(f"{{{relation_ns}}}embed"))
        if (element.tag != f"{{{A}}}blip" or element.get(f"{{{relation_ns}}}link") != identifier
                or embedded is None or embedded.get("TargetMode", "Internal") != "Internal"
                or embedded.get("Type") != RELATIONSHIP_ROOT + "image"):
            return False
        part = internal_relationship_part(owner, embedded, names)
        if not part.startswith(root + "/media/") or not archive.getinfo(part).file_size:
            return False
    return True


def external_asset_summary(archive: ZipFile, root: str, limit: int, *, saved_snapshot: bool = False) -> tuple[bool, int]:
    """返回真正缺件与保存快照关系计数；只读有界包内XML，不访问关系中的URL。"""

    names = set(archive.namelist())
    parts = sorted(name for name in names if name.startswith(root + "/") and name.endswith(".rels"))
    if sum(archive.getinfo(name).file_size for name in parts) > 64 * 1024**2:
        raise InspectionLimitError("Office 关系规模超出预检安全上限")
    external, snapshots, owner_size = False, 0, 0
    for name in parts:
        parent = posixpath.dirname(posixpath.dirname(name))
        owner = posixpath.join(parent, posixpath.basename(name)[:-5])
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
        nodes = {node.get("Id"): node for node in tree}
        owner_tree = None
        for node in tree:
            if node.get("Type", "").rsplit("/", 1)[-1] == "hyperlink":
                continue
            if node.get("TargetMode", "Internal") != "Internal":
                if (root == "word" and node.get("TargetMode") == "External"
                        and node.get("Type") == RELATIONSHIP_ROOT + "attachedTemplate"):
                    # 模板只影响外部排版，不补正文；精确类型例外不放行图片/OLE或内部缺件。
                    continue
                if saved_snapshot and node.get("TargetMode") == "External" and owner in names:
                    if owner_tree is None:
                        owner_size += archive.getinfo(owner).file_size
                        if owner_size > 64 * 1024**2:
                            raise InspectionLimitError("Office 保存快照规模超出预检安全上限")
                        owner_tree = xml_part(archive, owner, limit)
                    if saved_external_relationship(archive, root, owner, owner_tree, nodes, node, names):
                        snapshots += 1
                        continue
                external = True
                continue
            internal_relationship_part(owner, node, names)
    return external, snapshots


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
    """检查覆盖和真正缺件；更新依赖有保存内容时冻结PARTIAL快照范围，不渲染或联网。"""

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
        missing, snapshot_count = external_asset_summary(archive, root, limit, saved_snapshot=True)
        if missing:
            risks.append("EXTERNAL_ATTACHMENT_MISSING")
        if snapshot_count:
            signals["office_snapshot_rel_count"] = snapshot_count
            risks.append("OFFICE_SAVED_SNAPSHOT")
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
    """先验证容器再冻结覆盖及保存快照范围；真正缺件/损坏仍拒绝，不访问外链。"""

    try:
        with path.open("rb") as stream:
            validate_file_content(stream, kind)
        if kind in {"docx", "xlsx"}:
            return inspect_office(path, kind, settings)
        if kind == "pptx":
            with ZipFile(path) as archive:
                missing, snapshot_count = external_asset_summary(
                    archive, "ppt", settings.router_xml_part_max_mb * 1024**2, saved_snapshot=True)
            return Inspection(signals=StructuralSignals(office_snapshot_rel_count=snapshot_count),
                risks=tuple(code for code, enabled in (("EXTERNAL_ATTACHMENT_MISSING", missing),
                    ("OFFICE_SAVED_SNAPSHOT", bool(snapshot_count))) if enabled))
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
