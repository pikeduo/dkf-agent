"""只读来源复核后受限AST→显式SymPy构造→精确结果；不解析任意字符串或调用云服务。"""

import ast
import hashlib
import json
import logging
from time import perf_counter
from uuid import uuid5

from app.core.business_logging import log_event
from app.schemas.formula_binding import FormulaBindingRequest, FormulaBindingResult
from app.schemas.formula_calculation import CalculationStep, CalculationValue, FormulaCalculationResult
from app.services.formula import FormulaError, normalize_expression
from app.services.formula_binding import FormulaBindingError, FormulaBindingService, number_literal
from app.services.rag.evidence import EvidenceError

VERSION = "formula-calculation-v1"
MAX_INTEGER_BITS = 4096
ERRORS = {
    "FORMULA_CALC_INPUT": "计算输入无效，必须提供项目公式绑定请求或结果",
    "FORMULA_CALC_SOURCE": "绑定值、引文或来源快照不一致，未修补或覆盖",
    "FORMULA_CALC_SYMBOLS": "表达式与允许变量不一致，未执行计算",
    "FORMULA_CALC_EXPRESSION": "表达式不在有界四则白名单内，未执行任意代码",
    "FORMULA_CALC_ZERO_DIVISION": "原表达式的某一步分母为零，未通过化简绕过",
    "FORMULA_CALC_LIMIT": "有理数中间结果超过资源限制，未返回部分计算",
    "FORMULA_CALC_DEPENDENCY": "SymPy依赖不可用，请在项目环境中手动检查",
    "FORMULA_CALC_RESULT": "未能生成完整有限的精确计算结果",
}


class FormulaCalculationError(Exception):
    """仅输出固定安全代码，不承载公式、参数、答案、来源正文或异常原文。"""

    def __init__(self, code):
        """未知错误统一为计算结果错误，不能冒充缺参或泄露底层异常。"""
        self.code = code if code in ERRORS else "FORMULA_CALC_RESULT"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


def binding_request(binding):
    """从已绑定来源重建原请求；重新绑定后还须整体比对，不能只相信序列化后的value。"""
    users, evidence = [], []
    for value in binding.variables:
        source = value.source
        if source.kind == "user_input":
            users.append({"name": value.name, "input_id": source.input_id,
                          "value": source.raw_value, "unit": source.raw_unit})
        else:
            evidence.append({"name": value.name, "doc_id": source.trace.doc_id, "chunk_id": source.trace.chunk_id,
                "block_id": source.quote_reference.block_id, "quote": source.quote_reference.quote,
                "value_text": source.value_reference.quote,
                "unit_text": source.unit_reference.quote if source.unit_reference is not None else None})
    return FormulaBindingRequest(formula=binding.formula, user_inputs=users, evidence_inputs=evidence)


def _sympy_backend():
    """仅在变量齐全后惰性导入已声明SymPy，不安装依赖、不接收用户字符串解析参数。"""
    try:
        import sympy
        return sympy
    except ImportError:
        raise FormulaCalculationError("FORMULA_CALC_DEPENDENCY") from None


def _rational_literal(text, backend):
    """只把严格十进制拆成Python整数分子/分母后传给Rational，绝不将字符串交给SymPy解析。"""
    value = number_literal(text)
    negative = value.startswith("-")
    whole, _, fraction = value.lstrip("-").partition(".")
    numerator = int(whole + fraction)
    return backend.Rational(-numerator if negative else numerator, 10 ** len(fraction))


def _bounded(value, backend):
    """每个中间值必须为有限有理数，分子/分母各至多4096二进制位，限制算术与输出规模。"""
    if not isinstance(value, backend.Rational) or value.is_finite is not True:
        raise FormulaCalculationError("FORMULA_CALC_RESULT")
    if max(abs(int(value.p)).bit_length(), int(value.q).bit_length()) > MAX_INTEGER_BITS:
        raise FormulaCalculationError("FORMULA_CALC_LIMIT")
    return value


def _display(value):
    """整数商余数实现12位半偶舍入，避免float和Decimal全局上下文导致双重舍入或不可复现。"""
    numerator, denominator = int(value.p), int(value.q)
    scaled, remainder = divmod(abs(numerator) * 10**12, denominator)
    if remainder * 2 > denominator or remainder * 2 == denominator and scaled % 2:
        scaled += 1
    whole, fraction = divmod(scaled, 10**12)
    decimal = f"{whole}.{fraction:012d}".rstrip("0").rstrip(".")
    if numerator < 0 and scaled:
        decimal = "-" + decimal
    exact = str(numerator) if denominator == 1 else f"{numerator}/{denominator}"
    return CalculationValue(exact=exact, decimal=decimal, rounded=remainder != 0)


def evaluate_expression(expression, names, substitution):
    """白名单逐节点构造SymPy树并按原树精确计算；每次除法先验零，不调用通用解析或化简。"""
    try:
        canonical, allowed = normalize_expression(expression)
    except FormulaError:
        raise FormulaCalculationError("FORMULA_CALC_EXPRESSION") from None
    if (allowed != names or set(substitution) != set(allowed)
            or any(number_literal(value) != value for value in substitution.values())):
        raise FormulaCalculationError("FORMULA_CALC_SYMBOLS")
    backend = _sympy_backend()
    symbols = {name: backend.Symbol(name, real=True) for name in allowed}
    values = {name: _rational_literal(value, backend) for name, value in substitution.items()}
    steps = []

    def walk(node, depth=0):
        """显式处理有限AST节点，同时保留未化简符号树、代入算式与逐步结果。"""
        if depth > 16:
            raise FormulaCalculationError("FORMULA_CALC_LIMIT")
        if isinstance(node, ast.Name) and node.id in symbols:
            return symbols[node.id], values[node.id], f"({substitution[node.id]})"
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            literal = ast.get_source_segment(canonical, node)
            value = _rational_literal(literal, backend)
            return value, value, literal
        if isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
            symbolic, value, text = walk(node.operand, depth + 1)
            negative = isinstance(node.op, ast.USub)
            result = _bounded(-value if negative else value, backend)
            tree = backend.Mul(backend.Integer(-1), symbolic, evaluate=False) if negative else symbolic
            steps.append(CalculationStep(index=len(steps) + 1, operation="negative" if negative else "positive",
                operands=[str(value)], result=str(result)))
            return tree, result, f"({'-' if negative else '+'}{text})"
        if not isinstance(node, ast.BinOp) or type(node.op) not in {ast.Add, ast.Sub, ast.Mult, ast.Div}:
            raise FormulaCalculationError("FORMULA_CALC_EXPRESSION")
        left_tree, left, left_text = walk(node.left, depth + 1)
        right_tree, right, right_text = walk(node.right, depth + 1)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaCalculationError("FORMULA_CALC_ZERO_DIVISION")
        if isinstance(node.op, ast.Add):
            operation, sign, value = "add", "+", left + right
            tree = backend.Add(left_tree, right_tree, evaluate=False)
        elif isinstance(node.op, ast.Sub):
            operation, sign, value = "subtract", "-", left - right
            tree = backend.Add(left_tree, backend.Mul(backend.Integer(-1), right_tree, evaluate=False), evaluate=False)
        elif isinstance(node.op, ast.Mult):
            operation, sign, value = "multiply", "*", left * right
            tree = backend.Mul(left_tree, right_tree, evaluate=False)
        else:
            operation, sign, value = "divide", "/", left / right
            tree = backend.Mul(left_tree, backend.Pow(right_tree, backend.Integer(-1), evaluate=False), evaluate=False)
        value = _bounded(value, backend)
        steps.append(CalculationStep(index=len(steps) + 1, operation=operation,
            operands=[str(left), str(right)], result=str(value)))
        return tree, value, f"({left_text} {sign} {right_text})"

    symbolic, result, substituted = walk(ast.parse(canonical, mode="eval").body)
    if symbolic.free_symbols - set(symbols.values()):
        raise FormulaCalculationError("FORMULA_CALC_SYMBOLS")
    return _display(_bounded(result, backend)), substituted, steps, backend.__version__


class FormulaCalculationService:
    """独立同步计算入口；复用阶段28只读校验，无模型、云调用、业务写入或Agent编排。"""

    def __init__(self, *, binding_service=None):
        """仅保存可注入的绑定边界，构造不导入SymPy、不读取配置或连接数据库。"""
        self._binding = binding_service if binding_service is not None else FormulaBindingService()

    def calculate_formula(self, value: FormulaBindingRequest | FormulaBindingResult):
        """请求直接绑定，结果重建并逐字段核对；缺参不计算，成功保留所有来源和确定性计算过程。"""
        started, scope, stage = perf_counter(), {}, "input"
        try:
            previous = None
            if isinstance(value, FormulaBindingResult):
                previous = FormulaBindingResult.model_validate(value.model_dump(mode="python"))
                request = binding_request(previous)
            elif isinstance(value, FormulaBindingRequest):
                request = FormulaBindingRequest.model_validate(value.model_dump(mode="python"))
            else:
                raise FormulaCalculationError("FORMULA_CALC_INPUT")
            trace = request.formula.source
            scope = {"kb_id": trace.kb_id, "doc_id": trace.doc_id, "chunk_id": trace.chunk_id,
                     "block_id": request.formula.formula_block_id}
            log_event("formula_calculation.started", **scope)
            stage = "source"
            bound = self._binding.bind_formula_variables(request.formula,
                user_inputs=request.user_inputs, evidence_inputs=request.evidence_inputs)
            if not isinstance(bound, FormulaBindingResult):
                raise FormulaCalculationError("FORMULA_CALC_SOURCE")
            bound = FormulaBindingResult.model_validate(bound.model_dump(mode="python"))
            if previous is not None and previous != bound:
                raise FormulaCalculationError("FORMULA_CALC_SOURCE")
            if bound.formula != request.formula:
                raise FormulaCalculationError("FORMULA_CALC_SOURCE")
            output = {"status": bound.status, "formula": bound.formula, "variables": bound.variables,
                "substitution": {item.name: item.value for item in bound.variables},
                "formula_source": bound.formula.source, "variable_sources": {item.name: item.source for item in bound.variables},
                "required_variables": bound.required_variables, "steps": [], "result": None,
                "substituted_formula": None, "calculation_id": None, "engine_version": None}
            if bound.status == "need_variables":
                result = FormulaCalculationResult(**output)
                log_event("formula_calculation.need_variables", **scope, status=result.status,
                    item_count=len(bound.variables), duration_ms=(perf_counter() - started) * 1000)
                return result
            stage = "calculation"
            computed, substituted, steps, engine_version = evaluate_expression(bound.formula.normalized_formula,
                bound.formula.variables, output["substitution"])
            frozen = json.dumps({"binding": bound.model_dump(mode="json"), "engine_version": engine_version,
                "version": VERSION}, sort_keys=True, ensure_ascii=False, allow_nan=False)
            calculation_id = uuid5(bound.formula.formula_id, hashlib.sha256(frozen.encode("utf-8")).hexdigest())
            result = FormulaCalculationResult(**(output | {"result": computed, "substituted_formula": substituted,
                "steps": steps, "calculation_id": calculation_id, "engine_version": engine_version}))
            log_event("formula_calculation.finished", **scope, status=result.status,
                item_count=len(bound.variables), duration_ms=(perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            error = (exc if isinstance(exc, (FormulaCalculationError, FormulaBindingError, EvidenceError))
                     else FormulaCalculationError("FORMULA_CALC_SOURCE" if stage == "source"
                                                  else "FORMULA_CALC_INPUT" if stage == "input" else "FORMULA_CALC_RESULT"))
            log_event("formula_calculation.failed", level=logging.WARNING, **scope, stage=stage,
                error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None


def calculate_formula(value: FormulaBindingRequest | FormulaBindingResult):
    """统一项目安全计算入口；只消费既有定义/绑定，禁止原始字符串、任意代码及收费兜底。"""
    return FormulaCalculationService().calculate_formula(value)
