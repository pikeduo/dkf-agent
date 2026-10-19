"""阶段33真实结构的最小Mock回归；仅编写，由用户执行，不读取正式原件。"""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mineru_samples import result_zip

from app.services.mineru import jobs
from app.services.mineru.adapter import merge_evidence_error, safe_merge_diagnostic
from app.services.mineru.errors import MinerUError
from app.services.mineru.table_html import TableHTML, verify_projection
from app.services.mineru.table_merge import restore_merged_tables
from app.services.document_processing import router_failure_codes
from app.services import document_management as management
from test_mineru_recovery import context, ProviderStub
from test_mineru_table_merge import table_case, html


def test_first_raw_archive_publish_initializes_paths_and_same_batch_is_reused(tmp_path):
    """首次归档须先定位路径再写回执；随后同Batch只读复用，不重下载或覆盖。"""
    from app.core.config import Settings
    from app.services.mineru.raw_results import obtain_raw_result, load_raw_result
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    archive = result_zip([{"type": "text", "page_idx": 0, "text": "Mock正文"}])
    calls = []

    def download(url):
        """只返回内存Mock ZIP并记录次数，不读取URL或连接网络。"""
        calls.append(url)
        return archive

    provider = SimpleNamespace(download_result=download)
    identity = {"doc_id": uuid4(), "job_id": uuid4(), "ordinal": 1, "batch_id": "original-batch",
        "data_id": "original-data", "file_hash": "a" * 64}
    assert load_raw_result(settings, **identity) is None
    result, record = obtain_raw_result(provider, None, settings, **identity)
    expected = f"{identity['doc_id']}/mineru_raw/{identity['job_id']}/part-0001-original-batch.zip"
    assert result == archive and record["zip_path"] == expected
    assert record["sha256"] == hashlib.sha256(archive).hexdigest()
    assert record["size_bytes"] == len(archive) and record["content_items"] == 1
    assert record["source_file_hash"] == identity["file_hash"] and record["validation"] == "validated"
    path = settings.parsed_dir / record["zip_path"]
    receipt = path.with_suffix(".json")
    assert path.read_bytes() == archive
    assert json.loads(receipt.read_text(encoding="utf-8")) == record
    timestamps = path.stat().st_mtime_ns, receipt.stat().st_mtime_ns
    assert obtain_raw_result(provider, None, settings, **identity) == (archive, record)
    assert load_raw_result(settings, **identity) == (archive, record)
    assert calls == [None] and (path.stat().st_mtime_ns, receipt.stat().st_mtime_ns) == timestamps


def test_layout_equation_wrapper_matches_v1_without_changing_v1_cells():
    """layout的eq标签只转成比较表达，恢复仍切V1原HTML，数值/公式不得变化。"""
    items, pages = table_case(footnote=False)
    source = pages[0]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]
    source["html"] = source["html"].replace("10", "<eq>x+10</eq>")
    target = pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]
    target["html"] = target["html"].replace("10", "<eq>x+10</eq>")
    items[0]["table_body"] = items[0]["table_body"].replace("10", " $x+10$ ")
    original = deepcopy(items)
    restored, summary = restore_merged_tables(items, pages)
    assert summary[0]["rows"] == [2, 2] and items == original
    assert " $x+10$ " in restored[0]["table_body"] and "<eq>" not in restored[0]["table_body"]
    assert "x+10" not in restored[2]["table_body"]
    with pytest.raises(MinerUError):
        verify_projection(TableHTML(source["html"]), TableHTML(source["html"].replace("10", "11")))


@pytest.mark.parametrize("invalid", [
    '<table><tr><td><eq unsafe="1">x</eq></td></tr></table>',
    '<table><tr><td><eq><eq>x</eq></eq></td></tr></table>',
    '<table><tr><td><eq>x</td></eq></tr></table>',
    '<table><tr><td><eq><script>x</script></eq></td></tr></table>',
])
def test_equation_compatibility_does_not_accept_unknown_html(invalid):
    """未知属性/嵌套公式/坏HTML仍拒绝，不借公式标记放开HTML白名单。"""
    with pytest.raises(MinerUError):
        TableHTML(invalid)


def test_multiple_deleted_footnote_slots_are_not_nonempty_body():
    """每条附注的空占位均经删除证据核验，输出来自前页V1且回到真实后页。"""
    items, pages = table_case()
    note = deepcopy(pages[1]["preproc_blocks"][0]["blocks"][1])
    note.update(index=4, bbox=[48, 285, 552, 295])
    note["lines"][0]["spans"][0].update(content="第二条附注", bbox=note["bbox"])
    pages[1]["preproc_blocks"][0]["blocks"].append(deepcopy(note))
    pages[0]["para_blocks"][0]["blocks"].append(deepcopy(note))
    pages[1]["para_blocks"][0]["blocks"].append(note | {"lines": [], "lines_deleted": True})
    items[0]["table_footnote"].append("第二条附注")
    items[2]["table_footnote"] = ["", ""]
    restored, _ = restore_merged_tables(items, pages)
    assert restored[0]["table_footnote"] == []
    assert restored[2]["table_footnote"] == ["后页注释", "第二条附注"]
    items[2]["table_footnote"][1] = "未经验证的附注"
    with pytest.raises(MinerUError):
        restore_merged_tables(items, pages)


@pytest.mark.parametrize("count", [17, 32, 201])
def test_long_table_chain_is_bounded_by_provider_page_limit(count):
    """合法多页链不受旧16页假设限制，超过200页仍拒绝且不返回部分恢复。"""
    items, pages = table_case(footnote=False)
    bodies = [pages[0]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"],
              pages[1]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"]]
    for page in range(2, count):
        pages[page] = deepcopy(pages[1])
        pages[page]["page_idx"] = page
        value = html([[f"原行{page}", str(page)]])
        pages[page]["preproc_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = value
        items.append(deepcopy(items[2]) | {"page_idx": page})
        bodies.append(value)
    merged = "<table>" + "".join(value[7:-8] for value in bodies) + "</table>"
    items[0]["table_body"] = merged
    pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = merged
    if count > 200:
        with pytest.raises(MinerUError) as error:
            restore_merged_tables(items, pages)
        assert error.value.diagnostic["reason"] == "TABLE_LIMIT"
    else:
        restored, summary = restore_merged_tables(items, pages)
        assert summary[0]["pages"] == list(range(1, count + 1))
        assert [restored[i]["table_body"] for i in [0, *range(2, len(restored))]] == bodies


def test_error_reason_is_persisted_without_raw_exception_or_coordinates(context):
    """外层回滚后独立失败事务保存固定子码/位置，不保存正文、坐标或任意字段。"""
    settings, document, job, session = context
    error = merge_evidence_error("TABLE_BODY", page_idx=5, item_index=18, observed=2)
    error.diagnostic.update(text="PRIVATE_BODY", bbox=[1, 2, 3, 4], stage="PRIVATE_PATH")
    assert jobs.record_error(session, job.job_id, error, settings) is False
    detail = job.parse_options["_adapter_failure"]
    assert detail["error_code"] == job.error_code == "MERGE_EVIDENCE"
    assert detail["job_id"] == str(job.job_id) and detail["batch_id"] == job.batch_id
    assert detail["diagnostic"] == {"reason": "TABLE_BODY", "stage": "adapter.restore_merged_tables",
        "page_idx": 5, "item_index": 18, "observed": 2}
    assert "PRIVATE" not in str(detail) and document.status == "FAILED"


@pytest.mark.parametrize("saved", [True, False])
def test_explicit_adapter_retry_requires_valid_raw_result_and_keeps_paid_identity(context, monkeypatch, saved):
    """只恢复同Job/Batch/Hash且归档有效的Adapter失败；缺归档不能新建收费任务。"""
    settings, document, job, session = context
    job.state, job.error_code, job.is_active, document.status = "failed", "MERGE_EVIDENCE", False, "FAILED"
    original = job.job_id, job.task_id, job.batch_id, job.data_id, job.options_hash
    from app.services.parse_cache import NORMALIZER_VERSION
    job.parse_options = {"_cache_recipe": {"normalizer_version": NORMALIZER_VERSION}}
    monkeypatch.setattr(jobs, "local_file", lambda *args: SimpleNamespace(file_hash=job.file_hash))
    from app.services.mineru import provider, raw_results
    monkeypatch.setattr(provider, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(raw_results, "load_raw_result", lambda *args, **kw: (b"stub", {}) if saved else None)
    if not saved:
        with pytest.raises(management.ManagementError):
            management.retry_cloud_job(session, document, settings)
        assert not job.is_active and document.status == "FAILED"
    else:
        assert management.retry_cloud_job(session, document, settings) is job
        assert job.is_active and document.status == "PARSING"
    assert original == (job.job_id, job.task_id, job.batch_id, job.data_id, job.options_hash)


def test_saved_raw_archive_prevents_any_provider_query_or_download(context, monkeypatch):
    """已保全ZIP先本地转换，不请求过期链接；成功仍须原有Guard/缓存/写块边界。"""
    settings, document, job, session = context
    provider = ProviderStub(job, "pending")
    parsed = SimpleNamespace(blocks=["verified-stub"])
    monkeypatch.setattr(jobs, "load_raw_result", lambda *args, **kw: (b"saved-archive", {"sha256": "a" * 64}))
    monkeypatch.setattr(jobs, "local_file", lambda *args: SimpleNamespace(path="NOT_READ"))
    monkeypatch.setattr(jobs, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(jobs, "page_geometry", lambda *args: {})
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", lambda *args, **kw: parsed)
    monkeypatch.setattr(jobs, "save_cloud_cache", lambda *args: None)

    def finish(session, document, job, value):
        """仅模拟已验证落库边界，不写数据库或创建任何业务数据。"""
        assert value is parsed
        document.status, job.state, job.is_active = "CHUNKING", "done", False

    monkeypatch.setattr(jobs, "finish_job", finish)
    assert jobs.check_step(session, job.job_id, provider, settings) == ("done", 1)
    assert provider.queries == 0 and document.status == "CHUNKING"


def test_router_root_cause_is_not_a_freeform_error_export():
    """管理/阶段33只提取固定Router前缀，未知代码、任意正文和路径不能冒充根因。"""
    assert router_failure_codes("ROUTER_FAILED[PREFLIGHT_LIMIT]：安全说明") == ["PREFLIGHT_LIMIT"]
    for value in (None, "PRIVATE_BODY", "ROUTER_FAILED[PRIVATE_TOKEN]：正文",
                  "ROUTER_FAILED[PREFLIGHT_LIMIT,PREFLIGHT_LIMIT]：正文"):
        assert router_failure_codes(value) == []
    assert safe_merge_diagnostic({"reason": "PRIVATE_TOKEN"}) == {}


def test_pdf_dense_drawings_keep_safe_page_and_counts(monkeypatch):
    """密集矢量改为细查降级，保留页覆盖与精确计数，不记录坐标或原文。"""
    from app.core.config import Settings
    from app.parsers.routing import inspector
    events = []
    page = SimpleNamespace(number=0, rect=SimpleNamespace(width=600, height=800),
        get_image_info=lambda: [], get_drawings=lambda: [{}] * 5001,
        annots=lambda: iter(()), get_text=lambda *args, **kw: {"blocks": []})

    class PDFStub:
        """只提供资源检查所需元数据，不创建或打开真实PDF。"""
        needs_pass, is_repaired, page_count = False, False, 1

        def __enter__(self):
            """返回隔离只读元数据。"""
            return self

        def __exit__(self, *args):
            """无文件句柄或数据库副作用。"""

        def __iter__(self):
            """只返回触发细查降级的页面哨兵。"""
            return iter([page])

    def collect(event, **fields):
        """捕获安全结构摘要，不写日志文件。"""
        events.append((event, fields))

    monkeypatch.setattr(inspector.pymupdf, "open", lambda *args: PDFStub())
    monkeypatch.setattr(inspector, "log_event", collect)
    inspected = inspector.inspect_pdf("NOT_READ", Settings(_env_file=None))
    assert not inspected.uncertain and "PDF_COMPLEX_DRAWING" in inspected.risks
    assert inspected.signals.pages[0].required
    assert events == [("router.preflight_geometry_limited", {"stage": "preflight.pdf", "step": "PDF_DRAWING_DETAIL_LIMIT",
        "page": 1, "line_count": 0, "image_count": 0, "drawing_count": 5001,
        "object_count": 5001, "limit_count": 5000})]
