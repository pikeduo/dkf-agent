"""一个父 Job 的分片短任务 Mock 回归；不连接数据库/Redis/GPU/真实 MinerU。"""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from mineru_samples import result_zip

from app.core.config import Settings
from app.parsers.routing.contracts import Inspection, PageSignals, StructuralSignals
from app.parsers.routing.router import decide_route
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.mineru import jobs
from app.services.mineru import pdf_split as split
from app.services.mineru import pdf_split_jobs as workflow
from app.services.mineru.errors import MinerUError
from app.services.mineru.provider import UploadBatch
from app.services.parse_cache import ParseCache, recipe


class SessionStub:
    """只模拟当前文档/Job/Block 事务快照，不执行 SQL。"""

    def __init__(self, document, job):
        """保存隔离内存对象及已有证据，用来验证失败不会发布部分 Block。"""
        self.document, self.job = document, job
        self.blocks, self.commits, self.fail_final = ["OLD_BLOCK"], 0, False
        self.snapshot = None
        self.commit()

    def commit(self):
        """最终写块可注入 SQL 提交失败；其他检查点可单独持久化。"""
        if self.fail_final and self.document.status == "CHUNKING":
            raise OSError("PRIVATE_DB_ERROR")
        self.commits += 1
        self.snapshot = deepcopy((vars(self.document), vars(self.job), self.blocks))

    def rollback(self):
        """还原上一次检查点，模拟外层 processing_session 异常后的事务回滚。"""
        document, job, blocks = deepcopy(self.snapshot)
        self.document.__dict__.clear()
        self.document.__dict__.update(document)
        self.job.__dict__.clear()
        self.job.__dict__.update(job)
        self.blocks = blocks

    def scalars(self, statement):
        """默认无其他内容 owner；只检查构造查询，不连接 PostgreSQL。"""
        return SimpleNamespace(all=list)


class ProviderStub:
    """记录云文件数/身份；每次 GET 只返回一次状态，不发 HTTP。"""

    def __init__(self):
        """初始化独立计费观测和可注入的传输错误。"""
        self.posts, self.puts, self.gets, self.downloads = [], [], [], 0
        self.post_error = self.put_error = self.get_error = None
        self.state = "done"

    def request_upload_urls(self, files, options):
        """一片一文件，参数中即使有内部 manifest，Provider 仍只接收一份文件。"""
        assert len(files) == 1
        self.posts.append(files[0].data_id)
        if self.post_error:
            raise self.post_error
        return UploadBatch(f"batch-{len(self.posts)}", ["https://mineru.net/private-upload"], "trace")

    def upload(self, url, source):
        """重复 PUT 沿用同 data_id；失败不创建额外 Batch。"""
        self.puts.append(source.data_id)
        if self.put_error:
            raise self.put_error

    def query(self, batch_id, data_id):
        """一次异步状态观察，不在短任务内循环。"""
        self.gets.append((batch_id, data_id))
        if self.get_error:
            raise self.get_error
        return {"state": self.state, "trace_id": "trace", "data_id": data_id,
                "full_zip_url": "https://mineru.net/private-result"}

    def download_result(self, url):
        """返回内存 Mock 结构化 ZIP，验证独立归档；不伪造真实 OCR 验收。"""
        self.downloads += 1
        return result_zip()


@pytest.fixture
def context(tmp_path, monkeypatch):
    """用元数据和字节占位替代 PDF；真实生成器/HTTP 协议留给独立人工验收。"""
    source = tmp_path / "source.bin"
    source.write_bytes(b"PRIVATE_ORIGINAL")
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    document = SimpleNamespace(doc_id=uuid4(), kb_id=uuid4(), file_name="unchanged.pdf", file_type="pdf",
        file_path=str(source), file_hash=split.file_digest(source), status="PARSING", error_message=None,
        processing_task_id=uuid4(), management_config=None)
    pages = tuple(PageSignals(page=p, text_chars=0, image_count=1, image_area_ratio=1,
        drawing_count=0, annotation_count=0, required=True) for p in range(1, 258))
    decision = decide_route("pdf", "auto", Inspection(signals=StructuralSignals(page_count=257, pages=pages),
        risks=("PDF_SCAN_OR_NON_TEXT_PAGE",)))
    job = SimpleNamespace(job_id=uuid4(), doc_id=document.doc_id, task_id=document.processing_task_id,
        file_hash=document.file_hash, options_hash="b" * 64, data_id="initial-id", batch_id=None,
        trace_id=None, upload_complete=False, submission_attempted=False, submission_started_at=None,
        upload_url=None, upload_expires_at=None, submitted_at=None, created_at=datetime.now(UTC),
        is_active=True, state="waiting-file", retry_count=0, error_code=None, error_message=None, finished_at=None,
        parse_options=jobs.parse_options(settings) | {"_pdf_split_request": {"policy": split.policy(), "page_count": 257},
            "_cache_recipe": recipe(document, settings, "mineru_whole", pdf_split_policy=split.policy()),
            "_cache_guard": decision.model_dump(mode="json")})
    session, provider = SessionStub(document, job), ProviderStub()

    def lock(session, identifier):
        """模拟固定文档行锁/代次保护，结束或旧消息不能继续处理。"""
        if identifier == job.job_id and job.is_active and document.status == "PARSING" and document.processing_task_id == job.task_id:
            return document, job
        return None

    def prepare(doc, task, configuration, path):
        """生成两个字节占位片及完整冻结身份，禁止实际打开 PDF。"""
        parts = []
        for ordinal, start, end in [(1, 1, 200), (2, 201, 257)]:
            content = f"MOCK_PIECE_{ordinal}".encode()
            target = split.part_path(ParseCache(settings), doc.doc_id, task.job_id, ordinal)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            identity = uuid5(task.job_id, f"{split.VERSION}:{ordinal}:{start}:{end}:{digest}")
            parts.append(split.PDFPart(ordinal=ordinal, page_start=start, page_end=end,
                file_hash=digest, size_bytes=len(content), task_id=identity, data_id=f"dkf-{identity.hex}"))
        return split.SplitManifest(doc_id=doc.doc_id, file_hash=doc.file_hash, page_count=257,
                                   policy=split.policy(), parts=parts)

    def validate(source):
        """只核对占位片字节 Hash，不声称这些占位是合法 PDF。"""
        assert split.file_digest(source.path) == source.file_hash
        part = workflow.current_part(split.checked_manifest(document, job))
        return part.page_end - part.page_start + 1

    def geometry(path, kind):
        """给 Adapter Stub 提供冻结的局部页数，不调用 PyMuPDF。"""
        ordinal = int(path.stem.rsplit("-", 1)[1])
        part = split.checked_manifest(document, job).parts[ordinal - 1]
        return {p: (600, 800) for p in range(part.page_end - part.page_start + 1)}

    def normalize(self, archive, **kwargs):
        """返回逐页内存块，真实 content_list/几何转换继续由已有 Adapter 回归覆盖。"""
        blocks = [ParsedBlock(block_id=uuid5(document.doc_id, f"{kwargs['identity']}:{p}"),
            page=p + 1, block_type="text", text=f"PRIVATE_PAGE_{p + 1}", bbox=[1, 2, 30, 40], source="mineru_cloud")
            for p in kwargs["geometry"]]
        return ParsedDocument(doc_id=kwargs["doc_id"], file_name=kwargs["file_name"], file_type="pdf", blocks=blocks)

    def persist(session, doc, parsed):
        """模拟原持久化入口的全量替换，而不是分片直接写 Block。"""
        session.blocks = deepcopy(parsed.blocks)

    def no_sql_lock(session, keys):
        """替代 PostgreSQL advisory lock；并发隔离仍需要用户执行 DB 回归。"""
        assert keys and len(keys[0]) == 64

    def artifacts(archive):
        """保存明确的 Mock 字节回执，不读取 ZIP。"""
        return {"raw/mineru_result.zip": archive}

    monkeypatch.setattr(jobs, "lock_job", lock)
    monkeypatch.setattr(workflow, "prepare_manifest", prepare)
    monkeypatch.setattr(workflow, "lock_keys", no_sql_lock)
    monkeypatch.setattr(jobs, "validate_local_file", validate)
    monkeypatch.setattr(workflow, "validate_local_file", validate)
    monkeypatch.setattr(jobs, "page_geometry", geometry)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", normalize)
    monkeypatch.setattr(workflow, "archive_artifacts", artifacts)
    monkeypatch.setattr(jobs, "persist_blocks", persist)
    return SimpleNamespace(document=document, job=job, session=session, provider=provider, settings=settings)


def submit(c):
    """执行一次被测提交短步骤，仅使用 Provider Stub。"""
    return jobs.submit_step(c.session, c.job.job_id, c.provider, c.settings)


def check(c):
    """执行一次被测检查短步骤，不连接任何外部服务。"""
    return jobs.check_step(c.session, c.job.job_id, c.provider, c.settings)


def test_all_parts_complete_before_atomic_original_document_blocks(context):
    """未完成片不得触发 CHUNKING/READY；最后才一次替换257页，仍只有原 doc_id。"""
    c = context
    source = c.document.doc_id, c.document.file_hash, c.document.file_name, c.document.processing_task_id
    assert submit(c)[0] == "check"
    assert submit(c) == ("check", 0)  # 重复消息不再次 POST/PUT。
    assert len(c.provider.posts) == 1
    assert check(c) == ("submit", 0)
    assert c.session.blocks == ["OLD_BLOCK"] and c.document.status == "PARSING"
    assert workflow.submitted_count(c.job) == 1 and c.job.batch_id is None
    assert workflow.has_submission_history(c.job)
    assert submit(c)[0] == "check"
    assert check(c) == ("done", 257)
    assert len(c.provider.posts) == 2 and len(set(c.provider.posts)) == 2
    assert workflow.submitted_count(c.job) == 2
    assert [b.page for b in c.session.blocks] == list(range(1, 258))
    assert c.document.status == "CHUNKING" and c.job.state == "done" and not c.job.is_active
    assert (c.document.doc_id, c.document.file_hash, c.document.file_name, c.document.processing_task_id) == source
    assert check(c) == ("skipped", 0)
    assert len(c.provider.posts) == 2
    # 原 Index 仍是 READY 唯一提交者；这里不伪造 GPU/索引成功。
    assert c.document.status != "READY"
    parts = split.checked_manifest(c.document, c.job).parts
    for part in parts:
        raw = part.raw_result
        assert raw["validation"] == "validated" and raw["batch_id"] == part.batch_id
        archive = ParseCache(c.settings).root / raw["zip_path"]
        assert split.file_digest(archive) == raw["sha256"]


def test_adapter_failure_keeps_downloaded_zip_without_business_success(context, monkeypatch):
    """Adapter拒绝后回滚业务SQL，仍能取得同Batch原始ZIP及Hash；不写半份Block或提交第二片。"""
    import json

    c = context

    def reject(self, archive, **kwargs):
        """模拟结构合法但来源无法证明的转换错误，不运行实际 Adapter。"""
        raise MinerUError("MERGE_EVIDENCE", "安全来源错误")

    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", reject)
    submit(c)
    batch = c.job.batch_id
    with pytest.raises(MinerUError) as error:
        check(c)
    c.session.rollback()
    assert not jobs.record_error(c.session, c.job.job_id, error.value, c.settings)
    root = ParseCache(c.settings).root
    receipts = list(root.glob(f"*/mineru_raw/*/part-0001-{batch}.json"))
    assert len(receipts) == 1
    raw = json.loads(receipts[0].read_text(encoding="utf-8"))
    archive = root / raw["zip_path"]
    assert raw["validation"] == "validated" and raw["batch_id"] == batch
    assert split.file_digest(archive) == raw["sha256"] and raw["size_bytes"] == archive.stat().st_size
    assert c.document.status == "FAILED" and c.session.blocks == ["OLD_BLOCK"]
    assert len(c.provider.posts) == 1 and c.provider.downloads == 1
    assert c.job.state == "failed" and not c.job.is_active


def test_same_batch_raw_archive_is_reused_after_adapter_retry(context, monkeypatch):
    """已保存ZIP但后续转换暂时失败时，只复查同Batch，不重新下载/POST或覆盖ZIP。"""
    c = context
    original = jobs.MinerUResultAdapter.normalize

    def reject(self, archive, **kwargs):
        """模拟可恢复的本地I/O错误，不发HTTP。"""
        raise MinerUError("FILE_IO", "安全本地错误", retryable=True)

    submit(c)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", reject)
    with pytest.raises(MinerUError) as error:
        check(c)
    c.session.rollback()
    assert jobs.record_error(c.session, c.job.job_id, error.value, c.settings)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", original)
    assert check(c) == ("submit", 0)
    assert len(c.provider.posts) == c.provider.downloads == 1


def test_single_failed_part_retains_successful_cache_and_original_blocks(context):
    """第二片远端失败不能落半份 Block；错误持久化不损坏第一片结果或补新批次。"""
    c = context
    submit(c)
    check(c)
    submit(c)
    c.provider.state = "failed"
    with pytest.raises(MinerUError) as error:
        check(c)
    c.session.rollback()
    assert not jobs.record_error(c.session, c.job.job_id, error.value, c.settings)
    manifest = split.checked_manifest(c.document, c.job)
    assert manifest.parts[0].state == "done" and manifest.parts[0].result
    assert manifest.parts[1].state == "failed"
    assert c.document.status == "FAILED" and c.session.blocks == ["OLD_BLOCK"]
    assert len(c.provider.posts) == 2 and not workflow.resumable_split(c.document, c.job)


def test_upload_retry_reuses_batch_and_final_cache_restores_sql_rollback(context):
    """PUT重试沿用Batch；最终提交失败从完整原件级缓存恢复，不再收费/查询。"""
    c = context
    c.provider.put_error = MinerUError("UPLOAD_NETWORK", "安全上传错误", retryable=True)
    with pytest.raises(MinerUError) as error:
        submit(c)
    c.session.rollback()
    assert jobs.record_error(c.session, c.job.job_id, error.value, c.settings)
    c.provider.put_error = None
    submit(c)
    assert len(c.provider.posts) == 1 and len(c.provider.puts) == 2
    check(c)
    submit(c)
    c.session.fail_final = True
    with pytest.raises(OSError):
        check(c)
    c.session.rollback()
    assert c.document.status == "PARSING" and c.session.blocks == ["OLD_BLOCK"]
    calls = len(c.provider.posts), len(c.provider.gets), c.provider.downloads
    c.session.fail_final = False
    assert jobs.restore_cached_result(c.session, c.job.job_id, c.settings) == ("done", 257)
    assert (len(c.provider.posts), len(c.provider.gets), c.provider.downloads) == calls
    assert split.checked_manifest(c.document, c.job).parts[-1].state == "done"


def test_uncertain_post_never_creates_second_paid_batch(context):
    """POST超时/5xx保留意图，重复消息不能盲目申请另一批次。"""
    c = context
    c.provider.post_error = MinerUError("SUBMIT_UNCERTAIN", "安全不确定提交")
    with pytest.raises(MinerUError):
        submit(c)
    c.session.rollback()
    c.job.submission_started_at = datetime.now(UTC) - timedelta(hours=1)
    c.session.commit()
    with pytest.raises(MinerUError) as error:
        submit(c)
    assert error.value.code == "SUBMIT_UNCERTAIN"
    assert len(c.provider.posts) == 1 and workflow.has_submission_history(c.job)


def test_exhausted_put_retry_preserves_valid_batch_url_for_explicit_resume(context):
    """上传重试耗尽仍保留有效的原签名检查点；管理员恢复可PUT同Batch，不另POST。"""
    c = context
    c.provider.put_error = MinerUError("UPLOAD_NETWORK", "安全上传失败", retryable=True)
    with pytest.raises(MinerUError) as error:
        submit(c)
    c.session.rollback()
    c.job.retry_count = c.settings.mineru_max_retries
    assert not jobs.record_error(c.session, c.job.job_id, error.value, c.settings)
    assert c.job.upload_url and c.job.batch_id and c.document.status == "FAILED"
    assert workflow.resumable_split(c.document, c.job)
    c.job.upload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not workflow.resumable_split(c.document, c.job)


def test_missing_or_changed_piece_is_not_resubmitted(context):
    """分片 Hash损坏/丢失安全失败，不据平均页号重建并再次收费。"""
    c = context
    workflow.ensure_plan(c.session, c.document, c.job, c.settings)
    path = split.part_path(ParseCache(c.settings), c.document.doc_id, c.job.job_id, 1)
    path.write_bytes(b"CHANGED")
    with pytest.raises(MinerUError) as error:
        submit(c)
    assert error.value.code == "PDF_SPLIT_CHANGED" and c.provider.posts == []
    path.unlink()
    with pytest.raises(MinerUError) as error:
        submit(c)
    assert error.value.code == "PDF_SPLIT_MISSING" and c.provider.posts == []


def test_changed_original_hash_blocks_new_piece_submission(context):
    """原件内容变化即使已生成分片也不得再收费；原 doc_id/Hash 不自动改写。"""
    c = context
    workflow.ensure_plan(c.session, c.document, c.job, c.settings)
    from pathlib import Path
    Path(c.document.file_path).write_bytes(b"MODIFIED_SOURCE")
    with pytest.raises(MinerUError) as error:
        submit(c)
    assert error.value.code == "FILE_HASH" and c.provider.posts == []


def test_invalid_checkpoint_cannot_block_safe_failure_record(context):
    """损坏账本不许通过二次校验异常让Document永远PARSING；保留原JSON并安全失败。"""
    c = context
    workflow.ensure_plan(c.session, c.document, c.job, c.settings)
    c.job.parse_options["_pdf_split"]["parts"][0]["data_id"] = "corrupted"
    c.session.commit()
    assert not jobs.record_error(c.session, c.job.job_id,
        MinerUError("PDF_SPLIT_CHECKPOINT", "安全检查点错误"), c.settings)
    assert c.job.error_code == "PDF_SPLIT_CHECKPOINT" and c.document.status == "FAILED"
    assert c.job.parse_options["_pdf_split"]["parts"][0]["data_id"] == "corrupted"
    assert c.provider.posts == []


def test_piece_cache_survives_intermediate_sql_rollback(context, monkeypatch):
    """第一片已保存结果但游标提交失败时，恢复片指针而非再次请求/收费。"""
    c = context
    submit(c)
    original_commit = c.session.commit

    def fail_commit():
        """模拟片完成后 SQL 提交中断，文件缓存不回滚。"""
        raise OSError("PRIVATE_DB_ERROR")

    monkeypatch.setattr(c.session, "commit", fail_commit)
    with pytest.raises(OSError):
        check(c)
    c.session.rollback()
    calls = len(c.provider.posts), len(c.provider.gets), c.provider.downloads
    monkeypatch.setattr(c.session, "commit", original_commit)
    assert submit(c) == ("submit", 0)
    assert (len(c.provider.posts), len(c.provider.gets), c.provider.downloads) == calls
    assert split.checked_manifest(c.document, c.job).parts[0].state == "done"
    assert c.session.blocks == ["OLD_BLOCK"]


def test_explicit_new_generation_reuses_valid_pieces_instead_of_paying_again(context):
    """即使显式 Reparse 超限PDF，完整有效的片仍复用；仅确实缺结果的片可新收费。"""
    c = context
    submit(c)
    check(c)
    first = split.checked_manifest(c.document, c.job).parts[0]
    # 模拟新父代次已经生成相同字节的第一片，不更改任何官方/业务文档。
    clone = deepcopy(c.job)
    clone.job_id, clone.task_id = uuid4(), uuid4()
    clone.parse_options["_force_reparse"] = True
    clone.batch_id, clone.submission_attempted, clone.upload_complete = None, False, False
    manifest = split.SplitManifest.model_validate(clone.parse_options["_pdf_split"])
    for part in manifest.parts:
        part.state, part.result, part.batch_id = "waiting-file", None, None
        part.submission_attempted = part.upload_complete = False
        identity = uuid5(clone.job_id, f"{split.VERSION}:{part.ordinal}:{part.page_start}:{part.page_end}:{part.file_hash}")
        part.task_id, part.data_id = identity, f"dkf-{identity.hex}"
    clone.parse_options["_pdf_split"] = manifest.model_dump(mode="json")
    clone.data_id = manifest.parts[0].data_id
    document = deepcopy(c.document)
    document.processing_task_id = clone.task_id
    session = SessionStub(document, clone)
    before = len(c.provider.posts)
    assert workflow.complete_cached_piece(session, document, clone, c.settings) == ("submit", 0)
    assert split.checked_manifest(document, clone).parts[0].state == "done"
    assert len(c.provider.posts) == before


def test_local_timeout_can_observe_same_done_batch_after_worker_stop(context):
    """Worker停机使本地过期不等于云端失败；先GET同Batch的done，保留前片与收费身份。"""
    c = context
    submit(c)
    original = c.job.batch_id, c.job.data_id
    c.job.parse_options["_local_wait_started_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    c.job.created_at = datetime.now(UTC) - timedelta(days=2)
    c.session.commit()
    assert check(c) == ("submit", 0)
    assert c.provider.gets == [original] and len(c.provider.posts) == 1
    assert c.document.status == "PARSING" and c.session.blocks == ["OLD_BLOCK"]


@pytest.mark.parametrize("code,allowed", [("JOB_TIMEOUT", True), ("DOWNLOAD_NETWORK", True),
    ("HTTP_503", True), ("SUBMIT_UNCERTAIN", False), ("REMOTE_FAILED", False), ("ZIP_INVALID", False)])
def test_explicit_resume_is_limited_to_same_uploaded_batch(context, code, allowed):
    """仅已上传批次的本地/传输错误可显式恢复；坏结果/远端失败不自动收费重解析。"""
    c = context
    submit(c)
    c.job.error_code = code
    assert workflow.resumable_split(c.document, c.job) is allowed
    c.job.upload_complete = False
    assert not workflow.resumable_split(c.document, c.job)


@pytest.mark.parametrize("code", ["CACHE_IN_PROGRESS", "RATE_LIMIT", "DAILY_LIMIT", "JOB_TIMEOUT"])
def test_explicit_retry_can_resume_never_submitted_next_piece(context, code):
    """片一成功而片二未提交的安全等待可显式恢复原Job；不会重付片一。"""
    c = context
    submit(c)
    check(c)
    c.job.error_code = code
    assert workflow.resumable_split(c.document, c.job)
    c.job.submission_attempted = True
    assert not workflow.resumable_split(c.document, c.job)


@pytest.mark.parametrize("kind,pages,byte_size,needs_split", [("pdf", 257, 100, True), ("pdf", 217, 100, True),
    ("pdf", 382, 100, True), ("pdf", 200, 100, False), ("pdf", 1, split.MAX_BYTES + 1, True),
    ("doc", None, 100, False), ("docx", None, 100, False), ("pptx", None, 100, False)])
def test_create_job_defers_only_oversized_pdf_to_worker(context, monkeypatch, kind, pages, byte_size, needs_split):
    """仅云PDF超页数/大小登记需求；普通PDF/Office仍原校验，API内不拆文件。"""
    c = context
    document = deepcopy(c.document)
    document.file_type, document.file_name, document.status = kind, f"unchanged.{kind}", "UPLOADED"
    document.management_task_id = None
    calls = []

    class SourcePathStub:
        """用可控 stat 测试大小入口，不创建200MB样本，不影响分片实际字节验证用例。"""

        def open(self, *args, **kwargs):
            """源Hash检查仍读取本用例独立的真实占位字节。"""
            from pathlib import Path
            return Path(document.file_path).open(*args, **kwargs)

        def stat(self):
            """仅模拟原件大小元数据，不用它推算分片大小。"""
            return SimpleNamespace(st_size=byte_size)

    class CreateSession:
        """只模拟 create_job 两个查询和新增父 Job，不连接数据库。"""

        def __init__(self):
            """准备文档行和空活动Job的固定返回顺序。"""
            self.rows = iter([document, None])
            self.added = []

        def scalar(self, statement):
            """返回预设快照，不执行SQL。"""
            return next(self.rows)

        def add(self, row):
            """只记录父 Job，禁止隐式新增子 Document。"""
            self.added.append(row)

        def flush(self):
            """模拟检查点flush，不写入业务库。"""

    def inspect(path, file_type, settings):
        """只返回页数元数据，不打开原件占位字节。"""
        return Inspection(signals=StructuralSignals(page_count=pages))

    def source_path(doc):
        """为原件入口提供只读Hash/可控大小Stub，不指向阶段32素材。"""
        return SourcePathStub()

    def validate(source):
        """记录未拆分格式继续使用原 Provider 的提交前验证。"""
        calls.append(source)

    def lock_keys(session, keys):
        """不执行真实advisory锁；内容身份锁由现有DB回归验证。"""

    def claim(session, doc, value, visual_keys, *, force_reparse):
        """模拟首次无收费历史 miss，不修改任何已下载数据。"""

    monkeypatch.setattr(jobs, "inspect_document", inspect)
    monkeypatch.setattr(jobs, "source_path", source_path)
    monkeypatch.setattr(jobs, "validate_local_file", validate)
    monkeypatch.setattr(jobs, "lock_keys", lock_keys)
    monkeypatch.setattr(jobs, "claim_miss", claim)
    session = CreateSession()
    job = jobs.create_job(session, document, c.settings)
    assert ("_pdf_split_request" in job.parse_options) is needs_split
    assert ("pdf_split" in job.parse_options["_cache_recipe"]) is needs_split
    assert len(calls) == (0 if needs_split else 1)
    assert len(session.added) == 1 and session.added[0].doc_id == document.doc_id
    assert "_pdf_split" not in job.parse_options  # API阶段不执行真实分片。
