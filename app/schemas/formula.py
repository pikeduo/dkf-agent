"""公式语义结构化的项目契约；来源来自已核对Evidence，不含计算值或框架对象。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.schemas.evidence import EvidenceTrace


class FormulaReference(BaseModel):
    """由本地唯一原文匹配生成的字符引用，保留Block与Chunk两端Unicode偏移。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    evidence_id: StrictInt = Field(ge=1, le=1024)
    block_id: UUID
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    chunk_start_char: StrictInt = Field(ge=0)
    chunk_end_char: StrictInt = Field(gt=0)
    quote: StrictStr = Field(min_length=1, max_length=2000)


class FormulaVariableMeaning(BaseModel):
    """变量仅包含语义解释及其原文引用；未知保持null，不在本阶段绑定数值/单位。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    meaning: StrictStr | None = Field(max_length=250)
    evidence: list[FormulaReference] = Field(max_length=4)

    @model_validator(mode="after")
    def grounded_meaning(self):
        """已知含义必须有证据，未知含义不能假装已经找到解释来源。"""
        if (self.meaning is None and self.evidence or self.meaning is not None
                and (not self.meaning.strip() or not self.evidence)):
            raise ValueError("变量含义与来源不一致")
        return self


class FormulaSemanticResult(BaseModel):
    """公式定义及可反查来源快照；success仅表示结构校验完成，语义仍待人工核验。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal["formula-semantic-v1"] = "formula-semantic-v1"
    status: Literal["success", "insufficient_evidence", "unsupported_formula"]
    formula_id: UUID
    formula_block_id: UUID
    formula_name: StrictStr | None
    raw_formula: StrictStr
    normalized_formula: StrictStr | None
    variables: list[StrictStr] = Field(max_length=32)
    variable_meanings: dict[str, FormulaVariableMeaning] = Field(max_length=32)
    formula_name_evidence: list[FormulaReference] = Field(max_length=4)
    source: EvidenceTrace
    semantic_validation: Literal["manual_pending"] = "manual_pending"

    @model_validator(mode="after")
    def consistent_definition(self):
        """拒绝成功空式、变量不一致或把模型拒答附带的半份定义发布为可用结果。"""
        if self.status != "success":
            if (self.formula_name is not None or self.normalized_formula is not None or self.variables
                    or self.variable_meanings or self.formula_name_evidence):
                raise ValueError("未完成的公式不能携带可执行定义")
        elif (not self.normalized_formula or len(set(self.variables)) != len(self.variables)
                or set(self.variables) != set(self.variable_meanings)
                or self.formula_name is None and self.formula_name_evidence
                or self.formula_name is not None and (not self.formula_name.strip() or not self.formula_name_evidence)):
            raise ValueError("公式定义或来源不一致")
        return self
