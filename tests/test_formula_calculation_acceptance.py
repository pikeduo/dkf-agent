"""阶段29CLI安全门Mock；仅供用户运行，不执行真实数据库/模型/云端验收。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import business_logging, config
from app.services import formula_calculation as service
from scripts import stage29_formula_acceptance as cli
from test_formula_calculation import calculation


@pytest.mark.parametrize("argv", [[], ["--show-content"], ["--input", "PRIVATE_PATH"]])
def test_dry_run_never_reads_files_or_invokes_calculation(argv, monkeypatch, capsys):
    """无执行开关不读输入/配置或触发计算，私有路径不回显。"""
    def forbidden(*args, **kwargs):
        """任何业务入口调用均违反dry-run契约。"""
        pytest.fail("dry-run must not execute")

    monkeypatch.setattr(service, "calculate_formula", forbidden)
    assert cli.main(argv) == 0
    content = capsys.readouterr().out
    assert "DRY-RUN" in content and "PRIVATE" not in content


@pytest.mark.parametrize("content", ["{}", '{"formula":null}', '{"formula":NaN}',
    '{"formula":{},"formula":{}}', "PRIVATE_BAD_JSON"])
def test_invalid_input_is_rejected_before_source_or_engine(content, tmp_path, monkeypatch, capsys):
    """严格JSON/文件契约失败不能进入计算，也不能回显内容。"""
    path = tmp_path / "request.json"
    path.write_text(content, encoding="utf-8")

    def forbidden(*args, **kwargs):
        """无效请求不能触发后端服务。"""
        pytest.fail("invalid input must not execute")

    monkeypatch.setattr(service, "calculate_formula", forbidden)
    assert cli.main(["--input", str(path), "--execute"]) == 2
    output = capsys.readouterr().out
    assert "FORMULA_CALC_INPUT" in output and "PRIVATE" not in output and str(path) not in output


@pytest.mark.parametrize("show", [False, True])
def test_cli_uses_existing_request_and_registered_role_with_explicit_content_gate(show, tmp_path, monkeypatch, capsys):
    """仅调用一次项目入口，默认不显示结果或原式；内容展示需独立开关。"""
    _, request, _ = calculation("a/b", {"a": "1", "b": "2"})
    path = tmp_path / "request.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    calls, roles = [], []

    def settings():
        """避免读取本地密钥/部署配置。"""
        return None

    def configure(value, *, role):
        """只记录集中注册角色，不创建日志文件。"""
        roles.append(role)

    def full_content(**kwargs):
        """显式展示时才输出模拟私有结果，不写报告。"""
        return '{"result":"PRIVATE_RESULT"}'

    def calculate(value):
        """返回只含契约字段的Stub，不计算、不查库。"""
        calls.append(value)
        return SimpleNamespace(status="success", calculation_id=uuid4(), formula=value.formula,
            required_variables=[], variables=value.user_inputs, steps=[None], engine_version="1.14.0",
            calculation_scope="arithmetic_only", semantic_validation="manual_pending", unit_validation="manual_pending",
            model_dump_json=full_content)

    monkeypatch.setattr(config, "get_settings", settings)
    monkeypatch.setattr(business_logging, "configure_logging", configure)
    monkeypatch.setattr(service, "calculate_formula", calculate)
    assert cli.main(["--input", str(path), "--execute"] + (["--show-content"] if show else [])) == 0
    output = capsys.readouterr().out
    assert calls == [request] and roles == ["formula-calculation-cli"]
    assert ("PRIVATE_RESULT" in output) is show and "arithmetic_only" in output


def test_zero_denominator_is_safe_failure_without_retry(tmp_path, monkeypatch, capsys):
    """CLI保留除零分类且只执行一次，不输出参数/表达式或半份结果。"""
    _, request, _ = calculation("a/b", {"a": "1", "b": "0"})
    path = tmp_path / "request.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    calls = []

    def settings():
        """不读取真实配置。"""
        return None

    def configure(*args, **kwargs):
        """不创建日志输出。"""
        return None

    def calculate(value):
        """仅模拟安全除零失败，不求值。"""
        calls.append(value)
        raise service.FormulaCalculationError("FORMULA_CALC_ZERO_DIVISION")

    monkeypatch.setattr(config, "get_settings", settings)
    monkeypatch.setattr(business_logging, "configure_logging", configure)
    monkeypatch.setattr(service, "calculate_formula", calculate)
    assert cli.main(["--input", str(path), "--execute"]) == 1
    output = capsys.readouterr().out
    assert len(calls) == 1 and "FORMULA_CALC_ZERO_DIVISION" in output and "PRIVATE" not in output
