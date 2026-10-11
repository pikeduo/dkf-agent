"""复用单文件存储与调度，逐文件提交批量请求，不构建新的解析路由。"""

import logging
from collections.abc import Callable
from time import perf_counter
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.core.config import Settings
from app.schemas.batch_upload import BatchUploadItem, BatchUploadResponse
from app.schemas.knowledge_base import DocumentResponse
from app.schemas.parse_job import ParseJobResponse
from app.services.document_upload import store_document, validate_filename

MAX_BATCH_FILES = 50
Dispatch = Callable[[DocumentResponse], DocumentResponse | ParseJobResponse]


def validate_batch(files: list[UploadFile]) -> None:
    """在任何写盘、提交或投递前检查批次数量；不替代网关请求体大小限制。"""

    if not 1 <= len(files) <= MAX_BATCH_FILES:
        log_event("upload.batch_rejected", file_count=len(files), status_code=413)
        raise HTTPException(status_code=413, detail="每批必须包含 1～50 个文件")


def store_batch(
    session: Session,
    kb_id: UUID,
    files: list[UploadFile],
    settings: Settings,
    dispatch: Dispatch,
    *,
    allowed_types: set[str] | frozenset[str] | None = None,
) -> BatchUploadResponse:
    """逐文件复用校验、去重和独立提交，再调度任务；单项失败回滚当前事务后继续。

    上传和消息投递仍非原子操作。失败项带已知 doc_id，提交确认丢失时可能无法
    返回标识；客户端必须先查文档列表。调用入口负责知识库、配置预检及关闭文件。
    """

    validate_batch(files)
    start = perf_counter()
    log_event("upload.batch_started", kb_id=kb_id, file_count=len(files))
    items = []
    for index, upload in enumerate(files, start=1):
        item_start = perf_counter()
        saved = None
        try:
            if allowed_types is not None:
                _, extension = validate_filename(upload.filename)
                if extension[1:] not in allowed_types:
                    raise HTTPException(
                        status_code=415,
                        detail="MinerU 仅接受 PDF、图片与 DOC/DOCX/PPT/PPTX/XLS/XLSX",
                    )
            # store_document 每次自行提交；后续文件回滚不能撤销之前已提交的原件。
            saved = store_document(session, kb_id, upload, settings)
            result = dispatch(saved)
            if isinstance(result, ParseJobResponse):
                item = BatchUploadItem(
                    index=index,
                    file_name=upload.filename,
                    status_code=202,
                    doc_id=saved.doc_id,
                    parse_job=result,
                )
            else:
                failed = result.status == "FAILED"
                item = BatchUploadItem(
                    index=index,
                    file_name=upload.filename,
                    status_code=503 if failed else 201,
                    doc_id=saved.doc_id,
                    document=result,
                    detail=result.error_message if failed else None,
                )
        except HTTPException as exc:
            session.rollback()
            detail_id = (
                exc.detail.get("doc_id") if isinstance(exc.detail, dict) else None
            )
            item = BatchUploadItem(
                index=index,
                file_name=upload.filename,
                status_code=exc.status_code,
                doc_id=saved.doc_id if saved else detail_id,
                detail=exc.detail,
            )
        except SQLAlchemyError:
            session.rollback()
            item = BatchUploadItem(
                index=index,
                file_name=upload.filename,
                status_code=503,
                doc_id=saved.doc_id if saved else None,
                detail="数据库不可用或提交结果不确定，请先查询文档列表再重试",
            )
        except Exception:  # noqa: BLE001
            # 不透传意外异常原文；保留已提交原件，不能因批次失败误删其他文件。
            session.rollback()
            item = BatchUploadItem(
                index=index,
                file_name=upload.filename,
                status_code=500,
                doc_id=saved.doc_id if saved else None,
                detail="文件处理异常，请先查询文档列表和安全日志再重试",
            )
        items.append(item)
        log_event(
            "upload.batch_item",
            level=logging.ERROR
            if item.status_code >= 500
            else logging.WARNING
            if item.status_code >= 400
            else logging.INFO,
            kb_id=kb_id,
            doc_id=item.doc_id,
            file_name=upload.filename,
            file_index=index,
            status_code=item.status_code,
            error_code=f"ITEM_HTTP_{item.status_code}"
            if item.status_code >= 400
            else None,
            duration_ms=round((perf_counter() - item_start) * 1000, 2),
        )
    accepted = sum(item.status_code < 400 for item in items)
    log_event(
        "upload.batch_finished",
        kb_id=kb_id,
        file_count=len(items),
        accepted_count=accepted,
        failed_count=len(items) - accepted,
        duration_ms=round((perf_counter() - start) * 1000, 2),
    )
    return BatchUploadResponse(
        total=len(items),
        accepted_count=accepted,
        failed_count=len(items) - accepted,
        items=items,
    )
