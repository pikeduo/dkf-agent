"""Agent CLI安全门Mock；不构造真实Graph或请求数据库、模型、云服务。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents import knowledge
from app.core import business_logging, config
from app.schemas.knowledge_agent import KnowledgeAgentRequest
from scripts import stage30_agent_acceptance as cli


@pytest.mark.parametrize("argv", [[], ["--show-content"], ["--input", "PRIVATE_PATH"]])
def test_dry_run_never_reads_configuration_or_invokes_agent(argv, monkeypatch, capsys):
    """无execute开关不读取文件/配置、不构造图，路径和内容不回显。"""
    def forbidden(*args, **kwargs):
        """误调用真实入口即暴露安全门缺陷。"""
        pytest.fail("dry-run must not execute")

    monkeypatch.setattr(knowledge, "run_knowledge_agent", forbidden)
    monkeypatch.setattr(config, "get_settings", forbidden)
    assert cli.main(argv) == 0
    content = capsys.readouterr().out
    assert "DRY-RUN" in content and "PRIVATE" not in content


@pytest.mark.parametrize("content", ["{}", '{"kb_id":NaN}', '{"question":"a","question":"b"}', "PRIVATE_INVALID"])
def test_invalid_input_never_invokes_agent(content, tmp_path, monkeypatch, capsys):
    """坏JSON/重复键/契约错误不能调用图，也不能打印请求及本机路径。"""
    path = tmp_path / "agent.json"
    path.write_text(content, encoding="utf-8")

    def forbidden(*args, **kwargs):
        """坏输入不能走入业务入口。"""
        pytest.fail("invalid request must not execute")

    monkeypatch.setattr(knowledge, "run_knowledge_agent", forbidden)
    assert cli.main(["--input", str(path), "--execute"]) == 2
    output = capsys.readouterr().out
    assert "AGENT_INPUT" in output and "PRIVATE" not in output and str(path) not in output


@pytest.mark.parametrize("show,status,code", [(False, "success", 0), (True, "success", 0), (False, "failed", 1), (False, "need_variables", 1)])
def test_cli_calls_once_uses_registered_role_and_requires_content_permission(show, status, code, tmp_path, monkeypatch, capsys):
    """执行仅一次；摘要无正文，完整内容须独立开关，失败/缺参不当成通过。"""
    request = KnowledgeAgentRequest(kb_id=uuid4(), question="PRIVATE_QUERY", intent="knowledge_search")
    path = tmp_path / "agent.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    calls, roles = [], []

    def settings():
        """避免读取部署配置或密钥。"""
        return None

    def configure(value, *, role):
        """只捕获角色，不创建日志文件。"""
        roles.append(role)

    def dump(**kwargs):
        """仅用户显式允许展示时返回模拟正文。"""
        return '{"answer":"PRIVATE_ANSWER"}'

    def run(value):
        """只返回摘要Stub，不调用Graph、模型或业务服务。"""
        calls.append(value)
        return SimpleNamespace(agent_run_id=uuid4(), status=status, selected_route="knowledge_search",
            step_count=5, error_code="AGENT_TOOL" if status == "failed" else None, model_dump_json=dump)

    monkeypatch.setattr(config, "get_settings", settings)
    monkeypatch.setattr(business_logging, "configure_logging", configure)
    monkeypatch.setattr(knowledge, "run_knowledge_agent", run)
    assert cli.main(["--input", str(path), "--execute"] + (["--show-content"] if show else [])) == code
    output = capsys.readouterr().out
    assert calls == [request] and roles == ["knowledge-agent-cli"]
    assert ("PRIVATE_ANSWER" in output) is show and "PRIVATE_QUERY" not in output
