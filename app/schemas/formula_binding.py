"""阶段28参数及来源契约；金额/百分数保持原单位，不进行换算或求值。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.schemas.evidence import EvidenceTrace
from app.schemas.formula import FormulaReference, FormulaSemanticResult


class UserVariableInput(BaseModel):
    """用户明确指定的变量值；input_id由调用方提供以关联本次场景，不伪装为文档证据。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    value: StrictStr = Field(min_length=1, max_length=64)
    unit: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    input_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class EvidenceVariableInput(BaseModel):
    """调用方明确选中的数值引文；不自动从全文选择年份、期间或多个可能的参数。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    doc_id: UUID
    chunk_id: UUID
    block_id: UUID
    quote: StrictStr = Field(min_length=1, max_length=2000)
    value_text: StrictStr = Field(min_length=1, max_length=64)
    unit_text: StrictStr | None = Field(default=None, min_length=1, max_length=64)


class UserVariableSource(BaseModel):
    """保留人工输入身份及原始字面值，不生成虚构文档、页码或可信等级。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["user_input"] = "user_input"
    input_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    raw_value: StrictStr = Field(min_length=1, max_length=64)
    raw_unit: StrictStr | None = Field(min_length=1, max_length=64)


class EvidenceVariableSource(BaseModel):
    """通过当前Evidence反查的完整来源和精确数值/单位字符区间。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["document_evidence"] = "document_evidence"
    trace: EvidenceTrace
    quote_reference: FormulaReference
    value_reference: FormulaReference
    unit_reference: FormulaReference | None


class BoundFormulaVariable(BaseModel):
    """一条已绑定参数；label可未知，单位未知为null，不能由常识推断。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: StrictStr | None = Field(max_length=250)
    value: StrictStr = Field(pattern=r"^-?(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,12})?$", max_length=44)
    unit: StrictStr | None = Field(min_length=1, max_length=64)
    source: Annotated[UserVariableSource | EvidenceVariableSource, Field(discriminator="kind")]


class FormulaBindingResult(BaseModel):
    """稳定有序的已绑定值及缺参列表；success不表示数值语义/单位已验证或公式已经计算。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal["formula-binding-v1"] = "formula-binding-v1"
    status: Literal["success", "need_variables"]
    formula: FormulaSemanticResult
    variables: list[BoundFormulaVariable] = Field(max_length=32)
    required_variables: list[StrictStr] = Field(max_length=32)
    semantic_validation: Literal["manual_pending"] = "manual_pending"
    unit_validation: Literal["manual_pending"] = "manual_pending"
    calculation: Literal["not_performed"] = "not_performed"

    @model_validator(mode="after")
    def consistent_bindings(self):
        """缺参恰为公式变量减去已有绑定；拒绝多余/重复值以及假success。"""
        names = [item.name for item in self.variables]
        if (self.formula.status != "success" or names != sorted(set(names))
                or not set(names).issubset(self.formula.variables)
                or self.required_variables != sorted(set(self.formula.variables) - set(names))
                or (self.status == "need_variables") != bool(self.required_variables)):
            raise ValueError("公式参数与缺参状态不一致")
        return self


class FormulaBindingRequest(BaseModel):
    """显式绑定请求；不含提供方、Token、自动检索或付费模型开关。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    formula: FormulaSemanticResult
    user_inputs: list[UserVariableInput] = Field(default_factory=list, max_length=32)
    evidence_inputs: list[EvidenceVariableInput] = Field(default_factory=list, max_length=32)
