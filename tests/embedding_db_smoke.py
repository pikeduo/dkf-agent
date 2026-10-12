"""用户显式执行的隔离 GPU 任务验收，模型/Broker 为 Stub，不加载 GPU 或调用任何云接口。"""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from celery.exceptions import Retry
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import admin_knowledge_bases as admin_api
from app.core.celery_app import celery_app
from app.core.config import Settings
from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.chunk.splitter import ChunkOptions
from app.services.embedding import processing
from app.services.embedding.artifacts import ArtifactStore, digest_json
from app.services.embedding.service import DIMENSION, MODEL_ID, VERSION, EmbeddingError
from app.tasks import embeddings as tasks
from app.tasks.documents import DocumentProcessingError

pytest_plugins = ("knowledge_admin_smoke",)
PREFIX = "/api/admin/knowledge-bases"


class DenseStub:
    """只验证批处理和持久化顺序，输出向量不代表真实模型语义质量。"""

    def __init__(self):
        """收集批次与可控制失败位置，不实例化任何推理依赖。"""

        self.calls = []
        self.fail_batch = None

    def identity(self):
        """返回固定模型元数据，生产中必须来自真实本地模型文件指纹。"""

        return {"model_name": MODEL_ID, "dimension": DIMENSION, "normalized": True,
                "version": VERSION, "model_fingerprint": "f" * 64, "max_length": 8192, "precision": "fp16"}

    def embed_documents(self, texts):
        """收集整批文本，指定批次可安全失败，不提交每个 Chunk 的子任务。"""

        self.calls.append(list(texts))
        if self.fail_batch == len(self.calls):
            raise EmbeddingError("EMBEDDING_TOO_LONG")
        return [[1.0] + [0.0] * (DIMENSION - 1) for text in texts]


@pytest.fixture
def context(api, migrated_connection, monkeypatch, tmp_path):
    """在测试事务写入五个已切片的 Chunk Stub，提供隔离会话与消息收集，不创建原始文档。"""

    connection, _, _ = migrated_connection
    settings = Settings(_env_file=None, embedding_model_dir=tmp_path / "unused-model",
                        embeddings_dir=tmp_path / "results", embedding_device="cuda:0", embedding_batch_size=2)
    with Session(connection, join_transaction_mode="create_savepoint") as session, session.begin():
        kb = KnowledgeBase(name=f"向量化验收-{uuid4()}")
        session.add(kb)
        session.flush()
        parse_task, chunk_task = uuid4(), uuid4()
        document = Document(kb_id=kb.kb_id, file_name="已有内容.txt", file_type="txt",
            file_hash="a" * 64, file_path="unused", source_type="uploaded", status="EMBEDDING",
            processing_task_id=parse_task, chunk_task_id=chunk_task,
            chunk_config={"options": ChunkOptions().snapshot(), "source_parse_task_id": str(parse_task)})
        session.add(document)
        session.flush()
        for index in range(1, 6):
            session.add(DocumentChunk(doc_id=document.doc_id, kb_id=kb.kb_id, text=f"已有切片 {index}",
                page_start=1, page_end=1, chunk_metadata={"chunk_index": index,
                    "source_parse_task_id": str(parse_task)}))
        ids = kb.kb_id, document.doc_id, parse_task, chunk_task
    messages, model_loads, stub = [], [], DenseStub()

    @contextmanager
    def isolated_session():
        """使用保存点模拟独立提交，不修改 public 项目数据。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    def current_settings():
        """只返回当前用例配置，不读取实际 Token、模型目录或数据库连接串。"""

        return settings

    def accepted(*args, **kwargs):
        """确认发送前检查点已提交、仅发送 doc_id 且使用 gpu_queue。"""

        assert kwargs["queue"] == "gpu_queue" and len(kwargs["args"]) == 1
        with isolated_session() as session:
            row = session.get(Document, UUID(kwargs["args"][0]))
            assert str(row.embedding_task_id) == kwargs["task_id"]
        messages.append(kwargs)

    def service(options):
        """只有 Worker 执行时返回内存推理 Stub，不触发本机模型加载。"""

        assert options.device == "cuda:0" and options.batch_size == 2
        model_loads.append(options)
        return stub

    monkeypatch.setattr(admin_api, "get_settings", current_settings)
    monkeypatch.setattr(tasks, "processing_session", isolated_session)
    monkeypatch.setattr(tasks.embed_document, "apply_async", accepted)
    monkeypatch.setattr(processing, "get_embedding_service", service)
    return SimpleNamespace(api=api, ids=ids, session=isolated_session, settings=settings,
                           messages=messages, stub=stub, model_loads=model_loads)


def endpoint(context, suffix="embed"):
    """拼接限定知识库与文档的接口路径，避免跨库误请求。"""

    return f"{PREFIX}/{context.ids[0]}/documents/{context.ids[1]}/{suffix}"


def submit(context):
    """只投递 GPU 检查点，202 不表示已经生成向量或进入 READY。"""

    response = context.api("POST", endpoint(context))
    assert response.status_code == 202, response.text
    assert "model_dir" not in response.text and "output_dir" not in response.text
    return UUID(response.json()["embedding_task_id"])


def execute(context, task_id):
    """用户运行 pytest 时在本进程执行 Stub 任务，不连接 Broker 或 CUDA。"""

    return tasks.embed_document.apply(args=[str(context.ids[1])], task_id=str(task_id), throw=False)


def test_one_gpu_task_per_document_batches_and_local_results(context):
    """五个 Chunk 只有一条 GPU 消息，分 2/2/1 批生成，完成只进入 INDEXING 且 pgvector 仍为空。"""

    task_id = submit(context)
    assert len(context.messages) == 1 and context.model_loads == []
    result = execute(context, task_id)
    assert result.state == "SUCCESS", str(result.result)
    assert result.result["status"] == "awaiting_index" and result.result["dimension"] == DIMENSION
    assert [len(batch) for batch in context.stub.calls] == [2, 2, 1]
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "INDEXING" and document.error_message is None
        assert (document.processing_task_id, document.chunk_task_id) == context.ids[2:]
        assert session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.embedding.is_not(None))) == 0
        rows = processing.read_chunks(session, document)
        config = document.embedding_config
        manifest = ArtifactStore(config["output_dir"]).ready(document.doc_id, task_id, config["source_hash"],
                      digest_json(config["options"]), [str(row.chunk_id) for row in rows])
        assert manifest["chunk_count"] == 5 and manifest["model"]["dimension"] == DIMENSION
    assert "vector" not in result.result and "vectors" not in result.result and "unused-model" not in str(result.result)
    assert submit(context) == task_id and len(context.messages) == 1
    assert execute(context, task_id).result["status"] == "skipped"
    assert len(context.stub.calls) == 3


def test_waiting_retry_uses_frozen_config(context, monkeypatch):
    """未消费任务可重投同一代次，更新部署配置不会隐式改变正在执行的批大小或模型目录。"""

    task_id = submit(context)

    def changed_settings():
        """模拟 API 默认配置改变，已有任务继续使用原冻结参数。"""

        return Settings(_env_file=None, embedding_batch_size=16)

    monkeypatch.setattr(admin_api, "get_settings", changed_settings)
    assert submit(context) == task_id
    with context.session() as session:
        assert session.get(Document, context.ids[1]).embedding_config["options"]["batch_size"] == 2
    assert execute(context, task_id).state == "SUCCESS"


def test_second_batch_failure_is_atomic_and_same_task_can_resume(context):
    """第二批失败不发布成功结果、不修改 Chunk，手动恢复同一检查点后才能完成。"""

    task_id = submit(context)
    context.stub.fail_batch = 2
    failed = execute(context, task_id)
    assert failed.state == "FAILURE" and "EMBEDDING_TOO_LONG" in str(failed.result)
    directory = ArtifactStore(context.settings.embeddings_dir).directory(context.ids[1], task_id)
    assert not (directory / "manifest.json").exists() and not (directory / "vectors.jsonl").exists()
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.status == "FAILED" and "EMBEDDING_TOO_LONG" in document.error_message
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 5
    context.stub.fail_batch = None
    assert submit(context) == task_id
    assert execute(context, task_id).state == "SUCCESS"


def test_sql_rollback_reuses_verified_complete_artifact_without_inference(context, monkeypatch):
    """文件完整发布后 SQL 失败，同代次重试只校验并恢复状态，不重复本地推理。"""

    task_id = submit(context)
    original = processing.transition_document

    def fail_after_publish(document, target):
        """模拟文档状态写入失败，必须整体回滚但不删除已验证的不可变向量结果。"""

        raise OperationalError("SECRET SQL", {}, RuntimeError("SECRET connection"))

    monkeypatch.setattr(processing, "transition_document", fail_after_publish)
    with pytest.raises(OperationalError):
        with context.session() as session, session.begin():
            processing.run_embedding_step(session, context.ids[1], task_id)
    assert len(context.stub.calls) == 3
    monkeypatch.setattr(processing, "transition_document", original)

    def forbidden(*args, **kwargs):
        """恢复完整结果不应重新构造模型或重复推理。"""

        pytest.fail("完整结果恢复不应再次推理")

    monkeypatch.setattr(processing, "get_embedding_service", forbidden)
    result = execute(context, task_id)
    assert result.state == "SUCCESS" and result.result["artifact_reused"] is True
    assert len(context.stub.calls) == 3


def test_changed_chunk_input_fails_without_inference(context):
    """同一代次 Chunk 文本被改变时安全失败，不把旧输入向量附到新正文。"""

    task_id = submit(context)
    with context.session() as session, session.begin():
        session.scalar(select(DocumentChunk).where(DocumentChunk.chunk_metadata["chunk_index"].as_integer() == 1)).text = "改变的已有切片"
    result = execute(context, task_id)
    assert result.state == "FAILURE" and "EMBEDDING_SOURCE" in str(result.result)
    assert context.model_loads == []
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == "FAILED"


def test_old_generation_and_late_failure_cannot_overwrite_results(context):
    """已完成结果不被失败回调覆盖，重新解析换代次后旧 GPU 消息也不能落库。"""

    task_id = submit(context)
    assert execute(context, task_id).state == "SUCCESS"
    with context.session() as session, session.begin():
        processing.record_embedding_failure(session, context.ids[1], task_id, "迟到错误", True)
        assert session.get(Document, context.ids[1]).status == "INDEXING"
        document = session.get(Document, context.ids[1])
        document.embedding_task_id, document.embedding_config = None, None
        document.processing_task_id, document.status = uuid4(), "UPLOADED"
    assert execute(context, task_id).result["status"] == "stale_task"
    assert len(context.stub.calls) == 3


def test_publish_confirmation_lost_preserves_completed_indexing(context, monkeypatch):
    """投递确认丢失但 Worker 已完成时返回安全 503，不覆盖 INDEXING 或重算结果。"""

    def completed_then_failed(*args, **kwargs):
        """模拟 GPU 消费后 Broker 确认丢失，所有计算仍为内存 Stub。"""

        result = execute(context, UUID(kwargs["task_id"]))
        assert result.state == "SUCCESS", str(result.result)
        raise RuntimeError("SECRET Redis URL")

    monkeypatch.setattr(tasks.embed_document, "apply_async", completed_then_failed)
    response = context.api("POST", endpoint(context))
    assert response.status_code == 503 and "SECRET" not in response.text
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == "INDEXING"
    assert context.api("POST", endpoint(context)).status_code == 202
    assert len(context.stub.calls) == 3


@pytest.mark.parametrize("retry_count,delay", [(0, 5), (1, 10), (2, 20), (3, None)])
def test_database_retry_budget_and_safe_error(context, monkeypatch, retry_count, delay):
    """数据库短暂故障有固定有限退避，最终失败不暴露 SQL/连接原文，不重复创建任务。"""

    task_id, delays = submit(context), []

    def unavailable(*args, **kwargs):
        """注入私有数据库错误，不触发真实服务故障。"""

        raise OperationalError("SECRET SQL", {}, RuntimeError("SECRET connection"))

    def retry_stub(**kwargs):
        """捕获退避而不访问真实 Broker 或执行耗时等待。"""

        delays.append(kwargs["countdown"])
        return Retry(str(kwargs["exc"]))

    monkeypatch.setattr(tasks, "run_embedding_step", unavailable)
    monkeypatch.setattr(tasks.embed_document, "retry", retry_stub)
    tasks.embed_document.push_request(id=str(task_id), retries=retry_count)
    try:
        with pytest.raises(Retry if delay is not None else DocumentProcessingError) as error:
            tasks.embed_document.run(str(context.ids[1]))
    finally:
        tasks.embed_document.pop_request()
    assert "SECRET" not in str(error.value) and delays == ([] if delay is None else [delay])
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == ("FAILED" if delay is None else "EMBEDDING")


def test_invalid_checkpoint_is_not_left_forever_in_embedding(context):
    """当前任务参数损坏必须记录可追踪失败，不让 Document 永久保持转圈状态。"""

    task_id = submit(context)
    with context.session() as session, session.begin():
        document = session.get(Document, context.ids[1])
        document.embedding_config = document.embedding_config | {"options": {"unknown": True}}
    assert execute(context, task_id).state == "FAILURE"
    with context.session() as session:
        assert session.get(Document, context.ids[1]).status == "FAILED"


def test_api_rejects_cpu_config_cross_kb_and_unfinished_chunk(context, monkeypatch):
    """文档任务不能悄悄走 CPU，错误知识库与未切片文档均不创建 GPU 消息。"""

    wrong = f"{PREFIX}/{uuid4()}/documents/{context.ids[1]}/embed"
    assert context.api("POST", wrong).status_code == 404

    def cpu_settings():
        """显式 CPU 设置只允许直接调用 Service，不用于文档 GPU Worker。"""

        return Settings(_env_file=None, embedding_device="cpu")

    monkeypatch.setattr(admin_api, "get_settings", cpu_settings)
    response = context.api("POST", endpoint(context))
    assert response.status_code == 409 and "EMBEDDING_GPU_REQUIRED" in response.text
    with context.session() as session, session.begin():
        session.get(Document, context.ids[1]).status = "CHUNKING"
    assert context.api("POST", endpoint(context)).status_code == 409
    assert context.messages == [] and context.model_loads == []


def test_gpu_checkpoint_blocks_maintenance_until_completed(context, monkeypatch):
    """GPU 检查点活动时不换输入，完成后 Reindex 可换代次且保留历史文件。"""

    task_id = submit(context)
    assert context.api("POST", endpoint(context, "reindex")).status_code == 409
    assert context.api("POST", endpoint(context, "reparse") + "?force=true").status_code == 409
    assert execute(context, task_id).state == "SUCCESS"

    def accepted(*args, **kwargs):
        """只检查 Reindex 维护消息，不执行 Parser、缓存或 MinerU。"""

        assert kwargs["operation"] == "reindex"
        return True

    monkeypatch.setattr(admin_api, "publish_document", accepted)
    assert context.api("POST", endpoint(context, "reindex")).status_code == 202
    with context.session() as session:
        document = session.get(Document, context.ids[1])
        assert document.embedding_task_id is None and document.embedding_config is None
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 5
    assert (ArtifactStore(context.settings.embeddings_dir).directory(context.ids[1], task_id) / "manifest.json").is_file()


def test_gpu_task_routing_and_no_cpu_queue_fallback():
    """GPU 任务注册独立已有 gpu_queue，不建立逐 Chunk 任务或新的队列。"""

    assert "app.tasks.embeddings" in celery_app.conf.include
    assert celery_app.conf.task_routes[tasks.embed_document.name]["queue"] == "gpu_queue"


def test_chunk_repeat_after_embedding_does_not_reset_gpu_result(context):
    """向量完成后原 /chunk 重复调用只返回已完成切片，不清除 GPU 检查点或重新投递 CPU 任务。"""

    task_id = submit(context)
    assert execute(context, task_id).state == "SUCCESS"
    response = context.api("POST", endpoint(context, "chunk"))
    assert response.status_code == 202 and response.json()["status"] == "INDEXING"
    assert UUID(response.json()["chunk_task_id"]) == context.ids[3]
    with context.session() as session:
        assert session.get(Document, context.ids[1]).embedding_task_id == task_id
    assert len(context.stub.calls) == 3
