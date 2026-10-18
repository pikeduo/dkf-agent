"""MinerU合法结构的通用契约回归；仅编写内存用例，由用户执行，无云端或数据库。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.services.mineru.adapter import restore_merged_text
from app.services.mineru.errors import MinerUError
from app.services.mineru.table_html import TableHTML, verify_projection
from app.services.mineru.table_merge import partition_rows, restore_merged_tables
from test_mineru_merge import merged_case, normalize_case
from test_mineru_merge_boundaries import boundary_case
from test_mineru_stage33_text_compat import multispan_case
from test_mineru_table_merge import html, normalize, set_merged, table_case


def repeated_formula_case():
    """同一父段内两次出现相同公式及坐标，来源身份是出现序号而非字段值。"""
    values, layout, geometry = multispan_case()
    page = layout["pdf_info"][0]
    for group in ("preproc_blocks", "para_blocks"):
        spans = page[group][0]["lines"][0]["spans"]
        spans.append(deepcopy(spans[-1]))
    values[0]["text"] = "关系 $x+y$ $x+y$ 续段正文"
    return values, layout, geometry


def test_repeated_formula_occurrences_keep_all_v1_characters_and_source_pages():
    """合法重复不去重，不造字；重复转换、页码、ID及输入不变。"""
    values, layout, geometry = repeated_formula_case()
    snapshot, doc_id = (deepcopy(values), deepcopy(layout)), uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert [block.text.strip() for block in parsed.blocks] == ["关系 $x+y$ $x+y$", "续段正文"]
    assert "".join(block.text for block in parsed.blocks) == values[0]["text"]
    assert [block.page for block in parsed.blocks] == [1, 2]
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert (values, layout) == snapshot


@pytest.mark.parametrize("damage", ["source_only", "target_only", "v1_missing", "bbox", "formula"])
def test_repeated_formula_does_not_hide_sequence_or_source_conflicts(damage):
    """数量、完整V1、原式与坐标继续逐项检查，不能因允许重复而丢字。"""
    values, layout, geometry = repeated_formula_case()
    page = layout["pdf_info"][0]
    if damage in {"source_only", "target_only"}:
        group = "preproc_blocks" if damage == "source_only" else "para_blocks"
        page[group][0]["lines"][0]["spans"].pop()
    elif damage == "v1_missing":
        values[0]["text"] = "关系 $x+y$ 续段正文"
    elif damage == "bbox":
        page["para_blocks"][0]["lines"][0]["spans"][-1]["bbox"][0] += 10
    else:
        page["para_blocks"][0]["lines"][0]["spans"][-1]["content"] = "x-y"
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"


def test_multispan_header_is_not_mistaken_for_invalid_body():
    """页眉一行多Span是正常结构，仍完整匹配V1并保持非连续写回。"""
    values, layout, geometry = boundary_case()
    header = layout["pdf_info"][0]["discarded_blocks"][0]
    header["lines"][0]["spans"] *= 2
    values[1]["text"] *= 2
    snapshot = deepcopy(values), deepcopy(layout)
    parsed = normalize_case(values, layout, geometry)
    assert sum(block.text == values[1]["text"] for block in parsed.blocks) == 1
    assert (values, layout) == snapshot


def structural_gap(kind):
    """构造有独立layout身份的原样代码、旁注或页下注释，不生成文档文件。"""
    box = [500, 450, 900, 500]
    raw = [300., 360., 540., 400.]
    parent = {"type": kind, "index": 8, "bbox": raw}
    value = {"type": kind, "page_idx": 0, "bbox": box, "text": "独立间隔"}
    if kind == "code":
        parent["blocks"] = [{"type": "code_body", "index": 8, "bbox": raw,
                             "lines": [{"spans": [{"type": "code", "content": "独立间隔", "bbox": raw}]}]}]
        value.pop("text")
        value["code_body"] = "独立间隔"
    return value, parent


def test_code_between_cross_page_paragraphs_is_preserved_not_selected_as_target():
    """原样代码不是合并目标；正文仍按真实非连续索引回写且不吞代码。"""
    values, layout, geometry = merged_case(cross_page=True)
    gap, parent = structural_gap("code")
    for group in ("preproc_blocks", "para_blocks"):
        layout["pdf_info"][0][group].append(deepcopy(parent))
    values.insert(1, gap)
    snapshot = deepcopy(values), deepcopy(layout)
    restored, count = restore_merged_text(values, {page["page_idx"]: page for page in layout["pdf_info"]})
    assert count == 1 and restored[1] == gap
    assert restored[0]["text"] + restored[2]["text"] == values[0]["text"]
    assert normalize_case(values, layout, geometry).blocks
    assert (values, layout) == snapshot
    layout["pdf_info"][0]["para_blocks"][-1]["blocks"][0]["lines"] = []
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.diagnostic["reason"] == "BOUNDARY_MATCH"


@pytest.mark.parametrize("kind", ["aside_text", "page_footnote", "code"])
def test_tables_can_cross_verified_unchanged_gaps_without_modifying_them(kind):
    """跨页表格链可越过来源明确的原样间隔，不误判为缺行或丢失表体。"""
    items, pages = table_case(footnote=False)
    gap, parent = structural_gap(kind)
    if kind == "code":
        for group in ("preproc_blocks", "para_blocks"):
            pages[0][group].append(deepcopy(parent))
    else:
        pages[0]["discarded_blocks"].append(parent)
    items.insert(1, gap)
    snapshot = deepcopy(items), deepcopy(pages)
    restored, summary = restore_merged_tables(items, pages)
    assert restored[1] == gap and len(restored) == len(items)
    assert summary[0]["pages"] == [1, 2]
    assert summary[0]["rows"] == [2, 2]
    assert normalize(items, pages).blocks
    assert (items, pages) == snapshot
    destination = pages[0]["para_blocks"] if kind == "code" else pages[0]["discarded_blocks"]
    destination.append(deepcopy(parent))
    with pytest.raises(MinerUError):
        restore_merged_tables(items, pages)


@pytest.mark.parametrize("wrapper", [True, False])
def test_tbody_and_line_break_serialization_do_not_change_table_identity(wrapper):
    """tbody/br包装不是网格错误，正文、数值和跨度仍须相同。"""
    source = TableHTML("<table><tr><td>项目<br>金额</td><td>10</td></tr></table>")
    raw = "<tr><td>项目 金额</td><td>10</td></tr>"
    raw = "<table>" + ("<tbody>" + raw + "</tbody>" if wrapper else raw) + "</table>"
    verify_projection(source, TableHTML(raw))
    with pytest.raises(MinerUError):
        verify_projection(source, TableHTML(raw.replace(">10<", ">11<")))


@pytest.mark.parametrize("header_rows", [1, 2])
def test_verified_repeated_headers_use_v1_rows_and_keep_each_original_page(header_rows):
    """重复页首行必须真实存在于两页，复用已验证V1行而非拷贝layout正文。"""
    items, pages = table_case(footnote=False)
    headers = [[f"列{i}", f"单位{i}"] for i in range(header_rows)]
    sources = [headers + [["甲", "10"]], headers + [["乙", "20"]]]
    for index, rows in enumerate(sources):
        pages[index]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = html(rows)
    set_merged(items, pages, html(headers + [["甲", "10"], ["乙", "20"]]))
    snapshot = deepcopy(items), deepcopy(pages)
    restored, summary = restore_merged_tables(items, pages)
    assert [value["page_idx"] for value in (restored[0], restored[2])] == [0, 1]
    for index, position in enumerate((0, 2)):
        verify_projection(TableHTML(html(sources[index])), TableHTML(restored[position]["table_body"]))
        assert restored[position]["bbox"] == items[position]["bbox"]
    assert summary[0]["reused_prefix_rows"] == [0, header_rows]
    assert normalize(items, pages).blocks
    assert (items, pages) == snapshot


@pytest.mark.parametrize("damage", ["missing_data", "changed_header", "changed_number", "extra_row", "row_order"])
def test_repeated_header_support_cannot_hide_missing_or_changed_cells(damage):
    """仅页首相同内容可复用，不能据总行数凑出正文、调换单元格或修改数值。"""
    header = [["项目", "金额"]]
    sources = [TableHTML(html(header + [["甲", "10"]])),
               TableHTML(html(header + [["乙", "20"], ["丙", "30"]]))]
    rows = header + [["甲", "10"], ["乙", "20"], ["丙", "30"]]
    if damage == "missing_data":
        rows.pop(2)
    elif damage == "changed_header":
        rows[0] = ["其他项目", "金额"]
    elif damage == "changed_number":
        rows[2][1] = "21"
    elif damage == "extra_row":
        rows.append(["多余", "40"])
    else:
        rows[2], rows[3] = rows[3], rows[2]
    with pytest.raises(MinerUError):
        partition_rows(TableHTML(html(rows)), sources)


def test_cross_page_cell_concatenation_is_not_mistaken_for_repeated_header():
    """单元格续接不是重复表头；未实现可靠字符分配前仍拒绝，不猜来源。"""
    sources = [TableHTML(html([["甲", "前半句"]])), TableHTML(html([["", "后半句"]]))]
    with pytest.raises(MinerUError) as error:
        partition_rows(TableHTML(html([["甲", "前半句 后半句"]])), sources)
    assert error.value.diagnostic["reason"] == "TABLE_ROWS"
