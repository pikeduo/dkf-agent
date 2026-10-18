"""编排任务的内存数据库/消息Stub；不执行真实解析、GPU或供应商步骤。"""

from contextlib import contextmanager
from uuid import uuid4

from celery.exceptions import Retry
import pytest
from types import SimpleNamespace
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.models import Document
from app.services import document_management as service
from app.services.embedding.service import EmbeddingError
from app.services.chunk.splitter import ChunkError
from app.services.indexing import IndexingError
from app.tasks import management as worker


class SessionStub:
    """只记录事务退出和删除顺序，不能代替真实数据库事务验收。"""

    def __init__(self, document):
        """保存当前行和事件，便于检查提交后投递/日志。"""
        self.document, self.events = document, []

    def begin(self):
        """模拟短事务生命周期，不连接数据库。"""
        @contextmanager
        def transaction():
            """事务成功退出后才记录提交事件。"""
            yield
            self.events.append("commit")
        return transaction()

    def scalar(self, statement):
        """只返回当前文档，不执行SQL或轮询基础服务。"""
        return self.document

    def flush(self):
        """记录检查点需要刷新，不持久化业务行。"""
        return None

    def delete(self, document):
        """记录级联删除意图；真实级联由单独DB用例验证。"""
        self.events.append("delete")


@pytest.fixture
def context(monkeypatch):
    """只建立编排元数据，替代Session、Settings和Celery等待，不启动任何业务资源。"""
    document = Document(doc_id=uuid4(), kb_id=uuid4(), status="READY", processing_task_id=uuid4(),
        chunk_task_id=None, chunk_config=None, embedding_task_id=None, embedding_config=None,
        index_task_id=None, index_config=None, management_task_id=None, management_config=None)
    session = SessionStub(document)
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek")
    monkeypatch.setattr(service, "active_job", lambda session, document: None)
    service.prepare_management(session, document, settings, "reindex")

    @contextmanager
    def processing_session():
        """仅返回内存会话，不创建测试文件或读取用户环境。"""
        yield session

    monkeypatch.setattr(worker, "processing_session", processing_session)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    # 无sig的Retry只在本轮用户Mock中返回RETRY，不触发eager递归/真实消息投递。
    monkeypatch.setattr(worker.manage_document, "retry", lambda **kwargs: Retry())
    return document, session


def test_dispatch_after_commit_and_duplicate_message_does_not_repeat_child(context, monkeypatch):
    """下一阶段只在管理意图提交后投递；重复管理任务仅等待，不重复子任务。"""
    document, session = context
    calls = []

    def publish(session, doc_id, action):
        """记录既有投递边界，不调用实际任务主体。"""
        assert "commit" in session.events
        calls.append(action)
        return True

    monkeypatch.setattr(worker, "dispatch_action", publish)
    first = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    second = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert first.state == second.state == "RETRY" and len(calls) == 1
    assert calls[0]["operation"] == "reindex"


def test_delete_is_committed_only_after_file_cleanup_and_old_message_skips(context, monkeypatch):
    """清理返回成功才提交数据库删除；旧管理UUID不能执行任何文件操作。"""
    document, session = context
    document.management_config = document.management_config | {"operation": "delete", "stage": "delete", "cleanup_roots": {}}

    def cleanup(session, doc, roots):
        """模拟全部文件清理已完成，不删除任何测试/用户文件。"""
        session.events.append("cleanup")
        return {"removed_path_count": 7, "retained_shared_cache_count": 1, "shared_visual_cache_retained": True}

    monkeypatch.setattr(worker, "cleanup_owned_files", cleanup)
    stale = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(uuid4()))
    assert stale.result["status"] == "stale_task" and "cleanup" not in session.events
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert result.state == "SUCCESS" and result.result["status"] == "deleted"
    assert session.events.index("cleanup") < session.events.index("delete") < len(session.events) - 1


def test_delete_storage_failure_keeps_row_and_safe_failed_checkpoint(context, monkeypatch):
    """文件清理失败不调用SQL删除，检查点可恢复，不泄漏I/O原文或路径。"""
    document, session = context
    document.management_config = document.management_config | {"operation": "delete", "stage": "delete", "cleanup_roots": {}}

    def failure(session, document, roots):
        """模拟固定存储失败，不改动文件。"""
        raise service.ManagementError("DELETE_STORAGE")

    monkeypatch.setattr(worker, "cleanup_owned_files", failure)
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert result.state == "FAILURE" and "delete" not in session.events
    assert document.management_config["state"] == "failed"
    assert document.management_config["error_code"] == "DELETE_STORAGE"


def test_failed_stage_and_timeout_stop_without_dispatch_or_success(context, monkeypatch):
    """业务失败或截止时间不当成READY，不自动重发费用请求。"""
    document, session = context
    document.status = "FAILED"
    calls = []
    monkeypatch.setattr(worker, "dispatch_action", lambda *args: calls.append(args))
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert result.state == "FAILURE" and not calls
    assert document.management_config["error_code"] == "MANAGEMENT_STAGE_FAILED"


def test_busy_gpu_row_retries_short_check_without_marking_business_failed(context, monkeypatch):
    """后台GPU持锁只返回RETRY，不让管理任务阻塞等待，也不错误标记业务失败。"""
    document, session = context

    def occupied(statement):
        """模拟NOWAIT锁冲突，异常文本不发布为业务错误。"""
        if statement._for_update_arg is not None:
            raise OperationalError("PRIVATE_SQL", {}, SimpleNamespace(sqlstate="55P03"))
        return document

    monkeypatch.setattr(session, "scalar", occupied)
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert result.state == "RETRY" and document.management_config["state"] == "running"


@pytest.mark.parametrize("stage,error", [
    ("embed", EmbeddingError("EMBEDDING_LIMIT")),
    ("chunk", ChunkError("CHUNK_LIMIT：固定资源限制")),
    ("index", IndexingError("INDEX_SCHEMA")),
    ("index", EmbeddingError("EMBEDDING_SOURCE")),
])
def test_prepare_failure_keeps_actual_stage_and_cause_without_false_generation_conflict(context, monkeypatch, stage, error):
    """准备子任务时的已知失败如实记录阶段/原因；没有成功投递就不伪造任务编号或READY。"""
    document, session = context
    frozen = document.processing_task_id
    document.status = {"chunk": "CHUNKING", "embed": "EMBEDDING", "index": "INDEXING"}[stage]

    def rejected(*args):
        """仅抛固定服务异常，不执行切片/向量/数据库或消息投递。"""
        raise error

    dispatch, log = [], []
    monkeypatch.setattr(service, {"chunk": "prepare_chunk", "embed": "prepare_embedding", "index": "prepare_index"}[stage], rejected)
    monkeypatch.setattr(worker, "dispatch_action", lambda *args: dispatch.append(args))
    monkeypatch.setattr(worker, "log_event", lambda *args, **kwargs: log.append((args, kwargs)))
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    cause = service.exception_cause(error)
    assert result.state == "FAILURE" and f"原因={cause}" in str(result.result)
    config = document.management_config
    assert config["state"] == "failed" and config["stage"] == stage
    assert config["error_code"] == "MANAGEMENT_STAGE_FAILED" and config["cause_code"] == cause
    assert config["stage_task_id"] is None and config["dispatched"] is False
    assert document.processing_task_id == frozen and not dispatch
    assert log[-1][1]["error_type"] == type(error).__name__ and log[-1][1]["step"] == cause


def test_real_generation_conflict_remains_checkpoint_failure_and_never_dispatches(context, monkeypatch):
    """真实解析代次不符仍拒绝，不因修正资源错误分类而绕过检查点身份。"""
    document, session = context
    document.processing_task_id = uuid4()
    dispatch = []
    monkeypatch.setattr(worker, "dispatch_action", lambda *args: dispatch.append(args))
    result = worker.manage_document.apply(args=[str(document.doc_id)], task_id=str(document.management_task_id))
    assert result.state == "FAILURE" and "MANAGEMENT_CHECKPOINT" in str(result.result)
    assert not dispatch and document.status == "UPLOADED"
