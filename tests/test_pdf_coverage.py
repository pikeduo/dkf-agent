"""仅用页面/绘制元数据 Stub 验证覆盖白名单；不创建、渲染或读取真实 PDF。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pymupdf
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.parsers.common import make_block
from app.parsers.routing import inspector
from app.parsers.routing.contracts import PageSignals, RouteDecision, StructuralSignals
from app.parsers.routing.guard import CompletenessError, guard_result
from app.parsers.routing.pdf_coverage import margin_rule_only
from app.parsers.routing.router import decide_route
from app.schemas.parsed_document import ParsedDocument, ParseQuality
from app.services.mineru.errors import MinerUError
from app.services.mineru.pdf_split_jobs import validate_piece


def filled_path(rect, items, sequence, shade=0.6):
    """构造与 PyMuPDF 字段一致的填充路径，不生成文档或正文。"""
    return {"rect": tuple(rect), "items": items, "seqno": sequence, "type": "f",
            "fill": (shade, shade, shade), "fill_opacity": 1, "even_odd": False,
            "layer": "", "color": None}


def divider(top=30, *, left=30, right=570):
    """构造双层灰色横线及左右封口；尺寸不依赖真实样本的页码或文件名。"""
    bottom, middle = top + 1.4, top + 0.7
    rectangles = [(left, top, right, middle), (left, middle, right, bottom)]
    paths = [filled_path(rect, [("re", rect, 1)], index, shade=0.6 + index * 0.3)
             for index, rect in enumerate(rectangles)]
    paths.append(filled_path((right - 0.7, top, right, bottom), [
        ("l", (right - 0.7, middle), (right - 0.7, bottom)),
        ("l", (right - 0.7, bottom), (right, bottom)),
        ("l", (right, bottom), (right, top))], 2, shade=0.9))
    paths.append(filled_path((left, top, left + 0.7, bottom), [
        ("l", (left, top), (left, bottom)),
        ("l", (left, bottom), (left + 0.7, middle)),
        ("l", (left + 0.7, middle), (left + 0.7, top))], 3))
    return paths


class PageStub:
    """只提供结构元数据，拒绝测试依赖真实 PDF 文件或解析器。"""

    def __init__(self, paths, *, number=0, text=""):
        """初始化独立绘制列表，保留可检查的输入副本。"""
        self.paths, self.number, self.text = deepcopy(paths), number, text
        self.rotation, self.rect = 0, pymupdf.Rect(0, 0, 600, 800)
        self.traces, self.images, self.annotations = [], [], []
        self.paint = [("fill-path", item["rect"]) for item in paths]

    def get_drawings(self):
        """返回原始路径，调用方不得修改或删除它们。"""
        return self.paths

    def get_bboxlog(self):
        """返回包含未知绘制的可注入序列。"""
        return self.paint

    def get_texttrace(self):
        """允许注入隐藏或未能提取的字体绘制。"""
        return self.traces

    def get_image_info(self):
        """返回纯元数据图片引用。"""
        return self.images

    def annots(self):
        """返回批注迭代器，不操作系统对象。"""
        return iter(self.annotations)

    def get_text(self, *args, **kwargs):
        """向 Inspector 返回受控内存文本摘要，绝不打开 PDF。"""
        lines = [{"bbox": (40, 100, 200, 115), "spans": [{"text": self.text}]}] if self.text else []
        return {"blocks": [{"type": 0, "lines": lines}]}


@pytest.mark.parametrize("top", [25, 55, 750])
def test_only_margin_rule_is_decoration_with_unchanged_paths(top):
    """顶部/底部的完整灰线可免覆盖；多次判断不改变路径、序号或原始坐标。"""
    page = PageStub(divider(top))
    before = deepcopy(vars(page))
    assert margin_rule_only(page, page.paths)
    assert margin_rule_only(page, page.paths) and vars(page) == before


def test_single_full_width_gray_rectangle_is_supported():
    """通用白名单也接受单层细横线，不依赖真实样本恰有四条绘图路径。"""
    rect = (35, 45, 565, 46)
    page = PageStub([filled_path(rect, [("re", rect, 1)], 0)])
    assert margin_rule_only(page, page.paths)


@pytest.mark.parametrize("change", ["center", "thick", "short", "colored", "transparent", "curve",
    "stroke", "vertical", "rotation", "layer", "hidden_text", "image", "annotation", "shading",
    "extra_paint", "seqno", "unknown_shape", "middle_glyph", "nonfinite", "unknown_rect",
    "mismatched_paint", "outside_page", "many_paths"])
def test_uncertain_or_semantic_vector_page_is_not_decoration(change):
    """图表/公式/流程图特征、未知结构与额外绘制必须保守保留，不能按四个路径放行。"""
    page = PageStub(divider())
    if change == "center":
        page = PageStub(divider(300))
    elif change == "thick":
        rect = (30, 30, 570, 36)
        page = PageStub([filled_path(rect, [("re", rect, 1)], 0)])
    elif change == "short":
        page = PageStub(divider(right=150))
    elif change == "colored":
        page.paths[0]["fill"] = (1, 0, 0)
    elif change == "transparent":
        page.paths[0]["fill_opacity"] = 0.5
    elif change == "curve":
        page.paths[0]["items"] = [("c", (30, 30), (100, 30), (400, 31), (570, 30))]
    elif change == "stroke":
        page.paths[0]["type"] = "s"
    elif change == "vertical":
        rect = (30, 30, 31, 600)
        page = PageStub([filled_path(rect, [("re", rect, 1)], 0)])
    elif change == "rotation":
        page.rotation = 90
    elif change == "layer":
        page.paths[0]["layer"] = "layer"
    elif change == "hidden_text":
        page.traces = [{"type": 3}]
    elif change == "image":
        page.images = [{"bbox": (50, 50, 100, 100)}]
    elif change == "annotation":
        page.annotations = [object()]
    elif change in {"shading", "extra_paint"}:
        page.paint.append(("fill-shade" if change == "shading" else "ignore-text", (50, 50, 100, 100)))
    elif change == "seqno":
        page.paths[-1]["seqno"] = 0
    elif change == "unknown_shape":
        page.paths[2]["items"] = [("re", (1, 2)), ("unknown",)]
    elif change == "middle_glyph":
        rect = (100, 30, 100.5, 31)
        page.paths.append(filled_path(rect, [("re", rect, 1)], 4))
        page.paint.append(("fill-path", rect))
    elif change == "nonfinite":
        page.paths[0]["rect"] = (30, float("nan"), 570, 31)
    elif change == "unknown_rect":
        page.paths[0]["items"][0] = ("re", (30, 30, 569, 30.7), 1)
    elif change == "mismatched_paint":
        page.paint[0] = ("fill-path", (30, 30, 569, 30.7))
    elif change == "outside_page":
        page = PageStub(divider(left=-1))
    else:
        page.paths *= 9
        page.paint *= 9
    assert not margin_rule_only(page, page.paths)


class DocumentStub:
    """仅用于 Inspector 的内存页集合，不产生真实文档。"""

    def __init__(self, pages):
        """保留原物理页序与安全状态。"""
        self.pages, self.page_count = pages, len(pages)
        self.needs_pass, self.is_repaired = False, False

    def __enter__(self):
        """返回只读页集合。"""
        return self

    def __exit__(self, *args):
        """不产生文件/数据库副作用。"""

    def __iter__(self):
        """按物理页序提供页面。"""
        return iter(self.pages)


def inspect_stub(monkeypatch, pages, **configuration):
    """替换 PDF 打开入口，传入受控配置复用正式预检，不读取本地样本。"""
    def open_stub(*args, **kwargs):
        """确保所有调用仅得到内存元数据。"""
        return DocumentStub(pages)
    monkeypatch.setattr(inspector.pymupdf, "open", open_stub)
    return inspector.inspect_pdf(None, Settings(_env_file=None, **configuration))


def dense_drawing_page(*, number=0, count=9207, text=""):
    """提供只能计数的密集绘图元数据，任何几何遍历立即使测试失败。"""
    page = PageStub([], number=number, text=text)
    page.paths = MagicMock()
    page.paths.__len__.return_value = count
    page.paths.__iter__.side_effect = AssertionError("密集绘图页不得继续遍历路径")
    return page


@pytest.mark.parametrize("count,image_count,drawing_threshold", [
    (5001, 0, 6), (9207, 0, 1000), (4901, 100, 1000), (2, 4999, 1000),
    (58, 12639, 1000), (0, 5001, 1000)])
@pytest.mark.parametrize("text", ["正文", ""])
def test_dense_drawings_route_whole_without_losing_page_coverage(monkeypatch, count, image_count, drawing_threshold, text):
    """密集图片/绘图保守判云，不能因调高阈值或缺文字而判装饰或漏后续页。"""
    pages = [PageStub([], number=index, text="正文") for index in range(20)]
    target = dense_drawing_page(number=13, count=count, text=text)
    target.images = [{"bbox": (0, 0, 1, 1)}] * image_count
    pages[13] = target
    decoration = MagicMock(side_effect=AssertionError("密集绘图不能判定为装饰"))
    monkeypatch.setattr(inspector, "margin_rule_only", decoration)

    inspected = inspect_stub(monkeypatch, pages, router_pdf_drawing_count=drawing_threshold)
    decision = decide_route("pdf", "auto", inspected)
    assert not inspected.uncertain and decision.route_strategy == "mineru_whole"
    assert "PDF_COMPLEX_DRAWING" in decision.reason_codes and "PREFLIGHT_LIMIT" not in decision.reason_codes
    assert inspected.signals.page_count == 20 and len(inspected.signals.pages) == 20
    assert [page.page for page in inspected.signals.pages] == list(range(1, 21))
    assert all(page.required for page in inspected.signals.pages)
    signal = inspected.signals.pages[13]
    assert signal.drawing_count == count and signal.image_count == image_count
    assert signal.image_area_ratio == pytest.approx(min(1, image_count / (600 * 800)))
    assert signal.text_chars == len(text) and signal.decorative_drawing_count == 0
    assert signal.coverage_reason == "content_or_uncertain"
    decoration.assert_not_called()
    target.paths.__iter__.assert_not_called()


@pytest.mark.parametrize("missing_page", [2, 3])
@pytest.mark.parametrize("image_count", [0, 12639])
def test_dense_drawings_preserve_guard_for_drawing_and_later_page(monkeypatch, missing_page, image_count):
    """密集图片/绘图页和后续正文仍要求覆盖；云缺页保持PARTIAL，Native缺页仍拒绝。"""
    dense = dense_drawing_page(number=1)
    dense.images = [{"bbox": (0, 0, 1, 1)}] * image_count
    inspected = inspect_stub(monkeypatch, [PageStub([], text="正文"), dense,
        PageStub([], number=2, text="后续正文")])
    identifier = uuid4()
    parsed = ParsedDocument(doc_id=identifier, file_name="任意.pdf", file_type="pdf", quality=ParseQuality(), blocks=[
        make_block(identifier, page, page=page, block_type="text", text="可靠内容").model_copy(
            update={"source": "mineru_cloud"}) for page in range(1, 4)])
    decision = decide_route("pdf", "auto", inspected)
    guard_result(parsed, decision)
    assert parsed.quality.content == "COMPLETE" and not parsed.quality.missing_pages

    parsed.blocks = [block for block in parsed.blocks if block.page != missing_page]
    guard_result(parsed, decision)
    assert parsed.quality.content == "PARTIAL" and parsed.quality.missing_pages == [missing_page]
    assert parsed.quality.required_pages == [1, 2, 3]
    with pytest.raises(CompletenessError):
        guard_result(parsed, decide_route("pdf", "native", inspected))


@pytest.mark.parametrize("image_count", [0, 12639])
def test_dense_drawings_do_not_hide_later_hard_budget_failure(monkeypatch, image_count):
    """密集视觉页不提前返回成功，后页文本行数超限仍拒绝整份预检。"""
    later = PageStub([], number=1)
    later.get_text = MagicMock(return_value={"blocks": [{"type": 0, "lines": [{}] * 2001}]})
    dense = dense_drawing_page()
    dense.images = [{"bbox": (0, 0, 1, 1)}] * image_count
    inspected = inspect_stub(monkeypatch, [dense, later])
    decision = decide_route("pdf", "auto", inspected)
    assert inspected.uncertain and decision.route_strategy == "failed"
    assert decision.reason_codes == ("PREFLIGHT_LIMIT",)
    assert not inspected.signals.pages


def test_inspection_freezes_coverage_proof_and_guard_keeps_legacy_strict(monkeypatch):
    """新规则只免装饰页；同一结果在旧冻结 Guard 下仍失败，不自动更改旧页摘要。"""
    inspected = inspect_stub(monkeypatch, [PageStub([], text="正文"), PageStub(divider(), number=1)])
    assert inspected.signals.pdf_coverage_version == "pdf-coverage-v2"
    assert inspected.signals.pages[1].drawing_count == inspected.signals.pages[1].decorative_drawing_count == 4
    assert inspected.signals.pages[1].coverage_reason == "margin_rule_only"
    assert not inspected.signals.pages[1].required
    identifier = uuid4()
    parsed = ParsedDocument(doc_id=identifier, file_name="任意.pdf", file_type="pdf", blocks=[
        make_block(identifier, 1, page=1, block_type="text", text="正文")])
    decision = decide_route("pdf", "mineru", inspected)
    parsed.blocks = [b.model_copy(update={"source": "mineru_cloud"}) for b in parsed.blocks]
    before = parsed.model_dump_json()
    guard_result(parsed, decision)
    old = decision.model_dump(mode="json")
    old["signals"].pop("pdf_coverage_version")
    for page in old["signals"]["pages"]:
        page.pop("decorative_drawing_count")
        page.pop("coverage_reason")
        page["required"] = True
    frozen = RouteDecision.model_validate(old)
    assert frozen.signals.pdf_coverage_version == "pdf-coverage-v1"
    with pytest.raises(CompletenessError):
        guard_result(parsed, frozen)
    assert parsed.model_dump_json() == before and old["signals"]["pages"][1]["required"]


def test_inspector_keeps_non_decorative_four_drawings_required(monkeypatch):
    """同样四条路径位于内容区时必须覆盖，防止按计数阈值放过真实绘图。"""
    inspected = inspect_stub(monkeypatch, [PageStub(divider(300))])
    page = inspected.signals.pages[0]
    assert page.required and page.drawing_count == 4 and page.decorative_drawing_count == 0
    assert page.coverage_reason == "content_or_uncertain"
    assert "PDF_SCAN_OR_NON_TEXT_PAGE" in inspected.risks


def test_unclassified_paint_is_required_even_without_drawings(monkeypatch):
    """未知shading/隐藏绘制即使没有可提取路径也不能视为空白；真正空白仍免覆盖。"""
    page = PageStub([])
    page.paint = [("fill-shade", (40, 100, 500, 600))]
    inspected = inspect_stub(monkeypatch, [page, PageStub([], number=1)])
    first, empty = inspected.signals.pages
    assert first.required and first.coverage_reason == "content_or_uncertain"
    assert not empty.required and empty.coverage_reason == "empty"


def test_split_guard_translates_v2_physical_pages_without_dropping_semantic_page(monkeypatch):
    """局部分片沿用同一覆盖证据；装饰页不压缩页号，真实绘图缺失仍拒绝。"""
    inspected = inspect_stub(monkeypatch, [PageStub(divider(), number=0),
        PageStub([], number=1, text="正文"), PageStub(divider(300), number=2)])
    decision = decide_route("pdf", "mineru", inspected)
    job = SimpleNamespace(parse_options={"_route_decision": decision.model_dump(mode="json")})
    identifier = uuid4()
    parsed = ParsedDocument(doc_id=identifier, file_name="任意.pdf", file_type="pdf", blocks=[
        make_block(identifier, 1, page=1, block_type="text", text="正文").model_copy(update={"source": "mineru_cloud"})])
    validate_piece(None, job, SimpleNamespace(page_start=2, page_end=2), parsed)
    with pytest.raises(MinerUError) as caught:
        validate_piece(None, job, SimpleNamespace(page_start=2, page_end=3), parsed)
    assert caught.value.code == "COMPLETENESS"


def test_coverage_schema_rejects_unproven_relaxation_or_mixed_versions():
    """计数、说明或版本不一致不能包装成已验证装饰覆盖。"""
    values = dict(page=1, text_chars=0, image_count=0, image_area_ratio=0, drawing_count=4,
                  annotation_count=0, required=False, decorative_drawing_count=4, coverage_reason="margin_rule_only")
    page = PageSignals(**values)
    with pytest.raises(ValidationError):
        PageSignals(**(values | {"text_chars": 1}))
    with pytest.raises(ValidationError):
        PageSignals(**(values | {"decorative_drawing_count": 3}))
    with pytest.raises(ValidationError):
        StructuralSignals(page_count=1, pages=(page,))
    assert StructuralSignals(page_count=1, pages=(page,), pdf_coverage_version="pdf-coverage-v2")
