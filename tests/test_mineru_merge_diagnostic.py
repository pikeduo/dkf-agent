"""只读诊断工具Mock：不发HTTP、不连接数据库、不解析PDF或写业务缓存。"""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from scripts import mineru_merge_diagnostic as tool
from app.core.config import Settings
from app.services.mineru import pdf_split as split
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import canonical, recipe
from mineru_samples import result_zip
from test_mineru_merge import merged_case


@pytest.fixture
def context(tmp_path, monkeypatch):
    """构造有Hash的字节哨兵与真实字段账本，geometry/Guard仅由Stub替代且不声称原件通过。"""
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    doc_id, job_id, task_id = uuid4(), uuid4(), uuid4()
    original = tmp_path / "original.pdf"
    original.write_bytes(b"mock-original-not-a-pdf")
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    part_hash = hashlib.sha256(b"mock-part-not-a-pdf").hexdigest()
    part_id = uuid5(job_id, f"{split.VERSION}:1:1:2:{part_hash}")
    part = split.PDFPart(ordinal=1, page_start=1, page_end=2, file_hash=part_hash, size_bytes=19,
        data_id=f"dkf-{part_id.hex}", task_id=part_id, batch_id="original-batch", state="failed",
        submission_attempted=True, upload_complete=True, error_code="MERGE_EVIDENCE")
    manifest = split.SplitManifest(doc_id=doc_id, file_hash=original_hash, page_count=2,
                                  policy=split.policy(), parts=[part])
    document = SimpleNamespace(doc_id=doc_id, file_type="pdf", file_hash=original_hash,
        file_path=str(original), file_name="私有文件名.pdf", status="FAILED", processing_task_id=task_id)
    from app.services.mineru.jobs import parse_options
    options = parse_options(settings)
    job = SimpleNamespace(doc_id=doc_id, job_id=job_id, task_id=task_id, file_hash=original_hash,
        provider="mineru_cloud", state="failed", batch_id=part.batch_id, data_id=part.data_id,
        model_version=options["model_version"],
        options_hash=hashlib.sha256(canonical(options | {"pdf_split": split.policy()})).hexdigest(),
        upload_complete=True, parse_options=options | {"_pdf_split": manifest.model_dump(mode="json"),
            "_cache_recipe": recipe(document, settings, "mineru_whole", pdf_split_policy=split.policy())})
    path = split.part_path(tool.ParseCache(settings), doc_id, job_id, 1)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mock-part-not-a-pdf")
    monkeypatch.setattr(tool, "page_geometry", lambda *args: {0: (600, 800), 1: (600, 800)})
    from app.services.mineru import pdf_split_jobs
    monkeypatch.setattr(pdf_split_jobs, "validate_piece", lambda *args: None)
    return document, job, settings, path


def test_missing_zip_is_blocked_and_does_not_revive_failed_job(context):
    """缺ZIP只输出已核对的身份，不根据云done伪造Cache或复活终态。"""
    document, job, settings, _ = context
    snapshot = deepcopy(vars(document)), deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", None)
    assert report["status"] == "BLOCKED" and report["error_code"] == "ZIP_REQUIRED"
    assert report["source_hashes_verified"] and not report["cache_published"]
    assert (vars(document), vars(job)) == snapshot


def test_existing_zip_reproduces_safe_subreason_without_exporting_text_or_bbox(context, tmp_path):
    """真实Adapter消费内存ZIP的落盘副本；仅定位子原因，原文/坐标/路径/签名URL不进报告。"""
    document, job, settings, _ = context
    values, layout, _ = merged_case(originals=("PRIVATE_BODY", "PRIVATE_BODY_2"))
    layout["pdf_info"][0]["preproc_blocks"][1]["lines"][0]["spans"].append(
        deepcopy(layout["pdf_info"][0]["preproc_blocks"][1]["lines"][0]["spans"][0]))
    archive = tmp_path / "input.zip"
    archive.write_bytes(result_zip(values, layout=layout))
    snapshot = deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", archive)
    assert report["status"] == "FAIL" and report["error_code"] == "MERGE_EVIDENCE"
    assert report["merge_diagnostic"]["reason"] == "LINE_SPAN_COUNT"
    assert report["merge_diagnostic"]["original_page"] == 1
    assert report["zip_size_bytes"] == archive.stat().st_size
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in ("PRIVATE_BODY", "bbox", str(tmp_path), document.file_name))
    assert vars(job) == snapshot and not list(settings.parsed_dir.rglob("ready.json"))


def test_validated_zip_is_not_a_recovery_or_batch_provenance_proof(context, tmp_path):
    """离线通过也只标ADAPTER_PASS，必须保留人工下载来源核对，不发布结果或推进下一片。"""
    document, job, settings, _ = context
    values, layout, _ = merged_case()
    archive = tmp_path / "input.zip"
    archive.write_bytes(result_zip(values, layout=layout))
    snapshot = deepcopy(vars(job))
    report = tool.inspect_job(document, job, settings, "original-batch", archive)
    assert report["status"] == "ADAPTER_PASS_NOT_RECOVERED" and report["block_count"] == 2
    assert report["zip_batch_provenance"] == "manual_confirmation_required"
    assert report["original_pages"] == [1] and not report["database_modified"] and not report["cloud_submitted"]
    assert vars(job) == snapshot


@pytest.mark.parametrize("change,code", [("original", "PDF_SPLIT_CHANGED"), ("piece", "PDF_SPLIT_CHANGED"),
    ("batch", "DIAGNOSTIC_IDENTITY"), ("generation", "DIAGNOSTIC_IDENTITY"),
    ("normalizer", "CACHE_CONFIG_CHANGED"), ("part_identity", "PDF_SPLIT_CHECKPOINT"),
    ("options", "DIAGNOSTIC_OPTIONS")])
def test_wrong_source_or_frozen_identity_stops_before_adapter(context, monkeypatch, change, code):
    """核对原Hash、片Hash、Batch、代次、缓存版本与稳定片ID，不能借离线诊断放宽恢复。"""
    document, job, settings, path = context

    def forbidden(*args, **kwargs):
        """任何错误输入进入Adapter均说明验证顺序有误。"""
        pytest.fail("身份校验之前不应处理ZIP")

    monkeypatch.setattr(tool.MinerUResultAdapter, "normalize", forbidden)
    if change == "original":
        document.file_path = path
    elif change == "piece":
        path.write_bytes(b"broken")
    elif change == "batch":
        job.batch_id = "another-batch"
    elif change == "generation":
        document.processing_task_id = uuid4()
    elif change == "normalizer":
        job.parse_options["_cache_recipe"]["normalizer_version"] = "old-version"
    elif change == "options":
        job.parse_options["_cache_recipe"]["language"] = "changed"
    else:
        job.parse_options["_pdf_split"]["parts"][0]["data_id"] = "changed"
    with pytest.raises(MinerUError) as error:
        tool.inspect_job(document, job, settings, "original-batch", path)
    assert error.value.code == code


def test_report_is_exclusive_and_io_errors_do_not_leak_or_overwrite(tmp_path, monkeypatch, capsys):
    """同名输出拒绝，驱动/路径异常仅固定安全码，不输出异常原文。"""
    monkeypatch.setattr(tool, "ROOT", tmp_path)

    def unavailable(*args):
        """模拟带敏感URL的驱动错误，不连接数据库。"""
        raise RuntimeError("PRIVATE_TOKEN postgresql://PRIVATE_URL")

    monkeypatch.setattr(tool, "inspect_database", unavailable)
    from app.core import business_logging
    monkeypatch.setattr(business_logging, "configure_logging", lambda *args, **kwargs: None)
    args = ["--job-id", str(uuid4()), "--batch-id", "original-batch", "--output", "results/report.json"]
    assert tool.main(args) == 2
    target = tmp_path / "results/report.json"
    before = target.read_bytes()
    assert json.loads(before)["error_code"] == "DIAGNOSTIC_IO_OR_DB"
    assert tool.main(args) == 2 and target.read_bytes() == before
    assert "PRIVATE" not in capsys.readouterr().out and "PRIVATE" not in before.decode()


def test_diagnostic_page_and_item_log_fields_only_accept_bounded_integers():
    """新增定位字段不能借字符串、布尔或超限数字输出正文/伪造页码。"""
    from app.core.business_logging import safe_fields
    assert safe_fields({"page": 200, "item_index": 1000}) == {"page": 200, "item_index": 1000}
    for value in (True, -1, 0, 1.5, 10**9, "PRIVATE_BODY"):
        assert safe_fields({"page": value, "item_index": value}) == {}


def test_database_inspection_enforces_read_only_and_rollback_on_error(monkeypatch):
    """仅构造SQL并以Stub记录只读边界，禁止行锁/提交/flush或数据库写入。"""
    from app.db import session as database
    from sqlalchemy import orm
    calls = []

    class ReadOnlySession:
        """只能执行只读查询的测试会话，不建立连接。"""

        def __init__(self, *args, **kwargs):
            """校验禁用autoflush，避免诊断隐式修改记录。"""
            assert kwargs == {"autoflush": False}

        def __enter__(self):
            """返回内存会话。"""
            return self

        def __exit__(self, *args):
            """不隐式提交事务。"""

        def execute(self, statement):
            """记录只读SQL；不存在Job时返回空结果。"""
            calls.append(str(statement))
            return SimpleNamespace(one_or_none=lambda: None)

        def rollback(self):
            """无论异常类型如何均退出只读事务。"""
            calls.append("ROLLBACK")

    monkeypatch.setattr(database, "get_engine", lambda: object())
    monkeypatch.setattr(orm, "Session", ReadOnlySession)
    with pytest.raises(MinerUError) as error:
        tool.inspect_database(uuid4(), object(), "batch", None)
    assert error.value.code == "DIAGNOSTIC_JOB"
    assert calls[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert calls[-1] == "ROLLBACK" and "SELECT" in calls[-2]
    assert all(not any(word in statement.upper() for word in ("FOR UPDATE", "INSERT ", "DELETE ", "COMMIT"))
               for statement in calls)
