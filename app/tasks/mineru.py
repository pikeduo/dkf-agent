"""MinerU 提交与单次检查任务：不轮询睡眠、不暴露 Token、签名链接或上游原文。"""

import logging
import random
from uuid import UUID

from kombu.exceptions import OperationalError as BrokerError
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.models import DocumentParseJob
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import check_step, record_error, submit_step
from app.services.mineru.provider import MinerUCloudProvider
from app.tasks.documents import DocumentProcessingError, processing_session

logger = logging.getLogger(__name__)


def publish_step(job_id: UUID, step: str = "submit", delay: int = 0) -> None:
    """向 CPU 队列投递一个短步骤；只传本地任务标识，禁止 Token 或签名 URL 入消息。"""

    task = submit_mineru_parse if step == "submit" else check_mineru_result
    try:
        task.apply_async(args=[str(job_id)], countdown=delay, queue="default_queue")
    except (BrokerError, RedisError, OSError):
        raise MinerUError(
            "QUEUE", "MinerU 后续任务投递失败，请恢复队列后继续任务", retryable=True
        ) from None


def execute_step(task, job_id: str, step: str) -> dict:
    """执行一次 HTTP 步骤后结束；临时错误按持久化次数指数退避，永久错误立即失败。"""

    try:
        identifier = UUID(job_id)
    except (ValueError, TypeError, AttributeError):
        raise DocumentProcessingError("MinerU 任务标识必须为合法 UUID") from None
    settings = get_settings()
    provider = None
    try:
        provider = MinerUCloudProvider(settings)
        with processing_session() as session:
            action, value = (submit_step if step == "submit" else check_step)(
                session, identifier, provider, settings
            )
        if action in {"submit", "check"}:
            publish_step(identifier, action, value)
        return {
            "job_id": str(identifier),
            "status": {
                "done": "awaiting_chunk",
                "skipped": "skipped",
                "submit": "scheduled_submit",
                "check": "scheduled_check",
            }[action],
            **({"block_count": value} if action == "done" else {}),
        }
    except MinerUError as exc:
        error = exc
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
    if retry:
        delay = min(5 * 2 ** max(0, retries - 1), 300) + random.SystemRandom().randint(
            0, 5
        )
        delay = max(delay, error.retry_after or 0)
        raise task.retry(
            exc=DocumentProcessingError(str(error)),
            countdown=delay,
            max_retries=settings.mineru_max_retries,
            throw=False,
        ) from None
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
