"""落库前覆盖检查；不能证明 OCR 语义准确，也不会伪造缺失内容。"""

from app.core.file_formats import OFFICE_TYPES
from app.parsers.common import ParserError
from app.parsers.routing.contracts import RouteDecision
from app.schemas.parsed_document import ParsedDocument, ParseQuality

OFFICE_SNAPSHOT_WARNINGS = ("OFFICE_SAVED_SNAPSHOT", "EXTERNAL_SOURCE_UNVERIFIED")


class CompletenessError(ParserError):
    """明确的结构覆盖缺失，仅 auto Native 路径可受控回退一次。"""


def guard_result(parsed: ParsedDocument, decision: RouteDecision) -> None:
    """保留身份/范围校验；Office仅凭已冻结PDF摘要扩展逻辑页范围，缺页仍明确PARTIAL。"""

    if parsed.file_type != decision.file_type:
        raise CompletenessError("解析结果格式与路由决策不一致")
    if not parsed.blocks or not any(block.text.strip() for block in parsed.blocks):
        raise CompletenessError("解析结果没有有效正文，不能提交部分结果")
    pages = {block.page for block in parsed.blocks if block.text.strip()}
    signals = decision.signals
    if decision.file_type == "pdf":
        if signals.page_count is None:
            raise CompletenessError("PDF 缺少可信物理页范围")
        if any(page > signals.page_count for page in pages):
            raise CompletenessError("PDF 解析页码超出原件范围")
        required = {page.page for page in signals.pages if page.required}
        if not required.issubset(pages):
            if parsed.quality is None or decision.route_strategy != "mineru_whole":
                raise CompletenessError("PDF 部分非空页面缺少解析结果，需要人工核对")
            parsed.quality.content = "PARTIAL"
            parsed.quality.missing_pages = sorted(required - pages)
            if "MISSING_REQUIRED_PAGES" not in parsed.quality.warnings and len(parsed.quality.warnings) < 128:
                parsed.quality.warnings.append("MISSING_REQUIRED_PAGES")
        if parsed.quality is not None:
            parsed.quality.covered_pages = sorted(pages)
            parsed.quality.required_pages = sorted(required)
    if decision.file_type in OFFICE_TYPES and any(
        block.bbox is not None for block in parsed.blocks
    ):
        raise CompletenessError("Office 解析坐标必须为空，不得伪造原件坐标")
    if decision.route_strategy == "native":
        if decision.parse_mode == "auto" and decision.reason_codes != (
            "NATIVE_COVERAGE_SUPPORTED",
        ):
            raise CompletenessError("Native 不能静默忽略预检发现的未覆盖结构")
        if any(block.source != "native_parser" for block in parsed.blocks):
            raise CompletenessError("Native 结果来源不一致")
        if decision.file_type != "pdf" and pages != {1}:
            raise CompletenessError("Native 非分页文档必须使用逻辑页 1")
        if decision.file_type == "xlsx" and (
            len(parsed.blocks) != signals.nonempty_sheet_count
            or any(not block.section for block in parsed.blocks)
            or len({block.section for block in parsed.blocks}) != len(parsed.blocks)
        ):
            raise CompletenessError("XLSX 有效 Sheet 与解析结果不一致")
    elif decision.route_strategy == "mineru_whole":
        if any(block.source != "mineru_cloud" for block in parsed.blocks):
            raise CompletenessError("MinerU 结果来源不一致")
        if decision.file_type in {"jpg", "jpeg", "png"} and pages != {1}:
            raise CompletenessError("图片解析只能使用第 1 页")
        if decision.file_type in OFFICE_TYPES:
            limit = signals.page_count if signals.office_pdf_sha256 else 200
            if limit is None or any(page > limit for page in pages):
                raise CompletenessError("Office 转换后逻辑页超出已冻结范围")
            if signals.office_pdf_sha256:
                required = {page.page for page in signals.pages if page.required}
                if not required.issubset(pages):
                    if parsed.quality is None:
                        raise CompletenessError("Office转换PDF缺少要求覆盖的页面")
                    parsed.quality.content, parsed.quality.missing_pages = "PARTIAL", sorted(required - pages)
                    if "MISSING_REQUIRED_PAGES" not in parsed.quality.warnings and len(parsed.quality.warnings) < 128:
                        parsed.quality.warnings.append("MISSING_REQUIRED_PAGES")
                if parsed.quality is not None:
                    parsed.quality.covered_pages, parsed.quality.required_pages = sorted(pages), sorted(required)
    else:
        raise CompletenessError("尚未允许的路由结果不能写入最终 Block")
    if signals.office_snapshot_rel_count:
        if decision.file_type not in {"docx", "xlsx", "pptx"}:
            raise CompletenessError("保存快照范围与原件格式不一致")
        quality = parsed.quality or ParseQuality(base_block_count=len(parsed.blocks))
        warnings = list(dict.fromkeys(quality.warnings + list(OFFICE_SNAPSHOT_WARNINGS)))
        diagnostic = {"stage": "router.office_snapshot", "reason": "EXTERNAL_SOURCE_UNVERIFIED",
                      "count": signals.office_snapshot_rel_count}
        diagnostics = list(quality.diagnostics)
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
        if len(warnings) > 128 or len(diagnostics) > 128:
            raise CompletenessError("保存快照质量摘要超出安全上限")
        # 不更改块、原式或既有缺页/视觉记录；证明的是本地来源，未核验外部数据版本。
        quality.content, quality.warnings, quality.diagnostics = "PARTIAL", warnings, diagnostics
        parsed.quality = quality
