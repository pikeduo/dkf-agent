"""阶段 3 的异步链路测试任务。"""

from app.core.celery_app import celery_app


@celery_app.task(
    name="app.tasks.demo.add",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    throws=(ValueError,),
)
def add(x: int, y: int) -> int:
    """无副作用的幂等任务，可用相同任务验证 CPU 与 GPU 队列。"""

    # 类型错误不可通过重试修复，直接记录为失败；只重试暂时性 I/O 错误。
    if type(x) is not int or type(y) is not int:
        raise ValueError("x 和 y 必须为整数")
    return x + y
