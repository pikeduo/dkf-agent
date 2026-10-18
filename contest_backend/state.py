"""单进程有界会话与授权账本；敏感上下文仅短期内存，不持久化问题/SQL/结果。"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from threading import Lock, RLock
from time import monotonic
from uuid import UUID, uuid4

from contracts.contest_v1 import CloudAuthorization, ConfirmedSlot
from contest_backend.errors import ContestError
from contest_backend.registry import SourceSpec


@dataclass
class Grant:
    """绑定当前任务及范围的显式许可，所有步骤共用累计预算。"""

    policy: CloudAuthorization
    task_id: UUID
    calls: int = 0
    attempted_calls: int = 0
    reserved_tokens: int = 0
    uncertain: bool = False

    def reserve(self, tokens: int) -> None:
        """调用前保守占用预算，失败不退还；需由会话锁保护本方法。"""
        if (self.uncertain or self.calls >= self.policy.max_calls
                or self.reserved_tokens + tokens > self.policy.max_total_tokens):
            raise ContestError("BUDGET_EXHAUSTED", 403)
        self.calls += 1
        self.reserved_tokens += tokens


@dataclass
class Session:
    """固定源快照和私密会话令牌；多个用户不能仅凭UUID共享确认条件。"""

    id: UUID
    token_hash: str
    source: SourceSpec
    expires_at: float
    task_id: UUID = field(default_factory=uuid4)
    question: str | None = None
    slots: dict[str, ConfirmedSlot] = field(default_factory=dict)
    pending: dict | None = None
    revision: int = 0
    grants: dict[UUID, Grant] = field(default_factory=dict)
    requests: set[UUID] = field(default_factory=set)
    traces: dict[UUID, dict] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def reset(self) -> None:
        """修正任务时整体失效旧条件、待答问题和许可，不重用不同问题的确认结果。"""
        self.task_id = uuid4()
        self.question, self.pending = None, None
        self.slots.clear()
        self.grants.clear()
        self.revision += 1

    def grant(self, policy: CloudAuthorization) -> Grant:
        """客户端许可只能与已发放记录完全一致，不能增大预算或开启修复。"""
        grant = self.grants.get(policy.consent_id)
        if (not policy.allow_cloud or grant is None or grant.task_id != self.task_id
                or grant.policy != policy):
            raise ContestError("AUTHORIZATION_REQUIRED", 403)
        return grant


class SessionStore:
    """仅支持一个服务进程；有界TTL状态与密钥轮换/服务重启后均失效。"""

    def __init__(self, ttl: int, maximum: int):
        """初始化内存容器，不创建文件、数据库表或后台线程。"""
        self.ttl, self.maximum = ttl, maximum
        self.sessions: dict[UUID, Session] = {}
        self.lock = RLock()

    def create(self, source: SourceSpec) -> tuple[Session, str]:
        """发放一次性可见的高熵会话令牌；容量满时失败而非覆盖活动会话。"""
        with self.lock:
            now = monotonic()
            self.sessions = {key: value for key, value in self.sessions.items()
                             if value.expires_at > now or value.lock.locked()}
            if len(self.sessions) >= self.maximum:
                raise ContestError("DEPENDENCY_UNAVAILABLE", 503)
            token = secrets.token_urlsafe(32)
            session = Session(uuid4(), hashlib.sha256(token.encode()).hexdigest(), source.model_copy(deep=True), now + self.ttl)
            self.sessions[session.id] = session
            return session, token

    def get(self, identity: UUID, token: str) -> Session:
        """常量时间核对会话私钥；不存在/过期/越权统一拒绝，避免枚举。"""
        with self.lock:
            session = self.sessions.get(identity)
            digest = hashlib.sha256(token.encode()).hexdigest()
            if session is None or session.expires_at <= monotonic() or not hmac.compare_digest(session.token_hash, digest):
                raise ContestError("PERMISSION_DENIED", 403)
            return session
