"""阶段35单文档只读快照；有界前缀投影避免加载整表正文、向量和视觉附件清单。"""

import hashlib
import json
import logging
from time import perf_counter
from uuid import UUID, uuid5

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.business_logging import log_event
from app.db.session import get_engine
from app.models import Document, DocumentBlock, KnowledgeBase
from app.schemas.document_analysis import DocumentAnalysisRequest
from app.services.document_analysis import DocumentSnapshot, MAX_BLOCKS, analyze_observations
from app.services.specialty_errors import SpecialtyError

QUALITY_FIELDS = ("content", "structure", "evidence", "warnings", "covered_pages", "required_pages", "missing_pages",
                  "visual_review_pages", "quarantined_count", "visual_only_count")


def document_statement(kb_id, doc_id=None, *, with_source=False):
    """只读指定ACTIVE库和已索引文档摘要，视觉检查许可之外不读取私有原件路径。"""
    fields = [Document.kb_id, Document.doc_id, Document.file_name, Document.file_type, Document.file_hash,
              Document.processing_task_id, Document.status,
              Document.parse_quality.is_not(None).label("quality_present")]
    fields.extend(Document.parse_quality[key].label("quality_" + key) for key in QUALITY_FIELDS)
    if with_source:
        fields.append(Document.file_path)
    statement = (select(*fields).join(KnowledgeBase, KnowledgeBase.kb_id == Document.kb_id)
        .where(Document.kb_id == kb_id, KnowledgeBase.status == "ACTIVE",
               Document.status.in_(["READY", "PARTIAL_READY"])))
    if doc_id is not None:
        statement = statement.where(Document.doc_id == doc_id)
    return statement.order_by(Document.doc_id).limit(1)


def block_statement(doc_id):
    """整文档按稳定序号流式读取轻量字段；前缀不充当完整Block或公式来源核验。"""
    return (select(DocumentBlock.block_id, DocumentBlock.block_index, DocumentBlock.page,
        DocumentBlock.block_type, func.substr(DocumentBlock.text, 1, 512).label("preview"),
        func.char_length(DocumentBlock.text).label("text_chars"),
        func.substr(DocumentBlock.section, 1, 512).label("section"),
        DocumentBlock.source, DocumentBlock.bbox, DocumentBlock.confidence)
        .where(DocumentBlock.doc_id == doc_id)
        .order_by(DocumentBlock.block_index.asc().nulls_last(), DocumentBlock.block_id)
        .limit(MAX_BLOCKS + 1).execution_options(yield_per=128))


def candidate_statement(kb_id, case_name):
    """按当前真实元数据选择典型候选，不将候选标签当成已验证的效果或缺陷。"""
    statement = document_statement(kb_id)
    if case_name in {"outline", "formula", "complexity"}:
        kinds = ["title"] if case_name == "outline" else ["formula"] if case_name == "formula" else ["table", "formula"]
        count = (select(func.count()).select_from(DocumentBlock)
            .where(DocumentBlock.doc_id == Document.doc_id, DocumentBlock.block_type.in_(kinds))
            .correlate(Document).scalar_subquery())
        statement = statement.where(count > 0).order_by(None).order_by(count.desc(), Document.doc_id)
    elif case_name in {"coverage", "visual_review"}:
        field = Document.parse_quality["missing_pages" if case_name == "coverage" else "visual_review_pages"]
        count = case((func.jsonb_typeof(field) == "array", func.jsonb_array_length(field)), else_=0)
        statement = statement.where(count > 0).order_by(None).order_by(count.desc(), Document.doc_id)
    elif case_name != "overview":
        raise SpecialtyError("ANALYSIS_INPUT")
    return statement


def _readonly(session):
    """使用独立短快照，拒绝待写入或已开始的事务，不flush/commit或重试。"""
    if session.new or session.dirty or session.deleted or session.in_transaction():
        raise SpecialtyError("ANALYSIS_SESSION")
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.execute(text("SET TRANSACTION READ ONLY"))
    session.execute(text("SET LOCAL statement_timeout = '10s'"))
    session.execute(text("SET LOCAL lock_timeout = '2s'"))


class DocumentAssessmentService:
    """读取派生评估，不重解析、不运行检索模型、不更新Document/Block/质量字段。"""

    def __init__(self, *, session_factory=None, visual_reader=None):
        """保存可注入的会话与视觉边界，构造时无配置、数据库或模型操作。"""
        self._session_factory = session_factory or self._session
        self._visual_reader = visual_reader

    @staticmethod
    def _session():
        """首次显式执行才使用现有引擎，默认无自动flush和提交。"""
        return Session(get_engine(), autoflush=False, expire_on_commit=False)

    def choose_document(self, kb_id, *, case_name="overview"):
        """CLI未选文档时按case选择当前真实候选，返回实际ID，不填虚构示例。"""
        try:
            identity = UUID(str(kb_id))
            if identity.int == 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SpecialtyError("ANALYSIS_INPUT") from None
        try:
            with self._session_factory() as session:
                _readonly(session)
                with session.no_autoflush:
                    result = session.execute(candidate_statement(identity, case_name))
                    try:
                        row = result.mappings().one_or_none()
                    finally:
                        result.close()
                    if row is None:
                        raise SpecialtyError("ANALYSIS_UNAVAILABLE")
                    return row["doc_id"]
        except SpecialtyError:
            raise
        except Exception:
            raise SpecialtyError("ANALYSIS_DATABASE") from None

    def analyze(self, payload):
        """先完成只读Block快照，再按显式选页做原件Hash/视觉观测；失败不返回半份报告。"""
        started, scope, stage = perf_counter(), {}, "input"
        try:
            if not isinstance(payload, DocumentAnalysisRequest):
                raise SpecialtyError("ANALYSIS_INPUT")
            payload = DocumentAnalysisRequest.model_validate(payload.model_dump())
            scope = {"kb_id": payload.kb_id, "doc_id": payload.doc_id}
            log_event("document_analysis.started", **scope)
            stage = "database"
            with self._session_factory() as session:
                _readonly(session)
                with session.no_autoflush:
                    result = session.execute(document_statement(payload.kb_id, payload.doc_id,
                        with_source=bool(payload.visual_pages)))
                    try:
                        row = result.mappings().one_or_none()
                    finally:
                        result.close()
                    if row is None:
                        raise SpecialtyError("ANALYSIS_UNAVAILABLE")
                    if row["kb_id"] != payload.kb_id or row["doc_id"] != payload.doc_id:
                        raise SpecialtyError("ANALYSIS_METADATA")
                    quality = {key: row["quality_" + key] for key in QUALITY_FIELDS
                               if row["quality_" + key] is not None} if row["quality_present"] else None
                    snapshot = DocumentSnapshot(kb_id=row["kb_id"], doc_id=row["doc_id"],
                        file_name=row["file_name"], file_type=row["file_type"], file_hash=row["file_hash"],
                        processing_task_id=row["processing_task_id"], status=row["status"], quality=quality,
                        file_path=row["file_path"] if payload.visual_pages else None)
                    stage = "observations"
                    result = session.execute(block_statement(payload.doc_id))
                    try:
                        report = analyze_observations(snapshot, result.mappings())
                    finally:
                        result.close()
            if payload.visual_pages:
                stage = "visual"
                if self._visual_reader is None:
                    from app.services.document_visual_quality import inspect_visual_pages
                    reader = inspect_visual_pages
                else:
                    reader = self._visual_reader
                page_count, visuals = reader(snapshot, payload.visual_pages)
                if (type(page_count) is not int or page_count < 1
                        or [item.page for item in visuals] != payload.visual_pages):
                    raise SpecialtyError("SPECIALTY_RESULT")
                report.source_page_count = page_count
                report.quality.visual_pages = visuals
                # 选页检查仍不证明其他页面或OCR语义完整；保留全局未检查维度。
                report.quality.not_assessed = [item for item in report.quality.not_assessed
                    if item not in {"SOURCE_BLUR", "SOURCE_SKEW"}] + ["UNSELECTED_SOURCE_PAGES"]
                if any(item.blur == "unknown" for item in visuals):
                    report.quality.not_assessed.append("SELECTED_PAGE_BLUR_UNDETERMINED")
                if any(item.skew == "unknown" for item in visuals):
                    report.quality.not_assessed.append("SELECTED_PAGE_SKEW_UNDETERMINED")
                if any(item.blur == "review" or item.skew == "review" for item in visuals):
                    if report.quality.status in {"unknown", "no_observed_issue"}:
                        report.quality.status = "review"
                serialized = json.dumps([item.model_dump(mode="python") for item in visuals],
                    sort_keys=True, allow_nan=False)
                report.assessment_id = uuid5(report.assessment_id,
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest())
            # 输出再校验，不在JSON转换时把非有限值洗成null。
            serialized = json.dumps(report.model_dump(mode="python"), default=str,
                ensure_ascii=False, allow_nan=False)
            serialized.encode("utf-8", errors="strict")
            checked = type(report).model_validate_json(serialized)
            log_event("document_analysis.finished", **scope, block_count=report.block_count,
                page_count=len(report.observed_pages), status=report.quality.status,
                duration_ms=(perf_counter() - started) * 1000)
            return checked
        except Exception as exc:
            error = exc if isinstance(exc, SpecialtyError) else SpecialtyError(
                "ANALYSIS_DATABASE" if stage == "database" or isinstance(exc, SQLAlchemyError)
                else "ANALYSIS_INPUT" if stage == "input" else "ANALYSIS_METADATA")
            log_event("document_analysis.failed", level=logging.WARNING, **scope, stage=stage,
                error_code=error.code, duration_ms=(perf_counter() - started) * 1000)
            raise error from None
