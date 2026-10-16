"""显式参数绑定：用户值/已核对引文→变量及来源；无LLM、计算、落库或自动检索。"""

import hashlib
import logging
import re
from decimal import Decimal
from time import perf_counter

from app.core.business_logging import log_event
from app.schemas.evidence import EvidenceTrace
from app.schemas.formula import FormulaReference, FormulaSemanticResult
from app.schemas.formula_binding import (
    BoundFormulaVariable, EvidenceVariableSource, FormulaBindingRequest, FormulaBindingResult, UserVariableSource,
)
from app.services.formula import FormulaError, validate_formula_snapshot
from app.services.rag.evidence import EvidenceError, EvidenceService

NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]{0,29}|[1-9][0-9]{0,2}(?:,[0-9]{3}){1,9})(?:\.[0-9]{1,12})?\Z")
ERRORS = {
    "FORMULA_BINDING_INPUT": "参数输入或数值格式无效，未猜测或转换数值",
    "FORMULA_BINDING_SOURCE": "公式或参数来源不一致、过期或引文不唯一，未修复来源",
    "FORMULA_BINDING_UNKNOWN": "参数不属于当前公式，未新增变量或推断别名",
    "FORMULA_BINDING_CONFLICT": "同一变量被重复提供，未自动覆盖或选择来源",
    "FORMULA_BINDING_UNIT": "单位未与所选原数值明确相邻，未猜测或换算单位",
}


class FormulaBindingError(Exception):
    """固定安全错误，不透传输入值、引文、路径、连接串或上游异常。"""

    def __init__(self, code):
        """未知异常安全归为输入错误，完整业务失败不能伪装缺参。"""
        self.code = code if code in ERRORS else "FORMULA_BINDING_INPUT"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def number_literal(value):
    """严格识别有限普通十进制/合法千分组并无损规范化；不支持指数、百分号、范围或表达式。"""
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise FormulaBindingError("FORMULA_BINDING_INPUT")
    # format不应用Decimal上下文精度，避免对大数normalize时发生隐式舍入。
    result = format(Decimal(value.replace(",", "")), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if Decimal(result).is_zero() else result


def checked_unit(value):
    """单位只保留用户或证据原文，拒绝控制字符/空白占位，不进行货币、量纲或百分比换算。"""
    if value is not None:
        try:
            if (not isinstance(value, str) or not value.strip() or value != value.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError
            value.encode("utf-8", errors="strict")
        except Exception:
            raise FormulaBindingError("FORMULA_BINDING_UNIT") from None
    return value


def reference(span, evidence_id, start, end):
    """将已核对片段内部位置映射到Block/Chunk双端Unicode区间，保留真实原文。"""
    return FormulaReference(evidence_id=evidence_id, block_id=span.block_id,
        start_char=span.start_char + start, end_char=span.start_char + end,
        chunk_start_char=span.chunk_start_char + start, chunk_end_char=span.chunk_start_char + end,
        quote=span.text[start:end])


def document_value(item, trace):
    """唯一完整数值才绑定；句号须后接空白或经Hash确认的整块末尾，不改写来源/单位。"""
    matches = [(index, span) for index, span in enumerate(trace.source_spans, 1) if span.block_id == item.block_id]
    if len(matches) != 1:
        raise FormulaBindingError("FORMULA_BINDING_SOURCE")
    evidence_id, span = matches[0]
    position = span.text.find(item.quote)
    offset = item.quote.find(item.value_text)
    if (not item.quote.strip() or position < 0 or span.text.find(item.quote, position + 1) >= 0
            or offset < 0 or item.quote.find(item.value_text, offset + 1) >= 0):
        raise FormulaBindingError("FORMULA_BINDING_SOURCE")
    number = number_literal(item.value_text)
    start, end = position + offset, position + offset + len(item.value_text)
    # 保留左边界完整性；右侧句号仅在无数字延续时视为标点，不删除/裁剪原文或引文。
    left, right = span.text[start - 1:start] if start else "", span.text[end:end + 1]
    sentence_period = right == "." and (
        end + 1 < len(span.text) and span.text[end + 1].isspace()
        or end + 1 == len(span.text)
        and hashlib.sha256(span.text.encode("utf-8")).hexdigest() == span.block_text_hash
    )
    if (left and left in "0123456789.,eE+-"
            or right and right in "0123456789.,eE+-" and not sentence_period):
        raise FormulaBindingError("FORMULA_BINDING_SOURCE")
    unit_ref, unit = None, checked_unit(item.unit_text)
    if unit is not None:
        unit_offset = item.quote.find(unit)
        if unit_offset < 0 or item.quote.find(unit, unit_offset + 1) >= 0:
            raise FormulaBindingError("FORMULA_BINDING_UNIT")
        unit_start, unit_end = position + unit_offset, position + unit_offset + len(unit)
        if any(char and char.isascii() and char.isalpha() for char in (
                span.text[unit_start - 1:unit_start] if unit_start else "", span.text[unit_end:unit_end + 1])):
            raise FormulaBindingError("FORMULA_BINDING_UNIT")
        left = unit_end <= start and not span.text[unit_end:start].strip()
        right = unit_start >= end and not span.text[end:unit_start].strip()
        if not (left or right):
            raise FormulaBindingError("FORMULA_BINDING_UNIT")
        unit_ref = reference(span, evidence_id, unit_start, unit_end)
    return number, unit, EvidenceVariableSource(trace=trace.model_copy(deep=True),
        quote_reference=reference(span, evidence_id, position, position + len(item.quote)),
        value_reference=reference(span, evidence_id, start, end), unit_reference=unit_ref)


class FormulaBindingService:
    """只读来源、参数识别和绑定的独立边界；不会提取新公式、加载模型或调用收费API。"""

    def __init__(self, *, evidence_service=None):
        """保存可注入的既有Evidence服务；构造不连接数据库。"""
        self._evidence = evidence_service if evidence_service is not None else EvidenceService()

    def bind_formula_variables(self, formula: FormulaSemanticResult, *, user_inputs=None, evidence_inputs=None):
        """绑定显式输入并返回精确缺参；重复/未知/坏来源整体失败，无默认值、覆盖或自动换算。"""
        started, scope, stage = perf_counter(), {}, "input"
        try:
            request = FormulaBindingRequest.model_validate({"formula": formula,
                "user_inputs": [] if user_inputs is None else user_inputs,
                "evidence_inputs": [] if evidence_inputs is None else evidence_inputs})
            formula = request.formula.model_copy(deep=True)
            trace = formula.source
            scope = {"kb_id": trace.kb_id, "doc_id": trace.doc_id, "chunk_id": trace.chunk_id,
                     "block_id": formula.formula_block_id}
            log_event("formula_binding.started", **scope)
            inputs = [*request.user_inputs, *request.evidence_inputs]
            names = [item.name for item in inputs]
            if any(name not in formula.variables for name in names):
                raise FormulaBindingError("FORMULA_BINDING_UNKNOWN")
            if len(names) != len(set(names)):
                raise FormulaBindingError("FORMULA_BINDING_CONFLICT")
            # 先核对全部纯输入；坏值/零UUID不导致不必要的数据库读取。
            for item in request.user_inputs:
                number_literal(item.value)
                checked_unit(item.unit)
            for item in request.evidence_inputs:
                if not all(value.int for value in (item.doc_id, item.chunk_id, item.block_id)):
                    raise FormulaBindingError("FORMULA_BINDING_INPUT")
                number_literal(item.value_text)
                checked_unit(item.unit_text)
            stage = "source"
            snapshots = {}

            def resolve(chunk_id, doc_id):
                """固定原知识库及显式文档范围，同次同源复用快照，不跨库检索或扩大证据。"""
                key = chunk_id, doc_id
                if key not in snapshots:
                    current = self._evidence.resolve(trace.kb_id, chunk_id, doc_id=doc_id)
                    if not isinstance(current, EvidenceTrace):
                        raise FormulaBindingError("FORMULA_BINDING_SOURCE")
                    current = EvidenceTrace.model_validate(current.model_dump(mode="python"))
                    if (current.kb_id != trace.kb_id or current.chunk_id != chunk_id or current.doc_id != doc_id):
                        raise FormulaBindingError("FORMULA_BINDING_SOURCE")
                    for previous in snapshots.values():
                        if previous.doc_id == current.doc_id and any(getattr(previous, field) != getattr(current, field)
                                for field in ("file_hash", "source_parse_task_id", "source_chunk_task_id")):
                            raise FormulaBindingError("FORMULA_BINDING_SOURCE")
                    snapshots[key] = current
                return snapshots[key]

            formula = validate_formula_snapshot(formula, resolve(trace.chunk_id, trace.doc_id))
            stage, values = "binding", {}
            for item in request.user_inputs:
                values[item.name] = BoundFormulaVariable(name=item.name,
                    label=formula.variable_meanings[item.name].meaning,
                    value=number_literal(item.value), unit=checked_unit(item.unit),
                    source=UserVariableSource(input_id=item.input_id, raw_value=item.value, raw_unit=item.unit))
            for item in request.evidence_inputs:
                value, unit, source = document_value(item, resolve(item.chunk_id, item.doc_id))
                values[item.name] = BoundFormulaVariable(name=item.name,
                    label=formula.variable_meanings[item.name].meaning, value=value, unit=unit, source=source)
            missing = sorted(set(formula.variables) - set(values))
            result = FormulaBindingResult(status="need_variables" if missing else "success", formula=formula,
                variables=[values[name] for name in sorted(values)], required_variables=missing)
            log_event("formula_binding.need_variables" if missing else "formula_binding.finished",
                **scope, status=result.status, item_count=len(values), duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            error = (exc if isinstance(exc, (FormulaBindingError, EvidenceError)) else FormulaBindingError(
                "FORMULA_BINDING_SOURCE" if isinstance(exc, FormulaError) or stage == "source" else "FORMULA_BINDING_INPUT"))
            log_event("formula_binding.failed", level=logging.WARNING, **scope, stage=stage,
                error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None


def bind_formula_variables(formula: FormulaSemanticResult, *, user_inputs=None, evidence_inputs=None):
    """统一项目入口；只绑定已提取公式，默认缺参列表，不执行LLM、代入或计算。"""
    return FormulaBindingService().bind_formula_variables(formula, user_inputs=user_inputs, evidence_inputs=evidence_inputs)
