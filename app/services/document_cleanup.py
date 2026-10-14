"""仅清理数据库文档拥有的文件；路径校验失败关闭，不清理用户素材/模型/其他文档。"""

import os
import stat
from pathlib import Path

from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.models import Document, DocumentParseJob
from app.services.cache_files import cache_guard
from app.services.document_processing import source_path
from app.services.parse_cache import HASH, VERSION, ReadyRecord, key_for

MAX_CLEANUP_FILES = 100000


def path_error():
    """延迟导入固定管理错误，避免清理与编排模块相互初始化。"""
    from app.services.document_management import ManagementError
    return ManagementError("DELETE_PATH")


def absolute_root(value) -> Path:
    """固定到项目根目录，不允许磁盘根、项目根或用户主目录成为存储清理根。"""
    path = Path(value)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    path = Path(os.path.abspath(path))
    protected = [PROJECT_ROOT / name for name in ("app", "scripts", "tests", "alembic", "docs", "models", "logs", ".git", ".codex", ".agents")]
    protected.append(PROJECT_ROOT / "data/knowledge_base/preset")
    if (path in {Path(path.anchor), PROJECT_ROOT, Path.home()}
            or any(path.is_relative_to(folder) for folder in protected)):
        raise path_error()
    return path


def ensure_safe(root: Path, target: Path) -> None:
    """检查绝对边界及每级符号链接/Windows junction，不能仅靠resolve丢失链接信息。"""
    root, target = absolute_root(root), Path(os.path.abspath(target))
    if target == root or not target.is_relative_to(root) or not target.resolve().is_relative_to(root.resolve()):
        raise path_error()
    for path in (target, *target.parents):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise path_error()


def cleanup_roots(document, settings) -> dict:
    """冻结原件及当前/旧GPU输出根；只接受服务生成的UUID文件名，绝不删除正式preset素材。"""
    source = Path(os.path.abspath(source_path(document)))
    expected = f"{document.doc_id}.{document.file_type}"
    if source.name != expected:
        raise path_error()
    source_root = absolute_root(source.parent)
    ensure_safe(source_root, source)
    embeddings = {str(absolute_root(settings.embeddings_dir))}
    if document.embedding_config is not None:
        output = document.embedding_config.get("output_dir")
        if not isinstance(output, str):
            raise path_error()
        embeddings.add(str(absolute_root(output)))
    roots = {"source_root": str(source_root), "source_file": expected,
             "parsed_root": str(absolute_root(settings.parsed_dir)), "embedding_roots": sorted(embeddings)}
    # 额外保护用户显式部署的外部模型与日志目录，UUID文件名本身不是删除这些资源的授权。
    protected = []
    for configured in (settings.embedding_model_dir, settings.reranker_model_dir, settings.log_dir):
        folder = Path(configured)
        protected.append(Path(os.path.abspath(folder if folder.is_absolute() else PROJECT_ROOT / folder)))
    if any(Path(root).is_relative_to(folder) for root in (roots["source_root"], roots["parsed_root"], *roots["embedding_roots"])
           for folder in protected):
        raise path_error()
    # 每个目录都只删除一个doc_id子目录，不使用根目录作递归目标。
    for root in (roots["parsed_root"], *roots["embedding_roots"]):
        ensure_safe(Path(root), Path(root) / str(document.doc_id))
    return roots


def inventory(root: Path, target: Path) -> list[Path]:
    """先完成全树预检再删除，拒绝符号链接、设备节点、越界和无界遍历。"""
    ensure_safe(root, target)
    if not target.exists():
        return []
    pending, found = [target], []
    while pending:
        path = pending.pop()
        ensure_safe(root, path)
        info = path.lstat()
        found.append(path)
        if len(found) > MAX_CLEANUP_FILES:
            raise path_error()
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as rows:
                for row in rows:
                    pending.append(Path(row.path))
                    if len(pending) + len(found) > MAX_CLEANUP_FILES:
                        raise path_error()
        elif not stat.S_ISREG(info.st_mode):
            raise path_error()
    return found


def erase(root: Path, entries: list[Path]) -> int:
    """逐项再次核对边界，深层先删；缺失可幂等恢复，权限错误交给删除检查点记录。"""
    count = 0
    for path in sorted(entries, key=lambda item: len(item.parts), reverse=True):
        ensure_safe(root, path)
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            count += 1
        except FileNotFoundError:
            pass
    return count


def cleanup_owned_files(session, document, roots: dict) -> dict:
    """删除自有文件/Job指针及无其他文档引用的整份缓存；视觉共享缓存无可靠所有权则保留。"""
    try:
        doc_id = str(document.doc_id)
        source_root, parsed_root = absolute_root(roots["source_root"]), absolute_root(roots["parsed_root"])
        if roots["source_file"] != f"{doc_id}.{document.file_type}":
            raise path_error()
        targets = [(source_root, source_root / roots["source_file"]),
                   (source_root, source_root / ".hybrid" / doc_id), (parsed_root, parsed_root / doc_id)]
        targets.extend((absolute_root(root), absolute_root(root) / doc_id) for root in roots["embedding_roots"])
        job_ids = list(session.scalars(select(DocumentParseJob.job_id).where(DocumentParseJob.doc_id == document.doc_id)))
        if len(job_ids) > MAX_CLEANUP_FILES:
            raise path_error()
        targets.extend((parsed_root, parsed_root / ".jobs" / f"{job_id}.json") for job_id in job_ids)
        for root, target in targets:
            ensure_safe(root, target)
        # 与所有标准缓存读取/发布互斥，再按当前DB重新核对其他文档引用，避免误删跨库共享结果。
        with cache_guard(parsed_root):
            shared = session.scalar(select(Document.doc_id).where(
                Document.doc_id != document.doc_id, Document.file_hash == document.file_hash).limit(1)) is not None
            retained = 0
            folder = parsed_root / ".cache"
            if folder.exists():
                ensure_safe(parsed_root, folder)
                with os.scandir(folder) as entries:
                    for index, row in enumerate(entries):
                        if index >= MAX_CLEANUP_FILES:
                            raise path_error()
                        if not HASH.fullmatch(row.name):
                            continue
                        pointer = Path(row.path) / "ready.json"
                        ensure_safe(parsed_root, pointer)
                        if not pointer.is_file() or pointer.stat().st_size > 65536:
                            continue
                        try:
                            record = ReadyRecord.model_validate_json(pointer.read_bytes())
                        except ValueError:
                            # 未知/损坏的共享身份不能据猜测删除，也不把它当成本Doc可用缓存。
                            continue
                        if (record.version != VERSION or record.key != row.name or key_for(record.recipe) != row.name
                                or record.recipe.get("file_hash") != document.file_hash):
                            continue
                        if shared:
                            retained += 1
                        else:
                            targets.append((parsed_root, Path(row.path)))
            # 必须对所有目标预检成功后才开始；运行文件清理与SQL不宣称跨系统原子。
            planned = [(root, inventory(root, target)) for root, target in targets]
            removed = sum(erase(root, entries) for root, entries in planned)
        return {"removed_path_count": removed, "retained_shared_cache_count": retained,
                "shared_visual_cache_retained": True}
    except (OSError, ValueError, TypeError, KeyError):
        from app.services.document_management import ManagementError
        raise ManagementError("DELETE_STORAGE") from None
