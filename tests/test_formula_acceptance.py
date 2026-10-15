"""公式人工验收CLI的安全门测试；不执行来源查询、模型、真实HTTP或文件生成。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import stage27_formula_acceptance as cli
from app.core import business_logging, config
from app.services import formula as service


@pytest.mark.parametrize("argv", [[], ["--allow-cloud"], ["--show-content"], ["--kb-id", "PRIVATE_INPUT"]])
def test_default_dry_run_does_not_invoke_business_services(monkeypatch, capsys, argv):
    """无execute时只显示安全摘要，不要求配置/数据库，连无效ID也不外传或回显。"""
    def forbidden(*args, **kwargs):
        """真实业务入口一旦触发即失败，不执行任何验收。"""
        pytest.fail("dry-run不允许执行")

    monkeypatch.setattr(service, "extract_formula", forbidden)
    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    assert "DRY-RUN" in output and "PRIVATE_INPUT" not in output


def test_execute_without_cloud_permission_fails_before_business_imports(monkeypatch, capsys):
    """执行开关不等于云外传授权，缺许可时在来源/LLM入口之前返回非零。"""
    def forbidden(*args, **kwargs):
        """不允许权限缺失时执行服务。"""
        pytest.fail("未授权不能提取公式")

    monkeypatch.setattr(service, "extract_formula", forbidden)
    assert cli.main(["--execute"]) == 2
    assert "FORMULA_CLOUD_PERMISSION" in capsys.readouterr().out


def test_invalid_or_missing_uuid_never_enters_service(monkeypatch, capsys):
    """显式权限仍不允许用非法/缺失身份查询；错误不能回显私有输入。"""
    calls = []
    monkeypatch.setattr(service, "extract_formula", lambda *args, **kwargs: calls.append(kwargs))
    assert cli.main(["--execute", "--allow-cloud", "--kb-id", "PRIVATE_INPUT",
                     "--chunk-id", str(uuid4()), "--block-id", str(uuid4())]) == 2
    assert "PRIVATE_INPUT" not in capsys.readouterr().out and calls == []


@pytest.mark.parametrize("show_content", [False, True])
def test_explicit_cli_uses_registered_role_and_shows_content_only_when_requested(monkeypatch, capsys, show_content):
    """显式执行只调用一次项目服务；默认只打印摘要，正文显示需独立开关且不写日志文件。"""
    kb_id, doc_id, chunk_id, block_id = (uuid4() for _ in range(4))
    calls, roles = [], []

    def logging(settings, *, role):
        """捕获角色配置，不创建真实运行日志。"""
        roles.append(role)

    def result_json(**kwargs):
        """仅模拟明确同意后的终端内容，不生成数据文件。"""
        assert kwargs == {"indent": 2}
        return json.dumps({"raw_formula": "PRIVATE_FORMULA"})

    def extract(**kwargs):
        """代替业务服务，核对限定身份及精确云许可，不查库或请求模型。"""
        calls.append(kwargs)
        return SimpleNamespace(status="success", formula_id=uuid4(), formula_block_id=block_id, variables=[],
            source=SimpleNamespace(doc_id=doc_id, chunk_id=chunk_id, page_semantics="physical_pdf"),
            semantic_validation="manual_pending", model_dump_json=result_json)

    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(business_logging, "configure_logging", logging)
    monkeypatch.setattr(service, "extract_formula", extract)
    argv = ["--kb-id", str(kb_id), "--doc-id", str(doc_id), "--chunk-id", str(chunk_id),
            "--block-id", str(block_id), "--execute", "--allow-cloud"]
    assert cli.main(argv + (["--show-content"] if show_content else [])) == 0
    assert calls == [{"kb_id": kb_id, "doc_id": doc_id, "chunk_id": chunk_id,
                      "block_id": block_id, "allow_cloud": True}]
    output = capsys.readouterr().out
    assert ("PRIVATE_FORMULA" in output) is show_content
    assert "manual_pending" in output and roles == ["formula-cli"]
