"""Redis 原子共享频控：提交按文件数计量，查询按调用数计量，失败时关闭调用。"""

import math
from uuid import uuid4

import redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.services.mineru.errors import MinerUError

# 使用 Redis TIME 和滚动窗口，不依赖各 Worker 本机时钟；日额度使用更保守的滚动 24h。
# 优先级页数是观测量，不是硬限制：超过阈值仍允许提交。
RESERVE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local prefix, kind = ARGV[1], ARGV[2]
local count, limit, daily = tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
local minute = prefix .. ':' .. kind .. ':minute'
redis.call('ZREMRANGEBYSCORE', minute, '-inf', now - 60)
if redis.call('ZCARD', minute) + count > limit then
    local oldest = redis.call('ZRANGE', minute, 0, 0, 'WITHSCORES')
    return {0, math.max(1, math.ceil(60 - now + tonumber(oldest[2]))), 0}
end
local day = prefix .. ':submit:day'
if kind == 'submit' then
    redis.call('ZREMRANGEBYSCORE', day, '-inf', now - 86400)
    if redis.call('ZCARD', day) + count > daily then return {-1, 0, 0} end
end
for i=1,count do
    local member = ARGV[6] .. ':' .. i
    redis.call('ZADD', minute, now, member)
    if kind == 'submit' then redis.call('ZADD', day, now, member) end
end
redis.call('EXPIRE', minute, 61)
local priority = 0
if kind == 'submit' then
    redis.call('EXPIRE', day, 86401)
    local pages = prefix .. ':pages:' .. math.floor(now / 86400)
    local total = redis.call('INCRBY', pages, tonumber(ARGV[7]))
    redis.call('EXPIRE', pages, 172800)
    if total > tonumber(ARGV[8]) then priority = 1 end
end
return {1, 0, priority}
"""


class MinerURateLimiter:
    """同一账户的 Worker 必须共用 Redis 和 quota_namespace；不尝试绕过平台限额。"""

    def __init__(self, settings: Settings, client=None) -> None:
        """创建短超时 Redis 客户端，允许测试注入，不在构造时访问服务。"""

        self.settings = settings
        self.owns_client = client is None
        self.client = client or redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=3, socket_timeout=3
        )

    def close(self) -> None:
        """短任务结束后释放自建 Redis 连接；外部注入的共享客户端由调用方管理。"""

        if self.owns_client:
            self.client.close()

    def reserve(self, kind: str, files: int = 1, pages: int = 0) -> bool:
        """原子预留本次调用额度，返回低优先级提示；耗尽或服务故障不发 HTTP。"""

        if kind not in {"submit", "query"} or files < 1 or pages < 0:
            raise ValueError("MinerU 频控参数无效")
        settings = self.settings
        configured = (
            min(settings.mineru_submit_file_limit_per_minute, 50)
            if kind == "submit"
            else min(settings.mineru_result_query_limit_per_minute, 1000)
        )
        limit = max(1, math.floor(configured * settings.mineru_rate_safety_ratio))
        count = files if kind == "submit" else 1
        if count > limit:
            raise MinerUError("BATCH_LIMIT", "本次文件数超过安全提交额度，请拆分批次")
        try:
            result = self.client.eval(
                RESERVE,
                0,
                settings.mineru_quota_namespace,
                kind,
                count,
                limit,
                min(settings.mineru_daily_file_limit, 5000),
                uuid4().hex,
                pages,
                settings.mineru_high_priority_page_limit,
            )
        except RedisError:
            raise MinerUError(
                "QUOTA_UNAVAILABLE",
                "MinerU 共享限流服务暂时不可用",
                retryable=True,
            ) from None
        if result[0] == -1:
            raise MinerUError("DAILY_LIMIT", "本地 MinerU 滚动 24 小时文件额度已耗尽")
        if result[0] == 0:
            raise MinerUError(
                "RATE_LIMIT",
                "本地 MinerU 频控等待下一时间窗口",
                retryable=True,
                retry_after=int(result[1]),
            )
        return bool(result[2])
