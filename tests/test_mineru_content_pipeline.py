"""v4质量/缓存/只读回放/正式分母的Mock衔接；只编写，由用户执行。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.parsed_document import ParsedBlock, ParsedDocument, ParseQuality
from scripts import mineru_adapter_replay as batch
from scripts import stage33_common as common, stage33_runtime as runtime, stage33_reports as reports
from test_mineru_content import no_cloud, convert
from test_stage33_pipeline import inputs, ready


def test_native_serialization_keeps_original_checkpoint_shape():
    """可选质量/来源字段缺省不改变Native JSON及既有主结果Hash输入。"""
    value = ParsedDocument(doc_id=uuid4(), file_name="native.txt", file_type="txt", blocks=[
        ParsedBlock(block_id=uuid4(), page=1, text="原文", source="native_parser", block_type="text")])
    raw = value.model_dump(mode="json")
    assert set(raw) == {"doc_id", "file_name", "file_type", "title", "blocks"}
    assert set(raw["blocks"][0]) == {"block_id", "page", "section", "block_type", "text", "bbox", "source", "confidence"}
    assert ParsedDocument.model_validate_json(value.model_dump_json()) == value


def test_cache_roundtrip_rebind_keeps_quality_provenance_and_quarantine(tmp_path):
    """成功缓存指针代表转换产物有效，不将部分覆盖改为完整；跨文档重绑不丢来源。"""
    from app.core.config import Settings
    from app.services.parse_cache import ParseCache, recipe
    doc_id = uuid4()
    parsed = convert([{"type": "text", "page_idx": 0, "text": "可靠内容"},
                      {"type": "text", "page_idx": -1, "text": "缺页来源内容"}], doc_id=doc_id)
    document = SimpleNamespace(doc_id=doc_id, file_name="case.pdf", file_type="pdf", file_hash="a" * 64)
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    cache, key = ParseCache(settings), recipe(document, settings, "mineru_whole")
    cache.save_document(document, key, parsed)
    restored = cache.document(document, key)
    assert restored.quality.content == "PARTIAL" and restored.blocks[0].provenance == parsed.blocks[0].provenance
    assert restored.quarantined_content == parsed.quarantined_content
    target = SimpleNamespace(**(vars(document) | {"doc_id": uuid4()}))
    rebound = cache.document(target, key)
    assert rebound.doc_id == target.doc_id and rebound.blocks[0].block_id != parsed.blocks[0].block_id
    assert rebound.quality == parsed.quality and rebound.quarantined_content == parsed.quarantined_content


def test_chunk_source_spans_keep_v4_provenance_and_do_not_emit_unknown_page():
    """可靠页的原始Hash/指针进入字符证据；隔离内容不伪造page=1进入Chunk。"""
    from app.services.chunk.splitter import SourceBlock, build_chunks
    parsed = convert([{"type": "text", "page_idx": 0, "text": "可靠内容"},
                      {"type": "text", "page_idx": -1, "text": "不应进入Chunk"}])
    block = parsed.blocks[0]
    rows = build_chunks(parsed.doc_id, uuid4(), "pdf", [SourceBlock(block.block_id, 1, block.page,
        block.section, block.block_type, block.text, block.source, block.bbox,
        block.provenance.model_dump(mode="json"))])
    assert rows and all("不应进入Chunk" not in row.text for row in rows)
    span = rows[0].metadata["source_spans"][0]
    assert span["page_reliable"] is True and span["provenance"] == block.provenance.model_dump(mode="json")
    assert span["page"] == 1 and span["block_text_hash"]


def test_bulk_replay_registers_every_failure_and_never_hides_failed_items():
    """25只是本用例规模而非CLI截断；失败/部分覆盖逐项保留，不伪造批量成功。"""
    records = [{"file_id": str(i), "job_id": str(uuid4()), "batch_id": f"batch-{i}"} for i in range(25)]
    calls = []

    def inspect(job_id, settings, batch_id, path, **flags):
        """仅模拟本地诊断返回，不连接数据库或调用Adapter。"""
        assert path is None and flags == {"allow_normalizer_upgrade": True}
        calls.append(str(job_id))
        return {"status": "FAIL" if batch_id == "batch-0" else "ADAPTER_PARTIAL_NOT_RECOVERED"
            if batch_id == "batch-1" else "ADAPTER_PASS_NOT_RECOVERED", "error_code": None}

    result = batch.replay_records(records, object(), inspect=inspect)
    assert result["registered_count"] == len(calls) == len(result["documents"]) == 25
    assert (result["complete_count"], result["partial_count"], result["failed_or_blocked_count"]) == (23, 1, 1)
    assert result["cloud_requests"] == 0 and not result["database_modified"] and not result["cache_published"]


def test_bulk_replay_budget_and_missing_job_leave_explicit_records(monkeypatch):
    """预算结束后仍登记所有项目，不把尚未执行或无来源Job算作通过。"""
    records = [{"file_id": str(i)} for i in range(7)]
    moments = iter(range(20))
    monkeypatch.setattr(batch, "monotonic", lambda: next(moments))
    report = batch.replay_records(records, object(), timeout=2)
    assert len(report["documents"]) == 7 and report["complete_count"] == 0
    assert report["documents"][0]["error_code"] == "DIAGNOSTIC_JOB"
    assert all(r["status"] == "NOT_EXECUTED_BUDGET" for r in report["documents"][1:])


def test_stage33_partial_terminal_does_not_retry_upload_or_reduce_formal_denominator(tmp_path):
    """全200/4087项保留；部分状态是终态不是WAITING，但仍不能启动正式完整评测。"""
    data, kb = inputs(), uuid4()
    rows = {doc["sha256"]: ready(doc, kb) for doc in data["documents"]}
    first = next(iter(rows.values()))
    first.update(status="PARTIAL_READY", parse_quality={"content": "PARTIAL", "missing_pages": [2]})
    assert not runtime.is_ready(first)

    def forbidden(*args, **kwargs):
        """全部文件已入库；任何重复上传/重试说明部分终态处理有误。"""
        pytest.fail("不能为PARTIAL_READY重复上传")

    state = runtime.import_all(data, tmp_path, kb, SimpleNamespace(upload=forbidden, retry=forbidden),
        reader=lambda _: deepcopy(rows), pause=lambda _: None)
    assert len(state) == 200 and sum(v["execution_status"] == "PARTIAL_READY" for v in state.values()) == 1
    with pytest.raises(common.Stage33Error):
        runtime.check_scope(data, rows, kb, ready=True)


def test_existing_raw_zip_hash_damage_is_fatal_and_never_downloaded_again(tmp_path):
    """原归档Hash损坏必须拒绝，不能降级或把损坏当miss去请求云端。"""
    from app.core.config import Settings
    from app.services.mineru import raw_results
    from app.services.mineru.errors import MinerUError
    from app.services.parse_cache import ParseCache
    from mineru_samples import result_zip
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    archive = result_zip([{"type": "text", "text": "原文", "page_idx": 0}])
    calls = []

    def download(url):
        """内存模拟首次结果获取，不发HTTP。"""
        calls.append(url)
        return archive

    identity = dict(doc_id=uuid4(), job_id=uuid4(), ordinal=1, batch_id="batch", data_id="data", file_hash="a" * 64)
    provider = SimpleNamespace(download_result=download)
    raw_results.obtain_raw_result(provider, "NOT_A_URL", settings, **identity)
    path, _ = raw_results.result_paths(ParseCache(settings), identity["doc_id"], identity["job_id"], 1, "batch")
    path.write_bytes(b"damaged-local-raw")
    with pytest.raises(MinerUError):
        raw_results.obtain_raw_result(provider, "NOT_A_URL", settings, **identity)
    assert calls == ["NOT_A_URL"] and path.read_bytes() == b"damaged-local-raw"


def test_hybrid_missing_visual_content_keeps_native_formulas_but_marks_partial():
    """视觉页无文字可降级保留Native主结果，不能声明视觉完整或伪造Bundle原件页。"""
    from app.parsers.hybrid import merge_hybrid
    from test_hybrid import metadata_plan, visual_result
    plan = metadata_plan()
    visual = visual_result(plan, pages=(1,))
    visual.quality = ParseQuality(content="PARTIAL", visual_only_count=1)
    parsed = merge_hybrid(plan, visual)
    assert parsed.blocks[0] == plan.native.blocks[0] and "=B2*2" in parsed.blocks[0].text
    assert parsed.quality.content == "PARTIAL" and "MISSING_VISUAL_PAGES" in parsed.quality.warnings
    assert all(block.page == 1 and block.bbox is None for block in parsed.blocks)
    assert plan.manifest.entries[1].state == "failed" and not plan.manifest.entries[1].block_ids


def test_empty_visual_pdf_part_is_partial_intermediate_not_fake_block():
    """中间片无OCR文字可以保存缺口；普通整份入口仍拒绝全局没有可用内容。"""
    from app.services.mineru.adapter import MinerUResultAdapter
    from app.services.mineru.errors import MinerUError
    from mineru_samples import result_zip
    archive = result_zip([{"type": "image", "page_idx": 0, "img_path": "images/original.jpg"}])
    arguments = dict(doc_id=uuid4(), file_name="part.pdf", file_type="pdf", geometry={0: (600, 800)}, identity="fixed")
    with pytest.raises(MinerUError, match="没有来源可确认"):
        MinerUResultAdapter().normalize(archive, **arguments)
    part = MinerUResultAdapter().normalize(archive, **arguments, allow_empty_part=True)
    assert part.blocks == [] and part.quality.content == "PARTIAL" and part.quality.visual_only_count == 1


def test_normalizer_upgrade_cannot_hide_paid_whole_document_history():
    """v4缓存miss仍查询相同原Hash的旧收费记录，不依赖旧Normalizer缓存键相同。"""
    from app.services.cache_claims import claim_miss
    from app.services.mineru.errors import MinerUError
    statements = []
    owner = SimpleNamespace(is_active=False, batch_id="paid-v3-batch", submission_attempted=True, parse_options={})

    def scalars(statement):
        """只记录参数化查询并返回旧记录，不打开数据库。"""
        statements.append(statement)
        return SimpleNamespace(all=lambda: [owner])

    session = SimpleNamespace(execute=lambda _: None, scalars=scalars)
    document = SimpleNamespace(doc_id=uuid4(), file_hash="a" * 64)
    with pytest.raises(MinerUError) as error:
        claim_miss(session, document, {"file_hash": document.file_hash, "normalizer_version": "mineru-adapter-v4"},
                   [], force_reparse=False)
    assert error.value.code == "CACHE_REPARSE_REQUIRED"
    assert document.file_hash in statements[0].compile().params.values()


def test_normalizer_upgrade_cannot_hide_paid_identical_pdf_piece():
    """不同原件中的相同片也按片Hash保护，不因v3/v4键不同而重复收费。"""
    from app.services.mineru import pdf_split_jobs as workflow
    from app.services.mineru.errors import MinerUError
    from unittest.mock import patch
    part = SimpleNamespace(state="waiting-file", file_hash="b" * 64, page_start=1, page_end=2)
    job = SimpleNamespace(job_id=uuid4(), parse_options={"_cache_recipe": {"normalizer_version": "mineru-adapter-v4"}})
    owner = SimpleNamespace(is_active=False, parse_options={"_pdf_split": {"parts": [
        {"file_hash": part.file_hash, "batch_id": "old-paid-batch"}]}})
    statements = []

    def scalars(statement):
        """模拟按片Hash找到不同Normalizer下的旧收费记录。"""
        statements.append(statement)
        return SimpleNamespace(all=lambda: [owner])

    session = SimpleNamespace(execute=lambda _: None, scalars=scalars)
    with patch.object(workflow, "checked_manifest", return_value=SimpleNamespace(parts=[part])):
        with pytest.raises(MinerUError) as error:
            workflow.claim_piece(session, SimpleNamespace(doc_id=uuid4()), job)
    assert error.value.code == "CACHE_REPARSE_REQUIRED"
    assert [{"file_hash": part.file_hash}] in statements[0].compile().params.values()


def test_visual_quality_revision_uses_new_local_recipe_and_keeps_original_cache():
    """v4.1不能覆写旧v4质量语义；原云配方/Batch不改，转换缓存身份必须不同。"""
    from app.services.mineru.jobs import check_normalizer_recipe
    from app.services.mineru.pdf_split import result_prefix
    from app.services.parse_cache import CONTENT_POLICY_VERSION, key_for
    old = {"normalizer_version": "mineru-adapter-v4", "file_hash": "a" * 64}
    job = SimpleNamespace(parse_options={"_cache_recipe": deepcopy(old)}, batch_id="original-paid-batch")
    check_normalizer_recipe(job)
    assert job.parse_options["_cache_recipe"] == old
    assert job.parse_options["_adapter_recipe"]["content_policy_version"] == CONTENT_POLICY_VERSION
    assert key_for(old) != key_for(job.parse_options["_adapter_recipe"])
    assert result_prefix(job) == "result-v4.1" and job.batch_id == "original-paid-batch"


def test_bulk_replay_preserves_safe_identity_failure_details():
    """单项身份失败的准确条件必须进入批量报告，不输出用户正文或上游内容。"""
    from scripts.mineru_merge_diagnostic import require_identity
    records = [{"file_id": "fixed", "job_id": str(uuid4()), "batch_id": "original"}]

    def inspect(*args, **kwargs):
        """只注入固定布尔诊断，不读取数据库或任何原件。"""
        require_identity({"batch_unique": False})

    result = batch.replay_records(records, object(), inspect=inspect)
    assert result["failed_or_blocked_count"] == 1
    assert result["documents"][0]["identity_diagnostic"]["failed_checks"] == ["batch_unique"]
