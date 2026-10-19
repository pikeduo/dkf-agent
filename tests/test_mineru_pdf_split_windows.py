"""Windows保存句柄回归：仅临时生成PDF/Mock异常，无业务原件、网络或数据库。"""

import socket
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pymupdf
import pytest

from app.core.config import Settings
from app.services.mineru import pdf_split as tool
from app.services.mineru.errors import MinerUError, PDF_SPLIT_ERROR_CODES


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """本文件所有用例禁止连接或DNS，离线验证不能意外外传样本或访问业务服务。"""

    def forbidden(*args, **kwargs):
        """任何网络尝试立即失败，错误不回显目标地址。"""
        raise AssertionError("离线分片用例禁止网络")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def source_pdf(directory: Path, pages: int) -> Path:
    """仅在pytest临时目录生成无正文PDF，每页几何唯一，覆盖四种旋转与非零裁剪框。"""
    path = directory / "offline-source.pdf"
    with pymupdf.open() as document:
        for index in range(pages):
            width, height = 600 + index, 800 + index
            page = document.new_page(width=width, height=height)
            page.set_cropbox(pymupdf.Rect(10, 20, width - 10, height - 20))
            page.set_rotation((index % 4) * 90)
        document.save(str(path), no_new_id=True)
    return path


def test_real_pymupdf_saves_unopened_path_and_preserves_exact_bytes(tmp_path, monkeypatch):
    """真正执行MuPDF路径写入，禁止传入句柄/预建目标；输出保持既有序列化Hash和页几何。"""
    source = source_pdf(tmp_path, 6)
    original_hash = tool.file_digest(source)
    target = tmp_path / "candidate.pdf"
    original_save = pymupdf.Document.save
    calls = []

    def checked_save(document, filename, *args, **kwargs):
        """调用真实save前验证是不存在的字符串路径，避免Windows具名句柄冲突复发。"""
        assert isinstance(filename, str)
        assert not Path(filename).exists()
        calls.append(kwargs)
        return original_save(document, filename, *args, **kwargs)

    with pymupdf.open(source) as document:
        with pymupdf.open() as expected:
            expected.insert_pdf(document, from_page=1, to_page=4)
            # BytesIO没有具名文件句柄；作为原序列化参数的独立字节参考，不更换算法。
            stream = BytesIO()
            original_save(expected, stream, garbage=4, deflate=True, no_new_id=True)
            expected_bytes = stream.getvalue()
        monkeypatch.setattr(pymupdf.Document, "save", checked_save)
        tool.render_range(document, 2, 5, target)
        with pymupdf.open(target) as piece:
            assert piece.page_count == 4 and not piece.is_repaired
            for index, page in enumerate(piece):
                original = document[index + 1]
                assert (page.rect, page.cropbox, page.rotation) == (original.rect, original.cropbox, original.rotation)
    assert calls == [{"garbage": 4, "deflate": True, "no_new_id": True}]
    assert target.read_bytes() == expected_bytes
    assert tool.file_digest(source) == original_hash
    # 真实Windows下关闭MuPDF后目标必须可以改名/删除；只操作本用例拥有的临时文件。
    renamed = tmp_path / "renamed.pdf"
    target.rename(renamed)
    renamed.unlink()
    assert not list(tmp_path.glob(".pdf-write-*"))


def test_real_257_page_offline_split_is_continuous_and_hash_verified(tmp_path):
    """真实文件流程验证200+57页、全部原页几何、实际大小/Hash及源文件不变，不发云任务。"""
    source = source_pdf(tmp_path, 257)
    original_hash = tool.file_digest(source)
    directory = tmp_path / "parts"
    with pymupdf.open(source) as document:

        def render(first, last, target):
            """仅调用本次修复的本地复制/保存函数，不创建Document或Parse Job。"""
            tool.render_range(document, first, last, target)

        # 此回归冻结旧200页文件字节，默认新计划180页另由纯Mock覆盖。
        parts = tool.build_parts(document.page_count, directory, render, max_pages=200)
        assert [(p["page_start"], p["page_end"]) for p in parts] == [(1, 200), (201, 257)]
        pages = []
        for part in parts:
            path = directory / f"part-{part['ordinal']:04d}.pdf"
            assert part["file_hash"] == tool.file_digest(path)
            assert part["size_bytes"] == path.stat().st_size <= tool.MAX_BYTES
            with pymupdf.open(path) as piece:
                assert piece.page_count == part["page_end"] - part["page_start"] + 1 <= tool.MAX_PAGES
                for index, page in enumerate(piece):
                    original_page = part["page_start"] + index
                    original = document[original_page - 1]
                    assert (page.rect, page.cropbox, page.rotation) == (original.rect, original.cropbox, original.rotation)
                    pages.append(original_page)
        assert pages == list(range(1, 258))
    assert tool.file_digest(source) == original_hash
    assert not list(directory.glob(".split-*"))


def test_existing_target_is_never_overwritten(tmp_path):
    """调用者已有目标文件不得被MuPDF覆盖，也不得修改原文件字节。"""
    source = source_pdf(tmp_path, 2)
    target = tmp_path / "candidate.pdf"
    target.write_bytes(b"KEEP_EXISTING_FILE")
    with pymupdf.open(source) as document:
        with pytest.raises(MinerUError) as error:
            tool.render_range(document, 1, 1, target)
    assert error.value.code == "PDF_SPLIT_CHANGED"
    assert target.read_bytes() == b"KEEP_EXISTING_FILE"


def test_target_created_during_save_is_not_overwritten(tmp_path, monkeypatch):
    """模拟检查后另一个写入者创建目标，最终xb仍须独占失败，避免仅exists检查的竞态。"""
    source = source_pdf(tmp_path, 2)
    target = tmp_path / "candidate.pdf"
    original_save = pymupdf.Document.save

    def competing_save(document, filename, *args, **kwargs):
        """先完成真正MuPDF暂存写入，再模拟竞争者创建最终目标。"""
        result = original_save(document, filename, *args, **kwargs)
        target.write_bytes(b"KEEP_COMPETING_FILE")
        return result

    monkeypatch.setattr(pymupdf.Document, "save", competing_save)
    with pymupdf.open(source) as document:
        with pytest.raises(MinerUError) as error:
            tool.render_range(document, 1, 1, target)
    assert error.value.code == "PDF_SPLIT_CHANGED"
    assert target.read_bytes() == b"KEEP_COMPETING_FILE"


@pytest.mark.parametrize("exception_type", [pymupdf.mupdf.FzErrorSystem, pymupdf.mupdf.FzErrorGeneric, PermissionError])
def test_write_failure_has_safe_code_type_and_source_location(tmp_path, monkeypatch, exception_type):
    """底层MuPDF/系统写入异常归类WRITE，只记录类型/固定源码位置，不泄露异常原文。"""
    source = source_pdf(tmp_path, 2)
    target, events = tmp_path / "candidate.pdf", []
    private = "PRIVATE_TOKEN PRIVATE_TEXT https://private.invalid/?signature=PRIVATE C:/private/source.pdf"

    def fail_save(document, filename, **kwargs):
        """模拟实际MuPDF异常类型，而不是错误地用RuntimeError替代。"""
        raise exception_type(private)

    def record(event, **fields):
        """只接收业务安全字段，禁止写真实运行日志。"""
        events.append((event, fields))

    monkeypatch.setattr(pymupdf.Document, "save", fail_save)
    monkeypatch.setattr(tool, "log_event", record)
    with pymupdf.open(source) as document:
        with pytest.raises(MinerUError) as error:
            tool.render_range(document, 1, 1, target)
    assert error.value.code == "PDF_SPLIT_WRITE" and error.value.code in PDF_SPLIT_ERROR_CODES
    assert error.value.__suppress_context__ and not error.value.retryable
    assert events == [("mineru.split_io_failed", {"error_code": "PDF_SPLIT_WRITE",
        "error_type": exception_type.__name__, "stage": "pdf_split.render_range", "step": "save"})]
    assert "PRIVATE" not in str(error.value) + repr(events)
    assert "source.pdf" not in str(error.value) + repr(events)
    assert not target.exists() and not list(tmp_path.glob(".pdf-write-*"))


def test_manifest_maps_mupdf_read_error_instead_of_unknown(tmp_path, monkeypatch):
    """规划入口读原件的FzErrorSystem也归类安全IO错误，不能泄露路径或落入UNKNOWN。"""
    source = source_pdf(tmp_path, 2)
    document = SimpleNamespace(doc_id=uuid4(), file_hash=tool.file_digest(source))
    job = SimpleNamespace(job_id=uuid4(), parse_options={"_pdf_split_request": {"policy": tool.policy(), "page_count": 2}})
    settings, events = Settings(_env_file=None, parsed_dir=tmp_path / "parsed"), []

    def fail_open(*args, **kwargs):
        """原文件读取异常仅带测试私有标记，不读取业务素材。"""
        raise pymupdf.mupdf.FzErrorSystem("PRIVATE_PATH PRIVATE_TOKEN")

    def record(event, **fields):
        """截获安全诊断，不写运行日志或执行数据库。"""
        events.append((event, fields))

    monkeypatch.setattr(pymupdf, "open", fail_open)
    monkeypatch.setattr(tool, "log_event", record)
    with pytest.raises(MinerUError) as error:
        tool.prepare_manifest(document, job, settings, source)
    assert error.value.code == "PDF_SPLIT_IO" and "PRIVATE" not in str(error.value) + repr(events)
    assert events[0][1]["error_type"] == "FzErrorSystem"
    assert events[0][1]["stage"] == "pdf_split.prepare_manifest"
