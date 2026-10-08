"""单文档上传与本地存储，不解析正文，不触发异步任务。"""

import hashlib
import logging
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, Settings
from app.models import Document
from app.schemas.knowledge_base import DocumentResponse

logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png"}
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
            status_code=415, detail="仅支持 PDF、DOCX、TXT、MD、JPG、JPEG、PNG"
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
    """校验二进制格式的基本标识及 DOCX 容器结构，不解压、不解析正文或限制文本编码。"""

    upload.file.seek(0)
    header = upload.file.read(1024)
    valid = True
    if extension == ".pdf":
        valid = b"%PDF-" in header
    elif extension == ".png":
        valid = header.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = header.startswith(b"\xff\xd8\xff")
    elif extension == ".docx":
        try:
            upload.file.seek(0)
            with ZipFile(upload.file) as archive:
                valid = {"[Content_Types].xml", "word/document.xml"}.issubset(
                    archive.namelist()
                )
        except (BadZipFile, UnicodeDecodeError, ValueError):
            valid = False
    upload.file.seek(0)
    if not valid:
        raise HTTPException(status_code=415, detail="文件内容与扩展名不匹配或格式损坏")


def remove_owned_file(path: Path) -> None:
    """只清理本次请求创建的明确文件，清理失败时记录人工核对提示，不删除目录。"""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.error("上传文件清理失败，需人工核对文件与记录：%s", path.name)


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
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该知识库已存在相同内容的文档",
                "doc_id": str(existing.doc_id),
                "kb_id": str(kb_id),
            },
        )


def store_document(
    session: Session, kb_id: UUID, upload: UploadFile, settings: Settings
) -> DocumentResponse:
    """校验、去重、保存文件并提交 UPLOADED 记录，正常失败时补偿删除本次文件。"""

    filename, extension = validate_filename(upload.filename)
    file_path = None
    committed = False
    commit_started = False
    try:
        file_hash = hash_upload(upload, settings.max_upload_size_mb * 1024 * 1024)
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
            source_type="uploaded",
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
        return response
    except IntegrityError as exc:
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
        session.rollback()
        raise HTTPException(
            status_code=507, detail="文件存储不可用，请检查磁盘空间与目录权限"
        ) from exc
    except DBAPIError as exc:
        if commit_started and exc.connection_invalidated:
            # 提交确认丢失时无法断言数据库已回滚，保留文件避免误删已提交记录的原件。
            file_path = None
            logger.error(
                "上传提交结果不确定，需人工核对文件与 Document：doc_id=%s", doc_id
            )
        session.rollback()
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        if file_path is not None and not committed:
            remove_owned_file(file_path)
