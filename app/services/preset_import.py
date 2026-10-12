"""仅由用户运行的预置批量导入与有界状态驱动，不直接解析/加载模型或请求云端。"""

import logging
from dataclasses import dataclass
from time import monotonic, sleep
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.db.seed import DEFAULT_KB_ID, ensure_default_knowledge_base
from app.db.session import get_engine
from app.models import Document, KnowledgeBase
from app.services.chunk.processing import prepare_chunk, publish_chunk
from app.services.chunk.splitter import ChunkError
from app.services.document_dispatch import publish_document
from app.services.document_upload import hash_upload, store_document
from app.services.embedding.processing import prepare_embedding, publish_embedding
from app.services.embedding.service import EmbeddingError
from app.services.indexing import IndexingError, checked_index_config, prepare_index, publish_index, require_complete_vectors
from app.services.preset_manifest import PresetFile, PresetImportError, is_link

NEXT_STEPS = {
    "CHUNKING": ("chunk_task_id", prepare_chunk, publish_chunk),
    "EMBEDDING": ("embedding_task_id", prepare_embedding, publish_embedding),
    "INDEXING": ("index_task_id", prepare_index, publish_index),
}


@dataclass
class ImportItem:
    """导入进程的安全结果摘要，不携带正文、文件路径、上游原始错误或云签名地址。"""

    file_name: str
    doc_id: UUID | None = None
    status: str = "PENDING"
    error_code: str | None = None


def require_target(session: Session, kb_id: UUID) -> None:
    """默认库复用现有幂等 Seed；显式库必须已存在且 ACTIVE，不创建第二个默认库。"""

    kb = ensure_default_knowledge_base(session) if kb_id == DEFAULT_KB_ID else session.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "ACTIVE":
        raise PresetImportError("PRESET_STATE")


def existing_item(document: Document, file: PresetFile) -> ImportItem:
    """重复内容沿用原 doc_id，不改变名称/来源/状态，不重发解析或重新收费。"""

    if document.source_type != "preset" or document.file_type != file.file_type:
        error = PresetImportError("PRESET_SOURCE_CONFLICT")
        error.doc_id = document.doc_id
        raise error
    log_event("preset.reused", doc_id=document.doc_id, kb_id=document.kb_id,
              file_name=file.path.name, status=document.status)
    return ImportItem(file.path.name, document.doc_id, document.status)


def import_file(session: Session, kb_id: UUID, file: PresetFile, settings: Settings) -> ImportItem:
    """复用单文件存储后投递 auto；提交确认丢失不自动重复导入，活动/失败记录不重投。"""

    saved = None
    try:
        if is_link(file.path) or file.path.resolve() != file.path:
            raise PresetImportError("PRESET_CHANGED")
        with file.path.open("rb") as stream:
            upload = UploadFile(file=stream, filename=file.path.name)
            if hash_upload(upload, settings.max_upload_size_mb * 1024 * 1024) != file.file_hash:
                raise PresetImportError("PRESET_CHANGED")
            existing = session.scalar(select(Document).where(Document.kb_id == kb_id,
                                                             Document.file_hash == file.file_hash))
            if existing is not None:
                return existing_item(existing, file)
            # 原件写入独立 UUID 存储目录，保留人工维护的 preset 输入目录不变。
            saved = store_document(session, kb_id, upload, settings, source_type="preset",
                                   expected_file_hash=file.file_hash)
        if not publish_document(session, saved.doc_id, saved.task_id, parse_mode="auto"):
            raise PresetImportError("PRESET_DISPATCH")
        log_event("preset.imported", kb_id=kb_id, doc_id=saved.doc_id, task_id=saved.task_id,
                  file_name=file.path.name, file_type=file.file_type, status="UPLOADED")
        return ImportItem(file.path.name, saved.doc_id, "UPLOADED")
    except HTTPException as exc:
        session.rollback()
        # 并发导入由既有唯一约束裁决，只重读同 Hash，不重发消息或覆盖既有记录。
        if exc.status_code == 409 and isinstance(exc.detail, dict) and exc.detail.get("doc_id"):
            existing = session.scalar(select(Document).where(Document.kb_id == kb_id,
                                                             Document.file_hash == file.file_hash))
            if existing is not None:
                return existing_item(existing, file)
        raise PresetImportError("PRESET_FILE") from None
    except PresetImportError as exc:
        if saved is not None:
            # 标识随异常返回，便于投递确认不确定后人工核对，不能删除已提交原件。
            exc.doc_id = saved.doc_id
        raise
    except (SQLAlchemyError, OSError) as exc:
        error = PresetImportError("PRESET_DATABASE" if isinstance(exc, SQLAlchemyError) else "PRESET_FILE")
        error.doc_id = saved.doc_id if saved else None
        raise error from None
    finally:
        session.rollback()


def advance_document(session: Session, kb_id: UUID, doc_id: UUID, settings: Settings) -> str:
    """仅为未建下一阶段检查点的成功文档提交一次；已有消息/FAILED 从不自动重投。

    行锁下准备并提交检查点，退出事务后投递。Worker 正持锁时返回 WAITING，
    不跨过未完成的阶段，不在脚本进程执行 Parser/Embedding/Index 主体。
    """

    task_id = publisher = None
    try:
        with session.begin():
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            document = session.scalar(select(Document).where(Document.doc_id == doc_id,
                Document.kb_id == kb_id).with_for_update(skip_locked=True))
            if document is None:
                existing = session.get(Document, doc_id)
                if existing is None or existing.kb_id != kb_id:
                    raise PresetImportError("PRESET_STATE")
                return "WAITING"
            if document.source_type != "preset":
                raise PresetImportError("PRESET_SOURCE_CONFLICT")
            status = document.status
            if status == "FAILED":
                raise PresetImportError("PRESET_FAILED")
            if status == "READY":
                try:
                    config = checked_index_config(document)
                    require_complete_vectors(session, document, config["embedding_result"]["chunk_count"])
                except (IndexingError, KeyError, TypeError):
                    raise PresetImportError("PRESET_READY") from None
                return status
            if status in {"UPLOADED", "PARSING", "OCR_PROCESSING"}:
                return status
            if status not in NEXT_STEPS:
                raise PresetImportError("PRESET_STATE")
            attribute, prepare, publisher = NEXT_STEPS[status]
            if getattr(document, attribute) is not None:
                return status
            task_id, dispatch = prepare(session, document) if status == "INDEXING" else prepare(session, document, settings)
            if not dispatch:
                publisher = None
        if publisher is not None:
            if not publisher(session, doc_id, task_id):
                raise PresetImportError("PRESET_DISPATCH")
            log_event("preset.advanced", kb_id=kb_id, doc_id=doc_id, task_id=task_id, stage=status)
        return status
    except SQLAlchemyError as exc:
        session.rollback()
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03":
            return "WAITING"
        raise PresetImportError("PRESET_DATABASE") from None


def error_code(exc: Exception) -> str:
    """只输出允许的异常类别或固定阶段代码，不透传底层异常消息及敏感输入。"""

    if isinstance(exc, PresetImportError):
        return exc.code
    if isinstance(exc, SQLAlchemyError):
        return "PRESET_DATABASE"
    if isinstance(exc, (EmbeddingError, IndexingError)):
        return exc.code
    if isinstance(exc, ChunkError):
        # ChunkError 的既有契约是固定代码加中文原因，仅接受明确白名单前缀。
        code = str(exc).split("：", 1)[0]
        if code in {"CHUNK_CHECKPOINT_INVALID", "CHUNK_CONFIG_INVALID", "CHUNK_BUSY", "CHUNK_STATE",
                    "CHUNK_SOURCE_ORDER", "CHUNK_LIMIT", "CHUNK_EMPTY"}:
            return code
    return "PRESET_UNKNOWN"


def fail_item(item: ImportItem, exc: Exception, kb_id: UUID) -> None:
    """单项操作失败只记录摘要，不自行改 Document 的业务状态或回滚其他成功项。"""

    item.status, item.error_code = "ERROR", error_code(exc)
    log_event("preset.failed", level=logging.ERROR, kb_id=kb_id, doc_id=item.doc_id,
              file_name=item.file_name, error_code=item.error_code)


def wait_until_ready(items: list[ImportItem], kb_id: UUID, settings: Settings,
                     *, timeout: float = 1800, poll_interval: float = 5, session_factory=None,
                     clock=monotonic, pause=sleep) -> None:
    """用户 CLI 的有界数据库轮询，不查 MinerU API；超时不取消后台任务，不强制重新解析。

    每轮使用独立短会话；注入时钟/暂停/会话仅供用户执行 Stub 用例。
    """

    if not 0 < timeout <= 86400 or not 1 <= poll_interval <= 60:
        raise PresetImportError("PRESET_STATE")

    def default_session():
        """仅在用户执行等待步骤时创建短会话，不在函数定义/模块导入时连接数据库。"""

        return Session(get_engine())

    session_factory = session_factory or default_session
    deadline = clock() + timeout
    while any(item.doc_id and item.status not in {"READY", "ERROR"} for item in items):
        if clock() >= deadline:
            for item in items:
                if item.doc_id and item.status not in {"READY", "ERROR"}:
                    item.error_code = "PRESET_TIMEOUT"
                    log_event("preset.timeout", kb_id=kb_id, doc_id=item.doc_id, status=item.status,
                              error_code=item.error_code)
            return
        for item in items:
            if item.doc_id is None or item.status in {"READY", "ERROR"}:
                continue
            try:
                with session_factory() as session:
                    status = advance_document(session, kb_id, item.doc_id, settings)
                if status != "WAITING" and status != item.status:
                    item.status = status
                    log_event("preset.state", kb_id=kb_id, doc_id=item.doc_id, status=status)
            except Exception as exc:
                fail_item(item, exc, kb_id)
        if all(item.status in {"READY", "ERROR"} for item in items):
            return
        pause(min(poll_interval, max(0, deadline - clock())))


def run_import(files: list[PresetFile], settings: Settings, kb_id: UUID = DEFAULT_KB_ID,
               *, timeout: float = 1800, poll_interval: float = 5) -> list[ImportItem]:
    """只在用户显式执行后连接已有服务，逐文件独立提交，再通过既有任务链推进 READY。"""

    if not files or not 0 < timeout <= 86400 or not 1 <= poll_interval <= 60:
        raise PresetImportError("PRESET_STATE")
    log_event("preset.started", kb_id=kb_id, file_count=len(files), parse_mode="auto")
    try:
        engine = get_engine()
        with Session(engine) as session, session.begin():
            require_target(session, kb_id)
    except Exception as exc:
        code = error_code(exc)
        log_event("preset.failed", level=logging.ERROR, kb_id=kb_id, stage="target", error_code=code)
        raise PresetImportError(code) from None
    items = []
    for file in files:
        item = ImportItem(file.path.name)
        try:
            with Session(engine) as session:
                item = import_file(session, kb_id, file, settings)
        except Exception as exc:
            item.doc_id = getattr(exc, "doc_id", None)
            fail_item(item, exc, kb_id)
        items.append(item)
    # 所有已有 READY 也必须通过数据库全量校验，不能只凭重复记录状态报成功。
    for item in items:
        if item.status == "READY":
            item.status = "PENDING"
    wait_until_ready(items, kb_id, settings, timeout=timeout, poll_interval=poll_interval)
    ready = sum(item.status == "READY" and item.error_code is None for item in items)
    log_event("preset.finished", kb_id=kb_id, file_count=len(items), accepted_count=ready,
              failed_count=len(items) - ready, status="READY" if ready == len(items) else "INCOMPLETE")
    return items
