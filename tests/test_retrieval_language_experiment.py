"""独立实验的纯内存契约测试；不运行真实文档、数据库、GPU或付费API。"""

import copy
from uuid import UUID

import pytest

pytest.importorskip("llama_index.core.schema")

from scripts import retrieval_language_experiment as tool
from tests.test_retrieval_quality import case, hit

ZH = "请根据仓库记录说明库存设备总量是多少"
EN = "What is the total warehouse equipment inventory?"
BODY_EN = "Warehouse equipment inventory record and summary for the current reporting period."
BODY_ZH = "这是仓库设备的库存统计记录，包含不同区域的设备数量和汇总情况。"


@pytest.mark.parametrize("text,expected", [(BODY_EN, "en"), (BODY_ZH, "zh"),
    ("<table><td>12345</td></table>", "unknown"),
    ("English inventory status 库存统计资料字段", "mixed"),
    ("<table><tr><td>" + BODY_ZH + "</td></tr></table>", "zh")])
def test_language_uses_content_not_html_digits_or_suite_labels(text, expected):
    """候选语言只取正文字符，HTML与数字不能伪造英文语言置信。"""
    assert tool.text_language(text)["language"] == expected


def rankings():
    """六个同池内存候选，原问目标第六、英文表达第一；不使用任何真实案例UUID或数字。"""
    nodes = [hit(index, BODY_EN + f" item{index}", 10.0 - index) for index in range(1, 7)]
    return nodes, [nodes[5], nodes[0], nodes[1], nodes[2], nodes[3], nodes[4]]


def test_language_choice_and_equal_rrf_dilution_are_separate_from_truth():
    """同池RRF会稀释单表达第一的目标，语言策略不需要答案/目标/文档语言真值。"""
    first, second = rankings()
    before = [node.model_dump() for node in first]
    merged, _ = tool.base.reciprocal_merge([first, second])
    assert [node.node.node_id for node in merged].index(str(UUID(int=6))) == 2
    orders, chosen = tool.policy_orders([ZH, EN], [first, second])
    assert orders["language_preferred"][0].node.node_id == str(UUID(int=6))
    assert set(chosen.values()) == {1}
    assert [node.model_dump() for node in first] == before
    assert all(node.node.metadata["chunk_metadata"]["source_spans"] for node in orders["language_preferred"])


@pytest.mark.parametrize("queries,text", [([EN, ZH], BODY_EN), ([EN, ZH], BODY_ZH),
    ([ZH, EN], BODY_ZH), ([ZH, EN], "12345"), ([ZH, EN, "Another English expression about inventory"], BODY_EN)])
def test_original_english_mixed_unknown_and_ambiguous_cases_keep_original(queries, text):
    """英文原问、不确定候选和多个英文表达不自动换路，不能依测试真值选择最好答案。"""
    assert tool.preferred_query(queries, hit(1, text)) == 0


def test_mixed_candidate_languages_are_decided_per_node_without_raw_logit_comparison():
    """中文/英文候选可共存，按候选正文保守选择排名，不跨查询直接比较不可比logit。"""
    nodes = [hit(1, BODY_EN), hit(2, BODY_ZH), hit(3, "123")]
    _, chosen = tool.policy_orders([ZH, EN], [nodes, list(reversed(nodes))])
    assert chosen == {str(UUID(int=1)): 1, str(UUID(int=2)): 0, str(UUID(int=3)): 0}


def test_lexicographic_keeps_original_signal_only_in_exact_preferred_score_ties():
    """首选表达确切同分时取原问排名；不设近似阈值或破坏原始Node/评分来源。"""
    first, second = rankings()
    for node in second:
        node.score = 1.0
    orders, _ = tool.policy_orders([ZH, EN], [first, second])
    assert [h.node.node_id for h in orders["language_lexicographic"]] == [h.node.node_id for h in first]


@pytest.mark.parametrize("change", ["missing", "duplicate", "text"])
def test_inconsistent_pools_and_sources_fail_instead_of_dropping_evidence(change):
    """候选集合、稳定ID或来源冲突全批拒绝，不过滤异常项掩盖问题。"""
    first, second = copy.deepcopy(rankings())
    if change == "missing":
        second.pop()
    elif change == "duplicate":
        second.append(second[0])
    else:
        second[0] = hit(6, "changed evidence content")
    with pytest.raises(tool.base.QualityError):
        tool.policy_orders([ZH, EN], [first, second])


def test_measurement_cache_is_per_service_identity_and_exact_order_not_node_cache():
    """只复用完全相同输入logit；不同顺序/模型身份必须重新评分，报告不含私有文本。"""
    class FakeService:
        """正式服务的轻量内存Stub，不加载Tokenizer文件或模型。"""

        def __init__(self):
            """初始化可变身份和评分计数。"""
            self.version, self.calls = 1, 0
            self._backend = (self.tokens, None)

        def identity(self):
            """返回模拟版本以验证缓存身份边界。"""
            return {"version": self.version}

        def score(self, query, passages):
            """固定评分仅验证调用次数，不代表任何语义效果。"""
            self.calls += 1
            return [float(len(text)) for text in passages]

        def tokens(self, pairs, **kwargs):
            """仅返回内存字符长度对应token，不解析文档。"""
            return {"input_ids": [[1] * (len(query) + len(text)) for query, text in pairs]}

    service = FakeService()
    measured = tool.MeasuredService(service)
    expected = measured.score("PRIVATE_QUERY", ["PRIVATE_BODY", "OTHER"])
    assert measured.score("PRIVATE_QUERY", ["PRIVATE_BODY", "OTHER"]) == expected
    assert service.calls == 1
    measured.score("PRIVATE_QUERY", ["OTHER", "PRIVATE_BODY"])
    service.version = 2
    measured.score("PRIVATE_QUERY", ["PRIVATE_BODY", "OTHER"])
    assert service.calls == 3
    assert measured.events[1]["model_reused"] is True
    assert measured.events[1]["measured_model_ms"] == 0
    assert "PRIVATE" not in str(measured.events)


def test_evaluation_truth_labels_never_change_strategy_and_scope_is_preserved():
    """改变评测标签/目标只能影响指标，不能改变排序；仍逐次执行原kb/doc范围防御。"""
    first, second = rankings()

    def retrieve(query):
        """同池召回Stub，记录中不带答案特征。"""
        return first, [], first

    def rerank(nodes, query):
        """按测试表达返回固定排列，保留Node证据。"""
        return copy.deepcopy(first if query == ZH else second)

    original = case(ZH, [EN], number=6)
    result, _ = tool.evaluate(original, retrieve, rerank)
    changed = copy.deepcopy(original)
    changed.update(query_language="en", document_language="zh")
    changed["targets"][0].update(chunk_id=str(UUID(int=1)), answer="UNUSED_ANSWER")
    compared, _ = tool.evaluate(changed, retrieve, rerank)
    assert result["strategies"]["language_preferred"]["rows"] == compared["strategies"]["language_preferred"]["rows"]
    original["kb_id"] = str(UUID(int=999))
    with pytest.raises(tool.base.QualityError, match="QUALITY_SCOPE"):
        tool.evaluate(original, retrieve, rerank)


def test_metrics_percentiles_negative_cases_and_suite_audit():
    """无答案不进入指标分母；分位数和重复目标审计不能被当作独立样本证据。"""
    first, _ = rankings()

    def retrieve(query):
        """内存固定候选，不查库。"""
        return first, [], first

    def rerank(nodes, query):
        """原序Stub，隔离指标计算。"""
        return copy.deepcopy(nodes)

    positive = case(ZH, [], number=1)
    negative = case(EN, [], no_answer=True)
    measured, _ = tool.evaluate(positive, retrieve, rerank)
    no_answer, _ = tool.evaluate(negative, retrieve, rerank)
    assert tool.aggregate([measured, no_answer])["all"]["baseline"]["count"] == 1
    assert tool.latency_summary([10, 20, 30])["p50_ms"] == 20
    assert tool.percentile([], 0.95) is None
    audit = tool.suite_audit({"cases": [positive, positive]})
    assert audit["independent_target_documents"] == audit["unique_target_chunks"] == 1


def test_default_preview_never_uses_backends_or_overwrites(tmp_path, monkeypatch, capsys):
    """默认仅校验本地套件；缺显存确认或输出已存在均在真实运行前拒绝。"""
    path = tmp_path / "suite.json"
    tool.base.publish_json(path, {"version": 1, "cases": [case(ZH, [EN])]})

    def forbidden(*args, **kwargs):
        """意外模型/数据库调用立即让测试失败。"""
        raise AssertionError("unexpected backend")

    monkeypatch.setattr(tool, "run", forbidden)
    assert tool.main(["--suite", str(path)]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert tool.main(["--suite", str(path), "--execute"]) == 2
    output = tmp_path / "old.json"
    output.write_text("KEEP", encoding="utf-8")
    assert tool.main(["--suite", str(path), "--output", str(output), "--execute", "--gpu-idle-confirmed"]) == 2
    assert output.read_text(encoding="utf-8") == "KEEP"


@pytest.mark.parametrize("variants", [[], [EN], [EN, "Which equipment inventory figures are recorded?"]])
def test_measurements_match_original_call_order_and_do_not_hide_cached_gpu_cost(variants):
    """单/双/三表达调用与token按身份绑定；策略成本回填完整推理，不以缓存墙钟虚报加速。"""
    nodes = [hit(1, BODY_EN), hit(2, BODY_ZH)]

    def retrieve(query):
        """同池召回且不改变文档范围。"""
        return nodes, [], nodes

    def rerank(items, query):
        """保持输入顺序，让测试聚焦成本观察而非模型效果。"""
        return copy.deepcopy(items)

    sample = case(ZH, variants)
    result, calls = tool.evaluate(sample, retrieve, rerank)
    events = [{"model_reused": index > 0, "pair_count": 2,
        "measured_model_ms": 0.0, "standalone_model_ms": 500.0, "token_audit_ms": 0.0,
        "token_counts": [20, 30], "token_hashes": ["a" * 64, "b" * 64]} for index, _ in enumerate(calls)]
    tool.attach_measurements(result, calls, events, tool.base.unique_queries(sample))
    assert len(result["token_observations"]) == 1 + len(variants)
    assert result["compute"]["actual_model_calls"] == 1
    assert result["standalone_cost_estimate_ms"]["baseline"] >= 500
    if variants:
        assert result["standalone_cost_estimate_ms"]["parallel_recall_parallel_rerank"] >= 500 * (1 + len(variants))
    assert all(row["rows"][0]["chunk_id"] == nodes[0].node.node_id for row in result["token_observations"])
