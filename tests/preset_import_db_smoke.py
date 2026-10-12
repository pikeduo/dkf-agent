"""仅用户运行的预置导入数据库用例；隔离记录/Broker Stub，不产生文档或加载模型。"""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, KnowledgeBase
from app.services import document_upload as uploads
from app.services import preset_import as imports
from app.services.preset_manifest import PresetFile

pytest_plugins = ("knowledge_db_smoke",)


def test_preset_store_and_duplicate_keep_one_record(migrated_connection, monkeypatch):
    """复用真实事务及唯一约束；文件 IO 和格式检测用 Stub，不生成实际文档样本。"""

    connection, _, _ = migrated_connection
    digest = "a" * 64

    def hash_stub(upload, max_bytes):
        """返回预检摘要，不读取原件。"""

        return digest

    def no_file(*args):
        """格式/写盘 Stub 不生成文档，也不执行解析或云请求。"""

        return None

    monkeypatch.setattr(uploads, "hash_upload", hash_stub)
    monkeypatch.setattr(uploads, "validate_content", no_file)
    monkeypatch.setattr(uploads, "write_upload", no_file)

    class StreamStub:
        """提供 store_document 所需 seek，未承载文档正文。"""

        def seek(self, *args):
            """提供固定大小，不执行磁盘操作。"""

            return 16 if args == (0, 2) else 0

    with Session(connection, join_transaction_mode="create_savepoint") as session:
        kb = KnowledgeBase(name=f"预置隔离-{uuid4()}")
        session.add(kb)
        session.flush()
        upload = UploadFile(filename="预置元数据.txt", file=StreamStub())
        # 使用已经存在的系统临时目录，不 mkdir 原件/模型目录；write_upload 已 Stub。
        import tempfile
        settings = get_settings().model_copy(update={"upload_dir": Path(tempfile.gettempdir())})
        saved = uploads.store_document(session, kb.kb_id, upload, settings,
                                       source_type="preset", expected_file_hash=digest)
        try:
            row = session.get(Document, saved.doc_id)
            candidate = PresetFile(Path("未读取.txt"), "未读取.txt", "txt", digest, frozenset())
            repeated = imports.existing_item(row, candidate)
            assert repeated.doc_id == saved.doc_id and row.source_type == "preset"
            assert session.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.kb_id)) == 1
            assert row.status == "UPLOADED" and row.chunk_task_id is None
            assert row.embedding_task_id is None and row.index_task_id is None
        finally:
            session.rollback()


def test_changed_preflight_hash_rejected_before_any_insert(migrated_connection, monkeypatch):
    """预检后原件改变不能落入错误 Hash/缓存身份，拒绝前不写文件或 Document。"""

    connection, _, _ = migrated_connection

    def changed(upload, max_bytes):
        """模拟与预检不同的摘要，不生成真实原件。"""

        return "b" * 64

    def forbidden(*args):
        """变化后不得进入任何写盘步骤。"""

        pytest.fail("预检 Hash 改变不应写盘")

    monkeypatch.setattr(uploads, "hash_upload", changed)
    monkeypatch.setattr(uploads, "write_upload", forbidden)
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        before = session.scalar(select(func.count()).select_from(Document))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            uploads.store_document(session, uuid4(), UploadFile(filename="未读取.txt", file=SimpleNamespace()),
                                   get_settings(), source_type="preset", expected_file_hash="a" * 64)
        assert error.value.status_code == 409
        assert session.scalar(select(func.count()).select_from(Document)) == before
        session.rollback()
