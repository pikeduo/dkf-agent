"""管理编排纯Mock：只构造ORM元数据和任务Stub，不读取文档、连接DB或运行模型。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models import Document
from app.services import document_management as tool
from app.services.embedding.service import EmbeddingError


class SessionStub:
    """只记录提交前检查点操作，不创建业务记录或启动基础服务。"""

    def __init__(self, document=None):
        """冻结当前文档及模拟查询结果。"""
        self.document, self.flushes = document, 0

    def scalar(self, statement):
        """故障记录只返回指定文档，不执行SQL。"""
        return self.document

    def flush(self):
        """记录本地检查点需要flush，不执行数据库提交。"""
        self.flushes += 1


@pytest.fixture
def context(monkeypatch, tmp_path):
    """提供未加载模型的独立配置和完整空检查点，并禁止实际云活动查询。"""
    document = Document(doc_id=uuid4(), kb_id=uuid4(), file_name="existing.txt", file_type="txt",
        file_hash="a" * 64, file_path="data/uploads/existing.txt", status="READY", error_message=None,
        processing_task_id=uuid4(), chunk_task_id=uuid4(), chunk_config={"old": True},
        embedding_task_id=uuid4(), embedding_config={"old": True}, index_task_id=uuid4(), index_config={"old": True},
        management_task_id=None, management_config=None)
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek",
        upload_dir=tmp_path / "uploads", parsed_dir=tmp_path / "parsed", embeddings_dir=tmp_path / "embeddings")
    monkeypatch.setattr(tool, "active_job", lambda session, document: None)
    return document, SessionStub(document), settings


def test_reindex_changes_only_management_and_processing_generation_and_starts_cache_only(context):
    """显式Reindex换代次、失效下游检查点，不改原件或内容，不直接调用Parser。"""
    document, session, settings = context
    source = document.file_path, document.file_hash, document.doc_id
    previous = document.processing_task_id
    manager, dispatch = tool.prepare_management(session, document, settings, "reindex")
    assert dispatch and manager == document.management_task_id and document.processing_task_id != previous
    assert (document.file_path, document.file_hash, document.doc_id) == source
    assert document.chunk_task_id is document.embedding_task_id is document.index_task_id is None
    assert document.status == "UPLOADED"
    action = tool.next_action(session, document, settings)
    assert action["operation"] == "reindex" and action["stage"] == "parse"
    assert action["task_id"] == str(document.processing_task_id)
    assert tool.next_action(session, document, settings) == {"kind": "wait"}
    assert tool.public_management(document).stage == "parse"


def test_reindex_pipeline_prepares_each_existing_stage_once_and_stops_at_ready(context, monkeypatch):
    """缓存回放后按阶段依次投递，一个阶段一次；重复编排消息不重建Chunk/GPU/索引。"""
    document, session, settings = context
    tool.prepare_management(session, document, settings, "reindex")
    tool.next_action(session, document, settings)
    calls = []

    def chunk(session, doc, config):
        """模拟原切片准备接口，仅冻结标识不生成正文或Chunk。"""
        calls.append("chunk")
        doc.chunk_task_id = uuid4()
        return doc.chunk_task_id, True

    def embed(session, doc, config):
        """模拟文档级GPU准备，不加载模型或执行推理。"""
        calls.append("embed")
        doc.embedding_task_id = uuid4()
        return doc.embedding_task_id, True

    def index(session, doc):
        """模拟CPU入库准备，不直接插入/修改任何向量。"""
        calls.append("index")
        doc.index_task_id = uuid4()
        return doc.index_task_id, True

    monkeypatch.setattr(tool, "prepare_chunk", chunk)
    monkeypatch.setattr(tool, "prepare_embedding", embed)
    monkeypatch.setattr(tool, "prepare_index", index)
    for state, stage in (("CHUNKING", "chunk"), ("EMBEDDING", "embed"), ("INDEXING", "index")):
        document.status = state
        assert tool.next_action(session, document, settings)["stage"] == stage
        assert tool.next_action(session, document, settings) == {"kind": "wait"}
    document.status = "READY"
    assert tool.next_action(session, document, settings) == {"kind": "done"}
    assert tool.public_management(document).state == "done" and calls == ["chunk", "embed", "index"]


@pytest.mark.parametrize("status,field", [("PARSING", None), ("OCR_PROCESSING", None),
    ("CHUNKING", "chunk_task_id"), ("EMBEDDING", "embedding_task_id"), ("INDEXING", "index_task_id")])
def test_new_maintenance_rejects_active_business_stage(context, status, field):
    """活动原生/云/GPU/入库未结束不能被管理员重建或删除覆盖。"""
    document, session, settings = context
    document.status = status
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_BUSY"):
        tool.prepare_management(session, document, settings, "reindex")
    assert document.management_task_id is None


def test_active_cloud_job_blocks_delete_and_reindex(context, monkeypatch):
    """已提交云批次不能通过删除/重建静默追加新任务或取消远端费用。"""
    document, session, settings = context
    monkeypatch.setattr(tool, "active_job", lambda session, document: SimpleNamespace(job_id=uuid4()))
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_BUSY"):
        tool.prepare_management(session, document, settings, "delete")


def test_repeated_operation_is_idempotent_and_retry_only_republishes_manager(context):
    """重复Reindex沿用检查点，显式Retry恢复编排消息不改变业务任务ID。"""
    document, session, settings = context
    first, _ = tool.prepare_management(session, document, settings, "reindex")
    tool.next_action(session, document, settings)
    parse_id = document.processing_task_id
    assert tool.prepare_management(session, document, settings, "reindex") == (first, False)
    assert tool.prepare_management(session, document, settings, "retry") == (first, True)
    assert document.processing_task_id == parse_id
    assert tool.next_action(session, document, settings) == {"kind": "wait"}


def test_failed_force_reparse_retry_cannot_inherit_force(context):
    """失败Force的普通Retry使用ingest及原解析代次，绝不绕过收费历史限制。"""
    document, session, settings = context
    tool.prepare_management(session, document, settings, "reparse", parse_mode="mineru")
    tool.next_action(session, document, settings)
    task_id, manager = document.processing_task_id, document.management_task_id
    document.status = "FAILED"
    tool.mark_failed(session, document.doc_id, manager, "MANAGEMENT_STAGE_FAILED")
    tool.prepare_management(session, document, settings, "retry")
    action = tool.next_action(session, document, settings)
    assert document.processing_task_id == task_id
    assert action["operation"] == "ingest" and action["parse_mode"] == "mineru"


@pytest.mark.parametrize("stage,field", [("index", "index_task_id"), ("embed", "embedding_task_id"), ("chunk", "chunk_task_id")])
def test_retry_selects_latest_failed_checkpoint_not_parse(context, stage, field):
    """失败恢复优先已有最下游检查点，不重投Parse或重算已完成GPU阶段。"""
    document, session, settings = context
    document.status = "FAILED"
    if stage != "index":
        document.index_task_id, document.index_config = None, None
    if stage == "chunk":
        document.embedding_task_id, document.embedding_config = None, None
    existing = getattr(document, field)
    tool.prepare_management(session, document, settings, "retry")
    assert tool.config_for(document)["stage"] == stage
    assert getattr(document, field) == existing


def test_retry_resumes_exact_active_cloud_batch(context, monkeypatch):
    """沿用活动job/batch并只恢复check，不再次申请新批次。"""
    document, session, settings = context
    document.status = "OCR_PROCESSING"
    job = SimpleNamespace(job_id=uuid4(), task_id=document.processing_task_id, file_hash=document.file_hash,
                          state="running", upload_complete=True, batch_id="existing-batch",
                          created_at=datetime.now(UTC), parse_options={})
    monkeypatch.setattr(tool, "active_job", lambda session, document: job)
    tool.prepare_management(session, document, settings, "retry")
    action = tool.next_action(session, document, settings)
    assert action["stage"] == "cloud" and action["task_id"] == str(job.job_id) and action["cloud_step"] == "check"
    assert tool.next_action(session, document, settings) == {"kind": "wait"}


def test_cloud_completed_before_retry_dispatch_advances_to_chunk_instead_of_resubmit(context, monkeypatch):
    """准备Retry后云刚好完成时，跳过旧cloud意图并接续切片，不误报检查点错误。"""
    document, session, settings = context
    document.status = "OCR_PROCESSING"
    job = SimpleNamespace(job_id=uuid4(), task_id=document.processing_task_id, file_hash=document.file_hash,
                          state="running", upload_complete=True, batch_id="existing-batch",
                          created_at=datetime.now(UTC), parse_options={})
    monkeypatch.setattr(tool, "active_job", lambda session, document: job)
    tool.prepare_management(session, document, settings, "retry")
    document.status, document.chunk_task_id = "CHUNKING", None
    monkeypatch.setattr(tool, "active_job", lambda session, document: None)

    def chunk(session, doc, settings):
        """只模拟冻结切片标识，不处理文档或云任务。"""
        doc.chunk_task_id = uuid4()
        return doc.chunk_task_id, True

    monkeypatch.setattr(tool, "prepare_chunk", chunk)
    assert tool.next_action(session, document, settings)["stage"] == "chunk"


def test_timeout_and_failure_are_not_success_and_do_not_cancel_background_work(context):
    """截止时间与阶段FAILED明确报错，不能冒充完成或回退已提交任务。"""
    document, session, settings = context
    tool.prepare_management(session, document, settings, "reindex")
    document.management_config = document.management_config | {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_TIMEOUT"):
        tool.next_action(session, document, settings)
    assert document.status == "UPLOADED"
    assert tool.public_management(document).state == "failed"
    assert tool.public_management(document).error_code == "MANAGEMENT_TIMEOUT"


def test_ready_after_lost_final_manager_message_retry_only_marks_done(context):
    """入库已经READY而编排终态丢失时，仅补标记，不提交第二个GPU或索引任务。"""
    document, session, settings = context
    identifier, _ = tool.prepare_management(session, document, settings, "reindex")
    document.status = "READY"
    document.management_config = document.management_config | {"state": "failed", "error_code": "MANAGEMENT_TIMEOUT"}
    assert tool.prepare_management(session, document, settings, "retry") == (identifier, False)
    assert tool.public_management(document).stage == "done" and tool.public_management(document).state == "done"


def test_stale_or_corrupt_checkpoint_and_safe_root_cause(context):
    """不接受错误解析代次；模型准备失败保留固定根因，不返回路径或任意异常原文。"""
    document, session, settings = context
    manager, _ = tool.prepare_management(session, document, settings, "reindex")
    cause = tool.exception_cause(EmbeddingError("EMBEDDING_GPU_REQUIRED"))
    tool.mark_failed(session, document.doc_id, manager, "MANAGEMENT_CHECKPOINT", cause)
    assert tool.public_management(document).cause_code == "EMBEDDING_GPU_REQUIRED"
    assert tool.safe_cause_code("PRIVATE_PATH PRIVATE_KEY") is None
    previous = dict(document.management_config)
    tool.mark_failed(session, document.doc_id, uuid4(), "MANAGEMENT_DISPATCH")
    assert document.management_config == previous
    document.processing_task_id = uuid4()
    with pytest.raises(tool.ManagementError, match="MANAGEMENT_CHECKPOINT"):
        tool.config_for(document)
