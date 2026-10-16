"""阶段29待用户运行的Mock/本地有理数测试；无真实数据库、文档解析、模型或云API。"""

import ast
import copy
import io
import logging
from decimal import localcontext
from pathlib import Path
from uuid import uuid4

import pytest

from app.core import business_logging as logs
from app.schemas.formula_binding import FormulaBindingRequest
from app.services import formula_calculation as tool
from app.services.formula_binding import FormulaBindingError, FormulaBindingService
from app.services.rag.evidence import EvidenceError
from test_formula import LLMStub, extract, source, wire
from test_formula_binding import document_input, prepared, user


def calculation(expression, values):
    """仅用内存阶段27来源/响应构造显式绑定请求，不为测试生成真实文档。"""
    raw = "内存四则关系 = " + expression
    context = source(raw, context=None)
    formula = extract(context, LLMStub(wire(raw, expression, name=None, grounded=False)))
    request = FormulaBindingRequest(formula=formula,
        user_inputs=[user(name, value, unit=None) for name, value in values.items()])
    binding = FormulaBindingService(evidence_service=context[0])
    return tool.FormulaCalculationService(binding_service=binding), request, context


@pytest.mark.parametrize("expression,values,exact,decimal", [
    ("profit/revenue*100", {"profit": "20", "revenue": "100"}, "20", "20"),
    ("(revenue-cost)/revenue*100", {"revenue": "100", "cost": "80"}, "20", "20"),
    ("(current-previous)/previous*100", {"current": "120", "previous": "100"}, "20", "20"),
    ("(current-previous_year)/previous_year*100", {"current": "110", "previous_year": "100"}, "10", "10"),
    ("(current-previous_period)/previous_period*100", {"current": "90", "previous_period": "100"}, "-10", "-10"),
    ("part/total*100", {"part": "3", "total": "4"}, "75", "75"),
    ("(a*w+b*v)/(w+v)", {"a": "10", "b": "20", "w": "1", "v": "3"}, "35/2", "17.5"),
    ("a+b*c/d-e", {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"}, "-5/2", "-2.5"),
    ("85/100", {}, "17/20", "0.85"),
    ("a+b", {"a": "0.1", "b": "0.2"}, "3/10", "0.3"),
    ("-a+2.5", {"a": "1.5"}, "1", "1"),
    ("pi+e", {"pi": "3", "e": "2"}, "5", "5"),
])
def test_exact_arithmetic_families_follow_original_tree_and_keep_provenance(expression, values, exact, decimal):
    """九类目标及小数/负号/常量均精确计算；pi/e仅为显式符号，不隐式替换SymPy常量。"""
    service, request, context = calculation(expression, values)
    original = copy.deepcopy(request.model_dump()), context[2].text, context[3].processing_task_id
    result = service.calculate_formula(request)
    assert result.status == "success" and result.required_variables == []
    assert (result.result.exact, result.result.decimal, result.result.rounded) == (exact, decimal, False)
    assert result.formula == request.formula and result.formula_source == request.formula.source
    assert result.substitution == values and set(result.variable_sources) == set(values)
    assert all(source.kind == "user_input" for source in result.variable_sources.values())
    assert result.engine == "sympy" and result.engine_version
    assert result.semantic_validation == result.unit_validation == "manual_pending" and result.result_unit is None
    assert result.steps[-1].result == exact
    assert [step.index for step in result.steps] == list(range(1, len(result.steps) + 1))
    assert (request.model_dump(), context[2].text, context[3].processing_task_id) == original


@pytest.mark.parametrize("numerator,denominator,decimal", [
    ("1", "3", "0.333333333333"), ("1", "2000000000000", "0"),
    ("3", "2000000000000", "0.000000000002"), ("-3", "2000000000000", "-0.000000000002"),
])
def test_recurring_fraction_and_half_even_display_are_reproducible(numerator, denominator, decimal):
    """精确分数保留，12位展示半偶舍入；不把舍入值用于后续运算。"""
    service, request, _ = calculation("a/b", {"a": numerator, "b": denominator})
    result = service.calculate_formula(request)
    assert result.result.decimal == decimal and result.result.rounded is True
    assert result.result.decimal_places == 12 and result.result.rounding == "ROUND_HALF_EVEN"


def test_large_decimal_values_do_not_depend_on_global_decimal_precision():
    """绑定/算术只消费精确字符串，外部Decimal展示精度不能截断大数或参数尾数。"""
    service, request, _ = calculation("a+b", {"a": "123456789012345678901234567890", "b": "0.123456789012"})
    with localcontext() as context:
        context.prec = 2
        result = service.calculate_formula(request)
    assert result.result.decimal == "123456789012345678901234567890.123456789012"
    assert result.result.rounded is False


@pytest.mark.parametrize("expression,values", [("1/a", {"a": "0"}), ("a/(b-b)", {"a": "1", "b": "2"}),
    ("0*(1/a)", {"a": "0"}), ("(1/a)-(1/a)", {"a": "0"}), ("a/a", {"a": "0"}),
    ("1/(1/(a-a))", {"a": "1"})])
def test_every_original_denominator_is_checked_before_cancellation(expression, values):
    """零乘、相消及嵌套分式也检查原除法，不通过化简把未定义式变成成功。"""
    service, request, _ = calculation(expression, values)
    with pytest.raises(tool.FormulaCalculationError) as error:
        service.calculate_formula(request)
    assert error.value.code == "FORMULA_CALC_ZERO_DIVISION"


def test_missing_variables_never_import_sympy_or_fabricate_substitution(monkeypatch):
    """缺参沿用精确符号和现有来源，result/steps为空，不触发计算引擎。"""
    service, request, _ = calculation("a/b", {"a": "0"})

    def forbidden():
        """缺参时调用任何计算后端即失败。"""
        pytest.fail("missing variables must not load SymPy")

    monkeypatch.setattr(tool, "_sympy_backend", forbidden)
    result = service.calculate_formula(request)
    assert result.status == "need_variables" and result.required_variables == ["b"]
    assert result.result is result.substituted_formula is result.calculation_id is result.engine_version is None
    assert result.steps == [] and result.substitution == {"a": "0"}


@pytest.mark.parametrize("expression", ["__import__('os')", "a.__class__", "a[0]", "2**1000000", "sum([1,2])",
    "a//b", "a%b", "a; print(a)", "[x for x in a]", "lambda: 1", "1e3", "True", "I+1", "a" * 1025])
def test_unsafe_expression_is_rejected_before_sympy_backend(expression, monkeypatch):
    """代码、函数、属性、下标、幂及超限输入先被白名单拒绝，不交给SymPy字符串解析。"""
    def forbidden():
        """非法语法连SymPy导入都不应触发。"""
        pytest.fail("unsafe input must fail before backend")

    monkeypatch.setattr(tool, "_sympy_backend", forbidden)
    with pytest.raises(tool.FormulaCalculationError, match="FORMULA_CALC_EXPRESSION"):
        tool.evaluate_expression(expression, [], {})


def test_allowed_symbols_and_resource_limits_are_explicit(monkeypatch):
    """额外/缺失符号不使用默认值；每步有理数位数超限失败而非返回部分结果。"""
    with pytest.raises(tool.FormulaCalculationError, match="FORMULA_CALC_SYMBOLS"):
        tool.evaluate_expression("a+b", ["a"], {"a": "1"})
    service, request, _ = calculation("a*b", {"a": "100", "b": "100"})
    monkeypatch.setattr(tool, "MAX_INTEGER_BITS", 8)
    with pytest.raises(tool.FormulaCalculationError, match="FORMULA_CALC_LIMIT"):
        service.calculate_formula(request)


def test_calculation_source_has_no_arbitrary_python_or_sympy_parser_calls():
    """静态守卫禁止eval/exec/通用字符串解析器及lambdify进入生产计算边界。"""
    tree = ast.parse(Path(tool.__file__).read_text(encoding="utf-8"))
    forbidden = {"eval", "exec", "compile", "sympify", "parse_expr", "parse_latex", "lambdify"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            assert name not in forbidden


def test_direct_strings_and_extra_or_duplicate_parameters_are_not_calculation_inputs():
    """外部不能直接提交代码串绕过来源；未知/重复变量继续由原绑定服务拒绝。"""
    service, request, _ = calculation("a/b", {"a": "1", "b": "2"})
    with pytest.raises(tool.FormulaCalculationError, match="FORMULA_CALC_INPUT"):
        service.calculate_formula("PRIVATE_CODE")
    request.user_inputs.append(type(request.user_inputs[0])(name="unknown", value="1", unit=None, input_id="manual"))
    with pytest.raises(FormulaBindingError, match="FORMULA_BINDING_UNKNOWN"):
        service.calculate_formula(request)
    request.user_inputs[-1].name = "a"
    with pytest.raises(FormulaBindingError, match="FORMULA_BINDING_CONFLICT"):
        service.calculate_formula(request)


@pytest.mark.parametrize("change", ["value", "unit", "label", "source_value", "offset", "trace_hash"])
def test_serialized_binding_cannot_override_its_original_source(change):
    """重建原请求并核对整份绑定，篡改值/单位/来源字符位置不能被当作可信输入。"""
    context, _, formula, binding = prepared()
    bound = binding.bind_formula_variables(formula, user_inputs=[user("revenue", "100")],
        evidence_inputs=[document_input(context)])
    value = bound.variables[0]
    if change == "value":
        value.value = "99"
    elif change == "unit":
        value.unit = "美元"
    elif change == "label":
        value.label = "PRIVATE_LABEL"
    elif change == "source_value":
        value.source.value_reference.quote = "99"
    elif change == "offset":
        value.source.value_reference.start_char += 1
    else:
        value.source.trace.file_hash = "b" * 64
    with pytest.raises((tool.FormulaCalculationError, FormulaBindingError)):
        tool.FormulaCalculationService(binding_service=binding).calculate_formula(bound)


@pytest.mark.parametrize("change", ["generation", "hash", "not_ready", "foreign_kb"])
def test_current_evidence_is_rechecked_before_computation(change):
    """仍通过既有Evidence边界检查READY/Hash/代次及知识库，旧结果不自动复活或扩库。"""
    context, _, formula, binding = prepared()
    bound = binding.bind_formula_variables(formula, user_inputs=[user("profit", "20"), user("revenue", "100")])
    if change == "generation":
        context[3].processing_task_id = uuid4()
    elif change == "hash":
        context[3].file_hash = "b" * 64
    elif change == "not_ready":
        context[3].status = "INDEXING"
    else:
        context[3].kb_id = uuid4()
    with pytest.raises(EvidenceError):
        tool.FormulaCalculationService(binding_service=binding).calculate_formula(bound)


def test_request_and_bound_result_produce_same_id_steps_and_deep_copied_sources(monkeypatch):
    """同源/同参数/同引擎结果确定，不重提取公式、调用LLM、改变单位或写数据库。"""
    from app.services import formula as extraction, llm
    context, _, formula, binding = prepared()
    request = FormulaBindingRequest(formula=formula, user_inputs=[user("revenue", "100")],
        evidence_inputs=[document_input(context)])
    bound = binding.bind_formula_variables(request.formula, user_inputs=request.user_inputs, evidence_inputs=request.evidence_inputs)

    def forbidden(*args, **kwargs):
        """计算路径禁止隐式创建模型或再次提取公式。"""
        pytest.fail("no LLM allowed")

    monkeypatch.setattr(extraction, "extract_formula", forbidden)
    monkeypatch.setattr(extraction, "create_llm_service", forbidden)
    monkeypatch.setattr(llm, "create_llm_service", forbidden)
    service = tool.FormulaCalculationService(binding_service=binding)
    first, second = service.calculate_formula(request), service.calculate_formula(bound)
    assert first == second and first.result.exact == "20" and first.calculation_id
    first.variable_sources["profit"].trace.source_spans[0].bbox[0] = 99
    assert second.variable_sources["profit"].trace.source_spans[0].bbox[0] != 99
    assert second.formula.formula_id == formula.formula_id and context[1].closed


def test_units_and_percent_inputs_are_never_silently_converted():
    """数学结果仅来自显式数值，单位/比例业务解释仍manual_pending，不能自动除100。"""
    service, request, _ = calculation("a+b", {"a": "20", "b": "1"})
    request.user_inputs[0].unit = "%"
    request.user_inputs[1].unit = "元"
    result = service.calculate_formula(request)
    assert result.result.exact == "21" and result.result_unit is None
    assert [item.unit for item in result.variables] == ["%", "元"] and result.unit_validation == "manual_pending"


def test_calculation_logs_keep_safe_events_roles_and_ids_only():
    """日志保留过程、失败类别和角色，不记录原式、变量/数值、代入过程、结果或上下文。"""
    service, request, _ = calculation("a/b", {"a": "987654.123", "b": "2"})
    output, logger = io.StringIO(), logs.LOGGER
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("formula-calculation-cli"))
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)
    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        service.calculate_formula(request)
        request.user_inputs.pop()
        service.calculate_formula(request)
        request.user_inputs.append(type(request.user_inputs[0])(name="b", value="0", unit=None, input_id="scenario-1"))
        with pytest.raises(tool.FormulaCalculationError):
            service.calculate_formula(request)
        content = output.getvalue()
        for event in ("formula_calculation.started", "formula_calculation.finished",
                      "formula_calculation.need_variables", "formula_calculation.failed"):
            assert event in content
        assert "Formula Calculation CLI" in content and str(request.formula.source.chunk_id) in content and "pid=" in content
        assert "987654.123" not in content and request.formula.raw_formula not in content
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
