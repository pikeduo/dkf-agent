"""管理员知识库创建及查询入口；当前只供本机或可信内网使用，不提供复杂 RBAC。"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.file_formats import FILE_TYPES
from app.db.session import get_session
from app.models import Document, DocumentChunk, DocumentParseJob, KnowledgeBase
from app.core.business_logging import log_event
from app.parsers.routing.contracts import ParseMode
from app.schemas.batch_upload import BatchUploadResponse
from app.schemas.chunk import ChunkResponse, ChunkTaskResponse
from app.schemas.embedding import EmbeddingTaskResponse
from app.schemas.indexing import IndexTaskResponse
from app.schemas.knowledge_base import (
    DocumentResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    PageResponse,
)
from app.services.batch_upload import MAX_BATCH_FILES, store_batch, validate_batch
from app.services.document_dispatch import publish_document
from app.services.document_processing import transition_document
from app.services.document_upload import store_document
from app.services.chunk.processing import prepare_chunk, publish_chunk
from app.services.chunk.splitter import ChunkError
from app.services.embedding.processing import prepare_embedding, publish_embedding
from app.services.embedding.service import EmbeddingError
from app.services.indexing import IndexingError, prepare_index, publish_index

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
    response_model=DocumentResponse | BatchUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传一份或多份文档并逐文件提交异步处理任务",
    responses={
        200: {
            "model": BatchUploadResponse,
            "description": "多文件逐项报告；不代表全部成功",
        },
        400: {"description": "文件名无效或文件为空"},
        404: {"description": "知识库不存在"},
        409: {"description": "同一知识库已存在相同内容，detail 包含已有 doc_id"},
        413: {"description": "单文件大小超限或文件数量超过 50"},
        415: {"description": "格式不支持或内容标识不匹配"},
        507: {"description": "文件存储不可用"},
    },
)
def upload_knowledge_base_document(
    kb_id: UUID,
    session: AdminSession,
    response: Response,
    file: Annotated[
        list[UploadFile],
        File(
            description="可多选 1～50 份 PDF、Office、CSV、TXT、MD 或图片；每份独立 doc_id，可选 parse_mode",
            # 二进制数组负责 multipart；扩展元数据只供本地 Swagger 单控件多选使用。
            json_schema_extra={
                "items": {"type": "string", "format": "binary"},
                "x-dkf-file-picker": {
                    "extensions": sorted(FILE_TYPES),
                    "maxFiles": MAX_BATCH_FILES,
                },
            },
        ),
    ],
    parse_mode: Annotated[
        ParseMode,
        Form(
            description="默认 auto 自动完整性路由；native/mineru 仅供管理员显式覆盖"
        ),
    ] = "auto",
) -> DocumentResponse | BatchUploadResponse:
    """原上传入口默认自动路由；多选仍逐文件提交独立 Document 和处理代次。"""

    try:
        validate_batch(file)
        require_knowledge_base(session, kb_id)
        settings = get_settings()
        if len(file) == 1:
            saved = store_document(session, kb_id, file[0], settings)
            return dispatch_uploaded_document(session, saved, parse_mode)

        def dispatch(saved: DocumentResponse) -> DocumentResponse:
            """为当前单文件投递相同显式模式，不云端合批或合并 Document。"""

            return dispatch_uploaded_document(session, saved, parse_mode)

        response.status_code = status.HTTP_200_OK
        return store_batch(session, kb_id, file, settings, dispatch)
    finally:
        for upload in file:
            upload.file.close()


def dispatch_uploaded_document(
    session: Session, response: DocumentResponse, parse_mode: ParseMode = "auto"
) -> DocumentResponse:
    """提交后投递默认自动模式或管理员覆盖；队列失败仍保留原件与持久化状态。"""

    options = {"parse_mode": parse_mode}
    if not publish_document(session, response.doc_id, response.task_id, **options):
        session.expire_all()
        return DocumentResponse.model_validate(session.get(Document, response.doc_id))
    return response


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
    if lock and document.management_config is not None:
        config = document.management_config
        if config.get("operation") == "delete" or config.get("state") == "running":
            raise HTTPException(status_code=409, detail="文档已有管理任务，请查询新文档/status或用/retry恢复")
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
    kb_id: UUID,
    doc_id: UUID,
    session: AdminSession,
    parse_mode: Annotated[
        ParseMode,
        Query(description="默认 auto；native/mineru 仅供管理员显式覆盖"),
    ] = "auto",
) -> DocumentResponse:
    """待处理/失败文档新解析代次使旧切片任务失效；管理员可覆盖，不重解析已完成文档。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    if document.status not in {"UPLOADED", "FAILED"}:
        raise HTTPException(
            status_code=409, detail="文档已进入处理阶段或已经完成，不重复投递"
        )
    transition_document(document, "UPLOADED")
    document.processing_task_id = uuid4()
    document.management_task_id, document.management_config = None, None
    document.chunk_task_id, document.chunk_config = None, None
    document.embedding_task_id, document.embedding_config = None, None
    document.index_task_id, document.index_config = None, None
    session.flush()
    response = DocumentResponse.model_validate(document)
    session.commit()
    options = {"parse_mode": parse_mode}
    if not publish_document(session, response.doc_id, response.task_id, **options):
        raise HTTPException(
            status_code=503, detail="任务投递失败，文件已保留，请查询文档状态后重试"
        )
    return response


def submit_maintenance(session: Session, document: Document, operation: str,
                       parse_mode: ParseMode) -> DocumentResponse:
    """维护拒绝活动云/切片/GPU/入库任务，允许等待入库或 READY；成功替换前保留旧证据。"""

    active = session.scalar(select(DocumentParseJob.job_id).where(
        DocumentParseJob.doc_id == document.doc_id, DocumentParseJob.is_active.is_(True)))
    if (active is not None or document.status not in {"UPLOADED", "FAILED", "CHUNKING", "EMBEDDING", "INDEXING", "READY"}
            or document.status == "CHUNKING" and document.chunk_task_id is not None
            or document.status == "EMBEDDING" and document.embedding_task_id is not None
            or document.status == "INDEXING" and document.index_task_id is not None):
        raise HTTPException(status_code=409, detail="文档正在处理或当前阶段不允许维护，请先查询状态")
    # 显式维护才可从等待切片/向量化换代次；旧 Chunk 保留到成功解析/回放时事务失效。
    document.status, document.error_message = "UPLOADED", None
    document.processing_task_id = uuid4()
    document.management_task_id, document.management_config = None, None
    document.chunk_task_id, document.chunk_config = None, None
    document.embedding_task_id, document.embedding_config = None, None
    document.index_task_id, document.index_config = None, None
    session.flush()
    response = DocumentResponse.model_validate(document)
    session.commit()
    log_event("cache.maintenance_requested", doc_id=response.doc_id, task_id=response.task_id,
              operation=operation, parse_mode=parse_mode)
    if not publish_document(session, response.doc_id, response.task_id,
                            parse_mode=parse_mode, operation=operation):
        raise HTTPException(status_code=503, detail="维护任务投递失败，原件与旧解析结果已保留")
    return response


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=DocumentResponse, status_code=202,
    summary="仅从成功解析缓存恢复 Block，等待后续切片（不调用云端）",
    responses={404: {"description": "文档不存在"}, 409: {"description": "活动任务或不允许的状态"}})
def reindex_document(kb_id: UUID, doc_id: UUID, session: AdminSession) -> DocumentResponse:
    """当前阶段 Reindex 只回放 Parse Cache；miss/损坏/配置变更失败，不解析或重建索引。"""

    return submit_maintenance(session, require_document(session, kb_id, doc_id, lock=True), "reindex", "auto")


@router.post("/{kb_id}/documents/{doc_id}/reparse", response_model=DocumentResponse, status_code=202,
    summary="管理员明确强制重新解析；可能再次产生 MinerU 费用",
    responses={400: {"description": "必须确认 force=true"}, 409: {"description": "存在活动处理任务"}})
def reparse_document(kb_id: UUID, doc_id: UUID, session: AdminSession,
    force: Annotated[bool, Query(description="必须明确 true：绕过整份/视觉缓存并确认可能云收费")] = False,
    parse_mode: Annotated[ParseMode, Query(description="默认 auto，管理员可覆盖 Native/Whole")] = "auto",
) -> DocumentResponse:
    """唯一允许同一已收费文档跳过缓存的新代次入口；默认不确认，防止误操作付费。"""

    if not force:
        raise HTTPException(status_code=400, detail="Reparse 必须明确 force=true；可能重复消耗云 API 额度")
    return submit_maintenance(session, require_document(session, kb_id, doc_id, lock=True), "reparse", parse_mode)


@router.post("/{kb_id}/documents/{doc_id}/chunk", response_model=ChunkTaskResponse, status_code=202,
             summary="对已解析 Block 异步切片；不重新解析、不调用云端",
             responses={404: {"description": "文档不存在"}, 409: {"description": "解析未完成或仍有活动任务"}})
def chunk_existing_document(kb_id: UUID, doc_id: UUID, session: AdminSession) -> ChunkTaskResponse:
    """显式创建/恢复同一切片检查点，提交后才投递；重复请求不创建新的 Chunk 代次。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    try:
        task_id, dispatch = prepare_chunk(session, document, get_settings())
    except ChunkError as exc:
        log_event("chunk.rejected", doc_id=doc_id, kb_id=kb_id, error_code=str(exc).split("：", 1)[0])
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = ChunkTaskResponse(doc_id=doc_id, kb_id=kb_id, chunk_task_id=task_id, status=document.status)
    session.commit()
    if dispatch and not publish_chunk(session, doc_id, task_id):
        raise HTTPException(status_code=503, detail="CHUNK_DISPATCH：切片任务投递确认失败，请查询状态并重试 /chunk")
    return response


@router.get("/{kb_id}/documents/{doc_id}/chunks", response_model=PageResponse[ChunkResponse],
            summary="按切片序号查询正文及原 Block 字符跨度", responses={404: {"description": "文档不存在"}})
def list_document_chunks(kb_id: UUID, doc_id: UUID, session: AdminSession,
                         limit: PageLimit = 20, offset: PageOffset = 0) -> PageResponse[ChunkResponse]:
    """在限定文档内按稳定序号分页；返回原文证据，不暴露向量，维护失败时可查看保留结果。"""

    require_document(session, kb_id, doc_id)
    predicate = (DocumentChunk.doc_id == doc_id, DocumentChunk.kb_id == kb_id)
    total = session.scalar(select(func.count()).select_from(DocumentChunk).where(*predicate))
    chunks = session.scalars(select(DocumentChunk).where(*predicate)
        .order_by(DocumentChunk.chunk_metadata["chunk_index"].as_integer().asc().nulls_last(), DocumentChunk.chunk_id)
        .limit(limit).offset(offset))
    return PageResponse(items=[ChunkResponse.model_validate(chunk) for chunk in chunks],
                        total=total, limit=limit, offset=offset)


@router.post("/{kb_id}/documents/{doc_id}/embed", response_model=EmbeddingTaskResponse, status_code=202,
             summary="GPU Worker 批量向量化已有 Chunk，结果本地保存，不创建索引",
             responses={404: {"description": "文档不存在"}, 409: {"description": "状态、配置或 Chunk 检查点不可用"}})
def embed_existing_document(kb_id: UUID, doc_id: UUID, session: AdminSession) -> EmbeddingTaskResponse:
    """只建立一个文档 GPU 任务，提交后投递；不在 FastAPI 进程中导入或加载模型。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    try:
        task_id, dispatch = prepare_embedding(session, document, get_settings())
    except EmbeddingError as exc:
        log_event("embedding.rejected", doc_id=doc_id, kb_id=kb_id, error_code=exc.code)
        raise HTTPException(status_code=409, detail=str(exc)) from None
    response = EmbeddingTaskResponse(doc_id=doc_id, kb_id=kb_id, embedding_task_id=task_id, status=document.status)
    session.commit()
    if dispatch and not publish_embedding(session, doc_id, task_id):
        raise HTTPException(status_code=503, detail="EMBEDDING_DISPATCH：GPU 任务投递确认失败，请查询状态并恢复 /embed")
    return response


@router.post("/{kb_id}/documents/{doc_id}/index", response_model=IndexTaskResponse, status_code=202,
             summary="CPU Worker 将完整本地向量写入 pgvector，全部成功后标记 READY",
             responses={404: {"description": "文档不存在"}, 409: {"description": "状态或检查点不允许入库"},
                        503: {"description": "数据库/pgvector 索引不可用或入库任务投递确认失败"}})
def index_existing_document(kb_id: UUID, doc_id: UUID, session: AdminSession) -> IndexTaskResponse:
    """只提交文档级入库检查点，提交后投递 CPU 队列；不加载模型、不在 API 中读取向量。"""

    document = require_document(session, kb_id, doc_id, lock=True)
    try:
        task_id, dispatch = prepare_index(session, document)
    except IndexingError as exc:
        log_event("index.rejected", doc_id=doc_id, kb_id=kb_id, error_code=exc.code)
        raise HTTPException(status_code=503 if exc.code == "INDEX_SCHEMA" else 409, detail=str(exc)) from None
    response = IndexTaskResponse(doc_id=doc_id, kb_id=kb_id, index_task_id=task_id, status=document.status)
    session.commit()
    if dispatch and not publish_index(session, doc_id, task_id):
        raise HTTPException(status_code=503, detail=str(IndexingError("INDEX_DISPATCH")))
    return response
