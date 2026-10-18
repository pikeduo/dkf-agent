"""单一已授权提供方的Chat Completions调用，无隐式重试、重定向或Fallback。"""

import json
from urllib.parse import urlsplit

import httpx

from contest_backend.config import Settings
from contest_backend.errors import ContestError
from contest_backend.state import Grant


class LLMClient:
    """模型只负责生成候选SQL；安全决策不由模型承担。"""

    def __init__(self, settings: Settings, transport=None):
        """生产使用真实HTTP；transport用于编写无网络单元用例，不在本轮执行。"""
        self.settings, self.transport = settings, transport

    def complete(self, messages: list[dict], grant: Grant) -> str:
        """预留保守Token上界后只发一次POST；失败封闭该许可，不回显上游内容。"""
        config = self.settings
        base = urlsplit(config.llm_api_base_url)
        if (base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment
                or not config.llm_api_key.get_secret_value() or not config.llm_model
                or grant.policy.provider_id != config.llm_provider_id):
            raise ContestError("DEPENDENCY_UNAVAILABLE", 503)
        payload = {"model": config.llm_model, "messages": messages, "temperature": 0,
                   "max_tokens": config.llm_max_output_tokens, "stream": False}
        # 不冒称真实Tokenizer：按UTF-8字节的四倍加固定封装余量保守预留，未知用量不释放。
        reserved = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) * 4 + 4096 + config.llm_max_output_tokens
        grant.reserve(reserved)
        try:
            with httpx.Client(timeout=config.llm_timeout_seconds, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                grant.attempted_calls += 1
                with client.stream("POST", config.llm_api_base_url.rstrip("/") + "/chat/completions",
                                   headers={"Authorization": "Bearer " + config.llm_api_key.get_secret_value()}, json=payload) as response:
                    if response.status_code != 200:
                        raise ValueError("上游失败")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 262144:
                            raise ValueError("响应超限")
                        chunks.append(chunk)
            body = json.loads(b"".join(chunks))
            choice = body["choices"][0]
            text = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("模型结果不完整")
            usage_data = body.get("usage")
            usage = usage_data.get("total_tokens") if isinstance(usage_data, dict) else None
            if type(usage) is not int or usage <= 0 or usage > reserved:
                # 允许使用已完成的本次结果，但阻断后续额外请求，不伪造真实Token用量。
                grant.uncertain = True
            return text
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            grant.uncertain = True
            raise ContestError("MODEL_FAILURE", 502) from None
