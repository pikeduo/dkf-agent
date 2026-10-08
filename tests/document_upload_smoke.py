"""显式执行的上传验收，仅使用隔离数据库 schema 与 pytest 临时存储目录。"""

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import admin_knowledge_bases as admin_api
from app.core.config import get_settings
from app.models import Document
from app.services import document_upload as uploads
from app.tasks.documents import process_document

pytest_plugins = ("knowledge_admin_smoke",)
PREFIX = "/api/admin/knowledge-bases"


def sample_content(extension):
    """生成足以通过首版格式标识校验的样本，DOCX 使用不解压的最小 ZIP 容器。"""

    if extension == ".pdf":
        return b"%PDF-1.4\nminimal upload fixture\n%%EOF"
    if extension in {".jpg", ".jpeg"}:
        return b"\xff\xd8\xff\xe0upload fixture\xff\xd9"
    if extension == ".png":
        return b"\x89PNG\r\n\x1a\nupload fixture"
    if extension == ".docx":
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        return buffer.getvalue()
    return "非结构化知识库上传样本".encode()


@pytest.fixture
def upload_context(api, tmp_path, monkeypatch):
    """将文件存储指向临时目录、上限设为 1 MiB，并创建本用例的知识库。"""

    settings = get_settings().model_copy(
        update={"upload_dir": tmp_path / "uploads", "max_upload_size_mb": 1}
    )

    def test_settings():
        """返回独立配置副本，不改变缓存配置或真实 .env。"""

        return settings

    monkeypatch.setattr(admin_api, "get_settings", test_settings)

    def accepted_task(*args, **kwargs):
        """模拟 Broker 接受任务而不执行 Worker，避免上传验收污染真实 Redis 队列。"""

        return SimpleNamespace(id=kwargs["task_id"])

    monkeypatch.setattr(process_document, "apply_async", accepted_task)
    kb = api("POST", PREFIX, json={"name": "上传验收库"}).json()
    return api, kb["kb_id"], settings.upload_dir


def post_file(api, kb_id, name, content):
    """以 multipart 提交单文件，故意使用通用 MIME 验证服务不依赖客户端类型声明。"""

    return api(
        "POST",
        f"{PREFIX}/{kb_id}/documents",
        files={"file": (name, content, "application/octet-stream")},
    )


def assert_no_uploads(api, kb_id, directory):
    """验证失败请求没有新增数据库记录或残留文件，允许存储目录尚未创建。"""

    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 0
    assert not directory.exists() or not list(directory.iterdir())


@pytest.mark.parametrize(
    "extension", [".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png"]
)
def test_upload_creates_exact_file_and_document(
    upload_context, migrated_connection, extension
):
    """验证七种格式的原名、哈希、磁盘字节、状态和文档记录逐一对应。"""

    api, kb_id, directory = upload_context
    content = sample_content(extension)
    name = f"原始参考资料{extension.upper()}"
    response = post_file(api, kb_id, name, content)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["file_name"] == name
    assert result["file_type"] == extension[1:]
    assert result["file_hash"] == hashlib.sha256(content).hexdigest()
    assert result["source_type"] == "uploaded"
    assert result["status"] == "UPLOADED"
    assert result["error_message"] is None
    assert "file_path" not in result
    path = directory / f"{result['doc_id']}{extension}"
    assert list(directory.iterdir()) == [path]
    assert path.read_bytes() == content
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        document = session.get(Document, UUID(result["doc_id"]))
        assert document.kb_id == UUID(kb_id)
        assert document.file_path == str(path)
        assert document.file_name == name
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["items"] == [result]


def test_same_kb_duplicate_content_preserves_original(upload_context):
    """同库相同内容即使改名也返回已有标识，并保留唯一的原文件和记录。"""

    api, kb_id, directory = upload_context
    content = sample_content(".txt")
    first = post_file(api, kb_id, "原文件.txt", content).json()
    response = post_file(api, kb_id, "换名.md", content)
    assert response.status_code == 409
    assert response.json()["detail"]["doc_id"] == first["doc_id"]
    assert response.json()["detail"]["kb_id"] == kb_id
    assert len(list(directory.iterdir())) == 1
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["items"] == [first]


def test_same_content_in_different_kbs_has_distinct_files(upload_context):
    """验证跨库允许相同内容，各自使用独立 doc_id 和磁盘文件。"""

    api, kb_id, directory = upload_context
    other = api("POST", PREFIX, json={"name": "另一个上传库"}).json()["kb_id"]
    first = post_file(api, kb_id, "参考.txt", b"same content").json()
    second_response = post_file(api, other, "参考.txt", b"same content")
    assert second_response.status_code == 201
    second = second_response.json()
    assert first["doc_id"] != second["doc_id"]
    assert first["file_hash"] == second["file_hash"]
    assert len(list(directory.iterdir())) == 2


@pytest.mark.parametrize(
    "name", ["../escape.txt", "folder/name.txt", "folder\\name.txt", "a" * 252 + ".txt"]
)
def test_invalid_filename_is_rejected(upload_context, name):
    """拒绝路径穿越和过长原名，且不写入任何文件或 Document。"""

    api, kb_id, directory = upload_context
    assert post_file(api, kb_id, name, b"text").status_code == 400
    assert_no_uploads(api, kb_id, directory)


@pytest.mark.parametrize("name", ["file.exe", "file.zip", "file", "file.csv"])
def test_unsupported_extension_is_415(upload_context, name):
    """验证扩展名白名单拒绝未支持文件，失败请求不创建存储目录或记录。"""

    api, kb_id, directory = upload_context
    assert post_file(api, kb_id, name, b"text").status_code == 415
    assert_no_uploads(api, kb_id, directory)


@pytest.mark.parametrize("extension", [".pdf", ".docx", ".png", ".jpg", ".jpeg"])
def test_mismatched_content_is_415(upload_context, extension):
    """验证二进制格式不能仅靠改扩展名冒充有效类型。"""

    api, kb_id, directory = upload_context
    assert (
        post_file(
            api, kb_id, "fake" + extension, b"not the declared format"
        ).status_code
        == 415
    )
    assert_no_uploads(api, kb_id, directory)


def test_generic_zip_is_not_docx(upload_context):
    """验证合法 ZIP 但缺少 DOCX 必要成员时仍拒绝上传，不解压其中的文件。"""

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", "not docx")
    api, kb_id, directory = upload_context
    assert post_file(api, kb_id, "fake.docx", buffer.getvalue()).status_code == 415
    assert_no_uploads(api, kb_id, directory)


def test_empty_file_is_400(upload_context):
    """验证空文件被拒绝，而不是创建空内容哈希对应的文档。"""

    api, kb_id, directory = upload_context
    assert post_file(api, kb_id, "empty.txt", b"").status_code == 400
    assert_no_uploads(api, kb_id, directory)


def test_actual_size_boundary(upload_context):
    """验证实际字节大小恰好等于上限可上传，多一个字节则拒绝且不残留文件。"""

    api, kb_id, directory = upload_context
    assert (
        post_file(api, kb_id, "too-large.txt", b"x" * (1024 * 1024 + 1)).status_code
        == 413
    )
    assert_no_uploads(api, kb_id, directory)
    response = post_file(api, kb_id, "boundary.txt", b"x" * (1024 * 1024))
    assert response.status_code == 201
    assert (
        directory / f"{response.json()['doc_id']}.txt"
    ).stat().st_size == 1024 * 1024


@pytest.mark.parametrize(
    "identifier, expected", [("missing", 404), ("invalid-id", 422)]
)
def test_unknown_or_invalid_kb_does_not_store(upload_context, identifier, expected):
    """验证不存在或非法知识库标识不能产生文件或文档记录。"""

    api, kb_id, directory = upload_context
    identifier = str(uuid4()) if identifier == "missing" else identifier
    assert post_file(api, identifier, "sample.txt", b"text").status_code == expected
    assert_no_uploads(api, kb_id, directory)


def test_missing_file_is_422(upload_context):
    """验证 JSON 请求不能替代必填 multipart 文件字段。"""

    api, kb_id, directory = upload_context
    assert (
        api("POST", f"{PREFIX}/{kb_id}/documents", json={"file": "text"}).status_code
        == 422
    )
    assert_no_uploads(api, kb_id, directory)


def test_file_collision_does_not_overwrite_or_delete(upload_context, monkeypatch):
    """模拟 UUID 文件已存在，验证独占写入失败不会覆盖或清理不属于本请求的文件。"""

    api, kb_id, directory = upload_context
    doc_id = uuid4()
    directory.mkdir()
    existing = directory / f"{doc_id}.txt"
    existing.write_bytes(b"preserve existing file")

    def same_id():
        """固定生成标识以构造磁盘路径冲突，不修改数据库中的标识生成策略。"""

        return doc_id

    monkeypatch.setattr(uploads, "uuid4", same_id)
    assert post_file(api, kb_id, "sample.txt", b"new content").status_code == 507
    assert existing.read_bytes() == b"preserve existing file"
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 0


def test_partial_write_failure_removes_file(upload_context, monkeypatch):
    """模拟写入部分字节后读取失败，验证半成品由存储函数清理且数据库不新增记录。"""

    api, kb_id, directory = upload_context
    original = uploads.write_upload

    def failing_write(upload, path):
        """为写盘阶段替换故障输入流，使第一块成功、第二块触发磁盘类异常。"""

        class BrokenStream(BytesIO):
            """可定位但第二次读取失败的临时输入流。"""

            def read(self, size=-1):
                """首块返回少量数据，后续读取模拟 I/O 失败。"""

                if self.tell():
                    raise OSError("private storage error")
                return super().read(3)

        original(SimpleNamespace(file=BrokenStream(b"partial content")), path)

    monkeypatch.setattr(uploads, "write_upload", failing_write)
    response = post_file(api, kb_id, "sample.txt", b"text")
    assert response.status_code == 507
    assert "private storage" not in response.text
    assert_no_uploads(api, kb_id, directory)


@pytest.mark.parametrize("phase", ["flush", "commit"])
def test_database_failure_cleans_new_file(upload_context, monkeypatch, phase):
    """模拟写盘后的明确入库失败，验证事务回滚和文件清理，同时隐藏底层异常。"""

    api, kb_id, directory = upload_context
    with monkeypatch.context() as patch:
        original = getattr(Session, phase)

        def failed_database(*args, **kwargs):
            """在指定持久化阶段抛出连接仍有效的数据库异常。"""

            if phase == "flush" and not args[0].new:
                return original(*args, **kwargs)
            assert len(list(directory.iterdir())) == 1
            raise OperationalError("private_sql", None, Exception("secret_password"))

        patch.setattr(Session, phase, failed_database)
        response = post_file(api, kb_id, "sample.txt", b"text")
    assert response.status_code == 503
    assert "secret_password" not in response.text
    assert_no_uploads(api, kb_id, directory)


def test_concurrent_duplicate_constraint_cleans_loser_file(upload_context, monkeypatch):
    """绕过前置去重检查模拟竞争上传，数据库约束仍返回已有标识并清理失败方文件。"""

    api, kb_id, directory = upload_context
    first = post_file(api, kb_id, "first.txt", b"same").json()
    original = uploads.reject_duplicate
    checks = 0

    def skip_first_check(session, identifier, file_hash):
        """第一次假装尚无重复记录，唯一约束失败后的第二次查询使用真实实现。"""

        nonlocal checks
        checks += 1
        if checks > 1:
            original(session, identifier, file_hash)

    monkeypatch.setattr(uploads, "reject_duplicate", skip_first_check)
    response = post_file(api, kb_id, "second.txt", b"same")
    assert response.status_code == 409
    assert response.json()["detail"]["doc_id"] == first["doc_id"]
    assert len(list(directory.iterdir())) == 1
    assert api("GET", f"{PREFIX}/{kb_id}/documents").json()["total"] == 1


def test_uncertain_commit_preserves_original_for_manual_check(
    upload_context, monkeypatch
):
    """模拟提交确认丢失，验证返回 503 时保留原件供人工核对，而非擅自删除。"""

    api, kb_id, directory = upload_context

    def uncertain_commit(session):
        """模拟失效连接上的提交异常，无法从客户端确定服务端是否已提交。"""

        raise OperationalError(
            "COMMIT", None, Exception("private error"), connection_invalidated=True
        )

    monkeypatch.setattr(Session, "commit", uncertain_commit)
    response = post_file(api, kb_id, "sample.txt", b"preserve on uncertainty")
    assert response.status_code == 503
    assert len(list(directory.iterdir())) == 1
    assert next(directory.iterdir()).read_bytes() == b"preserve on uncertainty"


def test_relative_storage_path_is_root_anchored(
    upload_context, monkeypatch, migrated_connection
):
    """使用临时项目根模拟相对存储配置，验证不受工作目录影响且数据库保存相对路径。"""

    api, kb_id, directory = upload_context
    root = directory.parent
    monkeypatch.setattr(uploads, "PROJECT_ROOT", root)
    settings = get_settings().model_copy(update={"upload_dir": Path("uploads")})

    def relative_settings():
        """返回相对目录配置，其他运行配置保留原有默认值。"""

        return settings

    monkeypatch.setattr(admin_api, "get_settings", relative_settings)
    response = post_file(api, kb_id, "sample.txt", b"relative path")
    assert response.status_code == 201
    connection, _, _ = migrated_connection
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        document = session.scalar(
            select(Document).where(Document.doc_id == UUID(response.json()["doc_id"]))
        )
        assert document.file_path == f"uploads/{document.doc_id}.txt"
        assert (root / document.file_path).read_bytes() == b"relative path"


def test_upload_openapi_declares_multipart_and_error_statuses(upload_context):
    """确认 Swagger 提供文件选择控件及上传接口的成功、重复、超限和存储错误说明。"""

    api, _, _ = upload_context
    operation = api("GET", "/openapi.json").json()["paths"][
        f"{PREFIX}/{{kb_id}}/documents"
    ]["post"]
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert {"201", "400", "404", "409", "413", "415", "422", "503", "507"}.issubset(
        operation["responses"]
    )
