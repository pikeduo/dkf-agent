"""统一管理员文档观察、恢复与维护入口；当前仅限本机/可信内网，不提供RBAC。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import defer

from app.api.admin_knowledge_bases import AdminSession, PageLimit, PageOffset
from app.core.business_logging import log_event
from app.core.config import get_settings
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob
from app.parsers.routing.contracts import ParseMode
from app.schemas.chunk import ChunkResponse
from app.schemas.document_management import AdminParseJobResponse, BlockResponse, DocumentDetailResponse, DocumentStatusResponse, ManagementResponse
from app.schemas.knowledge_base import PageResponse
from app.services.document_management import ManagementError, mark_failed, prepare_management, public_management, publish_management
from app.services.document_processing import source_path
from app.services.parse_cache import CacheError, ParseCache

router = APIRouter(prefix="/api/admin/documents", tags=["admin-documents"],
    responses={404: {"description": "文档不存在"}, 409: {"description": "活动任务或状态不允许"},
               503: {"description": "数据库、迁移或任务队列不可用"}})
Mode = Annotated[ParseMode, Query(description="auto默认自动路由；native/mineru为管理员显式覆盖")]


def require_document(session, doc_id: UUID, *, lock=False):
    """文档ID全局唯一；维护使用NOWAIT行锁，正在运行的Worker不会拖住删除HTTP请求。"""
    statement = select(Document).where(Document.doc_id == doc_id).execution_options(populate_existing=True)
    try:
        document = session.scalar(statement.with_for_update(nowait=True) if lock else statement)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            session.rollback()
            raise HTTPException(409, "MANAGEMENT_BUSY：文档正在由其他任务处理") from None
        raise
    if document is None:
        raise HTTPException(404, "文档不存在")
    return document


def management_view(document):
    """损坏管理检查点返回固定冲突类别，不把私有JSON或原始异常回显给管理员。"""
    try:
        return public_management(document)
    except ManagementError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{doc_id}", response_model=DocumentDetailResponse, summary="查询文档详情与各阶段任务")
def get_document(doc_id: UUID, session: AdminSession) -> DocumentDetailResponse:
    """返回原有安全文档协议及最近20个云任务，不请求Redis或供应商。"""
    document = require_document(session, doc_id)
    jobs = session.scalars(select(DocumentParseJob).where(DocumentParseJob.doc_id == doc_id)
        .order_by(DocumentParseJob.created_at.desc(), DocumentParseJob.job_id.desc()).limit(20))
    result = DocumentDetailResponse.model_validate(document)
    result.management = management_view(document)
    result.parse_jobs = [AdminParseJobResponse.model_validate(job) for job in jobs]
    return result


@router.get("/{doc_id}/status", response_model=DocumentStatusResponse, summary="查询处理链路、数量与向量完整性")
def get_document_status(doc_id: UUID, session: AdminSession) -> DocumentStatusResponse:
    """一次SQL快照读取状态及Block/Chunk/向量数量，文件存在性不冒充缓存/内容验收。"""
    require_document(session, doc_id)
    blocks = select(func.count()).select_from(DocumentBlock).where(DocumentBlock.doc_id == Document.doc_id).correlate(Document).scalar_subquery()
    invalid = or_(func.vector_dims(DocumentChunk.embedding) != 1024,
                  func.abs(func.vector_norm(DocumentChunk.embedding) - 1) > 1e-4)
    predicate = (DocumentChunk.doc_id == Document.doc_id, DocumentChunk.kb_id == Document.kb_id)
    chunks = select(func.count()).select_from(DocumentChunk).where(*predicate).correlate(Document).scalar_subquery()
    vectors = select(func.count(DocumentChunk.embedding)).where(*predicate).correlate(Document).scalar_subquery()
    bad = select(func.count()).select_from(DocumentChunk).where(*predicate, invalid).correlate(Document).scalar_subquery()
    row = session.execute(select(Document, blocks, chunks, vectors, bad).where(Document.doc_id == doc_id)
                          .execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise HTTPException(404, "文档不存在")
    document, block_count, chunk_count, vector_count, invalid_count = row
    try:
        original_present = source_path(document).is_file()
        cache_present = ParseCache(get_settings()).path(str(doc_id), "latest.json").is_file()
    except (OSError, ValueError, CacheError):
        original_present = cache_present = False
    return DocumentStatusResponse(doc_id=doc_id, kb_id=document.kb_id, status=document.status,
        error_message=document.error_message, task_id=document.processing_task_id,
        chunk_task_id=document.chunk_task_id, embedding_task_id=document.embedding_task_id,
        index_task_id=document.index_task_id, management=management_view(document),
        block_count=block_count, chunk_count=chunk_count, vector_count=vector_count,
        invalid_vector_count=invalid_count, original_present=original_present,
        parse_cache_present=cache_present, updated_at=document.updated_at,
        parse_quality=getattr(document, "parse_quality", None))


@router.get("/{doc_id}/blocks", response_model=PageResponse[BlockResponse], summary="按原阅读序号分页查询Block")
def list_document_blocks(doc_id: UUID, session: AdminSession, limit: PageLimit = 20, offset: PageOffset = 0):
    """显式按block_index再UUID稳定分页，保留真实页码/bbox，历史空序号置后不修补。"""
    require_document(session, doc_id)
    predicate = DocumentBlock.doc_id == doc_id
    total = session.scalar(select(func.count()).select_from(DocumentBlock).where(predicate))
    rows = session.scalars(select(DocumentBlock).where(predicate)
        .order_by(DocumentBlock.block_index.asc().nulls_last(), DocumentBlock.block_id).limit(limit).offset(offset))
    return PageResponse(items=[BlockResponse.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@router.get("/{doc_id}/chunks", response_model=PageResponse[ChunkResponse], summary="分页查询Chunk及来源证据")
def list_document_chunks(doc_id: UUID, session: AdminSession, limit: PageLimit = 20, offset: PageOffset = 0):
    """复用原Chunk响应，不返回向量；限定原kb/doc并按原切片序号稳定分页。"""
    document = require_document(session, doc_id)
    predicate = (DocumentChunk.doc_id == doc_id, DocumentChunk.kb_id == document.kb_id)
    total = session.scalar(select(func.count()).select_from(DocumentChunk).where(*predicate))
    rows = session.scalars(select(DocumentChunk).options(defer(DocumentChunk.embedding)).where(*predicate)
        .order_by(DocumentChunk.chunk_metadata["chunk_index"].as_integer().asc().nulls_last(), DocumentChunk.chunk_id)
        .limit(limit).offset(offset))
    return PageResponse(items=[ChunkResponse.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


def submit_management(session, doc_id: UUID, operation: str, parse_mode="auto", *,
                      allow_new_split_parts=False, office_pdf_max_new_parts=0) -> ManagementResponse:
    """提交管理检查点后投递；API只冻结费用许可，不执行Office转换/解析或推理。"""
    document = require_document(session, doc_id, lock=True)
    try:
        options = {"allow_new_split_parts": True} if allow_new_split_parts else {}
        if office_pdf_max_new_parts:
            options["office_pdf_max_new_parts"] = office_pdf_max_new_parts
        task_id, dispatch = prepare_management(session, document, get_settings(), operation, parse_mode=parse_mode, **options)
        result = public_management(document)
        session.commit()
    except ManagementError as exc:
        session.rollback()
        log_event("management.rejected", doc_id=doc_id, operation=operation, error_code=exc.code)
        raise HTTPException(409, str(exc)) from None
    log_event("management.requested", doc_id=doc_id, task_id=task_id, operation=operation, parse_mode=parse_mode)
    if dispatch and not publish_management(doc_id, task_id):
        with session.begin():
            mark_failed(session, doc_id, task_id, "MANAGEMENT_DISPATCH")
        raise HTTPException(503, str(ManagementError("MANAGEMENT_DISPATCH")))
    return result


@router.post("/{doc_id}/retry", response_model=ManagementResponse, status_code=202,
             summary="恢复失败阶段或原批次；仅显式许可才续交从未提交的PDF片")
def retry_document(doc_id: UUID, session: AdminSession, parse_mode: Mode = "auto",
    allow_new_split_parts: Annotated[bool, Query(description="显式确认剩余从未提交PDF分片的云外传及新增费用；不重投已有Batch")] = False,
    office_pdf_max_new_parts: Annotated[int, Query(ge=0, le=128, description="默认0不授权；正数确认失败Office转PDF后的新分片外传及最多收费文件数，不重投原Batch")] = 0):
    """默认本地恢复；Office改用新PDF输入须单独给出最大新增片数，不执行Force。"""
    options = {"allow_new_split_parts": True} if allow_new_split_parts else {}
    if office_pdf_max_new_parts:
        options["office_pdf_max_new_parts"] = office_pdf_max_new_parts
    return submit_management(session, doc_id, "retry", parse_mode, **options)


@router.post("/{doc_id}/reindex", response_model=ManagementResponse, status_code=202,
             summary="复用成功Parse Cache并自动重建Chunk、Embedding和索引")
def reindex_document(doc_id: UUID, session: AdminSession):
    """保留原件，只回放缓存；缓存缺失/损坏明确失败，绝不调用Parser或MinerU。"""
    return submit_management(session, doc_id, "reindex")


@router.post("/{doc_id}/reparse", response_model=ManagementResponse, status_code=202,
             summary="明确强制重解析并继续到READY；可能再次产生MinerU费用",
             responses={400: {"description": "必须显式确认force=true"}})
def reparse_document(doc_id: UUID, session: AdminSession,
    force: Annotated[bool, Query(description="必须true：绕过解析/视觉缓存并确认可能再次云收费")] = False,
    parse_mode: Mode = "auto"):
    """沿用原Force许可，不因新增管理API隐式重新外传文档或重复计费。"""
    if not force:
        raise HTTPException(400, "Reparse必须明确force=true；可能再次消耗云API额度")
    return submit_management(session, doc_id, "reparse", parse_mode)


@router.delete("/{doc_id}", response_model=ManagementResponse, status_code=202,
               summary="异步删除文档及自有原件、Block、Chunk、向量和缓存")
def delete_document(doc_id: UUID, session: AdminSession):
    """只接收安全删除请求，活动云/GPU任务409；清理失败可用Retry恢复，不假装已删除。"""
    return submit_management(session, doc_id, "delete")
