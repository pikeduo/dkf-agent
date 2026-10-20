"""用户执行的内存像素/文件边界用例；不生成PDF/图片，不运行真实渲染或OCR。"""

import math
import sys
from types import SimpleNamespace

import pytest

from app.services import document_visual_quality as visual
from app.services.specialty_errors import SpecialtyError
from test_document_analysis import snapshot


def test_blank_page_keeps_blur_and_skew_unknown():
    """纯白像素不能被判为模糊，也不能从空白推断没有业务内容。"""
    result = visual.measure_gray_page(bytes([255]) * 10000, 100, 100, 100, page=1)
    assert result.blur == result.skew == "unknown" and result.skew_degrees is None
    assert result.contrast == result.edge_energy == result.dark_pixel_ratio == 0


@pytest.mark.parametrize("angle", [-4, 0, 4])
def test_text_like_pixel_rows_produce_direction_hint_with_explicit_review_threshold(angle):
    """仅内存像素验证行投影方向，不把此用例当作真实扫描件检测准确率。"""
    width, height = 240, 180
    samples = bytearray([255]) * (width * height)
    slope = math.tan(math.radians(angle))
    for row in range(20, 160, 20):
        for column in range(20, 220, 10):
            shift = round(slope * (column - width / 2))
            for dy in range(4):
                for dx in range(5):
                    samples[(row + shift + dy) * width + column + dx] = 0
    result = visual.measure_gray_page(samples, width, height, width, page=1)
    assert result.skew_degrees == pytest.approx(angle, abs=1)
    assert result.skew == ("review" if angle else "no_observed_issue")
    assert result.blur == "no_observed_issue"


@pytest.mark.parametrize("width,height,stride", [(0, 3, 3), (2048, 2, 2048), (5, 5, 4)])
def test_bad_pixel_dimensions_fail_without_partial_metrics(width, height, stride):
    """坏尺寸/步长不进入像素循环，不发布伪造统计。"""
    with pytest.raises(SpecialtyError) as caught:
        visual.measure_gray_page(b"", width, height, stride, page=1)
    assert caught.value.code == "ANALYSIS_VISUAL_LIMIT"


def test_source_hash_mismatch_is_rejected_before_renderer_opens(monkeypatch, tmp_path):
    """原件只放普通字节，Hash不符时模拟渲染入口也不得触发。"""
    path = tmp_path / "source.bin"
    path.write_bytes(b"not-a-document")
    calls = []
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda *args: calls.append(args)))
    monkeypatch.setattr(visual, "source_path", lambda document: path)
    with pytest.raises(SpecialtyError) as caught:
        visual.inspect_visual_pages(snapshot(), [1])
    assert caught.value.code == "ANALYSIS_VISUAL_SOURCE" and calls == []


def test_office_visual_request_does_not_invoke_conversion_or_renderer():
    """Office逻辑页不能作为原件PDF页；此入口不启动LibreOffice或云解析。"""
    with pytest.raises(SpecialtyError) as caught:
        visual.inspect_visual_pages(snapshot(file_type="docx"), [1])
    assert caught.value.code == "ANALYSIS_VISUAL_FORMAT"
