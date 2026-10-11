"""真实事务中的路由验收；隔离 schema、Mock 云端和 Broker，不消耗 MinerU 额度。"""

from uuid import UUID

import pytest
from kombu.exceptions import OperationalError as BrokerError
from mineru_samples import png_bytes
from office_samples import office_bytes
from router_samples import office_with_part, router_pdf
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.api import admin_knowledge_bases as admin
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob
from app.parsers.common import OCRRequiredError, ParserError
from app.parsers.routing.guard import CompletenessError
from app.schemas.parsed_document import ParsedDocument
from app.services import routed_processing as routing
from app.services.document_processing import record_failure
from app.services.mineru.errors import MinerUError
from app.tasks import documents, mineru

pytest_plugins = ("business_logging_smoke",)
PREFIX = "/api/admin/knowledge-bases"


@pytest.fixture
def router_context(cloud_context, monkeypatch):
    """将路由和云短任务绑定同一保存点会话及安全配置，捕获原任务消息参数。"""

    context = cloud_context

    def settings():
        """不读取真实 Token，使用现有离线云上下文。"""

        return context.settings

    def accepted(**kwargs):
        """捕获路由消息但不运行真实队列，避免用户 Worker 消费测试任务。"""

        context.messages.append(kwargs)

    monkeypatch.setattr(documents, "get_settings", settings)
    monkeypatch.setattr(admin, "get_settings", settings)
    monkeypatch.setattr(documents, "processing_session", context.session)
    monkeypatch.setattr(documents.process_document, "apply_async", accepted)
    return context


def upload(context, kind="txt", content=b"private body", mode="auto"):
    """复用原 multipart 上传路径；每份原件独立 Document，mode 可省略验证兼容。"""

    response = context.api(
        "POST",
        f"{PREFIX}/{context.kb_id}/documents",
        files={"file": (f"路由样本.{kind}", content)},
        data={} if mode is None else {"parse_mode": mode},
    )
    assert response.status_code == 201, response.text
    return response.json()


def execute(document, mode="auto"):
    """同步执行路由代次；后续云任务仍被 Mock Broker 截获。"""

    return documents.process_document.apply(
        args=[document["doc_id"]],
        kwargs={} if mode is None else {"parse_mode": mode},
        task_id=document["task_id"],
        throw=False,
    )


def snapshot(context, document):
    """读取持久化状态、块与云身份，检验任务 SUCCESS 是否被误当作解析完成。"""

    with context.session() as session:
        row = session.get(Document, UUID(document["doc_id"]))
        blocks = session.scalars(
            select(DocumentBlock)
            .where(DocumentBlock.doc_id == row.doc_id)
            .order_by(DocumentBlock.block_index)
        ).all()
        jobs = session.scalars(
            select(DocumentParseJob).where(DocumentParseJob.doc_id == row.doc_id)
        ).all()
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
        return row, blocks, jobs


@pytest.mark.parametrize("kind", ["txt", "csv", "md", "docx", "xlsx", "pdf"])
def test_auto_native_zero_cloud_and_idempotence(router_context, event_stream, kind):
    """六种简单文档 0 云调用，最终块顺序/身份稳定，重复消息不增块。"""

    context = router_context
    content = (
        office_bytes(kind)
        if kind in {"docx", "xlsx"}
        else router_pdf()
        if kind == "pdf"
        else b"a,b\n1,2"
        if kind == "csv"
        else b"native private body"
    )
    document = upload(context, kind, content)
    assert context.messages[-1]["kwargs"] == {"parse_mode": "auto"}
    result = execute(document)
    assert result.state == "SUCCESS", str(result.result)
    assert result.result["status"] == "awaiting_chunk"
    assert result.result["route_decision"]["route_strategy"] == "native"
    assert result.result["mineru_file_submit_count"] == 0
    row, blocks, jobs = snapshot(context, document)
    identities = [block.block_id for block in blocks]
    assert row.status == "CHUNKING" and not jobs
    assert context.provider.submissions == 0
    assert execute(document).result["status"] == "skipped"
    assert [block.block_id for block in snapshot(context, document)[1]] == identities
    from business_logging_smoke import events

    recorded = events(event_stream)
    decision = next(value for value in recorded if value["event"] == "router.decided")
    assert (
        decision["route_strategy"] == "native"
        and decision["reason_codes"] == "NATIVE_COVERAGE_SUPPORTED"
    )
    assert "native private body" not in event_stream.getvalue()


@pytest.mark.parametrize("kind", ["png", "doc", "xls", "ppt", "pptx", "docx", "pdf"])
def test_auto_whole_cloud_handoff_and_one_submission(router_context, kind):
    """风险文件以新代次交接整份云任务，旧消息失效，每份只创建一个有效批次。"""

    context = router_context
    content = (
        png_bytes()
        if kind == "png"
        else router_pdf("same_page")
        if kind == "pdf"
        else office_with_part("docx", "word/media/image.png", png_bytes())
        if kind == "docx"
        else office_bytes(kind)
    )
    document = upload(context, kind, content)
    result = execute(document)
    assert result.state == "SUCCESS", str(result.result)
    job = result.result
    assert (
        job["status"] == "delegated_to_mineru" and job["task_id"] != document["task_id"]
    )
    row, blocks, jobs = snapshot(context, document)
    assert row.status == "PARSING" and not blocks and len(jobs) == 1
    assert jobs[0].parse_options["_route_decision"] == job["route_decision"]
    assert execute(document).result["status"] == "stale_task"
    assert (
        mineru.submit_mineru_parse.apply(args=[job["job_id"]], throw=False).state
        == "SUCCESS"
    )
    assert context.provider.submissions == 1
    assert (
        mineru.submit_mineru_parse.apply(args=[job["job_id"]], throw=False).state
        == "SUCCESS"
    )
    assert context.provider.submissions == 1
    context.provider.state = "done"
    completed = mineru.check_mineru_result.apply(args=[job["job_id"]], throw=False)
    assert completed.state == "SUCCESS", str(completed.result)
    assert completed.result["mineru_file_submit_count"] == 1
    assert completed.result["route_decision"] == job["route_decision"]
    row, blocks, jobs = snapshot(context, document)
    ids = [block.block_id for block in blocks]
    assert row.status == "CHUNKING" and jobs[0].state == "done"
    assert all(block.source == "mineru_cloud" for block in blocks)
    if kind in {"doc", "docx", "xls", "ppt", "pptx"}:
        assert all(block.bbox is None for block in blocks)
    assert (
        mineru.check_mineru_result.apply(args=[job["job_id"]], throw=False).result[
            "status"
        ]
        == "skipped"
    )
    assert ids == [block.block_id for block in snapshot(context, document)[1]]


@pytest.mark.parametrize("kind", ["md", "xlsx"])
def test_rejection_is_failed_not_success_review(router_context, kind):
    """缺附件 Markdown/孤立 XLSX 部件安全失败，任务不能把拒绝冒充 SUCCESS。"""

    context = router_context
    content = (
        b"![asset](missing.png)"
        if kind == "md"
        else office_with_part("xlsx", "xl/charts/chart1.xml", "<root/>")
    )
    document = upload(context, kind, content)
    result = execute(document)
    assert result.state == "FAILURE"
    row, blocks, jobs = snapshot(context, document)
    assert row.status == "FAILED" and "ROUTER_FAILED" in row.error_message
    assert not blocks and not jobs and context.provider.submissions == 0
    assert execute(document).result["status"] == "skipped"


@pytest.mark.parametrize("error_type", [OCRRequiredError, CompletenessError])
def test_one_safe_native_fallback(router_context, monkeypatch, error_type):
    """明确 OCR/完整性错误只交接一次整份 MinerU，Native 失败没有临时块落库。"""

    calls = []

    class MissingCoverage:
        """模拟预检之后才发现的 Native 覆盖缺口。"""

        def parse(self, **kwargs):
            """返回受控覆盖错误，不提供伪造 OCR 文本。"""

            calls.append(kwargs)
            raise error_type("存在未覆盖内容")

    def parser(kind):
        """为当前样本注入一次可解释的完整性失败。"""

        return MissingCoverage()

    monkeypatch.setattr(routing, "get_parser", parser)
    document = upload(router_context, "pdf", router_pdf())
    result = execute(document)
    assert result.state == "SUCCESS"
    decision = result.result["route_decision"]
    assert (
        decision["fallback_count"] == 1
        and "NATIVE_COMPLETENESS_FALLBACK" in decision["reason_codes"]
    )
    assert len(calls) == 1 and not snapshot(router_context, document)[1]
    assert execute(document).result["status"] == "stale_task" and len(calls) == 1


def test_generic_native_error_no_cloud_fallback(router_context, monkeypatch):
    """非法编码或任意格式失败不转换为付费云请求，安全错误保存在 Document。"""

    document = upload(router_context, "csv", b"\x81\x30\x81")
    result = execute(document)
    assert result.state == "FAILURE"
    row, blocks, jobs = snapshot(router_context, document)
    assert row.status == "FAILED" and "编码无法识别" in row.error_message
    assert not blocks and not jobs and router_context.provider.submissions == 0


@pytest.mark.parametrize("failure", ["auth", "remote", "missing_page"])
def test_cloud_failure_no_native_or_second_batch(router_context, monkeypatch, failure):
    """鉴权/远端失败/缺页面不退 Native，不追加新批次或持久化半份正文。"""

    context = router_context

    def forbidden(kind):
        """一旦选择整份云端，就不允许再调用 Native。"""

        raise AssertionError("发生了禁止的 Native 降级")

    monkeypatch.setattr(routing, "get_parser", forbidden)
    content = router_pdf("mixed_pages") if failure == "missing_page" else png_bytes()
    document = upload(context, "pdf" if failure == "missing_page" else "png", content)
    job = execute(document).result
    if failure == "auth":
        context.provider.error = MinerUError("A0202", "鉴权失败")
        assert (
            mineru.submit_mineru_parse.apply(args=[job["job_id"]], throw=False).state
            == "FAILURE"
        )
    else:
        assert (
            mineru.submit_mineru_parse.apply(args=[job["job_id"]], throw=False).state
            == "SUCCESS"
        )
        context.provider.state = "failed" if failure == "remote" else "done"
        context.provider.err_msg = "模型服务暂时不可用"
        assert (
            mineru.check_mineru_result.apply(args=[job["job_id"]], throw=False).state
            == "FAILURE"
        )
    row, blocks, jobs = snapshot(context, document)
    assert row.status == "FAILED" and not blocks and jobs[0].state == "failed"
    assert context.provider.submissions == 1
    assert execute(document).result["status"] == "stale_task"


def test_guard_runs_before_any_native_write(router_context, monkeypatch):
    """空结果触发结构检查但 TXT 不可送 MinerU，旧块仍完整保留。"""

    document = upload(router_context)
    with router_context.session() as session:
        session.add(
            DocumentBlock(
                doc_id=UUID(document["doc_id"]),
                page=1,
                block_index=1,
                block_type="text",
                text="旧证据",
                source="native_parser",
            )
        )
        session.commit()

    class EmptyParser:
        """模拟仅有空列表的坏解析结果。"""

        def parse(self, **kwargs):
            """返回可建模但没有正文的契约对象，禁止进入持久化。"""

            return ParsedDocument(
                doc_id=UUID(document["doc_id"]),
                file_name="路由样本.txt",
                file_type="txt",
                blocks=[],
            )

    def parser(kind):
        """注入空结果，不改现有 TXT Parser。"""

        return EmptyParser()

    monkeypatch.setattr(routing, "get_parser", parser)
    assert execute(document).state == "FAILURE"
    row, blocks, jobs = snapshot(router_context, document)
    assert row.status == "FAILED" and [block.text for block in blocks] == ["旧证据"]
    assert not jobs


def test_handoff_queue_failure_manual_resume_same_job(router_context, monkeypatch):
    """云投递失败保留代次和检查点；显式恢复同一 Job，不再上传或建批次。"""

    context = router_context
    document = upload(context, "png", png_bytes())

    def unavailable(**kwargs):
        """模拟含敏感错误的队列故障，详情只能使用固定安全提示。"""

        raise BrokerError("PRIVATE_BROKER_SECRET")

    with monkeypatch.context() as patch:
        patch.setattr(mineru.submit_mineru_parse, "apply_async", unavailable)
        result = execute(document)
    assert (
        result.state == "SUCCESS" and result.result["status"] == "cloud_dispatch_failed"
    )
    row, blocks, jobs = snapshot(context, document)
    assert (
        row.status == "PARSING" and jobs[0].is_active and jobs[0].error_code == "QUEUE"
    )
    assert "PRIVATE_BROKER_SECRET" not in row.error_message and not blocks
    response = context.api(
        "POST", f"{PREFIX}/{context.kb_id}/documents/{document['doc_id']}/mineru"
    )
    assert (
        response.status_code == 202
        and response.json()["job_id"] == result.result["job_id"]
    )
    assert len(snapshot(context, document)[2]) == 1


@pytest.mark.parametrize("mode", ["native", "mineru", None])
def test_explicit_mode_and_omitted_compatibility(router_context, mode):
    """显式 Native 做范围对照；省略模式与显式 MinerU 都可自动交接云解析。"""

    context = router_context
    document = upload(
        context,
        "docx",
        office_with_part("docx", "word/media/image.png", png_bytes()),
        mode,
    )
    message = context.messages[-1]
    assert message["kwargs"] == {"parse_mode": mode or "auto"}
    result = execute(document, mode)
    assert result.state == "SUCCESS"
    if mode != "native":
        assert result.result["status"] == "delegated_to_mineru"
        assert result.result["route_decision"]["parse_mode"] == (mode or "auto")
    else:
        assert (
            result.result["status"] == "awaiting_chunk"
            and not snapshot(context, document)[2]
        )
        assert result.result["coverage"] == "explicit_native_scope"


def test_resubmit_modes_batch_and_invalid_mode(router_context):
    """原重投 Query 与批量 Form 传递相同模式，非法 hybrid 在文件保存前拒绝。"""

    context = router_context
    base = f"{PREFIX}/{context.kb_id}/documents"
    rejected = context.api(
        "POST",
        base,
        files={"file": ("无效.txt", b"invalid mode")},
        data={"parse_mode": "hybrid"},
    )
    assert rejected.status_code == 422
    response = context.api(
        "POST",
        base,
        files=[
            ("file", ("一.txt", b"first native")),
            ("file", ("二.txt", b"second native")),
        ],
        data={"parse_mode": "auto"},
    )
    assert response.status_code == 200
    assert [message["kwargs"] for message in context.messages[-2:]] == [
        {"parse_mode": "auto"}
    ] * 2
    document = upload(context, "md", b"![missing](missing.png)")
    assert execute(document).state == "FAILURE"
    current = context.api(
        "POST", base + f"/{document['doc_id']}/process?parse_mode=native"
    )
    assert current.status_code == 202 and context.messages[-1]["kwargs"] == {
        "parse_mode": "native"
    }
    assert execute(document).result["status"] == "stale_task"
    assert execute(current.json(), "native").result["status"] == "awaiting_chunk"


def test_hash_mutation_and_stale_failure_protected(router_context):
    """路由不解析被改写原件，旧 Native 失败不能覆盖交接后的云任务。"""

    context = router_context
    document = upload(context)
    with context.session() as session:
        path = routing.source_path(session.get(Document, UUID(document["doc_id"])))
    path.write_bytes(b"changed bytes")
    assert execute(document).state == "FAILURE"
    assert "Hash" in snapshot(context, document)[0].error_message
    other = upload(context, "png", png_bytes())
    assert execute(other).result["status"] == "delegated_to_mineru"
    with context.session() as session, session.begin():
        record_failure(
            session,
            UUID(other["doc_id"]),
            UUID(other["task_id"]),
            "过期 Native 错误",
            True,
        )
    assert snapshot(context, other)[0].status == "PARSING"


@pytest.mark.parametrize("transient", [False, True])
def test_routed_native_transaction_failure_atomic(
    router_context, monkeypatch, transient
):
    """新路由写块失败也整体回滚；受控重试保持稳定 ID，永久失败不假成功。"""

    context = router_context
    document = upload(context)
    original = routing.persist_blocks
    calls = []

    def failing_write(session, row, parsed):
        """先真实 flush 新块再模拟故障，验证旧块和状态不是部分提交。"""

        original(session, row, parsed)
        calls.append([block.block_id for block in parsed.blocks])
        if len(calls) == 1:
            if transient:
                raise OperationalError("SECRET SQL", None, Exception("SECRET"))
            raise ParserError("模拟永久写入失败")

    monkeypatch.setattr(routing, "persist_blocks", failing_write)
    result = execute(document)
    row, blocks, jobs = snapshot(context, document)
    assert not jobs and context.provider.submissions == 0
    if transient:
        assert result.state == "SUCCESS" and row.status == "CHUNKING"
        assert len(calls) == 2 and calls[0] == calls[1]
        assert [block.block_id for block in blocks] == calls[1]
    else:
        assert result.state == "FAILURE" and row.status == "FAILED" and not blocks


def test_native_guard_missing_page_falls_back_before_write(router_context, monkeypatch):
    """真实 Guard 检出缺页后只交接一次，不依赖 Parser 主动抛异常才保护。"""

    from app.parsers.common import make_block

    context = router_context
    document = upload(context, "pdf", router_pdf("mixed_pages"))
    # 此用例单独控制预检，使缺页只能在 Guard 阶段被发现。
    original = routing.inspect_document

    def simple_preflight(path, kind, settings):
        """保留真实三页 required 摘要，仅移除风险以测试二次保护。"""

        return original(path, kind, settings).model_copy(update={"risks": ()})

    class PartialParser:
        """模拟只返回第一页的 Native，不能把部分正文持久化。"""

        def parse(self, **kwargs):
            """返回合法契约但不覆盖所有非空物理页。"""

            identifier = UUID(document["doc_id"])
            return ParsedDocument(
                doc_id=identifier,
                file_name="路由样本.pdf",
                file_type="pdf",
                blocks=[
                    make_block(
                        identifier, 1, page=1, block_type="text", text="仅第一页"
                    )
                ],
            )

    def parser(kind):
        """注入部分结果，现有 Parser 实现不修改。"""

        return PartialParser()

    monkeypatch.setattr(routing, "inspect_document", simple_preflight)
    monkeypatch.setattr(routing, "get_parser", parser)
    result = execute(document)
    assert (
        result.state == "SUCCESS"
        and result.result["route_decision"]["fallback_count"] == 1
    )
    assert not snapshot(context, document)[1]
