"""只读核对原MinerU整份Job；显式许可后仅GET原Batch，不上传、下载结果、重投或修改业务数据。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import result_failure
from app.services.mineru.provider import EXTERNAL_STATES, MinerUCloudProvider, safe_identifier


# 只认完整固定短语，不从含文件名、URL或正文的长消息中猜原因，也不推断数字错误码。
# 中英文名称依据官方精准解析文档；分类仅供诊断，绝不改变生产重试或收费策略。
FAILURE_CATEGORIES = (
    ("FILE_CONVERSION_FAILED", ("-60015", "-60016"),
     ("文件转换失败", "file conversion failed"),
     "MinerU 报告文件转换失败，尚不能据此认定原件损坏"),
    ("FILE_READ_FAILED", ("-60003",), ("文件读取失败", "file read failure"),
     "MinerU 报告文件读取失败，是否损坏仍需核实"),
    ("FILE_READ_TIMEOUT", ("-60008",), ("文件读取超时", "file read timeout"),
     "MinerU 报告文件读取超时"),
    ("UNSUPPORTED_FILE_FORMAT", ("-60002",),
     ("获取匹配的文件格式失败", "failed to match file format",
      "文件格式不支持，请上传符合要求的文件类型",
      "unsupported file format, please upload a valid file type"),
     "MinerU 报告无法识别或不支持该文件格式"),
    ("FILE_SIZE_LIMIT", ("-60005",), ("文件大小超出限制", "file size exceeds limit"),
     "MinerU 报告文件大小超过平台限制"),
    ("PAGE_COUNT_LIMIT", ("-60006",), ("文件页数超过限制", "page count exceeds limit"),
     "MinerU 报告文件页数超过平台限制"),
    ("SERVICE_UNAVAILABLE", ("-10001", "-60007"),
     ("服务异常", "模型服务暂时不可用", "service error", "model service temporarily unavailable"),
     "MinerU 报告服务异常或模型服务暂时不可用"),
    ("QUEUE_FULL", ("-60009",), ("任务提交队列已满", "task submission queue is full"),
     "MinerU 报告任务提交队列已满"),
    ("PARSING_FAILED", ("-60010",),
     ("解析失败", "extract failed", "parsing failed, please try again later"),
     "MinerU 仅报告解析失败，没有提供更底层原因"),
    ("FILE_SPLITTING_FAILED", ("-60020",), ("文件拆分失败", "file splitting failed"),
     "MinerU 报告文件拆分失败"),
    ("PAGE_COUNT_READ_FAILED", ("-60021",), ("读取文件页数失败", "failed to read page count"),
     "MinerU 报告读取文件页数失败"),
)


def describe_failure_field(payload, name):
    """仅返回固定字段的存在性、类型与字符串指纹；不序列化未知对象或透传原文。"""
    if name not in payload:
        return {"status": "MISSING"}
    value = payload[name]
    if value is None:
        return {"status": "NULL"}
    if isinstance(value, str):
        field = {"status": "PRESENT" if value.strip() else "EMPTY", "length": len(value),
                 "sha256": hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()}
    elif name != "err_msg" and type(value) is int:
        field = {"status": "PRESENT"}
    else:
        # bool不能当数字错误码，数组/对象中也可能包含原文或私密链接。
        return {"status": "INVALID_TYPE"}
    if name != "err_msg":
        code = result_failure({name: value}).code
        field["known_code"] = code if code != "REMOTE_FAILED" else None
    return field


def describe_failure(payload):
    """区分未返回、空值、非法类型及未识别原因；分类只产生受控中文，不改变生产错误码。"""
    fields = {name: describe_failure_field(payload, name) for name in ("err_code", "error_code", "err_msg")}
    error = result_failure(payload)
    code_source = next((name for name in ("err_code", "error_code") if fields[name].get("known_code")), None)
    message = payload.get("err_msg")
    # 只规范大小写、空白与句末标点；超长消息仍记录完整指纹，但不做猜测性匹配。
    normalized = (" ".join(message.split()).casefold().rstrip(".。!！")
                  if isinstance(message, str) and len(message) <= 2048 else None)
    matched = next((entry for entry in FAILURE_CATEGORIES if normalized in entry[2]), None)
    code_match = next((entry for entry in FAILURE_CATEGORIES if code_source and error.code in entry[1]), None)
    selected = code_match if code_source else matched
    source, category, summary = None, None, str(error)
    if code_source:
        source, reason_status = code_source, "KNOWN_CODE"
        if selected:
            category, summary = selected[0], selected[3]
    elif matched or error.code != "REMOTE_FAILED":
        source, reason_status = "err_msg", "CLASSIFIED_MESSAGE"
        if matched:
            category, summary = matched[0], matched[3]
    else:
        reason_status, summary = {
            "MISSING": ("MESSAGE_MISSING", "本次 MinerU 响应未包含 err_msg，且未获得可识别错误码"),
            "NULL": ("MESSAGE_NULL", "本次 MinerU 返回的 err_msg 为 null，且未获得可识别错误码"),
            "EMPTY": ("MESSAGE_EMPTY", "本次 MinerU 返回的 err_msg 为空，且未获得可识别错误码"),
            "INVALID_TYPE": ("MESSAGE_INVALID_TYPE", "本次 MinerU 的 err_msg 类型无效，未输出原值"),
            "PRESENT": ("MESSAGE_UNRECOGNIZED", "MinerU 已返回非空原因，但本地分类尚未识别；不是平台未提供原因"),
        }[fields["err_msg"]["status"]]
    return {"cause_code": error.code, "message": summary, "failure_diagnostic": {
        "reason_status": reason_status, "source": source, "category": category, "fields": fields}}


def inspect_job(document, job, *, query=None):
    """核对当前同Hash整份Job；可选GET原Batch并输出安全原因诊断，不改Job或业务状态。"""
    if (document.doc_id != job.doc_id or document.file_hash != job.file_hash
            or document.processing_task_id != job.task_id or job.provider != "mineru_cloud"
            or not isinstance(job.parse_options, dict)
            or any(key in job.parse_options for key in ("_pdf_split", "_hybrid"))):
        raise MinerUError("DIAGNOSTIC_IDENTITY", "原Job身份不符或不是整份任务，未查询云端")
    batch_id = safe_identifier(job.batch_id, optional=True)
    data_id = safe_identifier(job.data_id)
    stored = job.parse_options.get("_remote_failure")
    stored_cause = (result_failure({"err_code": stored.get("cause_code")}).code
                    if isinstance(stored, dict) else None)
    report = {"doc_id": str(document.doc_id), "job_id": str(job.job_id),
              "batch_id": batch_id, "data_id": data_id,
              "status": "LOCAL_CHECKPOINT_ONLY", "stored_cause_code": stored_cause,
              "cloud_queries": 0, "cloud_submitted": False, "result_downloaded": False,
              "database_modified": False, "cache_published": False, "business_recovered": False}
    if query is None:
        return report
    if not batch_id or job.upload_complete is not True:
        raise MinerUError("DIAGNOSTIC_CHECKPOINT", "原批次或已上传检查点缺失，未查询云端")
    result = query(batch_id, data_id)
    state = result.get("state")
    if not isinstance(state, str) or state not in EXTERNAL_STATES:
        raise MinerUError("BAD_RESPONSE", "MinerU 返回的状态无效")
    report.update(status="REMOTE_STATUS_ONLY", remote_state=state, cloud_queries=1,
                  trace_id=safe_identifier(result.get("trace_id"), optional=True))
    if state == "failed":
        report.update(describe_failure(result))
    # done只是供应商状态；没有取得ZIP或执行入库，绝不将诊断当作恢复成功。
    return report


def inspect_database(doc_id, settings, *, query_existing=False):
    """在只读SQL事务中选择唯一当前Job；只有显式开关才构造Provider并GET一次原批次。"""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session, defer
    from app.db.session import get_engine
    from app.models import Document, DocumentParseJob

    with Session(get_engine(), autoflush=False) as session:
        provider = None
        try:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            rows = session.execute(select(Document, DocumentParseJob).join(
                DocumentParseJob, DocumentParseJob.doc_id == Document.doc_id).where(
                Document.doc_id == doc_id,
                DocumentParseJob.task_id == Document.processing_task_id).options(
                defer(Document.parse_quality), defer(DocumentParseJob.upload_url),
                defer(DocumentParseJob.error_message))).all()
            if len(rows) != 1:
                raise MinerUError("DIAGNOSTIC_JOB", "无法唯一定位当前原Job，未查询云端")
            # 先验证身份，不能因身份阻塞而初始化HTTP或触碰共享限流。
            report = inspect_job(*rows[0])
            if query_existing:
                if not report["batch_id"] or rows[0][1].upload_complete is not True:
                    raise MinerUError("DIAGNOSTIC_CHECKPOINT", "原批次或已上传检查点缺失，未查询云端")
                provider = MinerUCloudProvider(settings)
                report = inspect_job(*rows[0], query=provider.query)
            return report
        finally:
            try:
                if provider is not None:
                    provider.close()
            finally:
                session.rollback()


def main(argv=None):
    """默认不访问云端；查询既有批次也不修改Job，所有异常只输出安全代码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", required=True, type=UUID)
    parser.add_argument("--query-existing", action="store_true", help="显式只GET一次原Batch，不提交新解析")
    args = parser.parse_args(argv)
    try:
        from app.core.business_logging import configure_logging
        from app.core.config import get_settings
        settings = get_settings()
        configure_logging(settings, role="mineru-diagnostic-cli")
        report = inspect_database(args.doc_id, settings, query_existing=args.query_existing)
    except MinerUError as error:
        report = {"status": "BLOCKED", "error_code": error.code,
                  "cloud_submitted": False, "database_modified": False, "business_recovered": False}
    except Exception:
        report = {"status": "BLOCKED", "error_code": "DIAGNOSTIC_IO_OR_DB",
                  "cloud_submitted": False, "database_modified": False, "business_recovered": False}
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 2 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
