"""MinerU合法结构的通用契约回归；仅编写内存用例，由用户执行，无云端或数据库。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.services.mineru.adapter import restore_merged_text
from app.services.mineru.errors import MinerUError
from app.services.mineru.table_html import TableHTML, split_cell_text, verify_projection
from app.services.mineru.table_merge import partition_rows, restore_merged_tables
from app.services.mineru import table_merge as table_tool
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
    """单元格续接按V1原字符拆回两页，不当作重复表头或复制整表。"""
    sources = [TableHTML(html([["甲", "前半句"]])), TableHTML(html([["", "后半句"]]))]
    pieces, refinements, reused = partition_rows(TableHTML(html([["甲", "前半句 后半句"]])), sources)
    for source, piece in zip(sources, pieces):
        verify_projection(source, TableHTML(piece))
    assert refinements == [False, False] and reused == [0, 0]


def intervening_structure_case():
    """续段之间插入整页原样代码；物理页0/2的正文不归到中间页1。"""
    values, layout, geometry = merged_case(cross_page=True)
    values[1]["page_idx"] = 2
    layout["pdf_info"][1]["page_idx"] = 2
    geometry[2] = geometry.pop(1)
    geometry[1] = (600, 800)
    gap, parent = structural_gap("code")
    gap["page_idx"] = 1
    middle = {"page_idx": 1, "page_size": [600, 800], "para_blocks": [deepcopy(parent)],
              "preproc_blocks": [deepcopy(parent)], "discarded_blocks": []}
    layout["pdf_info"].append(middle)
    values.insert(1, gap)
    return values, layout, geometry


def test_verified_whole_structure_page_can_intervene_without_changing_original_pages():
    """每个中间页结构均被唯一保留时允许续段，正文原页/原ID不偏移。"""
    values, layout, geometry = intervening_structure_case()
    snapshot = deepcopy(values), deepcopy(layout)
    parsed = normalize_case(values, layout, geometry)
    assert [block.page for block in parsed.blocks] == [1, 2, 3]
    assert parsed.blocks[0].text + parsed.blocks[2].text == values[0]["text"]
    assert parsed.blocks[1].text == values[1]["code_body"]
    assert (values, layout) == snapshot


@pytest.mark.parametrize("damage", ["unretained_body", "changed_structure", "missing_page", "backwards", "cross_page"])
def test_nonadjacent_text_requires_complete_intermediate_page_evidence(damage):
    """缺页、隐藏独立正文、结构变化、倒页或错误跨页标记不能使用新例外。"""
    values, layout, geometry = intervening_structure_case()
    middle = layout["pdf_info"][-1]
    if damage == "unretained_body":
        for group in ("preproc_blocks", "para_blocks"):
            middle[group].append({"type": "text", "index": 9, "bbox": [10., 10., 100., 100.]})
    elif damage == "changed_structure":
        middle["preproc_blocks"][0]["blocks"][0]["lines"] = []
    elif damage == "missing_page":
        values.pop(1)
        layout["pdf_info"].pop()
    elif damage == "backwards":
        values[2]["page_idx"] = -1
    else:
        layout["pdf_info"][0]["para_blocks"][0]["lines"][1]["spans"][0]["cross_page"] = False
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code in {"MERGE_EVIDENCE", "EMPTY_BLOCK", "PAGE"}


@pytest.mark.parametrize("raw,left,right", [
    ("前半句 后半句", "前半句", "后半句"),
    ("前半句<br>后半句", "前半句", "后半句"),
    ("A &amp; B", "A &", "B"),
    ("12,345.67", "12,345.67", ""),
    (" 后续文字 ", "", "后续文字"),
])
def test_continued_cell_slices_preserve_exact_v1_bytes(raw, left, right):
    """原空白、br及实体保留，切片拼接严格等于原V1，不重新序列化或改数值。"""
    first, second = split_cell_text(raw, left, right)
    assert first + second == raw
    assert TableHTML(html([[first]])).rows[0]["cells"][0]["text"].replace(" ", "") == left.replace(" ", "")
    assert TableHTML(html([[second]])).rows[0]["cells"][0]["text"].replace(" ", "") == right.replace(" ", "")


@pytest.mark.parametrize("raw,left,right", [
    ("10 20", "11", "20"), ("10 20", "10", "21"),
    ("20 10", "10", "20"), ("10 20 extra", "10", "20"),
    ("1,200", "120", "0"), ("<eq>x+y</eq>", "$x", "+y$"),
    ("&NotEqualTilde;", "\u2242", "\u0338"),
])
def test_continued_cells_reject_changes_or_cuts_inside_markup(raw, left, right):
    """数字、字符、顺序或尾字冲突以及实体/公式内部切割均拒绝。"""
    with pytest.raises(MinerUError):
        split_cell_text(raw, left, right)


def continued_table_case():
    """构造末行和首行续接的两页表，正文仍仅取V1字节，不生成PDF原件。"""
    items, pages = table_case(footnote=False)
    originals = [html([["项目", "说明"], ["A &amp; B", "前半句"]]), html([["", "后半句"]])]
    for index, original in enumerate(originals):
        pages[index]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = original
    set_merged(items, pages, html([["项目", "说明"], ["A &amp; B", "前半句<br>后半句"]]))
    return items, pages


def test_continued_table_keeps_full_source_pages_cells_and_deterministic_ids():
    """完整Adapter恢复两页行数、值、来源和稳定ID；不将完整表复制到后页。"""
    items, pages = continued_table_case()
    snapshot, doc_id = (deepcopy(items), deepcopy(pages)), uuid4()
    parsed = normalize(items, pages, doc_id=doc_id)
    tables = [block for block in parsed.blocks if block.block_type == "table"]
    assert [block.page for block in tables] == [1, 2]
    assert [len(TableHTML(block.text).rows) for block in tables] == [2, 1]
    for index, block in enumerate(tables):
        source = pages[index]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"]
        verify_projection(TableHTML(source), TableHTML(block.text))
    assert normalize(items, pages, doc_id=doc_id) == parsed
    assert restore_merged_tables(items, pages)[1][0]["continued_row_count"] == 1
    assert (items, pages) == snapshot


@pytest.mark.parametrize("damage", ["value", "deleted", "page", "cross_rowspan"])
def test_continued_table_does_not_bypass_original_source_checks(damage):
    """续接核对原值/页码/跨度；无删除标记不猜恢复，由完整Adapter拒绝空表。"""
    items, pages = continued_table_case()
    if damage == "value":
        source = pages[1]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]
        source["html"] = source["html"].replace("后半句", "另一个值")
    elif damage == "deleted":
        pages[1]["para_blocks"][0]["blocks"][0]["lines_deleted"] = False
    elif damage == "page":
        items[2]["page_idx"] = 2
        pages[2] = pages.pop(1)
        pages[2]["page_idx"] = 2
    else:
        set_merged(items, pages, items[0]["table_body"].replace("<td>A", '<td rowspan="2">A'))
    snapshot = deepcopy(items), deepcopy(pages)
    if damage == "deleted":
        # 恢复函数只处理有删除证据的占位；不恢复不代表完整解析可接受空表。
        restored, summaries = restore_merged_tables(items, pages)
        assert restored == items
        assert summaries == []
        with pytest.raises(MinerUError) as error:
            normalize(items, pages)
        assert error.value.code == "EMPTY_BLOCK"
        assert error.value.diagnostic == {
            "reason": "TABLE_NO_BODY", "stage": "adapter.normalize",
            "structure_type": "table", "page": 2, "item_index": 3,
        }
    else:
        with pytest.raises(MinerUError) as error:
            restore_merged_tables(items, pages)
        assert error.value.code == "MERGE_EVIDENCE"
    assert (items, pages) == snapshot


def test_caption_absent_from_original_v1_is_reported_not_invented_from_layout():
    """附注在V1缺项时明确报告实际/预期数量，不能从布局补字或当作成功。"""
    items, pages = table_case(footnote=False)
    caption = {"type": "table_caption", "index": 1, "bbox": [48., 48., 552., 60.],
        "lines": [{"spans": [{"type": "text", "content": "PRIVATE_MISSING_CAPTION",
                                "bbox": [48., 48., 552., 60.]}]}]}
    pages[1]["preproc_blocks"][0]["blocks"].append(caption)
    pages[1]["para_blocks"][0]["blocks"].append(
        caption | {"lines": [], "lines_deleted": True})
    snapshot = deepcopy(items), deepcopy(pages)
    with pytest.raises(MinerUError) as error:
        restore_merged_tables(items, pages)
    assert error.value.diagnostic["reason"] == "TABLE_EXTRAS"
    assert (error.value.diagnostic["observed"], error.value.diagnostic["expected"]) == (0, 1)
    assert "PRIVATE" not in str(error.value.diagnostic) and "PRIVATE" not in str(error.value)
    assert (items, pages) == snapshot


def chained_table_case(*, count=3, second_join=False):
    """构造始终为两列的多页续接元数据；追加合并行保持行/单元格层级，不生成原件。"""
    header = [["项目", "说明"]]
    rows = [header + [["甲", "前段"]], header + [["", "后段"], ["乙", "中段"]]]
    if second_join:
        rows.append(header + [["", "尾段"], ["丙", "末段"]])
    else:
        rows.append(header + [["丙", "末段"]])
    rows.extend(header + [[f"条目{index}", f"数值{index}"]] for index in range(3, count))
    combined = header + [["甲", "前段<br>后段"], ["乙", "中段<br>尾段" if second_join else "中段"],
                         ["丙", "末段"]]
    # extend逐项追加一行，不能再包一层列表，否则两列会被序列化成一个单元格。
    combined.extend([f"条目{index}", f"数值{index}"] for index in range(3, count))
    items, pages = table_case(footnote=False)
    for index, source_rows in enumerate(rows):
        if index >= 2:
            pages[index] = deepcopy(pages[1])
            pages[index]["page_idx"] = index
            items.append(deepcopy(items[2]) | {"page_idx": index})
        pages[index]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = html(source_rows)
    set_merged(items, pages, html(combined))
    return items, pages


@pytest.mark.parametrize("count,second_join", [(3, False), (3, True), (4, False), (6, False)])
def test_chained_continuations_and_repeated_headers_keep_each_source_page(count, second_join):
    """先确认夹具两列完整，再核对Adapter的多页恢复、原页/值/稳定ID和输入不变。"""
    items, pages = chained_table_case(count=count, second_join=second_join)
    merged = TableHTML(items[0]["table_body"])
    assert merged.width == 2 and len(merged.rows) == count + 1
    assert all(len(row["cells"]) == 2 for row in merged.rows)
    snapshot, doc_id = (deepcopy(items), deepcopy(pages)), uuid4()
    restored, summary = restore_merged_tables(items, pages)
    positions = [index for index, value in enumerate(items) if value["type"] == "table"]
    assert restored[1] == items[1] and len(restored) == len(items)
    for page, position in enumerate(positions):
        raw = pages[page]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"]
        verify_projection(TableHTML(raw), TableHTML(restored[position]["table_body"]))
        assert restored[position]["bbox"] == items[position]["bbox"]
        assert restored[position]["page_idx"] == page
    assert summary[0]["reused_prefix_rows"] == [0] + [1] * (count - 1)
    assert summary[0]["continued_row_count"] == (2 if second_join else 1)
    layout, geometry = {"pdf_info": list(pages.values())}, {page: (600, 800) for page in pages}
    parsed = normalize_case(items, layout, geometry, doc_id=doc_id)
    assert [block.page for block in parsed.blocks if block.block_type == "table"] == list(range(1, count + 1))
    assert normalize_case(items, layout, geometry, doc_id=doc_id) == parsed
    assert (items, pages) == snapshot


def test_continued_row_column_refinement_is_proved_by_complete_source_page():
    """源页采用不同列网格时全页证明一致细化，不能只凭续接行文字相同接受。"""
    raw = ["<table><tr><td>标题</td><td>单位</td><td>金额</td></tr>"
           "<tr><td colspan=\"3\">前段</td></tr></table>",
           "<table><tr><td colspan=\"2\">后段</td></tr>"
           "<tr><td>甲</td><td>10</td></tr></table>",
           html([["乙", "20", "完成"]])]
    merged = TableHTML("<table><tr><td>标题</td><td>单位</td><td>金额</td></tr>"
        "<tr><td colspan=\"3\">前段 后段</td></tr>"
        "<tr><td colspan=\"2\">甲</td><td>10</td></tr>"
        "<tr><td>乙</td><td>20</td><td>完成</td></tr></table>")
    sources = [TableHTML(value) for value in raw]
    pieces, refinements, reused = partition_rows(merged, sources)
    for source, piece in zip(sources, pieces):
        verify_projection(source, TableHTML(piece))
    assert refinements == [False, True, False] and reused == [0, 0, 0]
    # 同一源列边界在后续行映射不一致，即使所有文字相同也不得接受。
    sources[1] = TableHTML(raw[1].replace("</table>", "<tr><td>丙</td><td>30</td></tr></table>"))
    inconsistent = TableHTML(merged.text.replace("<tr><td>乙", "<tr><td>丙</td><td colspan=\"2\">30</td></tr><tr><td>乙"))
    with pytest.raises(MinerUError):
        partition_rows(inconsistent, sources)


@pytest.mark.parametrize("damage,reason", [
    ("value", "TABLE_ROWS"), ("header", "TABLE_ROWS"), ("row_order", "TABLE_ROWS"),
    ("extra_row", "TABLE_ROWS"), ("bbox", "TABLE_MATCH"),
    ("deleted", "TABLE_DELETED"), ("cross_rowspan", "TABLE_ROWS"),
])
def test_chained_mapping_never_repairs_missing_values_or_invalid_sources(damage, reason):
    """先确认夹具合法，再按故障核对具体子原因，防止无关网格异常冒充来源校验通过。"""
    items, pages = chained_table_case(count=4)
    merged = TableHTML(items[0]["table_body"])
    assert merged.width == 2 and len(merged.rows) == 5
    assert all(len(row["cells"]) == 2 for row in merged.rows)
    if damage in {"value", "header"}:
        span = pages[2]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]
        span["html"] = span["html"].replace("末段", "被改写") if damage == "value" else span["html"].replace("项目", "其他列")
    elif damage == "row_order":
        fragments = [merged.text[row["start"]:row["end"]] for row in merged.rows]
        fragments[-2], fragments[-1] = fragments[-1], fragments[-2]
        set_merged(items, pages, "<table>" + "".join(fragments) + "</table>")
    elif damage == "extra_row":
        set_merged(items, pages, items[0]["table_body"].replace("</table>", "<tr><td>多项</td><td>99</td></tr></table>"))
    elif damage == "bbox":
        pages[2]["para_blocks"][0]["bbox"][0] += 30
    elif damage == "deleted":
        pages[2]["para_blocks"][0]["blocks"][0]["lines_deleted"] = False
    else:
        set_merged(items, pages, items[0]["table_body"].replace("<td>甲</td>", '<td rowspan="2">甲</td>')
                   .replace("<td>乙</td>", ""))
    snapshot = deepcopy(items), deepcopy(pages)
    with pytest.raises(MinerUError) as error:
        normalize_case(items, {"pdf_info": list(pages.values())}, {page: (600, 800) for page in pages})
    assert error.value.code == "MERGE_EVIDENCE"
    assert error.value.diagnostic["reason"] == reason
    assert (items, pages) == snapshot


def test_chained_mapping_refuses_ambiguous_reuse_or_empty_continuation():
    """空单元格既可重复又可续接时存在多解，不能任意选择其中一种来源解释。"""
    raw = html([["列"], [""]])
    with pytest.raises(MinerUError) as error:
        table_tool.partition_chained_rows(TableHTML(raw), [TableHTML(raw) for _ in range(3)])
    assert error.value.diagnostic["reason"] == "TABLE_ROW_MAPPING_AMBIGUOUS"


@pytest.mark.parametrize("budget", ["ROW_MAPPING_MAX_ATTEMPTS", "ROW_MAPPING_MAX_BYTES"])
def test_chained_mapping_budget_exhaustion_never_returns_first_candidate(monkeypatch, budget):
    """组合搜索超预算固定失败，不把尚未排除其他解释的首个候选当作成功。"""
    items, pages = chained_table_case()
    monkeypatch.setattr(table_tool, budget, 0)
    with pytest.raises(MinerUError) as error:
        restore_merged_tables(items, pages)
    assert error.value.diagnostic["reason"] == "TABLE_LIMIT"
