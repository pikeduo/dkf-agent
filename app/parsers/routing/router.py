"""纯自动决策：Native、受控云增强或安全拒绝，不设置人工 Review 策略。"""

from app.core.file_formats import MINERU_TYPES, FileType
from app.parsers.routing.contracts import Inspection, ParseMode, RouteDecision

WHOLE_TYPES = {"jpg", "jpeg", "png", "doc", "xls", "ppt", "pptx"}


def decide_route(
    kind: FileType, mode: ParseMode, inspection: Inspection, *, hybrid_enabled: bool = True
) -> RouteDecision:
    """生成固定理由；复杂 XLSX 优先预检 Hybrid，缺附件/资源超限直接失败。"""

    reasons = list(inspection.risks)
    if inspection.uncertain:
        strategy = "failed"
        if not reasons:
            reasons.append("PREFLIGHT_UNCERTAIN")
    elif mode != "native" and "EXTERNAL_ATTACHMENT_MISSING" in reasons:
        strategy = "failed"
    elif kind == "xlsx" and mode != "native" and (
        "XLSX_HAS_EXTERNAL_LINK" in reasons or "XLSX_EXTERNAL_ASSET_MISSING" in reasons
    ):
        strategy = "failed"
        if "XLSX_EXTERNAL_ASSET_MISSING" not in reasons:
            reasons.append("XLSX_EXTERNAL_ASSET_MISSING")
    elif mode == "native":
        strategy = "native"
        reasons.insert(0, "EXPLICIT_NATIVE")
    elif mode == "mineru":
        strategy = "mineru_whole" if kind in MINERU_TYPES else "failed"
        reasons.insert(
            0,
            "EXPLICIT_MINERU" if kind in MINERU_TYPES else "MINERU_UNSUPPORTED_FORMAT",
        )
    elif kind in WHOLE_TYPES:
        strategy = "mineru_whole"
        reasons.insert(0, "VISUAL_OR_LEGACY_FORMAT")
    elif kind == "xlsx" and reasons and hybrid_enabled:
        strategy = "hybrid_bundle"
        reasons.insert(0, "XLSX_HYBRID_ENABLED")
    elif kind == "md" and reasons:
        strategy = "failed"
    elif kind == "xlsx" and reasons:
        strategy = "mineru_whole"
        reasons.append("XLSX_WHOLE_WITH_NATIVE_PRIMARY")
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
