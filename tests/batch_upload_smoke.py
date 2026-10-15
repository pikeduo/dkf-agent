"""批量接口隔离数据库验收：逐文件事务、任务身份和日志，不调用真实云 API。"""

from io import BytesIO
from uuid import UUID, uuid4

import pytest
from business_log_helpers import read_log_line
from fastapi import HTTPException, Response, UploadFile
from kombu.exceptions import OperationalError as BrokerError
from mineru_samples import pdf_bytes, png_bytes
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import admin_knowledge_bases, mineru
from app.models import Document, DocumentParseJob
from app.services import batch_upload, document_upload
from app.tasks import documents

pytest_plugins = ("business_logging_smoke",)
PREFIX = "/api/admin/knowledge-bases"


def post_batch(api, kb_id, files, cloud=False):
    """在原上传路径重复 multipart file 字段模拟多选，不把多份原件合为文档。"""

    path = "mineru/documents" if cloud else "documents"
    return api(
        "POST",
        f"{PREFIX}/{kb_id}/{path}",
        files=[
            ("file", (name, content, "application/octet-stream"))
            for name, content in files
        ],
    )


def test_distinct_files_have_independent_ids_files_and_messages(
    upload_context, monkeypatch
):
    """每份不同内容对应独立原件、Document 和任务，顺序与原名都保留。"""

    api, kb_id, directory = upload_context
    messages = []

    def accepted(*args, **kwargs):
        """观察每个原生任务携带自己的 doc_id/task_id，不发送实际消息。"""

        messages.append(kwargs)

    monkeypatch.setattr(documents.process_document, "apply_async", accepted)
    files = [("同名.txt", b"first"), ("同名.txt", b"second"), ("正文.md", b"third")]
    response = post_batch(api, kb_id, files)
    assert response.status_code == 200
    payload = response.json()
    assert (payload["total"], payload["accepted_count"], payload["failed_count"]) == (
        3,
        3,
        0,
    )
    assert [item["index"] for item in payload["items"]] == [1, 2, 3]
    assert len({item["doc_id"] for item in payload["items"]}) == 3
    assert len(messages) == 3
    for item, (name, content), message in zip(
        payload["items"], files, messages, strict=True
    ):
        document = item["document"]
        assert item["status_code"] == 201 and document["file_name"] == name
        assert item["doc_id"] == document["doc_id"] == message["args"][0]
        assert document["task_id"] == message["task_id"]
        assert (
            directory / f"{item['doc_id']}.{document['file_type']}"
        ).read_bytes() == content
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 3


def test_partial_failure_and_duplicates_preserve_success(upload_context, monkeypatch):
    """同批重复、库内重复、空文件、超限和格式错误各自失败，其余文件仍提交。"""

    api, kb_id, directory = upload_context
    existing = api(
        "POST", f"{PREFIX}/{kb_id}/documents", files={"file": ("旧.txt", b"old")}
    ).json()
    messages = []

    def accepted(*args, **kwargs):
        """重复与拒绝的文件不得额外产生原生任务。"""

        messages.append(kwargs)

    monkeypatch.setattr(documents.process_document, "apply_async", accepted)
    response = post_batch(
        api,
        kb_id,
        [
            ("新.txt", b"new"),
            ("旧改名.md", b"old"),
            ("新改名.md", b"new"),
            ("空.txt", b""),
            ("非法.exe", b"x"),
            ("冒充.pdf", b"not pdf"),
            ("超限.txt", b"x" * (1024 * 1024 + 1)),
            ("最后.txt", b"last"),
        ],
    )
    items = response.json()["items"]
    assert [item["status_code"] for item in items] == [
        201,
        409,
        409,
        400,
        415,
        415,
        413,
        201,
    ]
    assert items[1]["doc_id"] == existing["doc_id"]
    assert items[2]["doc_id"] == items[0]["doc_id"]
    assert (
        response.json()["accepted_count"] == 2 and response.json()["failed_count"] == 6
    )
    assert len(list(directory.iterdir())) == 3 and len(messages) == 2


@pytest.mark.parametrize("cloud", [False, True])
def test_batch_preflight_errors_do_not_store(upload_context, cloud):
    """非法知识库、缺失字段和超批次上限在写盘与云配置初始化前拒绝。"""

    api, kb_id, directory = upload_context
    assert post_batch(api, uuid4(), [("a.txt", b"a")], cloud).status_code == 404
    assert (
        post_batch(
            api, kb_id, [(f"{n}.txt", b"x") for n in range(51)], cloud
        ).status_code
        == 413
    )
    path = "mineru/documents" if cloud else "documents"
    assert api("POST", f"{PREFIX}/{kb_id}/{path}", json={}).status_code == 422
    assert not directory.exists()


def test_native_dispatch_failure_keeps_file_and_later_success(
    upload_context, monkeypatch
):
    """单项 Broker 失败返回已保存 doc_id/FAILED，不回滚原件也不阻止后续投递。"""

    api, kb_id, directory = upload_context
    calls = 0

    def first_fails(*args, **kwargs):
        """只模拟首次投递失败，后续消息正常接受。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokerError("SECRET")

    monkeypatch.setattr(documents.process_document, "apply_async", first_fails)
    response = post_batch(api, kb_id, [("失败.txt", b"one"), ("成功.txt", b"two")])
    first, second = response.json()["items"]
    assert first["status_code"] == 503 and first["document"]["status"] == "FAILED"
    assert second["status_code"] == 201
    assert len(list(directory.iterdir())) == 2 and "SECRET" not in response.text
    assert (
        api("GET", f"{PREFIX}/{kb_id}/documents/{first['doc_id']}").json()["status"]
        == "FAILED"
    )


@pytest.mark.parametrize("error", [OSError, OperationalError, ValueError])
def test_storage_failure_rolls_back_only_current_item(
    upload_context, monkeypatch, error
):
    """写盘或明确入库失败仅影响当前文件，意外异常也不得泄漏路径或底层信息。"""

    api, kb_id, directory = upload_context
    original = document_upload.write_upload

    def fail_middle(upload, path):
        """在已独占写盘后模拟第二个文件故障，让已有补偿逻辑负责清理。"""

        original(upload, path)
        if upload.filename == "失败.txt":
            document_upload.remove_owned_file(path)
            if error is OperationalError:
                raise OperationalError(
                    "private_sql", None, Exception("SECRET local/path")
                )
            raise error("SECRET local/path")

    monkeypatch.setattr(document_upload, "write_upload", fail_middle)
    response = post_batch(
        api, kb_id, [("先.txt", b"one"), ("失败.txt", b"two"), ("后.txt", b"three")]
    )
    expected = {OSError: 507, OperationalError: 503, ValueError: 500}[error]
    assert [item["status_code"] for item in response.json()["items"]] == [
        201,
        expected,
        201,
    ]
    assert len(list(directory.iterdir())) == 2
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 2
    assert "SECRET" not in response.text and "local/path" not in response.text


def test_cloud_batch_is_explicit_independent_and_handles_duplicate(
    cloud_context, migrated_connection
):
    """图片和 PDF 各建一个云任务；CSV 拒绝、同批图片重复不新建任务或发起 API。"""

    context = cloud_context
    image = png_bytes()
    response = post_batch(
        context.api,
        context.kb_id,
        [
            ("图片.png", image),
            ("扫描.pdf", pdf_bytes()),
            ("表.csv", b"a,b\n1,2"),
            ("重复.png", image),
        ],
        cloud=True,
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["status_code"] for item in items] == [202, 202, 415, 409]
    assert items[0]["doc_id"] != items[1]["doc_id"]
    assert items[3]["doc_id"] == items[0]["doc_id"]
    assert len(context.messages) == 2 and context.provider.submissions == 0
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        jobs = session.scalars(select(DocumentParseJob)).all()
        assert {str(job.doc_id) for job in jobs} == {
            item["doc_id"] for item in items[:2]
        }
        assert len({job.task_id for job in jobs}) == 2
    assert len(list(context.directory.iterdir())) == 2


@pytest.mark.parametrize("file_count", [1, 2])
def test_cloud_cache_miss_without_token_preserves_upload_and_creates_no_job(
    cloud_context, monkeypatch, file_count
):
    """无Token的缓存miss保留已提交原件，逐项安全拒绝；不创建云Job或投递收费任务。"""

    settings = cloud_context.settings.model_copy(
        update={"mineru_api_token": SecretStr("")}
    )

    def no_token():
        """提供明确的空 Token 测试配置，不读取或修改真实密钥。"""

        return settings

    monkeypatch.setattr(mineru, "get_settings", no_token)
    files = [("图.png", png_bytes()), ("文.pdf", pdf_bytes())][:file_count]
    response = post_batch(
        cloud_context.api, cloud_context.kb_id, files, cloud=True
    )
    if file_count == 1:
        assert response.status_code == 503
        items = [{"status_code": response.status_code, "doc_id": response.json()["detail"]["doc_id"],
                  "detail": response.json()["detail"]}]
    else:
        assert response.status_code == 200
        payload = response.json()
        assert (payload["total"], payload["accepted_count"], payload["failed_count"]) == (2, 0, 2)
        items = payload["items"]
    assert len({item["doc_id"] for item in items}) == file_count
    for item, (name, content) in zip(items, files, strict=True):
        assert item["status_code"] == 503 and item["detail"]["error_code"] == "TOKEN_MISSING"
        prefix = f"{PREFIX}/{cloud_context.kb_id}/documents/{item['doc_id']}"
        document = cloud_context.api("GET", prefix).json()
        assert document["file_name"] == name and document["status"] == "UPLOADED"
        assert document["error_message"] is None
        assert cloud_context.api("GET", prefix + "/parse-jobs").json() == []
        assert (cloud_context.directory / f"{item['doc_id']}.{document['file_type']}").read_bytes() == content
    assert cloud_context.api("GET", f"{PREFIX}/{cloud_context.kb_id}/documents").json()["total"] == file_count
    assert len(list(cloud_context.directory.iterdir())) == file_count
    assert cloud_context.messages == []
    assert (cloud_context.provider.submissions, cloud_context.provider.uploads,
            cloud_context.provider.queries, cloud_context.provider.downloads) == (0, 0, 0, 0)
    assert "unit-secret-not-real" not in response.text


def test_cloud_publish_failure_preserves_job_and_continues(cloud_context, monkeypatch):
    """云任务投递失败保留 doc_id/job_id；后续文件仍有独立任务可被显式恢复。"""

    original = mineru.publish_step
    calls = 0

    def first_fails(*args, **kwargs):
        """首次短任务模拟队列失败，后续仍使用 Mock 投递。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            from app.services.mineru.errors import MinerUError

            raise MinerUError("QUEUE", "MinerU 任务投递失败", retryable=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(mineru, "publish_step", first_fails)
    response = post_batch(
        cloud_context.api,
        cloud_context.kb_id,
        [("图.png", png_bytes()), ("文.pdf", pdf_bytes())],
        cloud=True,
    )
    first, second = response.json()["items"]
    assert first["status_code"] == 503 and first["doc_id"]
    assert first["detail"]["job_id"]
    assert (
        second["status_code"] == 202
        and len(list(cloud_context.directory.iterdir())) == 2
    )


def test_batch_logs_are_safe_and_correlated(upload_context, event_stream):
    """批次与逐文件记录状态、数量及耗时，共享 request_id，不记录正文。"""

    api, kb_id, _ = upload_context
    response = post_batch(
        api, kb_id, [("正文.txt", b"PRIVATE_BATCH_CONTENT"), ("空.txt", b"")]
    )
    recorded = [read_log_line(line) for line in event_stream.getvalue().splitlines()]
    batch_events = [
        event for event in recorded if event["event"].startswith("upload.batch_")
    ]
    assert [event["event"] for event in batch_events] == [
        "upload.batch_started",
        "upload.batch_item",
        "upload.batch_item",
        "upload.batch_finished",
    ]
    assert {event["request_id"] for event in batch_events} == {
        response.headers["x-request-id"]
    }
    assert [
        event["file_index"]
        for event in batch_events
        if event["event"] == "upload.batch_item"
    ] == [1, 2]
    assert batch_events[-1]["accepted_count"] == batch_events[-1]["failed_count"] == 1
    request_event = next(
        event for event in recorded if event["event"] == "http.request"
    )
    assert request_event["route"] == f"{PREFIX}/{{kb_id}}/documents"
    assert request_event["status_code"] == 200
    assert "PRIVATE_BATCH_CONTENT" not in event_stream.getvalue()


def test_batch_openapi_is_file_array_without_changing_single(upload_context):
    """原路径的 file 为二进制数组以支持多选，保留单份响应且不注册 batch 路径。"""

    api, _, _ = upload_context
    schema = api("GET", "/openapi.json").json()
    for suffix in ("documents", "mineru/documents"):
        operation = schema["paths"][f"{PREFIX}/{{kb_id}}/{suffix}"]["post"]
        reference = operation["requestBody"]["content"]["multipart/form-data"][
            "schema"
        ]["$ref"]
        body = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        assert body["properties"]["file"]["type"] == "array"
        assert body["properties"]["file"]["items"] == {
            "type": "string",
            "format": "binary",
        }
        assert "file" in body["required"]
        assert f"{PREFIX}/{{kb_id}}/{suffix}/batch" not in schema["paths"]
        assert "200" in operation["responses"]
    assert (
        "201" in schema["paths"][f"{PREFIX}/{{kb_id}}/documents"]["post"]["responses"]
    )


@pytest.mark.parametrize("cloud", [False, True])
def test_batch_closes_all_files_when_preflight_fails(cloud):
    """直接调用入口验证超批次预检失败也关闭所有临时输入流。"""

    files = [UploadFile(filename="a.txt", file=BytesIO(b"x")) for _ in range(51)]
    function = (
        mineru.upload_mineru_document
        if cloud
        else admin_knowledge_bases.upload_knowledge_base_document
    )
    with pytest.raises(HTTPException) as error:
        function(UUID(int=1), None, Response(), files)
    assert error.value.status_code == 413
    assert all(upload.file.closed for upload in files)


def test_maximum_batch_and_cross_kb_deduplication(upload_context):
    """恰好 50 个文件可上传，同样内容在另一个知识库仍使用不同 doc_id。"""

    api, kb_id, directory = upload_context
    files = [(f"{n}.txt", str(n).encode()) for n in range(50)]
    response = post_batch(api, kb_id, files)
    assert response.json()["accepted_count"] == 50
    other = api("POST", PREFIX, json={"name": "批量第二库"}).json()["kb_id"]
    second = post_batch(api, other, files[:1]).json()
    assert second["doc_id"] != response.json()["items"][0]["doc_id"]
    assert len(list(directory.iterdir())) == 51


def test_database_flush_failure_does_not_revoke_previous_commit(
    upload_context, monkeypatch
):
    """第二份文件在真实写盘后 flush 失败，回滚其记录并清理原件，前后成功不受影响。"""

    api, kb_id, directory = upload_context
    original = Session.flush

    def fail_middle(session, *args, **kwargs):
        """只让指定待插入 Document 失败，不干扰知识库读取或其他事务。"""

        if any(
            isinstance(row, Document) and row.file_name == "失败.txt"
            for row in session.new
        ):
            raise OperationalError("private_sql", None, Exception("SECRET"))
        return original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_middle)
    response = post_batch(
        api, kb_id, [("先.txt", b"one"), ("失败.txt", b"two"), ("后.txt", b"three")]
    )
    assert [item["status_code"] for item in response.json()["items"]] == [201, 503, 201]
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 2
    assert len(list(directory.iterdir())) == 2 and "SECRET" not in response.text


def test_commit_confirmation_lost_preserves_file_for_list_recovery(
    upload_context, monkeypatch
):
    """定点模拟第二文件存储提交后确认丢失；额外编排commit不改变故障对象或丢失原件。"""

    api, kb_id, directory = upload_context
    original = Session.commit
    original_store = batch_upload.store_document
    target_name = "确认丢失.txt"
    fault_key = "test_batch_upload_commit_subject"
    injected, messages = [], []

    def track_storage_commit(session, kb_id, upload, settings, **kwargs):
        """只在单文件存储边界标记故障对象，退出后移除，不污染后续编排事务。"""

        session.info[fault_key] = upload.filename
        try:
            return original_store(session, kb_id, upload, settings, **kwargs)
        finally:
            session.info.pop(fault_key, None)

    def lost_confirmation(session):
        """目标原件真实提交后仅注入一次确认丢失，不依赖全局commit次数。"""

        target_storage = session.info.get(fault_key) == target_name
        original(session)
        if target_storage and not injected:
            injected.append(target_name)
            raise OperationalError(
                "COMMIT", None, Exception("SECRET"), connection_invalidated=True
            )

    def accepted(*args, **kwargs):
        """记录已确认文件的解析投递；确认丢失项不能被自动重投，不访问Broker。"""

        messages.append(kwargs)

    monkeypatch.setattr(batch_upload, "store_document", track_storage_commit)
    monkeypatch.setattr(Session, "commit", lost_confirmation)
    monkeypatch.setattr(documents.process_document, "apply_async", accepted)
    response = post_batch(
        api, kb_id, [("先.txt", b"one"), ("确认丢失.txt", b"two"), ("后.txt", b"three")]
    )
    assert [item["status_code"] for item in response.json()["items"]] == [201, 503, 201]
    assert response.json()["items"][1]["doc_id"] is None
    listing = api("GET", f"{PREFIX}/{kb_id}/documents").json()
    assert listing["total"] == 3 and injected == [target_name]
    recovered = next(item for item in listing["items"] if item["file_name"] == target_name)
    assert recovered["status"] == "UPLOADED"
    assert (directory / f"{recovered['doc_id']}.txt").read_bytes() == b"two"
    assert len(messages) == 2
    assert {message["args"][0] for message in messages} == {
        response.json()["items"][index]["doc_id"] for index in (0, 2)
    }
    assert len(list(directory.iterdir())) == 3
    assert "SECRET" not in response.text


def test_batch_can_execute_existing_native_task_per_document(processing_context):
    """批量上传不改变原生 Worker，独立任务可逐份解析并生成各自 Block。"""

    api, kb_id, _ = processing_context
    response = post_batch(api, kb_id, [("甲.txt", b"one"), ("乙.md", b"two")])
    for item in response.json()["items"]:
        document = item["document"]
        result = documents.process_document.apply(
            args=[document["doc_id"]], task_id=document["task_id"], throw=False
        )
        assert result.state == "SUCCESS" and result.result["block_count"] > 0
        assert (
            api("GET", f"{PREFIX}/{kb_id}/documents/{document['doc_id']}").json()[
                "status"
            ]
            == "CHUNKING"
        )


@pytest.mark.parametrize("cloud", [False, True])
def test_original_path_single_file_response_stays_compatible(cloud_context, cloud):
    """同一上传入口只选一份时仍返回原单文件对象和 201/202，不包进 items。"""

    name, content = ("图片.png", png_bytes()) if cloud else ("正文.txt", b"single")
    response = post_batch(
        cloud_context.api, cloud_context.kb_id, [(name, content)], cloud
    )
    assert response.status_code == (202 if cloud else 201)
    assert response.json()["doc_id"]
    assert "items" not in response.json()
    if cloud:
        assert response.json()["job_id"]
    else:
        assert response.json()["file_name"] == name


def test_trailing_slash_redirect_preserves_multiple_files(upload_context):
    """用户带末尾斜杠访问原路径时，307 重定向保持方法与全部原件。"""

    api, kb_id, directory = upload_context
    response = api(
        "POST",
        f"{PREFIX}/{kb_id}/documents/",
        follow_redirects=True,
        files=[("file", ("甲.txt", b"one")), ("file", ("乙.md", b"two"))],
    )
    assert response.status_code == 200
    assert [redirect.status_code for redirect in response.history] == [307]
    assert response.json()["accepted_count"] == 2
    assert len(list(directory.iterdir())) == 2


@pytest.mark.parametrize("cloud", [False, True])
def test_removed_batch_path_cannot_upload(upload_context, cloud):
    """旧新增 batch 路径已移除，客户端只能在原入口多选，不残留第二套上传 API。"""

    api, kb_id, directory = upload_context
    path = "mineru/documents/batch" if cloud else "documents/batch"
    response = api(
        "POST", f"{PREFIX}/{kb_id}/{path}", files={"files": ("甲.txt", b"one")}
    )
    assert response.status_code in {404, 405}
    assert not directory.exists()
