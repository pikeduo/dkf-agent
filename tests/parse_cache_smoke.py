"""用户执行的缓存事务验收；Stub 字节/元数据，不生成或解析 PDF、Office、图片样本。"""

from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.api import admin_knowledge_bases as admin
from app.models import Document, DocumentBlock, DocumentParseJob
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.parsers.routing.contracts import Inspection
from app.services import routed_processing
from app.services import parse_cache
from app.services import document_processing
from app.services.document_processing import transition_document
from app.services.mineru import jobs
from app.services.mineru.errors import MinerUError

pytest_plugins = ("mineru_db_smoke",)


@pytest.fixture
def cache_db(cloud_context, monkeypatch):
    """复用隔离数据库与 Mock Provider，原件/Adapter/ZIP 读取仅使用 Stub，不打开文档。"""

    ctx = cloud_context
    ctx.hash = parse_cache.digest(b"source-stub")

    class SourceStub:
        """只提供封闭原件字节流，禁止真实文件访问。"""

        def open(self, mode):
            """返回新的内存流供 SHA256 核对，不是图片解码或文档解析。"""

            return BytesIO(b"source-stub")

    def source(document):
        """不读取 document.file_path，所有测试身份共享同一精确内容。"""

        return SourceStub()

    def validate(file):
        """替代原件/页数读取，页覆盖由 Adapter 元数据 Stub 固定为一页。"""

        return 1

    def geometry(path, kind):
        """不打开图片/PDF，只返回 Stub 像素尺寸。"""

        return {0: (100.0, 100.0)}

    def normalize(self, archive, **kwargs):
        """仅构造统一 Block，不执行 ZIP/文档 Parser，不宣称真实 OCR 已通过。"""

        return ParsedDocument(doc_id=kwargs["doc_id"], file_name=kwargs["file_name"], file_type=kwargs["file_type"],
            blocks=[ParsedBlock(block_id=uuid4(), page=1, block_type="formula", text="x + y",
                                source="mineru_cloud", bbox=[1.0, 2.0, 3.0, 4.0])])

    def artifacts(archive):
        """持久化固定字节 Stub，不创建 ZIP/PDF 文件结构或执行解压。"""

        return {"raw/mineru_result.zip": b"result-byte-stub", "content_list.json": b"[]"}

    def checked(document):
        """存在性由 Stub 保证，恢复仍实际校验内存原件 Hash，不打开测试文档。"""

    def settings():
        """返回隔离配置，避免 API/Worker 在测试时写入真实缓存目录。"""

        return ctx.settings

    def inspect_stub(path, kind, settings):
        """提供图片的只读预检 Stub，无结构风险，不打开或解析任何文档。"""

        return Inspection()

    monkeypatch.setattr(jobs, "source_path", source)
    monkeypatch.setattr(jobs, "validate_local_file", validate)
    monkeypatch.setattr(jobs, "page_geometry", geometry)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", normalize)
    monkeypatch.setattr(jobs, "archive_artifacts", artifacts)
    monkeypatch.setattr(jobs, "inspect_document", inspect_stub)
    monkeypatch.setattr(admin, "get_settings", settings)
    monkeypatch.setattr(document_processing, "check_source", checked)
    return ctx


def new_document(ctx):
    """插入 Stub 文档，无上传样本；可在同一隔离库模拟跨库相同文件内容的复用。"""

    from app.models import KnowledgeBase

    with ctx.session() as session:
        kb = KnowledgeBase(name=f"缓存隔离-{uuid4().hex}")
        session.add(kb)
        session.flush()
        document = Document(doc_id=uuid4(), kb_id=kb.kb_id, file_name="stub.png", file_type="png",
            file_hash=ctx.hash, file_path="never-opened.png", source_type="uploaded",
            status="UPLOADED", processing_task_id=uuid4())
        session.add(document)
        session.commit()
        return document.doc_id, document.kb_id


def start(ctx, doc_id, *, force=False):
    """独立事务创建任务，force 仅模拟管理员已明确确认的 Reparse。"""

    with ctx.session() as session:
        job = jobs.create_job(session, session.get(Document, doc_id), ctx.settings, force_reparse=force)
        session.commit()
        return job.job_id


def complete(ctx, job_id):
    """只执行 Mock Provider 的两个短步骤，所有计数都来自离线 Mock。"""

    with ctx.session() as session:
        jobs.submit_step(session, job_id, ctx.provider, ctx.settings)
    ctx.provider.state = "done"
    with ctx.session() as session:
        return jobs.check_step(session, job_id, ctx.provider, ctx.settings)


def test_cross_document_cache_hit_zero_additional_cloud(cache_db):
    """相同原件/参数跨文档命中，缓存 Job done/batch 空，不追加 POST/PUT/GET/下载。"""

    ctx = cache_db
    first, _ = new_document(ctx)
    job = start(ctx, first)
    assert complete(ctx, job) == ("done", 1)
    counts = (ctx.provider.submissions, ctx.provider.uploads, ctx.provider.queries, ctx.provider.downloads)
    second, _ = new_document(ctx)
    hit = start(ctx, second)
    with ctx.session() as session:
        row = session.get(DocumentParseJob, hit)
        assert row.state == "done" and row.batch_id is None and not row.upload_complete
        blocks = session.scalars(select(DocumentBlock).where(DocumentBlock.doc_id.in_([first, second]))).all()
        assert len(blocks) == len({b.block_id for b in blocks}) == 2
        assert session.get(Document, second).status == "CHUNKING"
    assert counts == (ctx.provider.submissions, ctx.provider.uploads, ctx.provider.queries, ctx.provider.downloads)


def test_reindex_no_parser_provider_and_idempotent(cache_db, monkeypatch):
    """缓存回放不预检/解析/建云 Job；当前代次重复消息不重新替换或产生重复块。"""

    ctx = cache_db
    doc_id, _ = new_document(ctx)
    job = start(ctx, doc_id)
    complete(ctx, job)
    task_id = uuid4()

    def forbidden(*args, **kwargs):
        """任何预检/Parser/云交接均表明 Reindex 错误地重新解析。"""

        raise AssertionError("Reindex 只能使用缓存")

    def checked(document):
        """替代原件存在性检查，文件 Hash 仍由 SourceStub 严格核对。"""

    monkeypatch.setattr(routed_processing, "check_source", checked)
    monkeypatch.setattr(routed_processing, "source_path", jobs.source_path)
    monkeypatch.setattr(routed_processing, "inspect_document", forbidden)
    monkeypatch.setattr(routed_processing, "get_parser", forbidden)
    monkeypatch.setattr(routed_processing, "create_job", forbidden)
    with ctx.session() as session:
        document = session.get(Document, doc_id)
        before = session.scalars(select(DocumentBlock.block_id).where(DocumentBlock.doc_id == doc_id)).all()
        document.status, document.processing_task_id = "UPLOADED", task_id
        session.commit()
        result = routed_processing.run_routed_step(session, doc_id, task_id, "auto", ctx.settings, cache_only=True)
        session.commit()
        assert result["parse_cache_hit"] and result["mineru_file_submit_count"] == 0
        assert routed_processing.run_routed_step(session, doc_id, task_id, "auto", ctx.settings, cache_only=True)["status"] == "skipped"
        assert session.scalars(select(DocumentBlock.block_id).where(DocumentBlock.doc_id == doc_id)).all() == before
    assert ctx.provider.submissions == ctx.provider.queries == ctx.provider.downloads == 1


def test_paid_failure_normal_retry_refuses_force_only(cache_db):
    """收费失败无缓存时普通重投拒绝新 POST；Force Reparse 才创建第二份文件任务。"""

    ctx = cache_db
    doc_id, _ = new_document(ctx)
    job_id = start(ctx, doc_id)
    with ctx.session() as session:
        jobs.submit_step(session, job_id, ctx.provider, ctx.settings)
        jobs.record_error(session, job_id, MinerUError("REMOTE_FAILED", "安全失败"), ctx.settings)
    with pytest.raises(MinerUError) as caught:
        start(ctx, doc_id)
    assert caught.value.code == "CACHE_REPARSE_REQUIRED" and ctx.provider.submissions == 1
    forced = start(ctx, doc_id, force=True)
    complete(ctx, forced)
    assert ctx.provider.submissions == 2


def test_force_reparse_skips_ready_cache_but_active_recovery_same_job(cache_db):
    """显式 Force 可绕过成功缓存；活动恢复仍同 Job/batch，不把 Force 扩大为重复提交。"""

    ctx = cache_db
    doc_id, _ = new_document(ctx)
    complete(ctx, start(ctx, doc_id))
    with ctx.session() as session:
        session.get(Document, doc_id).status = "UPLOADED"
        session.commit()
    forced = start(ctx, doc_id, force=True)
    assert start(ctx, doc_id) == forced
    complete(ctx, forced)
    assert ctx.provider.submissions == 2


def test_pending_same_content_has_one_owner_no_duplicate_task(cache_db):
    """前一内容尚在解析时第二文档 fail-closed，不并行创建第二个收费 Job。"""

    ctx = cache_db
    first, _ = new_document(ctx)
    start(ctx, first)
    second, _ = new_document(ctx)
    with pytest.raises(MinerUError) as caught:
        start(ctx, second)
    assert caught.value.code == "CACHE_IN_PROGRESS" and ctx.provider.submissions == 0
    with ctx.session() as session:
        assert not session.scalars(select(DocumentParseJob).where(DocumentParseJob.doc_id == second)).all()


def test_db_rollback_restores_same_job_without_cloud_queries(cache_db, monkeypatch):
    """完整结果已缓存但 SQL 写入临时失败，同 Job 重试仅重放，不再 GET/下载或收费。"""

    ctx = cache_db
    doc_id, _ = new_document(ctx)
    job_id = start(ctx, doc_id)
    with ctx.session() as session:
        jobs.submit_step(session, job_id, ctx.provider, ctx.settings)
    ctx.provider.state = "done"
    original = jobs.persist_blocks

    def fail(session, document, parsed):
        """模拟数据库短暂写失败，外层回滚保留原状态而缓存存储已完成。"""

        raise OperationalError("fixture", {}, Exception("private SQL must not leak"))

    monkeypatch.setattr(jobs, "persist_blocks", fail)
    with pytest.raises(OperationalError):
        with ctx.session() as session:
            jobs.check_step(session, job_id, ctx.provider, ctx.settings)
    monkeypatch.setattr(jobs, "persist_blocks", original)
    with ctx.session() as session:
        assert jobs.check_step(session, job_id, ctx.provider, ctx.settings) == ("done", 1)
    assert ctx.provider.submissions == ctx.provider.queries == ctx.provider.downloads == 1


def test_reparse_requires_confirmation_and_maintenance_cannot_override_active(cache_db):
    """API 没有 force=true 不会排队或收费，活动文档不能通过维护入口换代次。"""

    ctx = cache_db
    doc_id, kb_id = new_document(ctx)
    prefix = f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}"
    assert ctx.api("POST", prefix + "/reparse").status_code == 400
    start(ctx, doc_id)
    assert ctx.api("POST", prefix + "/reparse?force=true").status_code == 409
    assert ctx.api("POST", prefix + "/reindex").status_code == 409
