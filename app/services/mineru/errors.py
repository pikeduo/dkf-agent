"""仅传递安全中文说明，不携带 Token、响应原文、签名 URL 或原始 HTTP 异常。"""

import re
from pathlib import Path

PDF_SPLIT_ERROR_CODES = frozenset({"PDF_SPLIT_CHECKPOINT", "PDF_SPLIT_PATH", "PDF_SPLIT_LIMIT",
    "PDF_SPLIT_CHANGED", "PDF_SPLIT_INVALID", "PDF_SPLIT_GEOMETRY", "PDF_SPLIT_SINGLE_PAGE",
    "PDF_SPLIT_IO", "PDF_SPLIT_WRITE", "PDF_SPLIT_RESULT", "PDF_SPLIT_INCOMPLETE", "PDF_SPLIT_PAGE", "PDF_SPLIT_MISSING"})
OFFICE_PDF_ERROR_CODES = frozenset({"OFFICE_PDF_PATH", "OFFICE_PDF_DEPENDENCY", "OFFICE_PDF_IDENTITY",
    "OFFICE_PDF_CHANGED", "OFFICE_PDF_TIMEOUT", "OFFICE_PDF_FAILED", "OFFICE_PDF_INVALID", "OFFICE_PDF_BUDGET"})

# 这些错误发生在原ZIP取得之后；普通Retry只能重放本地原件，不能据此再次访问云端。
LOCAL_ADAPTER_ERROR_CODES = frozenset({"MERGE_EVIDENCE", "EMPTY_BLOCK", "CONTENT_TYPE",
    "CONTENT_LIST", "BBOX", "PAGE", "GEOMETRY", "NO_TEXT", "COMPLETENESS",
    "LOCAL_RESULT_REQUIRED", "CONTENT_LIMIT", "CACHE_REPARSE_REQUIRED", "CACHE_CONFIG_CHANGED", "CACHE_INVALID",
    "LOCAL_PIPELINE_ERROR"})


class MinerUError(Exception):
    """供应商错误分类及建议延迟；消息只能来自受控映射。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        """保存安全错误码和重试属性，不保留包含凭据的底层 request/response。"""

        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


def internal_failure(exc, code, message, *, retryable=False):
    """保留异常类型和项目调用栈，不保存异常消息、局部变量、源码正文或第三方敏感堆栈。"""
    from app.core.config import PROJECT_ROOT
    error = MinerUError(code, message, retryable=retryable)
    kind = type(exc).__name__
    kind = kind if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind) else "Exception"
    frames, current = [], exc.__traceback__
    while current is not None:
        frame = current.tb_frame
        try:
            relative = Path(frame.f_code.co_filename).resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except (OSError, ValueError):
            relative = ""
        function = frame.f_code.co_name
        if (re.fullmatch(r"(?:app|scripts)/[A-Za-z0-9_/]+\.py", relative)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}|<module>|<lambda>|<listcomp>", function)):
            frames.append({"source_file": relative, "source_function": function, "source_line": current.tb_lineno})
        current = current.tb_next
    error.internal_diagnostic = {"error_type": kind, "frames": frames[-16:]}
    return error


RETRY_CODES = {
    "-10001",
    "-60001",
    "-60007",
    "-60008",
    "-60009",
    "-60010",
    "-60020",
    "-60021",
    "-60022",
}
MESSAGES = {
    "A0202": "MinerU Token 错误，请检查本地配置",
    "A0211": "MinerU Token 失效，请人工更换本地 Token",
    "-60003": "MinerU 无法读取文件，请检查原件是否损坏",
    "-60004": "MinerU 不接受空文件",
    "-60005": "MinerU 文件大小超过 200 MB",
    "-60006": "MinerU 文件页数超过 200 页",
    "-60013": "没有权限访问该 MinerU 任务",
    "-60018": "MinerU 每日任务数量已达到上限，请稍后人工重试",
    "-60019": "MinerU HTML 配额不足",
}


def api_error(code: object) -> MinerUError:
    """按官方错误码分类，不直接展示供应商 msg 或 err_msg。"""

    value = str(code)
    known = (
        value in RETRY_CODES
        or value in MESSAGES
        or value
        in {
            "-500",
            "-10002",
            "-60002",
            "-60011",
            "-60012",
            "-60014",
            "-60015",
            "-60016",
            "-60017",
        }
    )
    safe_code = value if known else "UPSTREAM_ERROR"
    return MinerUError(
        safe_code,
        MESSAGES.get(
            value,
            "MinerU 服务暂时不可用"
            if value in RETRY_CODES
            else "MinerU 请求失败，请检查格式、参数或任务权限",
        ),
        retryable=value in RETRY_CODES,
    )
