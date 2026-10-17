"""本地原ZIP重放边界：纯Mock用例，由用户执行，不访问云端、数据库或模型。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.services import document_management as management
from app.services.mineru import jobs, provider, raw_results
from app.services.mineru.errors import LOCAL_ADAPTER_ERROR_CODES, MinerUError
from app.services.parse_cache import NORMALIZER_VERSION
from app.tasks import mineru as tasks
from test_mineru_recovery import context


def forbid_cloud(*args, **kwargs):
    """本地重放只要尝试创建客户端或执行HTTP就失败，不把Stub当成真实云验收。"""
    raise AssertionError("本地重放不得访问云端")


def stub_local_conversion(monkeypatch, document, job):
    """只替换归档/几何/持久化边界，保留被测任务编排，不读业务文件或写数据库。"""
    saved = b"immutable-raw-zip-stub", {"sha256": "c" * 64}
    parsed = SimpleNamespace(blocks=["verified-block-stub"])
    monkeypatch.setattr(jobs, "load_raw_result", lambda *args, **kwargs: saved)
    monkeypatch.setattr(raw_results, "load_raw_result", lambda *args, **kwargs: saved)
    monkeypatch.setattr(jobs, "local_file", lambda *args: SimpleNamespace(path="NOT_READ", file_hash=job.file_hash))
    monkeypatch.setattr(jobs, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(provider, "validate_local_file", lambda *args: None)
    monkeypatch.setattr(jobs, "page_geometry", lambda *args: {})
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", lambda *args, **kwargs: parsed)
    monkeypatch.setattr(jobs, "save_cloud_cache", lambda *args: None)
    monkeypatch.setattr(jobs, "obtain_raw_result", forbid_cloud)
    monkeypatch.setattr(tasks, "MinerUCloudProvider", forbid_cloud)

    def finish(session, doc, current, result):
        """仅标记已验证的Mock边界，真实Block事务仍由既有数据库用例验收。"""
        assert result is parsed
        doc.status, current.state, current.is_active = "CHUNKING", "done", False

    monkeypatch.setattr(jobs, "finish_job", finish)
    return saved, parsed


@pytest.mark.parametrize("code", sorted(LOCAL_ADAPTER_ERROR_CODES))
def test_adapter_retry_is_local_for_every_supported_adapter_error(context, monkeypatch, code):
    """类型/空块/完整性等本地失败也可重放，不局限MERGE_EVIDENCE且不更换收费身份。"""
    settings, document, job, session = context
    stub_local_conversion(monkeypatch, document, job)
    job.parse_options = {"_cache_recipe": {"normalizer_version": NORMALIZER_VERSION}}
    job.state, job.error_code, job.is_active, document.status = "failed", code, False, "FAILED"
    identity = job.job_id, job.task_id, job.batch_id, job.data_id, job.file_hash, job.options_hash
    assert management.retry_cloud_job(session, document, settings) is job
    assert job.parse_options["_local_replay_only"] is True
    assert jobs.check_step(session, job.job_id, None, settings) == ("done", 1)
    assert identity == (job.job_id, job.task_id, job.batch_id, job.data_id, job.file_hash, job.options_hash)
    assert document.status == "CHUNKING"


@pytest.mark.parametrize("marked", [False, True])
def test_worker_with_saved_zip_never_initializes_cloud_client(context, monkeypatch, marked):
    """真实任务入口在空Token配置下消费已存ZIP，连Provider构造及Redis限流初始化都禁止。"""
    settings, document, job, session = context
    settings = settings.model_copy(update={"mineru_api_token": SecretStr("")})
    stub_local_conversion(monkeypatch, document, job)
    if marked:
        job.parse_options = {"_local_replay_only": True}
    assert not settings.mineru_api_token.get_secret_value()
    session.get = lambda *args: job

    @contextmanager
    def isolated_session():
        """返回内存事务Stub，不打开任何数据库连接。"""
        yield session

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "processing_session", isolated_session)
    monkeypatch.setattr(tasks, "restore_cached_result", lambda *args: None)
    task = SimpleNamespace(request=SimpleNamespace(retries=0))
    assert tasks.execute_step(task, str(job.job_id), "check")["status"] == "awaiting_chunk"
    assert document.status == "CHUNKING"


@pytest.mark.parametrize("step", ["submit", "check"])
@pytest.mark.parametrize("missing", ["archive", "batch", "upload"])
def test_offline_mode_missing_result_cannot_fall_back_to_cloud(context, monkeypatch, step, missing):
    """归档/Batch/上传检查点缺失立即安全失败，迟到submit消息也不能绕过离线限制。"""
    settings, document, job, session = context
    job.parse_options = {"_local_replay_only": True}
    if missing == "batch":
        job.batch_id = None
    elif missing == "upload":
        job.upload_complete = False
    cloud = SimpleNamespace(query=forbid_cloud, request_upload_urls=forbid_cloud,
        upload=forbid_cloud, download_result=forbid_cloud)
    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", forbid_cloud)
    with pytest.raises(MinerUError) as error:
        (jobs.submit_step if step == "submit" else jobs.check_step)(session, job.job_id, cloud, settings)
    assert error.value.code == "LOCAL_RESULT_REQUIRED" and session.commits == 0
    assert document.status == "PARSING"


def test_corrupt_local_archive_is_not_a_network_cache_miss(context, monkeypatch):
    """坏回执/Hash不能被当作缓存miss去下载，更不能重新申请批次。"""
    settings, _, job, session = context
    job.parse_options = {"_local_replay_only": True}

    def damaged(*args, **kwargs):
        """模拟独立归档器拒绝本地Hash不一致。"""
        raise MinerUError("RAW_RESULT_CHANGED", "原ZIP身份不一致")

    monkeypatch.setattr(jobs, "load_raw_result", damaged)
    cloud = SimpleNamespace(query=forbid_cloud, download_result=forbid_cloud)
    with pytest.raises(MinerUError) as error:
        jobs.check_step(session, job.job_id, cloud, settings)
    assert error.value.code == "RAW_RESULT_CHANGED" and session.commits == 0


def test_adapter_can_be_fixed_and_replayed_repeatedly_on_same_raw_result(context, monkeypatch):
    """两次本地失败后仍可复用同ZIP成功，无远端查询/下载/POST且失败不发布块或成功缓存。"""
    settings, document, job, session = context
    saved, parsed = stub_local_conversion(monkeypatch, document, job)
    job.parse_options = {"_cache_recipe": {"normalizer_version": NORMALIZER_VERSION}, "_local_replay_only": True}
    calls, publishes = [], []

    def adapting(self, archive, **kwargs):
        """模拟两次代码兼容失败及修复后的转换，不改变原ZIP哨兵或来源。"""
        assert archive is saved[0]
        calls.append(kwargs["identity"])
        if len(calls) < 3:
            raise MinerUError("MERGE_EVIDENCE", "本地适配尚不兼容")
        return parsed

    monkeypatch.setattr(jobs.MinerUResultAdapter, "normalize", adapting)
    monkeypatch.setattr(jobs, "save_cloud_cache", lambda *args: publishes.append("cache"))
    identity = job.job_id, job.batch_id, job.data_id, job.options_hash
    for _ in range(2):
        with pytest.raises(MinerUError) as error:
            jobs.check_step(session, job.job_id, None, settings)
        assert not publishes
        assert not jobs.record_error(session, job.job_id, error.value, settings)
        assert document.status == "FAILED"
        assert management.retry_cloud_job(session, document, settings) is job
    assert jobs.check_step(session, job.job_id, None, settings) == ("done", 1)
    assert len(calls) == 3 and len(set(calls)) == 1 and publishes == ["cache"]
    assert identity == (job.job_id, job.batch_id, job.data_id, job.options_hash)


def test_deferred_provider_retains_one_instance_for_real_cloud_operations(monkeypatch):
    """正常云路径仍只初始化一个Provider并释放，不改变原HTTP服务边界。"""
    calls = []
    cloud = SimpleNamespace(query=lambda *args: calls.append("query"), close=lambda: calls.append("close"))
    monkeypatch.setattr(tasks, "MinerUCloudProvider", lambda settings: calls.append("created") or cloud)
    lazy = tasks.DeferredCloudProvider(object())
    lazy.close()
    assert not calls
    lazy.query("same-batch", "same-data")
    lazy.query("same-batch", "same-data")
    lazy.close()
    assert calls == ["created", "query", "query", "close"]


def test_stage33_report_keeps_local_result_required_without_freeform_error():
    """阶段33共用安全导出保留缺本地结果的明确原因，不改为UNKNOWN或回显敏感字符串。"""
    from scripts.smoke_stage16_17 import safe_job_code
    assert safe_job_code("LOCAL_RESULT_REQUIRED") == "LOCAL_RESULT_REQUIRED"
    assert safe_job_code("PRIVATE_SIGNED_URL") == "UNKNOWN_ERROR_CODE"
