"""知识库耗时任务的 Celery 入口。"""

from celery import Celery
from kombu import Queue

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "knowledge-service",
    broker=settings.celery_broker_url or settings.redis_url,
    backend=settings.celery_result_backend or settings.redis_url,
    include=["app.tasks.demo"],
)

celery_app.conf.update(
    task_default_queue="default_queue",
    task_queues=(Queue("default_queue"), Queue("gpu_queue")),
    task_routes={"app.tasks.demo.add": {"queue": "default_queue"}},
    # 队列拼写错误应直接暴露，避免任务误入无人消费的新队列。
    task_create_missing_queues=False,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_ignore_result=False,
    result_expires=86400,
    # 测试任务是纯计算，可安全重复执行；后续文档任务须自行保证业务幂等。
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0.2,
        "interval_step": 0.2,
        "interval_max": 0.5,
    },
    redis_socket_connect_timeout=3,
    redis_socket_timeout=3,
    redis_retry_on_timeout=False,
    enable_utc=True,
    timezone="Asia/Shanghai",
)
