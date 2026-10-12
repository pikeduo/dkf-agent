"""预置导入单元用例，仅用户执行；使用路径/会话/状态 Stub，不生成真实文档样本。"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import preset_import as imports
from app.services import preset_manifest as manifest
from app.services.preset_import import ImportItem
from app.services.preset_manifest import PresetFile, PresetImportError
from scripts import import_knowledge_base as cli


def candidate(index, file_type="pdf", tags=()):
    """只提供虚拟路径、摘要和标签元数据，不创建或读取原始文件。"""

    path = Path(f"virtual-{index}.{file_type}").absolute()
    return PresetFile(path, path.name, file_type, f"{index:064x}", frozenset(tags))


def coverage_files():
    """准备十份纯元数据声明，PDF/旧 Office/现代格式均有覆盖，不生成文件。"""

    types = ["doc", "xls", "ppt", "txt", "md", "csv", "xlsx", "pptx", "pdf", "pdf"]
    return [candidate(index, kind, {"text_pdf"} if index == 8 else
                      manifest.TAGS - {"text_pdf"} if index == 9 else ())
            for index, kind in enumerate(types)]


def test_coverage_counts_distinct_content_and_modern_types():
    """同内容改名不凑数量，必须覆盖 DOC/XLS/PPT、现代多格式和全部内容标签。"""

    files = coverage_files()
    manifest.validate_coverage(files)
    for invalid in (files[:9], files[1:], [files[0]] * 10,
                    [candidate(index, "pdf", {"reading_order"}) for index in range(10)]):
        with pytest.raises(PresetImportError, match="PRESET_COVERAGE"):
            manifest.validate_coverage(invalid)


@pytest.mark.parametrize("name", ["../outside.pdf", "/outside.pdf", "C:/outside.pdf", "a\\b.pdf", "a/./b.pdf"])
def test_manifest_rejects_unsafe_paths(name):
    """清单不能越界或以 Windows 路径偷换根目录，不输出本机路径。"""

    with pytest.raises(PresetImportError, match="PRESET_MANIFEST") as error:
        manifest.manifest_entries({"version": 1, "documents": [{"file": name, "tags": []}]})
    assert name not in str(error.value)


def test_manifest_rejects_duplicates_unknown_tags_and_route_overrides():
    """人工清单只声明文件与覆盖标签，不能覆盖自动路由或夹带额外供应商参数。"""

    row = {"file": "one.pdf", "tags": ["text_pdf"]}
    valid = {"version": 1, "documents": [row]}
    assert manifest.manifest_entries(valid) == {"one.pdf": frozenset({"text_pdf"})}
    for rows in ([row, row], [{"file": "one.pdf", "tags": ["fake"]}], [row | {"parse_mode": "native"}]):
        with pytest.raises(PresetImportError):
            manifest.manifest_entries({"version": 1, "documents": rows})


def test_pdf_content_tags_cannot_be_claimed_by_non_pdf():
    """文本/扫描 PDF 必须分别声明为 PDF，标签只是声明，不自动识别正文质量。"""

    files = coverage_files()
    files[8] = candidate(8, "txt", {"text_pdf"})
    with pytest.raises(PresetImportError):
        manifest.validate_coverage(files)


def test_cli_defaults_to_preflight_without_import(monkeypatch, capsys):
    """默认与显式 dry-run 不连接数据库/Broker、不配置写盘日志或实际导入。"""

    def settings_stub():
        """返回纯配置 Stub，不读取真实密钥。"""

        return SimpleNamespace()

    def preflight_stub(directory, settings):
        """代替目录读取，不生成/读取任何样本。"""

        return coverage_files()

    def forbidden(*args, **kwargs):
        """日志配置或业务导入均不允许在预检模式触发。"""

        pytest.fail("dry-run 不应写入日志/数据库或投递任务")

    monkeypatch.setattr(cli, "get_settings", settings_stub)
    monkeypatch.setattr(cli, "preflight", preflight_stub)
    monkeypatch.setattr(cli, "configure_logging", forbidden)
    monkeypatch.setattr(imports, "run_import", forbidden)
    assert cli.main([]) == 0
    assert cli.main(["--dry-run"]) == 0
    assert "只读预检结束" in capsys.readouterr().out


def test_cli_requires_explicit_cloud_consent(monkeypatch):
    """正式导入缺少外传/费用确认时，在目录读取及任何写入之前拒绝。"""

    def forbidden(*args, **kwargs):
        """未确认费用时不应读取配置或进入正式业务。"""

        pytest.fail("未确认云调用不得执行")

    monkeypatch.setattr(cli, "get_settings", forbidden)
    assert cli.main(["--execute"]) == 2


def test_existing_document_reuses_id_without_rewriting_source():
    """重复预置内容不新建或重发任务；普通上传记录冲突不偷偷改成 preset。"""

    identifier = uuid4()
    row = SimpleNamespace(doc_id=identifier, kb_id=uuid4(), file_type="pdf", source_type="preset", status="READY")
    item = imports.existing_item(row, candidate(1))
    assert item.doc_id == identifier and item.status == "READY"
    row.source_type = "uploaded"
    with pytest.raises(PresetImportError, match="PRESET_SOURCE_CONFLICT") as error:
        imports.existing_item(row, candidate(1))
    assert error.value.doc_id == identifier and row.source_type == "uploaded"


class SessionStub:
    """内存行锁/事务 Stub，不实际建立数据库连接。"""

    def __init__(self, row):
        """保存纯内存 Document 表示，记录事务已退出后才允许投递。"""

        self.row, self.in_transaction = row, False

    def begin(self):
        """返回事务上下文 Stub，由 __exit__ 标记模拟已提交。"""

        return self

    def __enter__(self):
        """模拟进入事务，不执行 SQL。"""

        self.in_transaction = True
        return self

    def __exit__(self, *args):
        """模拟事务退出，后续 publisher 可验证投递时机。"""

        self.in_transaction = False

    def execute(self, *args):
        """忽略会话内临时时间限制语句，不实际修改数据库。"""

        return None

    def scalar(self, *args):
        """返回已加载的内存文档，不执行查询。"""

        return self.row


@pytest.mark.parametrize("status", ["CHUNKING", "EMBEDDING", "INDEXING"])
def test_successful_stage_dispatches_once_after_checkpoint_commit(monkeypatch, status):
    """阶段只能在未建立检查点时投递，提交后投递且第二轮不重复。"""

    row = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), source_type="preset", status=status)
    attribute = imports.NEXT_STEPS[status][0]
    setattr(row, attribute, None)
    session = SessionStub(row)
    messages = []

    def prepare(session, document, *args):
        """仅构造稳定检查点编号，不执行切片、模型或 SQL。"""

        assert session.in_transaction
        identifier = uuid4()
        setattr(document, attribute, identifier)
        return identifier, True

    def publish(session, doc_id, task_id):
        """提交后记录投递，明确不调用实际 Broker。"""

        assert not session.in_transaction
        messages.append((doc_id, task_id))
        return True

    monkeypatch.setitem(imports.NEXT_STEPS, status, (attribute, prepare, publish))
    assert imports.advance_document(session, row.kb_id, row.doc_id, SimpleNamespace()) == status
    assert imports.advance_document(session, row.kb_id, row.doc_id, SimpleNamespace()) == status
    assert len(messages) == 1


@pytest.mark.parametrize("status", ["UPLOADED", "PARSING", "OCR_PROCESSING", "FAILED"])
def test_active_or_failed_parse_does_not_resubmit(monkeypatch, status):
    """活动/排队解析不重复投递，FAILED 明确停止，不自动 Reparse 消耗云额度。"""

    row = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), source_type="preset", status=status)
    session = SessionStub(row)
    if status == "FAILED":
        with pytest.raises(PresetImportError, match="PRESET_FAILED"):
            imports.advance_document(session, row.kb_id, row.doc_id, SimpleNamespace())
    else:
        assert imports.advance_document(session, row.kb_id, row.doc_id, SimpleNamespace()) == status
    assert row.status == status


def test_bounded_wait_preserves_pending_state_and_does_not_cancel(monkeypatch):
    """无 Worker 时有界退出，不改业务状态、不取消后台任务、不重传云端。"""

    now = [0]
    item = ImportItem("未运行任务.pdf", uuid4(), "UPLOADED")

    def clock():
        """返回人工推进的单调时间，不读取系统时钟。"""

        return now[0]

    def pause(seconds):
        """推进虚拟时间，测试不实际 sleep。"""

        now[0] += seconds

    def session_factory():
        """只返回空上下文，不连接基础服务。"""

        return nullcontext(None)

    def advance(*args):
        """模拟队列尚未消费，不发实际任务。"""

        return "UPLOADED"

    monkeypatch.setattr(imports, "advance_document", advance)
    imports.wait_until_ready([item], uuid4(), SimpleNamespace(), timeout=2, poll_interval=1,
                            session_factory=session_factory, clock=clock, pause=pause)
    assert item.status == "UPLOADED" and item.error_code == "PRESET_TIMEOUT" and now[0] == 2


def test_wait_advances_all_stages_and_failure_does_not_cancel_other_items(monkeypatch):
    """模拟成功阶段链与单项 FAILED，其他文档可继续 READY，不直接调用模型或云端。"""

    failed_id, good_id = uuid4(), uuid4()
    items = [ImportItem("失败项.pdf", failed_id, "UPLOADED"), ImportItem("成功项.txt", good_id, "UPLOADED")]
    states = iter(["CHUNKING", "EMBEDDING", "INDEXING", "READY"])
    observations = []

    def advance(session, kb_id, doc_id, settings):
        """只返回虚拟状态链，单项错误不修改其他文档。"""

        if doc_id == failed_id:
            raise PresetImportError("PRESET_FAILED")
        state = next(states)
        observations.append(state)
        return state

    def session_factory():
        """提供空上下文，不创建数据库连接。"""

        return nullcontext(None)

    def clock():
        """有限状态链测试使用固定时钟，不真实等待。"""

        return 0

    def pause(seconds):
        """纯状态 Stub 不需要真实暂停。"""

        return None

    monkeypatch.setattr(imports, "advance_document", advance)
    imports.wait_until_ready(items, uuid4(), SimpleNamespace(), timeout=10, poll_interval=1,
                            session_factory=session_factory, clock=clock, pause=pause)
    assert observations == ["CHUNKING", "EMBEDDING", "INDEXING", "READY"]
    assert items[0].status == "ERROR" and items[0].error_code == "PRESET_FAILED"
    assert items[1].status == "READY" and items[1].error_code is None


def test_ready_checks_existing_vectors_and_does_not_publish(monkeypatch):
    """已有 READY 不重投或推理，但必须校验入库身份及全部向量后才计入成功。"""

    row = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), source_type="preset", status="READY")
    calls = []

    def checked(document):
        """只提供已完成入库检查点摘要，不读取实际模型或文件。"""

        return {"embedding_result": {"chunk_count": 3}}

    def complete(session, document, count):
        """记录全量向量检查被调用，不生成向量或新增记录。"""

        calls.append(count)

    monkeypatch.setattr(imports, "checked_index_config", checked)
    monkeypatch.setattr(imports, "require_complete_vectors", complete)
    assert imports.advance_document(SessionStub(row), row.kb_id, row.doc_id, SimpleNamespace()) == "READY"
    assert calls == [3] and row.status == "READY"


def test_error_summary_does_not_expose_original_failure():
    """未知异常/本机路径只归类为安全错误，不把原文带入批次结果。"""

    item = ImportItem("允许摘要.txt", uuid4())
    imports.fail_item(item, RuntimeError("SECRET:/private/path?token=PRIVATE"), uuid4())
    assert item.status == "ERROR" and item.error_code == "PRESET_UNKNOWN"


def test_scan_rejects_links_before_entering_subdirectory(monkeypatch):
    """目录扫描遇链接/Junction 必须终止，不跟随外部目录或读取文档正文。"""

    root = Path.cwd()

    def walk_stub(*args, **kwargs):
        """只提供虚拟目录名称，不扫描真实素材。"""

        assert kwargs["followlinks"] is False
        return iter([(str(root), ["linked"], [])])

    def link_stub(path):
        """模拟链接元数据，不创建真实符号链接或文件。"""

        return path.name == "linked"

    monkeypatch.setattr(manifest.os, "walk", walk_stub)
    monkeypatch.setattr(manifest, "is_link", link_stub)
    with pytest.raises(PresetImportError, match="PRESET_DIRECTORY"):
        manifest.scan_files(root)


def test_preflight_requires_exact_manifest_directory_match(monkeypatch):
    """遗漏目录文件时在任何 Hash/格式或数据库操作前拒绝，不自动漏项导入。"""

    class ManifestStub:
        """内存人工清单 Stub，不创建实际 manifest 或文档文件。"""

        def is_file(self):
            """模拟已存在的清单，不查询磁盘。"""

            return True

        def stat(self):
            """返回允许大小，不读取原件。"""

            return SimpleNamespace(st_size=100)

        def read_text(self, **kwargs):
            """返回清单结构文本而非文档正文，不写任何文件。"""

            return '{"version":1,"documents":[]}'

    class RootStub:
        """只支持清单路径组合的虚拟目录。"""

        def __truediv__(self, name):
            """返回内存清单对象，避免构造真实路径。"""

            assert name == "manifest.json"
            return ManifestStub()

    def scan_stub(root):
        """声明存在一份未列入清单的文件，不读取它。"""

        return {"omitted.pdf": Path("虚拟未读.pdf")}

    def link_stub(path):
        """清单 Stub 不是链接，不访问文件系统。"""

        return False

    monkeypatch.setattr(manifest, "scan_files", scan_stub)
    monkeypatch.setattr(manifest, "is_link", link_stub)
    with pytest.raises(PresetImportError, match="PRESET_MANIFEST"):
        manifest.preflight(RootStub(), SimpleNamespace())
