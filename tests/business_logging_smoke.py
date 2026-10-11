"""业务日志的事务验收：复用临时 schema，Mock 云服务与队列，不操作真实业务数据。"""

import io
import logging

import pytest
from business_log_helpers import read_log_line
from sqlalchemy.exc import OperationalError

from app.core.business_logging import LOGGER, ReadableFormatter
from app.services import document_processing
from app.tasks import documents, mineru

pytest_plugins = ("native_parser_db_smoke", "mineru_db_smoke")


@pytest.fixture
def event_stream(migrated_connection):
    """给现有业务 Logger 增加内存观察器，不改变生产处理器与事务配置。"""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ReadableFormatter())
    # Alembic 的 fileConfig 会禁用已存在的 Logger；仅恢复本测试的观察器。
    disabled, level = LOGGER.disabled, LOGGER.level
    LOGGER.disabled = False
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    try:
        yield stream
    finally:
        LOGGER.removeHandler(handler)
        handler.close()
        LOGGER.disabled, LOGGER.level = disabled, level


def events(stream):
    """按行读取安全业务事件，供提交成功与回滚场景比较。"""

    return [read_log_line(line) for line in stream.getvalue().splitlines()]


def native_upload(context):
    """通过真实上传路由保存一份文本样本，由 fixture 模拟消息投递。"""

    api, kb_id, _ = context
    response = api(
        "POST",
        f"/api/admin/knowledge-bases/{kb_id}/documents",
        files={"file": ("日志验收.txt", b"PRIVATE_DOCUMENT_CONTENT")},
    )
    assert response.status_code == 201
    return response.json()


def test_native_upload_parse_and_duplicate_logged(processing_context, event_stream):
    """正常上传与解析产生安全摘要，重复文件有明确记录，解析正文不进入日志。"""

    document = native_upload(processing_context)
    result = documents.process_document.apply(
        args=[document["doc_id"]], task_id=document["task_id"], throw=False
    )
    assert result.state == "SUCCESS"
    api, kb_id, _ = processing_context
    assert (
        api(
            "POST",
            f"/api/admin/knowledge-bases/{kb_id}/documents",
            files={"file": ("重复.txt", b"PRIVATE_DOCUMENT_CONTENT")},
        ).status_code
        == 409
    )
    recorded = events(event_stream)
    names = [event["event"] for event in recorded]
    for name in (
        "upload.received",
        "upload.stored",
        "document.dispatched",
        "parse.started",
        "parse.validated",
        "parse.persisted",
        "upload.duplicate",
    ):
        assert name in names
    stored = next(event for event in recorded if event["event"] == "upload.stored")
    persisted = next(event for event in recorded if event["event"] == "parse.persisted")
    assert stored["doc_id"] == persisted["doc_id"] == document["doc_id"]
    assert stored["file_name"] == "日志验收.txt"
    assert stored["size_bytes"] == len(b"PRIVATE_DOCUMENT_CONTENT")
    assert persisted["block_count"] == result.result["block_count"]
    assert persisted["state"] == "CHUNKING"
    assert "PRIVATE_DOCUMENT_CONTENT" not in event_stream.getvalue()


def test_native_persistence_failure_never_logs_success(
    processing_context, event_stream, monkeypatch
):
    """校验已通过但写块失败时只能记重试/失败，不能误记已落库。"""

    document = native_upload(processing_context)

    def failed_persist(*args):
        """模拟含敏感连接串的数据库错误，由任务边界安全分类。"""

        raise OperationalError("password=SECRET", {}, Exception("SECRET"))

    monkeypatch.setattr(document_processing, "persist_blocks", failed_persist)
    result = documents.process_document.apply(
        args=[document["doc_id"]], task_id=document["task_id"], retries=3, throw=False
    )
    assert result.state == "FAILURE"
    names = [event["event"] for event in events(event_stream)]
    assert "parse.validated" in names
    assert "parse.failed" in names
    assert "parse.persisted" not in names
    assert "SECRET" not in event_stream.getvalue()


def cloud_upload(context):
    """从离线 cloud fixture 的真实入口上传 PDF，生成可执行的云解析任务。"""

    from mineru_samples import pdf_bytes

    response = context.api(
        "POST",
        f"/api/admin/knowledge-bases/{context.kb_id}/mineru/documents",
        files={"file": ("云日志验收.pdf", pdf_bytes())},
    )
    assert response.status_code == 202
    return response.json()


def execute_cloud_step(job, step):
    """插件完成加载后再复用受控 RetryTask，避免提前导入导致断言重写失效。"""

    from mineru_db_smoke import RetryTask

    return mineru.execute_step(RetryTask(), job["job_id"], step)


def test_cloud_checkpoints_and_persistence_logged(cloud_context, event_stream):
    """批次、上传、远端状态、下载、Adapter 和落库各有摘要，不包含签名资源或正文。"""

    job = cloud_upload(cloud_context)
    execute_cloud_step(job, "submit")
    cloud_context.provider.state = "done"
    result = execute_cloud_step(job, "check")
    assert result["status"] == "awaiting_chunk"
    recorded = events(event_stream)
    names = [event["event"] for event in recorded]
    for name in (
        "mineru.batch_created",
        "mineru.upload_complete",
        "mineru.remote_state",
        "mineru.result_downloaded",
        "mineru.adapter_validated",
        "mineru.blocks_persisted",
    ):
        assert name in names
    persisted = next(
        event for event in recorded if event["event"] == "mineru.blocks_persisted"
    )
    assert persisted["job_id"] == job["job_id"]
    assert persisted["block_count"] == result["block_count"]
    assert "https://" not in event_stream.getvalue()
    assert "unit-secret-not-real" not in event_stream.getvalue()


def test_cloud_bad_result_logs_failure_not_persisted(cloud_context, event_stream):
    """结果 ZIP 损坏不能靠日志伪装成功；Adapter 错误安全分类且不记录原件正文。"""

    job = cloud_upload(cloud_context)
    execute_cloud_step(job, "submit")
    cloud_context.provider.state = "done"
    cloud_context.provider.archive = b"NOT_A_ZIP_SECRET"
    with pytest.raises(documents.DocumentProcessingError):
        execute_cloud_step(job, "check")
    names = [event["event"] for event in events(event_stream)]
    assert "mineru.failed" in names
    assert "mineru.blocks_persisted" not in names
    assert "NOT_A_ZIP_SECRET" not in event_stream.getvalue()
