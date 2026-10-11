"""用户执行的隔离事务测试；纯 Stub 文档与 Mock 云端，不生成 Office/PDF 验收素材。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import Document, DocumentBlock, DocumentParseJob
from app.parsers.hybrid import HybridPreflightError, digest
from app.parsers.routing.contracts import Inspection, StructuralSignals
from app.services import hybrid_processing, routed_processing
from app.services import xlsx_primary
from app.services.mineru import jobs
from app.services.mineru.errors import MinerUError
from test_hybrid import metadata_plan, visual_result

pytest_plugins = ("mineru_db_smoke",)


@pytest.fixture
def hybrid_context(cloud_context, monkeypatch):
    """只插入 Stub Document；Parser、Bundle 文件、Adapter 和 Broker 全隔离，不生成样本。"""

    context = cloud_context
    task_id, doc_id = uuid4(), uuid4()
    context.settings = context.settings.model_copy(update={"hybrid_xlsx_enabled": True})
    with context.session() as session:
        session.add(Document(doc_id=doc_id, kb_id=UUID(context.kb_id), file_name="source.xlsx",
            file_type="xlsx", file_path="stub-never-opened.xlsx", file_hash=digest(b"stub-source"),
            source_type="uploaded", status="UPLOADED", processing_task_id=task_id))
        session.commit()
    context.doc_id, context.task_id = doc_id, task_id
    context.plan = metadata_plan(doc_id)

    class SourceStub:
        """模拟原件流供 Hash 核对，不读取或写入任何文档。"""

        def open(self, mode):
            """每次返回独立内存字节流。"""
            return BytesIO(b"stub-source")

    def check_stub(document):
        """不打开原件；文件缺失行为由既有用例覆盖。"""

    def source_stub(document):
        """为路由和检查点 Hash 提供封闭内存流。"""
        return SourceStub()

    def inspection_stub(path, kind, settings):
        """提供两 Sheet、两张视觉图的预检摘要，不运行 Parser。"""
        return Inspection(risks=("XLSX_HAS_EMBEDDED_IMAGE",),
                          signals=StructuralSignals(sheet_count=2, nonempty_sheet_count=2, image_count=2))

    def checkpoint_stub(document, routing_task_id, settings, decision):
        """返回纯元数据计划，不调用 Bundle 生成或磁盘检查点写入。"""
        plan = deepcopy(context.plan)
        return plan, {"version": plan.manifest.version, "source_hash": document.file_hash,
            "routing_task_id": str(routing_task_id), "manifest": plan.manifest.model_dump(mode="json")}

    def file_stub(document, checkpoint):
        """模拟受检 Bundle 地址和 Hash；Provider 也是 Mock，不会读取该路径。"""
        return Path("stub-visual-bundle.pdf"), context.plan.manifest.bundle_hash

    def validate_stub(source):
        """不打开 PDF，仅供短步骤流程计数使用。"""
        return 2

    def geometry_stub(path, kind):
        """模拟两页画布，不读取 PDF。"""
        assert kind == "pdf"
        return {0: (100.0, 100.0), 1: (100.0, 100.0)}

    def normalize_stub(self, archive, **kwargs):
        """注入合法 Adapter 契约，ZIP 数据不被解析。"""
        assert kwargs["file_type"] == "pdf"
        return visual_result(context.plan)

    def regenerate_stub(document, checkpoint):
        """不运行 Native/Extractor，只恢复同一元数据计划。"""
        return deepcopy(context.plan)

    monkeypatch.setattr(routed_processing, "check_source", check_stub)
    monkeypatch.setattr(routed_processing, "source_path", source_stub)
    monkeypatch.setattr(routed_processing, "inspect_document", inspection_stub)
    monkeypatch.setattr(routed_processing, "prepare_checkpoint", checkpoint_stub)
    monkeypatch.setattr(hybrid_processing, "source_path", source_stub)
    monkeypatch.setattr(hybrid_processing, "regenerate_plan", regenerate_stub)
    monkeypatch.setattr(jobs, "bundle_file", file_stub)
    monkeypatch.setattr(jobs, "validate_local_file", validate_stub)
    monkeypatch.setattr(jobs, "page_geometry", geometry_stub)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", normalize_stub)
    return context


def route(context):
    """独立事务提交本代次 Hybrid 云交接，不使用实际 Celery 消息。"""

    with context.session() as session, session.begin():
        return routed_processing.run_routed_step(session, context.doc_id, context.task_id,
                                                 "auto", context.settings)


def snapshot(context):
    """查询隔离 schema 的当前状态及 Block，不将云任务成功等同于内容验收。"""

    with context.session() as session:
        document = session.get(Document, context.doc_id)
        blocks = session.scalars(select(DocumentBlock).where(DocumentBlock.doc_id == context.doc_id)
                                  .order_by(DocumentBlock.block_index)).all()
        parse_jobs = session.scalars(select(DocumentParseJob).where(DocumentParseJob.doc_id == context.doc_id)).all()
        return document, blocks, parse_jobs


def submit(context, job_id):
    """用户运行时仅调用 Mock Provider，按已有检查点执行单次申请/上传。"""

    with context.session() as session:
        return jobs.submit_step(session, job_id, context.provider, context.settings)


def check(context, job_id):
    """单次 Mock 查询及事务合并，不轮询、不请求真实网络。"""

    with context.session() as session:
        return jobs.check_step(session, job_id, context.provider, context.settings)


def test_one_submission_atomic_merge_resume_and_idempotence(hybrid_context):
    """交接后零块；活动恢复同 Job，最终一次提交完整块，重复短消息不增块。"""

    context = hybrid_context
    result = route(context)
    job_id = UUID(result["job_id"])
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "PARSING" and blocks == [] and len(parse_jobs) == 1
    assert result["planned_mineru_file_count"] == 1
    with context.session() as session:
        resumed = jobs.create_job(session, session.get(Document, context.doc_id), context.settings)
        assert resumed.job_id == job_id
    assert submit(context, job_id)[0] == "check"
    assert submit(context, job_id)[0] == "check"
    assert context.provider.submissions == context.provider.uploads == 1
    context.provider.state = "done"
    assert check(context, job_id) == ("done", 4)
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "CHUNKING" and parse_jobs[0].state == "done"
    assert [block.section for block in blocks] == ["第一表", "第一表", "第二表", "第二表"]
    assert all(block.page == 1 and block.bbox is None for block in blocks)
    assert "=B2*2" in blocks[0].text
    assert len({block.block_id for block in blocks}) == 4
    assert check(context, job_id) == ("skipped", 0)
    assert submit(context, job_id) == ("skipped", 0)
    assert len(snapshot(context)[1]) == 4 and context.provider.submissions == 1


def test_upload_retry_stays_same_batch(hybrid_context):
    """签名 PUT 临时失败后沿用原批次，不逐元素或重新申请文件。"""

    context = hybrid_context
    job_id = UUID(route(context)["job_id"])
    context.provider.upload_error = MinerUError("UPLOAD_NETWORK", "安全临时错误", retryable=True)
    with pytest.raises(MinerUError):
        submit(context, job_id)
    context.provider.upload_error = None
    submit(context, job_id)
    assert context.provider.submissions == 1 and context.provider.uploads == 2
    assert snapshot(context)[1] == []


@pytest.mark.parametrize("stage", ["remote", "merge", "persist"])
def test_failure_no_partial_blocks_no_second_whole(hybrid_context, monkeypatch, stage):
    """供应商/合并/写库失败全回滚并记录 FAILED，不自动提交第二个 Whole 任务。"""

    context = hybrid_context
    job_id = UUID(route(context)["job_id"])
    submit(context, job_id)
    context.provider.state = "failed" if stage == "remote" else "done"

    def bad_merge(*arguments):
        """模拟映射失败，安全异常不带供应商原文。"""
        raise MinerUError("HYBRID_MERGE", "视觉映射无法完整覆盖")

    def bad_persist(session, document, parsed):
        """真正写入隔离事务后制造失败，检验所有 Block 能一起回滚。"""
        from app.services.document_processing import persist_blocks

        persist_blocks(session, document, parsed)
        raise OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE PATH"))

    if stage == "merge":
        monkeypatch.setattr(jobs, "complete_hybrid", bad_merge)
    elif stage == "persist":
        monkeypatch.setattr(jobs, "persist_blocks", bad_persist)
    with pytest.raises((MinerUError, OperationalError)):
        check(context, job_id)
    assert snapshot(context)[1] == []
    with context.session() as session:
        assert not jobs.record_error(session, job_id, MinerUError("HYBRID_FAILED", "安全失败"), context.settings)
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "FAILED" and not blocks and not parse_jobs[0].is_active
    assert context.provider.submissions == 1
    ledger = parse_jobs[0].parse_options["_hybrid_result"]
    assert all(entry["state"] == "failed" for entry in ledger["entries"])
    assert check(context, job_id) == ("skipped", 0)


def test_resource_limit_is_failure_without_cloud(hybrid_context, monkeypatch):
    """候选超限进入可追踪失败，不借 Whole 绕过预算，零云身份、零最终 Block。"""

    def limit_stub(*arguments):
        """不生成大文档，直接注入资源限制异常。"""
        raise HybridPreflightError("HYBRID_RESOURCE_LIMIT")

    monkeypatch.setattr(routed_processing, "prepare_checkpoint", limit_stub)
    result = route(hybrid_context)
    document, blocks, parse_jobs = snapshot(hybrid_context)
    assert result["status"] == "failed" and "HYBRID_RESOURCE_LIMIT" in result["route_decision"]["reason_codes"]
    assert document.status == "FAILED" and not blocks and not parse_jobs
    assert hybrid_context.provider.submissions == 0


def test_filtered_only_atomic_native_zero_cloud(hybrid_context):
    """所有候选被确定性过滤时零云 Job，主结果完整落库，不强迫使用 Token。"""

    context = hybrid_context
    context.plan.manifest.bundle_page_count = 0
    context.plan.manifest.bundle_hash = digest(b"")
    context.plan.bundle = b""
    for entry in context.plan.manifest.entries:
        entry.state, entry.filter_reason, entry.bundle_page = "filtered", "small", None
    result = route(context)
    document, blocks, parse_jobs = snapshot(context)
    assert result["status"] == "awaiting_chunk" and result["mineru_file_submit_count"] == 0
    assert document.status == "CHUNKING" and len(blocks) == 2 and parse_jobs == []
    assert context.provider.submissions == 0 and "=B2*2" in blocks[0].text


def test_changed_configuration_does_not_replace_active_bundle(hybrid_context):
    """关闭开关不改变已创建活动 Job 的冻结输入；更换云模型参数拒绝覆盖。"""

    context = hybrid_context
    job_id = UUID(route(context)["job_id"])
    with context.session() as session:
        document = session.get(Document, context.doc_id)
        restored = jobs.create_job(session, document,
            context.settings.model_copy(update={"hybrid_xlsx_enabled": False}))
        assert restored.job_id == job_id
        with pytest.raises(MinerUError, match="有效解析任务"):
            jobs.create_job(session, document,
                context.settings.model_copy(update={"mineru_language": "en"}))
    assert context.provider.submissions == 0 and len(snapshot(context)[2]) == 1


@pytest.fixture
def whole_context(hybrid_context, monkeypatch):
    """模拟提交前 Hybrid 能力缺口；Native 保护与 Office 云输出均为元数据 Stub。"""

    context = hybrid_context

    def unsupported_chart(*arguments):
        """不解析文件，模拟无法结构化的复杂 Chart。"""
        raise HybridPreflightError("HYBRID_UNSUPPORTED_CHART")

    def primary_checkpoint(document, settings, decision):
        """冻结 Native 摘要，不写主结果正文或临时文档。"""
        return xlsx_primary.PrimaryCheckpoint(doc_id=document.doc_id, source_hash=document.file_hash,
            native_hash=digest(context.plan.native.model_dump_json().encode()),
            signals=decision.signals).model_dump(mode="json")

    def primary_stub(document, signals):
        """供合并恢复同一份公式原式 Stub，不执行 Native Parser。"""
        return deepcopy(context.plan.native)

    def geometry_stub(path, kind):
        """Office 原件没有可映射坐标，不伪造 PDF geometry。"""
        assert kind == "xlsx"
        return None

    def normalize_stub(self, archive, **kwargs):
        """模拟整份云逻辑页结果，计算值不能覆盖 Native 原式。"""
        assert kwargs["file_type"] == "xlsx"
        result = visual_result(context.plan)
        for block in result.blocks:
            block.bbox, block.text = None, "246"
        return result.model_copy(update={"file_type": "xlsx"})

    monkeypatch.setattr(routed_processing, "prepare_checkpoint", unsupported_chart)
    monkeypatch.setattr(jobs, "prepare_primary", primary_checkpoint)
    monkeypatch.setattr(xlsx_primary, "primary_result", primary_stub)
    monkeypatch.setattr(jobs, "page_geometry", geometry_stub)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", normalize_stub)
    return context


def test_preflight_fallback_one_whole_and_original_formulas(whole_context):
    """提交前缺口只建一个 Whole Job；活动恢复同 Job，完成后 Native 公式不丢失。"""

    context = whole_context
    result = route(context)
    decision = result["route_decision"]
    assert decision["route_strategy"] == "mineru_whole" and decision["fallback_count"] == 1
    assert "HYBRID_UNSUPPORTED_CHART" in decision["reason_codes"]
    assert "HYBRID_PREFLIGHT_WHOLE_FALLBACK" in decision["reason_codes"]
    job_id = UUID(result["job_id"])
    assert context.provider.submissions == 0 and not snapshot(context)[1]
    with context.session() as session:
        job = session.get(DocumentParseJob, job_id)
        assert "_hybrid" not in job.parse_options and "_xlsx_primary" in job.parse_options
        assert jobs.local_file(session.get(Document, context.doc_id), job).name == "source.xlsx"
        assert jobs.create_job(session, session.get(Document, context.doc_id), context.settings).job_id == job_id
    assert route(context)["status"] == "stale_task"
    submit(context, job_id)
    context.provider.state = "done"
    assert check(context, job_id) == ("done", 4)
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "CHUNKING" and parse_jobs[0].state == "done"
    assert "=B2*2" in blocks[0].text and blocks[0].source == "native_parser"
    assert [block.section for block in blocks[:2]] == ["第一表", "第二表"]
    assert all(block.source == "mineru_cloud" and block.bbox is None for block in blocks[2:])
    assert context.provider.submissions == 1 and check(context, job_id) == ("skipped", 0)


@pytest.mark.parametrize("code", ["HYBRID_INVALID_PACKAGE", "HYBRID_INVALID_IMAGE",
                                 "HYBRID_EXTERNAL_ASSET_MISSING", "HYBRID_ORPHAN_PART"])
def test_corrupt_or_missing_assets_never_attempt_whole(hybrid_context, monkeypatch, code):
    """损坏/缺附件/孤立部件给出具体安全理由，不创建付费云身份或半份 Block。"""

    def rejected(*arguments):
        """用固定原因模拟拒绝，不创建损坏测试文档。"""
        raise HybridPreflightError(code)

    monkeypatch.setattr(routed_processing, "prepare_checkpoint", rejected)
    result = route(hybrid_context)
    document, blocks, parse_jobs = snapshot(hybrid_context)
    assert result["status"] == "failed" and code in document.error_message
    assert not blocks and not parse_jobs and hybrid_context.provider.submissions == 0


def test_whole_fallback_remote_failure_no_primary_partial_result(whole_context):
    """回退 Whole 后远端失败，不把 Native 主表单独判成功，也不再新建批次。"""

    context = whole_context
    job_id = UUID(route(context)["job_id"])
    submit(context, job_id)
    context.provider.state = "failed"
    with pytest.raises(MinerUError) as caught:
        check(context, job_id)
    with context.session() as session:
        assert not jobs.record_error(session, job_id, caught.value, context.settings)
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "FAILED" and not blocks and not parse_jobs[0].is_active
    assert context.provider.submissions == 1


def test_whole_limits_checked_before_any_cloud_submission(whole_context, monkeypatch):
    """Whole 原件同样受平台文件限制，不因 Hybrid 拒绝而绕过最终提交校验。"""

    def too_large(source):
        """模拟文件大小超限，不创建大文件。"""
        raise MinerUError("FILE_SIZE", "MinerU 文件超过安全上限")

    monkeypatch.setattr(jobs, "validate_local_file", too_large)
    with pytest.raises(MinerUError, match="安全上限"):
        route(whole_context)
    assert snapshot(whole_context)[1:] == ([], [])
    assert whole_context.provider.submissions == 0


def test_uncertain_server_error_cannot_create_paid_duplicate(hybrid_context):
    """非幂等申请 5xx 保留不确定检查点，停止重发，不因可重试 HTTP 分类追加费用。"""

    context = hybrid_context
    job_id = UUID(route(context)["job_id"])
    context.provider.error = MinerUError("HTTP_503", "服务端暂时异常", retryable=True)
    with pytest.raises(MinerUError) as caught:
        submit(context, job_id)
    assert caught.value.code == "SUBMIT_UNCERTAIN" and not caught.value.retryable
    with context.session() as session:
        assert not jobs.record_error(session, job_id, caught.value, context.settings)
    assert context.provider.submissions == 1
    assert submit(context, job_id) == ("skipped", 0)
    assert not snapshot(context)[1]


@pytest.mark.parametrize("failure", ["primary", "persist"])
def test_whole_primary_or_persistence_failure_keeps_old_blocks(whole_context, monkeypatch, failure):
    """主原式检查点或全量写库失败保留旧块，云批次不新增，不能提交 Native 半结果。"""

    context = whole_context
    with context.session() as session:
        session.add(DocumentBlock(doc_id=context.doc_id, page=1, block_index=1,
                                 block_type="text", text="旧完整证据", source="native_parser"))
        session.commit()
    job_id = UUID(route(context)["job_id"])
    submit(context, job_id)
    context.provider.state = "done"
    if failure == "primary":
        context.plan.native.blocks[0].text = "原式被修改"
    else:
        original = jobs.persist_blocks

        def fail_after_flush(session, document, parsed):
            """写入新块后故障，让上下文回滚真实隔离事务。"""
            original(session, document, parsed)
            raise OperationalError("PRIVATE_SQL", {}, RuntimeError("PRIVATE_PATH"))

        monkeypatch.setattr(jobs, "persist_blocks", fail_after_flush)
    with pytest.raises((MinerUError, OperationalError)):
        check(context, job_id)
    assert [block.text for block in snapshot(context)[1]] == ["旧完整证据"]
    with context.session() as session:
        jobs.record_error(session, job_id, MinerUError("WHOLE_RESULT_FAILED", "安全失败"), context.settings)
    document, blocks, parse_jobs = snapshot(context)
    assert document.status == "FAILED" and [block.text for block in blocks] == ["旧完整证据"]
    assert len(parse_jobs) == 1 and context.provider.submissions == 1
