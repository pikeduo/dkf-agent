"""检索数据库异常的固定分类；不读取异常原文、SQL参数或连接信息。"""

import re

from sqlalchemy.exc import SQLAlchemyError

DATABASE_REASONS = {
    "QUERY_CANCELED": "数据库已取消查询，可能为语句超时或人工取消",
    "LOCK_UNAVAILABLE": "数据库未取得所需锁，请检查锁等待",
    "CONNECTION": "数据库连接不可用或已中断",
    "RESOURCE": "数据库资源不足，未返回部分检索结果",
}


def database_error_codes(prefix):
    """为既有服务前缀生成固定错误码，兼容原DATABASE类别与安全消息。"""
    return {f"{prefix}_DATABASE_{reason}": message for reason, message in DATABASE_REASONS.items()}


def database_diagnostic(error):
    """仅取驱动SQLSTATE；57014不武断归为超时，未知/无状态仍保留明确分类。"""
    state = None
    if isinstance(error, SQLAlchemyError):
        original = getattr(error, "orig", None)
        for field in ("sqlstate", "pgcode"):
            value = getattr(original, field, None)
            if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value):
                state = value
                break
    reason = {"57014": "QUERY_CANCELED", "55P03": "LOCK_UNAVAILABLE"}.get(state, "OTHER")
    if state and (state.startswith("08") or state in {"57P01", "57P02", "57P03"}):
        reason = "CONNECTION"
    elif state and state.startswith("53"):
        reason = "RESOURCE"
    return {"reason": reason, "sqlstate": state}
