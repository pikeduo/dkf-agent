"""DKF-Agent Knowledge Service 的 FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.admin_knowledge_bases import router as admin_knowledge_bases_router
from app.api.admin_documents import router as admin_documents_router
from app.api.docs import router as docs_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.knowledge_specialty import router as knowledge_specialty_router
from app.api.mineru import router as mineru_router
from app.api.tasks import router as tasks_router
from app.core.business_logging import RequestLoggingMiddleware, configure_logging
from app.core.config import PROJECT_ROOT, get_settings

settings = get_settings()
configure_logging(settings, "api")

app = FastAPI(
    title="DKF-Agent Knowledge Service",
    version="0.1.0",
    description="数知融问智能体的非结构化知识问答服务。",
    # 默认 Swagger 文件数组只能逐项添加，改用本地插件支持一次打开窗口多选。
    docs_url=None,
)

# 路由集中在入口注册，后续模块可按领域扩展而不影响应用启动流程。
app.include_router(health_router)
app.include_router(knowledge_router)
app.include_router(knowledge_specialty_router)
app.include_router(tasks_router)
app.include_router(admin_knowledge_bases_router)
app.include_router(admin_documents_router)
app.include_router(mineru_router)
app.include_router(docs_router)
app.mount(
    "/static/docs",
    StaticFiles(directory=PROJECT_ROOT / "app/static"),
    name="docs-static",
)
app.add_middleware(RequestLoggingMiddleware)
