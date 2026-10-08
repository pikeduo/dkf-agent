"""显式运行的 Redis/Worker/API 联调，不会被默认 pytest 自动收集。"""

import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from uuid import uuid4

import httpx
import redis

from app.core.celery_app import celery_app
from app.core.config import PROJECT_ROOT
from app.tasks.demo import add


def test_real_queues_and_result_api(tmp_path):
    """借用已运行 Redis；只管理本测试启动的进程，不修改系统服务。"""

    with redis.Redis.from_url(celery_app.conf.broker_url) as connection:
        assert connection.ping()
    with redis.Redis.from_url(celery_app.conf.result_backend) as connection:
        assert connection.ping()

    processes = []
    worker_names = []
    task_ids = []
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    run_id = uuid4().hex[:8]

    def wait_until(check, description):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if check():
                return
            assert all(process.poll() is None for process in processes), (
                "测试进程异常退出"
            )
            time.sleep(0.2)
        raise AssertionError(f"等待超时：{description}")

    with ExitStack() as stack:

        def start_process(name, args):
            logfile = stack.enter_context(
                (tmp_path / f"{name}.log").open("w", encoding="utf-8")
            )
            process = subprocess.Popen(
                [sys.executable, "-m", *args],
                cwd=PROJECT_ROOT,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            processes.append(process)

        try:
            for queue, pool, concurrency in (
                ("default_queue", "threads", "2"),
                ("gpu_queue", "solo", "1"),
            ):
                name = f"smoke-{queue}-{run_id}@localhost"
                worker_names.append(name)
                start_process(
                    queue,
                    [
                        "celery",
                        "-A",
                        "app.core.celery_app:celery_app",
                        "worker",
                        "-Q",
                        queue,
                        "--pool",
                        pool,
                        "--concurrency",
                        concurrency,
                        "--hostname",
                        name,
                        "--loglevel",
                        "WARNING",
                    ],
                )
                wait_until(
                    lambda name=name: celery_app.control.ping(
                        destination=[name], timeout=0.5
                    ),
                    f"{queue} Worker 就绪",
                )
                queues = celery_app.control.inspect(
                    destination=[name], timeout=1
                ).active_queues()
                assert queues and [item["name"] for item in queues[name]] == [queue]

            # 临时端口避免影响用户可能已经运行的 8000 服务。
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            start_process(
                "api",
                [
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
            )

            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:

                def api_ready():
                    try:
                        return client.get("/health").status_code == 200
                    except httpx.ConnectError:
                        return False

                wait_until(api_ready, "FastAPI 就绪")
                assert client.get("/docs").status_code == 200

                for queue in ("default_queue", "gpu_queue"):
                    submitted = client.post(
                        "/tasks/add", json={"x": 2, "y": 3, "queue": queue}
                    )
                    assert submitted.status_code == 202
                    task_id = submitted.json()["task_id"]
                    task_ids.append(task_id)
                    wait_until(
                        lambda task_id=task_id: (
                            client.get(f"/tasks/{task_id}").json()["status"]
                            == "SUCCESS"
                        ),
                        f"{queue} 任务执行",
                    )
                    assert client.get(f"/tasks/{task_id}").json()["result"] == 5

                # 验证直接投递的无效参数被 Worker 记录为 FAILURE，API 不暴露异常细节。
                failed = add.apply_async(args=("invalid", 3))
                task_ids.append(failed.id)
                wait_until(
                    lambda: (
                        client.get(f"/tasks/{failed.id}").json()["status"] == "FAILURE"
                    ),
                    "失败任务记录",
                )
                assert client.get(f"/tasks/{failed.id}").json()["error"]
                assert celery_app.backend.get_task_meta(failed.id)["traceback"]

        finally:
            # 只清理本次任务结果和独立命名的进程，绝不清空 Redis 或共享队列。
            for task_id in task_ids:
                celery_app.AsyncResult(task_id).forget()
            if worker_names:
                celery_app.control.broadcast("shutdown", destination=worker_names)
            for process in reversed(processes):
                if process.poll() is None:
                    if process is processes[-1]:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
