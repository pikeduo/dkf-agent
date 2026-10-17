"""跨页表格严格恢复回归：仅内存元数据/ZIP，无PDF生成、数据库或云端请求。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from mineru_samples import result_zip

from app.services.mineru.adapter import MinerUResultAdapter, EmptyBlockError
from app.services.mineru.errors import MinerUError
from app.services.mineru.table_html import TableHTML, verify_projection
from app.services.mineru.table_merge import restore_merged_tables


def html(rows):
    """把纯测试单元格包装成受限HTML，不创建或解析真实文档。"""
    return "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows) + "</table>"


def table_case(*, footnote=True):
    """构造两页原表、合并后V1/para及已删除后页table_body，保留严格来源坐标。"""
    original_html = [html([["A", "10"], ["C", "30"]]), html([["B", "20"], ["D", "40"]])]
    merged = original_html[0][:-8] + original_html[1][7:]
    items, pages = [], {}
    for index in range(2):
        box = [80, 700, 920, 900] if index == 0 else [80, 80, 920, 300]
        raw = [number * (600 if i % 2 == 0 else 800) / 1000 for i, number in enumerate(box)]
        span = {"type": "table", "html": original_html[index], "bbox": raw, "image_path": ""}
        child = {"type": "table_body", "index": 2, "bbox": raw,
                 "lines": [{"spans": [span]}]}
        parent = {"type": "table", "index": 2, "bbox": raw, "blocks": [child]}
        if index == 1 and footnote:
            parent["blocks"].append({"type": "table_footnote", "index": 3, "bbox": [48, 260, 552, 280],
                "lines": [{"spans": [{"type": "text", "content": "后页注释", "bbox": [48, 260, 552, 280]}]}]})
        pages[index] = {"page_idx": index, "page_size": [600, 800],
                        "preproc_blocks": [deepcopy(parent)], "para_blocks": [parent], "discarded_blocks": []}
        value = {"type": "table", "page_idx": index, "bbox": box, "img_path": "",
                 "table_caption": [], "table_footnote": ["后页注释"] if index == 0 and footnote else [""] if footnote else []}
        if index == 0:
            value["table_body"] = merged
            child["lines"][0]["spans"][0]["html"] = merged
        else:
            for block in parent["blocks"]:
                block.update(lines=[], lines_deleted=True)
        items.append(value)
    if footnote:
        pages[0]["para_blocks"][0]["blocks"].append(deepcopy(pages[1]["preproc_blocks"][0]["blocks"][1]))
    header = {"type": "header", "index": 0, "bbox": [48, 8, 552, 32],
              "lines": [{"spans": [{"type": "text", "content": "页眉", "bbox": [48, 8, 552, 32]}]}]}
    pages[0]["discarded_blocks"].append(header)
    items.insert(1, {"type": "header", "page_idx": 0, "bbox": [80, 10, 920, 40], "text": "页眉"})
    return items, pages


def normalize(items, pages, *, doc_id=None):
    """只调用正式Adapter，page geometry来自固定Mock，不读原件或外部服务。"""
    return MinerUResultAdapter().normalize(result_zip(items, layout={"pdf_info": list(pages.values())}),
        doc_id=doc_id or uuid4(), file_name="metadata.pdf", file_type="pdf",
        geometry={0: (600, 800), 1: (600, 800)}, identity="frozen-case")


def set_merged(items, pages, value):
    """同步改变V1和后处理HTML，用于专门测试结构冲突而非仅触发字符串不符。"""
    items[0]["table_body"] = value
    pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = value


@pytest.mark.parametrize("footnote", [True, False])
def test_verified_rows_and_footnotes_return_to_source_pages_without_copying_layout(footnote):
    """恢复原行与附注原页，页眉/位置/ID/所有单元格不变；重复执行稳定且输入无修改。"""
    items, pages = table_case(footnote=footnote)
    snapshot = deepcopy(items), deepcopy(pages)
    restored, summary = restore_merged_tables(items, pages)
    assert len(restored) == 3 and restored[1] == items[1]
    assert summary[0]["rows"] == [2, 2] and summary[0]["cells"] == [4, 4]
    assert summary[0]["pages"] == [1, 2] and summary[0]["item_indices"] == [1, 3]
    assert restored[0]["table_footnote"] == []
    assert restored[2]["table_footnote"] == (["后页注释"] if footnote else [])
    body = [TableHTML(restored[i]["table_body"]) for i in (0, 2)]
    merged = TableHTML(items[0]["table_body"])
    assert [c["text"] for part in body for row in part.rows for c in row["cells"]] == [
        c["text"] for row in merged.rows for c in row["cells"]]
    assert (items, pages) == snapshot and restore_merged_tables(items, pages) == (restored, summary)
    doc_id = uuid4()
    parsed = normalize(items, pages, doc_id=doc_id)
    tables = [b for b in parsed.blocks if b.block_type == "table"]
    assert [b.page for b in tables] == [1, 2]
    assert tables[0].block_id == uuid5(doc_id, "mineru-v1:frozen-case:1:0:table")
    assert tables[1].block_id == uuid5(doc_id, "mineru-v1:frozen-case:3:1:table")
    assert normalize(items, pages, doc_id=doc_id) == parsed
    from app.parsers.routing.contracts import RouteDecision, StructuralSignals, PageSignals
    from app.services.mineru.pdf_split_jobs import validate_piece
    decision = RouteDecision(file_type="pdf", parse_mode="mineru", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals(page_count=2, pages=tuple(
            PageSignals(page=page, text_chars=1, image_count=0, image_area_ratio=0,
                        drawing_count=0, annotation_count=0, required=True) for page in (1, 2))))
    validate_piece(SimpleNamespace(), SimpleNamespace(parse_options={"_cache_guard": decision.model_dump(mode="json")}),
                   SimpleNamespace(page_start=1, page_end=2), parsed)


@pytest.mark.parametrize("damage", ["missing_row", "duplicate_row", "cell_order", "value", "rowspan",
    "deleted", "index", "bbox", "ambiguous_layout", "ambiguous_v1", "page",
    "caption", "footnote", "unknown_child", "image", "foreign_text"])
def test_unproven_tables_fail_without_partial_recovery(damage):
    """缺/重复行、单元格乱序/改值、跨度/页/来源/附注/图像冲突均拒绝且不改输入。"""
    items, pages = table_case()
    source = pages[1]["preproc_blocks"][0]
    if damage in {"missing_row", "duplicate_row"}:
        row = "<tr><td>A</td><td>10</td></tr>"
        merged = items[0]["table_body"]
        set_merged(items, pages, merged.replace(row, "" if damage == "missing_row" else row + row))
    elif damage == "cell_order":
        set_merged(items, pages, items[0]["table_body"].replace("<td>B</td><td>20</td>", "<td>20</td><td>B</td>"))
    elif damage == "value":
        set_merged(items, pages, items[0]["table_body"].replace("<td>20</td>", "<td>21</td>"))
    elif damage == "rowspan":
        set_merged(items, pages, items[0]["table_body"].replace("<td>C</td>", '<td rowspan="2">C</td>'))
    elif damage == "deleted":
        pages[1]["para_blocks"][0]["blocks"][0]["lines_deleted"] = False
    elif damage == "index":
        source["index"] = 99
    elif damage == "bbox":
        source["bbox"] = [100, 100, 200, 200]
    elif damage == "ambiguous_layout":
        duplicate = deepcopy(source)
        duplicate["index"] = 9
        pages[1]["preproc_blocks"].append(duplicate)
    elif damage == "ambiguous_v1":
        items.insert(0, deepcopy(items[0]))
    elif damage == "page":
        items[2]["page_idx"] = 2
        pages[2] = pages.pop(1)
        pages[2]["page_idx"] = 2
    elif damage == "caption":
        items[0]["table_caption"] = ["PRIVATE_NEW_CAPTION"]
    elif damage == "footnote":
        items[0]["table_footnote"] = ["PRIVATE_CHANGED_FOOTNOTE"]
    elif damage == "unknown_child":
        source["blocks"].append({"type": "unknown", "index": 10})
    elif damage == "image":
        items[0]["img_path"] = "https://private.invalid/SECRET"
    else:
        items[1]["type"] = "text"
    snapshot = deepcopy(items), deepcopy(pages)
    with pytest.raises(MinerUError) as error:
        normalize(items, pages)
    assert error.value.code in {"MERGE_EVIDENCE", "EMPTY_BLOCK"}
    assert "PRIVATE" not in str(error.value) and "SECRET" not in str(error.value)
    assert (items, pages) == snapshot


def test_column_refinement_proves_relationships_without_rewriting_v1_spans():
    """稳定单调细化可兼容，但不能收缩/交换列或改变rowspan/单元格数。"""
    source = TableHTML(html([["A", "1"], ["B", "2"]]))
    refined_html = html([["A", "1"], ["B", "2"]]).replace("<td>A", '<td colspan="3">A').replace("<td>B", '<td colspan="3">B')
    refined = TableHTML(refined_html)
    verify_projection(source, refined)
    assert source.width == 2 and refined.width == 4 and refined.text == refined_html
    with pytest.raises(MinerUError):
        verify_projection(refined, source)
    inconsistent = TableHTML('<table><tr><td colspan="3">A</td><td>1</td></tr>'
                             '<tr><td>B</td><td colspan="3">2</td></tr></table>')
    with pytest.raises(MinerUError):
        verify_projection(source, inconsistent)


def test_rowspan_inside_a_page_is_preserved_but_crossing_slice_is_refused():
    """页内跨行/跨列可复核，切口跨rowspan时不能丢单元格或擅自截断跨度。"""
    value = '<table><tr><td rowspan="2">A</td><td>1</td></tr><tr><td>2</td></tr></table>'
    source = TableHTML(value)
    verify_projection(source, TableHTML(source.slice_rows(0, 2)))
    with pytest.raises(MinerUError):
        TableHTML(source.slice_rows(0, 1))


def test_whitespace_entities_and_v1_row_bytes_are_preserved():
    """白名单空白归一化只用于核对；输出仍是V1原HTML实体、数值及行字节。"""
    merged = TableHTML("<table><tr><td>A &amp; B</td><td>1,200.00</td></tr>\n"
                       "<tr><td>C</td><td>20%</td></tr></table>")
    source = TableHTML("<table><tr><td>A  &amp;  B</td><td>1,200.00</td></tr></table>")
    raw = merged.slice_rows(0, 1)
    verify_projection(source, TableHTML(raw))
    assert "A &amp; B" in raw and "1,200.00" in raw


@pytest.mark.parametrize("value", [
    "<table><tr><td>1</tr></table>", "<table><tr><td>1</td></tr>",
    '<table><tr><td rowspan="0">1</td></tr></table>',
    '<table><tr><td colspan="999999999999999">1</td></tr></table>',
    "<table><tr><td>1</td><script>PRIVATE</script></tr></table>",
    '<table><tr><td style="PRIVATE">1</td></tr></table>',
    "<table><tr><td><table><tr><td>1</td></tr></table></td></tr></table>",
    "<table><!--PRIVATE--><tr><td>1</td></tr></table>",
    "<table><tr><td>1</td></tr><tr><td>1</td><td>2</td></tr></table>",
])
def test_unknown_or_invalid_html_is_not_repaired_or_silently_flattened(value):
    """坏HTML/未知样式/嵌套表/网格缺口安全失败，不让结构修复伪造成功。"""
    with pytest.raises(MinerUError) as error:
        TableHTML(value)
    assert error.value.code == "MERGE_EVIDENCE" and "PRIVATE" not in str(error.value)


def test_images_must_match_original_ownership_and_unique_zip_members():
    """图片仅核对并保留V1引用，不能把前页整图复制到后页或接受重复/外部资源。"""
    items, pages = table_case(footnote=False)
    for index in range(2):
        path = f"source-{index}.jpg"
        pages[index]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["image_path"] = path
    pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["image_path"] = "source-0.jpg"
    items[0]["img_path"] = "images/source-0.jpg"
    members = ["images/source-0.jpg", "images/source-1.jpg"]
    restored, _ = restore_merged_tables(items, pages, archive_members=members)
    assert restored[0]["img_path"] == items[0]["img_path"] and restored[2]["img_path"] == ""
    for wrong in (None, members[:1], members + [members[0]]):
        with pytest.raises(MinerUError):
            restore_merged_tables(items, pages, archive_members=wrong)


def test_caption_and_moved_footnote_are_kept_once_with_their_original_table():
    """首表caption不跨页移动，后表footnote按原唯一来源从V1归还且各保留一次。"""
    items, pages = table_case()
    caption = {"type": "table_caption", "index": 1, "bbox": [48, 560, 552, 580],
        "lines": [{"spans": [{"type": "text", "content": "原表标题", "bbox": [48, 560, 552, 580]}]}]}
    for name in ("para_blocks", "preproc_blocks"):
        pages[0][name][0]["blocks"].insert(0, deepcopy(caption))
    items[0]["table_caption"] = ["原表标题"]
    restored, summary = restore_merged_tables(items, pages)
    assert restored[0]["table_caption"] == ["原表标题"] and restored[2]["table_caption"] == []
    assert restored[0]["table_footnote"] == [] and restored[2]["table_footnote"] == ["后页注释"]
    assert summary[0]["caption_counts"] == [1, 0] and summary[0]["footnote_counts"] == [0, 1]


def test_ambiguous_repeated_extra_spans_are_rejected_instead_of_guessing_a_page():
    """相同文字/坐标的附注若无法唯一关联原页，拒绝而不按数组位置猜来源。"""
    items, pages = table_case()
    note = deepcopy(pages[1]["preproc_blocks"][0]["blocks"][1])
    pages[0]["preproc_blocks"][0]["blocks"].append(deepcopy(note))
    extra = deepcopy(note)
    extra["index"] = 4
    pages[0]["para_blocks"][0]["blocks"].append(extra)
    items[0]["table_footnote"] *= 2
    with pytest.raises(MinerUError) as error:
        restore_merged_tables(items, pages)
    assert error.value.diagnostic["reason"] == "TABLE_EXTRAS"


def test_three_page_merge_uses_source_rows_and_physical_order_not_fixed_pairs():
    """通用连续多页链无需硬编码两页或验收索引；逐页保留原行数和单元格。"""
    items, pages = table_case(footnote=False)
    pages[2] = deepcopy(pages[1])
    pages[2]["page_idx"] = 2
    third = html([["E", "50"], ["F", "60"]])
    pages[2]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = third
    items.append(deepcopy(items[2]) | {"page_idx": 2})
    set_merged(items, pages, items[0]["table_body"][:-8] + third[7:])
    restored, summary = restore_merged_tables(items, pages)
    assert summary[0]["rows"] == [2, 2, 2] and summary[0]["pages"] == [1, 2, 3]
    assert [len(TableHTML(restored[i]["table_body"]).rows) for i in (0, 2, 3)] == [2, 2, 2]
    assert "50" in restored[3]["table_body"] and "50" not in restored[0]["table_body"]


def test_empty_diagnostic_has_safe_one_based_page_position_and_structure(monkeypatch):
    """无法恢复的空表格不拿caption冒充正文，诊断只含固定原因、1-based页/序号和类型。"""
    from app.services.mineru import adapter
    from app.core.business_logging import safe_fields
    events = []

    def collect(event, **fields):
        """捕获白名单摘要，不写日志文件。"""
        events.append((event, safe_fields(fields)))

    monkeypatch.setattr(adapter, "log_event", collect)
    items = [{"type": "table", "page_idx": 1, "table_caption": ["PRIVATE_BODY"]}]
    with pytest.raises(EmptyBlockError) as error:
        normalize(items, {})
    assert error.value.diagnostic == {"reason": "TABLE_NO_BODY", "stage": "adapter.normalize",
        "structure_type": "table", "page": 2, "item_index": 1}
    assert events[-1][0] == "mineru.empty_block" and events[-1][1]["structure_type"] == "table"
    assert "PRIVATE" not in str(events) and safe_fields({"structure_type": "PRIVATE_BODY"}) == {}
