"""Adapter之后的Guard、缓存与落库边界回归；纯Mock，由用户手动执行。"""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import business_logging as logs
from app.parsers.routing.contracts import RouteDecision, StructuralSignals
from app.schemas.parsed_document import BlockProvenance, ParsedBlock, ParsedDocument, ParseQuality
from app.services import document_management as management
from app.services.mineru import jobs
from app.services.parse_cache import NORMALIZER_VERSION
from app.tasks import mineru as tasks
from app.tasks.documents import DocumentProcessingError
from test_mineru_local_replay import forbid_cloud, stub_local_conversion
from test_mineru_recovery import context


def post_adapter_context(context, monkeypatch, content="COMPLETE"):
    """隔离原件/Adapter边界，保留真实Guard和任务入口，不创建PDF或访问网络。"""
    settings, document, job, session = context
    stub_local_conversion(monkeypatch, document, job)
    parsed = ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type="pdf",
        quality=ParseQuality(content=content), blocks=[ParsedBlock(block_id=uuid4(), page=1,
            text="PRIVATE_SOURCE", block_type="text", source="mineru_cloud",
            provenance=BlockProvenance(origin="content_list", pointer="/0", raw_sha256="c" * 64))])
    decision = RouteDecision(parse_mode="auto", file_type="pdf", route_strategy="mineru_whole",
        reason_codes=("PDF_SCAN_OR_NON_TEXT_PAGE",), signals=StructuralSignals(page_count=1))
    job.parse_options = {"_cache_guard": decision.model_dump(mode="json"), "_local_replay_only": True}
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", lambda *args, **kwargs: parsed)
    session.get = lambda *args: job
    return settings, document, job, session, parsed


@pytest.mark.parametrize("content,event", [("COMPLETE", "router.guard_passed"), ("PARTIAL", "router.guard_partial")])
def test_guard_log_reaches_cache_block_persistence_and_commit(context, monkeypatch, content, event):
    """两条真实Guard日志分支必须走到提交，捕获缺失logging导入的NameError回归。"""
    settings, document, job, session, parsed = post_adapter_context(context, monkeypatch, content)
    sequence = []
    monkeypatch.setattr(jobs, "log_event", lambda name, **fields: sequence.append(name))
    original_commit = session.commit

    def cache(doc, current, config, result, visual, archive, source):
        """覆盖V4质量和来源JSON序列化，缓存边界不访问真实业务文件。"""
        assert (doc, current, config) == (document, job, settings)
        restored = ParsedDocument.model_validate_json(result.model_dump_json())
        assert restored == parsed and restored.quality.content == content
        assert restored.blocks[0].provenance == parsed.blocks[0].provenance
        sequence.append("cache")

    def persist(current_session, doc, current, result):
        """确认全结果在缓存之后交给既有持久化边界，不模拟真实数据库成功。"""
        assert current_session is session and result is parsed
        sequence.append("blocks")
        doc.status, current.state, current.is_active = "CHUNKING", "done", False

    def commit():
        """记录提交先于持久化成功日志，不把缓存成功当作数据库成功。"""
        original_commit()
        sequence.append("commit")

    monkeypatch.setattr(jobs, "save_cloud_cache", cache)
    monkeypatch.setattr(jobs, "finish_job", persist)
    monkeypatch.setattr(session, "commit", commit)
    assert jobs.check_step(session, job.job_id, None, settings) == ("done", 1)
    expected = ["mineru.adapter_validated", event, "cache", "blocks", "commit", "mineru.blocks_persisted"]
    assert [name for name in sequence if name in expected] == expected
    assert document.status == "CHUNKING" and session.commits == 1


@pytest.mark.parametrize("boundary", ["cache", "persistence"])
def test_post_adapter_exception_keeps_safe_stack_and_fails_without_cloud(context, monkeypatch, boundary):
    """后置异常保留真实类型/位置并独立记错，任务和文档明确解析失败且使用相同安全原因。"""
    settings, document, job, session, _ = post_adapter_context(context, monkeypatch)
    events = []

    def failed(*args, **kwargs):
        """模拟缓存或ORM编程异常，敏感异常文本不得进入日志、错误响应或JSONB。"""
        raise TypeError("PRIVATE_BODY PRIVATE_TOKEN https://private.invalid/signed?key=PRIVATE_KEY")

    @contextmanager
    def isolated_session():
        """任务和独立错误事务只使用内存Stub，不打开数据库连接。"""
        yield session

    monkeypatch.setattr(jobs, "save_cloud_cache" if boundary == "cache" else "finish_job", failed)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "processing_session", isolated_session)
    monkeypatch.setattr(tasks, "restore_cached_result", lambda *args: None)
    monkeypatch.setattr(tasks, "MinerUCloudProvider", forbid_cloud)
    monkeypatch.setattr(tasks, "publish_step", forbid_cloud)
    monkeypatch.setattr(tasks, "log_event", lambda event, **fields: events.append((event, logs.safe_fields(fields))))
    with pytest.raises(DocumentProcessingError, match="LOCAL_PIPELINE_ERROR") as error:
        tasks.execute_step(SimpleNamespace(request=SimpleNamespace(retries=0)), str(job.job_id), "check")
    diagnostic = job.parse_options["_runtime_failure"]
    assert diagnostic["error_type"] == "TypeError" and diagnostic["frames"]
    assert any(frame["source_file"] == "app/services/mineru/jobs.py"
               and frame["source_function"] == "adapt_raw_result" for frame in diagnostic["frames"])
    assert any(name == "mineru.exception_frame" for name, _ in events)
    assert document.status == "FAILED" and job.error_code == "LOCAL_PIPELINE_ERROR"
    assert str(error.value) == document.error_message == job.error_message
    assert "文件解析失败：" in document.error_message
    assert session.commits == 1  # 只有独立失败事务提交，不是Block事务提交。
    assert "PRIVATE" not in str(error.value) + repr(diagnostic) + repr(events)
    assert "https://" not in repr(diagnostic) and "D:/" not in repr(diagnostic)


def test_internal_trace_fields_reject_private_paths_source_text_and_invalid_lines():
    """脱敏调用栈只接受项目相对源码位置，不接受绝对路径、URL、源码或任意负行号。"""
    safe = {"source_file": "app/services/mineru/jobs.py", "source_function": "adapt_raw_result", "source_line": 652}
    assert logs.safe_fields(safe) == safe
    assert logs.safe_fields({"source_file": "D:/PRIVATE/jobs.py", "source_function": "PRIVATE body()", "source_line": -1}) == {}
    assert logs.safe_fields({"source_file": "app/../PRIVATE.py", "source_function": "https://PRIVATE", "source_line": True}) == {}


@pytest.mark.parametrize("message,expected", [("LOCAL_PIPELINE_ERROR：固定安全摘要", "LOCAL_PIPELINE_ERROR"),
                                             ("PRIVATE_SECRET", None)])
def test_management_preserves_only_known_local_failure_cause(context, monkeypatch, message, expected):
    """上层管理失败保留固定本地原因，不再只有MANAGEMENT_STAGE_FAILED，也不转发任意消息。"""
    _, document, _, session = context
    document.management_task_id = uuid4()
    document.error_message = message
    monkeypatch.setattr(management, "config_for", lambda doc: {"state": "running", "operation": "retry"})
    monkeypatch.setattr(session, "scalar", lambda statement: document)
    management.mark_failed(session, document.doc_id, document.management_task_id, "MANAGEMENT_STAGE_FAILED")
    assert document.management_config["error_code"] == "MANAGEMENT_STAGE_FAILED"
    assert document.management_config["cause_code"] == expected


@pytest.mark.parametrize("rebind", [False, True])
def test_legacy_unknown_job_is_selected_only_with_explicit_local_replay_checkpoint(context, monkeypatch, rebind):
    """旧UNKNOWN必须进入带离线标记的SQL筛选，再校验原ZIP，不能误走原件重解析。"""
    from sqlalchemy.dialects.postgresql import dialect
    settings, document, job, session = context
    stub_local_conversion(monkeypatch, document, job)
    document.status, job.state, job.error_code, job.is_active = "FAILED", "failed", "UNKNOWN", False
    job.parse_options = {"_local_replay_only": True, "_cache_recipe": {"normalizer_version": NORMALIZER_VERSION}}
    statements = []

    def scalar(statement):
        """记录实际SQL筛选而非只让Stub返回结果，避免未来再次漏掉UNKNOWN离线谓词。"""
        statements.append(str(statement.compile(dialect=dialect(), compile_kwargs={"literal_binds": True})))
        return None if rebind else job

    def scalars(statement):
        """历史代次仅返回唯一同身份Job；真实Hash/归档核验仍执行被测函数。"""
        scalar(statement)
        return [job]

    monkeypatch.setattr(session, "scalar", scalar)
    monkeypatch.setattr(session, "scalars", scalars)
    assert management.retry_cloud_job(session, document, settings) is job
    assert job.state == "pending" and document.status == "PARSING"
    assert job.parse_options["_local_replay_only"] is True
    assert all("UNKNOWN" in sql and "_local_replay_only" in sql and "IS true" in sql for sql in statements)
    assert len(statements) == (2 if rebind else 1)


def test_legacy_unknown_without_local_checkpoint_cannot_enable_cloud_retry(context, monkeypatch):
    """没有离线标记的UNKNOWN不能恢复为活动云任务，不因修复历史异常放宽费用保护。"""
    settings, document, job, session = context
    stub_local_conversion(monkeypatch, document, job)
    document.status, job.state, job.error_code, job.is_active = "FAILED", "failed", "UNKNOWN", False
    job.parse_options = {"_cache_recipe": {"normalizer_version": NORMALIZER_VERSION}}
    with pytest.raises(management.ManagementError):
        management.retry_cloud_job(session, document, settings)
    assert not job.is_active and document.status == "FAILED"
