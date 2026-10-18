"""仅包含结构摘要的路由契约，不保存正文、凭据或资源地址。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from app.core.file_formats import FileType

ParseMode = Literal["auto", "native", "mineru"]
RouteStrategy = Literal["native", "mineru_whole", "hybrid_bundle", "failed"]
PDF_COVERAGE_VERSION = "pdf-coverage-v2"
OFFICE_SNAPSHOT_VERSION = "office-saved-snapshot-v1"
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
        "PREFLIGHT_UNCERTAIN",
        "EXTERNAL_ATTACHMENT_MISSING",
        "OFFICE_SAVED_SNAPSHOT",
        "XLSX_EXTERNAL_ASSET_MISSING",
        "XLSX_WHOLE_WITH_NATIVE_PRIMARY",
        "HYBRID_PREFLIGHT_WHOLE_FALLBACK",
        "HYBRID_UNSUPPORTED_ANCHOR",
        "HYBRID_UNSUPPORTED_CHART",
        "HYBRID_UNSUPPORTED_IMAGE_TRANSFORM",
        "HYBRID_UNSUPPORTED_IMAGE_FORMAT",
        "HYBRID_UNSUPPORTED_DRAWING",
        "HYBRID_UNSUPPORTED_SHEET",
        "HYBRID_EXTERNAL_ASSET_MISSING",
        "HYBRID_INVALID_PACKAGE",
        "HYBRID_INVALID_IMAGE",
        "HYBRID_ORPHAN_PART",
        "XLSX_HYBRID_ENABLED",
        "HYBRID_UNSUPPORTED_STRUCTURE",
        "HYBRID_RESOURCE_LIMIT",
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
    decorative_drawing_count: int = Field(default=0, ge=0)
    coverage_reason: Literal["legacy", "empty", "content_or_uncertain", "margin_rule_only"] = "legacy"

    @model_validator(mode="after")
    def validate_coverage(self):
        """新摘要必须说明白名单依据；旧快照保留 required，不通过默认值重新推断。"""
        if self.coverage_reason == "legacy":
            valid = self.decorative_drawing_count == 0
        elif self.coverage_reason == "empty":
            valid = not (self.required or self.text_chars or self.image_count or self.drawing_count
                         or self.annotation_count or self.decorative_drawing_count)
        elif self.coverage_reason == "margin_rule_only":
            valid = (not self.required and not (self.text_chars or self.image_count or self.annotation_count)
                     and self.drawing_count == self.decorative_drawing_count > 0)
        else:
            valid = self.required and self.decorative_drawing_count == 0
        if not valid:
            raise ValueError("PDF 页面覆盖依据与结构计数不一致")
        return self


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
    # 非零只表示已证明存在的包内保存内容；外部更新源仍未核验，不声明完整。
    office_snapshot_rel_count: int = Field(default=0, strict=True, ge=0)
    pages: tuple[PageSignals, ...] = ()
    # 缺省视为旧规则；新预检显式冻结独立版本，不改 Normalizer/供应商参数。
    pdf_coverage_version: Literal["pdf-coverage-v1", "pdf-coverage-v2"] = "pdf-coverage-v1"

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        """旧摘要不追加零计数字段，保持已有Job/主结果检查点的序列化身份。"""
        value = handler(self)
        if not self.office_snapshot_rel_count:
            value.pop("office_snapshot_rel_count", None)
        return value

    @model_validator(mode="after")
    def validate_pdf_coverage_version(self):
        """禁止新旧覆盖依据混装，确保旧付费 Job 不会因读取而自动升级。"""
        legacy = self.pdf_coverage_version == "pdf-coverage-v1"
        if any((page.coverage_reason == "legacy") != legacy for page in self.pages):
            raise ValueError("PDF 页面覆盖规则版本与逐页依据不一致")
        return self


class RouteDecision(BaseModel):
    """可解释的自动/管理员覆盖决策；failed 是拒绝终态，不是人工路由策略。"""

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
