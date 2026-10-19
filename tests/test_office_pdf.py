"""LibreOffice分页的纯Mock回归；不启动转换器、解析真实文档或连接业务服务，用户手动执行。"""

import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from app.core.config import Settings
from app.parsers.routing.contracts import Inspection, PageSignals, RouteDecision, StructuralSignals
from app.parsers.routing.guard import CompletenessError, guard_result
from app.schemas.parsed_document import ParsedBlock, ParsedDocument, ParseQuality
from app.services.mineru import office_pdf as tool, pdf_split as split, pdf_split_jobs as workflow
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import ParseCache, canonical, digest, key_for, recipe


@pytest.fixture
def context(tmp_path, monkeypatch):
    """提供非Office/PDF的字节哨兵和页摘要Stub，并禁止意外启动外部引擎。"""
    source = tmp_path / "original.docx"
    source.write_bytes(b"NOT_A_DOCUMENT_SOURCE_SENTINEL")
    document = SimpleNamespace(doc_id=uuid4(), file_name="original.docx", file_type="docx",
        file_hash=tool.file_hash(source), file_path=str(source))
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    identity = {"version": tool.VERSION, "source_hash": document.file_hash, "file_type": document.file_type,
                "engine_sha256": "a" * 64, "profile_sha256": digest(tool.PROFILE)}
    request = {"identity": identity, "key": digest(canonical(identity)),
        "executable": str(tmp_path / "soffice.com"), "timeout_seconds": 600}
    monkeypatch.setattr(tool, "source_path", lambda doc: source)
    monkeypatch.setattr(tool, "check_source_hash", lambda *args: None)
    monkeypatch.setattr(tool, "engine_fingerprint", lambda path: "a" * 64)

    def forbid_process(*args, **kwargs):
        """未显式替换的外部进程一律拒绝，不能运行真实LibreOffice。"""
        pytest.fail("纯Mock测试不得启动真实转换进程")

    monkeypatch.setattr(tool.subprocess, "Popen", forbid_process)
    return SimpleNamespace(document=document, settings=settings, source=source, request=request)


def page_signals(count):
    """构造内存页摘要，不读取任何Office/PDF文件。"""
    return StructuralSignals(page_count=count, pages=tuple(PageSignals(page=page, text_chars=10,
        image_count=0, image_area_ratio=0, drawing_count=0, annotation_count=0, required=True)
        for page in range(1, count + 1)))


def test_conversion_request_hashes_engine_without_running_it(tmp_path):
    """只读取启动器/引擎/版本字节来冻结请求，安装缺失明确失败；不执行版本命令。"""
    executable = tmp_path / "soffice.com"
    executable.write_bytes(b"LAUNCHER_SENTINEL")
    (tmp_path / "soffice.bin").write_bytes(b"ENGINE_SENTINEL")
    version = tmp_path / "version.ini"
    version.write_bytes(b"VERSION_ONE")
    settings = Settings(_env_file=None, libreoffice_executable=executable)
    document = SimpleNamespace(file_hash="a" * 64, file_type="docx")
    first = tool.conversion_request(document, settings)
    version.write_bytes(b"VERSION_TWO")
    second = tool.conversion_request(document, settings)
    assert first["identity"]["engine_sha256"] != second["identity"]["engine_sha256"]
    assert first["key"] != second["key"] and "executable" not in first["identity"]
    executable.unlink()
    with pytest.raises(MinerUError) as error:
        tool.conversion_request(document, settings)
    assert error.value.code == "OFFICE_PDF_DEPENDENCY"


def publish_stub(context, monkeypatch, count=201):
    """以字节哨兵模拟转换输出，预检全用Stub，返回真实缓存逻辑的冻结回执。"""
    calls = []

    def convert(request, source, work, kind):
        """只写非PDF占位字节并记录调用；不执行文档转换。"""
        calls.append(kind)
        target = work / "generated.bin"
        target.write_bytes(b"NOT_A_PDF_CONVERSION_SENTINEL")
        return target

    monkeypatch.setattr(tool, "run_converter", convert)
    monkeypatch.setattr(tool, "inspect_document", lambda *args: Inspection(signals=page_signals(count)))
    path, receipt = tool.ensure_conversion(context.document, context.request, context.settings)
    return path, receipt, calls


def test_conversion_receipt_is_reused_without_engine_or_source_overwrite(context, monkeypatch):
    """首次发布后只校验原件和派生Hash，安装路径失效也不重新转换或覆盖原件。"""
    original = context.source.read_bytes()
    path, receipt, calls = publish_stub(context, monkeypatch)
    monkeypatch.setattr(tool, "engine_fingerprint", lambda path: pytest.fail("已有回执不能重启引擎"))
    again, stored = tool.ensure_conversion(context.document, context.request, context.settings)
    assert again == path and stored == receipt and calls == ["docx"]
    assert receipt["page_semantics"] == "converted_office" and receipt["pdf_signals"]["page_count"] == 201
    assert context.source.read_bytes() == original and tool.file_hash(path) == receipt["pdf_sha256"]
    assert json.loads((path.parent / "receipt.json").read_text(encoding="utf-8")) == receipt


@pytest.mark.parametrize("damage", ["pdf", "missing", "source", "recipe", "pages"])
def test_changed_conversion_is_never_rerendered_or_uploaded(context, monkeypatch, damage):
    """冻结源/PDF/摘要被改时拒绝复用，不用重新渲染掩盖身份损坏。"""
    path, receipt, calls = publish_stub(context, monkeypatch)
    if damage == "pdf":
        path.write_bytes(b"CHANGED")
    elif damage == "missing":
        path.unlink()
    elif damage == "source":
        context.document.file_hash = "b" * 64
    elif damage == "recipe":
        receipt["identity"]["engine_sha256"] = "b" * 64
    else:
        receipt["pdf_signals"]["pages"].pop()
    with pytest.raises(MinerUError) as error:
        tool.checked_conversion(context.document, receipt, context.settings)
    assert error.value.code in {"OFFICE_PDF_CHANGED", "OFFICE_PDF_IDENTITY"} and calls == ["docx"]


def test_conversion_failure_publishes_no_receipt(context, monkeypatch):
    """转换失败保留工作目录，不发布成功回执，也不修改原件。"""
    def fail(*args):
        """模拟受控转换失败，不调用进程。"""
        raise MinerUError("OFFICE_PDF_FAILED", "模拟安全失败")

    monkeypatch.setattr(tool, "run_converter", fail)
    with pytest.raises(MinerUError):
        tool.ensure_conversion(context.document, context.request, context.settings)
    directory = tool.directory_for(context.settings, context.request["identity"])
    assert not (directory / "receipt.json").exists() and list(directory.glob("work-*"))


@pytest.mark.parametrize("timeout", [False, True])
def test_converter_uses_private_profile_no_shell_and_no_application_secrets(context, monkeypatch, timeout):
    """仅检查启动参数和独立profile，禁宏/外链/重算；超时不发布结果、不杀其他实例。"""
    captured, stopped = {}, []
    work = context.source.parent / "work"
    work.mkdir()
    monkeypatch.setenv("MINERU_API_TOKEN", "PRIVATE_TOKEN")
    monkeypatch.setenv("DATABASE_URL", "PRIVATE_DATABASE")
    monkeypatch.setattr(tool, "validate_file_content", lambda *args: None)

    class Process:
        """内存进程Stub，永不启动实际软件。"""

        def wait(self, *, timeout):
            """模拟完成或超时；参数必须来自冻结配置。"""
            assert timeout == context.request["timeout_seconds"]
            if captured["timeout"]:
                raise subprocess.TimeoutExpired("mock", timeout)
            (work / "output/input.pdf").write_bytes(b"OUTPUT_SENTINEL")
            return 0

    def popen(command, **options):
        """保存启动选项而不执行LibreOffice。"""
        captured.update(command=command, options=options, timeout=timeout)
        return Process()

    monkeypatch.setattr(tool.subprocess, "Popen", popen)
    monkeypatch.setattr(tool, "stop_conversion", lambda process: stopped.append(process))
    if timeout:
        with pytest.raises(MinerUError) as error:
            tool.run_converter(context.request, context.source, work, "docx")
        assert error.value.code == "OFFICE_PDF_TIMEOUT" and len(stopped) == 1
    else:
        assert tool.run_converter(context.request, context.source, work, "docx") == work / "output/input.pdf"
        assert not stopped
    command, options = captured["command"], captured["options"]
    assert "--headless" in command and "pdf:writer_pdf_Export" in command and options["shell"] is False
    assert f"-env:UserInstallation={(work / 'profile').as_uri()}" in command
    assert not {"MINERU_API_TOKEN", "DATABASE_URL"} & options["env"].keys()
    assert (work / "profile/user/registrymodifications.xcu").read_bytes() == tool.PROFILE
    assert b"DisableMacrosExecution" in tool.PROFILE and b"DisableActiveContent" in tool.PROFILE
    assert b"OOXMLRecalcMode" in tool.PROFILE and context.source.read_bytes() == (work / "input.docx").read_bytes()


@pytest.mark.parametrize("pages,expected", [(180, 1), (181, 2), (200, 2), (382, 3)])
def test_new_plan_never_fills_cloud_200_page_limit(context, pages, expected):
    """新计划最多180页，连续覆盖所有页；使用字节Stub而非生成PDF样本。"""
    def render(first, last, target):
        """只输出区间字节标识，不生成真实PDF。"""
        target.write_bytes(f"{first}:{last}".encode())

    parts = split.build_parts(pages, context.source.parent / "pieces", render)
    assert len(parts) == expected
    assert all(part["page_end"] - part["page_start"] < 180 for part in parts)
    assert [page for part in parts for page in range(part["page_start"], part["page_end"] + 1)] == list(range(1, pages + 1))
    assert split.valid_policy(split.policy(max_pages=200)) and split.valid_policy(split.policy())
    assert not split.valid_policy(split.policy(max_pages=201))


def test_office_cache_identity_is_separate_and_legacy_signals_do_not_change(context):
    """新Office缓存绑定转换引擎与180页策略；旧空字段序列化不改变冻结身份。"""
    document, settings = context.document, context.settings
    old = recipe(document, settings, "mineru_whole")
    new = recipe(document, settings, "mineru_whole", office_pdf=context.request["identity"], pdf_split_policy=split.policy())
    assert key_for(old) != key_for(new) and new["office_pdf"] == context.request["identity"]
    assert "office_pdf_sha256" not in StructuralSignals().model_dump(mode="json")
    assert "office_pdf" not in old and "pdf_split" not in old


def test_office_bound_pages_are_logical_and_missing_pages_remain_partial(context, monkeypatch):
    """有回执才允许超过200逻辑页；缺页/非空坐标不得被当作完整Office结果。"""
    _, receipt, _ = publish_stub(context, monkeypatch)
    decision = RouteDecision(file_type="docx", parse_mode="mineru", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals())
    bounded = tool.bound_decision(decision, receipt)
    parsed = ParsedDocument(doc_id=context.document.doc_id, file_name="original.docx", file_type="docx",
        quality=ParseQuality(), blocks=[ParsedBlock(block_id=uuid4(), block_type="text", page=201,
            text="SOURCE_SENTINEL", source="mineru_cloud")])
    with pytest.raises(CompletenessError):
        guard_result(parsed, decision)
    guard_result(parsed, bounded)
    assert parsed.quality.content == "PARTIAL" and parsed.quality.missing_pages == list(range(1, 201))
    assert bounded.signals.office_pdf_sha256 == receipt["pdf_sha256"] and decision.signals.page_count is None
    parsed.blocks[0].bbox = [0, 0, 1, 1]
    with pytest.raises(CompletenessError):
        guard_result(parsed, bounded)


def test_office_merge_maps_full_logical_range_but_never_original_bbox(context):
    """片内PDF坐标不冒充原Office坐标，页码按转换后起点平移且不修改中间结果。"""
    parts, results, identifier = [], [], uuid4()
    for ordinal, start, end in [(1, 1, 180), (2, 181, 201)]:
        value = digest(str(ordinal).encode())
        task = uuid5(identifier, f"{split.VERSION}:{ordinal}:{start}:{end}:{value}")
        parts.append(split.PDFPart(ordinal=ordinal, page_start=start, page_end=end, file_hash=value,
            size_bytes=1, task_id=task, data_id=f"dkf-{task.hex}", state="done", result={"key": value}))
        results.append(ParsedDocument(doc_id=context.document.doc_id, file_name="original.docx", file_type="pdf",
            blocks=[ParsedBlock(block_id=uuid4(), page=end - start + 1, block_type="formula", text="x+y",
                bbox=[1, 2, 3, 4], source="mineru_cloud")]))
    manifest = split.SplitManifest(doc_id=context.document.doc_id, file_hash=context.document.file_hash,
        page_count=201, policy=split.policy(), parts=parts)
    before = deepcopy(results)
    merged = split.merge_parts(context.document, manifest, results)
    assert merged.file_type == "docx" and [block.page for block in merged.blocks] == [180, 201]
    assert all(block.bbox is None and block.text == "x+y" for block in merged.blocks) and results == before


def test_budget_is_checked_before_any_batch_or_manifest_activation(context, monkeypatch):
    """实际片数超显式预算时本地规划失败，原件/旧Batch未被改动，不启动Provider。"""
    path, receipt, _ = publish_stub(context, monkeypatch)
    job = SimpleNamespace(job_id=uuid4(), parse_options={"_office_pdf": receipt,
        "_office_pdf_recovery": {"max_new_parts": 1},
        "_pdf_split_request": {"policy": split.policy(), "page_count": 201}})

    class PDF:
        """仅提供页数和上下文接口，不打开占位字节。"""
        page_count, needs_pass, is_repaired = 201, False, False

        def __enter__(self):
            """返回内存页数Stub。"""
            return self

        def __exit__(self, *args):
            """不持有文档句柄，无需清理。"""

    monkeypatch.setattr(split.pymupdf, "open", lambda *args: PDF())
    monkeypatch.setattr(split, "build_parts", lambda *args, **kwargs: [{"ordinal": 1}, {"ordinal": 2}])
    with pytest.raises(MinerUError) as error:
        split.prepare_manifest(context.document, job, context.settings, path)
    assert error.value.code == "OFFICE_PDF_BUDGET" and "_pdf_split" not in job.parse_options


def test_cached_reindex_does_not_require_libreoffice(context, monkeypatch):
    """Reindex只读已发布缓存，不能因本机引擎缺失重转换或要求新云费用。"""
    parsed = ParsedDocument(doc_id=context.document.doc_id, file_name=context.document.file_name, file_type="docx",
        quality=ParseQuality(content="PARTIAL", warnings=["EXTERNAL_SOURCE_UNVERIFIED"]),
        blocks=[ParsedBlock(block_id=uuid4(), page=201, block_type="text", text="CACHE_SENTINEL", source="mineru_cloud")])
    value = recipe(context.document, context.settings, "mineru_whole",
        office_pdf=context.request["identity"], pdf_split_policy=split.policy())
    cache = ParseCache(context.settings)
    cache.save_document(context.document, value, parsed, {})
    monkeypatch.setattr(tool, "conversion_request", lambda *args: pytest.fail("Reindex不能调用转换器"))
    assert cache.replay(context.document, context.settings) == parsed


def test_worker_binds_converted_pdf_before_planning_and_rechecks_it_on_upload(context, monkeypatch):
    """CPU先绑定真实转换摘要再规划；上传前发现派生PDF变化即停，不退回原Office直传。"""
    path, receipt, _ = publish_stub(context, monkeypatch)
    decision = RouteDecision(file_type="docx", parse_mode="mineru", route_strategy="mineru_whole",
        reason_codes=("EXPLICIT_MINERU",), signals=StructuralSignals())
    job = SimpleNamespace(job_id=uuid4(), file_hash=context.document.file_hash,
        parse_options={"_office_pdf_request": context.request, "_cache_guard": decision.model_dump(mode="json"),
            "_pdf_split_request": {"policy": split.policy(), "page_count": None}})
    calls = []

    def prepare(doc, task, settings, actual_source):
        """验证生产规划器接到转换PDF而非原Office；只写分片字节哨兵。"""
        assert actual_source == path and task.parse_options["_pdf_split_request"]["page_count"] == 201
        rows = []
        for ordinal, first, last in [(1, 1, 180), (2, 181, 201)]:
            piece = split.part_path(ParseCache(settings), doc.doc_id, task.job_id, ordinal)
            piece.parent.mkdir(parents=True, exist_ok=True)
            piece.write_bytes(f"PIECE_{ordinal}".encode())
            value = tool.file_hash(piece)
            identity = uuid5(task.job_id, f"{split.VERSION}:{ordinal}:{first}:{last}:{value}")
            rows.append(split.PDFPart(ordinal=ordinal, page_start=first, page_end=last, file_hash=value,
                size_bytes=piece.stat().st_size, task_id=identity, data_id=f"dkf-{identity.hex}"))
        return split.SplitManifest(doc_id=doc.doc_id, file_hash=doc.file_hash, page_count=201, policy=split.policy(), parts=rows)

    monkeypatch.setattr(workflow, "source_path", lambda doc: context.source)
    monkeypatch.setattr(workflow, "check_source_hash", lambda *args: None)
    monkeypatch.setattr(workflow, "prepare_manifest", prepare)
    session = SimpleNamespace(commit=lambda: calls.append("commit"))
    workflow.ensure_plan(session, context.document, job, context.settings)
    assert calls == ["commit"] and job.parse_options["_office_pdf"] == receipt
    assert job.parse_options["_cache_guard"]["signals"]["office_pdf_sha256"] == receipt["pdf_sha256"]
    original_piece = workflow.source_for_part(context.document, job, context.settings)
    assert original_piece.path.suffix == ".pdf" and original_piece.file_hash != context.document.file_hash
    path.write_bytes(b"TAMPERED_PDF")
    with pytest.raises(MinerUError) as error:
        workflow.source_for_part(context.document, job, context.settings)
    assert error.value.code == "OFFICE_PDF_CHANGED"
    job.parse_options = job.parse_options | {"_office_pdf_recovery": {"max_new_parts": 1}}
    with pytest.raises(MinerUError) as error:
        workflow.ensure_plan(session, context.document, job, context.settings)
    assert error.value.code == "OFFICE_PDF_BUDGET" and calls == ["commit"]
