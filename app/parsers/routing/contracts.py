"""仅包含结构摘要的路由契约，不保存正文、凭据或资源地址。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.file_formats import FileType

ParseMode = Literal["auto", "native", "mineru"]
RouteStrategy = Literal["native", "mineru_whole", "hybrid_bundle", "review"]
REASON_CODES = frozenset(
    {
        "PREFLIGHT_LIMIT",
        "EXPLICIT_NATIVE",
        "EXPLICIT_MINERU",
        "MINERU_UNSUPPORTED_FORMAT",
        "VISUAL_OR_LEGACY_FORMAT",
        "NATIVE_COVERAGE_SUPPORTED",
        "NATIVE_COMPLETENESS_FALLBACK",
        "MD_ASSET_NOT_UPLOADED",
        "XLSX_CLOUD_AB_REQUIRED",
        "XLSX_HAS_EMBEDDED_IMAGE",
        "XLSX_HAS_DRAWING",
        "XLSX_HAS_CHART",
        "XLSX_HAS_EMBEDDED_OBJECT",
        "XLSX_HAS_VML",
        "XLSX_HAS_EXTERNAL_LINK",
        "XLSX_UNSUPPORTED_SHEET_STRUCTURE",
        "DOCX_HAS_EMBEDDED_IMAGE",
        "DOCX_HAS_DRAWING",
        "DOCX_HAS_CHART",
        "DOCX_HAS_EMBEDDED_OBJECT",
        "DOCX_HAS_TEXTBOX",
        "DOCX_HAS_FORMULA",
        "DOCX_HAS_UNCOVERED_STRUCTURE",
        "DOCX_HAS_DIAGRAM",
        "DOCX_HAS_AUXILIARY_CONTENT",
        "PDF_SCAN_OR_NON_TEXT_PAGE",
        "PDF_TEXT_IMAGE_MIXED",
        "PDF_INSUFFICIENT_TEXT",
        "PDF_COMPLEX_DRAWING",
        "PDF_HAS_ANNOTATION",
        "PDF_MULTICOLUMN_RISK",
        "PDF_FORMULA_RISK",
        "PDF_ROTATED_TEXT",
    }
)


class PageSignals(BaseModel):
    """物理页结构摘要；面积是页面占比，不能当成语义识别置信度。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    page: int = Field(ge=1)
    text_chars: int = Field(ge=0)
    image_count: int = Field(ge=0)
    image_area_ratio: float = Field(ge=0, le=1)
    drawing_count: int = Field(ge=0)
    annotation_count: int = Field(ge=0)
    required: bool


class StructuralSignals(BaseModel):
    """固定白名单数值摘要；不允许任意 XML、路径、链接或文本进入决策。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    page_count: int | None = Field(default=None, ge=1)
    sheet_count: int = Field(default=0, ge=0)
    nonempty_sheet_count: int = Field(default=0, ge=0)
    formula_count: int = Field(default=0, ge=0)
    image_count: int = Field(default=0, ge=0)
    drawing_count: int = Field(default=0, ge=0)
    chart_count: int = Field(default=0, ge=0)
    embedded_count: int = Field(default=0, ge=0)
    image_reference_count: int = Field(default=0, ge=0)
    pages: tuple[PageSignals, ...] = ()


class RouteDecision(BaseModel):
    """可解释的代次决策；Hybrid 只预留名称，本版不会产生该策略。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decision_version: Literal["router-v1"] = "router-v1"
    parse_mode: ParseMode
    file_type: FileType
    route_strategy: RouteStrategy
    reason_codes: tuple[str, ...]
    signals: StructuralSignals
    fallback_count: int = Field(default=0, ge=0, le=1)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """只接受固定理由码，禁止决策快照借自由字符串透传上游原文或签名链接。"""

        if (
            not value
            or len(value) != len(set(value))
            or any(code not in REASON_CODES for code in value)
        ):
            raise ValueError("路由理由必须为非空、无重复的固定代码")
        return value


class Inspection(BaseModel):
    """预检结果与最终路由分离，显式 Native 仍可暴露已知遗漏风险。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    signals: StructuralSignals = StructuralSignals()
    risks: tuple[str, ...] = ()
    uncertain: bool = False
