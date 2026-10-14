"""只供测试核对中文文本日志，生产业务不依赖日志反向解析。"""

import json
from datetime import datetime

from app.core.business_logging import FIELD_LABELS, ROLE_LABELS


def read_log_line(line: str) -> dict:
    """复用集中角色表解析固定前缀/标量，不另建API/Worker专用映射或接受任意来源。"""

    timestamp, level, role, description, event, pid, *fields = line.split(" | ")
    datetime.fromisoformat(timestamp)
    assert description and event.startswith("event=") and pid.startswith("pid=")
    result = {
        "timestamp": timestamp,
        "level": level,
        "role": {label: name for name, label in ROLE_LABELS.items()}[role],
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
