"""分片至ZIP获取的HTTPX Mock全流程；不读取真实PDF、数据库、Redis或网络。"""

import json
from collections import Counter
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4, uuid5
from zipfile import ZipFile

import httpx
import pytest
from mineru_samples import result_zip

from app.core.config import Settings
from app.services.mineru import pdf_split as split
from app.services.mineru import provider as http_provider
from app.services.mineru.errors import MinerUError
from app.services.mineru.raw_results import obtain_raw_result, validate_archive
from app.services.parse_cache import ParseCache
from scripts import mineru_pdf_split_acceptance as cli


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    """生产分片规划器+生产Provider，替换PDF字节生成器、文件验证和Redis为隔离Stub。"""
    source = tmp_path / "input.pdf"
    source.write_bytes(b"MOCK_ORIGINAL_NOT_A_REAL_PDF")
    state = SimpleNamespace(calls=[], queries=Counter(), ids={}, reserves=[], now=0,
        pending=False, bad_zip=False, remote_failed=False, downloads=0, uncertain_post=False,
        second_zip_bad=False)
    settings = Settings(_env_file=None, mineru_api_token="PRIVATE_TEST_TOKEN")

    def prepare(document, job, config, path):
        """复用真实按实际字节规划；只写元数据占位，禁止调用真实PDF解析/渲染。"""
        folder = split.part_path(ParseCache(config), document.doc_id, job.job_id, 1).parent

        def render(first, last, target):
            """确定字节代表连续页段，供规划器stat/Hash，不冒充有效PDF。"""
            target.write_bytes(f"MOCK_RANGE_{first}_{last}".encode())

        rows = split.build_parts(257, folder, render)
        for row in rows:
            identity = uuid5(job.job_id, f"{split.VERSION}:{row['ordinal']}:{row['page_start']}:{row['page_end']}:{row['file_hash']}")
            row.update(task_id=identity, data_id=f"dkf-{identity.hex}")
        return split.SplitManifest(doc_id=document.doc_id, file_hash=document.file_hash,
            page_count=257, policy=split.policy(), parts=rows)

    def validate(source):
        """只验证占位字节身份；不把Mock样本送进PyMuPDF。"""
        assert split.file_digest(source.path) == source.file_hash
        return 200 if source.name == "part-0001.pdf" else 57

    def handler(request):
        """模拟真实POST/PUT/异步GET/CDN GET协议，验证对象存储不接收凭据。"""
        state.calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "mineru.net":
            assert request.headers["Authorization"] == "Bearer PRIVATE_TEST_TOKEN"
            if request.method == "POST":
                if state.uncertain_post:
                    raise httpx.ReadTimeout("SECRET_UPSTREAM", request=request)
                payload = json.loads(request.content)
                assert len(payload["files"]) == 1 and "_pdf_split" not in payload
                batch = f"batch-{len(state.ids) + 1}"
                state.ids[batch] = payload["files"][0]["data_id"]
                data = {"batch_id": batch, "file_urls": [f"https://mineru.oss-cn-shanghai.aliyuncs.com/{batch}?SECRET_SIGNATURE"]}
            else:
                batch = request.url.path.rsplit("/", 1)[1]
                state.queries[batch] += 1
                remote = "failed" if state.remote_failed else (
                    "pending" if state.pending else ["pending", "running", "done"][min(2, state.queries[batch] - 1)])
                data = {"batch_id": batch, "extract_result": [{"data_id": state.ids[batch],
                    "state": remote, "full_zip_url": f"https://cdn-mineru.openxlab.org.cn/{batch}.zip?SECRET_SIGNATURE"}]}
            return httpx.Response(200, json={"code": 0, "data": data, "trace_id": "trace"})
        assert "Authorization" not in request.headers and "Content-Type" not in request.headers
        if request.method == "PUT":
            assert request.content.startswith(b"MOCK_RANGE_")
            return httpx.Response(200)
        state.downloads += 1
        bad = state.bad_zip or state.second_zip_bad and request.url.path == "/batch-2.zip"
        return httpx.Response(200, content=b"NOT_A_ZIP" if bad else result_zip())

    class Limiter:
        """保留按实际文件数调用限流的观测，不连接Redis。"""

        def reserve(self, kind, files=1, pages=0):
            """仅记录额度请求，测试不得产生真实配额消耗。"""
            state.reserves.append((kind, files, pages))

    def factory(config):
        """注入MockTransport；未注入真实HTTP客户端或系统代理。"""
        return http_provider.MinerUCloudProvider(config,
            client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False), limiter=Limiter())

    def clock():
        """返回可控单调时间，不依赖真实等待。"""
        return state.now

    def wait(seconds):
        """推进虚拟时间，避免测试sleep或轮询真实云端。"""
        state.now += seconds

    monkeypatch.setattr(cli, "prepare_manifest", prepare)
    monkeypatch.setattr(cli, "validate_local_file", validate)
    monkeypatch.setattr(http_provider, "validate_local_file", validate)
    return SimpleNamespace(source=source, settings=settings, factory=factory, state=state,
        output=tmp_path / "run", clock=clock, wait=wait)


def run(cloud, *, max_files=2, timeout=30):
    """运行隔离验收函数，完全使用虚拟时间和Mock HTTP。"""
    return cli.execute(cloud.source, split.file_digest(cloud.source), 257, cloud.output, cloud.settings,
        max_files=max_files, timeout=timeout, poll_interval=1, provider_factory=cloud.factory,
        clock=cloud.clock, wait=cloud.wait)


def test_two_parts_submit_poll_download_and_preserve_exact_archives(cloud):
    """200+57连续页段分别上传/等待done/下载ZIP；报告不将云ZIP成功冒充业务READY。"""
    report = run(cloud)
    assert report["status"] == "CLOUD_ZIP_PASS"
    assert report["adapter_status"] == "NOT_RUN" and not report["business_parse_success"]
    assert report["business_database_modified"] is False
    assert [(p["page_start"], p["page_end"]) for p in report["parts"]] == [(1, 200), (201, 257)]
    assert report["planned_submissions"] == report["confirmed_submissions"] == report["submission_attempts"] == 2
    assert [p["batch_id"] for p in report["parts"]] == ["batch-1", "batch-2"]
    assert cloud.state.queries == {"batch-1": 3, "batch-2": 3}
    assert sum(method == "POST" for method, _, _ in cloud.state.calls) == 2
    assert sum(method == "PUT" for method, _, _ in cloud.state.calls) == 2
    assert cloud.state.downloads == 2
    assert [call for call in cloud.state.reserves if call[0] == "submit"] == [("submit", 1, 200), ("submit", 1, 57)]
    from pathlib import Path
    for row in report["parts"]:
        archive = Path(row["zip_path"])
        assert row["upload_status"] == "uploaded" and row["parse_status"] == "done"
        assert row["zip_status"] == "validated" and archive.stat().st_size == row["zip_size_bytes"]
        assert split.file_digest(archive) == row["zip_sha256"]
        assert validate_archive(archive.read_bytes(), 1024**2) > 0
    assert "PRIVATE_TEST_TOKEN" not in json.dumps(report) and "SECRET_SIGNATURE" not in json.dumps(report)


@pytest.mark.parametrize("mode,code", [("pending", "JOB_TIMEOUT"), ("bad_zip", "ZIP_INVALID"),
                                       ("remote_failed", "REMOTE_FAILED")])
def test_incomplete_cloud_or_corrupt_zip_is_not_a_pass_and_does_not_resubmit(cloud, mode, code):
    """超时不是远端失败；损坏ZIP/云失败均不伪装成功，不自动新POST。"""
    setattr(cloud.state, mode, True)
    report = run(cloud, timeout=3)
    assert report["status"] == "INCOMPLETE" and report["error_code"] == code
    assert report["confirmed_submissions"] == 1 and cloud.state.downloads <= 1
    assert report["parts"][1]["batch_id"] is None and not report["business_parse_success"]
    if mode == "pending":
        assert report["parts"][0]["parse_status"] == "pending"


def test_budget_and_existing_directory_prevent_new_cloud_submissions(cloud):
    """实际分片数超过显式预算或输出目录已经存在，拒绝启动客户端/重复收费。"""
    report = run(cloud, max_files=1)
    assert report["error_code"] == "SUBMISSION_BUDGET" and cloud.state.calls == []
    original = (cloud.output / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        run(cloud)
    assert (cloud.output / "report.json").read_bytes() == original and cloud.state.calls == []


def test_uncertain_post_stops_after_one_attempt_and_retains_intent(cloud):
    """POST响应超时不等于未收费；报告保留一次意图，绝不自动申请第二个Batch。"""
    cloud.state.uncertain_post = True
    report = run(cloud)
    assert report["status"] == "INCOMPLETE" and report["submission_attempts"] == 1
    assert report["confirmed_submissions"] == 0 and report["error_code"] == "SUBMIT_UNCERTAIN"
    assert sum(method == "POST" for method, _, _ in cloud.state.calls) == 1
    assert report["parts"][0]["upload_status"] == "requesting_batch"
    assert "SECRET_UPSTREAM" not in json.dumps(report)


def test_second_zip_failure_does_not_erase_first_result_or_claim_success(cloud):
    """第二片坏ZIP不丢第一片原结果；无第三次POST、业务成功或伪造READY。"""
    from pathlib import Path

    cloud.state.second_zip_bad = True
    report = run(cloud)
    first, second = report["parts"]
    assert report["status"] == "INCOMPLETE" and report["confirmed_submissions"] == 2
    assert first["zip_status"] == "validated" and second["zip_status"] == "download_or_validation_failed"
    assert split.file_digest(Path(first["zip_path"])) == first["zip_sha256"]
    assert not report["business_parse_success"] and report["adapter_status"] == "NOT_RUN"


def test_default_preview_and_cloud_denial_do_not_create_outputs_or_client(cloud, monkeypatch):
    """默认只读预览、未授权执行均无目录物化、模型、业务数据库或云端请求。"""
    def inspect(path):
        """返回只读元数据Stub，不打开实际PDF。"""
        return cloud.source, split.file_digest(cloud.source), 257

    def forbidden(*args, **kwargs):
        """默认路径不得调用执行函数。"""
        pytest.fail("不应执行云端流程")

    monkeypatch.setattr(cli, "inspect_source", inspect)
    monkeypatch.setattr(cli, "execute", forbidden)
    args = ["--pdf", str(cloud.source), "--output", str(cloud.output)]
    assert cli.main(args) == 0
    assert cli.main(args + ["--execute"]) == 1
    assert not cloud.output.exists() and cloud.state.calls == []


def test_saved_raw_result_reuse_and_corruption_never_redownload_or_overwrite(tmp_path):
    """同来源完整ZIP复用，归档篡改后明确失败；不能静默用云下载覆盖原回执。"""
    archive = result_zip()
    calls = []

    def download(url):
        """只返回内存ZIP，不读取URL或联网。"""
        calls.append(1)
        return archive

    provider = SimpleNamespace(download_result=download)
    settings = Settings(_env_file=None, parsed_dir=tmp_path)
    args = dict(doc_id=uuid4(), job_id=uuid4(), ordinal=1, batch_id="batch-original",
                data_id="data-original", file_hash="a" * 64)
    original, record = obtain_raw_result(provider, None, settings, **args)
    path = tmp_path / record["zip_path"]
    before = path.stat().st_mtime_ns
    assert obtain_raw_result(provider, None, settings, **args) == (original, record)
    assert calls == [1] and path.stat().st_mtime_ns == before
    path.write_bytes(b"CORRUPTED")
    with pytest.raises(MinerUError) as error:
        obtain_raw_result(provider, None, settings, **args)
    assert error.value.code == "RAW_RESULT_CHANGED" and calls == [1]
    assert path.read_bytes() == b"CORRUPTED"


def test_crc_validation_includes_members_other_than_content_list():
    """正文JSON完整但其他ZIP成员损坏也拒绝，不能只校验content_list。"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("content_list.json", '[{"type":"text","page_idx":0,"text":"mock"}]')
        archive.writestr("other.bin", b"UNIQUE_CRC_PAYLOAD")
    damaged = buffer.getvalue().replace(b"UNIQUE_CRC_PAYLOAD", b"CORRUPT_CRC_PAYLOD")
    with pytest.raises(MinerUError) as error:
        validate_archive(damaged, 1024**2)
    assert error.value.code == "ZIP_INVALID"


@pytest.mark.parametrize("archive", [result_zip([]), result_zip([{"type": "text"}])])
def test_empty_or_invalid_structured_result_is_rejected(archive):
    """有效容器不代表有效结构化结果；空列表/缺失页码拒绝。"""
    with pytest.raises(MinerUError) as error:
        validate_archive(archive, 1024**2)
    assert error.value.code == "CONTENT_LIST"
