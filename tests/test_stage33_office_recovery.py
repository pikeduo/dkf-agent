"""Office失败修复的离线回归；只用内存关系/SQL Stub，不解析真实文档或启动服务。"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from lxml import etree

from app.parsers import hybrid
from app.parsers.common import ParserError
from app.parsers.routing import inspector

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


class PackageStub:
    """仅提供已知关系部件名/大小，不创建ZIP或真实Office验收样本。"""

    def __init__(self, names, size=1):
        """冻结有限部件名和模拟XML大小；读取必须由测试显式提供。"""
        self.names, self.size = names, size
        self.read = MagicMock(side_effect=AssertionError("不得读取正文或外部资源"))

    def namelist(self):
        """返回包内成员名称。"""
        return self.names

    def getinfo(self, name):
        """仅返回给定部件的受限大小。"""
        assert name in self.names
        return SimpleNamespace(file_size=self.size)


def relationships(*entries):
    """提供内存关系XML，不包含任何文档正文或实际网络请求。"""
    tree = etree.Element("Relationships")
    for entry in entries:
        etree.SubElement(tree, "Relationship", **entry)
    return tree


@pytest.mark.parametrize("root,kind,mode,expected", [
    ("word", REL + "attachedTemplate", "External", False),
    ("word", REL + "hyperlink", "External", False),
    ("xl", REL + "attachedTemplate", "External", True),
    ("word", "https://untrusted.invalid/attachedTemplate", "External", True),
    ("word", REL + "image", "External", True),
    ("word", REL + "oleObject", "External", True),
    ("word", REL + "attachedTemplate", "Unexpected", True),
])
def test_only_exact_word_external_template_is_not_a_missing_content_asset(monkeypatch, root, kind, mode, expected):
    """模板例外仅限DOCX官方外部关系；图片/OLE/伪造类型及未知模式仍保留阻塞。"""
    tree = relationships({"Id": "r1", "Target": "https://private.invalid/template", "Type": kind, "TargetMode": mode})
    monkeypatch.setattr(inspector, "xml_part", lambda *args: tree)
    package = PackageStub([root + "/_rels/document.xml.rels"])
    assert inspector.missing_external_assets(package, root, 1024) is expected
    package.read.assert_not_called()


def test_template_exception_does_not_hide_missing_internal_part_or_other_external_content(monkeypatch):
    """内部缺模板仍属包损坏；同时含外部图片时不能因模板例外而宣称依赖齐全。"""
    tree = relationships({"Id": "r1", "Target": "missing.dotx", "Type": REL + "attachedTemplate"})
    monkeypatch.setattr(inspector, "xml_part", lambda *args: tree)
    package = PackageStub(["word/_rels/document.xml.rels"])
    with pytest.raises(ParserError):
        inspector.missing_external_assets(package, "word", 1024)
    tree[0].set("TargetMode", "External")
    etree.SubElement(tree, "Relationship", Id="r2", Target="https://private.invalid/image",
                     Type=REL + "image", TargetMode="External")
    assert inspector.missing_external_assets(package, "word", 1024)
    package.read.assert_not_called()


@pytest.mark.parametrize("target", ["../customXml/item1.xml", "/customXml/item1.xml"])
def test_hybrid_accepts_existing_package_custom_xml_without_reading_it(monkeypatch, target):
    """复现两份Workbook合法的根customXml关系；接受映射但不把元数据当视觉正文读取。"""
    tree = relationships({"Id": "custom", "Type": REL + "customXml", "Target": target},
                         {"Id": "sheet", "Type": REL + "worksheet", "Target": "worksheets/s1.xml"})
    monkeypatch.setattr(hybrid, "xml_part", lambda *args: tree)
    package = PackageStub(["xl/_rels/workbook.xml.rels", "customXml/item1.xml", "xl/worksheets/s1.xml"])
    assert hybrid.relationships(package, "xl/workbook.xml", 1024) == {
        "custom": "customXml/item1.xml", "sheet": "xl/worksheets/s1.xml"}
    package.read.assert_not_called()


@pytest.mark.parametrize("kind,target,mode", [
    (REL + "customXml", "../../outside.xml", "Internal"),
    (REL + "customXml", "../customXml/missing.xml", "Internal"),
    (REL + "image", "../customXml/item1.xml", "Internal"),
    (REL + "customXml", "../customXml/item1.xml#fragment", "Internal"),
    (REL + "customXml", "https://private.invalid/item1.xml", "External"),
])
def test_hybrid_custom_xml_exception_keeps_missing_path_and_external_checks(monkeypatch, kind, target, mode):
    """合法位置例外不允许缺件、包外路径、图片越界、URI片段或外链。"""
    tree = relationships({"Id": "custom", "Type": kind, "Target": target, "TargetMode": mode})
    monkeypatch.setattr(hybrid, "xml_part", lambda *args: tree)
    package = PackageStub(["xl/_rels/workbook.xml.rels", "customXml/item1.xml"])
    with pytest.raises(hybrid.HybridPreflightError):
        hybrid.relationships(package, "xl/workbook.xml", 1024)
    package.read.assert_not_called()


def test_xml_part_budget_reports_counts_before_reading_private_xml(monkeypatch):
    """超限日志显示实测字节/阈值/步骤，不读取或输出XML、路径和外部URL。"""
    package = PackageStub(["xl/worksheets/private.xml"], size=20_204_999)
    event = MagicMock()
    monkeypatch.setattr(inspector, "log_event", event)
    with pytest.raises(inspector.InspectionLimitError):
        inspector.xml_part(package, "xl/worksheets/private.xml", 16 * 1024**2)
    package.read.assert_not_called()
    event.assert_called_once_with("router.inspection_rejected", stage="router.xml_part", step="XML_PART_SIZE",
                                  size_bytes=20_204_999, limit_count=16 * 1024**2, error_code="PREFLIGHT_LIMIT")


@pytest.mark.parametrize("version,allowed", [
    ("block-chunk-v3", False), ("block-chunk-v4", True), ("block-chunk-v5", True), ("block-chunk-v6", True),
])
def test_sql_chunk_budget_matches_frozen_algorithm_before_fetching_body(monkeypatch, version, allowed):
    """旧代次保留版本预算，v6进入逐块读取；测试在正文边界停止，不产生任何真实切片。"""
    from app.services.chunk import processing, splitter

    task_id, parse_id, doc_id = uuid4(), uuid4(), uuid4()
    document = SimpleNamespace(doc_id=doc_id, kb_id=uuid4(), chunk_task_id=task_id, status="CHUNKING",
        file_type="xlsx", file_hash="a" * 64,
        processing_task_id=parse_id, chunk_config={"options": splitter.ChunkOptions(version=version).snapshot(),
                                                  "source_parse_task_id": str(parse_id)})
    session = MagicMock()
    session.scalar.return_value = document
    session.execute.return_value.one.return_value = (150, 1)
    # 在拉取正文处停止，测试不会替换Chunk、运行Embedding或访问真实数据库。
    session.scalars.side_effect = LookupError("已到达正文读取边界")
    monkeypatch.setattr(splitter, "MAX_SOURCE_CHARS", 100)
    monkeypatch.setattr(splitter, "MAX_V4_SOURCE_CHARS", 200)
    with pytest.raises(LookupError if allowed else splitter.ChunkError):
        processing.run_chunk_step(session, doc_id, task_id)
    assert session.scalars.called is allowed
    session.add.assert_not_called()


def test_new_office_limit_event_has_registered_safe_chinese_summary():
    """新事件使用集中注册摘要和既有白名单字段，不引入原文/路径日志。"""
    from app.core import business_logging as logs

    assert "Office" in logs.EVENT_LABELS["router.inspection_rejected"]


def test_hybrid_terminal_error_precedes_route_information_in_management_report(monkeypatch):
    """Hybrid拒绝后管理/阶段33首先读到真正错误，不把XLSX_HYBRID_ENABLED当原因。"""
    import hashlib
    from io import BytesIO
    from app.core.config import Settings
    from app.parsers.routing.contracts import Inspection, StructuralSignals
    from app.services import routed_processing
    from app.services.document_processing import router_failure_codes

    task_id, doc_id = uuid4(), uuid4()
    document = SimpleNamespace(doc_id=doc_id, processing_task_id=task_id, status="UPLOADED",
        file_type="xlsx", file_hash=hashlib.sha256(b"only-stub-bytes").hexdigest())
    path = SimpleNamespace(open=lambda mode: BytesIO(b"only-stub-bytes"))
    monkeypatch.setattr(routed_processing, "check_source", lambda doc: None)
    monkeypatch.setattr(routed_processing, "source_path", lambda doc: path)
    monkeypatch.setattr(routed_processing, "ParseCache", MagicMock())
    monkeypatch.setattr(routed_processing, "inspect_document", lambda *args: Inspection(
        risks=("XLSX_HAS_EMBEDDED_IMAGE",), signals=StructuralSignals(image_count=1)))
    monkeypatch.setattr(routed_processing, "prepare_checkpoint",
        MagicMock(side_effect=hybrid.HybridPreflightError("HYBRID_INVALID_PACKAGE")))
    submit = MagicMock(side_effect=AssertionError("预检失败不能创建收费Job"))
    monkeypatch.setattr(routed_processing, "create_job", submit)
    session = MagicMock()
    session.scalar.return_value = document
    result = routed_processing.run_routed_step(session, doc_id, task_id, "auto", Settings(_env_file=None))
    assert result["status"] == "failed" and document.status == "FAILED"
    assert router_failure_codes(document.error_message)[0] == "HYBRID_INVALID_PACKAGE"
    submit.assert_not_called()
