"""显式执行的异步入口验收：隔离数据库与原件，模拟 Broker，不访问真实 Redis。"""

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from kombu.exceptions import OperationalError as BrokerError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.api import admin_knowledge_bases as admin_api
from app.core.celery_app import celery_app
from app.models import Document, DocumentBlock, DocumentChunk
from app.services import document_processing as processing
from app.services.document_dispatch import PUBLISH_ERROR
from app.tasks import documents as tasks

pytest_plugins = ("document_upload_smoke",)
PREFIX = "/api/admin/knowledge-bases"


def post_file(api, kb_id, name, content):
    """通过 multipart 调用真实上传接口，避免提前导入插件影响 pytest 断言重写。"""

    return api(
        "POST",
        f"{PREFIX}/{kb_id}/documents",
        files={"file": (name, content, "application/octet-stream")},
    )


@pytest.fixture
def processing_context(upload_context, migrated_connection, monkeypatch):
    """将每次 Worker 执行和错误落库绑定测试保存点，不提交真实项目数据。"""

    connection, _, _ = migrated_connection

    @contextmanager
    def isolated_processing_session():
        """给任务提供独立会话，模拟生产事务提交但保留外层测试隔离。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    monkeypatch.setattr(tasks, "processing_session", isolated_processing_session)
    return upload_context


def upload_document(context):
    """上传固定样本并返回文档详情，由夹具模拟消息接收，不提前执行任务。"""

    api, kb_id, _ = context
    response = post_file(api, kb_id, "异步参考.txt", b"document processing fixture")
    assert response.status_code == 201, response.text
    return response.json()


def document_detail(context, document):
    """调用真实详情接口查询持久化状态，而非使用上传时的响应快照。"""

    api, kb_id, _ = context
    response = api("GET", f"{PREFIX}/{kb_id}/documents/{document['doc_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def execute_task(document):
    """在当前进程执行指定代次的任务，保留 Celery 重试和失败状态而不连接 Broker。"""

    return tasks.process_document.apply(
        args=[document["doc_id"]], task_id=document["task_id"], throw=False
    )


def test_upload_publishes_after_commit_and_duplicate_does_not_publish(
    processing_context, migrated_connection, monkeypatch
):
    """验证文档及文件就绪后投递同一标识到 CPU 队列，重复上传不产生第二条消息。"""

    api, kb_id, directory = processing_context
    connection, _, _ = migrated_connection
    messages = []
    original_publish = admin_api.publish_document

    def after_commit(session, doc_id, task_id):
        """发送之前上传会话必须已完成事务，保证生产 Worker 能读取提交后的记录。"""

        assert not session.in_transaction()
        return original_publish(session, doc_id, task_id)

    def accepted(*args, **kwargs):
        """在发送点检查已提交事务和记录，避免 Worker 收到尚不存在的文档。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            row = session.get(Document, UUID(kwargs["args"][0]))
            assert str(row.processing_task_id) == kwargs["task_id"]
            assert row.status == "UPLOADED"
            assert (directory / f"{row.doc_id}.txt").is_file()
        messages.append(kwargs)

    monkeypatch.setattr(tasks.process_document, "apply_async", accepted)
    monkeypatch.setattr(admin_api, "publish_document", after_commit)
    document = upload_document(processing_context)
    UUID(document["task_id"])
    assert messages == [
        {
            "args": [document["doc_id"]],
            "task_id": document["task_id"],
            "queue": "default_queue",
        }
    ]
    assert (
        post_file(api, kb_id, "重复.md", b"document processing fixture").status_code
        == 409
    )
    assert len(messages) == 1


def test_worker_native_parser_is_idempotent_without_chunks(
    processing_context, migrated_connection
):
    """原生解析写入块后等待切片，重复任务不重新解析或生成 Chunk、READY。"""

    document = upload_document(processing_context)
    first = execute_task(document)
    assert first.state == "SUCCESS"
    assert first.result == {
        "doc_id": document["doc_id"],
        "document_status": "CHUNKING",
        "status": "awaiting_chunk",
        "block_count": 1,
    }
    snapshot = document_detail(processing_context, document)
    assert snapshot["status"] == "CHUNKING"
    assert snapshot["error_message"] is None
    assert execute_task(document).result == {
        "doc_id": document["doc_id"],
        "document_status": "CHUNKING",
        "status": "skipped",
    }
    assert document_detail(processing_context, document) == snapshot
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentBlock)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


@pytest.mark.parametrize("problem", ["missing", "empty"])
def test_permanent_source_error_records_failure(processing_context, problem):
    """原件缺失或为空时立即失败并持久化原因，不进行无效自动重试。"""

    document = upload_document(processing_context)
    _, _, directory = processing_context
    path = directory / f"{document['doc_id']}.txt"
    if problem == "missing":
        path.unlink()
    else:
        path.write_bytes(b"")
    result = execute_task(document)
    assert result.state == "FAILURE"
    assert isinstance(result.result, tasks.DocumentProcessingError)
    detail = document_detail(processing_context, document)
    assert detail["status"] == "FAILED"
    assert detail["error_message"] == str(result.result)
    assert "重试" not in detail["error_message"]
    assert str(directory) not in str(result.result)


@pytest.mark.parametrize("fault", ["io", "database"])
def test_transient_failure_retries_and_clears_error(
    processing_context, monkeypatch, fault
):
    """临时 I/O 或数据库错误使用同一 task_id 重试，成功后清除错误提示。"""

    document = upload_document(processing_context)
    original = processing.check_source
    original_save = tasks.save_failure
    failures = []
    calls = 0

    def temporary_failure(row):
        """第一次模拟连接或读取中断，第二次恢复真实原件检查。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            if fault == "database":
                raise OperationalError(
                    "private SQL", None, Exception("secret_password")
                )
            raise OSError("secret_password")
        original(row)

    def observe_failure(*args, **kwargs):
        """在重试之前读取错误落库结果，验证恢复后并非一直残留旧错误。"""

        original_save(*args, **kwargs)
        failures.append(document_detail(processing_context, document))

    monkeypatch.setattr(processing, "check_source", temporary_failure)
    monkeypatch.setattr(tasks, "save_failure", observe_failure)
    result = execute_task(document)
    assert result.state == "SUCCESS"
    assert calls == 2
    assert failures[0]["status"] == "UPLOADED"
    assert "第 1 次重试" in failures[0]["error_message"]
    assert "secret_password" not in failures[0]["error_message"]
    final = document_detail(processing_context, document)
    assert final["status"] == "CHUNKING"
    assert final["task_id"] == document["task_id"]
    assert final["error_message"] is None


def test_retry_exhaustion_is_bounded_and_recorded(processing_context, monkeypatch):
    """持续临时故障最多执行四次，退避为 5/10/20 秒，最终记录安全 FAILED 原因。"""

    document = upload_document(processing_context)
    calls = 0
    countdowns = []
    original_retry = tasks.process_document.retry

    def unavailable(row):
        """始终模拟文件临时不可读，不破坏磁盘原件。"""

        nonlocal calls
        calls += 1
        raise OSError("secret_password")

    def record_retry(*args, **kwargs):
        """记录退避参数并保留 Celery 本身的重试机制，eager 模式不实际等待。"""

        countdowns.append(kwargs["countdown"])
        return original_retry(*args, **kwargs)

    monkeypatch.setattr(processing, "check_source", unavailable)
    monkeypatch.setattr(tasks.process_document, "retry", record_retry)
    result = execute_task(document)
    assert result.state == "FAILURE"
    assert calls == 4
    assert countdowns == [5, 10, 20]
    detail = document_detail(processing_context, document)
    assert detail["status"] == "FAILED"
    assert "重试已耗尽" in detail["error_message"]
    assert "secret_password" not in str(result.result)


def test_unexpected_failure_is_recorded_without_raw_message(
    processing_context, monkeypatch
):
    """未知框架异常也必须进入 FAILED，但数据库和任务结果不泄漏原始敏感信息。"""

    document = upload_document(processing_context)

    def broken_step(row):
        """模拟实现缺陷而非临时故障，避免无意义的重复执行。"""

        raise ValueError("secret_password")

    monkeypatch.setattr(processing, "check_source", broken_step)
    result = execute_task(document)
    assert result.state == "FAILURE"
    detail = document_detail(processing_context, document)
    assert detail["status"] == "FAILED"
    assert "ValueError" in detail["error_message"]
    assert "secret_password" not in detail["error_message"]
    assert "secret_password" not in str(result.result)


def test_resubmit_replaces_generation_and_old_task_cannot_overwrite(processing_context):
    """手动重投分配新标识，旧任务执行及失败回调都不能覆盖新一轮状态。"""

    api, kb_id, _ = processing_context
    old = upload_document(processing_context)
    response = api("POST", f"{PREFIX}/{kb_id}/documents/{old['doc_id']}/process")
    assert response.status_code == 202, response.text
    current = response.json()
    assert current["task_id"] != old["task_id"]
    assert current["status"] == "UPLOADED"
    assert execute_task(old).result["status"] == "stale_task"
    tasks.save_failure(UUID(old["doc_id"]), UUID(old["task_id"]), "过期错误", True)
    assert document_detail(processing_context, current) == current
    assert execute_task(current).result["status"] == "awaiting_chunk"
    assert (
        api("POST", f"{PREFIX}/{kb_id}/documents/{old['doc_id']}/process").status_code
        == 409
    )


def test_publish_failure_preserves_original_and_can_resubmit(
    processing_context, monkeypatch
):
    """投递失败仍保留已上传原件和唯一记录，恢复消息通道后可重新投递。"""

    api, kb_id, directory = processing_context

    def unavailable(*args, **kwargs):
        """模拟 Broker 不可用且原始错误含凭据，验证响应只返回安全提示。"""

        raise BrokerError("redis://secret_password@localhost")

    with monkeypatch.context() as patch:
        patch.setattr(tasks.process_document, "apply_async", unavailable)
        document = upload_document(processing_context)
        assert document["status"] == "FAILED"
        assert document["error_message"] == PUBLISH_ERROR
        failed = api("POST", f"{PREFIX}/{kb_id}/documents/{document['doc_id']}/process")
        assert failed.status_code == 503
        assert "secret_password" not in failed.text
    assert len(list(directory.iterdir())) == 1
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 1
    response = api("POST", f"{PREFIX}/{kb_id}/documents/{document['doc_id']}/process")
    assert response.status_code == 202
    current = response.json()
    assert current["task_id"] != document["task_id"]
    assert current["error_message"] is None
    assert execute_task(current).state == "SUCCESS"
    assert document_detail(processing_context, current)["status"] == "CHUNKING"


def test_lost_publish_confirmation_does_not_regress_worker_state(
    processing_context, monkeypatch
):
    """模拟 Worker 已处理但发送端确认丢失，不将已经推进的文档错误回退为 FAILED。"""

    def accepted_but_confirmation_lost(*args, **kwargs):
        """在发送异常前执行消息，重现发送和消费不同步的边界条件。"""

        result = tasks.process_document.apply(
            args=kwargs["args"], task_id=kwargs["task_id"], throw=False
        )
        assert result.state == "SUCCESS"
        raise BrokerError("confirmation lost")

    monkeypatch.setattr(
        tasks.process_document, "apply_async", accepted_but_confirmation_lost
    )
    document = upload_document(processing_context)
    assert document["status"] == "CHUNKING"
    assert document["error_message"] is None


@pytest.mark.parametrize("method, suffix", [("GET", ""), ("POST", "/process")])
def test_document_routes_reject_cross_kb_and_missing_ids(
    processing_context, method, suffix
):
    """文档详情和重投入口均按知识库隔离，不存在和非法 UUID 返回预期错误。"""

    api, kb_id, _ = processing_context
    document = upload_document(processing_context)
    other = api("POST", PREFIX, json={"name": "隔离库"}).json()["kb_id"]
    assert (
        api(
            method, f"{PREFIX}/{other}/documents/{document['doc_id']}{suffix}"
        ).status_code
        == 404
    )
    assert (
        api(method, f"{PREFIX}/{kb_id}/documents/{uuid4()}{suffix}").status_code == 404
    )
    assert api(method, f"{PREFIX}/{kb_id}/documents/invalid{suffix}").status_code == 422


def test_task_result_api_supports_document_dictionary(processing_context, monkeypatch):
    """任务查询支持文档框架字典结果，保留 add 任务的兼容结构且不连接真实 Redis。"""

    api, _, _ = processing_context
    document = upload_document(processing_context)
    result = execute_task(document).result

    def metadata(backend, task_id):
        """模拟 Result Backend 成功元信息，API 仍执行真实序列化和状态判定。"""

        return {"status": "SUCCESS", "result": result}

    monkeypatch.setattr(type(celery_app.backend), "get_task_meta", metadata)
    response = api("GET", f"/tasks/{document['task_id']}")
    assert response.status_code == 200
    assert response.json()["result"] == result


def test_task_registration_and_reliability_options():
    """核对 CPU 队列、任务注册、最大重试次数和晚确认配置，无需启动 Worker。"""

    task = tasks.process_document
    assert task.name in celery_app.tasks
    assert "app.tasks.documents" in celery_app.conf.include
    assert celery_app.conf.task_routes[task.name]["queue"] == "default_queue"
    assert task.max_retries == 3
    assert task.acks_late and task.reject_on_worker_lost


@pytest.mark.parametrize(
    "state", ["OCR_PROCESSING", "CHUNKING", "EMBEDDING", "INDEXING", "READY", "FAILED"]
)
def test_advanced_or_failed_document_is_not_overwritten(
    processing_context, migrated_connection, state
):
    """当前入口不重复处理已推进或失败的文档，失败回调也不会覆盖这些状态。"""

    api, kb_id, _ = processing_context
    document = upload_document(processing_context)
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        row = session.get(Document, UUID(document["doc_id"]))
        row.status = state
        row.error_message = "已保存失败" if state == "FAILED" else None
        session.commit()
    snapshot = document_detail(processing_context, document)
    assert execute_task(document).result["status"] == "skipped"
    tasks.save_failure(
        UUID(document["doc_id"]), UUID(document["task_id"]), "过期错误", True
    )
    assert document_detail(processing_context, document) == snapshot
    if state != "FAILED":
        assert (
            api(
                "POST", f"{PREFIX}/{kb_id}/documents/{document['doc_id']}/process"
            ).status_code
            == 409
        )


def test_missing_document_and_invalid_task_identifiers(processing_context):
    """已删除文档消息安全跳过，非法文档或任务标识明确失败而不访问数据库。"""

    missing = {"doc_id": str(uuid4()), "task_id": str(uuid4())}
    assert execute_task(missing).result["status"] == "not_found"
    for field in ("doc_id", "task_id"):
        invalid = missing | {field: "invalid"}
        result = execute_task(invalid)
        assert result.state == "FAILURE"
        assert isinstance(result.result, tasks.DocumentProcessingError)
        assert "合法 UUID" in str(result.result)


def test_relative_source_path_is_root_anchored(
    processing_context, migrated_connection, monkeypatch
):
    """Worker 相对路径固定以项目根为基准，不依赖 Worker 启动时的当前目录。"""

    document = upload_document(processing_context)
    _, _, directory = processing_context
    monkeypatch.setattr(processing, "PROJECT_ROOT", directory.parent)
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        row = session.get(Document, UUID(document["doc_id"]))
        row.file_path = f"uploads/{row.doc_id}.txt"
        session.commit()
    assert execute_task(document).state == "SUCCESS"
    assert document_detail(processing_context, document)["status"] == "CHUNKING"


def test_failure_to_record_reason_preserves_task_failure(
    processing_context, monkeypatch
):
    """数据库持续不可用时错误落库可失败，但任务仍明确失败并保留安全日志。"""

    document = upload_document(processing_context)
    logs = []

    def error_log(template, *args):
        """捕获错误日志调用，避免 Alembic 测试日志配置影响应用 logger 的启用状态。"""

        logs.append(template % args)

    @contextmanager
    def unavailable_database():
        """模拟连接获取失败，使处理和失败原因持久化都无法取得数据库会话。"""

        raise OperationalError("private SQL", None, Exception("secret_password"))
        yield  # 无法到达的 yield 用于保持上下文管理器协议。

    monkeypatch.setattr(tasks, "processing_session", unavailable_database)
    monkeypatch.setattr(tasks.logger, "error", error_log)
    result = execute_task(document)
    assert result.state == "FAILURE"
    assert "重试已耗尽" in str(result.result)
    assert len(logs) == 4
    assert all("未能写入数据库" in message for message in logs)
    assert all("secret_password" not in message for message in logs)


@pytest.mark.parametrize(
    "source, target",
    [
        (source, target)
        for source, targets in processing.TRANSITIONS.items()
        for target in targets
    ],
)
def test_allowed_state_transitions(source, target):
    """验证预留阶段的合法状态边，同时要求失败原因且正常推进清除旧错误。"""

    document = Document(status=source, error_message="旧错误")
    error = "处理失败" if target == "FAILED" else None
    processing.transition_document(document, target, error)
    assert document.status == target
    assert document.error_message == error


@pytest.mark.parametrize("target", ["READY", "CHUNKING", "UNKNOWN"])
def test_invalid_state_shortcuts_are_rejected(target):
    """不能从上传状态跳过解析标记成功，也不能使用未知状态。"""

    document = Document(status="UPLOADED")
    with pytest.raises(ValueError, match="状态流转"):
        processing.transition_document(document, target)
    with pytest.raises(ValueError, match="失败原因"):
        processing.transition_document(document, "FAILED")
    assert document.status == "UPLOADED"


def test_migration_preserves_existing_documents(
    processing_context, migrated_connection
):
    """回退及升级仅改变任务关联列，保留旧文档并为历史记录设置空标识。"""

    document = upload_document(processing_context)
    connection, config, schema = migrated_connection
    with connection.begin_nested():
        command.downgrade(config, "0001_knowledge_base")
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("documents", schema=schema)
        }
        assert "processing_task_id" not in columns
        assert connection.scalar(text("SELECT count(*) FROM documents")) == 1
        command.upgrade(config, "head")
        assert (
            connection.scalar(text("SELECT processing_task_id FROM documents")) is None
        )
        assert connection.scalar(text("SELECT doc_id FROM documents")) == UUID(
            document["doc_id"]
        )
        command.check(config)
