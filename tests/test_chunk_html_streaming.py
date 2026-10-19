"""大HTML表流式切行回归；仅编写，由用户手动执行，不读取真实文档或加载模型。"""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from app.services.chunk import html_rows, splitter
from app.services.mineru import table_html
from test_chunk_splitter import assert_evidence, block, chunks


def paragraph_table(rows=120):
    """构造保留原标签/实体/CRLF的内存行，模拟已入库的p单元格云表。"""
    return '<table>\r\n<tbody>\r\n' + ''.join(
        f'<tr><td><p>原值<strong>项目{i} &amp; &#49;</strong></p></td>'
        f'<td><p><eq>x_{i}+1</eq><br/>说明</p></td></tr>\r\n' for i in range(rows)) + '</tbody>\r\n</table>'


@pytest.mark.parametrize("limit,value", [
    ("max_chars", 1000), ("max_rows", 20), ("max_cells", 40), ("max_grid_cells", 40),
])
def test_v7_splits_past_each_old_html_aggregate_limit_without_changing_v6(monkeypatch, limit, value):
    """缩小旧预算复现三张巨表回退；v7不使用全表预算，v6身份/整表结果保持冻结。"""
    original_limits = table_html.TableLimits

    def reduced_limits(**kwargs):
        """只缩小旧解析器相应预算，不能让新切片靠抬高常量蒙混通过。"""
        return replace(original_limits(**kwargs), **{limit: value})

    monkeypatch.setattr(table_html, "TableLimits", reduced_limits)
    source = replace(block(1, paragraph_table(), "table", page=4), source="mineru_cloud")
    before, doc_id, kb_id = deepcopy(source), uuid4(), uuid4()
    old_options = splitter.ChunkOptions(version=splitter.STREAM_VERSION)
    old = chunks([source], old_options, doc_id=doc_id, kb_id=kb_id)
    new = chunks([source], doc_id=doc_id, kb_id=kb_id)
    assert len(old) == 1 and old[0].text == source.text and old[0].metadata["oversized"]
    assert old[0].metadata["chunker_version"] == "block-chunk-v6"
    assert len(new) > 1 and source == before
    assert all(draft.metadata["chunker_version"] == "block-chunk-v7" and not draft.metadata["oversized"] for draft in new)
    assert all(len(draft.text) < 2500 and draft.page_start == draft.page_end == 4 for draft in new)
    assert sum(len(table_html.TableHTML(draft.text, cell_paragraphs=True, cell_strong=True).rows) for draft in new) == 120
    assert new[0].metadata["input_blocks_hash"] == old[0].metadata["input_blocks_hash"]
    assert new == chunks([source], doc_id=doc_id, kb_id=kb_id)
    assert old == chunks([source], old_options, doc_id=doc_id, kb_id=kb_id)
    assert_evidence(new, [source])


def test_v7_scans_more_than_twenty_thousand_rows_without_materializing_table_grid(monkeypatch):
    """直接覆盖原2万行门槛；每次只留有界feed及列终点，不构建Adapter网格或截尾。"""
    source = replace(block(1, '<table>' + '<tr><td><p>A</p></td><td><p>B</p></td></tr>' * 20_001 + '</table>', "table"),
                     source="mineru_cloud")

    def reject_dom(*args, **kwargs):
        """确保v7没有重新启用或放大整表DOM解析器。"""
        raise AssertionError("不能为流式切片建立整表网格")

    monkeypatch.setattr(table_html, "TableHTML", reject_dom)
    result = chunks([source])
    assert len(result) > 1 and sum(draft.text.count('<tr>') for draft in result) == 20_001
    assert all(not draft.metadata["oversized"] for draft in result)
    assert_evidence(result, [source])


def test_v7_preserves_offsets_across_feed_boundaries_and_transitive_rowspan(monkeypatch):
    """跨feed的实体/标签/CRLF偏移准确；传递rowspan超目标仍整组，表题脚注只保留一次。"""
    monkeypatch.setattr(html_rows, "FEED_CHARS", 47)
    body = ('<table>\r\n<tbody>\r\n<tr><td rowspan="2">跨行A</td><td>' + '甲' * 250
            + '</td></tr>\r\n<tr><td rowspan="3">跨行B &amp; 原式</td></tr>\r\n'
            + '<tr><td>' + '乙' * 250 + '</td></tr>\r\n<tr><td>' + '丙' * 250
            + '</td></tr>\r\n<tr><td colspan="2"><strong>独立末行</strong></td></tr>\r\n</tbody></table>')
    source = block(1, '原表题\n' + body + '\n原脚注', "table", page=7)
    result = chunks([source])
    fragments = []
    for draft in result:
        spans = [span for span in draft.metadata["source_spans"]
                 if span["start_char"] >= len('原表题\n') and span["end_char"] <= len('原表题\n') + len(body)]
        if spans:
            fragments.append('\n\n'.join(draft.text[span["chunk_start_char"]:span["chunk_end_char"]] for span in spans))
    tables = [table_html.TableHTML(value, cell_strong=True) for value in fragments]
    assert [len(value.rows) for value in tables] == [4, 1]
    assert tables[0].rows[0]["cells"][0]["rowspan"] == 2
    assert tables[0].rows[1]["cells"][0]["rowspan"] == 3
    assert_evidence(result, [source])


@pytest.mark.parametrize("tail", [
    '<tr><td>缺列</td></tr>',
    '<tr><td rowspan="2">越界</td><td>B</td></tr>',
    '<tr><td rowspan="2">A</td><td>B</td></tr><tr><td colspan="2">重叠</td></tr>',
    '<tr><td style="hidden">未知属性</td><td>B</td></tr>',
    '<tr><td colspan="2" colspan="2">重复属性</td></tr>',
    '<tr><td rowspan="513">跨度越界</td><td>B</td></tr>',
    '<tr><td colspan="512">A</td><td colspan="512">B</td><td>C</td></tr>',
    '<tr><td><strong>坏嵌套</td></strong><td>B</td></tr>',
    '<!--隐藏结构-->', '<!DOCTYPE html>', '<tr></tr>',
])
def test_v7_late_invalid_html_never_emits_partial_rows_before_whole_fallback(monkeypatch, tail):
    """错误位于多个feed之后也必须先整体验证；不能先产子表再回退全表造成重复证据。"""
    monkeypatch.setattr(html_rows, "FEED_CHARS", 64)
    source = block(1, '<table>' + '<tr><td>A</td><td>B</td></tr>' * 200 + tail + '</table>', "table")
    result = chunks([source])
    assert len(result) == 1 and result[0].text == source.text and result[0].metadata["oversized"]
    assert_evidence(result, [source])


def test_v7_single_row_is_not_guessed_or_truncated_to_hide_token_limit():
    """一个不可拆长行仍完整保留，不能通过截断单元格或伪造来源规避真实Tokenizer。"""
    source = block(1, '<table><tr><td>' + '甲' * 9000 + '</td><td>原值</td></tr></table>', "table")
    result = chunks([source])
    assert len(result) == 1 and result[0].text == source.text and result[0].metadata["oversized"]
    assert_evidence(result, [source])
