"""服务健康检查接口。"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="服务健康检查")
def health_check() -> dict[str, str]:
    """仅确认 FastAPI 进程可用；数据库与缓存连通性在后续阶段加入。"""

    return {"status": "ok", "service": "knowledge-service"}
