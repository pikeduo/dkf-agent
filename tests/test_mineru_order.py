"""验证仅凭官方 layout 索引恢复页边界项，不以 bbox 排序重建复杂阅读顺序。"""

from copy import deepcopy
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from mineru_samples import result_zip

from app.services.mineru.adapter import MinerUResultAdapter


def item(text, bbox, *, kind="text", page=0, **fields):
    """建立含页码与位置的独立结构项，保留调用方提供的表格和公式字段。"""

    return {"type": kind, "text": text, "bbox": bbox, "page_idx": page, **fields}


def page_items(page=0):
    """模拟正文、页底、最后追加页眉的真实字段形状，不依赖某个文件名或 OCR 标记。"""

    return [
        item("正文", [80, 200, 650, 230], page=page),
        item("页底", [80, 900, 500, 920], page=page),
        item("顶部标题", [80, 50, 650, 80], kind="header", page=page),
    ]


def layout_for(items, indices=None):
    """给每个原始项添加可唯一关联的父块和页内索引，坐标单位使用真实页面尺寸。"""

    if indices is None:
        indices = [
            2
            if value["type"] == "footer"
            else 0
            if value["type"] == "header"
            else offset + 1
            for offset, value in enumerate(items)
        ]
    pages = {}
    for value, index in zip(items, indices):
        page = pages.setdefault(
            value["page_idx"],
            {
                "page_idx": value["page_idx"],
                "page_size": [1000, 1000],
                "para_blocks": [],
                "discarded_blocks": [],
            },
        )
        block = {"type": value["type"], "bbox": value["bbox"], "index": index}
        group = (
            "discarded_blocks"
            if value["type"] in {"header", "footer"}
            else "para_blocks"
        )
        page[group].append(block)
    return {"pdf_info": list(pages.values())}


def normalize(items, layout=None, *, doc_id=None, archive=None):
    """只在内存运行 Adapter，不调用网络、Celery 或真实数据库。"""

    return MinerUResultAdapter().normalize(
        result_zip(items, layout=layout) if archive is None else archive,
        doc_id=uuid4() if doc_id is None else doc_id,
        file_name="验收.pdf",
        file_type="pdf",
        geometry={value["page_idx"]: (1000, 1000) for value in items},
        identity="same-source-and-options",
    )


def test_single_column_restores_header_and_preserves_block_identity():
    """顶部页眉恢复到正文之前；顺序改变和重试都不改变由原始位置生成的稳定 ID。"""

    values = page_items()
    identity = uuid4()
    original = normalize(values, doc_id=identity)
    repaired = normalize(values, layout_for(values, [1, 2, 0]), doc_id=identity)
    assert [block.text for block in repaired.blocks] == ["顶部标题", "正文", "页底"]
    assert {block.text: block.block_id for block in original.blocks} == {
        block.text: block.block_id for block in repaired.blocks
    }
    assert repaired == normalize(values, layout_for(values, [1, 2, 0]), doc_id=identity)
    assert all(
        block.page == 1 and block.source == "mineru_cloud" for block in repaired.blocks
    )
    assert repaired.blocks[0].bbox == values[2]["bbox"]
    assert repaired.blocks[0].block_type == "text"  # 页眉不冒充上游未提供的标题层级。


@pytest.mark.parametrize("pages", [(0, 1), (1, 0)])
def test_two_pages_never_change_physical_page_slots(pages):
    """只改各自页面内部槽位，既不按全局 y 排序，也不擅自调整原始页序。"""

    values = page_items(pages[0]) + page_items(pages[1])
    repaired = normalize(values, layout_for(values, [1, 2, 0, 1, 2, 0]))
    assert [block.page for block in repaired.blocks] == [pages[0] + 1] * 3 + [
        pages[1] + 1
    ] * 3
    assert [block.text for block in repaired.blocks] == ["顶部标题", "正文", "页底"] * 2


def test_interleaved_page_slots_are_not_grouped_or_globally_sorted():
    """即使上游页面交错，也只能在对应页面的原有槽位内恢复页眉。"""

    values = [value for pair in zip(page_items(0), page_items(1)) for value in pair]
    repaired = normalize(values, layout_for(values, [1, 1, 2, 2, 0, 0]))
    assert [block.page for block in repaired.blocks] == [1, 2, 1, 2, 1, 2]
    assert [block.text for block in repaired.blocks] == [
        "顶部标题",
        "顶部标题",
        "正文",
        "正文",
        "页底",
        "页底",
    ]


def test_equal_y_body_blocks_keep_their_original_relative_order():
    """同一 y 区域的正文以原始顺序为准，不按 x、文本或 UUID 改序。"""

    values = [
        item("正文乙", [80, 200, 650, 230]),
        item("正文甲", [82, 200, 640, 231]),
        page_items()[2],
    ]
    repaired = normalize(values, layout_for(values, [1, 2, 0]))
    assert [block.text for block in repaired.blocks] == ["顶部标题", "正文乙", "正文甲"]


def test_table_and_its_caption_body_footnote_remain_one_block():
    """恢复页眉不拆分表格 HTML、合并单元格、图注和脚注，也不改变实际块类型。"""

    table = item(
        "",
        [80, 300, 850, 600],
        kind="table",
        table_caption=["表题"],
        table_body='<table><tr><td colspan="2">合并单元格</td></tr></table>',
        table_footnote=["被上游归入脚注的正文"],
    )
    values = [page_items()[0], table, page_items()[1], page_items()[2]]
    repaired = normalize(values, layout_for(values, [1, 2, 3, 0]))
    assert len(repaired.blocks) == 4
    block = repaired.blocks[2]
    assert block.block_type == "table" and block.bbox == table["bbox"]
    assert block.text == "表题\n" + table["table_body"] + "\n被上游归入脚注的正文"


def test_formula_text_is_atomic_and_not_semantically_corrected():
    """公式仅作为一个证据块移动，不改写上游名称、LaTeX 或百分比。"""

    formula = item(
        r"$G_0=(a-b)/b\times100\%$",
        [80, 300, 750, 350],
        kind="equation",
        confidence=0.8,
    )
    values = [page_items()[0], formula, page_items()[1], page_items()[2]]
    repaired = normalize(values, layout_for(values, [1, 2, 3, 0]))
    block = repaired.blocks[2]
    assert block.block_type == "formula" and block.text == formula["text"]
    assert block.bbox == formula["bbox"] and block.confidence == 0.8


def test_missing_bbox_preserves_entire_page_without_dropping_block():
    """缺少位置时无法唯一关联，整页原序保留且未知 bbox 仍为空。"""

    values = page_items()
    layout = layout_for(values, [1, 2, 0])
    values[0]["bbox"] = None
    parsed = normalize(values, layout)
    assert [block.text for block in parsed.blocks] == ["正文", "页底", "顶部标题"]
    assert parsed.blocks[0].bbox is None and len(parsed.blocks) == 3


@pytest.mark.parametrize("columns", [2, 3])
def test_multiple_columns_keep_the_original_order_even_with_layout(columns):
    """两栏或多栏的并排正文触发布局不确定保护，不移动页眉或混排两栏内容。"""

    values = [
        item(f"第{column}栏", [50 + column * 300, 200, 250 + column * 300, 250])
        for column in range(columns)
    ]
    values.append(page_items()[2])
    parsed = normalize(values, layout_for(values, list(range(1, columns + 1)) + [0]))
    assert [block.text for block in parsed.blocks] == [
        value["text"] for value in values
    ]


def test_staggered_column_reading_order_is_not_rebuilt():
    """按栏阅读发生纵向回跳时保守保留，不能因为没有同一 y 并排块就判为单栏。"""

    values = [
        item("左栏上", [50, 200, 400, 250]),
        item("左栏下", [50, 500, 400, 550]),
        item("右栏上", [550, 300, 950, 350]),
        page_items()[2],
    ]
    parsed = normalize(values, layout_for(values, [1, 2, 3, 0]))
    assert [block.text for block in parsed.blocks] == [
        value["text"] for value in values
    ]


def test_empty_image_is_not_filled_and_retains_original_position_for_ids():
    """空图片只保留用于关联的边界信息，不伪造 OCR；跳过后正文 ID 仍取原始位置。"""

    values = page_items()
    values.insert(
        1,
        item(
            "",
            [475, 640, 830, 740],
            kind="image",
            content="",
            image_caption=[],
            image_footnote=[],
        ),
    )
    parsed = normalize(values, layout_for(values, [1, 2, 3, 0]))
    assert [block.text for block in parsed.blocks] == ["顶部标题", "正文", "页底"]


@pytest.mark.parametrize(
    "failure",
    [
        "absent",
        "unknown_schema",
        "duplicate_page",
        "missing_parent",
        "duplicate_index",
        "bool_index",
        "different_bbox",
        "ambiguous_bbox",
        "body_reorder",
        "wrong_header_index",
        "header_not_at_top",
        "bad_page_size",
        "bad_layout_bbox",
    ],
)
def test_uncertain_order_metadata_preserves_original_page(failure):
    """缺少可信顺序证据或证据与正文冲突时整页退回原序，不把未知元数据当指令。"""

    values = page_items()
    layout = layout_for(values, [1, 2, 0])
    page = layout["pdf_info"][0]
    if failure == "absent":
        layout = None
    elif failure == "unknown_schema":
        layout = {"pages": []}
    elif failure == "duplicate_page":
        layout["pdf_info"].append(deepcopy(page))
    elif failure == "missing_parent":
        page["para_blocks"].pop()
    elif failure == "duplicate_index":
        page["para_blocks"][1]["index"] = 1
    elif failure == "bool_index":
        page["discarded_blocks"][0]["index"] = False
    elif failure == "different_bbox":
        page["para_blocks"][0]["bbox"] = [500, 500, 600, 600]
    elif failure == "ambiguous_bbox":
        page["para_blocks"][1]["bbox"] = values[0]["bbox"]
    elif failure == "body_reorder":
        page["para_blocks"][0]["index"], page["para_blocks"][1]["index"] = 2, 1
    elif failure == "wrong_header_index":
        page["discarded_blocks"][0]["index"] = 3
    elif failure == "header_not_at_top":
        values[2]["bbox"] = [80, 500, 650, 530]
        layout = layout_for(values, [1, 2, 0])
    elif failure == "bad_page_size":
        page["page_size"] = [1000, 0]
    elif failure == "bad_layout_bbox":
        page["para_blocks"][0]["bbox"] = [0, 0, 1001, 10]
    parsed = normalize(values, layout)
    assert [block.text for block in parsed.blocks] == ["正文", "页底", "顶部标题"]


def test_explicit_footer_uses_official_index_not_y_sort():
    """页脚仅在上游明确标为 footer、位置与索引都在正文之后时才恢复到页底。"""

    values = [
        item("页脚", [80, 900, 500, 920], kind="footer"),
        page_items()[0],
        page_items()[2],
    ]
    parsed = normalize(values, layout_for(values, [2, 1, 0]))
    assert [block.text for block in parsed.blocks] == ["顶部标题", "正文", "页脚"]


def test_plain_title_is_not_reordered_from_geometry_alone():
    """普通 title 的位置不是充分依据；本版只恢复明确的 header/footer。"""

    values = page_items()
    values[2]["type"] = "title"
    parsed = normalize(values, layout_for(values, [1, 2, 0]))
    assert [block.text for block in parsed.blocks] == ["正文", "页底", "顶部标题"]


@pytest.mark.parametrize("mode", ["invalid_json", "ambiguous_member"])
def test_optional_layout_failure_does_not_replace_valid_content(mode):
    """可选 layout 损坏或存在多个文件时保留 V1 正文，不回退 Markdown 或 V2。"""

    values = page_items()
    buffer = BytesIO(result_zip(values))
    with ZipFile(buffer, "a") as archive:
        archive.writestr("layout.json", "not-json" if mode == "invalid_json" else "{}")
        if mode == "ambiguous_member":
            archive.writestr("other/layout.json", "{}")
    parsed = normalize(values, archive=buffer.getvalue())
    assert [block.text for block in parsed.blocks] == ["正文", "页底", "顶部标题"]
