"""v4基础提取/可选增强/覆盖质量契约；纯内存Mock，由用户执行，不发网络或加载模型。"""

from copy import deepcopy
from io import BytesIO, StringIO
import logging
import stat
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest

from app.services.mineru.adapter import MinerUResultAdapter, merge_evidence_error
from app.services.mineru.errors import MinerUError
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from mineru_samples import result_zip
from test_mineru_merge import merged_case
from test_mineru_table_merge import table_case


def convert(values, layout=None, *, geometry=None, doc_id=None):
    """只调用公共v4入口，固定页面元数据不读取任何PDF或数据库。"""
    return MinerUResultAdapter().normalize(result_zip(values, layout=layout), doc_id=doc_id or uuid4(),
        file_name="case.pdf", file_type="pdf", geometry={0: (600, 800)} if geometry is None else geometry,
        identity="v4-mock")


@pytest.fixture(autouse=True)
def no_cloud(monkeypatch):
    """任何测试一旦触发HTTP就直接失败，不依赖真实Token/服务或收费接口。"""
    import httpx
    def forbidden(*args, **kwargs):
        """拒绝全部网络传输。"""
        raise AssertionError("本用例禁止网络")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def test_plain_v1_keeps_original_text_order_ids_and_provenance():
    """简单V1无layout也保留正文、原页、稳定ID和可追溯来源，重复执行不变。"""
    values = [{"type": "text", "page_idx": 0, "bbox": [10, 10, 900, 100], "text": "原文 123.45"}]
    identity, snapshot = uuid4(), deepcopy(values)
    first = convert(values, doc_id=identity)
    assert first == convert(values, doc_id=identity)
    assert first.blocks[0].text == values[0]["text"] and first.blocks[0].page == 1
    assert first.blocks[0].provenance.page_reliable
    assert first.quality.content == "COMPLETE" and values == snapshot


@pytest.mark.parametrize("reason", ["TARGET_TEXT", "BOUNDARY_TEXT", "BOUNDARY_POSITION", "LINE_SPAN_COUNT",
                                  "TABLE_BODY", "TABLE_ROWS", "TABLE_EXTRAS", "TABLE_LIMIT"])
def test_structure_enhancement_failure_does_not_erase_reliable_base(monkeypatch, reason):
    """所有局部结构子码都退回逐页原内容，不吞异常后返回空块或丢失公式。"""
    values, layout, geometry = merged_case(cross_page=True, originals=("前段 $x+y$", "后段正文"))
    def broken(*args, **kwargs):
        """仅模拟结构增强失败，基础提取仍消费真实形状的内存ZIP。"""
        raise merge_evidence_error(reason)
    monkeypatch.setattr(MinerUResultAdapter, "normalize_strict", broken)
    parsed = convert(values, layout, geometry=geometry)
    assert [b.page for b in parsed.blocks] == [1, 2]
    assert [b.text for b in parsed.blocks] == ["前段 $x+y$", "后段正文"]
    assert parsed.quality.content == "COMPLETE" and parsed.quality.structure == "DEGRADED"
    assert reason in parsed.quality.warnings
    assert all(b.provenance.origin == "layout_preproc" for b in parsed.blocks)


def test_tables_use_original_page_html_and_keep_caption_absent_from_v1():
    """V1合并结果缺附注不再丢弃可靠layout原文；两页HTML、数值和caption均有原始指针。"""
    items, pages = table_case(footnote=False)
    caption = {"type": "table_caption", "index": 3, "bbox": [48., 260., 552., 280.],
               "lines": [{"spans": [{"type": "text", "content": "真实后页附注", "bbox": [48., 260., 552., 280.]}]}]}
    pages[1]["preproc_blocks"][0]["blocks"].append(caption)
    snapshot = deepcopy(items), deepcopy(pages)
    parsed = convert(items, {"pdf_info": list(pages.values())}, geometry={0: (600, 800), 1: (600, 800)})
    tables = [b for b in parsed.blocks if b.block_type == "table"]
    assert [b.page for b in tables] == [1, 2]
    assert "真实后页附注" in tables[1].text and "真实后页附注" not in tables[0].text
    assert "B" in tables[1].text and "20" in tables[1].text
    assert parsed.quality.content == "COMPLETE" and parsed.quality.structure == "DEGRADED"
    assert (items, pages) == snapshot


def test_mixed_spans_keep_text_formula_and_occurrence_order(monkeypatch):
    """多个Span和行内公式按原序保留，不因无法重建合并而删除公式或重复去重。"""
    values, layout, geometry = merged_case(originals=("甲", "乙"))
    spans = layout["pdf_info"][0]["preproc_blocks"][0]["lines"][0]["spans"]
    spans.extend([{"type": "inline_equation", "content": "x+y"}, {"type": "text", "content": "尾部"}])
    parsed = convert(values, layout, geometry=geometry)
    assert parsed.blocks[0].text == "甲$x+y$尾部" and parsed.blocks[1].text == "乙"
    assert parsed.quality.structure == "DEGRADED"


@pytest.mark.parametrize("bbox", [None, [20, 20, 10, 10], [float("nan"), 0, 10, 20]])
def test_unavailable_bbox_never_blocks_known_page_or_invents_coordinates(bbox):
    """位置未知设NULL，可靠原页与正文仍保留，不填默认矩形。"""
    parsed = convert([{"type": "text", "page_idx": 0, "bbox": bbox, "text": "可用正文"}])
    assert parsed.blocks[0].bbox is None and parsed.blocks[0].page == 1
    assert parsed.quality.evidence == "PARTIAL" and parsed.quality.content == "COMPLETE"


@pytest.mark.parametrize("page_fields", [{}, {"page_idx": None}, {"page_idx": -1}, {"page_idx": True},
                                       {"page_idx": "1"}, {"page_idx": 99}],
                         ids=["missing", "null", "negative", "boolean", "string", "out-of-range"])
def test_invalid_local_page_is_quarantined_and_other_blocks_remain_usable(page_fields):
    """缺失或无效页码只保留隔离原文，可靠邻块仍可用且不伪造页码或完整质量。"""
    parsed = convert([{"type": "text", "page_idx": 0, "text": "可靠正文"},
                      {"type": "text", "text": "未确认来源正文", **page_fields}])
    assert [b.text for b in parsed.blocks] == ["可靠正文"]
    assert parsed.quarantined_content[0]["page"] is None
    assert parsed.quarantined_content[0]["text"] == "未确认来源正文"
    assert parsed.quality.content == "PARTIAL" and parsed.quality.evidence == "PARTIAL"


def test_globally_unmapped_or_empty_content_remains_fatal():
    """归档通过后，整体缺失/无效页码或全空仍失败，不能发布空结果或制造业务成功。"""
    for values, code in [([{"type": "text", "page_idx": 99, "text": "无可靠页"}], "PAGE"),
                         ([{"type": "text", "text": "缺失页码正文"}], "PAGE"),
                         ([{"type": "text"}], "NO_TEXT"),
                         ([{"type": "text", "page_idx": 0, "text": ""}], "NO_TEXT")]:
        with pytest.raises(MinerUError) as error:
            convert(values)
        assert error.value.code == code


def test_required_visual_only_page_is_partial_not_complete_ready():
    """无OCR图片不能伪造正文或解除原required页；可靠正文可继续索引但覆盖必须PARTIAL。"""
    from app.parsers.routing.contracts import PageSignals, StructuralSignals, RouteDecision
    from app.parsers.routing.guard import guard_result, CompletenessError
    parsed = convert([{"type": "text", "page_idx": 0, "text": "已解析正文"},
                      {"type": "image", "page_idx": 1, "img_path": "images/raw.jpg"}],
                     geometry={0: (600, 800), 1: (600, 800)})
    decision = RouteDecision(parse_mode="mineru", file_type="pdf", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals(page_count=2, pages=tuple(
            PageSignals(page=p, text_chars=10, image_count=0, image_area_ratio=0, drawing_count=0,
                        annotation_count=0, required=True) for p in (1, 2))))
    guard_result(parsed, decision)
    assert parsed.quality.content == "PARTIAL" and parsed.quality.missing_pages == [2]
    legacy = parsed.model_copy(update={"quality": None})
    with pytest.raises(CompletenessError):
        guard_result(legacy, decision)


@pytest.mark.parametrize("damage", ["crc", "path", "symlink"])
def test_zip_integrity_and_security_errors_remain_fatal(damage):
    """CRC、路径穿越及符号链接不降级，不发布任何基础块。"""
    values = [{"type": "text", "page_idx": 0, "text": "PRIVATE_ZIP"}]
    archive = result_zip(values)
    if damage == "crc":
        archive = archive.replace(b"PRIVATE_ZIP", b"CHANGED_ZIP", 1)
    else:
        stream = BytesIO()
        with ZipFile(BytesIO(archive)) as original, ZipFile(stream, "w") as target:
            for info in original.infolist():
                target.writestr(info.filename, original.read(info))
            info = ZipInfo("../unsafe" if damage == "path" else "linked")
            if damage == "symlink":
                info.create_system, info.external_attr = 3, (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, "target")
        archive = stream.getvalue()
    with pytest.raises(MinerUError) as error:
        MinerUResultAdapter().normalize(archive, doc_id=uuid4(), file_name="case.pdf", file_type="pdf",
                                       geometry={0: (600, 800)}, identity="safe")
    assert error.value.code in {"ZIP_INVALID", "ZIP_PATH"}


def test_local_upgrade_keeps_frozen_cloud_identity_without_provider():
    """v3原Job只新增v4本地配方，不修改原Batch/云参数/缓存键或调用Provider。"""
    from app.services.mineru.jobs import upgrade_local_adapter, check_normalizer_recipe
    frozen = {"normalizer_version": "mineru-adapter-v3", "file_hash": "a" * 64}
    job = SimpleNamespace(parse_options={"_cache_recipe": deepcopy(frozen), "_cache_key": "old"},
                          batch_id="original", data_id="original-data")
    upgrade_local_adapter(job)
    check_normalizer_recipe(job)
    assert job.parse_options["_cache_recipe"] == frozen and job.parse_options["_cache_key"] == "old"
    assert job.parse_options["_adapter_recipe"]["normalizer_version"] == "mineru-adapter-v4"
    assert job.parse_options["_local_replay_only"] and job.batch_id == "original"


@pytest.mark.parametrize("content,expected", [("COMPLETE", "READY"), ("PARTIAL", "PARTIAL_READY")])
def test_index_terminal_tracks_coverage_and_does_not_fake_full_ready(monkeypatch, content, expected):
    """复用真实Index状态推进代码，仅向量/SQL边界Stub；部分覆盖不升级完整READY。"""
    from app.services import indexing as tool
    task, doc_id = uuid4(), uuid4()
    document = SimpleNamespace(doc_id=doc_id, kb_id=uuid4(), status="INDEXING", index_task_id=task,
                               embedding_task_id=uuid4(), parse_quality={"content": content})
    rows = [SimpleNamespace(chunk_id=uuid4())]
    config = {"source_hash": "fixed", "embedding_result": {"chunk_count": 1}}
    session = SimpleNamespace(scalar=lambda *a: document, execute=lambda *a: SimpleNamespace(all=lambda: []), flush=lambda: None)
    monkeypatch.setattr(tool, "checked_index_config", lambda *a: config)
    monkeypatch.setattr(tool, "require_vector_index", lambda *a: None)
    monkeypatch.setattr(tool, "read_chunks", lambda *a: rows)
    monkeypatch.setattr(tool, "chunks_hash", lambda *a: "fixed")
    monkeypatch.setattr(tool, "checked_manifest", lambda *a: (object(), {"model": {}, "vectors_sha256": "a" * 64}))
    monkeypatch.setattr(tool, "vector_batches", lambda *a: iter([[]]))
    monkeypatch.setattr(tool, "write_batch", lambda *a: None)
    monkeypatch.setattr(tool, "require_complete_vectors", lambda *a: None)
    result = tool.run_index_step(session, doc_id, task)
    assert result["document_status"] == expected and document.status == expected


def test_business_logs_never_include_base_text_formula_or_private_paths(monkeypatch):
    """仅日志状态/数量/ID可见，保留正文不等于把正文写入日志。"""
    from app.core import business_logging as logs
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logs.ReadableFormatter("mineru-diagnostic-cli"))
    monkeypatch.setattr(logs.LOGGER, "handlers", [handler])
    monkeypatch.setattr(logs.LOGGER, "disabled", False)
    monkeypatch.setattr(logs.LOGGER, "level", logging.INFO)
    convert([{"type": "text", "page_idx": 0, "text": "PRIVATE_BODY $PRIVATE_FORMULA$"}])
    assert "mineru.adapter_content" in output.getvalue()
    assert "PRIVATE" not in output.getvalue()


def visual_archive(values, *, layout=None, members=None):
    """为Mock增加可核对字节的原图成员；不生成真实图片、不执行OCR或联网。"""
    stream = BytesIO()
    with ZipFile(BytesIO(result_zip(values, layout=layout))) as source, ZipFile(stream, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        for name, content in (members or {}).items():
            target.writestr("result/" + name, content)
    return stream.getvalue()


@pytest.mark.parametrize("kind,expected", [("image", "COMPLETE"), ("chart", "PARTIAL")])
def test_archived_visual_on_text_page_distinguishes_image_from_unread_chart(kind, expected):
    """普通原图保全不等于缺文；图表无结构化文字仍待审，不把图注冒充图表正文。"""
    import hashlib
    values = [{"type": "text", "page_idx": 0, "text": "可靠正文"},
              {"type": kind, "page_idx": 0, "img_path": "images/source.jpg", "image_caption": ["原始图注"]}]
    raw = b"mock-visual-bytes-not-decoded"
    parsed = MinerUResultAdapter().normalize(visual_archive(values, members={"images/source.jpg": raw}),
        doc_id=uuid4(), file_name="case.pdf", file_type="pdf", geometry={0: (600, 800)}, identity="mock")
    assert parsed.quality.content == expected and parsed.quality.visual_review_pages == [1]
    assert parsed.quality.visual_assets[0].sha256 == hashlib.sha256(raw).hexdigest()
    assert parsed.quality.visual_assets[0].kind == kind
    assert [block.text for block in parsed.blocks] == ["可靠正文", "原始图注"]
    assert all("source.jpg" not in block.text for block in parsed.blocks)


@pytest.mark.parametrize("reference", ["images/missing.jpg", "../source.jpg", "https://private/source.jpg"])
def test_missing_or_unsafe_visual_asset_stays_partial(reference):
    """未保全原图及外部/越界引用不能借同页文字冒充完整，也不自动抓取图片。"""
    parsed = convert([{"type": "text", "page_idx": 0, "text": "可靠正文"},
                      {"type": "image", "page_idx": 0, "img_path": reference}])
    assert parsed.quality.content == "PARTIAL" and parsed.quality.visual_assets == []
    assert "VISUAL_ASSET_UNAVAILABLE" in parsed.quality.warnings


def test_ambiguous_image_reference_is_not_matched_by_basename():
    """两个精确候选都存在时拒绝选择，不凭同名图片猜测来源或宣布完整。"""
    values = [{"type": "text", "page_idx": 0, "text": "正文"},
              {"type": "image", "page_idx": 0, "img_path": "source.jpg"}]
    parsed = MinerUResultAdapter().normalize(visual_archive(values,
        members={"source.jpg": b"first", "images/source.jpg": b"second"}),
        doc_id=uuid4(), file_name="case.pdf", file_type="pdf", geometry={0: (600, 800)}, identity="mock")
    assert parsed.quality.content == "PARTIAL" and parsed.quality.visual_assets == []


def test_visual_only_page_keeps_original_asset_but_never_creates_text_or_page_hit():
    """整页原图可定位回原ZIP，仍明确缺少可检索文字，不造Block或解除required。"""
    from app.parsers.routing.contracts import PageSignals, RouteDecision, StructuralSignals
    from app.parsers.routing.guard import guard_result
    from scripts.mineru_merge_diagnostic import content_summary
    values = [{"type": "text", "page_idx": 0, "text": "第一页"},
              {"type": "image", "page_idx": 1, "img_path": "images/source.jpg"}]
    parsed = MinerUResultAdapter().normalize(visual_archive(values, members={"images/source.jpg": b"mock"}),
        doc_id=uuid4(), file_name="case.pdf", file_type="pdf",
        geometry={0: (600, 800), 1: (600, 800)}, identity="mock")
    route = RouteDecision(parse_mode="mineru", file_type="pdf", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals(page_count=2, pages=tuple(
            PageSignals(page=page, text_chars=10, image_count=0, image_area_ratio=0, drawing_count=0,
                        annotation_count=0, required=True) for page in (1, 2))))
    guard_result(parsed, route)
    assert parsed.quality.content == "PARTIAL" and parsed.quality.missing_pages == [2]
    assert [block.page for block in parsed.blocks] == [1]
    assert parsed.quality.visual_assets[0].page == 2
    assert content_summary(parsed)["coverage_gaps"] == [{"page": 2, "reason": "VISUAL_ONLY_PAGE",
        "archived_visual_count": 1, "text_recoverable_from_current_zip": False, "manual_review_required": True}]


@pytest.mark.parametrize("layer,origin", [("base", "layout_discarded"), ("enhanced", "verified_enhancement")])
def test_discarded_text_is_retained_when_page_has_no_preproc_body(layer, origin):
    """分别核对基础及增强来源；空preproc不丢discarded文字，增强成功后保留真实来源类型。"""
    from app.services.mineru.content import BasicContent, canonical_hash
    import hashlib

    block = {"type": "header", "index": 0, "bbox": [10, 10, 100, 30],
             "lines": [{"spans": [{"type": "text", "content": "保留原始页眉"}]}]}
    values = [{"type": "text", "page_idx": 0, "text": "保留原始页眉"}]
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [600, 800],
        "preproc_blocks": [], "discarded_blocks": [block], "para_blocks": []}]}
    archive, doc_id, geometry = result_zip(values, layout=layout), uuid4(), {0: (600, 800)}
    if layer == "base":
        parsed = BasicContent(archive, doc_id, "case.pdf", "pdf", geometry, "v4-mock").extract()
    else:
        parsed = MinerUResultAdapter().normalize(archive, doc_id=doc_id, file_name="case.pdf",
            file_type="pdf", geometry=geometry, identity="v4-mock")
    assert [b.text for b in parsed.blocks] == ["保留原始页眉"]
    assert parsed.blocks[0].page == 1 and parsed.blocks[0].provenance.origin == origin
    assert parsed.quality.enhanced is (layer == "enhanced")
    assert parsed.blocks[0].provenance.pointer == (
        "layout/page/0/discarded_blocks/0" if layer == "base" else "enhanced/0")
    assert parsed.blocks[0].provenance.raw_sha256 == (
        canonical_hash(block) if layer == "base" else hashlib.sha256(archive).hexdigest())


def test_nested_visual_span_keeps_exact_pointer_and_original_page():
    """layout纯图片名按唯一images成员核对，保留原Span指针而非按文件名猜测页面。"""
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [600, 800], "preproc_blocks": [
        {"type": "text", "index": 0, "lines": [{"spans": [{"type": "text", "content": "正文"}]}]},
        {"type": "image", "index": 1, "blocks": [{"type": "image_body", "lines": [
            {"spans": [{"type": "image", "image_path": "source.jpg"}]}]}]}]}]}
    parsed = MinerUResultAdapter().normalize(visual_archive(
        [{"type": "text", "page_idx": 0, "text": "正文"}], layout=layout,
        members={"images/source.jpg": b"mock"}), doc_id=uuid4(), file_name="case.pdf", file_type="pdf",
        geometry={0: (600, 800)}, identity="mock")
    assert parsed.quality.content == "COMPLETE" and parsed.quality.visual_only_count == 1
    assert parsed.quality.visual_assets[0].pointer.endswith("preproc_blocks/1/blocks/0/lines/0/spans/0")


@pytest.mark.parametrize("kind", ["header", "footer", "page_number", "title"])
def test_page_chrome_does_not_make_full_page_visual_semantically_complete(kind):
    """页眉/页脚/页码或标题不等于图片正文，防止扫描页仅识别页码便被升级READY。"""
    values = [{"type": kind, "page_idx": 0, "text": "原始页面标识"},
              {"type": "image", "page_idx": 0, "img_path": "images/source.jpg"}]
    parsed = MinerUResultAdapter().normalize(visual_archive(values, members={"images/source.jpg": b"mock"}),
        doc_id=uuid4(), file_name="case.pdf", file_type="pdf", geometry={0: (600, 800)}, identity="mock")
    assert parsed.quality.content == "PARTIAL" and "VISUAL_ONLY_PAGES" in parsed.quality.warnings
