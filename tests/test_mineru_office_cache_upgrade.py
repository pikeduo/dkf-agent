"""旧Office成功缓存升级回归；仅用元数据/Adapter Stub，测试由用户手动执行。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
import hashlib
import json

import pytest

from app.core.config import Settings
from app.parsers.routing.contracts import RouteDecision, StructuralSignals
from app.parsers.routing.guard import CompletenessError
from app.schemas.parsed_document import ParseQuality, ParsedBlock, ParsedDocument
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import CacheError, ParseCache, cloud_options, key_for, recipe
from scripts import upgrade_mineru_office_cache as tool


@pytest.fixture(params=[(kind, version) for kind in ("docx", "pptx")
                       for version in ("mineru-adapter-v2", "mineru-adapter-v3")])
def context(tmp_path, monkeypatch, request):
    """创建隔离JSON缓存和成功Job快照，原件Hash/ZIP验证/Adapter均为显式Stub。"""
    kind, version = request.param
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    source = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), file_type=kind,
                             file_name="source." + kind, file_hash="a" * 64)
    target = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), file_type=kind,
        file_name="target." + kind, file_hash=source.file_hash, status="FAILED", management_config=None)
    old = recipe(source, settings, "mineru_whole") | {"normalizer_version": version}
    old.pop("content_policy_version")
    options = cloud_options(settings)
    decision = RouteDecision(parse_mode="auto", file_type=kind, route_strategy="mineru_whole",
                             reason_codes=("VISUAL_OR_LEGACY_FORMAT",), signals=StructuralSignals())
    job = SimpleNamespace(job_id=uuid4(), doc_id=source.doc_id, file_hash=source.file_hash,
        provider="mineru_cloud", model_version=options["model_version"], state="done", is_active=False,
        batch_id="original-batch", data_id="original-data", upload_complete=True, submission_attempted=True,
        options_hash=hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest(),
        parse_options=options | {"_cache_recipe": old, "_cache_key": key_for(old),
                                 "_cache_guard": decision.model_dump(mode="json")})
    original = ParsedDocument(doc_id=source.doc_id, file_name=source.file_name, file_type=kind,
        blocks=[ParsedBlock(block_id=uuid4(), page=2, block_type="text", source="mineru_cloud", text="原有正文")])
    converted = original.model_copy(update={"doc_id": target.doc_id, "file_name": target.file_name,
                                            "quality": ParseQuality(content="PARTIAL", warnings=["VISUAL_ONLY_PAGES"])})
    cache = ParseCache(settings)
    cache.save_document(source, old, original, {"raw/mineru_result.zip": b"stored-archive-stub"}, job_id=job.job_id)
    adapter = MagicMock()
    adapter.normalize.return_value = converted
    monkeypatch.setattr(tool, "MinerUResultAdapter", MagicMock(return_value=adapter))
    monkeypatch.setattr(tool, "file_hash_source", lambda document: document.file_hash)
    validate_zip = MagicMock()
    monkeypatch.setattr(tool, "validate_archive", validate_zip)
    monkeypatch.setattr(tool, "log_event", MagicMock())
    return SimpleNamespace(settings=settings, source=source, target=target, job=job,
        old=old, cache=cache, adapter=adapter, converted=converted, validate_zip=validate_zip)


def test_default_checks_identity_without_conversion_publication_or_state_changes(context):
    """默认核验不运行Adapter、不建目标latest、不改变原成功Job及目标失败状态。"""
    c = context
    before = deepcopy(vars(c.job)), deepcopy(vars(c.target))
    result = tool.upgrade_cache(c.source, c.target, c.job, c.settings)
    assert result["status"] == "IDENTITY_VALIDATED" and not result["cache_published"]
    assert not result["cloud_submitted"] and not result["database_modified"]
    c.adapter.normalize.assert_not_called()
    c.validate_zip.assert_called_once_with(b"stored-archive-stub", c.settings.mineru_result_max_mb * 1024**2)
    assert (vars(c.job), vars(c.target)) == before
    assert not c.cache.path(str(c.target.doc_id), "latest.json").exists()


def test_conversion_only_does_not_publish_or_promote_partial_to_complete(context):
    """显式转换仍不落缓存；质量缺口保留，不把Adapter通过当成READY。"""
    c = context
    result = tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True)
    assert result["status"] == "ADAPTER_VALIDATED" and result["parse_quality"]["content"] == "PARTIAL"
    assert not result["cache_published"] and c.target.status == "FAILED"
    assert not c.cache.path(str(c.target.doc_id), "latest.json").exists()


def test_publish_uses_normal_cache_and_preserves_original_job_receipts(context):
    """发布后可由原Reindex回放目标身份/原页/部分质量；源缓存和Job指针逐字不变。"""
    c = context
    pointers = [c.cache.path(str(c.source.doc_id), "latest.json"),
                c.cache.path(".jobs", str(c.job.job_id) + ".json")]
    previous = [path.read_bytes() for path in pointers]
    frozen_options = deepcopy(c.job.parse_options)
    result = tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    restored = c.cache.replay(c.target, c.settings)
    assert result["status"] == "CACHE_PUBLISHED_NOT_RECOVERED" and result["cache_published"]
    assert result["source_cache_key"] != result["target_cache_key"]
    assert restored == c.converted and restored.blocks[0].page == 2 and restored.blocks[0].bbox is None
    assert restored.quality.content == "PARTIAL" and c.target.status == "FAILED"
    assert c.job.parse_options == frozen_options and [path.read_bytes() for path in pointers] == previous
    c.adapter.normalize.assert_called_once_with(b"stored-archive-stub", doc_id=c.target.doc_id,
        file_name=c.target.file_name, file_type=c.target.file_type, geometry=None,
        identity=c.job.file_hash + ":" + c.job.options_hash)


@pytest.mark.parametrize("damage,check", [
    ("hash", "SAME_SOURCE_FILE"), ("active", "SUCCESSFUL_SOURCE_JOB"),
    ("remote_failed", "SUCCESSFUL_SOURCE_JOB"), ("batch", "SUCCESSFUL_SOURCE_JOB"),
    ("key", "JOB_CACHE_BINDING"), ("options", "CLOUD_OPTIONS"),
    ("hybrid", "WHOLE_SOURCE_ONLY"), ("scope", "FROZEN_RECIPE"),
    ("guard", "FROZEN_GUARD_SCOPE"), ("running", "TARGET_FAILED_IDLE"),
    ("conflicting_guards", "FROZEN_GUARD_AGREEMENT"),
    ("ready", "TARGET_FAILED_IDLE"), ("partial_ready", "TARGET_FAILED_IDLE"),
])
def test_damaged_identity_stops_before_adapter_and_cache_publication(context, damage, check):
    """真实错误类型分别定位到Hash/Job/Batch/冻结参数/覆盖/活动状态，不能变成云重试。"""
    c = context
    if damage == "hash":
        c.target.file_hash = "b" * 64
    elif damage == "active":
        c.job.is_active = True
    elif damage == "remote_failed":
        c.job.state = "failed"
    elif damage == "batch":
        c.job.batch_id = None
    elif damage == "key":
        c.job.parse_options["_cache_key"] = "b" * 64
    elif damage == "options":
        c.job.options_hash = "b" * 64
    elif damage == "hybrid":
        c.job.parse_options["_hybrid"] = {}
    elif damage == "scope":
        c.settings = c.settings.model_copy(update={"router_xml_part_max_mb": 32})
    elif damage == "guard":
        c.job.parse_options["_cache_guard"]["route_strategy"] = "native"
    elif damage == "conflicting_guards":
        route = deepcopy(c.job.parse_options["_cache_guard"])
        route["signals"]["image_count"] = 1
        c.job.parse_options["_route_decision"] = route
    elif damage in {"ready", "partial_ready"}:
        c.target.status = damage.upper()
    else:
        c.target.management_config = {"state": "running"}
    with pytest.raises(MinerUError) as failure:
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    assert failure.value.diagnostic["failed_check"] == check
    c.adapter.normalize.assert_not_called()
    assert not c.cache.path(str(c.target.doc_id), "latest.json").exists()


def test_corrupted_cached_zip_is_rejected_by_existing_checksum_validation(context):
    """即便旧Job成功，缓存ZIP字节损坏仍拒绝，不忽略Hash或使用其他同名ZIP。"""
    c = context
    record = c.cache.load(c.old)[0]
    c.cache.path(".cache", record.key, str(record.generation), "raw/mineru_result.zip").write_bytes(b"changed")
    with pytest.raises(CacheError):
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    c.adapter.normalize.assert_not_called()


def test_target_original_hash_mismatch_stops_before_conversion(context, monkeypatch):
    """数据库Hash相同不代表磁盘原件有效；目标实际字节Hash不符时禁止转换与发布。"""
    c = context
    monkeypatch.setattr(tool, "file_hash_source", MagicMock(side_effect=[c.source.file_hash, "b" * 64]))
    with pytest.raises(MinerUError) as failure:
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    assert failure.value.diagnostic["failed_check"] == "TARGET_ORIGINAL_HASH"
    c.adapter.normalize.assert_not_called()
    assert not c.cache.path(str(c.target.doc_id), "latest.json").exists()


def test_guard_failure_and_existing_target_cache_are_never_overwritten(context):
    """坐标等必要Guard失败不发布；目标已存在缓存时不覆盖成功或其他维护产物。"""
    c = context
    c.converted.blocks[0].bbox = [0.0, 0.0, 1.0, 1.0]
    with pytest.raises(CompletenessError):
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    path = c.cache.path(str(c.target.doc_id), "latest.json")
    assert not path.exists()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing-private-receipt")
    with pytest.raises(MinerUError) as failure:
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    assert failure.value.diagnostic["failed_check"] == "TARGET_CACHE_ABSENT"
    assert path.read_bytes() == b"existing-private-receipt"


def test_upgrade_cli_rejects_publication_without_explicit_execution():
    """发布许可与执行许可须同时显式给出，参数错误不打开数据库或运行Adapter。"""
    with pytest.raises(SystemExit) as failure:
        tool.main(["--source-doc-id", str(uuid4()), "--source-job-id", str(uuid4()),
            "--target-doc-id", str(uuid4()), "--kb-id", str(uuid4()), "--output", "results/new.json", "--publish"])
    assert failure.value.code == 2


def test_failed_target_materialization_reports_uncertain_publication(context, monkeypatch):
    """全局新缓存已写但目标物化失败时报告null，保留原回执，不诱导盲目重复发布。"""
    c = context
    original = c.cache.path(".jobs", str(c.job.job_id) + ".json").read_bytes()
    monkeypatch.setattr(ParseCache, "materialize", MagicMock(side_effect=CacheError("CACHE_IO")))
    with pytest.raises(CacheError) as failure:
        tool.upgrade_cache(c.source, c.target, c.job, c.settings, execute=True, publish=True)
    report = tool.failure_report(failure.value)
    assert report["publication_attempted"] and report["cache_published"] is None
    assert report["error_code"] == "CACHE_IO" and not report["database_modified"]
    assert c.cache.load(recipe(c.target, c.settings, "mineru_whole")) is not None
    assert not c.cache.path(str(c.target.doc_id), "latest.json").exists()
    assert c.cache.path(".jobs", str(c.job.job_id) + ".json").read_bytes() == original


def test_unexpected_failure_report_never_exports_raw_diagnostic_or_exception_message():
    """未知异常仅保留类型/安全项目栈，不能经diagnostic泄漏正文、密钥或私有路径。"""
    failure = ValueError("SECRET_KEY SECRET_BODY SECRET_PATH")
    failure.diagnostic = {"text": "SECRET_BODY", "path": "SECRET_PATH"}
    report = tool.failure_report(failure)
    assert report["error_type"] == "ValueError" and report["identity_diagnostic"] is None
    assert not report["publication_attempted"] and report["cache_published"] is False
    assert "SECRET" not in json.dumps(report)


def test_database_snapshot_is_read_only_and_rolled_back(context, monkeypatch):
    """上下文从有界只读事务读取；不提交、flush或修改原Job，即使后续转换失败也回滚。"""
    from app.db import session as database
    from sqlalchemy.orm import Session
    c = context
    session = MagicMock(spec=Session)
    session.get.side_effect = [c.source, c.target]
    session.scalar.side_effect = [c.job, 0]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr("sqlalchemy.orm.Session", factory)
    monkeypatch.setattr(database, "get_engine", lambda: object())
    upgrade = MagicMock(side_effect=MinerUError("LOCAL_TEST_FAILURE", "安全失败"))
    monkeypatch.setattr(tool, "upgrade_cache", upgrade)
    with pytest.raises(MinerUError):
        tool.inspect_database(c.source.doc_id, c.target.doc_id, c.job.job_id, c.target.kb_id, c.settings)
    assert "READ ONLY" in str(session.execute.call_args_list[0].args[0])
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    session.flush.assert_not_called()
