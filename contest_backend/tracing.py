"""统一Trace只采集实际步骤与安全指标，绝不序列化业务输入/输出。"""

from contextlib import contextmanager
from time import perf_counter
from uuid import uuid4

from app.core.business_logging import log_event
from contracts.contest_v1 import RunContext, TraceStep
from contest_backend.errors import ContestError


class Trace:
    """每请求独立实例；无进程级last_trace或原始payload日志。"""

    def __init__(self, context: RunContext):
        """绑定服务端分配的上下文，步骤顺序反映实际完成次序。"""
        self.context, self.steps = context, []

    @contextmanager
    def step(self, operation: str, *, tool: str = "sql", attempt: int | None = None):
        """计量真实执行段；调用方仅可补充固定状态/错误码和实际调用次数。"""
        started = perf_counter()
        item = {"status": "succeeded", "error_code": None, "llm_calls": 0}
        try:
            yield item
        except ContestError as error:
            item.update(status="unknown" if error.execution == "unknown" else "failed", error_code=error.code)
            raise
        except Exception:
            item.update(status="failed", error_code="INTERNAL_ERROR")
            raise ContestError("INTERNAL_ERROR", 500) from None
        finally:
            step = TraceStep(step_id=uuid4(), tool=tool, operation=operation, sql_attempt=attempt,
                             elapsed_ms=(perf_counter() - started) * 1000, **item)
            self.steps.append(step)
            log_event("contest.step", request_id=self.context.request_id, agent_run_id=self.context.run_id,
                      trace_id=self.context.trace_id, step=operation, status=step.status,
                      error_code=step.error_code, duration_ms=step.elapsed_ms, item_count=step.llm_calls)

    def safe_snapshot(self, reply_to_run_id=None) -> dict:
        """只保存关联身份和TraceStep，不保存context中的确认值或任何业务结果。"""
        return {"request_id": str(self.context.request_id), "run_id": str(self.context.run_id),
                "trace_id": str(self.context.trace_id), "session_id": str(self.context.session_id),
                "reply_to_run_id": str(reply_to_run_id) if reply_to_run_id else None,
                "trace": [item.model_dump(mode="json") for item in self.steps]}
