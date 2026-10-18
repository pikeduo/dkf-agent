"""内部异常只保存固定安全分类，不保留上游异常、SQL或连接信息。"""


class ContestError(Exception):
    """跨层传递固定错误码、HTTP状态和可重试语义，不携带异常原文。"""

    def __init__(self, code: str, http_status: int = 400, *, execution: str = "not_run", repairable: bool = False):
        """execution描述用户SQL是否已送出；repairable只用于明确技术错误的一次修复。"""
        super().__init__(code)
        self.code, self.http_status = code, http_status
        self.execution, self.repairable = execution, repairable
