"""仅传递安全中文说明，不携带 Token、响应原文、签名 URL 或原始 HTTP 异常。"""


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
