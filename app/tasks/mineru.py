"""MinerU 提交与单次检查任务：不轮询睡眠、不暴露 Token、签名链接或上游原文。"""

import logging
import random
from time import perf_counter
from uuid import UUID

from kombu.exceptions import OperationalError as BrokerError
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.business_logging import log_event
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.models import DocumentParseJob
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import check_step, record_error, submit_step, restore_cached_result
from app.services.mineru.provider import MinerUCloudProvider
from app.services.parse_cache import CacheError
from app.parsers.hybrid import BundleManifest
from app.services.hybrid_processing import metrics
from app.tasks.documents import DocumentProcessingError, processing_session

logger = logging.getLogger(__name__)


class DeferredCloudProvider:
    """只有真正的云操作才构造客户端；本地ZIP重放不需要Token、Redis或网络客户端。"""

    def __init__(self, settings):
        """只保存本地配置，不校验云凭据或初始化HTTP/限流资源。"""
        self.settings, self.instance = settings, None

    def client(self):
        """首次云操作才实例化唯一Provider，保留原有TLS、限流与费用保护。"""
        if self.instance is None:
            self.instance = MinerUCloudProvider(self.settings)
        return self.instance

    def request_upload_urls(self, files, options):
        """仅正常云提交路径委托原Provider申请批次。"""
        return self.client().request_upload_urls(files, options)

    def upload(self, url, source):
        """仅正常云提交路径委托原Provider上传。"""
        return self.client().upload(url, source)

    def query(self, batch_id, data_id):
        """本地结果确实不存在且允许云操作时才查询原批次。"""
        return self.client().query(batch_id, data_id)

    def download_result(self, url):
        """只在云结果获取阶段下载，不参与Adapter处理。"""
        return self.client().download_result(url)

    def close(self):
        """未初始化的本地重放不触碰云资源；已初始化则按原逻辑释放。"""
        if self.instance is not None:
            self.instance.close()


def publish_step(job_id: UUID, step: str = "submit", delay: int = 0) -> None:
    """向 CPU 队列投递一个短步骤；只传本地任务标识，禁止 Token 或签名 URL 入消息。"""

    task = submit_mineru_parse if step == "submit" else check_mineru_result
    try:
        task.apply_async(args=[str(job_id)], countdown=delay, queue="default_queue")
        log_event(
            "mineru.dispatched",
            job_id=job_id,
            step=step,
            delay_seconds=delay,
            queue="default_queue",
        )
    except (BrokerError, RedisError, OSError):
        log_event(
            "mineru.dispatch_failed",
            level=logging.ERROR,
            job_id=job_id,
            step=step,
            error_code="QUEUE",
        )
        raise MinerUError(
            "QUEUE", "MinerU 后续任务投递失败，请恢复队列后继续任务", retryable=True
        ) from None


def execute_step(task, job_id: str, step: str) -> dict:
    """执行短步骤；客户端延迟创建，本地重放在入口和服务层都禁止访问云端。"""

    try:
        identifier = UUID(job_id)
    except (ValueError, TypeError, AttributeError):
        raise DocumentProcessingError("MinerU 任务标识必须为合法 UUID") from None
    settings = get_settings()
    started = perf_counter()
    log_event("mineru.step_started", job_id=identifier, step=step)
    provider = None
    try:
        with processing_session() as session:
            restored = restore_cached_result(session, identifier, settings)
            if restored is not None:
                action, value = restored
            else:
                job = session.get(DocumentParseJob, identifier)
                # 本地重放没有Provider可调用；正常检查也先读ZIP，真正缺结果才初始化客户端。
                provider = None if job is not None and job.parse_options.get("_local_replay_only") is True else DeferredCloudProvider(settings)
                action, value = (submit_step if step == "submit" else check_step)(
                    session, identifier, provider, settings
                )
            job = session.get(DocumentParseJob, identifier)
            route_summary = {}
            if job is not None:
                from app.services.mineru.pdf_split_jobs import submitted_count
                route_summary["mineru_file_submit_count"] = submitted_count(job)
            if job is not None and "_route_decision" in job.parse_options:
                route_summary.update({
                    "route_decision": job.parse_options["_route_decision"],
                })
                if "_hybrid" in job.parse_options:
                    route_summary.update(metrics(BundleManifest.model_validate(
                        job.parse_options["_hybrid"]["manifest"]
                    )))
        if action in {"submit", "check"}:
            publish_step(identifier, action, value)
        summary = {
            "job_id": str(identifier),
            "status": {
                "done": "awaiting_chunk",
                "skipped": "skipped",
                "submit": "scheduled_submit",
                "check": "scheduled_check",
            }[action],
            **({"block_count": value} if action == "done" else {}),
            **route_summary,
        }
        log_event(
            "mineru.step_finished",
            job_id=identifier,
            step=step,
            status=summary["status"],
            block_count=summary.get("block_count"),
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )
        return summary
    except MinerUError as exc:
        error = exc
    except CacheError as exc:
        error = MinerUError(exc.code, str(exc))
    except (OperationalError, OSError):
        error = MinerUError(
            "DATABASE", "MinerU 数据库或文件访问暂时不可用", retryable=True
        )
    except SQLAlchemyError:
        error = MinerUError("DATABASE_SCHEMA", "MinerU 数据落库失败，请检查迁移与约束")
    except Exception:  # noqa: BLE001
        error = MinerUError("UNKNOWN", "MinerU 处理异常，请检查 Worker 配置与结果结构")
    finally:
        if provider is not None:
            provider.close()
    try:
        with processing_session() as session:
            retry = record_error(session, identifier, error, settings)
            job = session.get(DocumentParseJob, identifier)
            retries = job.retry_count if job else 0
    except (SQLAlchemyError, RuntimeError, ValueError):
        # 数据库失联时无法递增持久化次数，使用 Celery 的有限重试次数，不能吞掉失败。
        logger.error(
            "MinerU 错误记录未落库：job_id=%s，错误码=%s", identifier, error.code
        )
        retry, retries = (
            error.retryable and task.request.retries < settings.mineru_max_retries,
            task.request.retries + 1,
        )
        log_event(
            "mineru.error_record_failed",
            level=logging.ERROR,
            job_id=identifier,
            error_code=error.code,
        )
    if retry:
        delay = min(5 * 2 ** max(0, retries - 1), 300) + random.SystemRandom().randint(
            0, 5
        )
        delay = max(delay, error.retry_after or 0)
        log_event(
            "mineru.retry",
            level=logging.WARNING,
            job_id=identifier,
            step=step,
            retry_count=retries,
            delay_seconds=delay,
            error_code=error.code,
        )
        raise task.retry(
            exc=DocumentProcessingError(str(error)),
            countdown=delay,
            max_retries=settings.mineru_max_retries,
            throw=False,
        ) from None
    log_event(
        "mineru.failed",
        level=logging.ERROR,
        job_id=identifier,
        step=step,
        error_code=error.code,
        duration_ms=round((perf_counter() - started) * 1000, 2),
    )
    raise DocumentProcessingError(str(error)) from None


@celery_app.task(
    bind=True,
    name="app.tasks.mineru.submit_mineru_parse",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=20,
)
def submit_mineru_parse(self, job_id: str) -> dict:
    """申请批次并上传原件，保存检查点后延时投递结果检查，不等待云端解析。"""

    return execute_step(self, job_id, "submit")


@celery_app.task(
    bind=True,
    name="app.tasks.mineru.check_mineru_result",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=20,
)
def check_mineru_result(self, job_id: str) -> dict:
    """检查一次外部状态；未完成延时再查，完成时事务写块并停在 CHUNKING。"""

    return execute_step(self, job_id, "check")
