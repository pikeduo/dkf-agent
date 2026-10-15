"""阶段28CLI安全门Mock，只编写供用户运行；不生成/解析真实文档或访问数据库。"""

import json
from types import SimpleNamespace

import pytest

from app.core import business_logging, config
from app.services import formula_binding as service
from scripts import stage28_formula_acceptance as cli
from test_formula_binding import prepared, user


@pytest.mark.parametrize("argv", [[], ["--show-content"], ["--input", "PRIVATE_PATH"]])
def test_preview_does_not_read_input_or_invoke_business(monkeypatch, capsys, argv):
    """无execute时不读取正文/配置、不查来源，不输出私有输入路径。"""
    def forbidden(*args, **kwargs):
        """任何预览期间业务调用都应失败，不执行实际验收。"""
        pytest.fail("dry-run cannot invoke business")

    monkeypatch.setattr(service, "bind_formula_variables", forbidden)
    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    assert "DRY-RUN" in output and "PRIVATE_PATH" not in output


@pytest.mark.parametrize("content", ["{}", '{"formula":null}', '{"formula":NaN}',
    '{"formula":{},"formula":{}}', "PRIVATE_BAD_JSON"])
def test_bad_input_is_safely_rejected_before_source_query(tmp_path, monkeypatch, capsys, content):
    """无效/重复字段/非有限JSON不修补，不在报错回显原始值或路径。"""
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")
    calls = []

    def forbidden(*args, **kwargs):
        """记录是否错误地进入业务边界；不查库或加载模型。"""
        calls.append(kwargs)

    monkeypatch.setattr(service, "bind_formula_variables", forbidden)
    assert cli.main(["--input", str(path), "--execute"]) == 2
    output = capsys.readouterr().out
    assert calls == [] and "FORMULA_BINDING_INPUT" in output and "PRIVATE" not in output and str(path) not in output


@pytest.mark.parametrize("show", [False, True])
def test_explicit_cli_reads_existing_definition_once_with_registered_role(tmp_path, monkeypatch, capsys, show):
    """明确执行才绑定，默认只输出缺参摘要；无重新提取公式/付费请求的代码路径。"""
    _, _, formula, _ = prepared()
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"formula": formula.model_dump(mode="json"),
        "user_inputs": [user("profit", "20")]}), encoding="utf-8")
    calls, roles = [], []

    def configure(settings, *, role):
        """不创建真实日志文件，只验证集中注册角色名称。"""
        roles.append(role)

    def bind(definition, **kwargs):
        """捕获绑定一次，不调用来源、LLM或计算服务。"""
        calls.append((definition, kwargs))
        return SimpleNamespace(status="need_variables", formula=definition,
            variables=[None], required_variables=["revenue"], semantic_validation="manual_pending",
            unit_validation="manual_pending", calculation="not_performed", model_dump_json=content_json)

    def content_json(**kwargs):
        """只有显式show-content才允许向可信终端显示正文。"""
        return '{"private":"PRIVATE_FORMULA_VALUE"}'

    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(business_logging, "configure_logging", configure)
    monkeypatch.setattr(service, "bind_formula_variables", bind)
    assert cli.main(["--input", str(path), "--execute"] + (["--show-content"] if show else [])) == 1
    output = capsys.readouterr().out
    assert len(calls) == 1 and calls[0][0] == formula
    assert calls[0][1]["user_inputs"][0].value == "20" and roles == ["formula-binding-cli"]
    assert ("PRIVATE_FORMULA_VALUE" in output) is show
    assert '"required_variables": ["revenue"]' in output and "not_performed" in output


def test_source_error_has_safe_code_and_no_retry(tmp_path, monkeypatch, capsys):
    """来源错误保留固定代码，不变成need_variables、不重试或回显异常原文。"""
    _, _, formula, _ = prepared()
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"formula": formula.model_dump(mode="json")}), encoding="utf-8")
    calls = []

    def bind(*args, **kwargs):
        """仅模拟安全来源失败。"""
        calls.append(True)
        raise service.FormulaBindingError("FORMULA_BINDING_SOURCE")

    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(business_logging, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "bind_formula_variables", bind)
    assert cli.main(["--input", str(path), "--execute"]) == 1
    assert calls == [True] and "FORMULA_BINDING_SOURCE" in capsys.readouterr().out
