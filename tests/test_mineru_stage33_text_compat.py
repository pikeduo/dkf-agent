"""阶段33真实结构的元数据回归；用户运行，无云端、原件解析、模型或数据库。"""

from copy import deepcopy
from uuid import uuid4, uuid5

import pytest

from app.services.mineru.adapter import restore_merged_text
from app.services.mineru.errors import MinerUError
from app.services.mineru.table_merge import restore_merged_tables
from test_mineru_merge import merged_case, normalize_case
from test_mineru_merge_boundaries import boundary_case
from test_mineru_table_merge import table_case


def layout_pages(layout):
    """只转换内存页索引，不读取或推断原件内容。"""
    return {page["page_idx"]: page for page in layout["pdf_info"]}


def test_reference_text_is_a_regular_text_block_with_unchanged_v1_provenance():
    """真实V1的ref_text保留原字/页/bbox及位置ID，不从layout拼接或编造引文。"""
    values, layout, geometry = merged_case(originals=("原有正文",))
    value = {"type": "ref_text", "text": "参考文本 [1]：原样保留。", "page_idx": 0,
             "bbox": [80, 500, 420, 580]}
    values.append(value)
    original = deepcopy(values), deepcopy(layout)
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert len(parsed.blocks) == 2
    reference = parsed.blocks[1]
    assert reference.text == value["text"] and reference.page == 1 and reference.block_type == "text"
    assert reference.bbox == [48., 400., 252., 464.] and reference.source == "mineru_cloud"
    assert reference.block_id == uuid5(doc_id, "mineru-v1:frozen-input:2:0:text")
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert (values, layout) == original


@pytest.mark.parametrize("damage,code", [("empty", "EMPTY_BLOCK"), ("page", "PAGE"),
    ("bbox", "BBOX"), ("unknown_type", "CONTENT_TYPE")])
def test_reference_text_still_requires_real_text_page_and_geometry(damage, code):
    """支持ref_text不放宽真正空正文、错误页码、坏坐标或未知类型。"""
    values, layout, geometry = merged_case(originals=("原有正文",))
    value = {"type": "ref_text", "text": "参考原文", "page_idx": 0, "bbox": [80, 500, 420, 580]}
    if damage == "empty":
        value["text"] = ""
    elif damage == "page":
        value["page_idx"] = 10
    elif damage == "bbox":
        value["bbox"] = [80, 500, 420]
    else:
        value["type"] = "unknown_reference_type"
    values.append(value)
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == code


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("cross_page", [False, True])
def test_verified_cross_page_dehyphenation_preserves_exact_v1_and_source_identity(retained, cross_page):
    """同页分栏/跨页的已验证接缝可续词；V1字符、原页/bbox/ID均保留。"""
    values, layout, geometry = merged_case(cross_page=cross_page,
        originals=("Reporting inter-", "national results"))
    values[0]["text"] = "Reporting inter-national results" if retained else "Reporting international results"
    original = deepcopy(values), deepcopy(layout)
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert "".join(b.text for b in parsed.blocks) == values[0]["text"]
    assert parsed.blocks[0].text == ("Reporting inter-" if retained else "Reporting inter")
    assert parsed.blocks[1].text == "national results"
    assert [b.page for b in parsed.blocks] == [1, 2 if cross_page else 1]
    size = geometry[values[1]["page_idx"]]
    assert parsed.blocks[1].bbox == [number * size[index % 2] / 1000
                                     for index, number in enumerate(values[1]["bbox"])]
    assert parsed.blocks[1].block_id == uuid5(doc_id, f"mineru-v1:frozen-input:2:{1 if cross_page else 0}:text")
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert (values, layout) == original


@pytest.mark.parametrize("left,right,v1", [
    ("value 12-", "34", "value 1234"),
    ("delta x-", "5", "delta x5"),
    ("word inter-", "national", "word inter national"),
    ("word inter-", "national", "word interXnational"),
    ("word inter-", "national", "word international EXTRA"),
    ("word in-ter", "national", "word international"),
    ("word inter−", "national", "word international"),
])
def test_dehyphenation_does_not_hide_numbers_changes_or_unmatched_v1(left, right, v1):
    """只兼容英文跨页断词，数字/算符/中间连字符/空格/额外正文仍须拒绝。"""
    values, layout, geometry = merged_case(cross_page=True, originals=(left, right))
    values[0]["text"] = v1
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"
    assert error.value.diagnostic["reason"] in {"TEXT_CHARACTER", "TEXT_TRAILING"}


@pytest.mark.parametrize("damage,reason", [
    ("cross_page", "MOVED_CROSS_PAGE"),
    ("bbox", "MOVED_BBOX"), ("moved_text", "MOVED_TEXT"), ("source_page", "SOURCE_PAGE"),
])
def test_dehyphenation_requires_all_original_source_checks(damage, reason):
    """逐项验证原来源子码；bbox故障只改目标Span，不污染共享坐标的占位父块。"""
    values, layout, geometry = merged_case(cross_page=True,
        originals=("Reporting inter-", "national results"))
    values[0]["text"] = "Reporting international results"
    moved = layout["pdf_info"][0]["para_blocks"][0]["lines"][1]["spans"][0]
    if damage == "cross_page":
        moved["cross_page"] = False
    elif damage == "bbox":
        original_bbox = list(moved["bbox"])
        # 夹具的原Span与占位父块共享raw_box；替换列表，避免同时破坏占位定位。
        moved["bbox"] = [original_bbox[0] + 30, *original_bbox[1:]]
        page = layout["pdf_info"][1]
        assert page["para_blocks"][0]["bbox"] == original_bbox
        assert page["preproc_blocks"][0]["bbox"] == original_bbox
        assert page["preproc_blocks"][0]["lines"][0]["spans"][0]["bbox"] == original_bbox
    elif damage == "moved_text":
        moved["content"] = "different results"
    elif damage == "source_page":
        values[1]["page_idx"] = 2
        layout["pdf_info"][1]["page_idx"] = 2
        geometry[2] = geometry.pop(1)
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"
    assert error.value.diagnostic["reason"] == reason


def test_verified_empty_header_is_preserved_with_original_id_and_no_fabricated_text():
    """V1/layout同时确认为空的页眉保留空块，不删除项或改变正文来源。"""
    values, layout, geometry = boundary_case()
    values[1]["text"] = ""
    layout["pdf_info"][0]["discarded_blocks"][0]["lines"][0]["spans"][0]["content"] = ""
    original = deepcopy(values), deepcopy(layout)
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    header = next(b for b in parsed.blocks if b.block_id == uuid5(doc_id, "mineru-v1:frozen-input:2:0:text"))
    assert header.text == "" and header.page == 1 and header.bbox == [48., 8., 252., 32.]
    assert len(parsed.blocks) == 3
    assert "".join(b.text for b in parsed.blocks) == values[0]["text"]
    assert (values, layout) == original


@pytest.mark.parametrize("damage", ["nonempty_layout", "missing_span", "cross_page", "missing_bbox"])
def test_empty_header_does_not_bypass_original_source_checks(damage):
    """空页眉必须有唯一真实空来源，不放过不一致、缺失或跨页Span。"""
    values, layout, geometry = boundary_case()
    values[1]["text"] = ""
    span = layout["pdf_info"][0]["discarded_blocks"][0]["lines"][0]["spans"][0]
    span["content"] = "" if damage != "nonempty_layout" else "不可丢失的文字"
    if damage == "missing_span":
        layout["pdf_info"][0]["discarded_blocks"][0]["lines"] = []
    elif damage == "cross_page":
        span["cross_page"] = True
    elif damage == "missing_bbox":
        span.pop("bbox")
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"
    assert error.value.diagnostic["reason"] == "BOUNDARY_TEXT"


def test_empty_boundary_cannot_satisfy_required_page_coverage():
    """空页眉不是有效正文页，保持原Completeness拒绝能力。"""
    from app.parsers.routing.contracts import RouteDecision, PageSignals, StructuralSignals
    from app.parsers.routing.guard import CompletenessError, guard_result
    values, layout, geometry = boundary_case()
    # 先生成合法空页眉块，再将其作为另一必需页的唯一候选，Guard必须拒绝。
    values[1]["text"] = ""
    layout["pdf_info"][0]["discarded_blocks"][0]["lines"][0]["spans"][0]["content"] = ""
    parsed = normalize_case(values, layout, geometry)
    header = next(b for b in parsed.blocks if not b.text)
    body = next(b for b in parsed.blocks if b.text)
    parsed = parsed.model_copy(update={"blocks": [body.model_copy(update={"page": 1}),
        header.model_copy(update={"page": 2})]})
    decision = RouteDecision(parse_mode="mineru", file_type="pdf", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals(page_count=2,
        pages=tuple(PageSignals(page=page, text_chars=1, image_count=0, image_area_ratio=0,
            drawing_count=0, annotation_count=0, required=True) for page in (1, 2))))
    with pytest.raises(CompletenessError):
        guard_result(parsed, decision)


@pytest.mark.parametrize("kind", ["image", "table", "aside_text", "page_footnote"])
def test_unique_unchanged_structural_gap_does_not_steal_merged_text(kind):
    """图表/旁注有唯一layout身份时只越过原样项，正文仍须完整逐Span核对。"""
    values, layout, geometry = merged_case(cross_page=True)
    page = layout["pdf_info"][0]
    box, raw = [500, 200, 900, 400], [300., 160., 540., 320.]
    parent = {"type": kind, "index": 8, "bbox": raw}
    if kind in {"aside_text", "page_footnote"}:
        page["discarded_blocks"].append(parent)
    else:
        page["para_blocks"].append(deepcopy(parent))
        page["preproc_blocks"].append(deepcopy(parent))
    gap = {"type": kind, "page_idx": 0, "bbox": box, "text": "独立结构"}
    if kind == "table":
        gap["table_body"] = "<table><tr><td>独立结构</td></tr></table>"
    values.insert(1, gap)
    original = deepcopy(values), deepcopy(layout)
    restored, count = restore_merged_text(values, layout_pages(layout))
    assert count == 1 and restored[1] == gap and len(restored) == len(values)
    assert restored[0]["text"] + restored[2]["text"] == values[0]["text"]
    assert (values, layout) == original
    assert [restored[i]["page_idx"] for i in (0, 2)] == [0, 1]
    assert normalize_case(values, layout, geometry).blocks
    # 重复父块或缺少前处理证据不能作为合法间隔。
    destination = page["discarded_blocks"] if kind in {"aside_text", "page_footnote"} else page["para_blocks"]
    destination.append(parent | {"index": 9})
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, layout_pages(layout))
    assert error.value.diagnostic["reason"] == "BOUNDARY_MATCH"


@pytest.mark.parametrize("kind", ["header", "footer", "page_number"])
def test_boundary_position_uses_2d_overlap_and_does_not_reorder_body(kind):
    """跨栏标题/另一阅读流的索引不应误拒；页码仍须在全部正文之外。"""
    values, layout, geometry = boundary_case(kind=kind)
    page = layout["pdf_info"][0]
    boundary = page["discarded_blocks"][0]
    if kind == "header":
        # 与左侧页眉同y、但x不相交的右栏标题，不应被一维y判断误拒。
        page["para_blocks"].append({"type": "title", "index": 9, "bbox": [336., 8., 540., 32.]})
    elif kind == "footer":
        boundary["index"] = 0  # 唯一，但早于另一阅读流的正文索引。
    else:
        values[1]["bbox"] = [80, 810, 420, 840]
        boundary["bbox"] = [48., 648., 252., 672.]
        boundary["lines"][0]["spans"][0]["bbox"] = list(boundary["bbox"])
    restored, count = restore_merged_text(values, layout_pages(layout))
    assert count == 1 and restored[1] == values[1]
    assert restored[0]["text"] + restored[2]["text"] == values[0]["text"]
    if kind == "header":
        page["para_blocks"][-1]["bbox"] = [48., 8., 252., 32.]
        with pytest.raises(MinerUError) as error:
            restore_merged_text(values, layout_pages(layout))
        assert error.value.diagnostic["reason"] == "BOUNDARY_POSITION"


def multispan_case():
    """构造跨页文字及行内公式Span，V1保留公式标点而非从layout补造。"""
    values, layout, geometry = merged_case(cross_page=True)
    source = layout["pdf_info"][0]["preproc_blocks"][0]
    left, top, right, bottom = source["bbox"]
    middle = (left + right) / 2
    source["lines"] = [{"spans": [
        {"type": "text", "content": "关系", "bbox": [left, top, middle, bottom]},
        {"type": "inline_equation", "content": "x+y", "bbox": [middle, top, right, bottom]}]}]
    moved = layout["pdf_info"][0]["para_blocks"][0]
    moved["lines"][0] = deepcopy(source["lines"][0])
    values[0]["text"] = "关系 $x+y$ 续段正文"
    return values, layout, geometry


def test_multispan_text_and_inline_formula_preserve_all_v1_characters_and_pages():
    """多个Span归还原父块，保留原式/转义/空白/页码和稳定业务ID。"""
    values, layout, geometry = multispan_case()
    original = deepcopy(values), deepcopy(layout)
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert [b.text.strip() for b in parsed.blocks] == ["关系 $x+y$", "续段正文"]
    assert "".join(b.text for b in parsed.blocks) == values[0]["text"]
    assert [b.page for b in parsed.blocks] == [1, 2]
    assert parsed.blocks[1].block_id == uuid5(doc_id, "mineru-v1:frozen-input:2:1:text")
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert (values, layout) == original


@pytest.mark.parametrize("damage", ["formula", "bbox", "cross_page", "v1_extra", "unknown_span"])
def test_multispan_conflicts_are_not_hidden(damage):
    """混合Span仍逐项核对原式、坐标、跨页标记、V1字符及固定类型。"""
    values, layout, geometry = multispan_case()
    moved = layout["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][1]
    if damage == "formula":
        moved["content"] = "x-y"
    elif damage == "bbox":
        moved["bbox"][0] += 20
    elif damage == "cross_page":
        layout["pdf_info"][0]["para_blocks"][0]["lines"][1]["spans"][0]["cross_page"] = False
    elif damage == "v1_extra":
        values[0]["text"] += "不可忽略的尾字"
    else:
        moved["type"] = "unknown"
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"


def test_table_footnote_multispan_requires_complete_original_v1_and_source_geometry():
    """多Span表下注释保留原页和原V1文字；丢字/缺项仍安全失败。"""
    items, pages = table_case()
    source = pages[1]["preproc_blocks"][0]["blocks"][1]
    source["lines"] = [{"spans": [
        {"type": "text", "content": "说明", "bbox": [48., 260., 300., 280.]},
        {"type": "inline_equation", "content": "x+y", "bbox": [300., 260., 552., 280.]}]}]
    pages[0]["para_blocks"][0]["blocks"][1] = deepcopy(source)
    items[0]["table_footnote"] = ["说明 $x+y$"]
    restored, _ = restore_merged_tables(items, pages)
    assert restored[0]["table_footnote"] == []
    assert restored[2]["table_footnote"] == ["说明 $x+y$"]
    items[0]["table_footnote"] = []
    with pytest.raises(MinerUError) as error:
        restore_merged_tables(items, pages)
    assert error.value.diagnostic["reason"] == "TABLE_EXTRAS"
