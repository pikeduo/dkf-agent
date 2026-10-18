"""Office保存快照回归：只用XML元数据、Parser Stub及临时JSON缓存，由用户执行。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from lxml import etree

from app.core.config import Settings
from app.parsers.common import ParserError
from app.parsers.routing import inspector
from app.parsers.routing.contracts import OFFICE_SNAPSHOT_VERSION, Inspection, StructuralSignals
from app.parsers.routing.guard import CompletenessError, OFFICE_SNAPSHOT_WARNINGS, guard_result
from app.parsers.routing.router import decide_route
from app.schemas.parsed_document import ParsedBlock, ParsedDocument, ParseQuality
from app.services import parse_cache, routed_processing, xlsx_primary
from test_stage33_office_recovery import PackageStub, REL, relationships


def chart_tree():
    """构造含稀疏保存点的图表元数据，不产生Office原件或调用文档Parser。"""
    return etree.fromstring(f'''<c:chartSpace xmlns:c="{inspector.C}" xmlns:r="{REL[:-1]}">
      <c:chart><c:plotArea><c:barChart><c:ser><c:val><c:numRef>
        <c:f>PRIVATE_FORMULA</c:f><c:numCache><c:ptCount val="5"/>
          <c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="4"><c:v>9</c:v></c:pt>
        </c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart>
      <c:externalData r:id="source"/>
    </c:chartSpace>''')


def package_stub(monkeypatch, root, owner, tree, rels, *extra):
    """固定关系所属部件及包内目标，禁止任何未显式替代的文件读取或网络访问。"""
    directory, filename = owner.rsplit("/", 1)
    rel_name = directory + "/_rels/" + filename + ".rels"
    package = PackageStub([owner, rel_name, *extra])
    parts = {owner: tree, rel_name: rels}
    monkeypatch.setattr(inspector, "xml_part", lambda archive, name, limit: parts[name])
    return package


@pytest.mark.parametrize("root", ["word", "ppt"])
def test_saved_chart_update_uses_local_sparse_cache_only(monkeypatch, root):
    """复现DOCX/PPTX外部图表更新源；稀疏保存点可用，但不补点或放宽旧严格入口。"""
    tree = chart_tree()
    before = etree.tostring(tree)
    package = package_stub(monkeypatch, root, root + "/charts/chart1.xml", tree,
        relationships({"Id": "source", "Type": REL + "oleObject", "TargetMode": "External",
                       "Target": "https://private.invalid/book"}))
    assert inspector.external_asset_summary(package, root, 4096, saved_snapshot=True) == (False, 1)
    assert inspector.missing_external_assets(package, root, 4096) is True
    assert etree.tostring(tree) == before
    package.read.assert_not_called()


@pytest.mark.parametrize("damage", ["cache", "series", "count", "index", "duplicate", "value",
                                     "empty_value", "unrelated_ref", "unknown_type", "unknown_mode"])
def test_saved_chart_exception_keeps_source_and_structure_checks(monkeypatch, damage):
    """保存快照不是忽略所有外链：无缓存、坏索引、未知关系及内容引用仍拒绝。"""
    tree = chart_tree()
    rels = relationships({"Id": "source", "Type": REL + "oleObject", "TargetMode": "External",
                          "Target": "https://private.invalid/book"})
    cache = tree.find(f".//{{{inspector.C}}}numCache")
    if damage == "cache":
        cache.getparent().remove(cache)
    elif damage == "series":
        series = tree.find(f".//{{{inspector.C}}}ser")
        series.getparent().remove(series)
    elif damage == "count":
        cache[0].set("val", "not-a-count")
    elif damage == "index":
        cache[1].set("idx", "5")
    elif damage == "duplicate":
        cache.append(deepcopy(cache[1]))
    elif damage == "value":
        cache[1].remove(cache[1][0])
    elif damage == "empty_value":
        cache[1][0].text = ""
        cache[2][0].text = ""
    elif damage == "unrelated_ref":
        etree.SubElement(tree, f"{{{inspector.C}}}other", {f"{{{REL[:-1]}}}id": "source"})
    elif damage == "unknown_type":
        rels[0].set("Type", "https://unknown.invalid/oleObject")
    else:
        rels[0].set("TargetMode", "Unexpected")
    package = package_stub(monkeypatch, "word", "word/charts/chart1.xml", tree, rels)
    assert inspector.external_asset_summary(package, "word", 4096, saved_snapshot=True) == (True, 0)
    package.read.assert_not_called()


@pytest.mark.parametrize("alternate", [False, True])
def test_external_workbook_update_metadata_does_not_require_source_fetch(monkeypatch, alternate):
    """外部工作簿及新版备用URL只表示更新依赖，原公式须由Native另行保留。"""
    tree = etree.fromstring(f'''<externalLink xmlns="{inspector.S}" xmlns:r="{REL[:-1]}"
      xmlns:x="{inspector.EXTERNAL_LINKS}"><externalBook r:id="source">
      <x:alternateUrls><x:absoluteUrl r:id="alternate"/></x:alternateUrls>
      </externalBook></externalLink>''')
    entries = [{"Id": "source", "Type": REL + "externalLinkPath", "TargetMode": "External",
                "Target": "file:///private/source.xlsx"}]
    if alternate:
        entries.append(dict(entries[0], Id="alternate"))
    package = package_stub(monkeypatch, "xl", "xl/externalLinks/externalLink1.xml", tree,
                           relationships(*entries))
    assert inspector.external_asset_summary(package, "xl", 4096, saved_snapshot=True) == (False, len(entries))
    package.read.assert_not_called()


@pytest.mark.parametrize("damage", ["namespace", "owner", "unreferenced"])
def test_unknown_external_workbook_reference_is_still_missing(monkeypatch, damage):
    """精确类型、拥有者、命名空间及真实引用缺一不可，不放行未知内容关系。"""
    namespace = "https://unknown.invalid" if damage == "namespace" else inspector.S
    tree = etree.fromstring(f'''<externalLink xmlns="{namespace}" xmlns:r="{REL[:-1]}">
        <externalBook r:id="{'other' if damage == 'unreferenced' else 'source'}"/></externalLink>''')
    owner = "xl/workbook.xml" if damage == "owner" else "xl/externalLinks/externalLink1.xml"
    package = package_stub(monkeypatch, "xl", owner, tree,
        relationships({"Id": "source", "Type": REL + "externalLinkPath", "TargetMode": "External",
                       "Target": "file:///private/source.xlsx"}))
    assert inspector.external_asset_summary(package, "xl", 4096, saved_snapshot=True) == (True, 0)


@pytest.mark.parametrize("damage", [None, "no_embed", "external_embed", "wrong_type", "missing_target"])
def test_linked_image_requires_same_element_real_embedded_copy(monkeypatch, damage):
    """链接图片只能使用同元素已有包内副本；外链单独存在或内部缺件仍失败。"""
    tree = etree.fromstring(f'''<root xmlns:a="{inspector.A}" xmlns:r="{REL[:-1]}">
        <a:blip r:link="source" r:embed="copy"/></root>''')
    entries = [{"Id": "source", "Type": REL + "image", "TargetMode": "External",
                "Target": "https://private.invalid/image"},
               {"Id": "copy", "Type": REL + "image", "Target": "media/image.png"}]
    if damage == "no_embed":
        tree[0].attrib.pop(f"{{{REL[:-1]}}}embed")
    elif damage == "external_embed":
        entries[1].update(TargetMode="External", Target="https://private.invalid/copy")
    elif damage == "wrong_type":
        entries[1]["Type"] = REL + "oleObject"
    package = package_stub(monkeypatch, "word", "word/document.xml", tree, relationships(*entries),
        *([] if damage == "missing_target" else ["word/media/image.png"]))
    if damage == "missing_target":
        with pytest.raises(ParserError):
            inspector.external_asset_summary(package, "word", 4096, saved_snapshot=True)
    else:
        assert inspector.external_asset_summary(package, "word", 4096, saved_snapshot=True) == (
            (False, 1) if damage is None else (True, 0))
    package.read.assert_not_called()


def test_snapshot_does_not_hide_duplicate_relationship_or_xml_budget(monkeypatch):
    """快照例外继续约束重复关系和总XML预算，不能无限读取或掩盖包损坏。"""
    entry = {"Id": "source", "Type": REL + "oleObject", "TargetMode": "External", "Target": "private"}
    package = package_stub(monkeypatch, "word", "word/charts/chart1.xml", chart_tree(),
                           relationships(entry, entry))
    with pytest.raises(ParserError):
        inspector.external_asset_summary(package, "word", 4096, saved_snapshot=True)
    package.size = 65 * 1024**2
    with pytest.raises(inspector.InspectionLimitError):
        inspector.external_asset_summary(package, "word", 4096, saved_snapshot=True)


@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
@pytest.mark.parametrize("mode", ["auto", "mineru"])
def test_saved_office_routes_whole_without_relaxing_missing_assets(kind, mode):
    """三个格式的保存快照均可云解析；外部源未核验，真实缺件始终优先拒绝。"""
    signals = StructuralSignals(office_snapshot_rel_count=2)
    risks = ("OFFICE_SAVED_SNAPSHOT",) + (("XLSX_HAS_EXTERNAL_LINK",) if kind == "xlsx" else ())
    decision = decide_route(kind, mode, Inspection(signals=signals, risks=risks))
    assert decision.route_strategy == "mineru_whole"
    assert decide_route(kind, mode, Inspection(signals=signals,
        risks=risks + ("EXTERNAL_ATTACHMENT_MISSING",))).route_strategy == "failed"
    if kind == "xlsx":
        assert decide_route(kind, mode, Inspection(risks=("XLSX_HAS_EXTERNAL_LINK",))).route_strategy == "failed"
    assert decide_route(kind, "native", Inspection(signals=signals, risks=risks)).route_strategy == "native"


def parsed_office(kind="docx", source="mineru_cloud"):
    """构造统一结果元数据，避免调用真实Parser、MinerU或Embedding。"""
    return ParsedDocument(doc_id=uuid4(), file_name="snapshot." + kind, file_type=kind,
        blocks=[ParsedBlock(block_id=uuid4(), page=1, section="Sheet1" if kind == "xlsx" else "",
                            block_type="table", text="='[1]Sheet1'!A1", source=source)])


def test_snapshot_quality_preserves_blocks_and_previous_gaps_idempotently():
    """质量缺口只追加安全摘要；块/原式/来源及先前缺页视觉诊断全部保留。"""
    parsed = parsed_office()
    parsed.quality = ParseQuality(structure="DEGRADED", evidence="PARTIAL", missing_pages=[2],
        warnings=["PREVIOUS_WARNING"], visual_review_pages=[3], diagnostics=[{"reason": "TABLE_ROWS"}])
    before = [block.model_dump_json() for block in parsed.blocks]
    decision = decide_route("docx", "auto", Inspection(signals=StructuralSignals(office_snapshot_rel_count=3),
        risks=("OFFICE_SAVED_SNAPSHOT",)))
    guard_result(parsed, decision)
    snapshot = parsed.model_dump_json()
    guard_result(parsed, decision)
    assert parsed.model_dump_json() == snapshot and parsed.quality.content == "PARTIAL"
    assert [block.model_dump_json() for block in parsed.blocks] == before
    assert parsed.quality.missing_pages == [2] and parsed.quality.visual_review_pages == [3]
    assert parsed.quality.structure == "DEGRADED" and parsed.quality.evidence == "PARTIAL"
    assert parsed.quality.warnings == ["PREVIOUS_WARNING", *OFFICE_SNAPSHOT_WARNINGS]
    assert parsed.quality.diagnostics[-1] == {"stage": "router.office_snapshot",
                                           "reason": "EXTERNAL_SOURCE_UNVERIFIED", "count": 3}
    parsed.blocks[0].bbox = [0.0, 0.0, 1.0, 1.0]
    with pytest.raises(CompletenessError):
        guard_result(parsed, decision)


def test_snapshot_whole_keeps_external_formula_and_native_checkpoint(monkeypatch):
    """Whole原式和云数值分别保留，Native Hash可复验，质量不得升级COMPLETE。"""
    from io import BytesIO

    native = parsed_office("xlsx", "native_parser")
    document = SimpleNamespace(doc_id=native.doc_id, file_name=native.file_name, file_type="xlsx",
                               file_hash=parse_cache.digest(b"only-source-stub"))
    signals = StructuralSignals(sheet_count=1, nonempty_sheet_count=1, formula_count=1,
                                office_snapshot_rel_count=1)
    inspection = Inspection(signals=signals, risks=("XLSX_HAS_EXTERNAL_LINK", "OFFICE_SAVED_SNAPSHOT"))
    monkeypatch.setattr(xlsx_primary, "source_path", lambda doc: SimpleNamespace(
        open=lambda mode: BytesIO(b"only-source-stub")))
    monkeypatch.setattr(xlsx_primary, "inspect_document", lambda *args: inspection)
    monkeypatch.setattr(xlsx_primary.XlsxParser, "parse", lambda self, **kwargs: deepcopy(native))
    decision = decide_route("xlsx", "auto", inspection)
    checkpoint = xlsx_primary.prepare_primary(document, None, decision)
    cloud = native.model_copy(deep=True, update={"blocks": [native.blocks[0].model_copy(
        update={"block_id": uuid4(), "text": "42", "source": "mineru_cloud"})]})
    result = xlsx_primary.complete_primary(document, checkpoint, cloud, decision)
    assert result.blocks[0] == native.blocks[0] and result.blocks[1].text == "42"
    assert result.quality.content == "PARTIAL" and result.blocks[0].text == "='[1]Sheet1'!A1"
    assert xlsx_primary.complete_primary(document, checkpoint, cloud, decision) == result
    monkeypatch.setattr(xlsx_primary, "inspect_document", lambda *args: Inspection(signals=signals,
        risks=inspection.risks + ("EXTERNAL_ATTACHMENT_MISSING",)))
    with pytest.raises(xlsx_primary.MinerUError, match="XLSX_EXTERNAL_ASSET_MISSING"):
        xlsx_primary.prepare_primary(document, None, decision)


def test_snapshot_cache_isolated_and_replay_cannot_promote_complete(tmp_path):
    """新策略仅隔离受影响缓存；JSON回放保持PARTIAL，不调用Parser/网络/业务数据库。"""
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    parsed = parsed_office()
    document = SimpleNamespace(doc_id=parsed.doc_id, file_name=parsed.file_name,
                               file_type=parsed.file_type, file_hash="a" * 64)
    signals = StructuralSignals(office_snapshot_rel_count=1)
    decision = decide_route("docx", "auto", Inspection(signals=signals, risks=("OFFICE_SAVED_SNAPSHOT",)))
    old = parse_cache.recipe(document, settings, "mineru_whole")
    new = parse_cache.recipe(document, settings, "mineru_whole", office_snapshot_version=OFFICE_SNAPSHOT_VERSION)
    assert new == old | {"office_snapshot_version": OFFICE_SNAPSHOT_VERSION}
    assert parse_cache.key_for(old) != parse_cache.key_for(new)
    assert "office_snapshot_rel_count" not in StructuralSignals().model_dump(mode="json")
    guard_result(parsed, decision)
    cache = parse_cache.ParseCache(settings)
    cache.save_document(document, new, parsed)
    replayed = cache.replay(document, settings)
    assert replayed == parsed and replayed.quality.content == "PARTIAL"
    replayed.quality.content = "COMPLETE"
    with pytest.raises(parse_cache.CacheError):
        parse_cache.guard_cached(replayed, decision)
    cache.save_document(document, new, replayed)
    with pytest.raises(parse_cache.CacheError):
        cache.replay(document, settings)
    with pytest.raises(parse_cache.CacheError):
        parse_cache.recipe(document, settings, "mineru_whole", office_snapshot_version="unrecognized")


def test_snapshot_route_log_only_emits_safe_count(monkeypatch):
    """更新依赖日志只含理由码与数量，不泄漏XML、原式、外部地址或路径。"""
    event = MagicMock()
    monkeypatch.setattr(routed_processing, "log_event", event)
    decision = decide_route("docx", "auto", Inspection(signals=StructuralSignals(office_snapshot_rel_count=7),
        risks=("OFFICE_SAVED_SNAPSHOT",)))
    routed_processing.log_decision("router.decided", decision, uuid4(), uuid4())
    fields = event.call_args.kwargs
    assert fields["item_count"] == 7 and fields["reason_codes"] == "OFFICE_SAVED_SNAPSHOT"
    assert "PRIVATE" not in str(fields) and "https://" not in str(fields)


@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
def test_job_freezes_snapshot_cache_policy_before_any_cloud_submission(monkeypatch, kind):
    """Mock提交前Job创建，确认保存快照版本和范围同时冻结，不读取真实Office或访问云。"""
    from io import BytesIO
    from app.services.mineru import jobs
    from app.services import document_management

    settings = Settings(_env_file=None)
    document = SimpleNamespace(doc_id=uuid4(), file_name="snapshot." + kind, file_type=kind,
        file_hash=parse_cache.digest(b"only-source-stub"), processing_task_id=uuid4(), status="FAILED")
    inspection = Inspection(signals=StructuralSignals(office_snapshot_rel_count=1),
                            risks=("OFFICE_SAVED_SNAPSHOT",))
    decision = decide_route(kind, "auto", inspection)
    session = MagicMock()
    session.scalar.side_effect = [document, None]
    monkeypatch.setattr(jobs, "source_path", lambda doc: SimpleNamespace(
        open=lambda mode: BytesIO(b"only-source-stub")))
    monkeypatch.setattr(jobs, "inspect_document", lambda *args: inspection)
    monkeypatch.setattr(jobs, "prepare_primary", lambda *args: {"only": "primary-stub"})
    cache = MagicMock()
    cache.document.return_value = None
    monkeypatch.setattr(jobs, "ParseCache", lambda *args: cache)
    monkeypatch.setattr(jobs, "lock_keys", MagicMock())
    monkeypatch.setattr(jobs, "claim_miss", MagicMock())
    monkeypatch.setattr(jobs, "validate_local_file", MagicMock())
    monkeypatch.setattr(document_management, "rebind_cloud_management", MagicMock())
    job = jobs.create_job(session, document, settings, route_decision=decision.model_dump(mode="json"))
    assert job.state == "waiting-file" and not job.submission_attempted and job.batch_id is None
    assert job.parse_options["_cache_recipe"]["office_snapshot_version"] == OFFICE_SNAPSHOT_VERSION
    assert job.parse_options["_cache_guard"]["signals"]["office_snapshot_rel_count"] == 1
    assert not job.upload_complete and document.status == "PARSING"
    session.commit.assert_not_called()
