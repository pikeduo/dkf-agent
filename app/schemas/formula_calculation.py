"""阶段29可复现计算结果；只含项目类型和来源快照，不暴露SymPy对象。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from app.schemas.evidence import EvidenceTrace
from app.schemas.formula import FormulaSemanticResult
from app.schemas.formula_binding import BoundFormulaVariable, EvidenceVariableSource, UserVariableSource


class CalculationValue(BaseModel):
    """精确分数是真值，小数是最多12位的半偶舍入展示；不使用JSON浮点数。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    exact: StrictStr = Field(max_length=2500)
    decimal: StrictStr = Field(max_length=1300)
    rounded: StrictBool
    decimal_places: Literal[12] = 12
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"


class CalculationStep(BaseModel):
    """按原AST后序记录每个四则/一元运算，不用代数化简隐藏原始除法。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    index: StrictInt = Field(ge=1, le=128)
    operation: Literal["add", "subtract", "multiply", "divide", "positive", "negative"]
    operands: list[StrictStr] = Field(min_length=1, max_length=2)
    result: StrictStr = Field(max_length=2500)


class FormulaCalculationResult(BaseModel):
    """来源核对与机械数学计算，不证明公式语义、单位、业务期间或模型推断正确。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal["formula-calculation-v1"] = "formula-calculation-v1"
    status: Literal["success", "need_variables"]
    calculation_id: UUID | None
    formula: FormulaSemanticResult
    variables: list[BoundFormulaVariable] = Field(max_length=32)
    substitution: dict[str, StrictStr] = Field(max_length=32)
    substituted_formula: StrictStr | None = Field(max_length=8192)
    result: CalculationValue | None
    formula_source: EvidenceTrace
    variable_sources: dict[str, UserVariableSource | EvidenceVariableSource] = Field(max_length=32)
    required_variables: list[StrictStr] = Field(max_length=32)
    steps: list[CalculationStep] = Field(max_length=128)
    engine: Literal["sympy"] = "sympy"
    engine_version: StrictStr | None = Field(max_length=64)
    calculation_scope: Literal["arithmetic_only"] = "arithmetic_only"
    semantic_validation: Literal["manual_pending"] = "manual_pending"
    unit_validation: Literal["manual_pending"] = "manual_pending"
    result_unit: None = None

    @model_validator(mode="after")
    def consistent_calculation(self):
        """成功必须参数齐全且有结果；缺参不能携带半份算式、步骤或假计算值。"""
        names = [value.name for value in self.variables]
        if (self.formula.status != "success" or names != sorted(set(names))
                or self.formula_source != self.formula.source
                or self.substitution != {value.name: value.value for value in self.variables}
                or self.variable_sources != {value.name: value.source for value in self.variables}
                or not set(names).issubset(self.formula.variables)
                or self.required_variables != sorted(set(self.formula.variables) - set(names))):
            raise ValueError("计算参数与来源不一致")
        if self.status == "success":
            if (self.required_variables or self.result is None or self.calculation_id is None
                    or not self.substituted_formula or not self.engine_version):
                raise ValueError("计算未完成")
        elif (not self.required_variables or self.result is not None or self.steps
                or self.substituted_formula is not None or self.calculation_id is not None or self.engine_version is not None):
            raise ValueError("缺参状态不能携带计算结果")
        return self
