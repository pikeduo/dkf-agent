"""竞赛FastAPI入口；只暴露真实问数服务，不注册统一跨源Agent或知识库副本。"""

import hmac
import logging
from contextlib import asynccontextmanager, contextmanager
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, StrictInt, model_validator

from app.core.business_logging import CONTEXT, configure_logging, log_event
from contracts.contest_v1 import AgentRequest, ContractModel, Identifier, SQLScope
from contest_backend.config import Settings, get_settings
from contest_backend.errors import ContestError
from contest_backend.service import ContestService
from contest_backend.schema_linking import stable_id


class SessionRequest(ContractModel):
    """先选服务端授权范围，再取得一次性可见的会话凭据。"""

    scope: SQLScope


class ConsentRequest(ContractModel):
    """显式确认本任务的问题/授权结构外传及费用，许可不覆盖MinerU或知识服务。"""

    accept_data_transfer: Literal[True]
    accept_charges: Literal[True]
    provider_id: Identifier
    max_calls: StrictInt = Field(ge=1, le=4)
    max_total_tokens: StrictInt = Field(ge=1, le=32768)
    allow_sql_repair: StrictBool = False

    @model_validator(mode="after")
    def repair_budget(self):
        """修复许可至少保留两次总调用额度，输入错误不进入运行时异常。"""
        if self.allow_sql_repair and self.max_calls < 2:
            raise ValueError("修复预算不足")
        return self


def safe_error(code: str, status: int) -> JSONResponse:
    """固定错误说明不拼接任何用户输入、凭据、数据库或模型原始异常。"""
    return JSONResponse(status_code=status, content={"detail": {"code": code, "message": "请求未完成，请根据错误码检查授权、范围或服务状态。"}})


@contextmanager
def exclusive(session):
    """同会话并发直接冲突，不在模型/数据库等待期间排队重放业务。"""
    if not session.lock.acquire(blocking=False):
        raise ContestError("RESULT_INVALID", 409)
    try:
        yield session
    finally:
        session.lock.release()


def create_app(settings: Settings | None = None, service_factory=ContestService) -> FastAPI:
    """构造应用但不连接数据库/模型；服务目录在首个业务请求时只读加载。"""
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(application):
        """只有用户手动启动服务才初始化原日志组件，角色集中注册。"""
        configure_logging(config, role="contest-api")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        yield

    api = FastAPI(title="DKF Contest SQL Service", version="1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    api.state.service = None
    service_lock = Lock()

    def authorize(authorization: str = Header(default="")) -> None:
        """全局入口令牌默认未配置即关闭；令牌仅走请求头且永不回显。"""
        secret = config.api_token.get_secret_value()
        if not secret:
            raise ContestError("DEPENDENCY_UNAVAILABLE", 503)
        expected = ("Bearer " + secret).encode("utf-8")
        if not hmac.compare_digest(authorization.encode("utf-8"), expected):
            raise ContestError("PERMISSION_DENIED", 401)

    def service() -> ContestService:
        """复用唯一目录快照；不根据路由或工作目录创建第二份注册表。"""
        with service_lock:
            if api.state.service is None:
                api.state.service = service_factory(config)
        return api.state.service

    def session_for(identity: UUID, token: str):
        """全局认证后还须会话私钥，UUID本身不授予Trace/补参/费用权限。"""
        return service().sessions.get(identity, token)

    @api.middleware("http")
    async def safe_boundary(request: Request, call_next):
        """分配可信请求身份并恢复上下文；只记路由模板，吞并敏感异常堆栈。"""
        identity = uuid4()
        request.state.request_id = identity
        token = CONTEXT.set({"request_id": identity})
        try:
            try:
                response = await call_next(request)
            except ContestError as error:
                response = safe_error(error.code, error.http_status)
            except Exception:
                response = safe_error("INTERNAL_ERROR", 500)
            route = request.scope.get("route")
            log_event("contest.http", route=route.path if route else "/unmatched", status_code=response.status_code)
            response.headers["X-Request-ID"] = str(identity)
            return response
        finally:
            CONTEXT.reset(token)

    @api.exception_handler(ContestError)
    async def contest_error(request: Request, error: ContestError):
        """只映射固定内部分类，不序列化异常链或原始数据库信息。"""
        return safe_error(error.code, error.http_status)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        """输入失败不返回Pydantic.errors中的原始input字段。"""
        return safe_error("RESULT_INVALID", 422)

    @api.get("/health")
    def health():
        """仅声明当前HTTP进程存活，不伪造数据库/模型就绪或测试通过。"""
        return {"status": "alive", "service": "contest-sql", "database_verified": False, "model_verified": False}

    @api.get("/api/catalog", dependencies=[Depends(authorize)])
    def catalog():
        """返回真实注册目录及复合外键依据，永不返回连接标识对应的密钥。"""
        return {"sources": [{"scope": {"db_id": item.db_id, "sub_db": item.sub_db},
                "schema_name": item.schema_name, "tables": [table.model_dump() for table in item.tables],
                "foreign_keys": [dict(key.model_dump(), reference_id=stable_id(key.id)) for key in item.foreign_keys],
                "aliases": [{"alias": alias, "targets": targets, "reference_id": stable_id(alias)}
                            for alias, targets in item.aliases.items()],
                "conditions": [dict(condition.model_dump(), reference_id=stable_id(condition.slot))
                               for condition in item.conditions],
                "catalog_fingerprint": item.fingerprint()} for item in service().registry.sources]}

    @api.post("/api/sessions", status_code=201, dependencies=[Depends(authorize)])
    def create_session(body: SessionRequest):
        """创建绑定固定注册范围的真实内存会话，私钥只在本次响应发放。"""
        source = service().registry.resolve(body.scope)
        session, token = service().sessions.create(source)
        return {"session_id": str(session.id), "session_token": token, "scope": body.scope.model_dump(),
                "scope_fingerprint": source.fingerprint(), "expires_in_seconds": config.session_ttl_seconds}

    @api.post("/api/sessions/{session_id}/consents", status_code=201, dependencies=[Depends(authorize)])
    def consent(session_id: UUID, body: ConsentRequest, x_session_token: str = Header(default="")):
        """用户明确授权后发放有限许可，不触发模型或数据库调用。"""
        with exclusive(session_for(session_id, x_session_token)) as session:
            return service().issue_consent(session, provider_id=body.provider_id, max_calls=body.max_calls,
                max_total_tokens=body.max_total_tokens, allow_sql_repair=body.allow_sql_repair)

    @api.post("/api/sessions/{session_id}/reset", dependencies=[Depends(authorize)])
    def reset(session_id: UUID, x_session_token: str = Header(default="")):
        """用户修正问题/条件时清空旧确认和许可，保留固定数据源范围。"""
        with exclusive(session_for(session_id, x_session_token)) as session:
            session.reset()
            return {"status": "reset", "session_id": str(session.id)}

    @api.post("/api/query", dependencies=[Depends(authorize)])
    def query(body: AgentRequest, request: Request, x_session_token: str = Header(default="")):
        """接收冻结AgentRequest并返回AgentResponse，领域入口只实现SQL工具。"""
        if body.session_id is None:
            raise ContestError("RESULT_INVALID", 422)
        with exclusive(session_for(body.session_id, x_session_token)) as session:
            response = service().query(body, session, request_id=request.state.request_id)
        code = getattr(response.result, "error_code", None) or getattr(response.result, "code", None)
        statuses = {"PERMISSION_DENIED": 403, "SCOPE_INVALID": 403, "AUTHORIZATION_REQUIRED": 403,
                    "BUDGET_EXHAUSTED": 403, "SQL_REJECTED": 403, "UNSUPPORTED": 422,
                    "DEPENDENCY_UNAVAILABLE": 503, "EXECUTION_UNKNOWN": 503, "MODEL_FAILURE": 502,
                    "SQL_EXECUTION_FAILED": 502, "RESULT_INVALID": 502, "INTERNAL_ERROR": 500}
        return JSONResponse(status_code=statuses.get(code, 200), content=response.model_dump(mode="json"))

    @api.get("/api/traces/{trace_id}", dependencies=[Depends(authorize)])
    def trace(trace_id: UUID, session_id: UUID, x_session_token: str = Header(default="")):
        """只向原会话返回安全Trace，查询串的UUID也不会进入业务访问日志。"""
        with exclusive(session_for(session_id, x_session_token)) as session:
            record = session.traces.get(trace_id)
            if record is None:
                raise ContestError("PERMISSION_DENIED", 403)
            return record

    return api


app = create_app()
