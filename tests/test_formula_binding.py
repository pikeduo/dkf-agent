"""阶段28纯内存Mock；供用户执行，不生成文档、不连接模型/真实数据库或云端。"""

import copy
import io
import logging
from uuid import uuid4

import pytest

from app.core import business_logging as logs
from app.schemas.formula_binding import FormulaBindingResult
from app.services import formula_binding as tool
from app.services.rag.evidence import EvidenceError
from test_formula import CONTEXT, LLMStub, extract, source, wire


def prepared(*, file_type="pdf", page=2):
    """用阶段27既有Stub取得有完整来源的定义，后续绑定不得再调用该LLM。"""
    context = source(context=CONTEXT + " 利润为20万元；营业收入为100万元。", file_type=file_type, page=page)
    llm = LLMStub()
    formula = extract(context, llm)
    return context, llm, formula, tool.FormulaBindingService(evidence_service=context[0])


def user(name, value, unit="万元", input_id="scenario-1"):
    """显式用户参数不是文档事实；字符串值避免二进制浮点隐式舍入。"""
    return {"name": name, "value": value, "unit": unit, "input_id": input_id}


def document_input(context, name="profit", value="20", quote="利润为20万元", unit="万元"):
    """只选择已有内存来源中的明确数值，不通过答案查找业务文档。"""
    _, _, chunk, document, blocks = context
    return {"name": name, "doc_id": document.doc_id, "chunk_id": chunk.chunk_id,
            "block_id": blocks[1].block_id, "quote": quote, "value_text": value, "unit_text": unit}


@pytest.mark.parametrize("inputs,missing", [([], ["profit", "revenue"]),
    ([user("profit", "0")], ["revenue"]), ([user("revenue", "100")], ["profit"])])
def test_missing_values_return_exact_symbols_without_guessing_or_zero_default(inputs, missing):
    """即使上下文存在数字也不自动选取；零是已绑定值，不是缺参。"""
    _, llm, formula, service = prepared()
    result = service.bind_formula_variables(formula, user_inputs=inputs)
    assert result.status == "need_variables" and result.required_variables == missing
    assert [item.name for item in result.variables] == sorted(item["name"] for item in inputs)
    assert len(llm.calls) == 1 and result.calculation == "not_performed"


def test_user_values_keep_raw_source_stable_order_and_do_not_compute_or_convert():
    """输入顺序不影响绑定；百分数保持数值+原单位，不除100、不执行公式或零分母计算。"""
    context, llm, formula, service = prepared()
    before = copy.deepcopy(formula.model_dump()), context[2].text, context[3].processing_task_id
    inputs = [user("revenue", "0", None), user("profit", "-1,234.5000", "%")]
    first = service.bind_formula_variables(formula, user_inputs=inputs)
    again = service.bind_formula_variables(formula, user_inputs=list(reversed(inputs)))
    assert first == again and first.status == "success" and first.required_variables == []
    assert [value.name for value in first.variables] == ["profit", "revenue"]
    value = first.variables[0]
    assert (value.label, value.value, value.unit) == ("利润", "-1234.5", "%")
    assert value.source.kind == "user_input" and value.source.raw_value == "-1,234.5000"
    assert value.source.input_id == "scenario-1" and not hasattr(value.source, "trace")
    assert first.formula.formula_id == formula.formula_id and first.semantic_validation == first.unit_validation == "manual_pending"
    assert (formula.model_dump(), context[2].text, context[3].processing_task_id) == before
    assert len(llm.calls) == 1
    first.formula.source.source_spans[0].bbox[0] = 99
    assert formula.source.source_spans[0].bbox != first.formula.source.source_spans[0].bbox


@pytest.mark.parametrize("file_type,page,semantics", [("pdf", 2, "physical_pdf"), ("docx", 5, "logical"),
                                                     ("png", 1, "image_page")])
def test_document_value_rechecks_trace_and_preserves_exact_numeric_unit_offsets(file_type, page, semantics):
    """参数引文和数值/单位均可反查原Block/Chunk；保持PDF、图片与Office真实页语义。"""
    context, _, formula, service = prepared(file_type=file_type, page=page)
    result = service.bind_formula_variables(formula, user_inputs=[user("revenue", "100")],
        evidence_inputs=[document_input(context)])
    assert result.status == "success"
    value = result.variables[0]
    assert value.value == "20" and value.source.kind == "document_evidence"
    assert value.source.trace.page_semantics == semantics and value.source.trace.page_start == page
    assert (value.source.trace.source_spans[0].bbox is None) == (semantics == "logical")
    for ref in (value.source.quote_reference, value.source.value_reference, value.source.unit_reference):
        assert context[-1][1].text[ref.start_char:ref.end_char] == ref.quote
        assert context[2].text[ref.chunk_start_char:ref.chunk_end_char] == ref.quote
    assert value.source.value_reference.quote == "20" and value.source.unit_reference.quote == "万元"


@pytest.mark.parametrize("value,expected", [("0", "0"), ("-0.00", "0"), ("+12.500", "12.5"),
    ("1,234,567.0100", "1234567.01"), ("123456789012345678901234567890.123456789012", "123456789012345678901234567890.123456789012")])
def test_strict_decimal_normalization_never_rounds_or_uses_float(value, expected):
    """超过默认Decimal上下文精度仍原样保留，不做求值或科学记数转换。"""
    assert tool.number_literal(value) == expected


@pytest.mark.parametrize("value", [True, 1.2, 100, "NaN", "Infinity", "1e3", "1/2", "20%", "12,34", "01", "", " 1", "1_000"])
def test_invalid_or_ambiguous_values_fail_without_source_lookup_or_private_error(value):
    """范围/比例/百分号字符串、浮点及非有限值不能猜成确定参数。"""
    context, _, formula, service = prepared()
    count = len(context[1].statements)
    with pytest.raises(tool.FormulaBindingError) as error:
        service.bind_formula_variables(formula, user_inputs=[user("profit", value)])
    assert error.value.code == "FORMULA_BINDING_INPUT" and len(context[1].statements) == count


@pytest.mark.parametrize("kind,expected", [("unknown", "FORMULA_BINDING_UNKNOWN"),
    ("duplicate", "FORMULA_BINDING_CONFLICT"), ("cross_source", "FORMULA_BINDING_CONFLICT")])
def test_unknown_duplicate_or_conflicting_inputs_never_override(kind, expected):
    """同值重复也要求调用方消歧；不默认用户优先、文档优先或按数值相同合并。"""
    context, _, formula, service = prepared()
    inputs, evidence = [user("profit", "20")], []
    if kind == "unknown":
        inputs.append(user("invented", "1"))
    elif kind == "duplicate":
        inputs.append(user("profit", "20", input_id="other"))
    else:
        evidence.append(document_input(context))
    with pytest.raises(tool.FormulaBindingError) as error:
        service.bind_formula_variables(formula, user_inputs=inputs, evidence_inputs=evidence)
    assert error.value.code == expected


@pytest.mark.parametrize("change", ["generation", "not_ready", "block_text", "source_snapshot", "formula_id", "raw_formula",
    "symbols", "expression", "meaning_reference"])
def test_stale_or_corrupt_formula_cannot_be_used_as_binding_source(change):
    """复用Evidence当前代次/Hash核对，并额外拒绝来源快照、公式ID/原式/变量引用被篡改。"""
    context, _, formula, service = prepared()
    if change == "generation":
        context[3].processing_task_id = uuid4()
    elif change == "not_ready":
        context[3].status = "INDEXING"
    elif change == "block_text":
        context[-1][0].text = "PRIVATE_CHANGED"
    elif change == "source_snapshot":
        formula.source.file_hash = "b" * 64
    elif change == "formula_id":
        formula.formula_id = uuid4()
    elif change == "raw_formula":
        formula.raw_formula = "PRIVATE_CHANGED"
    elif change == "symbols":
        formula.variables.append("invented")
    elif change == "expression":
        formula.normalized_formula = "__import__('PRIVATE')"
    else:
        formula.variable_meanings["profit"].evidence[0].start_char += 1
    with pytest.raises((tool.FormulaBindingError, EvidenceError)):
        service.bind_formula_variables(formula, user_inputs=[user("profit", "20")])


@pytest.mark.parametrize("change,code", [("quote", "FORMULA_BINDING_SOURCE"),
    ("number", "FORMULA_BINDING_SOURCE"), ("partial_number", "FORMULA_BINDING_SOURCE"),
    ("unit", "FORMULA_BINDING_UNIT"), ("separate_unit", "FORMULA_BINDING_UNIT"),
    ("block", "FORMULA_BINDING_SOURCE")])
def test_document_quote_number_and_unit_must_be_unique_and_consistent(change, code):
    """数值不能切自更大数字；不能拿另一数值的单位、随意补单位或指向不存在的块。"""
    context, _, formula, service = prepared()
    item = document_input(context)
    if change == "quote":
        item["quote"] = "PRIVATE_NOT_IN_SOURCE"
    elif change == "number":
        item["value_text"] = "999"
    elif change == "partial_number":
        item.update(value_text="0", quote="0万元", unit_text="万元")
    elif change == "unit":
        item["unit_text"] = "美元"
    elif change == "separate_unit":
        item.update(quote="利润为20万元；营业收入为100万元", unit_text="100万元")
    else:
        item["block_id"] = uuid4()
    with pytest.raises(tool.FormulaBindingError) as error:
        service.bind_formula_variables(formula, evidence_inputs=[item])
    assert error.value.code == code


def test_ambiguous_repeated_quote_and_unknown_unit_remain_safe():
    """重复原句不取第一次；单位未知保持null，不从公式含义或其他句子补齐。"""
    context = source(context=CONTEXT + " 利润为20万元。利润为20万元。")
    formula = extract(context, LLMStub())
    service = tool.FormulaBindingService(evidence_service=context[0])
    with pytest.raises(tool.FormulaBindingError, match="FORMULA_BINDING_SOURCE"):
        service.bind_formula_variables(formula, evidence_inputs=[document_input(context)])
    context, _, formula, service = prepared()
    value = service.bind_formula_variables(formula, evidence_inputs=[document_input(context, unit=None)]).variables[0]
    assert value.unit is None and value.source.unit_reference is None


@pytest.mark.parametrize("unit", [" ", "元\n", "\t元", "元\x00", "\ud800"])
def test_bad_units_fail_without_conversion_or_exception_payload(unit):
    """空白/控制字符/非法Unicode单位明确失败，不清洗成看似可靠的参数。"""
    with pytest.raises(tool.FormulaBindingError, match="FORMULA_BINDING_UNIT"):
        tool.checked_unit(unit)


def test_document_thousands_and_decimal_value_is_exact_and_not_unit_converted():
    """实际引文中的千分位/小数规范化为精确字符串；原数值和单位位置仍逐字可追溯。"""
    context = source(context=CONTEXT + " 利润为1,234.500元。")
    formula = extract(context, LLMStub())
    service = tool.FormulaBindingService(evidence_service=context[0])
    result = service.bind_formula_variables(formula, evidence_inputs=[document_input(context,
        value="1,234.500", quote="利润为1,234.500元", unit="元")])
    value = result.variables[0]
    assert value.value == "1234.5" and value.unit == "元"
    assert value.source.value_reference.quote == "1,234.500"


def test_binding_never_creates_llm_service_or_reextracts_formula(monkeypatch):
    """已有定义的参数绑定没有任何云调用/再提取入口，不因缺参自动付费。"""
    from app.services import formula as extraction, llm
    _, _, formula, service = prepared()

    def forbidden(*args, **kwargs):
        """任何构造LLM或提取公式调用都让用例失败。"""
        pytest.fail("binding must not invoke LLM")

    monkeypatch.setattr(llm, "create_llm_service", forbidden)
    monkeypatch.setattr(extraction, "create_llm_service", forbidden)
    monkeypatch.setattr(extraction, "extract_formula", forbidden)
    assert service.bind_formula_variables(formula).status == "need_variables"


def test_constant_formula_has_no_missing_variables_and_is_not_evaluated():
    """常量表达式没有输入变量，不强行造参数或输出计算值。"""
    raw = "常量比例 = 85%"
    context = source(raw, context=None)
    formula = extract(context, LLMStub(wire(raw, "85/100", name=None, grounded=False)))
    result = tool.FormulaBindingService(evidence_service=context[0]).bind_formula_variables(formula)
    assert result.status == "success" and result.variables == result.required_variables == []
    assert result.calculation == "not_performed" and not hasattr(result, "result")


def test_same_document_different_chunk_generations_are_rejected():
    """多个Chunk读取间发生文档重建时不把新旧代次拼成同一参数集合。"""
    context, _, formula, _ = prepared()
    later = formula.source.model_copy(deep=True)
    later.chunk_id, later.source_chunk_task_id = uuid4(), uuid4()

    class Resolver:
        """模拟同文档不同读取时刻的快照，不修改任何业务记录。"""

        def resolve(self, kb_id, chunk_id, *, doc_id):
            """先返回公式来源，后返回已经变化的切片代次。"""
            return formula.source if chunk_id == formula.source.chunk_id else later

    item = document_input(context)
    item["chunk_id"] = later.chunk_id
    with pytest.raises(tool.FormulaBindingError, match="FORMULA_BINDING_SOURCE"):
        tool.FormulaBindingService(evidence_service=Resolver()).bind_formula_variables(formula, evidence_inputs=[item])


def test_cross_document_values_require_explicit_identity_and_same_kb():
    """跨文档数值须显式指定且同知识库；解析器返回跨库来源全批拒绝而非删行。"""
    _, _, formula, _ = prepared()
    other_context, _, other, _ = prepared()
    other.source.kb_id = formula.source.kb_id
    calls = []

    class Resolver:
        """注入两个已核对的内存快照，不运行数据库。"""

        def resolve(self, kb_id, chunk_id, *, doc_id):
            """按调用身份返回快照，测试服务仍需检查所有返回范围。"""
            calls.append((kb_id, chunk_id, doc_id))
            return formula.source if chunk_id == formula.source.chunk_id else other.source

    service = tool.FormulaBindingService(evidence_service=Resolver())
    item = document_input(other_context)
    result = service.bind_formula_variables(formula, evidence_inputs=[item])
    assert result.variables[0].source.trace.doc_id == other.source.doc_id
    assert len(calls) == 2 and all(call[0] == formula.source.kb_id for call in calls)
    other.source.kb_id = uuid4()
    with pytest.raises(tool.FormulaBindingError, match="FORMULA_BINDING_SOURCE"):
        service.bind_formula_variables(formula, evidence_inputs=[item])


def test_logs_only_keep_roles_identifiers_counts_and_safe_statuses():
    """记录开始/缺参/成功/失败，禁止参数、单位、原式、变量名或引文进入日志。"""
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("formula-binding-cli"))
    logger = logs.LOGGER
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)
    context, _, formula, service = prepared()
    logger.handlers, logger.level, logger.propagate, logger.disabled = [handler], logging.INFO, False, False
    try:
        service.bind_formula_variables(formula)
        service.bind_formula_variables(formula, user_inputs=[user("profit", "987654.123", "PRIVATE_UNIT"), user("revenue", "100")])
        with pytest.raises(tool.FormulaBindingError):
            service.bind_formula_variables(formula, user_inputs=[user("profit", "PRIVATE_VALUE")])
        content = output.getvalue()
        assert all(event in content for event in ("formula_binding.started", "formula_binding.finished",
                                                  "formula_binding.need_variables", "formula_binding.failed"))
        assert "Formula Binding CLI" in content and str(formula.source.chunk_id) in content and "pid=" in content
        assert all(secret not in content for secret in ("PRIVATE", "987654.123", "revenue", "profit", formula.raw_formula))
    finally:
        logger.handlers, logger.level, logger.propagate, logger.disabled = previous
        logs.CONTEXT.reset(token)


def test_public_result_rejects_success_with_missing_or_duplicate_symbols():
    """公开契约不能把缺参洗成成功，也不能发布多余/重复绑定。"""
    _, _, formula, service = prepared()
    result = service.bind_formula_variables(formula).model_dump()
    result["status"] = "success"
    with pytest.raises(ValueError):
        FormulaBindingResult.model_validate(result)
