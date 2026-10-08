"""DKF-Agent Knowledge Service 的 FastAPI 应用入口。"""

from fastapi import FastAPI

from app.api.admin_knowledge_bases import router as admin_knowledge_bases_router
from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="DKF-Agent Knowledge Service",
    version="0.1.0",
    description="数知融问智能体的非结构化知识问答服务。",
)

# 路由集中在入口注册，后续模块可按领域扩展而不影响应用启动流程。
app.include_router(health_router)
app.include_router(tasks_router)
app.include_router(admin_knowledge_bases_router)
