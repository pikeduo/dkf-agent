"""Office新输入的显式费用预算与只读预览回归；只用内存ORM/文件哨兵，用户手动执行。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models import Document, DocumentParseJob
from app.services import cache_claims, document_management as management
from app.services.mineru import office_pdf, office_recovery as tool
from app.services.mineru.errors import MinerUError


@pytest.fixture
def context(tmp_path, monkeypatch):
    """冻结一个已上传远端失败的原Whole Job，不读取真实Office或创建业务记录。"""
    document = Document(doc_id=uuid4(), kb_id=uuid4(), file_hash="a" * 64, file_name="original.docx",
        file_path="unused.docx", file_type="docx", status="FAILED", processing_task_id=uuid4(),
        management_task_id=None, management_config=None, chunk_task_id=None, embedding_task_id=None, index_task_id=None)
    source = DocumentParseJob(job_id=uuid4(), doc_id=document.doc_id, file_hash=document.file_hash,
        task_id=document.processing_task_id, provider="mineru_cloud", state="failed", is_active=False,
        batch_id="original-batch", data_id="original-data", options_hash="b" * 64,
        submission_attempted=True, upload_complete=True, error_code="ROUTED_REMOTE_FAILED", parse_options={})
    monkeypatch.setattr(tool, "check_source_hash", lambda *args: None)
    monkeypatch.setattr(tool, "load_raw_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(management, "active_job", lambda *args: None)
    monkeypatch.setattr(tool, "unsubmitted_job", lambda *args: None)
    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    return SimpleNamespace(document=document, source=source, settings=settings)


class Session:
    """仅提供准备授权所需的查询/flush，不连接数据库或发任务。"""

    def __init__(self, source):
        """保存原收费任务和计数器。"""
        self.source, self.flushes = source, 0

    def scalar(self, statement):
        """返回原收费Job；文档行由调用者行锁语义提供。"""
        return self.source

    def get(self, entity, identifier):
        """只按明确原Job身份查询，拒绝意外访问其他对象。"""
        assert entity is DocumentParseJob and identifier == self.source.job_id
        return self.source

    def flush(self):
        """仅记录检查点变化，不进行真实SQL提交。"""
        self.flushes += 1


def test_explicit_recovery_creates_new_local_generation_but_keeps_charged_source(context):
    """正数预算只授权新的转换输入，旧Batch/Job和原件Hash完全保持不变。"""
    c = context
    before = deepcopy({key: value for key, value in vars(c.source).items() if key != "_sa_instance_state"})
    task = c.document.processing_task_id
    manager, dispatch = management.prepare_management(Session(c.source), c.document, c.settings,
        "retry", office_pdf_max_new_parts=3)
    assert dispatch and manager == c.document.management_task_id and c.document.processing_task_id != task
    permission = c.document.management_config["office_pdf_recovery"]
    assert permission["source_job_id"] == str(c.source.job_id) and permission["max_new_parts"] == 3
    assert c.document.status == "UPLOADED" and c.document.management_config["parse_mode"] == "mineru"
    assert {key: value for key, value in vars(c.source).items() if key != "_sa_instance_state"} == before
    assert tool.checked_authorization(Session(c.source), c.document, c.settings) == permission


@pytest.mark.parametrize("damage", ["active", "hash", "not_uploaded", "existing_zip", "split", "native", "zero_budget"])
def test_recovery_rejects_unsafe_or_unnecessary_new_cloud_input(context, monkeypatch, damage):
    """已有ZIP/费用不确定/来源不符均拒绝，正数预算不是Force或安全校验豁免。"""
    c, maximum = context, 3
    if damage == "active":
        c.source.is_active = True
    elif damage == "hash":
        c.source.file_hash = "c" * 64
    elif damage == "not_uploaded":
        c.source.upload_complete = False
    elif damage == "existing_zip":
        monkeypatch.setattr(tool, "load_raw_result", lambda *args, **kwargs: (b"LOCAL", {}))
    elif damage == "split":
        c.source.parse_options = {"_pdf_split": {}}
    elif damage == "native":
        c.document.file_type = "txt"
    else:
        maximum = 0
    with pytest.raises(management.ManagementError) as error:
        tool.source_permission(c.document, c.source, c.settings, maximum)
    assert error.value.code == ("OFFICE_PDF_RAW_AVAILABLE" if damage == "existing_zip" else "OFFICE_PDF_RECOVERY")


def test_cpu_authorization_error_has_safe_stage_code_not_unknown(context):
    """管理许可被改后CPU保留明确OFFICE_PDF_IDENTITY，不向最外层抛通用UNKNOWN。"""
    c = context
    management.prepare_management(Session(c.source), c.document, c.settings, "retry", office_pdf_max_new_parts=3)
    c.document.management_config["office_pdf_recovery"]["source_hash"] = "c" * 64
    with pytest.raises(MinerUError) as error:
        tool.checked_authorization(Session(c.source), c.document, c.settings)
    assert error.value.code == "OFFICE_PDF_IDENTITY"


def test_budget_parameter_is_not_an_alternative_to_force_reparse(context):
    """新输入许可只允许Retry且与旧片续交开关互斥。"""
    c = context
    for operation, additional in [("reparse", {}), ("retry", {"allow_new_split_parts": True})]:
        with pytest.raises(management.ManagementError) as error:
            management.prepare_management(Session(c.source), c.document, c.settings, operation,
                office_pdf_max_new_parts=3, **additional)
        assert error.value.code == "OFFICE_PDF_RECOVERY"


def test_claim_bypass_excludes_only_the_authorized_failed_source(context, monkeypatch):
    """授权只排除一个已验证的源Job，其他活动/收费历史仍阻止重复费用。"""
    c = context
    value = {"file_hash": c.document.file_hash}
    monkeypatch.setattr(cache_claims, "lock_keys", lambda *args: None)

    class Claims:
        """模拟两次历史查询，不执行SQL。"""

        def __init__(self, rows):
            """冻结候选收费历史。"""
            self.rows = rows

        def scalars(self, statement):
            """保持SQLAlchemy all接口，结果完全由本用例配置。"""
            return SimpleNamespace(all=lambda: self.rows)

    with pytest.raises(MinerUError) as error:
        cache_claims.claim_miss(Claims([c.source]), c.document, value, [], force_reparse=False)
    assert error.value.code == "CACHE_REPARSE_REQUIRED"
    cache_claims.claim_miss(Claims([c.source]), c.document, value, [], force_reparse=False,
        authorized_office_source=c.source.job_id)
    other = SimpleNamespace(job_id=uuid4(), is_active=False, batch_id="other-batch", submission_attempted=True, parse_options={})
    with pytest.raises(MinerUError) as error:
        cache_claims.claim_miss(Claims([c.source, other]), c.document, value, [], force_reparse=False,
            authorized_office_source=c.source.job_id)
    assert error.value.code == "CACHE_REPARSE_REQUIRED"


def test_unsubmitted_job_without_original_permission_cannot_adjust_budget(context, monkeypatch):
    """普通上传准备失败没有原Whole许可时拒绝预算调整，不抛KeyError或复用错误Job。"""
    c = context
    pending = SimpleNamespace(parse_options={"_office_pdf_request": {}})
    monkeypatch.setattr(tool, "unsubmitted_job", lambda *args: pending)
    with pytest.raises(management.ManagementError) as error:
        management.prepare_management(Session(c.source), c.document, c.settings, "retry", office_pdf_max_new_parts=2)
    assert error.value.code == "OFFICE_PDF_RECOVERY"


def test_preview_default_does_not_convert_query_cloud_or_change_job(context, monkeypatch, tmp_path):
    """本地规划默认dry-run仅检查依赖身份，--prepare才允许转换，绝不操作原Job。"""
    from scripts import office_pdf_plan as cli

    c = context
    monkeypatch.setattr(office_pdf, "conversion_request", lambda *args: {"identity": {}})
    monkeypatch.setattr(office_pdf, "ensure_conversion", lambda *args: pytest.fail("默认预览不能转换"))
    report = cli.plan_document(c.document, c.settings, tmp_path / "preview")
    assert report["status"] == "DRY_RUN" and not report["conversion_prepared"]
    assert not report["cloud_submitted"] and not report["database_modified"] and not report["business_recovered"]
    assert not (tmp_path / "preview").exists() and c.source.batch_id == "original-batch"


def test_preview_reports_only_new_results_paths(tmp_path, monkeypatch):
    """报告不能越界或覆盖旧结果；不把私有原件路径打印到业务日志。"""
    from scripts import office_pdf_plan as cli

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    good = tmp_path / "results/new.json"
    assert cli.report_target(good) == good
    good.parent.mkdir()
    good.write_text("KEEP", encoding="utf-8")
    for target in (good, tmp_path / "outside.json", tmp_path / "results/../outside.json"):
        with pytest.raises(MinerUError) as error:
            cli.report_target(target)
        assert error.value.code == "OFFICE_PDF_PATH"
    assert good.read_text(encoding="utf-8") == "KEEP"
