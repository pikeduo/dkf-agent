"""Hybrid 纯契约/Stub 测试：仅由用户运行，不创建 Office/PDF/图片验收文档。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from lxml import etree

from app.core.business_logging import safe_fields
from app.core.config import Settings
from app.parsers import hybrid
from app.parsers.common import make_block
from app.parsers.hybrid import (
    VERSION, BundleManifest, HybridPlan, HybridPolicy, HybridPreflightError, VisualEntry,
    anchor_coordinates, chart_text, digest, image_dimensions, is_solid, merge_hybrid,
)
from app.parsers.routing.contracts import Inspection, StructuralSignals
from app.parsers.routing.router import decide_route
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.hybrid_processing import metrics


def metadata_plan(doc_id=None, entries=None, page_count=2):
    """构造纯 Python 元数据主结果；不打开 Parser，不写 Office 或 PDF 文件。"""

    doc_id = doc_id or uuid4()
    native = ParsedDocument(doc_id=doc_id, file_name="source.xlsx", file_type="xlsx", blocks=[
        make_block(doc_id, 1, page=1, section="第一表", block_type="table", text="项目\t公式\n收入\t=B2*2"),
        make_block(doc_id, 2, page=1, section="第二表", block_type="table", text="项目\t金额\n成本\t20"),
    ])
    if entries is None:
        entries = [
            VisualEntry(visual_id=uuid5(doc_id, "image1"), source_doc_id=doc_id,
                source_type="embedded_image", source_section="第一表", sheet_index=1,
                source_anchor=(12, 0, 0, 0), position=1, content_hash="a"*64,
                media_type="image/png", width=100, height=100, state="pending", bundle_page=1),
            VisualEntry(visual_id=uuid5(doc_id, "image2"), source_doc_id=doc_id,
                source_type="embedded_image", source_section="第二表", sheet_index=2,
                source_anchor=(0, 0, 0, 0), position=1, content_hash="b"*64,
                media_type="image/png", width=100, height=100, state="pending", bundle_page=2),
        ]
    manifest = BundleManifest(policy=HybridPolicy(), sheet_names=["第一表", "第二表"], entries=entries,
        bundle_page_count=page_count, bundle_hash=digest(b"stub-bundle"),
        native_hash=digest(native.model_dump_json().encode()))
    return HybridPlan(native, manifest, b"stub-bundle", {})


def visual_result(plan, pages=(1, 2), kind="text"):
    """模拟 Adapter 输出，不下载 ZIP、不制造 OCR 结果来声称真实验收成功。"""

    return ParsedDocument(doc_id=plan.native.doc_id, file_name=plan.native.file_name,
        file_type="pdf", blocks=[ParsedBlock(block_id=uuid4(), page=page, block_type=kind,
            text=f"Stub 视觉页 {page}", source="mineru_cloud", bbox=[0.0, 0.0, 10.0, 20.0])
            for page in pages])


def test_hybrid_router_default_and_explicit_scope():
    """默认自动尝试 XLSX Hybrid；管理员可覆盖或关闭增强，但不恢复 Review。"""

    inspection = Inspection(risks=("XLSX_HAS_EMBEDDED_IMAGE",), signals=StructuralSignals(image_count=1))
    assert Settings(_env_file=None).hybrid_xlsx_enabled
    assert decide_route("xlsx", "auto", inspection).route_strategy == "hybrid_bundle"
    assert decide_route("xlsx", "auto", inspection, hybrid_enabled=False).route_strategy == "mineru_whole"
    assert decide_route("xlsx", "auto", inspection, hybrid_enabled=True).route_strategy == "hybrid_bundle"
    assert decide_route("xlsx", "native", inspection, hybrid_enabled=True).route_strategy == "native"
    assert decide_route("xlsx", "mineru", inspection, hybrid_enabled=True).route_strategy == "mineru_whole"
    assert decide_route("xlsx", "auto", Inspection(), hybrid_enabled=True).route_strategy == "native"
    assert decide_route("xlsx", "auto", Inspection(risks=("XLSX_HAS_VML",)), hybrid_enabled=True).route_strategy == "hybrid_bundle"
    assert decide_route("pdf", "auto", Inspection(risks=("PDF_TEXT_IMAGE_MIXED",)), hybrid_enabled=True).route_strategy == "mineru_whole"


def test_merge_keeps_formulas_sheet_order_and_stable_ids():
    """主表及原公式不变、页映射回 XLSX 逻辑页，重复合并身份稳定。"""

    plan = metadata_plan()
    cloud = visual_result(plan)
    original = deepcopy(plan)
    result = merge_hybrid(plan, cloud)
    retry = merge_hybrid(original, cloud)
    assert result == retry
    assert result.blocks[0] == plan.native.blocks[0]
    assert "=B2*2" in result.blocks[0].text
    assert [b.section for b in result.blocks] == ["第一表", "第一表", "第二表", "第二表"]
    assert all(b.page == 1 and b.bbox is None for b in result.blocks)
    assert [b.source for b in result.blocks] == ["native_parser", "mineru_cloud", "native_parser", "mineru_cloud"]


def test_duplicate_hash_reuses_page_at_two_anchors_and_dedups_text():
    """相同图片只使用一页，在多个 Anchor 回插；只消除同候选内同类型精确重复。"""

    plan = metadata_plan(page_count=1)
    first, repeated = plan.manifest.entries
    repeated.content_hash = first.content_hash
    repeated.reused_from, repeated.bundle_page, repeated.state = first.visual_id, 1, "reused"
    cloud = visual_result(plan, (1, 1))
    cloud.blocks[1].text = cloud.blocks[0].text
    result = merge_hybrid(plan, cloud)
    visuals = [b for b in result.blocks if b.source == "mineru_cloud"]
    assert len(visuals) == 2 and visuals[0].text == visuals[1].text
    assert visuals[0].block_id != visuals[1].block_id
    assert metrics(plan.manifest)["hybrid_visual_dedup_count"] == 1
    assert metrics(plan.manifest)["hybrid_bundle_page_count"] == 1


@pytest.mark.parametrize("kind", ["table", "formula"])
def test_structural_visual_blocks_remain_whole(kind):
    """表格和公式不拆分，Cloud 原文、类型和未知置信度保留。"""

    plan = metadata_plan()
    result = merge_hybrid(plan, visual_result(plan, kind=kind))
    cloud_blocks = [b for b in result.blocks if b.source == "mineru_cloud"]
    assert all(b.block_type == kind and b.confidence is None for b in cloud_blocks)
    assert len(cloud_blocks) == 2


@pytest.mark.parametrize("pages", [(1,), (1, 2, 3)])
def test_missing_or_extra_visual_page_rejects_entire_merge(pages):
    """缺少或多出 Bundle 页必须失败，不能以 Native 半结果通过。"""

    plan = metadata_plan()
    with pytest.raises(HybridPreflightError):
        merge_hybrid(plan, visual_result(plan, pages))


def test_source_identity_manifest_and_native_truth_tamper_rejected():
    """错误源身份、未知 Sheet 或主公式被改写都拒绝合并。"""

    for mutation in ("identity", "sheet", "formula"):
        plan = metadata_plan()
        cloud = visual_result(plan)
        if mutation == "identity":
            cloud.doc_id = uuid4()
        elif mutation == "sheet":
            plan.manifest.entries[0].source_section = "未知表"
        else:
            plan.native.blocks[0].text = "供应商计算值替代公式"
        with pytest.raises(HybridPreflightError):
            merge_hybrid(plan, cloud)


def test_chart_and_filtered_only_no_cloud_merge():
    """仅可结构化 Chart 与纯色装饰图时零云页，也可输出完整主结果。"""

    plan = metadata_plan(page_count=0)
    entry = plan.manifest.entries[0]
    block = make_block(plan.native.doc_id, 30, page=1, block_type="text", section="第一表", text="Chart 原始引用")
    entry.source_type, entry.state, entry.bundle_page = "chart", "parsed", None
    entry.content_hash, entry.block_ids = digest(block.text.encode()), [block.block_id]
    plan.charts[entry.visual_id] = block
    other = plan.manifest.entries[1]
    other.state, other.filter_reason, other.bundle_page = "filtered", "solid", None
    result = merge_hybrid(plan, None)
    assert len(result.blocks) == 3
    assert all(b.source == "native_parser" for b in result.blocks)


def test_chart_structured_references_never_eval():
    """基础图表保留类别与原式，不使用缓存值计算公式，复杂视觉结构明确失败。"""

    tree = etree.fromstring(b'<chartSpace><chart><title><t>Revenue</t></title><plotArea><barChart><ser><tx><v>S1</v></tx><cat><strRef><f>Sheet1!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet1!$B$1:$B$2</f></numRef></val></ser></barChart></plotArea></chart></chartSpace>')
    text = chart_text(tree, {"Sheet1": {"A1": "Q1", "A2": "Q2", "B1": "=C1*2", "B2": "10"}})
    assert "=C1*2" in text and "Q1" in text and "S1" in text
    etree.SubElement(tree, "trendline")
    with pytest.raises(HybridPreflightError):
        chart_text(tree, {})


def test_anchor_bounds_and_unknown_absolute_anchor():
    """保留零基行列/偏移，绝对位置没有可靠单元格语义时拒绝局部增强。"""

    node = etree.fromstring(f'<oneCellAnchor xmlns="{hybrid.XDR}"><from><row>2</row><col>3</col><rowOff>0</rowOff><colOff>0</colOff></from></oneCellAnchor>'.encode())
    assert anchor_coordinates(node) == (2, 3, 0, 0)
    with pytest.raises(HybridPreflightError):
        anchor_coordinates(etree.fromstring(f'<absoluteAnchor xmlns="{hybrid.XDR}"/>'.encode()))


def test_solid_filter_does_not_discard_black_white_text():
    """纯色可过滤，含黑白不同像素的扫描文字不可误当纯色背景。"""

    assert is_solid(SimpleNamespace(samples=b"\xff"*4, n=1, width=2, height=2))
    assert not is_solid(SimpleNamespace(samples=b"\x00\xff\xff\x00", n=1, width=2, height=2))


def test_extractor_stub_dedup_filter_limits_and_single_bundle(monkeypatch):
    """纯 Stub 包关系覆盖多图、复用和超限；不构造 ZIP/XLSX/PDF/图片文件。"""

    doc_id = uuid4()
    base = metadata_plan(doc_id)
    parts = {
        "xl/workbook.xml": f'<workbook xmlns:r="{hybrid.R}"><sheets><sheet name="第一表" r:id="s1"/><sheet name="第二表" r:id="s2"/></sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": b'<Relationships><Relationship Id="s1" Target="worksheets/s1.xml"/><Relationship Id="s2" Target="worksheets/s2.xml"/></Relationships>',
        "xl/worksheets/s1.xml": f'<worksheet xmlns:r="{hybrid.R}"><drawing r:id="d1"/></worksheet>'.encode(),
        "xl/worksheets/s2.xml": b'<worksheet/>',
        "xl/worksheets/_rels/s1.xml.rels": b'<Relationships><Relationship Id="d1" Target="../drawings/d1.xml"/></Relationships>',
        "xl/drawings/_rels/d1.xml.rels": b'<Relationships><Relationship Id="img" Target="../media/one.png"/></Relationships>',
        "xl/media/one.png": b"non-image-stub",
    }
    fragment = f'<oneCellAnchor xmlns="{hybrid.XDR}" xmlns:a="{hybrid.IMAGE}" xmlns:r="{hybrid.R}"><from><row>1</row><col>0</col><rowOff>0</rowOff><colOff>0</colOff></from><pic><blipFill><a:blip r:embed="img"/><a:srcRect l="0" t="0" r="0" b="0"/></blipFill><spPr><a:effectLst/></spPr></pic></oneCellAnchor>'
    parts["xl/drawings/d1.xml"] = (f'<wsDr xmlns="{hybrid.XDR}">' + fragment*3 + '</wsDr>').encode()
    calls = []

    class PackageStub:
        """仅在内存提供 XML 关系，模拟 ZIP 读接口但不创建文档样本。"""

        def __init__(self, path):
            """不读取 path，所有内容来自封闭 Stub 字典。"""

        def __enter__(self):
            """模拟只读包上下文。"""
            return self

        def __exit__(self, *arguments):
            """不持有文件或网络资源。"""

        def namelist(self):
            """返回已知包内名称，供孤立部件保护核对。"""
            return list(parts)

        def getinfo(self, name):
            """返回有界部件大小摘要。"""
            return SimpleNamespace(file_size=len(parts[name]))

        def read(self, name):
            """只读固定 Stub XML/字节，不解压真实文档。"""
            return parts[name]

    def native_stub(self, **kwargs):
        """不调用 Native Parser，返回契约主结果。"""
        return base.native.model_copy(deep=True)

    def image_stub(content):
        """不解码图片，返回非纯色的像素 Stub。"""
        return SimpleNamespace(width=100, height=100, n=1, samples=b"\x00\xff"*5000)

    def dimensions_stub(content):
        """不读取图头，提供固定尺寸以隔离候选流程。"""
        return 100, 100, "image/png"

    def bundle_stub(images, policy):
        """捕获唯一图片数量，不生成或渲染 PDF。"""
        calls.append(len(images))
        return b"stub-bundle" if images else b""

    monkeypatch.setattr(hybrid, "ZipFile", PackageStub)
    monkeypatch.setattr(hybrid.XlsxParser, "parse", native_stub)
    monkeypatch.setattr(hybrid, "image_dimensions", dimensions_stub)
    monkeypatch.setattr(hybrid.pymupdf, "Pixmap", image_stub)
    monkeypatch.setattr(hybrid, "build_bundle", bundle_stub)
    plan = hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy())
    assert calls == [1] and plan.manifest.bundle_page_count == 1
    assert [e.state for e in plan.manifest.entries] == ["pending", "reused", "reused"]
    calls.clear()
    cached = visual_result(base, (1,)).blocks
    cached = [block.model_copy(update={"page": 1, "bbox": None, "section": ""}) for block in cached]

    def cached_visual(content_hash):
        """相同精确视觉字节复用 Stub 标准化结果，不依赖新 Bundle/Anchor/文档身份。"""

        return cached

    hit_plan = hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(),
                                    visual_lookup=cached_visual)
    assert calls == [0] and hit_plan.manifest.bundle_page_count == 0
    assert all(entry.cache_hit and entry.bundle_page is None for entry in hit_plan.manifest.entries)
    assert len(merge_hybrid(hit_plan, None).blocks) == 5
    calls.clear()
    with pytest.raises(HybridPreflightError, match="HYBRID_RESOURCE_LIMIT"):
        hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(max_candidates=2))
    calls.clear()
    plan = hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(min_side=101))
    assert calls == [0] and all(e.state == "filtered" for e in plan.manifest.entries)
    calls.clear()
    with pytest.raises(HybridPreflightError):
        hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(max_image_bytes=1))
    assert calls == []
    # 多个不同字节内容只靠关系与 Hash 测流程，仍不生成图片/工作簿。
    parts["xl/media/two.png"] = b"different-image-stub"
    parts["xl/drawings/_rels/d1.xml.rels"] = b'<Relationships><Relationship Id="img" Target="../media/one.png"/><Relationship Id="img2" Target="../media/two.png"/></Relationships>'
    parts["xl/drawings/d1.xml"] = (f'<wsDr xmlns="{hybrid.XDR}">' + fragment + fragment.replace('r:embed="img"', 'r:embed="img2"') + '</wsDr>').encode()
    with pytest.raises(HybridPreflightError):
        hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(max_pages=1))
    assert calls == []
    plan = hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy())
    assert calls == [2] and plan.manifest.bundle_page_count == 2
    first_hash = digest(b"non-image-stub")

    def partial_visual(content_hash):
        """只缓存第一张图，第二张仍进入唯一 Bundle，禁止每图提交。"""

        return cached if content_hash == first_hash else None

    partial = hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy(),
                                   visual_lookup=partial_visual)
    assert calls == [2, 1] and partial.manifest.bundle_page_count == 1
    assert partial.manifest.entries[0].cache_hit and partial.manifest.entries[0].bundle_page is None
    assert partial.manifest.entries[1].bundle_page == 1 and not partial.manifest.entries[1].cache_hit
    parts["xl/drawings/d1.xml"] = parts["xl/drawings/d1.xml"].replace(b'<a:srcRect l="0"', b'<a:srcRect l="1"')
    with pytest.raises(HybridPreflightError) as caught:
        hybrid.prepare_hybrid(doc_id, Path("unused.xlsx"), "source.xlsx", HybridPolicy())
    assert caught.value.code == "HYBRID_UNSUPPORTED_IMAGE_TRANSFORM"
    assert caught.value.whole_compatible and calls == [2, 1]


def test_bundle_resource_limit_before_pdf_open(monkeypatch):
    """页数超限在打开 PDF 前拒绝，不生成超量或逐元素任务。"""

    def forbidden_open(*arguments, **kwargs):
        """若保护未生效则立即失败，绝不打开 PDF 引擎。"""
        raise AssertionError("页数超限不得打开 PDF")

    monkeypatch.setattr(hybrid.pymupdf, "open", forbidden_open)
    with pytest.raises(HybridPreflightError):
        hybrid.build_bundle([b"stub1", b"stub2"], HybridPolicy(max_pages=1))


def test_unmapped_entry_and_failed_candidate_never_pass():
    """已失败或来源不属于当前源文档的候选不能被当成完整成功结果。"""

    for failure in ("state", "doc"):
        plan = metadata_plan()
        if failure == "state":
            plan.manifest.entries[0].state = "failed"
        else:
            plan.manifest.entries[0].source_doc_id = uuid4()
        with pytest.raises(HybridPreflightError):
            merge_hybrid(plan, visual_result(plan))


def test_log_whitelist_excludes_manifest_and_body():
    """日志只纳入候选数量，来源账本与正文不进入日志。"""

    summary = safe_fields(metrics(metadata_plan().manifest) | {"manifest": "private", "text": "secret"})
    assert "manifest" not in summary and "text" not in summary
    assert summary["hybrid_bundle_page_count"] == 2


def test_invalid_image_header_safe_error():
    """非法图头不带二进制或本机路径；不调用图像解码器。"""

    with pytest.raises(HybridPreflightError) as error:
        image_dimensions(b"invalid-private-bytes")
    assert "invalid-private-bytes" not in str(error.value)


@pytest.mark.parametrize("code,compatible", [
    ("HYBRID_UNSUPPORTED_ANCHOR", True), ("HYBRID_UNSUPPORTED_CHART", True),
    ("HYBRID_UNSUPPORTED_IMAGE_TRANSFORM", True), ("HYBRID_RESOURCE_LIMIT", False),
    ("HYBRID_EXTERNAL_ASSET_MISSING", False), ("HYBRID_INVALID_PACKAGE", False),
    ("HYBRID_INVALID_IMAGE", False), ("HYBRID_ORPHAN_PART", False),
])
def test_specific_preflight_reasons_gate_whole_fallback(code, compatible):
    """只有固定能力缺口可提交前 Whole；损坏/缺附件/超限不能消耗云额度。"""

    error = HybridPreflightError(code)
    assert error.code == code and error.whole_compatible is compatible
    assert code in str(error) and "REVIEW" not in str(error)


def test_shared_formulas_restore_expressions_without_cached_values():
    """共享公式按地址平移保留原式，忽略缓存计算结果而不执行公式。"""

    tree = etree.fromstring(b'<worksheet><sheetData><row><c r="B1"><f t="shared" si="0">A1*2</f><v>100</v></c><c r="B2"><f t="shared" si="0"/><v>200</v></c></row></sheetData></worksheet>')
    values = hybrid.sheet_cells(tree, [])
    assert values == {"B1": "=A1*2", "B2": "=A2*2"}


def test_basic_chart_default_line_style_not_rejected():
    """常见实线和纯色样式保留为结构元信息，不因非空 spPr 无条件拒绝。"""

    tree = etree.fromstring(b'<chartSpace><chart><plotArea><lineChart><ser><spPr><ln><solidFill><srgbClr val="336699"/></solidFill><prstDash val="solid"/></ln></spPr><val><numRef><f>Sheet1!$A$1</f></numRef></val></ser></lineChart></plotArea></chart></chartSpace>')
    text = chart_text(tree, {"Sheet1": {"A1": "=B1*2"}})
    assert "=B1*2" in text and "336699" in text and "solid" in text


def test_visual_only_sheet_requires_actual_visual_result():
    """无单元格工作表可以合并视觉正文，但所有视觉被过滤后不得伪装成功。"""

    from app.parsers.common import NoTextError
    plan = metadata_plan()
    plan.native.blocks = []
    plan.manifest.native_hash = digest(plan.native.model_dump_json().encode())
    parsed = merge_hybrid(plan, visual_result(plan))
    assert len(parsed.blocks) == 2 and all(b.source == "mineru_cloud" for b in parsed.blocks)
    plan = metadata_plan(page_count=0)
    plan.native.blocks = []
    plan.manifest.native_hash = digest(plan.native.model_dump_json().encode())
    for entry in plan.manifest.entries:
        entry.state, entry.filter_reason, entry.bundle_page = "filtered", "small", None
    with pytest.raises(NoTextError):
        merge_hybrid(plan, None)
