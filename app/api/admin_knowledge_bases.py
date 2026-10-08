"""管理员知识库创建及查询入口；当前只供本机或可信内网使用，不提供复杂 RBAC。"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.models import Document, KnowledgeBase
from app.schemas.knowledge_base import (
    DocumentResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    PageResponse,
)
from app.services.document_dispatch import publish_document
from app.services.document_processing import transition_document
from app.services.document_upload import store_document

router = APIRouter(
    prefix="/api/admin/knowledge-bases",
    tags=["admin-knowledge-bases"],
    responses={503: {"description": "数据库配置、连接或迁移不可用"}},
)


def get_admin_session() -> Iterator[Session]:
    """复用独立数据库会话，将配置、连接及 SQL 失败转为不含凭据的 503 响应。"""

    try:
        # 异常会沿 yield 传回会话上下文；关闭会话时回滚未提交事务。
        yield from get_session()
    except (SQLAlchemyError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="知识库数据库不可用，请检查数据库连接与迁移状态"
        ) from exc


AdminSession = Annotated[Session, Depends(get_admin_session)]
PageLimit = Annotated[int, Query(ge=1, le=100, description="每页条数，最多 100")]
PageOffset = Annotated[int, Query(ge=0, description="跳过的记录数")]


def require_knowledge_base(session: Session, kb_id: UUID) -> KnowledgeBase:
    """按标识读取知识库，不存在时返回 404，区分空文档列表与无效知识库。"""

    kb = session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


@router.post(
    "",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
    responses={409: {"description": "知识库名称已存在"}},
)
def create_knowledge_base(
    payload: KnowledgeBaseCreate, session: AdminSession
) -> KnowledgeBaseResponse:
    """提交新知识库并返回详情，名称唯一冲突时回滚并返回 409，不预先创建文档。"""

    kb = KnowledgeBase(
        name=payload.name, description=payload.description, status="ACTIVE"
    )
    session.add(kb)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # 由数据库唯一约束裁决并发请求；其他完整性错误不得误报为名称重复。
        if (
            getattr(exc.orig, "sqlstate", None) == "23505"
            and getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "uq_knowledge_bases_name"
        ):
            raise HTTPException(status_code=409, detail="知识库名称已存在") from exc
        raise
    session.refresh(kb)
    return KnowledgeBaseResponse.model_validate(kb)


@router.get(
    "", response_model=PageResponse[KnowledgeBaseResponse], summary="查询知识库列表"
)
def list_knowledge_bases(
    session: AdminSession, limit: PageLimit = 20, offset: PageOffset = 0
) -> PageResponse[KnowledgeBaseResponse]:
    """分页读取全部知识库，按创建时间及标识倒序排列，避免同一时间记录排序不稳定。"""

    total = session.scalar(select(func.count()).select_from(KnowledgeBase))
    rows = session.scalars(
        select(KnowledgeBase)
        .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.kb_id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return PageResponse(
        items=[KnowledgeBaseResponse.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{kb_id}",
    response_model=KnowledgeBaseResponse,
    summary="查询知识库详情",
    responses={404: {"description": "知识库不存在"}},
)
def get_knowledge_base(kb_id: UUID, session: AdminSession) -> KnowledgeBaseResponse:
    """返回指定知识库的详情，非法 UUID 由请求校验拒绝，不存在的标识返回 404。"""

    return KnowledgeBaseResponse.model_validate(require_knowledge_base(session, kb_id))


@router.get(
    "/{kb_id}/documents",
    response_model=PageResponse[DocumentResponse],
    summary="查询知识库文档列表",
    responses={404: {"description": "知识库不存在"}},
)
def list_knowledge_base_documents(
    kb_id: UUID, session: AdminSession, limit: PageLimit = 20, offset: PageOffset = 0
) -> PageResponse[DocumentResponse]:
    """验证知识库存在后分页读取其文档，计数和列表均按 kb_id 隔离。"""

    require_knowledge_base(session, kb_id)
    total = session.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb_id)
    )
    rows = session.scalars(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc(), Document.doc_id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return PageResponse(
        items=[DocumentResponse.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{kb_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传文档并提交异步处理任务",
    responses={
        400: {"description": "文件名无效或文件为空"},
        404: {"description": "知识库不存在"},
        409: {"description": "同一知识库已存在相同内容，detail 包含已有 doc_id"},
        413: {"description": "单文件大小超限"},
        415: {"description": "格式不支持或内容标识不匹配"},
        507: {"description": "文件存储不可用"},
    },
)
def upload_knowledge_base_document(
    kb_id: UUID,
    session: AdminSession,
    file: Annotated[UploadFile, File(description="单个 PDF、DOCX、TXT、MD 或图片文件")],
) -> DocumentResponse:
    """文档与原件提交成功后才投递 Celery，投递失败保留原件并返回持久化失败状态。"""

    try:
        require_knowledge_base(session, kb_id)
        response = store_document(session, kb_id, file, get_settings())
        if not publish_document(session, response.doc_id, response.task_id):
            session.expire_all()
            document = session.get(Document, response.doc_id)
            return DocumentResponse.model_validate(document)
        return response
    finally:
        file.file.close()


def require_document(
    session: Session, kb_id: UUID, doc_id: UUID, lock: bool = False
) -> Document:
    """按知识库和文档双标识查询，必要时加行锁，阻止跨库访问及并发重投覆盖。"""

    query = select(Document).where(Document.doc_id == doc_id, Document.kb_id == kb_id)
    if lock:
        query = query.with_for_update()
    document = session.scalar(query)
    if document is None:
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    return document


@router.get(
    "/{kb_id}/documents/{doc_id}",
    response_model=DocumentResponse,
    summary="查询文档状态与当前任务标识",
    responses={404: {"description": "文档不存在"}},
)
def get_document(kb_id: UUID, doc_id: UUID, session: AdminSession) -> DocumentResponse:
    """返回文档状态、失败原因和任务标识，不依赖 Redis 获取数据库中的持久化状态。"""

    return DocumentResponse.model_validate(require_document(session, kb_id, doc_id))


@router.post(
    "/{kb_id}/documents/{doc_id}/process",
    response_model=DocumentResponse,
    status_code=202,
    summary="重新投递待处理或失败文档",
    responses={
        404: {"description": "文档不存在"},
        409: {"description": "已进入处理阶段或已经完成"},
    },
)
def resubmit_document(
    kb_id: UUID, doc_id: UUID, session: AdminSession
) -> DocumentResponse:
    """为待处理或失败文档分配新任务标识并提交后投递，旧消息因标识不符自动跳过。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    if document.status not in {"UPLOADED", "FAILED"}:
        raise HTTPException(
            status_code=409, detail="文档已进入处理阶段或已经完成，不重复投递"
        )
    transition_document(document, "UPLOADED")
    document.processing_task_id = uuid4()
    session.flush()
    response = DocumentResponse.model_validate(document)
    session.commit()
    if not publish_document(session, response.doc_id, response.task_id):
        raise HTTPException(
            status_code=503, detail="任务投递失败，文件已保留，请查询文档状态后重试"
        )
    return response
