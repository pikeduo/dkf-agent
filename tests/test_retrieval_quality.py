"""跨语言诊断工具Mock契约；合成内存词项真值不代表真实BGE/RAG质量评测。"""

import copy
from uuid import UUID

import pytest

pytest.importorskip("llama_index.core.schema")

from app.schemas.retrieval import RetrievalResult
from app.services.rag.bm25 import LocalBM25Index, tokenize
from app.services.rag.llama_index.node_adapter import result_to_node
from scripts import retrieval_quality as tool

KB, DOC = str(UUID(int=100)), str(UUID(int=200))


def hit(number, text, score=1.0, *, kb=KB, doc=DOC, page=1, oversized=False):
    """只构造有来源页的内存Node，不创建任何业务文档、记录或向量。"""
    return result_to_node(RetrievalResult(kb_id=kb, doc_id=doc, chunk_id=UUID(int=number),
        file_name="synthetic-unit-fixture.txt", file_type="txt", text=text,
        page_start=page, page_end=page, section="unit-fixture", metadata={
            "page_semantics": "logical", "oversized": oversized, "estimated_token_count": 3600 if oversized else 20,
            "source_spans": [{"block_id": str(UUID(int=number + 1000)), "page": page,
                              "start_char": 0, "end_char": len(text)}]}, retrieval_score=score), log_success=False)


def case(question, variants, *, number=1, language="zh", document_language="en", no_answer=False):
    """建立明确标为合成的测试真值，不引用/修改官方QA或真实文档内容。"""
    return {"id": "synthetic-unit", "kb_id": KB, "doc_ids": [DOC], "question": question,
        "variants": variants, "query_language": language, "document_language": document_language,
        "no_answer": no_answer, "targets": [] if no_answer else [{"chunk_id": str(UUID(int=number)),
            "doc_id": DOC, "pages": [1], "answer": "synthetic-ground-truth"}]}


def lexical(nodes):
    """使用真实本地BM25算法；没有Dense/GPU，重排为保序Stub，不能冒充真实模型对比。"""
    nodes = sorted(nodes, key=lambda node: UUID(node.node.node_id).int)
    index = LocalBM25Index([(UUID(node.node.node_id), node.node.text) for node in nodes])

    def retrieve(query):
        """只评分现有内存词项，模拟另外分支为空，不连接SQL。"""
        scored = []
        for position, score in index.search(query, 20):
            cloned = nodes[position].model_copy(deep=True)
            cloned.score = float(score)
            scored.append(cloned)
        return [], scored, scored

    def rerank(nodes, query):
        """保序Stub只验证工具路径；没有伪造BGE logit或语义提升结果。"""
        return copy.deepcopy(nodes)

    return retrieve, rerank


@pytest.mark.parametrize("question,variant,language,doc_language,text", [
    ("设备库存总量", [], "zh", "zh", "设备库存总量为147台。"),
    ("warehouse inventory total", [], "en", "en", "warehouse inventory total: 147 units"),
    ("设备库存总量", ["warehouse inventory total"], "zh", "en", "warehouse inventory total: 147 units"),
    ("warehouse inventory total", ["设备库存总量"], "en", "zh", "设备库存总量为147台。"),
    ("分支机构收入", ["branch revenue"], "zh", "en", "<table><tr><td>branch revenue</td><td>881</td></tr></table>"),
    ("期间利润率", ["period profit margin"], "zh", "en", "period profit margin | current 12% | previous 11%"),
])
def test_fixed_language_directions_table_numbers_and_similar_statistics(question, variant, language, doc_language, text):
    """四种语言方向/表格/近似数字有稳定Chunk页真值；只量化词项可达性，不声称真实质量。"""
    nodes = [hit(1, text, oversized="<table>" in text),
        hit(2, "Report date 2025; employee count 148; incidental figure 8810")]
    incoming = [node.model_dump() for node in nodes]
    sample = case(question, variant, language=language, document_language=doc_language)
    retrieve, rerank = lexical(nodes)
    result = tool.evaluate_case(sample, retrieve, rerank)
    baseline = result["strategies"]["baseline"]["metrics"]
    expanded = result["strategies"]["parallel_recall_original_rerank"]["metrics"]
    assert baseline["hit_at_1"] == (0 if variant else 1)
    assert expanded["recall_at_5"] == expanded["hit_at_1"] == expanded["mrr"] == 1.0
    assert result["branches"][0]["bm25_count"] == (0 if variant else 1)
    assert [node.model_dump() for node in nodes] == incoming


def test_cjk_and_english_have_no_implicit_translation_or_early_truncation():
    """中文字符/二元词项不翻译成英文，扩展必须有显式来源，不能把字面空命中当BM25缺陷。"""
    assert set(tokenize("仓库库存数量")).isdisjoint(tokenize("warehouse inventory quantity"))
    nodes = [hit(number, f"inventory warehouse item-{number}") for number in range(1, 31)]
    index = LocalBM25Index([(UUID(node.node.node_id), node.node.text) for node in nodes])
    assert len(index.search("warehouse", 20)) == 20
    assert len(index.search("warehouse", 30)) == 30


def test_no_matching_terms_and_no_answer_are_not_faked_as_success():
    """零重叠保持空；无答案不能仅凭候选存在宣称拒答正确，也不进入命中率分母。"""
    sample = case("unknownunmatchedterm", [], no_answer=True)
    result = tool.evaluate_case(sample, *lexical([hit(1, "warehouse inventory")]))
    assert result["strategies"]["baseline"]["metrics"]["status"] == "not_applicable"
    assert result["strategies"]["baseline"]["rows"] == []
    assert tool.summarize([result])["all"]["baseline"]["count"] == 0


@pytest.mark.parametrize("change", ["kb", "doc"])
def test_scope_violation_is_rejected_not_removed(change):
    """同库指定文档过滤和知识库隔离失败必须全批拒绝，不能靠删除越界项伪造通过。"""
    kwargs = {change: str(UUID(int=999))}
    with pytest.raises(tool.QualityError, match="QUALITY_SCOPE"):
        tool.scoped([hit(1, "fixture", **kwargs)], case("question", []))


def test_parallel_rrf_is_stable_deduplicated_and_does_not_forge_scores():
    """同Chunk每表达只贡献一次；实验RRF单独存储，不覆写已有Fusion/Rerank来源。"""
    first, second = hit(1, "first"), hit(2, "second")
    before = first.model_dump(), second.model_dump()
    nodes, votes = tool.reciprocal_merge([[first, second], [second, first]])
    assert [node.node.node_id for node in nodes] == [str(UUID(int=1)), str(UUID(int=2))]
    assert len(votes) == 2 and votes[str(UUID(int=1))]["query_ranks"] == {"0": 1, "1": 2}
    assert (first.model_dump(), second.model_dump()) == before
    with pytest.raises(tool.QualityError, match="QUALITY_DUPLICATE"):
        tool.reciprocal_merge([[first, first]])
    with pytest.raises(tool.QualityError, match="QUALITY_SOURCE_CONFLICT"):
        tool.reciprocal_merge([[first], [hit(1, "changed")]])


def test_same_candidate_rerank_comparison_and_ties_are_observable():
    """比较原/双语Query在同一集合中的效果，长表格不截断，同logit计数而不猜因果。"""
    nodes = [hit(1, "complete table formula =A1+B1", oversized=True), hit(2, "date only")]
    calls = []

    def retrieve(query):
        """仅返回同一候选池及分支，隔离真实检索数值。"""
        return nodes, [], nodes

    def rerank(items, query):
        """记录完整输入，提供同分保序Stub，不把FP16当作已定位根因。"""
        calls.append((query, tuple(node.node.node_id for node in items), tuple(node.node.text for node in items)))
        return copy.deepcopy(items)

    result = tool.evaluate_case(case("原问题", ["parallel expression"]), retrieve, rerank)
    assert {entry[1] for entry in calls} == {(str(UUID(int=1)), str(UUID(int=2)))}
    assert all(entry[2][0] == nodes[0].node.text for entry in calls)
    assert len(result["same_baseline_candidates"]) == 2
    rows = result["strategies"]["baseline"]["rows"]
    assert rows[0]["oversized"] is True and rows[0]["estimated_tokens"] == 3600
    assert all(row["equal_score_count"] == 2 for row in rows)
    assert "complete table" not in str(result) and "原问题" not in str(result)


def test_ground_truth_page_is_checked_against_spans_not_broad_page_range():
    """Chunk跨页范围不能冒充每一页都有证据，真值页必须在source_spans中。"""
    sample = case("fixture", [])
    sample["targets"][0]["pages"] = [2]
    with pytest.raises(tool.QualityError, match="QUALITY_SOURCE_PAGE"):
        tool.metrics([hit(1, "only page one")], sample)


def test_default_dry_run_never_builds_backends_and_reports_do_not_overwrite(tmp_path, monkeypatch, capsys):
    """预览只校验真值，已有文件独占保护，不自动加载模型/数据库或改正式策略。"""
    path = tmp_path / "suite.json"
    tool.publish_json(path, {"version": 1, "cases": [case("question", [])]})

    def forbidden(*args):
        """任何预览期间的模型/读库调用都让用例失败。"""
        raise AssertionError("unexpected backend")

    monkeypatch.setattr(tool, "real_backends", forbidden)
    assert tool.main(["--suite", str(path)]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        tool.publish_json(path, {})


def test_truth_uses_existing_evidence_boundary_and_does_not_compare_answer_strings():
    """先核对真实当前来源/Hash/页，答案语义仍为人工真值，不按包含关系猜正确。"""
    from types import SimpleNamespace
    calls = []

    class ResolverStub:
        """只读来源Stub，不加载模型或通过答案查找匹配Chunk。"""

        def resolve(self, kb_id, chunk_id, *, doc_id):
            """返回来源快照；正文不含答案也不能被工具当作质量失败/通过的语义判断。"""
            calls.append((kb_id, chunk_id, doc_id))
            return SimpleNamespace(file_hash="a" * 64, source_parse_task_id=UUID(int=7),
                source_chunk_task_id=UUID(int=8), page_semantics="logical_native",
                source_spans=[SimpleNamespace(page=1)])

    sample = case("question", [])
    result = tool.verify_truth(sample, ResolverStub())
    assert len(calls) == 1 and result["status"] == "source_verified_not_answer_semantics"
    sample["targets"][0]["file_hash"] = "b" * 64
    with pytest.raises(tool.QualityError, match="QUALITY_SOURCE_HASH"):
        tool.verify_truth(sample, ResolverStub())


def test_failure_report_keeps_known_codes_without_private_exception_text():
    """配置/模型/来源阻塞能追到固定类别，任意上游错误原文不进入报告。"""
    from app.services.reranking import RerankError
    assert tool.failure_code(RerankError("RERANK_TOO_LONG")) == "RERANK_TOO_LONG"
    assert tool.failure_code(RuntimeError("PRIVATE_QUERY PRIVATE_PATH PRIVATE_TOKEN")) == "QUALITY_BACKEND_OR_SOURCE"
