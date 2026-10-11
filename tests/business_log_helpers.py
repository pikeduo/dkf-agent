"""只供测试核对中文文本日志，生产业务不依赖日志反向解析。"""

import json
from datetime import datetime

from app.core.business_logging import FIELD_LABELS


def read_log_line(line: str) -> dict:
    """解析固定前缀及引用过的标量，验证完整单行并还原原事件字段用于回归断言。"""

    timestamp, level, role, description, event, pid, *fields = line.split(" | ")
    datetime.fromisoformat(timestamp)
    assert description and event.startswith("event=") and pid.startswith("pid=")
    result = {
        "timestamp": timestamp,
        "level": level,
        "role": {"API": "api", "Worker": "worker", "未知进程": "unknown"}[role],
        "event": event.removeprefix("event="),
        "pid": int(pid.removeprefix("pid=")),
    }
    labels = {label: key for key, label in FIELD_LABELS.items()}
    for field in fields:
        label, value = field.split("=", 1)
        key = labels[label]
        assert key not in result
        result[key] = json.loads(value)
    return result
