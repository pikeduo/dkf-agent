"""页边界插入的非连续合并回归：只用内存JSON/ZIP，不发HTTP或连接数据库。"""

from copy import deepcopy
from uuid import uuid4, uuid5

import pytest

from app.services.mineru.adapter import merge_group_indices, restore_merged_text
from app.services.mineru.errors import MinerUError
from test_mineru_merge import merged_case, normalize_case


def add_boundary(values, layout, geometry, position, *, page_idx=0, kind="header"):
    """插入具有独立discarded父块/原文/边界位置的页眉脚，不改动正文或合并Span。"""
    page = next(page for page in layout["pdf_info"] if page["page_idx"] == page_idx)
    top = kind == "header"
    box = [80, 10, 420, 40] if top else [80, 950, 420, 980]
    size = geometry[page_idx]
    raw = [number * size[index % 2] / 1000 for index, number in enumerate(box)]
    parent_index = 0 if top else max(b["index"] for b in page["para_blocks"]) + 1
    page["discarded_blocks"].append({"type": kind, "index": parent_index, "bbox": raw,
        "lines": [{"spans": [{"type": "text", "content": "独立边界文字", "bbox": raw}]}]})
    values.insert(position, {"type": kind, "text": "独立边界文字", "bbox": box, "page_idx": page_idx})


def boundary_case(*, kind="header", originals=("前段正文", "后页正文")):
    """保留跨页合并证据，为每页父块预留header索引0后插入独立边界。"""
    values, layout, geometry = merged_case(cross_page=True, originals=originals)
    for page in layout["pdf_info"]:
        for group in ("para_blocks", "preproc_blocks"):
            for block in page[group]:
                block["index"] += 1
    add_boundary(values, layout, geometry, 1, kind=kind)
    return values, layout, geometry


@pytest.mark.parametrize("kind", ["header", "footer", "page_number"])
def test_verified_margin_is_preserved_and_noncontiguous_source_indices_are_used(kind):
    """仅恢复[0,2]，页边界及其余字段原样保留，拼接字符/原页/稳定ID不变。"""
    values, layout, geometry = boundary_case(kind=kind)
    snapshot = deepcopy(values)
    assert merge_group_indices(values, 2) == ([0, 2], [1])
    restored, count = restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert count == 1 and len(restored) == len(values) and values == snapshot
    assert restored[1] == values[1] and restored[0]["text"] + restored[2]["text"] == values[0]["text"]
    for index in (0, 2):
        assert restored[index] | {"text": values[index]["text"]} == values[index]
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert {b.block_id for b in parsed.blocks} == {
        uuid5(doc_id, f"mineru-v1:frozen-input:{index + 1}:{item['page_idx']}:text")
        for index, item in enumerate(values)}
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert sorted(b.page for b in parsed.blocks) == [1, 1, 2]


def test_boundaries_between_multiple_placeholders_do_not_overwrite_any_boundary():
    """[0,2,4]多占位分别回原索引；两页页眉均不变，不压缩数组或连续覆盖。"""
    values, layout, geometry = boundary_case(originals=("原段", "后页第一段", "后页第二段"))
    add_boundary(values, layout, geometry, 3, page_idx=1)
    snapshot = deepcopy(values)
    assert merge_group_indices(values, 2) == ([0, 2, 4], [1, 3])
    restored, count = restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert count == 2 and len(restored) == 5 and values == snapshot
    assert restored[1] == values[1] and restored[3] == values[3]
    assert "".join(restored[index]["text"] for index in (0, 2, 4)) == values[0]["text"]


@pytest.mark.parametrize("damage,reason", [
    ("missing_layout", "BOUNDARY_MATCH"), ("duplicate_parent", "BOUNDARY_MATCH"),
    ("duplicate_index", "BOUNDARY_INDEX"), ("wrong_bbox", "BOUNDARY_MATCH"),
    ("interior_position", "BOUNDARY_POSITION"), ("wrong_boundary_text", "BOUNDARY_TEXT"),
    ("boundary_cross_page", "BOUNDARY_TEXT"), ("boundary_span_bbox", "BOUNDARY_TEXT"),
    ("wrong_source_text", "MOVED_TEXT"), ("wrong_cross_page", "MOVED_CROSS_PAGE"),
    ("nonadjacent_page", "SOURCE_PAGE"), ("wrong_source_bbox", "MOVED_BBOX"),
    ("duplicate_target", "TARGET_AMBIGUOUS"), ("duplicate_boundary_item", "BOUNDARY_REUSED"),
])
def test_boundary_skip_requires_unique_full_source_evidence(damage, reason):
    """页边界/目标多候选、文字/坐标或跨页证据冲突必须拒绝，不修改任意输入。"""
    values, layout, _ = boundary_case()
    first, second = layout["pdf_info"]
    margin = first["discarded_blocks"][0]
    moved = first["para_blocks"][0]["lines"][1]["spans"][0]
    if damage == "missing_layout":
        first["discarded_blocks"] = []
    elif damage == "duplicate_parent":
        duplicate = deepcopy(margin)
        duplicate["index"] = 99
        first["discarded_blocks"].append(duplicate)
    elif damage == "duplicate_index":
        margin["index"] = first["para_blocks"][0]["index"]
    elif damage == "wrong_bbox":
        values[1]["bbox"][0] += 20
    elif damage == "interior_position":
        values[1]["bbox"] = [80, 500, 420, 550]
        margin["bbox"] = [48, 400, 252, 440]
    elif damage == "wrong_boundary_text":
        values[1]["text"] = "PRIVATE_CHANGED_BOUNDARY"
    elif damage == "boundary_cross_page":
        margin["lines"][0]["spans"][0]["cross_page"] = True
    elif damage == "boundary_span_bbox":
        margin["lines"][0]["spans"][0].pop("bbox")
    elif damage == "wrong_source_text":
        moved["content"] = "PRIVATE_CHANGED_SOURCE"
    elif damage == "wrong_cross_page":
        moved["cross_page"] = False
    elif damage == "nonadjacent_page":
        values[2]["page_idx"] = second["page_idx"] = 2
    elif damage == "wrong_source_bbox":
        moved["bbox"] = [moved["bbox"][0] + 20, *moved["bbox"][1:]]
    elif damage == "duplicate_target":
        values.insert(0, deepcopy(values[0]))
    else:
        values.insert(1, deepcopy(values[1]))
    snapshot = deepcopy(values), deepcopy(layout)
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert error.value.code == "MERGE_EVIDENCE" and error.value.diagnostic["reason"] == reason
    assert (values, layout) == snapshot and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("kind", ["text", "title", "table", "equation", "image", "unknown"])
def test_other_content_types_are_never_skipped_to_find_an_earlier_target(kind):
    """其他正文、标题、表格、公式、图像及未知类型不能冒充可跨越边界。"""
    values, layout, _ = boundary_case()
    values[1]["type"] = kind
    snapshot = deepcopy(values)
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert error.value.code == "MERGE_EVIDENCE" and values == snapshot


def test_an_intervening_real_text_is_not_skipped_even_when_earlier_target_would_match():
    """最近正文不包含合并来源时失败，禁止为寻找能通过的旧目标跨越真实Text。"""
    values, layout, geometry = boundary_case()
    page = layout["pdf_info"][0]
    box = [80, 400, 420, 450]
    raw = [number * geometry[0][index % 2] / 1000 for index, number in enumerate(box)]
    paragraph = {"type": "text", "index": 9, "bbox": raw,
        "lines": [{"spans": [{"type": "text", "content": "插入的真实正文", "bbox": raw}]}]}
    for group in ("para_blocks", "preproc_blocks"):
        page[group].append(deepcopy(paragraph))
    values.insert(1, {"type": "text", "text": "插入的真实正文", "page_idx": 0, "bbox": box})
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert error.value.diagnostic["reason"] == "TARGET_SPAN_COUNT"


def test_many_margin_items_have_a_bounded_search_budget():
    """候选回溯有64项上限，不能因大量边界项无限回扫内容。"""
    values, layout, _ = boundary_case()
    values[1:1] = [deepcopy(values[1]) for _ in range(64)]
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert error.value.diagnostic["reason"] == "BOUNDARY_LIMIT"


def test_restored_text_does_not_hide_an_independent_empty_table():
    """合并恢复成功不代表整份结果通过；无正文表格仍需EMPTY_BLOCK，不能跳过或补造。"""
    values, layout, geometry = boundary_case()
    values.append({"type": "table", "page_idx": 1, "bbox": [80, 500, 900, 800],
                   "table_caption": [], "table_footnote": [], "img_path": "private-image-reference"})
    snapshot = deepcopy(values)
    restored, count = restore_merged_text(values, {p["page_idx"]: p for p in layout["pdf_info"]})
    assert count == 1 and restored[-1] == values[-1] and len(restored) == len(values)
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "EMPTY_BLOCK" and values == snapshot
