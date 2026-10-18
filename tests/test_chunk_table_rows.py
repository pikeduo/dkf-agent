"""长表格行分组的纯内存回归；不读取真实文档、不加载模型、不连接业务数据库。"""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.services.chunk import splitter
from app.services.chunk.splitter import ChunkError, ChunkOptions, LEGACY_VERSION, ROW_VERSION, CAPTION_VERSION, VERSION
from app.services.mineru.table_html import TableHTML
from test_chunk_splitter import assert_evidence, block, chunks


def html_table(rows=80):
    """构造带实体/公式/换行的内存表格文本，验证切片不重写来源字符。"""
    body = "\n".join(f"<tr><td>项目{i} &amp; 原值</td><td><eq>x_{i}+1</eq></td></tr>" for i in range(rows))
    return "<table>\n<tbody>\n" + body + "\n</tbody>\n</table>"


def test_long_html_table_rows_keep_complete_grid_and_exact_evidence():
    """各片含完整表格外壳、原行及实体，所有原字符主内容恰好覆盖一次且原Block不变。"""
    sources = [block(1, "表格章节", "title"), block(2, html_table(), "table", page=13), block(3, "表后正文", page=14)]
    before = deepcopy(sources)
    drafts = chunks(sources)
    tables = [draft for draft in drafts if any(span["block_type"] == "table" for span in draft.metadata["source_spans"])]
    assert len(tables) > 1 and sources == before
    assert_evidence(drafts, sources)
    observed = []
    for draft in tables:
        spans = [span for span in draft.metadata["source_spans"] if span["block_type"] == "table"]
        # 排除独立标题上下文，只核对表格引用本身；分隔空白不是新增源文字。
        text = "\n\n".join(draft.text[span["chunk_start_char"]:span["chunk_end_char"]] for span in spans)
        table = TableHTML(text)
        observed.extend(cell["text"] for row in table.rows for cell in row["cells"])
        assert draft.metadata["chunker_version"] == VERSION
        assert all(span["page"] == 13 and span["block_id"] == str(sources[1].block_id) for span in spans)
    expected = [cell["text"] for row in TableHTML(sources[1].text).rows for cell in row["cells"]]
    assert observed == expected


@pytest.mark.parametrize("placement", ["caption", "footnote", "both"])
def test_captioned_or_footnoted_table_splits_body_with_exact_original_offsets(placement):
    """复现MinerU的表题+表体+脚注Block；只分表体，附文与原字符均保留且不重复。"""
    prefix = "原始表题及章节\n" if placement in {"caption", "both"} else ""
    suffix = "\n原始脚注与说明" * 60 if placement in {"footnote", "both"} else ""
    body = html_table()
    source = block(1, prefix + body + suffix, "table", page=13)
    drafts = chunks([source])
    assert len(drafts) > 1
    assert_evidence(drafts, [source])
    observed, table_count = [], 0
    for draft in drafts:
        # 标题/注释可能独立成片；只把完全位于原HTML表体的来源交给既有网格核对。
        spans = [span for span in draft.metadata["source_spans"]
                 if span["start_char"] >= len(prefix) and span["end_char"] <= len(prefix) + len(body)]
        if not spans:
            continue
        fragment = "\n\n".join(draft.text[span["chunk_start_char"]:span["chunk_end_char"]] for span in spans)
        table_count += 1
        observed.extend(cell["text"] for row in TableHTML(fragment).rows for cell in row["cells"])
    expected = [cell["text"] for row in TableHTML(body).rows for cell in row["cells"]]
    assert table_count > 1 and observed == expected
    assert all(draft.page_start == draft.page_end == 13 for draft in drafts)


def test_surrounding_whitespace_does_not_become_an_empty_chunk():
    """表外纯空白仍留在原外壳，不因超长rowspan关联组产生无法向量化的空白片。"""
    body = '<table><tr><td rowspan="2">A</td><td>' + "甲" * 700 + '</td></tr>'
    body += '<tr><td>' + "乙" * 700 + '</td></tr><tr><td colspan="2">末行</td></tr></table>'
    source = block(1, "\n  " + body + "\n  ", "table")
    drafts = chunks([source])
    assert len(drafts) > 1 and all(draft.text.strip() for draft in drafts)
    assert_evidence(drafts, [source])


def test_multiple_table_bodies_are_not_guessed_or_partially_dropped():
    """一个Block含多个表体时保留全块，不猜表题归属或丢掉第二张表。"""
    source = block(1, "表题\n" + html_table() + "\n另一表题\n" + html_table(), "table")
    drafts = chunks([source])
    assert len(drafts) == 1 and drafts[0].text == source.text
    assert_evidence(drafts, [source])


def test_transitive_rowspan_rows_are_never_separated():
    """rowspan关联具有传递性；超目标的关联行整组保留，不破坏跨行或colspan。"""
    rows = [
        '<tr><td rowspan="2">跨行A</td><td>' + "甲" * 240 + '</td></tr>',
        '<tr><td rowspan="3">跨行B</td></tr>',
        '<tr><td>' + "乙" * 240 + '</td></tr>',
        '<tr><td>' + "丙" * 240 + '</td></tr>',
        '<tr><td colspan="2">独立行</td></tr>',
    ]
    source = block(1, "<table>" + "".join(rows) + "</table>", "table")
    drafts = chunks([source])
    assert len(drafts) > 1
    first = TableHTML(drafts[0].text)
    assert len(first.rows) == 4 and drafts[0].metadata["oversized"]
    assert first.rows[0]["cells"][0]["rowspan"] == 2
    assert first.rows[1]["cells"][0]["rowspan"] == 3
    assert_evidence(drafts, [source])
    assert sum(len(TableHTML(draft.text).rows) for draft in drafts) == 5


def test_legacy_checkpoint_retains_whole_table_without_running_new_policy(monkeypatch):
    """旧v1检查点继续整表策略，升级只由新检查点或显式Reindex产生。"""
    source = block(1, html_table(), "table")
    monkeypatch.setattr(splitter, "table_units", MagicMock(side_effect=AssertionError("旧检查点不能改切片策略")))
    drafts = chunks([source], ChunkOptions(version=LEGACY_VERSION))
    assert len(drafts) == 1 and drafts[0].text == source.text
    assert drafts[0].metadata["chunker_version"] == LEGACY_VERSION
    assert drafts[0].metadata["config"]["version"] == LEGACY_VERSION
    assert_evidence(drafts, [source])


@pytest.mark.parametrize("version", [LEGACY_VERSION, ROW_VERSION, CAPTION_VERSION])
def test_completed_checkpoint_is_reused_without_upgrading_options(version):
    """已切片文档重复请求沿用v1～v3检查点，不重建Chunk或使现有向量失效。"""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.core.config import Settings
    from app.services.chunk.processing import checkpoint_options, prepare_chunk

    parse_id, chunk_id = uuid4(), uuid4()
    document = SimpleNamespace(doc_id=uuid4(), status="EMBEDDING", processing_task_id=parse_id,
        chunk_task_id=chunk_id, chunk_config={"options": ChunkOptions(version=version).snapshot(),
                                             "source_parse_task_id": str(parse_id)})
    session = MagicMock()
    session.scalar.return_value = None
    assert prepare_chunk(session, document, Settings(_env_file=None)) == (chunk_id, False)
    assert checkpoint_options(document).version == version
    session.flush.assert_not_called()


def test_frozen_v2_keeps_prior_behavior_for_captioned_table():
    """已经执行过的v2配方保持原行为；修复只用于新v3检查点，不暗改旧Chunk身份。"""
    from uuid import uuid4

    doc_id, kb_id = uuid4(), uuid4()
    source = block(1, "表题\n" + html_table() + "\n表下注释", "table")
    previous = chunks([source], ChunkOptions(version=ROW_VERSION), doc_id=doc_id, kb_id=kb_id)
    current = chunks([source], doc_id=doc_id, kb_id=kb_id)
    assert len(previous) == 1 and previous[0].text == source.text and len(current) > 1
    assert previous[0].metadata["chunker_version"] == ROW_VERSION
    assert {draft.chunk_id for draft in previous}.isdisjoint(draft.chunk_id for draft in current)
    assert_evidence(previous, [source])
    assert_evidence(current, [source])


def test_new_table_policy_is_deterministic_and_uses_different_chunk_identity():
    """同源同版本重跑稳定；新策略和旧整表Chunk不得复用身份或旧向量。"""
    from uuid import uuid4

    source, doc_id, kb_id = block(1, html_table(), "table"), uuid4(), uuid4()
    current = chunks([source], doc_id=doc_id, kb_id=kb_id)
    assert current == chunks([source], doc_id=doc_id, kb_id=kb_id)
    legacy = chunks([source], ChunkOptions(version=LEGACY_VERSION), doc_id=doc_id, kb_id=kb_id)
    assert {draft.chunk_id for draft in current}.isdisjoint(draft.chunk_id for draft in legacy)


@pytest.mark.parametrize("version", [[], {}, "unknown"])
def test_invalid_checkpoint_version_keeps_safe_chunk_error(version):
    """不可信版本包含列表/字典时也返回既有安全错误，不产生未分类TypeError。"""
    with pytest.raises(ChunkError, match="CHUNK_CONFIG_INVALID"):
        ChunkOptions(version=version).validate()


@pytest.mark.parametrize("text", [
    '<table><tr><td>' + '长' * 900 + '</td></tr></table>',
    '<table><tr><td rowspan="2">' + '长' * 900 + '</td></tr></table>',
    '<table style="width:100%"><tr><td>' + '长' * 900 + '</td></tr></table>',
    '列一\t列二\n' + '原生表格' * 900,
])
def test_unsplittable_or_unproven_rows_keep_original_content(text):
    """单个长行、未知结构或非HTML表格不猜切、不丢值，仍交真实Tokenizer判定模型上限。"""
    source = block(1, text, "table")
    drafts = chunks([source])
    assert len(drafts) == 1 and drafts[0].text == text and drafts[0].metadata["oversized"]
    assert_evidence(drafts, [source])
    assert "".join(drafts[0].text[span["chunk_start_char"]:span["chunk_end_char"]]
        for span in drafts[0].metadata["source_spans"] if span["role"] == "content") == text


def test_long_formula_keeps_original_unit():
    """新增行策略只作用于表格，超长公式完整保留，不借行策略截断公式。"""
    source = block(1, "公式" * 900, "formula")
    drafts = chunks([source])
    assert len(drafts) == 1 and drafts[0].text == source.text and drafts[0].metadata["oversized"]
    assert_evidence(drafts, [source])


@pytest.mark.parametrize("kind", ["xlsx", "csv"])
def test_native_quoted_tsv_splits_logical_rows_without_changing_cells(kind):
    """原生XLSX/CSV均输出TSV；字段内换行、Tab、双引号及公式必须完整留在原行。"""
    import csv
    from io import StringIO
    from app.parsers.csv import table_text

    rows = [[f"项目{i}\n单元格内换行", f'=SUM(A{i}:B{i})', '含\tTab及"引号"'] for i in range(180)]
    source = block(1, table_text(rows), "table", section="原工作表")
    drafts = chunks([source], file_type=kind)
    assert len(drafts) > 1
    observed = []
    for draft in drafts:
        observed.extend(csv.reader(StringIO(draft.text, newline=""), delimiter="\t", strict=True))
        assert draft.section == "原工作表" and draft.page_start == draft.page_end == 1
        assert not draft.metadata["oversized"]
        assert all(span["role"] == "content" for span in draft.metadata["source_spans"])
    assert observed == rows
    assert_evidence(drafts, [source])


@pytest.mark.parametrize("version", [LEGACY_VERSION, ROW_VERSION, CAPTION_VERSION])
def test_native_tsv_new_policy_does_not_change_frozen_versions(version):
    """旧v1～v3的TSV整表行为冻结；新v4才按行拆分，Chunk身份不混用。"""
    source = block(1, "\n".join(f"日期{i}\t数值{i}" for i in range(300)), "table")
    old = chunks([source], ChunkOptions(version=version), file_type="xlsx")
    current = chunks([source], file_type="xlsx")
    assert len(old) == 1 and old[0].text == source.text and len(current) > 1
    assert_evidence(current, [source])


def test_two_sheet_timeseries_uses_v4_rows_instead_of_two_oversized_v3_chunks():
    """复现两Sheet/1640个短日期数值行：v3整表，v4必须分组且原行/字符全部保留。"""
    sources = [block(1, "说明\t原值\n来源\t本地", "table", section="说明"),
        block(2, "\n".join(f"2020-01-{index % 28 + 1:02d}\t-{index}.123456789" for index in range(1640)),
              "table", section="数值")]
    old = chunks(sources, ChunkOptions(version=CAPTION_VERSION), file_type="xlsx")
    current = chunks(sources, file_type="xlsx")
    assert len(old) == 2 and old[1].metadata["oversized"]
    assert len(current) > 2 and all(chunk.metadata["chunker_version"] == VERSION for chunk in current)
    assert all(not chunk.metadata["oversized"] and len(chunk.text) <= 2000 for chunk in current)
    assert_evidence(current, sources)


@pytest.mark.parametrize("damage", ["quote", "width", "single_long_row", "single_column", "cloud"])
def test_native_rows_never_guess_bad_structure_or_split_inside_long_record(damage):
    """坏引号/列数、单列不明格式、单个超长记录或云端TSV不猜切，更不截断字段。"""
    from dataclasses import replace

    text = 'A\t"' + "长" * 900 if damage == "quote" else 'A\t' + "长" * 900
    if damage == "width":
        text = 'A\tB\nC\tD\t' + "长" * 900
    if damage == "single_column":
        text = "\n".join("单列" for _ in range(500))
    if damage == "cloud":
        text = "\n".join(f"项目{i}\t数值{i}" for i in range(300))
    source = block(1, text, "table")
    if damage == "cloud":
        source = replace(source, source="mineru_cloud")
    drafts = chunks([source], file_type="xlsx")
    assert len(drafts) == 1 and drafts[0].text == text
    assert_evidence(drafts, [source])


def test_large_html_new_budget_keeps_adapter_defaults_and_exact_evidence():
    """超过旧4096行的大表仅v4扩大资源预算；Adapter默认拒绝，行/网格校验仍保留。"""
    from app.services.mineru.errors import MinerUError
    from app.services.mineru.table_html import TableLimits

    source = block(1, '<table>' + '<tr><td>A</td><td>B</td></tr>' * 4100 + '</table>', "table")
    with pytest.raises(MinerUError):
        TableHTML(source.text)
    old = chunks([source], ChunkOptions(version=CAPTION_VERSION))
    current = chunks([source])
    assert len(old) == 1 and len(current) > 1
    assert_evidence(current, [source])
    assert sum(len(TableHTML(draft.text).rows) for draft in current) == 4100
    # 新规模预算不放松越界rowspan与矩形网格检查。
    with pytest.raises(MinerUError):
        TableHTML('<table><tr><td rowspan="2">A</td></tr></table>',
                  limits=TableLimits(max_rows=20_000, max_cells=400_000, max_grid_cells=524_288))


def test_source_budget_is_versioned_and_keeps_bounded_rejection(monkeypatch):
    """缩小预算验证v4与旧版各用自己的上限，超预算仍整份失败，不制造半份Chunk。"""
    monkeypatch.setattr(splitter, "MAX_SOURCE_CHARS", 100)
    monkeypatch.setattr(splitter, "MAX_V4_SOURCE_CHARS", 200)
    source = block(1, "甲" * 150)
    assert chunks([source])
    with pytest.raises(ChunkError, match="CHUNK_LIMIT"):
        chunks([source], ChunkOptions(version=CAPTION_VERSION))
    with pytest.raises(ChunkError, match="CHUNK_LIMIT"):
        chunks([block(1, "甲" * 201)])


def test_v4_html_cell_paragraphs_keep_values_raw_markup_and_rowspan():
    """复现大工作簿单元格中的p；仅v4可分组，原HTML/段落/跨行关系完整保留。"""
    from app.services.mineru.errors import MinerUError

    rows = '<tr><th><p>原表头</p></th><th><p>公式</p></th></tr>'
    rows += ''.join(f'<tr><td><p>项目{i}</p><p>说明 &amp; 原值</p></td>'
                    f'<td><p><eq>x_{i}</eq><br/>原式</p></td></tr>' for i in range(80))
    source = block(1, '<table>' + rows + '</table>', 'table')
    with pytest.raises(MinerUError):
        TableHTML(source.text)
    previous = chunks([source], ChunkOptions(version=CAPTION_VERSION))
    current = chunks([source])
    assert len(previous) == 1 and len(current) > 1
    expected = [cell['text'] for row in TableHTML(source.text, cell_paragraphs=True).rows for cell in row['cells']]
    observed = [cell['text'] for draft in current for row in TableHTML(draft.text, cell_paragraphs=True).rows
                for cell in row['cells']]
    assert observed == expected
    assert_evidence(current, [source])
    spanning = block(1, '<table><tr><td rowspan="2"><p>关联</p></td><td><p>' + '甲' * 700
        + '</p></td></tr><tr><td><p>续行</p></td></tr><tr><td colspan="2"><p>末行</p></td></tr></table>', 'table')
    grouped = chunks([spanning])
    assert len(TableHTML(grouped[0].text, cell_paragraphs=True).rows) == 2
    assert_evidence(grouped, [spanning])


@pytest.mark.parametrize('body', [
    '<p><tr><td>A</td></tr></p>',
    '<tr><td><p style="hidden">A</p></td></tr>',
    '<tr><td><p><p>A</p></p></td></tr>',
    '<tr><td rowspan="2"><p>A</p></td></tr>',
])
def test_cell_paragraph_option_keeps_grid_nesting_and_attribute_guards(body):
    """段落兼容不放松表外p、嵌套p、未知属性或越界rowspan的结构保护。"""
    from app.services.mineru.errors import MinerUError

    with pytest.raises(MinerUError):
        TableHTML('<table>' + body + '</table>', cell_paragraphs=True)
