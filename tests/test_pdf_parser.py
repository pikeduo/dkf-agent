"""用真实 PDF 覆盖原生抽取、物理页码、坐标和需要 OCR 的失败边界。"""

from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from app.parsers.common import NoTextError, OCRRequiredError, ParserError
from app.parsers.pdf import PdfParser


def save_pdf(tmp_path: Path, pages: list[str | None], **save_options) -> Path:
    """在临时目录创建带指定原生文本或空白页的 PDF，并允许加密参数。"""

    file_path = tmp_path / "sample.pdf"
    with pymupdf.open() as document:
        for text in pages:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        document.save(file_path, **save_options)
    return file_path


def parse_pdf(file_path: Path, doc_id=None):
    """通过正式公共入口解析测试原件，保持原始文档身份信息。"""

    return PdfParser().parse(
        doc_id=doc_id or uuid4(),
        file_path=file_path,
        file_name="原生文档.pdf",
        file_type="pdf",
    )


def scan_png() -> bytes:
    """将测试文字渲染成 PNG，构造没有原生文本层的扫描页面。"""

    with pymupdf.open() as document:
        page = document.new_page(width=200, height=100)
        page.insert_text((20, 40), "Scanned text")
        return page.get_pixmap().tobytes("png")


def test_pdf_preserves_pages_coordinates_and_native_text(tmp_path):
    """多页原生 PDF 输出统一 Block，物理页码和点数坐标可追溯。"""

    path = save_pdf(tmp_path, ["First page\nSecond line", "Second page"])
    parsed = parse_pdf(path)
    assert [block.page for block in parsed.blocks] == [1, 2]
    assert [block.text for block in parsed.blocks] == [
        "First page\nSecond line",
        "Second page",
    ]
    assert parsed.title == ""
    assert parsed.file_name == "原生文档.pdf"
    assert parsed.file_type == "pdf"
    for block in parsed.blocks:
        assert block.source == "native_parser"
        assert block.confidence == 1.0
        assert block.block_type == "text"
        assert len(block.bbox) == 4
        assert 0 <= block.bbox[0] < block.bbox[2] <= 595
        assert 0 <= block.bbox[1] < block.bbox[3] <= 842


def test_pdf_blocks_follow_sorted_layout_order(tmp_path):
    """正文插入顺序与页面位置不同时，统一块按从上到下排列。"""

    path = tmp_path / "layout.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 180), "Bottom")
        page.insert_text((72, 72), "Top", fontsize=24)
        document.save(path)
    parsed = parse_pdf(path)
    assert [block.text for block in parsed.blocks] == ["Top", "Bottom"]
    assert all(block.block_type == "text" for block in parsed.blocks)


def test_pdf_extracts_native_chinese_text_without_ocr(tmp_path):
    """内嵌中文字体的原生文字保持 Unicode 内容，不调用 OCR。"""

    path = tmp_path / "chinese.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "知识库文档解析", fontname="china-s")
        document.save(path)
    assert parse_pdf(path).blocks[0].text == "知识库文档解析"


def test_pdf_repeat_parse_uses_stable_unique_block_ids(tmp_path):
    """同一原件和文档身份重复解析得到稳定块 ID，不同文档身份不共用 ID。"""

    path = save_pdf(tmp_path, ["Same text", "Same text"])
    doc_id = uuid4()
    first = parse_pdf(path, doc_id)
    second = parse_pdf(path, doc_id)
    assert first == second
    assert len({block.block_id for block in first.blocks}) == 2
    different = parse_pdf(path)
    assert {block.block_id for block in first.blocks}.isdisjoint(
        block.block_id for block in different.blocks
    )


def test_pdf_blank_page_does_not_renumber_physical_pages(tmp_path):
    """真正空白页允许跳过，但后续原生块仍保留真实物理页码。"""

    path = save_pdf(tmp_path, ["Page one", None, "Page three"])
    assert [block.page for block in parse_pdf(path).blocks] == [1, 3]


def test_pdf_all_blank_pages_fail_with_no_text(tmp_path):
    """仅含真实空白页的 PDF 不得作为成功解析返回空块集合。"""

    path = save_pdf(tmp_path, [None, None])
    with pytest.raises(NoTextError, match="原生文本"):
        parse_pdf(path)


@pytest.mark.parametrize("native_page_first", [False, True])
def test_pdf_scanned_or_mixed_pages_require_ocr(tmp_path, native_page_first):
    """扫描 PDF 和原生/扫描混合 PDF 均整体报需 OCR，不返回部分结果。"""

    path = tmp_path / "scan.pdf"
    with pymupdf.open() as document:
        if native_page_first:
            document.new_page().insert_text((72, 72), "Native page")
        page = document.new_page()
        page.insert_image(page.rect, stream=scan_png())
        document.save(path)
    expected_page = 2 if native_page_first else 1
    with pytest.raises(OCRRequiredError, match=f"第 {expected_page} 页"):
        parse_pdf(path)


def test_pdf_vector_only_page_is_not_silently_dropped(tmp_path):
    """没有文本层但存在矢量内容的页面不得假装为空白页。"""

    path = tmp_path / "outline.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.draw_rect(pymupdf.Rect(72, 72, 180, 180))
        document.save(path)
    with pytest.raises(OCRRequiredError):
        parse_pdf(path)


def test_pdf_annotation_only_page_is_not_treated_as_blank(tmp_path):
    """注释中承载可见内容但原生正文为空时，不静默跳过整个页面。"""

    path = tmp_path / "annotation.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.add_rect_annot(pymupdf.Rect(72, 72, 272, 172))
        document.save(path)
    with pytest.raises(OCRRequiredError):
        parse_pdf(path)


def test_pdf_text_with_decorative_image_keeps_native_text_only(tmp_path):
    """有原生正文的页面抽取文字，不把图片元信息伪造成识别正文。"""

    path = tmp_path / "illustration.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Native body")
        page.insert_image(pymupdf.Rect(72, 120, 272, 220), stream=scan_png())
        document.save(path)
    parsed = parse_pdf(path)
    assert [block.text for block in parsed.blocks] == ["Native body"]


@pytest.mark.parametrize("user_password", ["secret-user", ""])
def test_pdf_encryption_fails_even_when_no_user_password_needed(
    tmp_path, user_password
):
    """有用户密码和仅所有者密码的加密 PDF 都明确拒绝，不尝试解密。"""

    path = save_pdf(
        tmp_path,
        ["Encrypted"],
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw=user_password,
        owner_pw="secret-owner",
    )
    with pytest.raises(ParserError, match="加密") as error:
        parse_pdf(path)
    assert str(path) not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("content", [b"", b"%PDF-1.7\nnot a real PDF", b"hello"])
def test_pdf_invalid_content_fails_without_leaking_path(tmp_path, content):
    """空文件和损坏内容返回安全解析错误，不泄露磁盘路径。"""

    path = tmp_path / "private.pdf"
    path.write_bytes(content)
    with pytest.raises(ParserError, match="无法解析") as error:
        parse_pdf(path)
    assert str(path) not in str(error.value)


def test_pdf_rotation_keeps_unrotated_point_coordinates(tmp_path):
    """页面旋转不改变原生 bbox 坐标系，避免引用定位时二次旋转。"""

    path = tmp_path / "rotated.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Rotated body")
        original_bbox = list(page.get_text("blocks")[0][:4])
        page.set_rotation(90)
        document.save(path)
    assert parse_pdf(path).blocks[0].bbox == pytest.approx(original_bbox)


def test_pdf_non_pdf_type_is_rejected_before_reading_file(tmp_path):
    """错误文件类型在读取前拒绝，图片不得被原生 PDF Parser 误处理。"""

    with pytest.raises(ParserError, match="只支持"):
        PdfParser().parse(
            doc_id=uuid4(),
            file_path=tmp_path / "missing.png",
            file_name="扫描.png",
            file_type="png",
        )


def test_pdf_missing_file_preserves_os_error_for_task_layer(tmp_path):
    """原件访问错误保留系统异常类型，使任务层能执行既有重试策略。"""

    with pytest.raises(FileNotFoundError):
        parse_pdf(tmp_path / "missing.pdf")


def test_pdf_repaired_structure_is_rejected_instead_of_silently_accepted(tmp_path):
    """底层能自动修复的截断 PDF 也拒绝，防止对不完整文档静默确认成功。"""

    path = save_pdf(tmp_path, ["Repairable"])
    path.write_bytes(path.read_bytes()[:-10])
    with pymupdf.open(path) as document:
        assert document.is_repaired
    with pytest.raises(ParserError, match="结构损坏"):
        parse_pdf(path)


def test_pdf_extraction_runtime_failure_is_sanitized(tmp_path, monkeypatch):
    """正文抽取时底层报错只输出安全中文错误，不传播路径和内部详情。"""

    path = save_pdf(tmp_path, ["Native"])

    def broken_get_text(self, *args, **kwargs):
        """模拟 PDF 解码阶段包含磁盘路径的内部异常。"""

        raise RuntimeError(f"cannot decode {path}")

    monkeypatch.setattr(pymupdf.Page, "get_text", broken_get_text)
    with pytest.raises(ParserError, match="无法解析") as error:
        parse_pdf(path)
    assert str(path) not in str(error.value)


def test_pdf_file_permission_failure_remains_retriable(tmp_path, monkeypatch):
    """读取原件权限错误仍为 OSError，不误包装为永久格式错误。"""

    def unreadable_file(self):
        """模拟原件暂时不可访问的文件系统错误。"""

        raise PermissionError("temporary access denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable_file)
    with pytest.raises(PermissionError):
        parse_pdf(tmp_path / "locked.pdf")
