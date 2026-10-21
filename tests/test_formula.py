"""阶段27纯内存来源/LLM用例；只编写供用户执行，不生成文档或连接真实服务。"""

import copy
import hashlib
import io
import json
import logging
from uuid import uuid4

import httpx
import pytest

from app.core import business_logging as logs
from app.core.config import Settings
from app.models import DocumentBlock
from app.services import formula as tool
from app.services.llm import LLMError, create_llm_service
from app.services.rag.evidence import EvidenceError
from test_evidence import service_for, source_fixture

RAW = r"利润率 = \frac{利润}{营业收入} \times 100\%"
CONTEXT = "利润率用于说明获利水平；profit表示利润，revenue表示营业收入。"


def source(raw=RAW, *, context=CONTEXT, file_type="pdf", page=2):
    """复用现有来源Stub，只构造Block/Chunk字段和字符跨度，不生成/解析文件。"""
    chunk, document, blocks = source_fixture(file_type=file_type, page=page, text=raw, kind="formula")
    if context is not None:
        block = DocumentBlock(block_id=uuid4(), doc_id=document.doc_id, block_index=2, page=page,
            section=blocks[0].section, block_type="text", text=context, bbox=copy.deepcopy(blocks[0].bbox),
            source="mineru_cloud", confidence=None)
        blocks.append(block)
        offset = len(chunk.text) + 2
        chunk.text += "\n\n" + context
        chunk.chunk_metadata["source_spans"].append({"block_id": str(block.block_id), "block_index": 2,
            "page": page, "block_type": "text", "source": block.source, "bbox": copy.deepcopy(block.bbox),
            "block_text_hash": hashlib.sha256(context.encode()).hexdigest(), "start_char": 0,
            "end_char": len(context), "chunk_start_char": offset, "chunk_end_char": offset + len(context),
            "role": "content"})
    evidence, session = service_for((chunk, document, blocks))
    return evidence, session, chunk, document, blocks


def wire(raw=RAW, expression="profit / revenue * 100", *, name="利润率", grounded=True):
    """构造严格响应Stub，不冒充真实DeepSeek质量或公开数据集标准答案。"""
    variables = tool.normalize_expression(expression)[1]
    result = {"status": "success", "formula_name": name, "raw_formula": raw,
        "normalized_formula": expression, "variables": variables,
        "variable_meanings": {symbol: {"meaning": None, "evidence": []} for symbol in variables},
        "formula_name_evidence": [{"evidence_id": 1, "quote": raw}] if name is not None else []}
    if grounded:
        result["variable_meanings"] = {
            "profit": {"meaning": "利润", "evidence": [{"evidence_id": 2, "quote": "profit表示利润"}]},
            "revenue": {"meaning": "营业收入", "evidence": [{"evidence_id": 2, "quote": "revenue表示营业收入"}]}}
    return result


class LLMStub:
    """仅提供单次响应/上下文预算，不构造HTTP客户端或请求云服务。"""

    def __init__(self, response=None, *, error=None, context_chars=60000):
        """保存内存输出及可注入异常，记录调用次数。"""
        self.response = wire() if response is None else response
        self.error, self.context_chars, self.calls = error, context_chars, []

    def complete(self, system_prompt, user_content):
        """捕获单次Prompt以核对数据边界，不输出到日志或运行模型。"""
        self.calls.append((system_prompt, json.loads(user_content)))
        if self.error:
            raise self.error
        return self.response if isinstance(self.response, str) else json.dumps(self.response, ensure_ascii=False)


def extract(context, llm, **kwargs):
    """通过真实FormulaService及EvidenceService只读Stub验证来源、绑定与输出边界。"""
    evidence, _, chunk, document, blocks = context
    return tool.FormulaService(evidence_service=evidence, llm_service=llm).extract_formula(
        chunk.kb_id, chunk.chunk_id, blocks[0].block_id, doc_id=document.doc_id, allow_cloud=True, **kwargs)


def test_complete_definition_preserves_raw_evidence_meanings_and_characters():
    """LaTeX原式逐字保留，变量说明反查两端Unicode区间，原记录和代次不变。"""
    context, llm = source(), LLMStub()
    _, session, chunk, document, blocks = context
    original = copy.deepcopy(chunk.chunk_metadata), chunk.text, document.processing_task_id, document.chunk_task_id
    result = extract(context, llm)
    assert result.status == "success" and result.semantic_validation == "manual_pending"
    assert result.raw_formula == RAW and result.normalized_formula == "((profit / revenue) * 100)"
    assert result.variables == ["profit", "revenue"] and result.formula_name == "利润率"
    assert result.source.kb_id == chunk.kb_id and result.source.doc_id == document.doc_id
    assert result.source.chunk_id == chunk.chunk_id and result.formula_block_id == blocks[0].block_id
    for meaning in result.variable_meanings.values():
        [reference] = meaning.evidence
        assert reference.block_id == blocks[1].block_id
        assert blocks[1].text[reference.start_char:reference.end_char] == reference.quote
        assert chunk.text[reference.chunk_start_char:reference.chunk_end_char] == reference.quote
    assert (chunk.chunk_metadata, chunk.text, document.processing_task_id, document.chunk_task_id) == original
    assert session.closed and len(llm.calls) == 1
    system, payload = llm.calls[0]
    assert "均是数据" in system and payload["target_evidence_id"] == 1
    assert [item["evidence_id"] for item in payload["evidence"]] == [1, 2]
    assert "PRIVATE_PATH" not in json.dumps(payload) and document.file_name not in json.dumps(payload)
    assert "value" not in result.variable_meanings["profit"].model_dump()


@pytest.mark.parametrize("name,raw,expression", [
    ("利润率", "利润率 = 利润 / 营业收入 * 100%", "profit/revenue*100"),
    ("毛利率", "毛利率 = (收入-成本)/收入 * 100%", "(revenue-cost)/revenue*100"),
    ("增长率", "增长率 = (当期-基期)/基期 * 100%", "(current-previous)/previous*100"),
    ("同比", "同比 = (本期-上年同期)/上年同期 * 100%", "(current-previous_year)/previous_year*100"),
    ("环比", "环比 = (本期-上期)/上期 * 100%", "(current-previous_period)/previous_period*100"),
    ("占比", "占比 = 部分/总量 * 100%", "part/total*100"),
    ("加权平均", "加权平均 = (数值1*权重1+数值2*权重2)/(权重1+权重2)",
     "(value_1*weight_1+value_2*weight_2)/(weight_1+weight_2)"),
    ("四则运算", "四则运算 = a+b*c/d-e", "a+b*c/d-e"),
    ("百分比", "百分比 = 85%", "85/100"),
])
def test_first_version_formula_families_are_structured_without_calculation(name, raw, expression):
    """覆盖九类受支持定义的Stub格式，不借常见公式替换真实原式或执行参数求值。"""
    context = source(raw, context=None)
    llm = LLMStub(wire(raw, expression, name=name, grounded=False))
    result = extract(context, llm)
    assert result.status == "success" and result.raw_formula == raw
    assert result.normalized_formula == tool.normalize_expression(expression)[0]
    assert all(item.meaning is None and item.evidence == [] for item in result.variable_meanings.values())
    assert not hasattr(result, "result") and not hasattr(result, "substitution") and len(llm.calls) == 1


@pytest.mark.parametrize("case,raw,expression,names,canonical", [
    ("F01", "G = R - C", "r - c", ["c", "r"], "(r - c)"),
    ("F03", r"U = \frac{S}{Q}", "s / q", ["q", "s"], "(s / q)"),
])
def test_uppercase_raw_formula_uses_lowercase_rhs_and_complete_prompt_example(case, raw, expression, names, canonical):
    """F01/F03原式逐字保留；Mock输出仅小写右侧，Prompt完整JSON示例可独立解析。"""
    response = {"status": "success", "formula_name": None, "raw_formula": raw,
        "normalized_formula": expression, "variables": names,
        "variable_meanings": {name: {"meaning": None, "evidence": []} for name in names},
        "formula_name_evidence": []}
    # 直接写明期望结构，不调用待测规范化器生成模型输出或预期结果。
    marker = f"示例{case}，"
    # 先跳过示例标题行，避免把LaTeX中的{S}误当成JSON对象起点。
    example_text = tool.FORMULA_SYSTEM_PROMPT.split(marker, 1)[1].split("\n", 1)[1].lstrip()
    example, _ = json.JSONDecoder().raw_decode(example_text)
    assert example == response
    context, llm = source(raw, context=None), LLMStub(response)
    result = extract(context, llm)
    assert result.raw_formula == raw == context[-1][0].text
    assert result.normalized_formula == canonical and result.variables == names
    assert set(result.variable_meanings) == set(names) and len(llm.calls) == 1
    assert result.semantic_validation == "manual_pending"
    assert llm.calls[0][1]["evidence"][0]["text"] == raw


@pytest.mark.parametrize("expression,reason", [
    (None, "EXPRESSION_REQUIRED"),
    ("G = R - C", "EXPRESSION_EQUATION"),
    ("R - C", "EXPRESSION_UPPERCASE"),
    (r"\frac{s}{q}", "EXPRESSION_LATEX"),
    ("r − c", "EXPRESSION_UNICODE"),
    ("s ÷ q", "EXPRESSION_UNICODE"),
    ("r +", "EXPRESSION_SYNTAX"),
    ("r**2", "EXPRESSION_OPERATOR"),
    ("r//c", "EXPRESSION_OPERATOR"),
    ("r.real", "EXPRESSION_NODE"),
    ("sum(r)", "EXPRESSION_NODE"),
    ("__import__('os')", "EXPRESSION_CHARACTERS"),
    ("1e3", "EXPRESSION_NUMBER"),
    ("_private", "EXPRESSION_SYMBOL"),
])
def test_invalid_model_expression_has_safe_reason_without_repair_or_retry(monkeypatch, expression, reason):
    """模型HTTP成功后的非法表达式仍拒绝，记录有限原因且不泄漏模型正文或追加调用。"""
    events = []

    def record(event, **fields):
        """捕获实际服务日志字段，仍应用生产白名单而不写运行日志。"""
        events.append((event, logs.safe_fields(fields)))

    monkeypatch.setattr(tool, "log_event", record)
    reply = wire()
    reply["normalized_formula"] = expression
    context, llm = source(context=CONTEXT + "PRIVATE_CONTEXT"), LLMStub(reply)
    with pytest.raises(tool.FormulaError, match="FORMULA_EXPRESSION") as error:
        extract(context, llm)
    assert error.value.reason == reason and len(llm.calls) == 1
    event, fields = events[-1]
    assert event == "formula.failed" and fields["stage"] == "output"
    assert fields["error_code"] == "FORMULA_EXPRESSION" and fields["formula_failure_reason"] == reason
    encoded = json.dumps(events)
    assert "PRIVATE" not in encoded and RAW not in encoded and "normalized_formula" not in encoded
    assert "FORMULA_EXPRESSION" in str(error.value) and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("change,reason", [("raw", "RAW_FORMULA_MISMATCH"),
    ("variables", "VARIABLE_SET_MISMATCH"), ("meaning_keys", "VARIABLE_SET_MISMATCH")])
def test_raw_identity_and_variable_mapping_are_not_repaired(change, reason):
    """原式一字符变化或大小写映射不一致仍整体拒绝，不能靠格式兼容绕过身份。"""
    reply = wire("G = R - C", "r-c", name=None, grounded=False)
    if change == "raw":
        reply["raw_formula"] = "G=R-C"
    elif change == "variables":
        reply["variables"] = ["R", "C"]
    else:
        reply["variable_meanings"] = {"R": {"meaning": None, "evidence": []},
                                      "C": {"meaning": None, "evidence": []}}
    llm = LLMStub(reply)
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT") as error:
        extract(source("G = R - C", context=None), llm)
    assert error.value.reason == reason and len(llm.calls) == 1


@pytest.mark.parametrize("raw,expression,canonical", [
    ("G = R - C", "r-c", "(r - c)"),
    (r"U = \frac{S}{Q}", "s/q", "(s / q)"),
])
@pytest.mark.parametrize("invalid_output", [False, True])
def test_f01_f03_http_200_still_requires_strict_output_contract(raw, expression, canonical, invalid_output):
    """真实HTTP封装配合MockTransport：200仅代表收到响应，格式非法仍拒绝且只发一次。"""
    response = wire(raw, expression, name=None, grounded=False)
    if invalid_output:
        response["normalized_formula"] = "R-C" if raw.startswith("G") else r"\frac{S}{Q}"
    requests = []

    def respond(request):
        """在内存返回HTTP200及指定JSON，绝不连接DeepSeek或读取真实密钥。"""
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(response)}}]})

    settings = Settings(_env_file=None, llm_provider="deepseek", deepseek_provider=None,
                        deepseek_api_key="unit-test-key", llm_fallback_enabled=False)
    llm = create_llm_service(settings, transport=httpx.MockTransport(respond))
    context = source(raw, context=None)
    if invalid_output:
        with pytest.raises(tool.FormulaError, match="FORMULA_EXPRESSION") as error:
            extract(context, llm)
        assert error.value.reason in {"EXPRESSION_UPPERCASE", "EXPRESSION_LATEX"}
    else:
        result = extract(context, llm)
        assert result.raw_formula == raw and result.normalized_formula == canonical
    assert len(requests) == 1


@pytest.mark.parametrize("file_type,page,semantics", [("pdf", 3, "physical_pdf"), ("docx", 5, "logical"),
                                                    ("png", 1, "image_page")])
def test_original_page_semantics_and_source_are_not_model_generated(file_type, page, semantics):
    """PDF物理页、Office转换逻辑页、图片像素页保留；模型无权补文件名/页码/bbox。"""
    context = source(file_type=file_type, page=page)
    result = extract(context, LLMStub())
    assert result.source.page_semantics == semantics
    assert result.source.source_spans[0].page == page
    assert (result.source.source_spans[0].bbox is None) == (semantics == "logical")


def test_canonicalization_and_formula_id_are_stable_and_result_is_deep_copied():
    """同来源及仅空白/小数写法差异产生相同ID与规范式，不宣称跨模型语义确定性。"""
    context = source()
    first = extract(context, LLMStub(wire(expression=" profit / revenue * 100.00 ")))
    again = extract(context, LLMStub())
    assert first.formula_id == again.formula_id and first.normalized_formula == again.normalized_formula
    first.source.source_spans[0].bbox[0] = 99
    assert again.source.source_spans[0].bbox == context[-1][0].bbox
    assert first.source.source_parse_task_id == context[3].processing_task_id


def test_semantic_name_can_use_context_without_rewriting_ocr_formula():
    """名称从上下文解释，OCR原式一字不改；不在生产Adapter硬编码某个公式别名。"""
    raw = r"Y_0Y = \frac{Current-Previous}{Previous}\times100\%"
    context = source(raw, context="该公式用于计算同比增长率，比较本期与上年同期。")
    reply = wire(raw, "(current-previous)/previous*100", name="同比增长率", grounded=False)
    reply["formula_name_evidence"] = [{"evidence_id": 2, "quote": "同比增长率"}]
    result = extract(context, LLMStub(reply))
    assert result.formula_name == "同比增长率" and result.raw_formula == raw
    assert context[-1][0].text == raw and result.formula_name_evidence[0].block_id == context[-1][1].block_id


@pytest.mark.parametrize("permission", [False, None, 0, 1, "true"])
def test_permission_denial_happens_before_source_config_or_llm(permission, monkeypatch):
    """未精确允许True时不查来源、不读配置/密钥、不触发LLM或后续模型。"""
    evidence, session, chunk, _, blocks = source()
    llm = LLMStub()

    def forbidden():
        """任何隐式创建LLM服务即失败，不请求外部资源。"""
        pytest.fail("未授权不能读取LLM配置")

    monkeypatch.setattr(tool, "create_llm_service", forbidden)
    with pytest.raises(tool.FormulaError, match="FORMULA_CLOUD_PERMISSION"):
        tool.FormulaService(evidence_service=evidence).extract_formula(chunk.kb_id, chunk.chunk_id,
            blocks[0].block_id, allow_cloud=permission)
    assert session.statements == [] and llm.calls == []


@pytest.mark.parametrize("field", ["kb_id", "chunk_id", "block_id", "doc_id"])
def test_invalid_identity_is_rejected_before_source_or_llm(field):
    """非法身份安全拒绝，不回显输入或把空文档范围扩大到全库。"""
    evidence, session, chunk, document, blocks = source()
    values = dict(kb_id=chunk.kb_id, chunk_id=chunk.chunk_id, block_id=blocks[0].block_id,
                  doc_id=document.doc_id, allow_cloud=True)
    values[field] = "PRIVATE_PATH_OR_TOKEN"
    llm = LLMStub()
    with pytest.raises(tool.FormulaError, match="FORMULA_INPUT") as error:
        tool.FormulaService(evidence_service=evidence, llm_service=llm).extract_formula(**values)
    assert session.statements == [] and llm.calls == [] and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("change", ["foreign_kb", "foreign_doc", "not_ready", "missing_block", "hash", "generation"])
def test_source_errors_stop_before_llm_and_do_not_repair_metadata(change):
    """越界/非READY/损坏来源沿用Evidence严格失败，不能为了公式调用跳过校验。"""
    context = source()
    evidence, _, chunk, document, blocks = context
    kb_id, doc_id, block_id = chunk.kb_id, document.doc_id, blocks[0].block_id
    if change == "foreign_kb":
        kb_id = uuid4()
    elif change == "foreign_doc":
        doc_id = uuid4()
    elif change == "not_ready":
        document.status = "INDEXING"
    elif change == "missing_block":
        blocks.clear()
    elif change == "hash":
        chunk.chunk_metadata["source_spans"][0]["block_text_hash"] = "b" * 64
    else:
        document.processing_task_id = uuid4()
    llm = LLMStub()
    with pytest.raises(EvidenceError):
        tool.FormulaService(evidence_service=evidence, llm_service=llm).extract_formula(kb_id, chunk.chunk_id,
            block_id, doc_id=doc_id, allow_cloud=True)
    assert llm.calls == []


@pytest.mark.parametrize("change", ["partial_start", "partial_end", "not_formula", "not_mineru"])
def test_partial_or_non_mineru_formula_is_not_treated_as_whole_definition(change):
    """合法来源片段仍可能只是长公式切片；只接受完整MinerU公式，不把片段当完整业务式。"""
    context = source(context=None)
    _, _, chunk, _, blocks = context
    span = chunk.chunk_metadata["source_spans"][0]
    if change == "partial_start":
        span["start_char"] = 1
        span["chunk_end_char"] -= 1
        chunk.text = chunk.text[1:]
    elif change == "partial_end":
        span["end_char"] -= 1
        span["chunk_end_char"] -= 1
        chunk.text = chunk.text[:-1]
    elif change == "not_formula":
        span["block_type"] = blocks[0].block_type = "text"
    else:
        span["source"] = blocks[0].source = "native_parser"
    llm = LLMStub()
    with pytest.raises(tool.FormulaError, match="FORMULA_SOURCE"):
        extract(context, llm)
    assert llm.calls == []


def test_complete_context_budget_fails_before_request_and_does_not_truncate():
    """完整上下文超预算明确失败，不裁剪公式/相邻说明或拆成多次收费调用。"""
    llm = LLMStub(context_chars=100)
    with pytest.raises(tool.FormulaError, match="FORMULA_CONTEXT_LIMIT"):
        extract(source(), llm)
    assert llm.calls == []


def test_context_outside_adjacent_blocks_is_not_an_allowed_model_reference():
    """同Chunk也不代表都是邻近上下文；远处解释不会自动发送或成为有效引用。"""
    context = source()
    context[-1][1].block_index = 10
    context[2].chunk_metadata["source_spans"][1]["block_index"] = 10
    llm = LLMStub()
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT"):
        extract(context, llm)
    assert [entry["evidence_id"] for entry in llm.calls[0][1]["evidence"]] == [1]


@pytest.mark.parametrize("provider", ["deepseek", "custom"])
def test_default_factory_uses_only_selected_existing_llm_service(monkeypatch, provider):
    """MockTransport验证默认工厂只请求所选提供方，独立Key/模型/端点不混用，不联网。"""
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider=provider,
        deepseek_api_key="unit-official-key" if provider == "deepseek" else "",
        custom_llm_api_key="unit-custom-key" if provider == "custom" else "",
        custom_llm_api_base_url="https://custom.example/v1", custom_llm_model="unit-compatible-model")
    requests, factories = [], []

    def respond(request):
        """仅拦截httpx内存请求，检查消息/密钥并返回兼容Chat Completions信封。"""
        requests.append(request)
        expected_key = "unit-official-key" if provider == "deepseek" else "unit-custom-key"
        assert request.headers["authorization"] == "Bearer " + expected_key
        assert request.url.host == ("api.deepseek.com" if provider == "deepseek" else "custom.example")
        payload = json.loads(request.content)
        assert payload["model"] == (settings.deepseek_model if provider == "deepseek" else settings.custom_llm_model)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(wire(), ensure_ascii=False)}}]})

    def factory():
        """复用真实统一工厂但仅注入MockTransport，不创建第二套HTTP实现。"""
        factories.append(provider)
        return create_llm_service(settings, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(tool, "create_llm_service", factory)
    evidence, _, chunk, document, blocks = source()
    result = tool.FormulaService(evidence_service=evidence).extract_formula(chunk.kb_id, chunk.chunk_id,
        blocks[0].block_id, doc_id=document.doc_id, allow_cloud=True)
    assert result.status == "success" and len(requests) == 1 and factories == [provider]


@pytest.mark.parametrize("change", ["raw", "source", "unknown_reference", "quote", "meaning",
                                    "missing_variable", "duplicate_variable", "unknown_variable", "status"])
def test_bad_model_definitions_are_rejected_without_partial_result(change):
    """原式修改、伪造来源/变量、无根据含义及坏引用全批拒绝，不修补输出或追加请求。"""
    reply = wire()
    if change == "raw":
        reply["raw_formula"] = "PRIVATE_REWRITTEN_FORMULA"
    elif change == "source":
        reply["source"] = {"file_name": "PRIVATE_PATH", "page": 999}
    elif change == "unknown_reference":
        reply["formula_name_evidence"][0]["evidence_id"] = 999
    elif change == "quote":
        reply["variable_meanings"]["profit"]["evidence"][0]["quote"] = "PRIVATE_INVENTED_MEANING"
    elif change == "meaning":
        reply["variable_meanings"]["profit"]["evidence"] = []
    elif change == "missing_variable":
        reply["variables"].remove("profit")
    elif change == "duplicate_variable":
        reply["variables"].append("profit")
    elif change == "unknown_variable":
        reply["variable_meanings"]["invented"] = {"meaning": None, "evidence": []}
    else:
        reply["status"] = "success_with_guessed_values"
    llm = LLMStub(reply)
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT") as error:
        extract(source(), llm)
    assert len(llm.calls) == 1 and "PRIVATE" not in str(error.value)


def test_ambiguous_quote_cannot_use_first_occurrence_as_fake_source():
    """同一片段内重复引文无法唯一定位时拒绝，不选择第一次来掩盖歧义。"""
    reply = wire()
    llm = LLMStub(reply)
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT"):
        extract(source(context=CONTEXT + CONTEXT), llm)


@pytest.mark.parametrize("response", ['{"status":"success","status":"unsupported_formula"}',
    '{"status":NaN}', '{"status":Infinity}', '```json\n{}\n```', '{}', '[]'])
def test_non_strict_json_is_not_repaired(response):
    """重复键、非有限数、Markdown及缺字段不洗成正常不足或成功。"""
    llm = LLMStub(response)
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT"):
        extract(source(), llm)
    assert len(llm.calls) == 1


@pytest.mark.parametrize("expression", ["__import__('os')", "a.real", "a[0]", "sum(a)", "a**2", "a//b",
    "a%b", "a=1", "True", "1e999", "0xff", "NaN", "a if b else c", "[a for a in b]", "a; b",
    "a" * 33, "1" * 13, "0.123456789", "((a+b)*2)/__private"])
def test_expression_grammar_rejects_executable_or_out_of_scope_nodes(expression):
    """四则白名单拒绝代码/函数/属性/幂及资源超限，不调用Python求值或SymPy。"""
    with pytest.raises(tool.FormulaError, match="FORMULA_EXPRESSION"):
        tool.normalize_expression(expression)


@pytest.mark.parametrize("expression,canonical,names", [
    ("a+b*c", "(a + (b * c))", ["a", "b", "c"]),
    ("(a+b)*c", "((a + b) * c)", ["a", "b", "c"]),
    ("a-b-c", "((a - b) - c)", ["a", "b", "c"]),
    ("a/(b/c)", "(a / (b / c))", ["a", "b", "c"]),
    ("-(a+b)/2.00", "((-(a + b)) / 2)", ["a", "b"]),
    ("85.00/100", "(85 / 100)", []),
])
def test_canonical_ast_keeps_precedence_without_calculating(expression, canonical, names):
    """独立文字断言覆盖优先级、结合顺序、负号和小数规范，不用同一实现生成期望值。"""
    assert tool.normalize_expression(expression) == (canonical, names)


@pytest.mark.parametrize("expression", ["a" * 1025, "-" * 18 + "a", "+".join(["a"] * 65)])
def test_expression_resource_limits_reject_before_any_evaluation(expression):
    """长度、递归深度和节点预算限制有明确失败，不求值或扩大运算范围。"""
    with pytest.raises(tool.FormulaError, match="FORMULA_EXPRESSION"):
        tool.normalize_expression(expression)


@pytest.mark.parametrize("status", ["insufficient_evidence", "unsupported_formula"])
def test_semantic_refusal_preserves_raw_source_and_no_invented_definition(status):
    """真实模型拒答保持明确状态，不因Block非空而强制成功或重试收费。"""
    reply = {"status": status, "formula_name": None, "raw_formula": RAW, "normalized_formula": None,
             "variables": [], "variable_meanings": {}, "formula_name_evidence": []}
    llm = LLMStub(reply)
    result = extract(source(), llm)
    assert result.status == status and result.raw_formula == RAW and result.source.status == "verified"
    assert result.normalized_formula is None and result.variables == [] and result.variable_meanings == {}
    assert len(llm.calls) == 1
    reply["normalized_formula"] = "a+b"
    with pytest.raises(tool.FormulaError, match="FORMULA_OUTPUT"):
        extract(source(), LLMStub(reply))


def test_upstream_failure_does_not_retry_switch_provider_or_mask_as_insufficient(monkeypatch):
    """上游失败保留固定分类，最多调用注入服务一次，禁止换提供方或返回不足掩盖异常。"""
    def forbidden():
        """禁止失败后创建第二个服务或访问其他提供方。"""
        pytest.fail("不得自动切换提供方")

    monkeypatch.setattr(tool, "create_llm_service", forbidden)
    llm = LLMStub(error=LLMError("LLM_NETWORK", provider="custom"))
    with pytest.raises(LLMError, match="LLM_NETWORK"):
        extract(source(), llm)
    assert len(llm.calls) == 1


@pytest.mark.parametrize("cached_level", [logging.WARNING, logging.DEBUG])
def test_logs_keep_process_role_ids_and_safe_results_without_formula_or_context(cached_level):
    """覆盖先前级别缓存后捕获关键事件/脱敏；设置与恢复均清理缓存，不污染后续用例。"""
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("formula-cli"))
    logger = logs.LOGGER
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    token = logs.CONTEXT.set(None)
    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        # 先制造前序用例可能留下的INFO开关缓存，再通过公开API切换，禁止直接赋值level。
        logger.setLevel(cached_level)
        logger.isEnabledFor(logging.INFO)
        logger.setLevel(logging.INFO)
        assert logger.isEnabledFor(logging.INFO)
        context = source(context=CONTEXT + "PRIVATE_CONTEXT")
        result = extract(context, LLMStub())
        with pytest.raises(LLMError):
            extract(context, LLMStub(error=LLMError("LLM_AUTH", provider="deepseek")))
        content = output.getvalue()
        assert all(event in content for event in ("formula.started", "formula.synthesis_started",
                                                 "formula.finished", "formula.failed"))
        assert "Formula CLI" in content and str(result.formula_block_id) in content
        assert "PRIVATE" not in content and RAW not in content and result.normalized_formula not in content
        assert CONTEXT not in content and "获利水平" not in content
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
