"""异步测试任务提交，以及通用任务状态与结果查询。"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from kombu.exceptions import OperationalError
from pydantic import BaseModel, StrictInt
from redis.exceptions import RedisError

from app.core.celery_app import celery_app
from app.tasks.demo import add

router = APIRouter(prefix="/tasks", tags=["tasks"])


class AddRequest(BaseModel):
    x: StrictInt
    y: StrictInt
    queue: Literal["default_queue", "gpu_queue"] = "default_queue"


class TaskSubmitted(BaseModel):
    task_id: str
    status: Literal["submitted"] = "submitted"
    queue: str


class TaskStatus(BaseModel):
    task_id: str
    status: str
    result: int | dict[str, Any] | None = None
    error: str | None = None


@router.post("/add", response_model=TaskSubmitted, status_code=status.HTTP_202_ACCEPTED)
def submit_add(payload: AddRequest) -> TaskSubmitted:
    """只投递任务；HTTP 202 不代表 Worker 已开始执行。"""

    try:
        task = add.apply_async(args=(payload.x, payload.y), queue=payload.queue)
    except (OperationalError, RedisError, OSError) as exc:
        # 不向客户端返回连接 URL 或底层异常，避免泄露本地凭据。
        raise HTTPException(
            status_code=503, detail="任务队列不可用，请检查 Redis"
        ) from exc
    return TaskSubmitted(task_id=task.id, queue=payload.queue)


@router.get("/{task_id}", response_model=TaskStatus)
def get_task_status(task_id: UUID) -> TaskStatus:
    """查询 Redis 中的结果元数据，不阻塞等待任务完成。"""

    try:
        metadata = celery_app.backend.get_task_meta(str(task_id))
    except (OperationalError, RedisError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="任务结果存储不可用，请检查 Redis"
        ) from exc

    task_state = metadata["status"]
    return TaskStatus(
        task_id=str(task_id),
        status=task_state,
        result=metadata.get("result") if task_state == "SUCCESS" else None,
        error="任务执行失败，请查看 Worker 日志" if task_state == "FAILURE" else None,
    )
