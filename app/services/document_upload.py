"""单文档上传与本地存储，不解析正文，不触发异步任务。"""

import hashlib
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings
from app.core.file_formats import FILE_TYPES, FileFormatError, validate_file_content
from app.models import Document
from app.schemas.knowledge_base import DocumentResponse

logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {f".{file_type}" for file_type in FILE_TYPES}
READ_CHUNK_SIZE = 64 * 1024


def validate_filename(filename: str | None) -> tuple[str, str]:
    """保留安全的原始文件名并返回小写扩展名，拒绝路径、控制字符和未支持的格式。"""

    if (
        not filename
        or not filename.strip()
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise HTTPException(
            status_code=400, detail="文件名不能为空、超过 255 字或包含路径及控制字符"
        )
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="仅支持 PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、CSV、TXT、MD、JPG、JPEG、PNG",
        )
    return filename, extension


def hash_upload(upload: UploadFile, max_bytes: int) -> str:
    """分块读取实际文件内容并计算 SHA256，拒绝空文件或超限文件，不信任声明的大小。"""

    upload.file.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := upload.file.read(READ_CHUNK_SIZE):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail="文件超过配置的单文件上传大小上限"
            )
        digest.update(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="不能上传空文件")
    upload.file.seek(0)
    return digest.hexdigest()


def validate_content(upload: UploadFile, extension: str) -> None:
    """复用统一内容校验区分 OOXML/OLE 类型；文本编码和正文可读性由 Parser 判断。"""

    try:
        validate_file_content(upload.file, extension[1:])
    except FileFormatError:
        raise HTTPException(
            status_code=415, detail="文件内容与扩展名不匹配或格式损坏"
        ) from None


def remove_owned_file(path: Path) -> None:
    """只清理本次请求创建的明确文件，清理失败时记录人工核对提示，不删除目录。"""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.error("上传文件清理失败，需人工核对文件与记录：%s", path.name)
        log_event(
            "upload.cleanup_failed",
            level=logging.ERROR,
            doc_id=path.stem,
            error_code="FILE_IO",
        )


def write_upload(upload: UploadFile, path: Path) -> None:
    """独占创建 UUID 文件并分块写入，遇到写入失败清理半成品，绝不覆盖已有文件。"""

    created = False
    try:
        with path.open("xb") as target:
            created = True
            upload.file.seek(0)
            while chunk := upload.file.read(READ_CHUNK_SIZE):
                target.write(chunk)
    except BaseException:
        if created:
            remove_owned_file(path)
        raise


def reject_duplicate(session: Session, kb_id: UUID, file_hash: str) -> None:
    """按知识库及内容哈希检查重复，返回已有文档标识，不覆盖原记录或原文件。"""

    existing = session.scalar(
        select(Document).where(Document.kb_id == kb_id, Document.file_hash == file_hash)
    )
    if existing is not None:
        log_event(
            "upload.duplicate", kb_id=kb_id, doc_id=existing.doc_id, status_code=409
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该知识库已存在相同内容的文档",
                "doc_id": str(existing.doc_id),
                "kb_id": str(kb_id),
            },
        )


def store_document(
    session: Session, kb_id: UUID, upload: UploadFile, settings: Settings,
    *, source_type: Literal["uploaded", "preset"] = "uploaded",
    expected_file_hash: str | None = None,
) -> DocumentResponse:
    """校验、去重并独立提交原件；预置导入可指定来源及预检 Hash，HTTP 默认行为不变。

    预检后内容变化时在任何写盘/落库前拒绝；正常失败补偿删除本次文件。
    """

    log_event("upload.received", kb_id=kb_id, file_name=upload.filename)
    if source_type not in {"uploaded", "preset"}:
        log_event("upload.rejected", kb_id=kb_id, status_code=400)
        raise HTTPException(status_code=400, detail="文档来源类型无效")
    try:
        filename, extension = validate_filename(upload.filename)
    except HTTPException as exc:
        log_event("upload.rejected", kb_id=kb_id, status_code=exc.status_code)
        raise
    file_path = None
    doc_id = None
    committed = False
    commit_started = False
    try:
        file_hash = hash_upload(upload, settings.max_upload_size_mb * 1024 * 1024)
        if expected_file_hash is not None and file_hash != expected_file_hash:
            raise HTTPException(status_code=409, detail="预检后的文件内容已变化，请重新预检")
        size_bytes = upload.file.seek(0, 2)
        upload.file.seek(0)
        validate_content(upload, extension)
        reject_duplicate(session, kb_id, file_hash)
        directory = settings.upload_dir
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        doc_id = uuid4()
        destination = directory / f"{doc_id}{extension}"
        write_upload(upload, destination)
        file_path = destination
        # 项目内使用可移植的相对路径；独立磁盘存储则保留明确的绝对路径。
        stored_path = (
            destination.relative_to(PROJECT_ROOT).as_posix()
            if destination.is_relative_to(PROJECT_ROOT)
            else str(destination)
        )
        document = Document(
            doc_id=doc_id,
            kb_id=kb_id,
            file_name=filename,
            file_type=extension[1:],
            file_hash=file_hash,
            file_path=stored_path,
            source_type=source_type,
            status="UPLOADED",
            processing_task_id=uuid4(),
        )
        session.add(document)
        # flush 先裁决唯一约束并取回时间；响应校验必须在提交前完成，避免成功后误删文件。
        session.flush()
        response = DocumentResponse.model_validate(document)
        commit_started = True
        session.commit()
        committed = True
        log_event(
            "upload.stored",
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=filename,
            file_type=extension[1:],
            size_bytes=size_bytes,
            task_id=response.task_id,
            status="UPLOADED",
        )
        return response
    except IntegrityError as exc:
        log_event(
            "upload.constraint_conflict",
            kb_id=kb_id,
            doc_id=doc_id,
            error_code="DATABASE_CONSTRAINT",
        )
        session.rollback()
        if (
            getattr(exc.orig, "sqlstate", None) == "23505"
            and getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "uq_documents_kb_id_file_hash"
        ):
            # 并发上传可能绕过前置查询，最终仍由数据库约束保证只保留一条记录。
            reject_duplicate(session, kb_id, file_hash)
        raise
    except OSError as exc:
        log_event(
            "upload.failed",
            level=logging.ERROR,
            kb_id=kb_id,
            doc_id=doc_id,
            error_code="FILE_IO",
        )
        session.rollback()
        raise HTTPException(
            status_code=507, detail="文件存储不可用，请检查磁盘空间与目录权限"
        ) from exc
    except DBAPIError as exc:
        log_event(
            "upload.failed",
            level=logging.ERROR,
            kb_id=kb_id,
            doc_id=doc_id,
            error_code="COMMIT_UNCERTAIN"
            if commit_started and exc.connection_invalidated
            else "DATABASE",
        )
        if commit_started and exc.connection_invalidated:
            # 提交确认丢失时无法断言数据库已回滚，保留文件避免误删已提交记录的原件。
            file_path = None
            logger.error(
                "上传提交结果不确定，需人工核对文件与 Document：doc_id=%s", doc_id
            )
        session.rollback()
        raise
    except HTTPException as exc:
        log_event(
            "upload.rejected",
            kb_id=kb_id,
            file_name=filename,
            status_code=exc.status_code,
        )
        session.rollback()
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        if file_path is not None and not committed:
            remove_owned_file(file_path)
