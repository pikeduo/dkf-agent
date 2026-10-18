"""长表格行分组的纯内存回归；不读取真实文档、不加载模型、不连接业务数据库。"""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.services.chunk import splitter
from app.services.chunk.splitter import ChunkError, ChunkOptions, LEGACY_VERSION, VERSION
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


def test_completed_v1_checkpoint_is_reused_without_upgrading_options():
    """已切片文档重复请求沿用v1检查点，不偷偷重建Chunk或使现有向量失效。"""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.core.config import Settings
    from app.services.chunk.processing import checkpoint_options, prepare_chunk

    parse_id, chunk_id = uuid4(), uuid4()
    document = SimpleNamespace(doc_id=uuid4(), status="EMBEDDING", processing_task_id=parse_id,
        chunk_task_id=chunk_id, chunk_config={"options": ChunkOptions(version=LEGACY_VERSION).snapshot(),
                                             "source_parse_task_id": str(parse_id)})
    session = MagicMock()
    session.scalar.return_value = None
    assert prepare_chunk(session, document, Settings(_env_file=None)) == (chunk_id, False)
    assert checkpoint_options(document).version == LEGACY_VERSION
    session.flush.assert_not_called()


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
