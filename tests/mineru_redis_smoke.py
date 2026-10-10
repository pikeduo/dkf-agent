"""显式运行的共享频控验收；只操作随机命名空间，不清空数据库或触碰业务队列。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.services.mineru.errors import MinerUError
from app.services.mineru.rate_limit import MinerURateLimiter


@pytest.fixture
def quota_context():
    """连接已有 Redis 并建立一次性命名空间，结束时仅清理本测试的键。"""

    settings = get_settings().model_copy(
        update={
            "mineru_quota_namespace": f"dkf-test:mineru:{uuid4().hex}",
            "mineru_rate_safety_ratio": 1.0,
        }
    )
    client = redis.Redis.from_url(
        settings.redis_url, socket_connect_timeout=3, socket_timeout=3
    )
    try:
        assert client.ping()
    except RedisError:
        pytest.fail("现有 Redis 不可用，请先人工检查 Windows 服务和端口", pytrace=False)
    try:
        yield settings, client
    finally:
        keys = list(client.scan_iter(match=settings.mineru_quota_namespace + ":*"))
        if keys:
            client.delete(*keys)
        client.close()


def test_real_redis_atomic_multiworker_limit(quota_context):
    """多线程模拟多个 Worker 原子竞争同一文件额度，限额之外全部被关闭。"""

    settings, client = quota_context
    settings = settings.model_copy(update={"mineru_submit_file_limit_per_minute": 10})

    def reserve_one(index):
        """创建独立限流器但使用同一 Redis，捕获安全限流码而不是让异常丢失。"""

        try:
            MinerURateLimiter(settings, client).reserve("submit", pages=1)
            return "accepted"
        except MinerUError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve_one, range(30)))
    assert results.count("accepted") == 10 and results.count("RATE_LIMIT") == 20
    assert client.zcard(settings.mineru_quota_namespace + ":submit:minute") == 10
    assert client.zcard(settings.mineru_quota_namespace + ":submit:day") == 10
    MinerURateLimiter(settings, client).reserve("query")
    assert client.zcard(settings.mineru_quota_namespace + ":query:minute") == 1


def test_real_redis_files_daily_limit_and_priority_is_soft(quota_context):
    """批量按两份文件记账；优先级页数超阈值不拒绝，日文件额度才是硬限制。"""

    settings, client = quota_context
    settings = settings.model_copy(update={"mineru_daily_file_limit": 3})
    limiter = MinerURateLimiter(settings, client)
    assert limiter.reserve("submit", files=2, pages=1001)
    with pytest.raises(MinerUError) as error:
        limiter.reserve("submit", files=2)
    assert error.value.code == "DAILY_LIMIT" and not error.value.retryable
    assert client.zcard(settings.mineru_quota_namespace + ":submit:day") == 2
    assert client.zcard(settings.mineru_quota_namespace + ":submit:minute") == 2


def test_real_redis_rolling_window_removes_old_members(quota_context):
    """过期分钟及 24 小时记录被原子移除，不依赖测试 sleep 或 Worker 本机时钟。"""

    settings, client = quota_context
    seconds, _ = client.time()
    prefix = settings.mineru_quota_namespace
    client.zadd(prefix + ":submit:minute", {"old": seconds - 70})
    client.zadd(prefix + ":submit:day", {"old": seconds - 86402})
    MinerURateLimiter(settings, client).reserve("submit", files=2)
    assert (
        client.zcard(prefix + ":submit:minute")
        == client.zcard(prefix + ":submit:day")
        == 2
    )
