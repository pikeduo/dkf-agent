"""本地解析缓存读取/发布/清理互斥；固定锁不随文档删除，禁止共享盘跨主机使用。"""

import errno
import os
from contextlib import contextmanager
from time import perf_counter, sleep

from app.core.business_logging import lock_file


@contextmanager
def cache_guard(root):
    """复用OS锁原语串行全局缓存访问，最多等5秒；失败不绕过锁或静默收费。"""
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / ".cache-cleanup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        deadline = perf_counter() + 5
        while True:
            try:
                lock_file(descriptor)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or perf_counter() >= deadline:
                    raise OSError("解析缓存暂时被占用") from None
                sleep(0.02)
        yield
    finally:
        try:
            if acquired:
                lock_file(descriptor, unlock=True)
        finally:
            os.close(descriptor)
