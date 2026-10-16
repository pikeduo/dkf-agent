"""阶段22脚本入口Mock，不启动模型/数据库，也不执行真实收费API。"""

import sys

import pytest

from app.core.config import Settings
from app.services.llm import LLMError
from app.services.rag.synthesis import RAGError, insufficient_answer
from scripts import stage22_rag_acceptance as tool


@pytest.mark.parametrize("provider,fallback", [("deepseek", False), ("custom", False), ("custom", True)])
def test_existing_entry_uses_selected_configuration_and_one_allowed_query(provider, fallback, monkeypatch, capsys):
    """旧入口及显式备用预检可用；预检不联网，逻辑问答由Stub替代并提示潜在两次收费。"""
    from app.core import config, business_logging
    from app.services.rag import query
    settings = Settings(_env_file=None, llm_provider=provider, deepseek_provider=None,
        llm_fallback_enabled=fallback, llm_fallback_provider="deepseek",
        deepseek_api_key="official-fake" if provider == "deepseek" or fallback else "",
        deepseek_api_base_url="https://api.deepseek.com",
        custom_llm_api_key="custom-fake" if provider == "custom" else "",
        custom_llm_api_base_url="https://custom.example/v1")
    calls = []

    def respond(kb_id, question, **kwargs):
        """明确记录外传保护步骤与允许步骤，绝不触发实际RAG业务。"""
        calls.append(kwargs)
        if kwargs["allow_cloud"] is False:
            raise RAGError("RAG_CLOUD_PERMISSION")
        return insufficient_answer()

    monkeypatch.chdir(tool.Path(__file__).resolve().parents[1])
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(business_logging, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(query, "query_knowledge_base", respond)
    monkeypatch.setattr(sys, "argv", ["stage22_rag_acceptance", "--question", "PRIVATE_QUESTION"])
    assert tool.main() == 0
    assert [item["allow_cloud"] for item in calls] == [False, True]
    output = capsys.readouterr().out
    assert f"LLM_PROVIDER = {provider}" in output and "STAGE22_STRUCTURE_PASS" in output
    assert ("FALLBACK_PROVIDER = deepseek" in output) is fallback
    assert not any(value in output for value in ("PRIVATE_QUESTION", "official-fake", "custom-fake", "custom.example"))


def test_acceptance_config_failure_stops_before_any_rag_query(monkeypatch, capsys):
    """所选提供方配置错误只输出安全类别，不运行检索或尝试其他提供方。"""
    from app.core import config
    from app.services.rag import query
    import app.services.llm as llm
    calls = []

    def bad_service(settings):
        """模拟工厂配置错误，不请求云端。"""
        raise LLMError("LLM_CONFIG", provider="custom")

    monkeypatch.chdir(tool.Path(__file__).resolve().parents[1])
    monkeypatch.setattr(config, "get_settings", lambda: object())
    monkeypatch.setattr(llm, "create_llm_service", bad_service)
    monkeypatch.setattr(query, "query_knowledge_base", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["stage22_rag_acceptance", "--question", "PRIVATE_QUESTION"])
    assert tool.main() == 2 and calls == []
    output = capsys.readouterr().out
    assert "LLM_CONFIG" in output and "PRIVATE" not in output
