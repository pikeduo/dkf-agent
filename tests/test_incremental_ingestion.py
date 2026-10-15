"""阶段26内存元数据/消息Stub；用户执行，不连接数据库、Broker、GPU或真实MinerU。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import admin_knowledge_bases as api
from app.api import mineru as cloud_api
from app.core.config import Settings
from app.models import Document
from app.schemas.knowledge_base import DocumentResponse
from app.services import document_management as tool
from app.services import routed_processing


class SessionStub:
    """记录短事务与检查点提交顺序，不创建业务数据库记录。"""

    def __init__(self, document):
        """保存当前文档和发送前可观察的事务事件。"""
        self.document, self.events, self.in_transaction = document, [], False

    def scalar(self, statement):
        """模拟文档行锁查询，只返回本用例的行。"""
        self.in_transaction = True
        return self.document

    def flush(self):
        """标记检查点待提交，不产生真实SQL。"""
        self.in_transaction = True

    def commit(self):
        """捕获发送前检查点快照，验证先提交后投递。"""
        self.events.append(("commit", tool.config_for(self.document)))
        self.in_transaction = False

    def rollback(self):
        """清除模拟读事务，不回滚已确认的存储或其他文档。"""
        self.in_transaction = False

    def begin(self):
        """模拟独立失败记录事务，不连接数据库。"""
        @contextmanager
        def transaction():
            """只有成功退出时才记录模拟提交。"""
            yield
            self.commit()
        return transaction()


@pytest.fixture
def context(monkeypatch):
    """提供新上传行及配置元数据，不生成文档/模型/缓存文件。"""
    now = datetime.now(UTC)
    document = Document(doc_id=uuid4(), kb_id=uuid4(), file_name="metadata.txt", file_type="txt",
        file_hash="a" * 64, file_path="PRIVATE_SOURCE_PATH", source_type="uploaded", status="UPLOADED",
        error_message=None, processing_task_id=uuid4(), chunk_task_id=None, chunk_config=None,
        embedding_task_id=None, embedding_config=None, index_task_id=None, index_config=None,
        management_task_id=None, management_config=None, created_at=now, updated_at=now)
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(tool, "active_job", lambda *args: None)
    return document, SessionStub(document), settings


def advance_stub(monkeypatch):
    """替代下游prepare接口，只记录文档级任务，不切片、推理或写入向量。"""
    calls = []

    def chunk(session, doc, settings):
        """冻结一个Chunk任务，保留原解析代次。"""
        calls.append(("chunk", doc.doc_id))
        doc.chunk_task_id = uuid4()
        return doc.chunk_task_id, True

    def embed(session, doc, settings):
        """冻结一个文档GPU任务，不为各Chunk创建独立任务。"""
        calls.append(("embed", doc.doc_id))
        doc.embedding_task_id = uuid4()
        return doc.embedding_task_id, True

    def index(session, doc):
        """冻结已有向量的文档入库任务，不改变KB或其他文档。"""
        calls.append(("index", doc.doc_id))
        doc.index_task_id = uuid4()
        return doc.index_task_id, True

    monkeypatch.setattr(tool, "prepare_chunk", chunk)
    monkeypatch.setattr(tool, "prepare_embedding", embed)
    monkeypatch.setattr(tool, "prepare_index", index)
    return calls


@pytest.mark.parametrize("mode", ["auto", "native", "mineru"])
def test_upload_freezes_generation_before_original_parse_and_manager_publish(context, monkeypatch, mode):
    """三种上传模式先提交检查点，再分别投递解析与编排；保持原task_id及单文件响应。"""
    document, session, _ = context
    response = DocumentResponse.model_validate(document)
    calls = []

    def parse(session, doc_id, task_id, **kwargs):
        """观察发送时的已提交意图，不运行原任务。"""
        assert not session.in_transaction and session.events[-1][0] == "commit"
        assert tool.config_for(document)["dispatched"] is True
        assert kwargs == {"parse_mode": mode}
        calls.append(("parse", doc_id, task_id))
        return True

    def manager(doc_id, task_id):
        """观察独立管理ID，不把它冒充原解析任务ID。"""
        assert not session.in_transaction
        calls.append(("manager", doc_id, task_id))
        return True

    monkeypatch.setattr(api, "publish_document", parse)
    monkeypatch.setattr(tool, "publish_management", manager)
    assert api.dispatch_uploaded_document(session, response, mode) == response
    assert calls == [("parse", document.doc_id, response.task_id),
                     ("manager", document.doc_id, document.management_task_id)]
    assert document.processing_task_id == response.task_id
    assert tool.public_management(document).operation == "ingest"
    assert tool.next_action(session, document, api.get_settings()) == {"kind": "wait"}


def test_native_pipeline_advances_only_new_document_once_and_never_marks_ready_itself(context, monkeypatch):
    """旧READY文档及标识不变；新文档各阶段只准备一次，READY仍由已有入库事务负责。"""
    document, session, settings = context
    older = Document(doc_id=uuid4(), kb_id=document.kb_id, status="READY",
        processing_task_id=uuid4(), chunk_task_id=uuid4(), embedding_task_id=uuid4(), index_task_id=uuid4())
    old_snapshot = (older.status, older.processing_task_id, older.chunk_task_id, older.embedding_task_id, older.index_task_id)
    source = (document.doc_id, document.processing_task_id, document.file_hash, document.file_path)
    manager = tool.prepare_ingestion(session, document, settings)
    assert tool.prepare_ingestion(session, document, settings) == manager
    calls = advance_stub(monkeypatch)
    for status, stage in (("CHUNKING", "chunk"), ("EMBEDDING", "embed"), ("INDEXING", "index")):
        document.status = status
        assert tool.next_action(session, document, settings)["stage"] == stage
        assert tool.next_action(session, document, settings) == {"kind": "wait"}
        assert document.status == status
    assert calls == [(stage, document.doc_id) for stage in ("chunk", "embed", "index")]
    assert (document.doc_id, document.processing_task_id, document.file_hash, document.file_path) == source
    assert (older.status, older.processing_task_id, older.chunk_task_id, older.embedding_task_id, older.index_task_id) == old_snapshot
    document.status = "READY"
    assert tool.next_action(session, document, settings) == {"kind": "done"}
    assert tool.next_action(session, document, settings) == {"kind": "stopped"}


def test_cloud_handoff_rebinds_generation_and_waits_for_atomic_blocks(context, monkeypatch):
    """扫描PDF换绑云代次后等待同一Job，不再提交解析；结果完成才推进Chunk。"""
    document, session, settings = context
    manager = tool.prepare_ingestion(session, document, settings)
    original_parse = document.processing_task_id
    job_id, cloud_parse = uuid4(), uuid4()
    tool.rebind_cloud_management(document, cloud_parse, job_id)
    # 生产create_job在同一个事务内执行这次赋值；此处只模拟元数据，不调用MinerU。
    document.processing_task_id, document.status = cloud_parse, "OCR_PROCESSING"
    config = tool.config_for(document)
    assert document.management_task_id == manager and original_parse != cloud_parse
    assert config["stage_task_id"] == str(job_id) and config["source_parse_task_id"] == str(cloud_parse)
    assert tool.next_action(session, document, settings) == {"kind": "wait"}
    calls = advance_stub(monkeypatch)
    document.status = "CHUNKING"
    assert tool.next_action(session, document, settings)["stage"] == "chunk"
    assert calls == [("chunk", document.doc_id)]


def test_cloud_generation_change_is_rejected_when_checkpoint_is_stale(context):
    """代次冲突不能修补或越过幂等边界；无管理检查点的旧文档保持原云语义。"""
    document, session, settings = context
    tool.rebind_cloud_management(document, uuid4(), uuid4())
    assert document.management_config is None
    tool.prepare_ingestion(session, document, settings)
    document.processing_task_id = uuid4()
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_CHECKPOINT"):
        tool.rebind_cloud_management(document, uuid4(), uuid4())


def test_manager_dispatch_failure_keeps_original_and_requires_explicit_retry(context, monkeypatch):
    """编排消息确认失败仅标记管理失败，保留已投递的解析/原件，不自动补投或伪装READY。"""
    document, session, settings = context
    calls = []
    monkeypatch.setattr(api, "publish_document", lambda *args, **kwargs: calls.append("parse") or True)
    monkeypatch.setattr(tool, "publish_management", lambda *args: False)
    with pytest.raises(HTTPException) as error:
        api.dispatch_uploaded_document(session, DocumentResponse.model_validate(document))
    assert error.value.status_code == 503 and "PRIVATE_SOURCE_PATH" not in str(error.value.detail)
    assert tool.public_management(document).error_code == "MANAGEMENT_DISPATCH"
    assert document.status == "UPLOADED" and calls == ["parse"]
    with pytest.raises(tool.ManagementError):
        tool.prepare_ingestion(session, document, settings)
    source = document.processing_task_id
    document.status = "CHUNKING"
    tool.prepare_management(session, document, settings, "retry")
    assert document.processing_task_id == source and tool.config_for(document)["stage"] == "chunk"


def test_batch_each_file_gets_independent_manager_and_failure_does_not_revoke_other(context, monkeypatch):
    """批量逐项复用真实调度边界；首项管理投递失败仍保留身份，后项独立继续。"""
    from app.services import batch_upload
    first, session, settings = context
    second = Document(doc_id=uuid4(), kb_id=first.kb_id, file_name="second.txt", file_type="txt",
        file_hash="b" * 64, file_path="PRIVATE_SECOND_PATH", source_type="uploaded", status="UPLOADED",
        error_message=None, processing_task_id=uuid4(), chunk_task_id=None, embedding_task_id=None,
        index_task_id=None, management_task_id=None, management_config=None,
        created_at=first.created_at, updated_at=first.updated_at)
    rows = {first.file_name: first, second.file_name: second}
    parse_calls, manager_calls = [], []

    def store(session, kb_id, upload, configuration):
        """模拟原件已保存及提交，不创建任何测试文档文件。"""
        assert kb_id == first.kb_id
        session.document = rows[upload.filename]
        session.commit()
        return DocumentResponse.model_validate(session.document)

    def dispatch(saved):
        """两项都走真实增量API调度，不合并doc_id或管理任务。"""
        return api.dispatch_uploaded_document(session, saved)

    def manager(doc_id, task_id):
        """只模拟首项管理消息确认失败，其余接受；不访问Redis。"""
        manager_calls.append((doc_id, task_id))
        return doc_id != first.doc_id

    monkeypatch.setattr(batch_upload, "store_document", store)
    monkeypatch.setattr(api, "publish_document", lambda session, doc_id, *args, **kwargs: parse_calls.append(doc_id) or True)
    monkeypatch.setattr(tool, "publish_management", manager)
    response = batch_upload.store_batch(session, first.kb_id,
        [SimpleNamespace(filename=row.file_name) for row in (first, second)], settings, dispatch)
    assert [item.status_code for item in response.items] == [503, 201]
    assert [item.doc_id for item in response.items] == [first.doc_id, second.doc_id]
    assert response.accepted_count == response.failed_count == 1
    assert parse_calls == [first.doc_id, second.doc_id]
    assert first.management_task_id != second.management_task_id and len(manager_calls) == 2
    assert tool.public_management(first).state == "failed" and tool.public_management(second).state == "running"


def test_router_cloud_dispatch_failure_retry_keeps_same_paid_batch(context, monkeypatch):
    """云短步骤投递失败绑定同一活动Job；显式Retry只恢复check，不申请新付费批次。"""
    document, session, settings = context
    tool.prepare_ingestion(session, document, settings)
    job = SimpleNamespace(job_id=uuid4(), doc_id=document.doc_id, task_id=uuid4(), file_hash=document.file_hash,
        state="running", is_active=True, upload_complete=True, batch_id="existing-batch")
    tool.rebind_cloud_management(document, job.task_id, job.job_id)
    document.processing_task_id, document.status = job.task_id, "OCR_PROCESSING"
    from app.services.mineru import jobs
    monkeypatch.setattr(jobs, "lock_job", lambda *args: (document, job))
    routed_processing.record_dispatch_error(session, job.job_id)
    assert tool.public_management(document).state == "failed"
    monkeypatch.setattr(tool, "active_job", lambda *args: job)
    tool.prepare_management(session, document, settings, "retry")
    action = tool.next_action(session, document, settings)
    assert action["task_id"] == str(job.job_id) and action["cloud_step"] == "check"
    assert job.batch_id == "existing-batch" and document.processing_task_id == job.task_id


def test_failed_parse_never_advances_or_reactivates_automatically(context, monkeypatch):
    """解析失败不能拿旧Block推进新阶段；自动入口不复活失败文档或更换收费身份。"""
    document, session, settings = context
    tool.prepare_ingestion(session, document, settings)
    calls = advance_stub(monkeypatch)
    document.status = "FAILED"
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_STAGE_FAILED"):
        tool.next_action(session, document, settings)
    tool.mark_failed(session, document.doc_id, document.management_task_id, "MANAGEMENT_STAGE_FAILED")
    with pytest.raises(tool.ManagementError):
        tool.prepare_ingestion(session, document, settings)
    assert calls == [] and document.status == "FAILED"


@pytest.mark.parametrize("cache_hit", [False, True])
def test_explicit_cloud_upload_continues_same_job_or_cache_without_second_submit(context, monkeypatch, cache_hit):
    """显式云上传保持202契约，首阶段只有一次投递；缓存命中从Chunk接续，零云提交。"""
    document, session, settings = context
    now = datetime.now(UTC)
    job = SimpleNamespace(job_id=uuid4(), doc_id=document.doc_id, task_id=uuid4(), file_hash=document.file_hash,
        state="done" if cache_hit else "waiting-file", is_active=not cache_hit, upload_complete=False,
        provider="mineru_cloud", model_version="vlm", data_id="metadata-only", batch_id=None, trace_id=None,
        retry_count=0, error_code=None, error_message=None, submitted_at=None, finished_at=now if cache_hit else None,
        created_at=now, updated_at=now)
    calls = []

    def create(*args):
        """模拟原云检查点创建/缓存回放，仅修改当前解析代次和状态。"""
        document.processing_task_id = job.task_id
        document.status = "CHUNKING" if cache_hit else "PARSING"
        return job

    def publish(job_id, step):
        """发送前检查云Job和管理意图已提交，不联网。"""
        assert not session.in_transaction and session.events[-1][1]["operation"] == "ingest"
        calls.append((job_id, step))

    monkeypatch.setattr(cloud_api, "create_job", create)
    monkeypatch.setattr(cloud_api, "MinerUCloudProvider", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cloud_api, "publish_step", publish)
    monkeypatch.setattr(tool, "publish_management", lambda *args: True)
    response = cloud_api.start_job(session, document, settings, continue_to_ready=True)
    assert response.job_id == job.job_id and response.task_id == job.task_id
    assert calls == ([] if cache_hit else [(job.job_id, "submit")])
    assert tool.config_for(document)["stage"] == ("chunk" if cache_hit else "cloud")
