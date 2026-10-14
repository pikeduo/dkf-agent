"""用户运行的存储清理单元用例；只写临时哨兵/缓存元数据，不生成或解析文档素材。"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import PROJECT_ROOT, Settings
from app.models import Document
from app.services import document_cleanup as tool
from app.services.document_management import ManagementError, prepare_management
from app.services.parse_cache import ParseCache


class SessionStub:
    """只有共享引用和Job标识的Stub，无数据库或云端服务。"""

    def __init__(self, *, shared=False, job_ids=()):
        """保存是否存在另一文档的相同Hash引用。"""
        self.shared, self.job_ids = shared, job_ids

    def scalar(self, statement):
        """整份缓存引用查询返回安全UUID或空。"""
        return uuid4() if self.shared else None

    def scalars(self, statement):
        """返回当前文档的Job标识，不执行SQL。"""
        return list(self.job_ids)

    def flush(self):
        """允许纯元数据准备检查点，不执行真实提交。"""
        return None


@pytest.fixture
def context(tmp_path):
    """用户运行时建立临时存储哨兵；不使用真实preset、下载目录或模型文件。"""
    settings = Settings(_env_file=None, deepseek_provider=None, llm_provider="deepseek",
        upload_dir=tmp_path / "upload", parsed_dir=tmp_path / "parsed", embeddings_dir=tmp_path / "vectors")
    identifier = uuid4()
    document = Document(doc_id=identifier, kb_id=uuid4(), file_name="existing.txt", file_type="txt",
        file_path=str(settings.upload_dir / f"{identifier}.txt"), file_hash="a" * 64, status="READY",
        processing_task_id=uuid4(), management_task_id=None, management_config=None,
        chunk_task_id=None, chunk_config=None, embedding_task_id=None, embedding_config=None,
        index_task_id=None, index_config=None)
    return document, settings


def sentinel(path):
    """写存储哨兵字节，不模拟文档正文、OCR或向量结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"storage-sentinel")


def test_delete_removes_only_owned_paths_jobs_and_unshared_whole_cache(context):
    """删除一个doc_id的原件/旧代次/Hybrid/Job指针，不删除其他文档或全局固定锁。"""
    document, settings = context
    roots = tool.cleanup_roots(document, settings)
    job_id = uuid4()
    source, parsed, vectors = Path(document.file_path), settings.parsed_dir / str(document.doc_id), settings.embeddings_dir / str(document.doc_id)
    for path in (source, parsed / "archive" / "sentinel", vectors / "generation" / "sentinel",
                 settings.upload_dir / ".hybrid" / str(document.doc_id) / "sentinel",
                 settings.parsed_dir / ".jobs" / f"{job_id}.json"):
        sentinel(path)
    other = settings.embeddings_dir / str(uuid4()) / "sentinel"
    sentinel(other)
    record = ParseCache(settings).save({"file_hash": document.file_hash, "route_strategy": "native"},
                                       {"normalized_document.json": b"cache-sentinel"})
    shared_visual = ParseCache(settings).save({"visual_content_hash": "b" * 64, "route_strategy": "hybrid_visual"},
                                             {"hybrid/normalized_visual_blocks.json": b"visual-sentinel"})
    result = tool.cleanup_owned_files(SessionStub(job_ids=[job_id]), document, roots)
    assert not source.exists() and not parsed.exists() and not vectors.exists()
    assert not (settings.parsed_dir / ".cache" / record.key).exists()
    assert (settings.parsed_dir / ".cache" / shared_visual.key).exists()
    assert other.read_bytes() == b"storage-sentinel" and (settings.parsed_dir / ".cache-cleanup.lock").exists()
    assert result["retained_shared_cache_count"] == 0 and result["shared_visual_cache_retained"] is True
    assert tool.cleanup_owned_files(SessionStub(job_ids=[job_id]), document, roots)["removed_path_count"] == 0


def test_whole_cache_referenced_by_other_document_is_preserved(context):
    """跨知识库的相同原件仍可复用缓存，删除单文档不得损坏另一份Reindex。"""
    document, settings = context
    roots = tool.cleanup_roots(document, settings)
    record = ParseCache(settings).save({"file_hash": document.file_hash, "route_strategy": "native"},
                                       {"normalized_document.json": b"cache-sentinel"})
    result = tool.cleanup_owned_files(SessionStub(shared=True), document, roots)
    assert result["retained_shared_cache_count"] == 1
    assert ParseCache(settings).load(record.recipe) is not None


@pytest.mark.parametrize("kind", ["root", "outside", "wrong_filename", "preset_original"])
def test_unsafe_paths_and_nonowned_sources_are_rejected(context, tmp_path, kind):
    """不能以根目录、越界路径或用户原始素材为清理目标，也不靠扩展名猜所有权。"""
    document, settings = context
    if kind == "root":
        with pytest.raises(ManagementError, match="DELETE_PATH"):
            tool.ensure_safe(settings.parsed_dir, settings.parsed_dir)
    elif kind == "outside":
        with pytest.raises(ManagementError, match="DELETE_PATH"):
            tool.ensure_safe(settings.parsed_dir, tmp_path / "outside")
    else:
        document.file_path = str(tmp_path / ("wrong.txt" if kind == "wrong_filename" else "preset/original.txt"))
        with pytest.raises(ManagementError, match="DELETE_PATH"):
            tool.cleanup_roots(document, settings)


def test_preset_model_and_source_directories_are_protected_even_with_uuid_filename(context):
    """正式素材/模型/源码中的UUID命名文件仍不是服务上传副本，必须拒绝删除。"""
    document, settings = context
    for directory in ("data/knowledge_base/preset", "models", "docs/test", "app", "scripts", "logs"):
        document.file_path = str(PROJECT_ROOT / directory / f"{document.doc_id}.txt")
        with pytest.raises(ManagementError, match="DELETE_PATH"):
            tool.cleanup_roots(document, settings)


def test_symbolic_link_never_traversed_or_deleted(context, tmp_path):
    """有符号链接时全批拒绝；Windows没有创建链接权限可跳过，此项不冒充通过。"""
    document, settings = context
    owned = settings.parsed_dir / str(document.doc_id)
    external = tmp_path / "external"
    sentinel(external / "user-data")
    owned.mkdir(parents=True)
    try:
        (owned / "link").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("用户环境无创建符号链接权限")
    with pytest.raises(ManagementError, match="DELETE_PATH"):
        tool.inventory(settings.parsed_dir, owned)
    assert (external / "user-data").read_bytes() == b"storage-sentinel"


def test_cleanup_failure_is_reported_and_does_not_claim_deleted(context, monkeypatch):
    """权限/I/O失败有安全类别，可恢复删除；异常不泄漏私有路径。"""
    document, settings = context
    roots = tool.cleanup_roots(document, settings)
    sentinel(Path(document.file_path))

    def failure(root, entries):
        """模拟文件被其他进程占用，不清理真实资料。"""
        raise PermissionError("PRIVATE_PATH PRIVATE_KEY")

    monkeypatch.setattr(tool, "erase", failure)
    with pytest.raises(ManagementError, match="DELETE_STORAGE") as error:
        tool.cleanup_owned_files(SessionStub(), document, roots)
    assert "PRIVATE" not in str(error.value) and Path(document.file_path).exists()


def test_delete_freezes_previous_gpu_root_before_invalidating_checkpoint(context, monkeypatch):
    """环境输出目录改动后仍能清理上一代GPU根，不能清空配置后才收集旧目录。"""
    from app.services import document_management
    document, settings = context
    previous_root = settings.embeddings_dir.parent / "old-vectors"
    document.embedding_task_id = uuid4()
    document.embedding_config = {"output_dir": str(previous_root)}
    monkeypatch.setattr(document_management, "active_job", lambda session, document: None)
    prepare_management(SessionStub(), document, settings, "delete")
    assert str(previous_root) in document.management_config["cleanup_roots"]["embedding_roots"]
    assert document.status == "FAILED" and document.embedding_task_id is None
