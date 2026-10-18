"""落库前覆盖检查；不能证明 OCR 语义准确，也不会伪造缺失内容。"""

from app.core.file_formats import OFFICE_TYPES
from app.parsers.common import ParserError
from app.parsers.routing.contracts import RouteDecision
from app.schemas.parsed_document import ParsedDocument


class CompletenessError(ParserError):
    """明确的结构覆盖缺失，仅 auto Native 路径可受控回退一次。"""


def guard_result(parsed: ParsedDocument, decision: RouteDecision) -> None:
    """保留身份/范围硬校验；MinerU v4缺页记录PARTIAL，Native完整性要求不变。"""

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
        if decision.file_type in OFFICE_TYPES and any(page > 200 for page in pages):
            raise CompletenessError("Office 转换后逻辑页超出平台范围")
    else:
        raise CompletenessError("尚未允许的路由结果不能写入最终 Block")
