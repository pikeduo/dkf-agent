"""阶段35纯内存观测/只读SQL用例；仅编写，由用户执行，不解析或生成真实文档。"""

import copy
from contextlib import nullcontext
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.models import Document, DocumentBlock, KnowledgeBase
from app.schemas.document_analysis import BlockObservation, DocumentAnalysisRequest, VisualPageAssessment
from app.services import document_analysis as analysis
from app.services import document_assessment as storage
from app.services.specialty_errors import SpecialtyError
from test_evidence import bound_filter_value


def snapshot(*, file_type="pdf", quality=None):
    """构造已有记录身份摘要，不创建文件、业务记录或验收结论。"""
    return analysis.DocumentSnapshot(kb_id=uuid4(), doc_id=uuid4(), file_name="原件." + file_type,
        file_type=file_type, file_hash="a" * 64, processing_task_id=uuid4(), status="PARTIAL_READY",
        quality=copy.deepcopy(quality))


def block(index, text, *, kind="text", page=1, **updates):
    """构造前缀与整块长度观测，长表格只设置字符数，不物化巨型正文。"""
    values = dict(block_id=uuid4(), block_index=index, page=page, block_type=kind,
        preview=text[:512], text_chars=len(text), section=None, source="mineru_cloud", confidence=None)
    return BlockObservation(**(values | updates))


def complete_quality(**updates):
    """构造覆盖摘要；COMPLETE只表示现有结构覆盖状态，不证明视觉或语义准确。"""
    return dict(content="COMPLETE", structure="BASE", covered_pages=[1], required_pages=[1],
        missing_pages=[], visual_review_pages=[], quarantined_count=0) | updates


def test_outline_recovers_parent_and_keeps_gap_without_inventing_missing_title():
    """编号1/1.1可连接，3.2不能错误连到第1章；所有标题保留原字符位置。"""
    doc = snapshot()
    observations = [block(1, " 1. 总论 ", kind="title"), block(2, "1.1 范围"),
        block(3, "3.2 缺失父章的标题", kind="title"), block(4, "# 附录"), block(5, "1.25%")]
    before = copy.deepcopy(observations)
    report = analysis.analyze_observations(doc, observations)
    first, second, orphan, appendix = report.outline.nodes
    assert second.parent_id == first.node_id and second.level == 2
    assert orphan.parent_id is None and orphan.level == 1 and orphan.declared_level == 2
    assert "MISSING_NUMBERED_PARENT" in orphan.issues and "LEVEL_GAP" in orphan.issues
    assert appendix.basis == "markdown" and len(report.outline.nodes) == 4
    for node in report.outline.nodes:
        original = next(item.preview for item in observations if item.block_id == node.source.block_id)
        assert original[node.source.start_char:node.source.end_char] == node.source.quote == node.title
    assert observations == before and doc.status == "PARTIAL_READY"
    assert analysis.analyze_observations(doc, observations) == report


def test_toc_printed_page_is_only_hint_and_duplicate_lines_keep_distinct_offsets():
    """重复目录行不共享错误偏移，目录页99不能变为正文来源页或虚构物理页数。"""
    lines = " 1 总论 ...... 99\n 1 总论 ...... 99\n2 未出现 ...... 100"
    observations = [block(1, lines, page=1), block(2, "1 总论", kind="title", page=7)]
    report = analysis.analyze_observations(snapshot(), observations)
    first, again, absent = report.outline.toc_entries
    assert first.match_status == again.match_status == "matched" and absent.match_status == "unmatched"
    assert first.matched_node_id == report.outline.nodes[0].node_id
    assert first.source.start_char != again.source.start_char
    assert first.printed_page_hint == "99" and report.outline.nodes[0].source.page == 7
    assert report.source_page_count is None and report.observed_pages == [1, 7]


@pytest.mark.parametrize("value", ["1.25%", "3.14", "2026年财务报告", "1. 这是完整句子。"])
def test_numbered_values_and_sentences_are_not_silently_promoted_to_headings(value):
    """数字、比例、年份和普通句子不能因有数字就成为已确认标题。"""
    assert not analysis.analyze_observations(snapshot(), [block(1, value)]).outline.nodes


def test_chinese_markdown_and_unnumbered_titles_expose_inference_limits():
    """中文章节/节可连接，无编号标题不凭空借用未保存字体确定层级。"""
    report = analysis.analyze_observations(snapshot(file_type="docx"), [block(1, "第一章 总论"),
        block(2, "第一节 适用范围"), block(3, "无编号标题", kind="title")])
    assert report.outline.nodes[1].parent_id == report.outline.nodes[0].node_id
    assert "UNNUMBERED_LEVEL_UNCERTAIN" in report.outline.nodes[2].issues
    assert "FONT_INFORMATION_NOT_STORED" in report.outline.limitations
    assert report.page_semantics == "logical" and report.source_page_count is None


def test_unknown_block_order_never_presents_a_verified_parent_hierarchy():
    """历史空序号只保留候选，不用UUID顺序猜标题父子或阅读顺序。"""
    report = analysis.analyze_observations(snapshot(), [block(None, "1 总论"), block(None, "1.1 范围")])
    assert all(node.parent_id is None and "ORDER_UNKNOWN" in node.issues for node in report.outline.nodes)
    assert "BLOCK_ORDER_UNKNOWN" in {item.code for item in report.quality.findings}


def test_visual_without_text_remains_review_and_does_not_assert_missing_business_information():
    """视觉待审不等同于解析失败，也不能升级/降级原Document业务状态。"""
    doc = snapshot(quality=complete_quality(content="PARTIAL", visual_review_pages=[7, 47]))
    report = analysis.analyze_observations(doc, [block(1, "可靠正文")])
    codes = {item.code for item in report.quality.findings}
    assert report.quality.status == "review" and "VISUAL_REVIEW_REQUIRED" in codes
    assert "MISSING_REQUIRED_PAGES" not in codes and report.document_status == "PARTIAL_READY"
    assert doc.quality["content"] == "PARTIAL" and "VISUAL_BUSINESS_INFORMATION" in report.quality.not_assessed


def test_missing_coverage_is_derived_without_trusting_empty_missing_pages_marker():
    """即使旧摘要missing为空，required减covered的缺口仍需要明确展示。"""
    report = analysis.analyze_observations(snapshot(quality=complete_quality(
        required_pages=[1, 2, 59, 61], covered_pages=[1])), [block(1, "正文")])
    missing = next(item for item in report.quality.findings if item.code == "MISSING_REQUIRED_PAGES")
    assert missing.pages == [2, 59, 61] and report.quality.status == "attention"


def test_absent_quality_is_unknown_and_unobserved_layout_does_not_get_a_quality_pass():
    """Native/历史空质量保持未知，复杂度低分仅适用于已观测部分。"""
    report = analysis.analyze_observations(snapshot(file_type="xlsx"), [block(1, "原表", kind="table")])
    assert report.quality.status == "unknown" and report.quality.parse_content is None
    assert "LAYOUT" in report.complexity.unknown_dimensions
    assert "FULL_DOCUMENT_COVERAGE" in report.quality.not_assessed and report.source_page_count is None


def test_complexity_counts_full_lengths_without_materializing_giant_table_text():
    """整块字符统计与前缀物化分离，规则分值可解释，不把Token或未知总页数伪造出来。"""
    observations = [block(index, "表格前缀", kind="table", page=index, text_chars=100000)
                    for index in range(1, 51)]
    report = analysis.analyze_observations(snapshot(), iter(observations))
    factors = {item.code: item for item in report.complexity.factors}
    assert report.text_chars == 5000000 and report.block_types["table"] == 50
    assert factors["TEXT_VOLUME"].observed == 5000000 and factors["OBSERVED_PAGES"].observed == 50
    assert report.complexity.score == sum(item.points for item in report.complexity.factors)
    assert "SOURCE_TOTAL_PAGES" in report.complexity.unknown_dimensions


def test_observation_budget_fails_whole_report_without_silent_truncation(monkeypatch):
    """超预算不把前两块发布成整份报告，更不改变原文档状态。"""
    monkeypatch.setattr(analysis, "MAX_BLOCKS", 2)
    doc = snapshot()
    with pytest.raises(SpecialtyError) as caught:
        analysis.analyze_observations(doc, (block(index, "正文") for index in range(1, 4)))
    assert caught.value.code == "ANALYSIS_LIMIT" and doc.status == "PARTIAL_READY"


@pytest.mark.parametrize("quality", [complete_quality(required_pages=[True]), complete_quality(missing_pages="2"),
    complete_quality(content="UNKNOWN"), complete_quality(quarantined_count=-1)])
def test_invalid_quality_metadata_is_not_silently_treated_as_complete(quality):
    """坏质量元数据明确失败，不能清空缺口或变成正常高质量文档。"""
    with pytest.raises(SpecialtyError) as caught:
        analysis.analyze_observations(snapshot(quality=quality), [block(1, "正文")])
    assert caught.value.code == "ANALYSIS_METADATA"


@pytest.mark.parametrize("updates", [{"kb_id": UUID(int=0)}, {"visual_pages": [True]},
    {"visual_pages": [1, 1]}, {"visual_pages": [1, 2, 3, 4, 5, 6]}, {"visual_pages": [0]}])
def test_analysis_request_rejects_ambiguous_scope_or_page_selection(updates):
    """所有身份/页面在查询前验证，不能用空UUID或重复页隐式扩范围。"""
    with pytest.raises(ValidationError):
        DocumentAnalysisRequest(**({"kb_id": uuid4(), "doc_id": uuid4()} | updates))


class ResultStub:
    """独立记录每个结果集的关闭，不复用一个对象掩盖游标泄漏。"""

    def __init__(self, rows):
        """保存仅含字典的观测数据。"""
        self.rows, self.closed = rows, False

    def mappings(self):
        """返回可迭代映射，不创建ORM对象。"""
        return self

    def one_or_none(self):
        """文档投影至多一行，缺失保持None。"""
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def __iter__(self):
        """按原顺序流式暴露内存观测。"""
        return iter(self.rows)

    def close(self):
        """记录资源释放，未产生数据库副作用。"""
        self.closed = True


class SessionStub:
    """只允许只读设置和指定范围SELECT，任何意外写入都因没有对应接口而失败。"""

    def __init__(self, doc, observations):
        """提供独立会话状态及真实字段名，不依赖自动生成的SQL参数编号。"""
        self.doc, self.observations = doc, observations
        self.new, self.dirty, self.deleted = (), (), ()
        self.active, self.closed, self.failure = False, False, None
        self.no_autoflush, self.statements, self.results = nullcontext(), [], []
        self.options = None

    def __enter__(self):
        """不打开真实连接。"""
        return self

    def __exit__(self, *args):
        """只标记关闭，禁止提交或flush。"""
        self.closed = True

    def in_transaction(self):
        """模拟需拒绝的已有事务。"""
        return self.active

    def connection(self, *, execution_options):
        """记录隔离设置。"""
        self.options = execution_options

    def execute(self, statement):
        """检查真正SQL字段范围，再返回轻量字典；不猜测WHERE自动参数名。"""
        self.statements.append(statement)
        if not statement.is_select:
            return ResultStub([])
        if self.failure:
            raise self.failure
        if "quality_present" in statement.selected_columns.keys():
            assert bound_filter_value(statement, Document.kb_id) == self.doc.kb_id
            assert bound_filter_value(statement, KnowledgeBase.status) == "ACTIVE"
            selected_id = bound_filter_value(statement, Document.doc_id, required=False)
            assert selected_id is None or selected_id == self.doc.doc_id
            row = {name: getattr(self.doc, name) for name in ("kb_id", "doc_id", "file_name", "file_type",
                "file_hash", "processing_task_id", "status")}
            row["quality_present"] = self.doc.quality is not None
            row.update({"quality_" + name: (self.doc.quality or {}).get(name) for name in storage.QUALITY_FIELDS})
            if "file_path" in statement.selected_columns.keys():
                row["file_path"] = self.doc.file_path
            result = ResultStub([row])
        else:
            assert bound_filter_value(statement, DocumentBlock.doc_id) == self.doc.doc_id
            result = ResultStub([item.model_dump() for item in self.observations])
        self.results.append(result)
        return result


def test_sql_projection_is_scoped_bounded_and_excludes_full_corpus_payloads():
    """只读完整文档统计，但SQL只返回前缀/标量，不取向量、整表或视觉附件JSON。"""
    kb_id, doc_id = uuid4(), uuid4()
    statement = storage.document_statement(kb_id, doc_id)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "documents.kb_id =" in sql and "documents.doc_id =" in sql and "knowledge_bases.status =" in sql
    assert ["READY", "PARTIAL_READY"] in statement.compile().params.values()
    assert "file_path" not in statement.selected_columns.keys() and "visual_assets" not in str(statement.compile().params)
    statement = storage.block_statement(doc_id)
    assert "text" not in statement.selected_columns.keys() and "preview" in statement.selected_columns.keys()
    assert statement.get_execution_options()["yield_per"] == 128
    assert statement._limit_clause.value == analysis.MAX_BLOCKS + 1


def test_assessment_closes_readonly_snapshot_and_never_calls_visual_reader_by_default():
    """默认评估不读取原件，全部结果集关闭且没有commit/flush入口。"""
    doc, observations = snapshot(), [block(1, "1 总论", kind="title")]
    session = SessionStub(doc, observations)

    def no_visual(*args):
        """默认请求触达原件即违反约定。"""
        pytest.fail("不应检查原件")

    service = storage.DocumentAssessmentService(session_factory=lambda: session, visual_reader=no_visual)
    assert not session.statements
    report = service.analyze(DocumentAnalysisRequest(kb_id=doc.kb_id, doc_id=doc.doc_id))
    assert report.doc_id == doc.doc_id and session.closed and all(result.closed for result in session.results)
    assert session.options == {"isolation_level": "REPEATABLE READ"}
    assert [str(item) for item in session.statements[:3]] == ["SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '10s'", "SET LOCAL lock_timeout = '2s'"]


def test_explicit_visual_selection_happens_after_snapshot_closes_and_preserves_unknown_results():
    """明确选页才检查，未知模糊/倾斜不会被当作已经通过。"""
    doc = replace(snapshot(), file_path="PRIVATE_SOURCE")
    session, calls = SessionStub(doc, [block(1, "正文")]), []

    def visual_reader(current, pages):
        """仅返回像素统计Stub，不读取或渲染任何真实文件。"""
        assert session.closed and current.file_path == "PRIVATE_SOURCE"
        calls.append(pages)
        return 8, [VisualPageAssessment(page=2, width=10, height=10, contrast=0, edge_energy=0,
            dark_pixel_ratio=0, blur="unknown", skew_degrees=None, skew="unknown")]

    report = storage.DocumentAssessmentService(session_factory=lambda: session, visual_reader=visual_reader).analyze(
        DocumentAnalysisRequest(kb_id=doc.kb_id, doc_id=doc.doc_id, visual_pages=[2]))
    assert calls == [[2]] and report.source_page_count == 8
    assert "SELECTED_PAGE_BLUR_UNDETERMINED" in report.quality.not_assessed
    assert "PRIVATE_SOURCE" not in report.model_dump_json()


@pytest.mark.parametrize("dirty", [True, False])
def test_session_conflict_and_database_failure_keep_precise_safe_codes(dirty):
    """独立会话冲突和数据库故障不能被洗成评估成功/质量未知或回显异常原文。"""
    doc = snapshot()
    session = SessionStub(doc, [])
    if dirty:
        session.dirty = (object(),)
    else:
        session.failure = SQLAlchemyError("PRIVATE_DATABASE_SECRET")
    with pytest.raises(SpecialtyError) as caught:
        storage.DocumentAssessmentService(session_factory=lambda: session).analyze(
            DocumentAnalysisRequest(kb_id=doc.kb_id, doc_id=doc.doc_id))
    assert caught.value.code == ("ANALYSIS_SESSION" if dirty else "ANALYSIS_DATABASE")
    assert "PRIVATE" not in str(caught.value) and session.closed
