"""阶段35只读文档评估契约；推断与已知事实分开，不修改解析质量或业务状态。"""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from app.core.file_formats import FileType
from app.schemas.evidence import PageSemantics
from app.schemas.parsed_document import BlockType, BoundingBox

NonzeroUUID = Annotated[UUID, Field(description="已有业务记录的非零UUID")]


class AnalysisModel(BaseModel):
    """拒绝多余字段及非有限数，验证失败不暴露输入内容。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class DocumentAnalysisRequest(AnalysisModel):
    """默认只读现有Block；明确选择页面才读取原PDF/图片做有界视觉检查。"""

    kb_id: NonzeroUUID
    doc_id: NonzeroUUID
    visual_pages: list[Annotated[StrictInt, Field(ge=1)]] = Field(default_factory=list, max_length=5)

    @field_validator("kb_id", "doc_id")
    @classmethod
    def nonzero_identity(cls, value):
        """零UUID不能表示任意文档或扩大查询范围。"""
        if value.int == 0:
            raise ValueError("业务标识无效")
        return value

    @field_validator("visual_pages")
    @classmethod
    def unique_pages(cls, value):
        """明确拒绝重复页，保留调用方原始选择顺序。"""
        if len(value) != len(set(value)):
            raise ValueError("页面不能重复")
        return value


class BlockObservation(AnalysisModel):
    """仅物化正文前512字符及整块字符数，不加载整表、向量或来源JSON。"""

    block_id: UUID
    block_index: StrictInt | None = Field(default=None, ge=1)
    page: StrictInt = Field(ge=1)
    block_type: BlockType
    preview: StrictStr = Field(max_length=512)
    text_chars: StrictInt = Field(ge=0)
    section: StrictStr | None = Field(default=None, max_length=512)
    source: StrictStr = Field(min_length=1, max_length=64)
    bbox: BoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class AnalysisSource(AnalysisModel):
    """章节判断对应的原始Block字符片段；Hash仅针对该片段，不冒充整块核验。"""

    block_id: UUID
    block_index: StrictInt | None
    page: StrictInt = Field(ge=1)
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    quote: StrictStr = Field(min_length=1, max_length=512)
    quote_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent_fragment(self):
        """字符长度与片段Hash须相符，不能拿其他位置或正文摘要冒充来源。"""
        if (self.end_char - self.start_char != len(self.quote)
                or hashlib.sha256(self.quote.encode("utf-8")).hexdigest() != self.quote_sha256):
            raise ValueError("片段来源不一致")
        return self


class OutlineNode(AnalysisModel):
    """从正文标题恢复的派生层级；声明层级与实际可连接层级分别保留。"""

    node_id: UUID
    parent_id: UUID | None
    title: StrictStr
    level: StrictInt = Field(ge=1, le=6)
    declared_level: StrictInt = Field(ge=1, le=6)
    basis: Literal["numbering", "markdown", "parser_title"]
    reliability: Literal["parser_supported", "heuristic"]
    issues: list[StrictStr] = Field(default_factory=list)
    source: AnalysisSource


class TocEntry(AnalysisModel):
    """目录印刷页提示不替换真实页码；只在标题唯一匹配时关联正文节点。"""

    title: StrictStr
    printed_page_hint: StrictStr
    matched_node_id: UUID | None
    match_status: Literal["matched", "unmatched", "ambiguous"]
    source: AnalysisSource


class OutlineAssessment(AnalysisModel):
    """没有标题候选时明确不足，不用章节元数据伪造正文标题。"""

    status: Literal["recovered", "insufficient_evidence"]
    nodes: list[OutlineNode] = Field(default_factory=list)
    toc_entries: list[TocEntry] = Field(default_factory=list)
    limitations: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_hierarchy(self):
        """父节点必须真实且先于子节点；目录印刷页仅关联已有且唯一匹配的节点。"""
        seen = {}
        for node in self.nodes:
            if node.node_id in seen:
                raise ValueError("标题身份重复")
            if node.parent_id is not None:
                parent = seen.get(node.parent_id)
                if parent is None or node.level != min(6, parent.level + 1):
                    raise ValueError("标题父子结构无效")
            elif node.level != 1:
                raise ValueError("根标题层级无效")
            seen[node.node_id] = node
        if (bool(self.nodes) != (self.status == "recovered") or any(
                (entry.match_status == "matched") != (entry.matched_node_id is not None)
                or entry.matched_node_id is not None and entry.matched_node_id not in seen
                for entry in self.toc_entries)):
            raise ValueError("目录状态与来源不一致")
        return self


class AssessmentFinding(AnalysisModel):
    """固定可解释原因、观测数量和页；风险提示不等同于真实业务内容缺失。"""

    code: StrictStr
    severity: Literal["info", "review", "attention"]
    message: StrictStr
    count: StrictInt = Field(default=0, ge=0)
    pages: list[StrictInt] = Field(default_factory=list)


class ComplexityFactor(AnalysisModel):
    """公开规则分值是解析复杂度启发式，不是概率、质量分或处理耗时预测。"""

    code: StrictStr
    observed: StrictInt = Field(ge=0)
    points: StrictInt = Field(ge=0, le=100)
    explanation: StrictStr


class ComplexityAssessment(AnalysisModel):
    """只评价可观测部分；未知布局/总页数不会被当作简单或高质量。"""

    level: Literal["low", "medium", "high"]
    score: StrictInt = Field(ge=0, le=100)
    scope: Literal["observed_blocks_only"] = "observed_blocks_only"
    factors: list[ComplexityFactor]
    unknown_dimensions: list[StrictStr]


class VisualPageAssessment(AnalysisModel):
    """固定尺度的灰度启发式；疑似模糊/倾斜必须人工确认，不宣称OCR准确率。"""

    page: StrictInt = Field(ge=1)
    width: StrictInt = Field(ge=1)
    height: StrictInt = Field(ge=1)
    contrast: float = Field(ge=0)
    edge_energy: float = Field(ge=0)
    dark_pixel_ratio: float = Field(ge=0, le=1)
    blur: Literal["review", "no_observed_issue", "unknown"]
    skew_degrees: float | None
    skew: Literal["review", "no_observed_issue", "unknown"]


class QualityAssessment(AnalysisModel):
    """结构覆盖与图像观测分层，无全量语义质量分，也不升级PARTIAL_READY。"""

    status: Literal["attention", "review", "no_observed_issue", "unknown"]
    parse_content: Literal["COMPLETE", "PARTIAL"] | None
    findings: list[AssessmentFinding]
    visual_pages: list[VisualPageAssessment] = Field(default_factory=list)
    not_assessed: list[StrictStr]


class DocumentAnalysisReport(AnalysisModel):
    """绑定原件及解析代次的派生报告，未执行原文修复、重索引或数据库写入。"""

    version: Literal["document-analysis-v1"] = "document-analysis-v1"
    assessment_id: UUID
    kb_id: UUID
    doc_id: UUID
    file_name: StrictStr
    file_type: FileType
    file_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_parse_task_id: UUID
    document_status: Literal["READY", "PARTIAL_READY"]
    page_semantics: PageSemantics
    observed_pages: list[StrictInt]
    source_page_count: StrictInt | None = Field(default=None, ge=1)
    block_count: StrictInt = Field(ge=0)
    text_chars: StrictInt = Field(ge=0)
    block_types: dict[str, StrictInt]
    outline: OutlineAssessment
    complexity: ComplexityAssessment
    quality: QualityAssessment
    semantic_validation: Literal["manual_pending"] = "manual_pending"
