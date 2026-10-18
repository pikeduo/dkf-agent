"""整份 XLSX 云解析的 Native 主结果保护；仅存摘要检查点，不是 Parse Cache。"""

import hashlib
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.business_logging import log_event
from app.parsers.common import NoTextError, ParserError
from app.parsers.routing.contracts import RouteDecision, StructuralSignals
from app.parsers.routing.guard import guard_result
from app.parsers.routing.inspector import inspect_document
from app.parsers.xlsx import XlsxParser
from app.schemas.parsed_document import ParsedDocument
from app.services.document_processing import source_path
from app.services.mineru.errors import MinerUError


class PrimaryCheckpoint(BaseModel):
    """冻结原件、Native 原式结果 Hash 和覆盖摘要，不保存单元格正文或本机路径。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["xlsx-primary-v1"] = "xlsx-primary-v1"
    doc_id: UUID
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signals: StructuralSignals


def primary_result(document, signals: StructuralSignals) -> ParsedDocument:
    """重读原件的 Sheet/Cell/公式原式并校验；缺附件或源 Hash 变化不得提交部分结果。"""

    path = source_path(document)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
            raise MinerUError("XLSX_PRIMARY_SOURCE", "XLSX 原件 Hash 已变化，停止云端处理")
    visual_only = (signals.nonempty_sheet_count == 0 and signals.formula_count == 0
                   and any((signals.image_count, signals.drawing_count, signals.chart_count, signals.embedded_count)))
    try:
        parsed = XlsxParser().parse(doc_id=document.doc_id, file_path=path,
                                  file_name=document.file_name, file_type="xlsx")
    except NoTextError:
        if not visual_only:
            raise
        parsed = ParsedDocument(doc_id=document.doc_id, file_name=document.file_name,
                                file_type="xlsx", blocks=[])
    decision = RouteDecision(parse_mode="native", file_type="xlsx", route_strategy="native",
                             reason_codes=("EXPLICIT_NATIVE",), signals=signals)
    if parsed.blocks or not visual_only:
        guard_result(parsed, decision)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
            raise MinerUError("XLSX_PRIMARY_SOURCE", "XLSX 主结果读取期间原件发生变化")
    return parsed


def prepare_primary(document, settings: Settings, decision: RouteDecision | None = None) -> dict:
    """云申请前确认 Native 主结果可保留；无法保留原式时安全失败，不转半份云结果。"""

    try:
        inspection = inspect_document(source_path(document), "xlsx", settings)
    except ParserError:
        raise MinerUError("XLSX_PRIMARY_INVALID", "XLSX 原件结构无法读取，未提交云任务") from None
    if inspection.uncertain:
        raise MinerUError("XLSX_PRIMARY_LIMIT", "XLSX 预检超限，未提交云任务")
    if {"XLSX_HAS_EXTERNAL_LINK", "XLSX_EXTERNAL_ASSET_MISSING", "EXTERNAL_ATTACHMENT_MISSING"} & set(inspection.risks):
        raise MinerUError("XLSX_EXTERNAL_ASSET_MISSING", "XLSX 外部工作簿未随原件提供，不能完整解析")
    signals = inspection.signals
    if decision is not None and signals != decision.signals:
        raise MinerUError("XLSX_PRIMARY_SOURCE", "XLSX 结构摘要与路由检查点不一致")
    try:
        native = primary_result(document, signals)
        log_event("xlsx.primary_validated", doc_id=document.doc_id,
                  block_count=len(native.blocks), sheet_count=signals.nonempty_sheet_count)
        return PrimaryCheckpoint(doc_id=document.doc_id, source_hash=document.file_hash,
            native_hash=hashlib.sha256(native.model_dump_json().encode("utf-8")).hexdigest(),
            signals=signals).model_dump(mode="json")
    except ParserError:
        raise MinerUError("XLSX_PRIMARY_INVALID", "XLSX 主结果或公式原式无法可靠保留，未提交云任务") from None


def complete_primary(document, context: dict, cloud: ParsedDocument,
                     decision: RouteDecision | None) -> ParsedDocument:
    """保留Native原式及云块原序/质量；不猜Sheet、覆盖公式或把部分云内容冒充完整。"""

    try:
        checkpoint = PrimaryCheckpoint.model_validate(context)
        if (document.file_type != "xlsx" or checkpoint.doc_id != document.doc_id
            or checkpoint.source_hash != document.file_hash
            or cloud.doc_id != document.doc_id or cloud.file_name != document.file_name):
            raise ValueError
        native = primary_result(document, checkpoint.signals)
        if hashlib.sha256(native.model_dump_json().encode("utf-8")).hexdigest() != checkpoint.native_hash:
            raise ValueError
        cloud_decision = decision or RouteDecision(parse_mode="mineru", file_type="xlsx",
            route_strategy="mineru_whole", reason_codes=("EXPLICIT_MINERU",), signals=checkpoint.signals)
        guard_result(cloud, cloud_decision)
        # Whole 云结果可能与 Native 有内容重复；宁可保留来源，也不语义删块或冒认原 Sheet。
        blocks = list(native.blocks) + [block.model_copy(update={
            "block_id": uuid5(document.doc_id, f"xlsx-primary-v1:whole:{block.block_id}")
        }) for block in cloud.blocks]
        return ParsedDocument(doc_id=document.doc_id, file_name=document.file_name,
                              file_type="xlsx", title=native.title, blocks=blocks,
                              quality=cloud.quality, quarantined_content=cloud.quarantined_content)
    except (ParserError, ValueError, KeyError, TypeError, AttributeError):
        raise MinerUError("XLSX_PRIMARY_CHECKPOINT", "XLSX 主结果或云结果校验失败，未写入部分 Block") from None
