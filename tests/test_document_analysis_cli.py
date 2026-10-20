"""用户执行的阶段35 CLI用例；默认无IO，执行路径只替换现有报告边界，不解析文档。"""

import json
from types import SimpleNamespace
from uuid import uuid4

from app.core import business_logging as logs
from app.core import config
from app.services import document_assessment as storage
from app.services.document_analysis import analyze_observations
from scripts import analyze_document as cli
from test_document_analysis import block, snapshot


def test_default_dry_run_does_not_construct_service_or_create_report(monkeypatch, tmp_path, capsys):
    """预览不构造业务服务、读配置/输入、连接数据库或创建目录。"""
    def forbidden(*args, **kwargs):
        """dry-run触达执行边界即违反约定。"""
        raise AssertionError("不得执行")

    monkeypatch.setattr(storage, "DocumentAssessmentService", forbidden)
    monkeypatch.setattr(config, "get_settings", forbidden)
    path = tmp_path / "not-created" / "report.json"
    assert cli.main(["--output", str(path)]) == 0 and not path.parent.exists()
    assert "DRY-RUN" in capsys.readouterr().out


def test_explicit_execute_selects_existing_candidate_and_preserves_report_history(monkeypatch, tmp_path, capsys):
    """真实候选选择参数和来源ID贯穿，报告只排他创建，重复执行不覆盖旧文件。"""
    doc = snapshot()
    report = analyze_observations(doc, [block(1, "1 PRIVATE_TITLE", kind="title")])
    calls, roles = [], []

    def choose(kb_id, *, case_name):
        """模拟已有库候选，不查询数据库或产生假业务文档。"""
        calls.append((kb_id, case_name))
        return doc.doc_id

    def analyze(payload):
        """仅返回派生报告，验证调用的实际doc/kb范围。"""
        assert payload.doc_id == doc.doc_id and payload.kb_id == doc.kb_id and not payload.visual_pages
        return report

    monkeypatch.setattr(storage, "DocumentAssessmentService", lambda: SimpleNamespace(choose_document=choose, analyze=analyze))
    monkeypatch.setattr(config, "get_settings", lambda: object())
    monkeypatch.setattr(logs, "configure_logging", lambda settings, *, role: roles.append(role))
    path = tmp_path / "report.json"
    argv = ["--kb-id", str(doc.kb_id), "--case", "outline", "--execute", "--output", str(path)]
    assert cli.main(argv) == 0
    before = path.read_bytes()
    assert json.loads(before)["doc_id"] == str(doc.doc_id) and calls == [(doc.kb_id, "outline")]
    assert roles == ["document-analysis-cli"] and "PRIVATE_TITLE" not in capsys.readouterr().out
    assert cli.main(argv) == 2 and path.read_bytes() == before and len(calls) == 1


def test_invalid_parameters_fail_before_database_or_report_io(tmp_path):
    """缺少知识库或非法页数不允许执行只读查询，更不会创建报告。"""
    output = tmp_path / "report.json"
    assert cli.main(["--execute", "--output", str(output)]) == 2
    assert cli.main(["--execute", "--kb-id", str(uuid4()), "--visual-pages", "1", "2", "3", "4", "5", "6"]) == 2
    assert cli.main(["--execute", "--kb-id", str(uuid4()), "--visual-pages", "0"]) == 2
    assert cli.main(["--execute", "--kb-id", str(uuid4()), "--visual-pages", "1", "1"]) == 2
    assert not output.exists()
