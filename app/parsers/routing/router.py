"""纯决策规则：整份解析、显式覆盖和保守 review，不调 HTTP 或写数据库。"""

from app.core.file_formats import MINERU_TYPES, FileType
from app.parsers.routing.contracts import Inspection, ParseMode, RouteDecision

WHOLE_TYPES = {"jpg", "jpeg", "png", "doc", "xls", "ppt", "pptx"}


def decide_route(
    kind: FileType, mode: ParseMode, inspection: Inspection
) -> RouteDecision:
    """根据覆盖风险生成稳定理由；显式 Native 允许能力对照但不声称视觉内容完整。"""

    reasons = list(inspection.risks)
    if inspection.uncertain:
        strategy = "review"
    elif mode == "native":
        strategy = "native"
        reasons.insert(0, "EXPLICIT_NATIVE")
    elif mode == "mineru":
        strategy = "mineru_whole" if kind in MINERU_TYPES else "review"
        reasons.insert(
            0,
            "EXPLICIT_MINERU" if kind in MINERU_TYPES else "MINERU_UNSUPPORTED_FORMAT",
        )
    elif kind in WHOLE_TYPES:
        strategy = "mineru_whole"
        reasons.insert(0, "VISUAL_OR_LEGACY_FORMAT")
    elif kind in {"md", "xlsx"} and reasons:
        strategy = "review"
        if kind == "xlsx":
            reasons.insert(0, "XLSX_CLOUD_AB_REQUIRED")
    elif kind in {"docx", "pdf"} and reasons:
        strategy = "mineru_whole"
    else:
        strategy = "native"
        reasons.append("NATIVE_COVERAGE_SUPPORTED")
    return RouteDecision(
        parse_mode=mode,
        file_type=kind,
        route_strategy=strategy,
        reason_codes=tuple(dict.fromkeys(reasons)),
        signals=inspection.signals,
    )


def fallback_route(decision: RouteDecision) -> RouteDecision:
    """只允许 auto Native 的一次明确完整性回退，其他模式和格式禁止循环回退。"""

    if (
        decision.parse_mode != "auto"
        or decision.route_strategy != "native"
        or decision.fallback_count
        or decision.file_type not in MINERU_TYPES
    ):
        raise ValueError("当前决策不允许回退云端解析")
    return decision.model_copy(
        update={
            "route_strategy": "mineru_whole",
            "fallback_count": 1,
            "reason_codes": decision.reason_codes + ("NATIVE_COMPLETENESS_FALLBACK",),
        }
    )
