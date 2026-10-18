"""合并占位兼容用例：仅元数据与内存 ZIP，用户执行；不读取真实样本或调用云端。"""

from copy import deepcopy
from uuid import uuid4, uuid5

import pytest
from mineru_samples import result_zip

from app.services.mineru.adapter import MinerUResultAdapter, merged_text_slices
from app.services.mineru.errors import MinerUError


def merged_case(*, cross_page=False, columns=False, originals=("前段正文", "续段正文")):
    """建立真实字段形状的删除占位与合并 span；不生成 PDF、图片或 Office 原件。"""

    geometry = {0: (600, 800)}
    if cross_page:
        geometry[1] = (900, 1200)
    values, pages, source_spans = [], {}, []
    for index, text in enumerate(originals):
        page_index = 1 if index and cross_page else 0
        if index == 0:
            box = [80, 700, 420, 780] if cross_page or columns else [80, 200, 420, 280]
        elif columns:
            box = [560, 80 + (index - 1) * 100, 900, 160 + (index - 1) * 100]
        else:
            top = (80 if cross_page else 300) + (index - 1) * 100
            box = [80, top, 420, top + 80]
        size = geometry[page_index]
        raw_box = [n * size[i % 2] / 1000 for i, n in enumerate(box)]
        span = {"type": "text", "content": text, "bbox": raw_box}
        parent = {"type": "text", "index": index, "bbox": raw_box,
                  "lines": [{"spans": [deepcopy(span)]}]}
        page = pages.setdefault(page_index, {"page_idx": page_index, "page_size": list(size),
            "preproc_blocks": [], "para_blocks": [], "discarded_blocks": []})
        page["preproc_blocks"].append(deepcopy(parent))
        if index:
            parent.update(lines=[], lines_deleted=True)
        page["para_blocks"].append(parent)
        values.append({"type": "text", "text": " ".join(originals) if index == 0 else "",
                       "page_idx": page_index, "bbox": box})
        if index and cross_page:
            span["cross_page"] = True
        source_spans.append(span)
    pages[0]["para_blocks"][0]["lines"] = [{"spans": [s]} for s in source_spans]
    return values, {"pdf_info": list(pages.values())}, geometry


def normalize_case(values, layout, geometry, *, doc_id=None):
    """调用唯一 Adapter；输入全为内存元数据，不读取原件、不使用任务或数据库。"""

    return MinerUResultAdapter().normalize(result_zip(values, layout=layout),
        doc_id=doc_id or uuid4(), file_name="元数据用例.pdf", file_type="pdf",
        geometry=geometry, identity="frozen-input")


@pytest.mark.parametrize("cross_page,columns", [(False, False), (False, True), (True, False)])
def test_restore_verified_merge_preserves_page_bbox_identity_and_all_v1_text(cross_page, columns):
    """同页/跨栏/跨页只分配已有 V1 字符，不跨页排序；重复执行及未合并 ID 均稳定。"""

    values, layout, geometry = merged_case(cross_page=cross_page, columns=columns)
    original_values, original_layout = deepcopy(values), deepcopy(layout)
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert [b.text.strip() for b in parsed.blocks] == ["前段正文", "续段正文"]
    assert "".join(b.text for b in parsed.blocks) == values[0]["text"]
    assert [b.page for b in parsed.blocks] == [1, 2 if cross_page else 1]
    for position, (block, value) in enumerate(zip(parsed.blocks, values), 1):
        assert block.bbox == [n * geometry[value["page_idx"]][i % 2] / 1000
                              for i, n in enumerate(value["bbox"])]
        assert block.block_id == uuid5(doc_id,
            f"mineru-v1:frozen-input:{position}:{value['page_idx']}:text")
        assert block.source == "mineru_cloud" and block.confidence is None
    assert normalize_case(values, layout, geometry, doc_id=doc_id) == parsed
    assert values == original_values and layout == original_layout


def test_multiple_placeholders_and_markdown_escaping_are_not_duplicated_or_rewritten():
    """多个合并来源及美元/星号转义须完整核对，输出保留 V1 原字符和源位置。"""

    values, layout, geometry = merged_case(originals=("预算 $10", "含 * 标点", "末段正文"))
    values[0]["text"] = r"预算 \$10 含 \* 标点 末段正文"
    parsed = normalize_case(values, layout, geometry)
    assert [b.text.strip() for b in parsed.blocks] == [r"预算 \$10", r"含 \* 标点", "末段正文"]
    assert "".join(b.text for b in parsed.blocks) == values[0]["text"]
    assert len({b.block_id for b in parsed.blocks}) == 3


def test_identical_paragraph_text_keeps_distinct_source_blocks():
    """同文但不同位置的两段凭唯一来源分别保留，不能按文本去重丢掉真实原段。"""

    values, layout, geometry = merged_case(originals=("重复正文", "重复正文"))
    parsed = normalize_case(values, layout, geometry)
    assert [b.text.strip() for b in parsed.blocks] == ["重复正文", "重复正文"]
    assert parsed.blocks[0].block_id != parsed.blocks[1].block_id
    assert parsed.blocks[0].bbox != parsed.blocks[1].bbox


def test_unmerged_title_table_formula_keep_content_section_and_business_ids():
    """合并兼容不拆表格/公式、不改标题章节、不压缩原数组位置或其他块 ID。"""

    values, layout, geometry = merged_case(columns=True)
    title = {"type": "text", "text": "章节", "text_level": 1, "page_idx": 0}
    table = {"type": "table", "table_body": "<table><tr><td>42</td></tr></table>",
             "table_caption": ["表题"], "page_idx": 0}
    formula = {"type": "equation", "text": r"$x_0=a/b$", "page_idx": 0}
    values = [title, *values, table, formula]
    doc_id = uuid4()
    parsed = normalize_case(values, layout, geometry, doc_id=doc_id)
    assert len(parsed.blocks) == 5 and all(b.section == "章节" for b in parsed.blocks)
    assert parsed.blocks[-2].text == "表题\n" + table["table_body"]
    assert parsed.blocks[-1].text == formula["text"]
    assert [b.block_type for b in parsed.blocks[-2:]] == ["table", "formula"]
    assert parsed.blocks[-2].block_id == uuid5(doc_id, "mineru-v1:frozen-input:4:0:table")
    assert parsed.blocks[-1].block_id == uuid5(doc_id, "mineru-v1:frozen-input:5:0:formula")


@pytest.mark.parametrize("mode", ["absent_layout", "no_deleted_marker", "empty_table", "empty_formula"])
def test_real_empty_content_is_still_rejected(mode):
    """没有完整合并证据的正文、空表格/公式仍为 EMPTY_BLOCK，不通过跳过伪装成功。"""

    values, layout, geometry = merged_case()
    if mode == "absent_layout":
        layout = None
    elif mode == "no_deleted_marker":
        layout["pdf_info"][0]["para_blocks"][1].pop("lines_deleted")
    else:
        values[1]["type"] = "table" if mode == "empty_table" else "equation"
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "EMPTY_BLOCK"


@pytest.mark.parametrize("damage", [
    "missing_preproc", "duplicate_index", "different_index", "ambiguous_bbox", "wrong_bbox",
    "undeleted_lines", "empty_original", "mixed_span", "multiple_source_spans", "reversed_spans",
    "changed_span", "extra_v1_text", "missing_v1_text", "missing_cross_page", "bool_page",
    "nonadjacent_page", "wrong_cross_page", "title_source", "missing_bbox", "target_deleted",
])
def test_uncertain_merge_fails_atomically_with_safe_reason(damage):
    """损坏/歧义/正文冲突/跨页证据不足明确失败，不猜测关联、不输出私有正文或路径。"""

    values, layout, geometry = merged_case(cross_page=True,
        originals=("private-unit-content", "续段正文"))
    first, second = layout["pdf_info"]
    before = second["preproc_blocks"][0]
    moved = first["para_blocks"][0]["lines"][1]["spans"][0]
    if damage == "missing_preproc":
        second.pop("preproc_blocks")
    elif damage == "duplicate_index":
        second["preproc_blocks"].append(deepcopy(before))
    elif damage == "different_index":
        before["index"] = 9
    elif damage == "ambiguous_bbox":
        duplicate = deepcopy(before)
        duplicate["index"] = 9
        second["preproc_blocks"].append(duplicate)
    elif damage == "wrong_bbox":
        moved["bbox"] = [500, 500, 600, 600]
    elif damage == "undeleted_lines":
        second["para_blocks"][0]["lines"] = deepcopy(before["lines"])
    elif damage == "empty_original":
        before["lines"][0]["spans"][0]["content"] = ""
    elif damage == "mixed_span":
        moved["type"] = "inline_equation"
    elif damage == "multiple_source_spans":
        before["lines"][0]["spans"].append(deepcopy(before["lines"][0]["spans"][0]))
    elif damage == "reversed_spans":
        first["para_blocks"][0]["lines"].reverse()
    elif damage == "changed_span":
        moved["content"] = "不同正文"
    elif damage == "extra_v1_text":
        values[0]["text"] += " 未关联内容"
    elif damage == "missing_v1_text":
        values[0]["text"] = "private-unit-content"
    elif damage == "missing_cross_page":
        moved.pop("cross_page")
    elif damage == "bool_page":
        values[0]["page_idx"] = False
    elif damage == "nonadjacent_page":
        values[1]["page_idx"] = second["page_idx"] = 2
    elif damage == "wrong_cross_page":
        moved["cross_page"] = 1
    elif damage == "title_source":
        values[0]["text_level"] = 1
    elif damage == "missing_bbox":
        values[0].pop("bbox")
    elif damage == "target_deleted":
        first["para_blocks"][0]["lines_deleted"] = True
    with pytest.raises(MinerUError) as error:
        normalize_case(values, layout, geometry)
    assert error.value.code == "MERGE_EVIDENCE"
    expected_reasons = {
        "missing_preproc": "PARENT_LIST", "duplicate_index": "PARENT_INDEX",
        "different_index": "PARENT_INDEX_MISMATCH", "ambiguous_bbox": "PARENT_MATCH",
        "wrong_bbox": "MOVED_BBOX", "undeleted_lines": "SOURCE_NOT_DELETED",
        "empty_original": "SPAN_TEXT", "mixed_span": "SPAN_TYPE",
        "multiple_source_spans": "TARGET_SPAN_COUNT", "reversed_spans": "MOVED_TEXT",
        "changed_span": "MOVED_TEXT", "extra_v1_text": "TEXT_TRAILING",
        "missing_v1_text": "TEXT_CHARACTER", "missing_cross_page": "MOVED_CROSS_PAGE",
        "bool_page": "LAYOUT_PAGE", "nonadjacent_page": "SOURCE_PAGE",
        "wrong_cross_page": "MOVED_CROSS_PAGE", "title_source": "TEXT_LEVEL",
        "missing_bbox": "ITEM_BBOX", "target_deleted": "TARGET_DELETED",
    }
    assert error.value.diagnostic["reason"] == expected_reasons[damage]
    assert error.value.diagnostic["stage"].startswith("adapter.")
    assert not error.value.retryable
    assert "private-unit-content" not in str(error.value)
    assert "http" not in str(error.value) and "C:\\" not in str(error.value)


def test_success_and_failure_logs_only_identifiers_and_counts(monkeypatch):
    """兼容过程和拒绝原因只记安全摘要，不记录 V1/layout 原文或签名资源地址。"""

    from app.services.mineru import adapter
    events = []

    def collect(event, **fields):
        """捕获白名单事件，不写运行日志。"""

        events.append((event, fields))

    monkeypatch.setattr(adapter, "log_event", collect)
    values, layout, geometry = merged_case(originals=("private-unit-content", "续段"))
    normalize_case(values, layout, geometry)
    assert events[-1][0] == "mineru.merge_restored" and events[-1][1]["item_count"] == 1
    values[0]["text"] += " 未关联内容"
    with pytest.raises(MinerUError):
        normalize_case(values, layout, geometry)
    assert events[-1][0] == "mineru.merge_rejected"
    assert events[-1][1]["error_code"] == "MERGE_EVIDENCE"
    assert events[-1][1]["step"] == "TEXT_TRAILING"
    assert events[-1][1]["page"] == 1 and events[-1][1]["item_index"] == 1
    assert "private-unit-content" not in str(events)


def test_merge_limits_and_missing_characters_are_not_relaxed():
    """超过单组合并预算或正文字符不一致仍拒绝，不模糊匹配或吞掉额外字符。"""

    with pytest.raises(MinerUError, match="来源证据"):
        merged_text_slices("a" * (1024**2 + 1), ["a"])
    with pytest.raises(MinerUError):
        merged_text_slices("abcX def", ["abc", "def"])


@pytest.mark.parametrize("change,reason", [
    ("source_lines", "LINES_MISSING"), ("line_schema", "LINE_SCHEMA"),
    ("source_multiline", "TARGET_SPAN_COUNT"), ("source_span_bbox", "SOURCE_SPAN_BBOX"),
    ("source_bbox_coverage", "SOURCE_BBOX_COVERAGE"), ("target_span_count", "TARGET_SPAN_COUNT"),
    ("original_cross_page", "SOURCE_CROSS_PAGE"), ("parent_limit", "PARENT_LIMIT"),
    ("page_parent_limit", "PAGE_PARENT_LIMIT"), ("placeholder_match", "PLACEHOLDER_MATCH"),
    ("group_no_target", "GROUP_NO_TARGET"), ("group_limit", "GROUP_LIMIT"),
    ("target_text", "TARGET_TEXT"),
])
def test_merge_structural_subreasons_remain_strict_and_distinct(change, reason):
    """单侧多出Span、父块预算和占位冲突继续拒绝；合法重复不再按值误判。"""
    from app.services.mineru.adapter import restore_merged_text
    values, layout, _ = merged_case()
    page = layout["pdf_info"][0]
    source = page["preproc_blocks"][1]
    target = page["para_blocks"][0]
    if change == "source_lines":
        source["lines"] = []
    elif change == "line_schema":
        source["lines"] = [None]
    elif change == "source_multiline":
        source["lines"].append(deepcopy(source["lines"][0]))
    elif change == "source_span_bbox":
        source["lines"][0]["spans"][0].pop("bbox")
    elif change == "source_bbox_coverage":
        source["lines"][0]["spans"][0]["bbox"][0] += 20
    elif change == "target_span_count":
        target["lines"].pop()
    elif change == "original_cross_page":
        source["lines"][0]["spans"][0]["cross_page"] = True
    elif change == "parent_limit":
        page["preproc_blocks"] *= 300
    elif change == "page_parent_limit":
        page["para_blocks"] *= 300
    elif change == "placeholder_match":
        page["para_blocks"].append(deepcopy(page["para_blocks"][1]))
    elif change == "group_no_target":
        values = values[1:]
    elif change == "group_limit":
        values.extend(deepcopy(values[1]) for _ in range(64))
    else:
        values[0]["type"] = "table"
    before = deepcopy(values), deepcopy(layout)
    with pytest.raises(MinerUError) as error:
        restore_merged_text(values, {0: page})
    assert error.value.code == "MERGE_EVIDENCE" and error.value.diagnostic["reason"] == reason
    assert (values, layout) == before


def test_diagnostic_inputs_never_export_arbitrary_text_coordinates_or_booleans():
    """未知子码及字段不透传，内部定位仅限整数；错误仍为永久MERGE_EVIDENCE。"""
    from app.services.mineru.adapter import merge_evidence_error
    error = merge_evidence_error("PRIVATE_TOKEN", page_idx=True, item_index=7, observed=-1,
        expected=10**9, bbox=[1, 2, 3, 4], text="PRIVATE_BODY", url="PRIVATE_URL")
    assert error.diagnostic == {"reason": "UNCLASSIFIED", "stage": "adapter.restore_merged_text", "item_index": 7}
    assert error.code == "MERGE_EVIDENCE" and not error.retryable
    assert "PRIVATE" not in str(error) and "PRIVATE" not in str(error.diagnostic)


def test_group_diagnostic_keeps_original_array_position_on_later_physical_page():
    """大型PDF前置199页不影响错误定位；原数组序号和局部页均不靠坐标重排推断。"""
    from app.services.mineru.adapter import restore_merged_text
    values, layout, _ = merged_case()
    page = layout["pdf_info"][0]
    page["page_idx"] = 199
    for value in values:
        value["page_idx"] = 199
    page["preproc_blocks"][1]["lines"][0]["spans"].append(
        deepcopy(page["preproc_blocks"][1]["lines"][0]["spans"][0]))
    prefix = [{"type": "text", "text": "非合并元数据", "page_idx": index} for index in range(199)]
    with pytest.raises(MinerUError) as error:
        restore_merged_text(prefix + values, {199: page})
    assert error.value.diagnostic == {"reason": "TARGET_SPAN_COUNT", "stage": "adapter.restore_merged_text",
        "observed": 2, "expected": 3, "page_idx": 199, "item_index": 200}
