"""下载器的纯元数据/HTTP Stub 用例；不请求网络、不生成或解析真实文档，用户自行执行。"""

import hashlib
import json
from dataclasses import asdict
from unittest.mock import Mock

import httpx
import pytest

from scripts import download_benchmarks as dl

REV = "a" * 40
GIT = "b" * 40


def raw_json(value):
    """仅编码用于 API Stub 的元数据，非文档/OCR 验收素材。"""
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def info(dataset="ocb"):
    """返回 Dataset API 字段 Stub，不冒充真实已下载官方元数据。"""
    return {"id": dl.REPOS[dataset], "sha": REV, "cardData": {"license": "stub"}, "siblings": []}


def entry(path="data/ocb_qna_data.parquet", data=b"metadata-stub"):
    """保留 HF Tree API 对象结构与真实按 Stub 字节计算的摘要。"""
    return {"type": "file", "path": path, "size": len(data), "oid": GIT,
            "lfs": {"oid": hashlib.sha256(data).hexdigest(), "size": len(data), "pointerSize": 130}}


def spec(data=b"metadata-stub", local="test.json"):
    """定义纯传输元数据；格式验证由 Stub 接管，绝不假装真实文档格式。"""
    return dl.FileSpec(local, local, dl.REPOS["ocb"], REV, len(data), hashlib.sha256(data).hexdigest(), None, "json")


def stub_validator(path, expected):
    """只校验传输 Stub 身份，不创建或解析 PDF/Office/图片。"""
    data = path.read_bytes()
    if len(data) != expected.size or hashlib.sha256(data).hexdigest() != expected.sha256:
        raise dl.DownloadError("FORMAT")
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "official_digest_checked": True}


def network(handler, retries=1):
    """所有 HTTP 请求进入 MockTransport；退避仅记录，不真实等待。"""
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False,
                          headers={"Accept-Encoding": "identity"})
    return dl.Network("https://hf-mirror.com", client=client, retries=retries, sleep=Mock())


def test_first_run_and_cwd_independence(tmp_path, monkeypatch):
    """验证根路径来自脚本常量，首次创建目录不生成目标文件或批准记录。"""
    monkeypatch.setattr(dl, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.chdir(tmp_path)
    root = dl.source_root("ocb")
    assert root == tmp_path / "project/docs/test/ocb_preset/source"
    assert (root / "reference_files").is_dir()
    assert not (root / "ocb_qna_data.parquet").exists()
    assert not (root / "source_review.json").exists()
    assert dl.source_root("omni").joinpath("images").is_dir()
    assert not dl.source_root("ohr", create=False).exists()


def test_endpoint_precedence(monkeypatch):
    """显式入口优先，国内镜像默认覆盖 API 与下载，不接受任意站点。"""
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert dl.endpoint_value() == "https://hf-mirror.com"
    monkeypatch.setenv("HF_ENDPOINT", "https://huggingface.co")
    assert dl.endpoint_value() == "https://huggingface.co"
    assert dl.endpoint_value("https://hf-mirror.com") == "https://hf-mirror.com"
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_PATH"):
        dl.endpoint_value("https://private.invalid")
    assert spec().url("https://hf-mirror.com").startswith("https://hf-mirror.com/datasets/")


@pytest.mark.parametrize("value", [None, "", "main", "a" * 39, "https://private.invalid"])
def test_no_empty_revision(value):
    """空或非 SHA 版本不能进入 URL 拼接或文件下载。"""
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_REVISION"):
        dl.revision_value(value)


@pytest.mark.parametrize("bad", [{}, {"id": dl.REPOS["ocb"], "sha": REV},
    {"id": "other/repo", "sha": REV, "cardData": {}},
    {"id": dl.REPOS["ocb"], "sha": REV, "cardData": "not-object"}])
def test_metadata_fields_rejected(bad):
    """不完整原始 API 信息不得补造字段或替换成 SDK __dict__。"""
    with pytest.raises(dl.DownloadError):
        dl.dataset_info(bad, dl.REPOS["ocb"])


def test_revision_failure_stops_without_final_metadata(tmp_path):
    """版本请求失败立即停止，未建立正式 JSON，也不调用后续文件。"""
    calls = []

    def handler(request):
        """将首次版本请求安全拒绝，检查后续流程没有请求。"""
        calls.append(str(request.url))
        return httpx.Response(404)

    net = network(handler)
    with pytest.raises(dl.DownloadError, match="HTTP 404"):
        dl.fixed_metadata(tmp_path, "ocb", net)
    assert len(calls) == 1
    assert not (tmp_path / "dataset_info.json").exists()


def test_raw_dataset_info_and_pagination(tmp_path):
    """原始元数据字节不改写，分页完整对象原样保留；官方 next Link 仍走镜像。"""
    calls = []
    first, second = entry("reference_files/folder/a.xlsx"), entry("reference_files/b.docx")
    raw = b"  " + raw_json(info()) + b"\n"

    def handler(request):
        """按 API 请求返回两页结构化树，不生成真实 Office。"""
        calls.append(request)
        if "/tree/" not in request.url.path:
            return httpx.Response(200, content=raw)
        if "cursor" not in request.url.params:
            return httpx.Response(200, json=[first], headers={"Link":
                f'<https://huggingface.co/api/datasets/{dl.REPOS["ocb"]}/tree/{REV}?cursor=next>; rel="next"'})
        return httpx.Response(200, json=[second])

    net = network(handler)
    metadata, objects = dl.fixed_metadata(tmp_path, "ocb", net)
    assert metadata["sha"] == REV and objects == [first, second]
    assert (tmp_path / "dataset_info.json").read_bytes() == raw
    hosted = dl.read_json(tmp_path / "hosted_files.json")
    assert hosted == [first, second] and dl.ocb.hosted_index(hosted)
    assert all(request.url.host == "hf-mirror.com" for request in calls)
    assert all("Authorization" not in request.headers for request in calls)
    assert all(REV in request.url.path for request in calls[1:])


@pytest.mark.parametrize("page", [["reference_files/a.xlsx"],
    [{"type": "file", "path": "../outside", "size": 10, "oid": GIT}],
    [entry(), entry()]])
def test_invalid_file_tree(page):
    """字符串列表、路径跳转和重复记录不能冒充完整 HF Tree。"""
    def handler(request):
        """返回单页不合法元数据供校验，不读取真实数据。"""
        return httpx.Response(200, json=page)

    with pytest.raises(dl.DownloadError):
        dl.tree_entries(network(handler), dl.REPOS["ocb"], REV)


def test_ocb_official_name_mapping(tmp_path):
    """官方 source_urls 字节来源保留，兼容本地 file_sources 名称。"""
    entries = [entry("data/ocb_qna_data.parquet"), entry("data/ocb_source_urls.parquet")]
    specs = dl.required_specs("ocb", REV, entries, tmp_path, None)
    assert specs[1].remote == "data/ocb_source_urls.parquet"
    assert specs[1].local == "ocb_file_sources.parquet"
    assert specs[1].revision == specs[0].revision == REV


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_stops(status, tmp_path):
    """确定性 HTTP 错误不重试，正式目标和既有用户文件均不受影响。"""
    handler = Mock(return_value=httpx.Response(status))
    net = network(handler, retries=3)
    preserved = tmp_path / "source_review.json"
    preserved.write_bytes(b"[]")
    with pytest.raises(dl.DownloadError, match=f"HTTP {status}"):
        dl.Downloader(tmp_path, net, stub_validator).download(spec())
    assert handler.call_count == 1 and not (tmp_path / "test.json").exists()
    assert preserved.read_bytes() == b"[]"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_temporary_http_retry_is_bounded(status, tmp_path):
    """429/5xx 有限退避并尊重有界 Retry-After，不真实等待。"""
    count = 0

    def handler(request):
        """第一次暂时失败，第二次返回纯传输 Stub。"""
        nonlocal count
        count += 1
        return httpx.Response(status, headers={"Retry-After": "7"}) if count == 1 else httpx.Response(200, content=b"metadata-stub")

    net = network(handler)
    result = dl.Downloader(tmp_path, net, stub_validator).download(spec())
    assert result["size_bytes"] == 13 and count == 2
    net.sleep.assert_called_once_with(7)


def test_timeout_does_not_publish(tmp_path):
    """连接超时达到重试上限后非成功，留下检查点而非空正式文件。"""
    def handler(request):
        """模拟网络超时，不发出任何真实请求。"""
        raise httpx.ReadTimeout("secret-url?token=never-print", request=request)

    net = network(handler)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_NETWORK"):
        dl.Downloader(tmp_path, net, stub_validator).download(spec())
    assert net.sleep.call_count == 1 and not (tmp_path / "test.json").exists()


class InterruptedStream(httpx.SyncByteStream):
    """纯字节传输 Stub，模拟已下载一块后连接断开。"""

    def __iter__(self):
        """先交付一个 HTTP 分块，再制造安全断线，非真实文档数据。"""
        yield b"x" * dl.MIB
        raise httpx.ReadError("private-upstream-body")


def test_disconnect_and_range_resume(tmp_path):
    """中途断线后用 206 精确续传，完整摘要通过前不发布目标。"""
    data = b"x" * dl.MIB + b"tail"
    requests = []

    def handler(request):
        """根据 Range 返回剩余字节，验证断点没有追加完整文件。"""
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, headers={"ETag": '"stable"', "Content-Length": str(len(data))}, stream=InterruptedStream())
        return httpx.Response(206, headers={"ETag": '"stable"', "Content-Range": f"bytes {dl.MIB}-{len(data)-1}/{len(data)}"}, content=b"tail")

    net = network(handler)
    dl.Downloader(tmp_path, net, stub_validator).download(spec(data))
    assert requests[1].headers["Range"] == f"bytes={dl.MIB}-"
    assert requests[1].headers["If-Range"] == '"stable"'
    assert (tmp_path / "test.json").read_bytes() == data
    assert not (tmp_path / "test.json.part").exists()


@pytest.mark.parametrize("status,headers", [(200, {}), (206, {"Content-Range": "bytes 0-2/3"}),
    (206, {"Content-Range": "bytes 1-2/3", "ETag": '"changed"'})])
def test_unsafe_range_preserves_partial(tmp_path, status, headers):
    """HTTP 200、错误 Range 或变化的 ETag 均拒绝追加，原有断点字节不被修改。"""
    expected = spec(b"abc")
    net = network(Mock(return_value=httpx.Response(status, headers=headers, content=b"bc")))
    downloader = dl.Downloader(tmp_path, net, stub_validator)
    (tmp_path / "test.json.part").write_bytes(b"a")
    dl.write_private(downloader.state_path(expected), {"spec": asdict(expected), "etag": '"old"'}, tmp_path)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_RESUME"):
        downloader.download(expected)
    assert (tmp_path / "test.json.part").read_bytes() == b"a"
    assert not (tmp_path / "test.json").exists()


def test_existing_good_skip_bad_refuse_and_orphan_partial(tmp_path):
    """已有完整原件校验后跳过；坏原件及来源不明 .part 均保留并拒绝。"""
    handler = Mock()
    downloader = dl.Downloader(tmp_path, network(handler), stub_validator)
    target = tmp_path / "test.json"
    target.write_bytes(b"metadata-stub")
    downloader.download(spec())
    handler.assert_not_called()
    target.write_bytes(b"broken-existing")
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        downloader.download(spec())
    assert target.read_bytes() == b"broken-existing"
    (tmp_path / "other.json.part").write_bytes(b"unknown")
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_RESUME"):
        downloader.download(spec(local="other.json"))


@pytest.mark.parametrize("kind,data", [("json", b"<html>wrong</html>"), ("json", b"{}"),
    ("parquet", b"not-a-parquet"), ("zip", b"not-a-zip"),
    ("zip", b"version https://git-lfs.github.com/spec/v1\n")])
def test_wrong_format_never_published(tmp_path, kind, data):
    """仅无效字节 Stub 验证拒绝路径，不生成或解析真实容器样本。"""
    expected = dl.FileSpec("bad." + kind, "bad." + kind, dl.REPOS["ocb"], REV,
                           len(data), hashlib.sha256(data).hexdigest(), None, kind)
    path = tmp_path / ("bad." + kind + ".part")
    path.write_bytes(data)
    with pytest.raises(dl.DownloadError):
        dl.validate_file(path, expected)
    assert not (tmp_path / expected.local).exists()


def test_ocb_qa_preview_and_track_gap(tmp_path, monkeypatch):
    """实际字段识别与多文件依赖保留，不输出题目答案；兼容缺口显式记录不放宽 Parser。"""
    (tmp_path / "ocb_qna_data.parquet").write_bytes(b"metadata-only")
    rows = [{"track": "FileFidelity", "reference_files": ["a.docx", "b.xlsx"],
             "question": "private-question", "expected_assertions": ["private-answer"], "feature": "table"},
            {"track": "File Fidelity Q&A", "reference_files": ["c.pptx"],
             "question": "private-question", "expected_assertions": ["private-answer"]}]
    monkeypatch.setattr(dl.common, "rows_file", Mock(return_value=rows))
    preview = dl.ocb_preview(tmp_path)
    assert preview["dependency_groups"][0]["reference_files"] == ["a.docx", "b.xlsx"]
    assert len(preview["compatibility_gaps"]) == 1
    assert "private-question" not in json.dumps(preview) and "private-answer" not in json.dumps(preview)


def test_ocb_requires_review_and_full_dependency(tmp_path):
    """未审核、遗漏 QA 依赖和外部原件不得自动下载或填写批准。"""
    path = tmp_path / "source_review.json"
    path.write_bytes(b"[]")
    files = [entry("reference_files/a.docx"), entry("reference_files/b.xlsx")]
    preview = {"dependency_groups": [{"reference_files": ["a.docx", "b.xlsx"]}]}
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_REVIEW"):
        dl.reviewed_specs(tmp_path, REV, files, preview)
    review = {"reference_file": "a.docx", "original_native": True, "local_use_confirmed": True,
              "conversion_required": False, "license": "user-reviewed",
              "source_url": f"https://huggingface.co/datasets/{dl.REPOS['ocb']}/resolve/{REV}/reference_files/a.docx"}
    path.write_bytes(raw_json([review]))
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_REVIEW"):
        dl.reviewed_specs(tmp_path, REV, files, preview)
    assert dl.read_json(path) == [review]
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXTERNAL"):
        dl.reviewed_specs(tmp_path, REV, [], preview)


def test_omni_exact_path_selection(tmp_path, monkeypatch):
    """仅使用标注的完整身份，不用 basename、渲染或伪造原 PDF 映射。"""
    rows = [{"page_info": {"image_path": "folder/a.png", "width": 10, "height": 20}},
            {"page_info": {"image_path": "images/b.png", "width": 10, "height": 20}}]
    monkeypatch.setattr(dl, "read_json", Mock(return_value=rows))

    def page_record(row):
        """以元数据 Stub 替代区域校验，不构造图片样本。"""
        return row["page_info"]["image_path"], {"text"}

    monkeypatch.setattr(dl.omni, "page_record", page_record)
    files = [entry("images/folder/a.png"), entry("images/b.png")]
    selected, gaps = dl.omni_candidates(tmp_path, files, 42)
    assert {item["entry"]["path"] for item in selected} == {"images/folder/a.png", "images/b.png"}
    assert not gaps
    repeated, _ = dl.omni_candidates(tmp_path, list(reversed(files)), 42)
    assert selected == repeated
    _, missing = dl.omni_candidates(tmp_path, [entry("images/a.png")], 42)
    assert all(item["reason"] == "missing_official_image" for item in missing)


def test_safe_logging_and_redirect(tmp_path, capsys):
    """错误/重试不输出签名、Token 或上游正文，未知重定向在发请求前拒绝。"""
    def handler(request):
        """只返回含敏感签名的拒绝响应，终端应仅包含安全 HTTP 数字。"""
        return httpx.Response(403, text="private-body https://secret.invalid/?token=secret", headers={"Location": "https://secret.invalid/?token=secret"})

    with pytest.raises(dl.DownloadError) as captured:
        dl.Downloader(tmp_path, network(handler), stub_validator).download(spec())
    print(captured.value)
    output = capsys.readouterr().out
    assert "private-body" not in output and "token=" not in output and "secret.invalid" not in output
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_PATH"):
        network(handler).check_url("https://127.0.0.1/private", binary=True)


def test_read_only_modes_never_create_http_client(tmp_path, monkeypatch):
    """只读缺失查看不发网络请求，首次缺少元数据清楚返回缺口。"""
    monkeypatch.setattr(dl, "PROJECT_ROOT", tmp_path)
    client = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(dl, "Network", client)
    assert dl.main(["--dataset", "ocb", "--scope", "metadata", "--list-missing"]) == 2
    assert dl.main(["--dataset", "omni", "--scope", "metadata", "--verify-only"]) == 1
    client.assert_not_called()


def test_source_review_initialization_never_overwrites(tmp_path):
    """许可文件只能首次生成空数组，既有人工内容绝不覆盖。"""
    path = tmp_path / "source_review.json"
    dl.publish_bytes(path, b"[]\n", tmp_path)
    path.write_bytes(raw_json([{"local_use_confirmed": False}]))
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        dl.publish_bytes(path, b"[]\n", tmp_path)
    assert dl.read_json(path) == [{"local_use_confirmed": False}]


def test_failure_stops_remaining_dependencies(tmp_path):
    """首项下载失败不继续后续文件，既有用户内容原样保留。"""
    preserved = tmp_path / "existing.json"
    preserved.write_bytes(b"user-owned")
    downloader = Mock(root=tmp_path)
    downloader.download.side_effect = dl.DownloadError("NETWORK")
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_NETWORK"):
        dl.process_specs([spec(local="first.json"), spec(local="second.json")], downloader)
    assert downloader.download.call_count == 1
    assert preserved.read_bytes() == b"user-owned"


def test_partial_is_not_formal_verify_input(tmp_path):
    """完整字节仅在 .part 也仍未正式发布，离线校验不把它当成功输入。"""
    (tmp_path / "test.json.part").write_bytes(b"metadata-stub")
    downloader = dl.Downloader(tmp_path, None, stub_validator)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_MISSING"):
        dl.process_specs([spec()], downloader, verify_only=True)


def test_json_success_uses_original_bytes(tmp_path):
    """有效 JSON 元数据下载后字节原样保留，不重写成新的官方记录。"""
    data = b'  [ {"metadata": "stub"} ]\n'
    expected = spec(data)
    net = network(Mock(return_value=httpx.Response(200, content=data)))
    dl.Downloader(tmp_path, net).download(expected)
    assert (tmp_path / "test.json").read_bytes() == data
    assert dl.read_json(tmp_path / "test.json") == [{"metadata": "stub"}]


def test_parquet_branch_requires_real_decoder(tmp_path, monkeypatch):
    """只用元数据字节 Stub 验证 Parquet 分支必须调用既有有界读取器，不执行真实解码。"""
    data = b"PAR1" + b"metadata-stub" + (3).to_bytes(4, "little") + b"PAR1"
    expected = dl.FileSpec("test.parquet", "test.parquet", dl.REPOS["ocb"], REV,
        len(data), hashlib.sha256(data).hexdigest(), None, "parquet")
    path = tmp_path / "test.parquet.part"
    path.write_bytes(data)
    reader = Mock(return_value=[{"track": "stub"}])
    monkeypatch.setattr(dl.common, "rows_file", reader)
    result = dl.validate_file(path, expected)
    reader.assert_called_once_with(path)
    assert result["sha256"] == expected.sha256


def test_zip_branch_checks_central_directory(tmp_path, monkeypatch):
    """ZIP 正向分支完全 Mock，仅验证中央目录安全检查被调用，不生成真实 ZIP/PDF。"""
    data = b"container-byte-stub"
    expected = dl.FileSpec("pdfs.zip", "pdfs.zip", dl.REPOS["ohr"], REV,
        len(data), hashlib.sha256(data).hexdigest(), None, "zip")
    path = tmp_path / "pdfs.zip.part"
    path.write_bytes(data)
    archive = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=archive)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(dl, "ZipFile", Mock(return_value=context))
    directory = Mock(return_value=({"metadata-only.pdf": Mock()}, "digest-stub"))
    monkeypatch.setattr(dl.common.ohr, "archive_index", directory)
    assert dl.validate_file(path, expected)["size_bytes"] == len(data)
    directory.assert_called_once_with(archive)


def test_ocb_approved_group_keeps_official_paths(tmp_path):
    """许可 Stub 全部确认后保留同一 QA 两个完整托管路径，不合并成一文件或自动转换。"""
    files = [entry("reference_files/folder/a.docx"), entry("reference_files/b.xlsx")]
    reviews = []
    for item in files:
        reviews.append({"reference_file": item["path"].removeprefix("reference_files/"),
            "original_native": True, "local_use_confirmed": True, "conversion_required": False,
            "license": "permission-stub-not-real-approval",
            "source_url": f"https://huggingface.co/datasets/{dl.REPOS['ocb']}/resolve/{REV}/{item['path']}"})
    (tmp_path / "source_review.json").write_bytes(raw_json(reviews))
    selected = dl.reviewed_specs(tmp_path, REV, files,
        {"dependency_groups": [{"reference_files": ["folder/a.docx", "b.xlsx"]}]})
    assert {item.local for item in selected} == {item["path"] for item in files}
    assert len(selected) == 2


def test_disk_space_failure_keeps_prior_files(tmp_path, monkeypatch):
    """写盘前空间不足明确失败，不产生正式文件或删除已下载内容。"""
    monkeypatch.setattr(dl.shutil, "disk_usage", Mock(return_value=Mock(free=0)))
    downloader = dl.Downloader(tmp_path, network(Mock()), stub_validator)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_SPACE"):
        downloader.download(spec())
    assert not (tmp_path / "test.json").exists()
    assert not (tmp_path / "test.json.part").exists()


def test_ocb_candidate_addresses_do_not_approve():
    """候选提供完整固定官方地址，无需手工拼 SHA，但不生成任何批准值。"""
    files = [entry("reference_files/folder/a doc.xlsx")]
    sources = dl.ocb_candidate_sources({"dependency_groups": [{"reference_files": ["folder/a doc.xlsx"]}]}, files, REV)
    assert sources[0]["official_source_url"].endswith(f"/{REV}/reference_files/folder/a%20doc.xlsx")
    assert sources[0]["review_status"] == "not_reviewed"
    assert "local_use_confirmed" not in sources[0] and "license" not in sources[0]


def test_ocb_tree_recovery_preserves_existing_metadata_and_strings(tmp_path, monkeypatch):
    """只取得固定 SHA 的全分页对象副本，原 dataset_info、字符串列表、审核和旧树缓存不动。"""
    metadata = b" " + raw_json(info()) + b"\n"
    legacy = b'["reference_files/a.xlsx","reference_files/b.docx"]'
    (tmp_path / "dataset_info.json").write_bytes(metadata)
    (tmp_path / "hosted_files.json").write_bytes(legacy)
    (tmp_path / "source_review.json").write_bytes(b"[]")
    old_cache = tmp_path / ".download/tree.json"
    dl.write_private(old_cache, {"revision": REV, "entries": ["legacy-list"], "tree_sha256": "old-stub"}, tmp_path)
    old_cache_bytes = old_cache.read_bytes()
    objects = [entry("reference_files/a.xlsx"), entry("reference_files/b.docx")]
    requests = []

    def handler(request):
        """仅响应固定版本 Tree API，任何 Dataset API/文件下载请求都使断言失败。"""
        requests.append(request)
        assert request.url.path == f"/api/datasets/{dl.REPOS['ocb']}/tree/{REV}"
        if "cursor" not in request.url.params:
            return httpx.Response(200, json=[objects[0]], headers={"Link":
                f'<https://huggingface.co/api/datasets/{dl.REPOS["ocb"]}/tree/{REV}?cursor=next>; rel="next"'})
        return httpx.Response(200, json=[objects[1]])

    forbidden = Mock(side_effect=AssertionError("不得下载 Parquet/原件或重新申请元数据"))
    monkeypatch.setattr(dl, "required_specs", forbidden)
    args = dl.build_parser().parse_args(["--dataset", "ocb", "--scope", "hosted-tree"])
    assert dl.run(args, tmp_path, network(handler)) == 0
    assert len(requests) == 2 and all(request.url.host == "hf-mirror.com" for request in requests)
    assert dl.read_json(tmp_path / "hosted_files.structured.json") == objects
    cache = dl.read_json(tmp_path / ".download/ocb-structured-tree.json")
    assert cache["pagination"] == {"revision": REV, "page_count": 2,
        "pages": [{"item_count": 1, "had_next": True}, {"item_count": 1, "had_next": False}], "complete": True}
    assert (tmp_path / "dataset_info.json").read_bytes() == metadata
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy
    assert (tmp_path / "source_review.json").read_bytes() == b"[]"
    assert old_cache.read_bytes() == old_cache_bytes
    forbidden.assert_not_called()
    assert dl.ocb.hosted_index(objects)


def test_tree_recovery_needs_existing_valid_revision_without_creating_info(tmp_path):
    """树修复模式不新建/重取 dataset_info，也不把 main 当固定版本。"""
    handler = Mock(side_effect=AssertionError("不应请求元数据"))
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_MISSING"):
        dl.fixed_metadata(tmp_path, "ocb", network(handler), tree_only=True,
                          hosted_file="hosted_files.structured.json")
    handler.assert_not_called()
    assert not (tmp_path / "dataset_info.json").exists()


def test_tree_recovery_refuses_to_overwrite_existing_bad_sidecar(tmp_path):
    """新副本若也已存在且无效，保留原字节并明确 hosted_tree，不自动覆盖或伪造对象。"""
    (tmp_path / "dataset_info.json").write_bytes(raw_json(info()))
    target = tmp_path / "hosted_files.structured.json"
    target.write_bytes(b'["legacy-path"]')
    handler = Mock(return_value=httpx.Response(200, json=[entry("reference_files/a.xlsx")]))
    with pytest.raises(dl.common.BenchmarkError, match="hosted_tree"):
        dl.fixed_metadata(tmp_path, "ocb", network(handler), tree_only=True,
                          hosted_file="hosted_files.structured.json")
    assert target.read_bytes() == b'["legacy-path"]'


def test_downloader_preview_supports_real_fidelity_subtypes(tmp_path, monkeypatch):
    """下载候选接受三种明确官方分类，保留原 track；Domain 不成为文件候选。"""
    (tmp_path / "ocb_qna_data.parquet").write_bytes(b"metadata-stub")
    rows = [{"track": track, "reference_files": ["schema.xlsx"], "question": "private-question",
             "expected_assertions": ["private-answer"]} for track in
            ["Domain", "File Fidelity Excel", "File Fidelity PowerPoint", "File Fidelity Word"]]
    monkeypatch.setattr(dl.common, "rows_file", Mock(return_value=rows))
    preview = dl.ocb_preview(tmp_path)
    assert len(preview["dependency_groups"]) == 3 and preview["compatibility_gaps"] == []
    assert {group["track"] for group in preview["dependency_groups"]} == {row["track"] for row in rows[1:]}
    assert preview["tracks"]["Domain"] == 1
    assert "private-question" not in json.dumps(preview) and "private-answer" not in json.dumps(preview)


def replacement_inputs(root):
    """只准备 JSON/分页/摘要 Stub，禁止生成 Office/PDF 或读取真实用户下载资料。"""
    objects = [entry("reference_files/a.xlsx") | {"upstream_extra": {"untouched": True}},
               entry("reference_files/b.docx"), entry("data/ocb_qna_data.parquet")]
    legacy = b' ["reference_files/a.xlsx", "reference_files/b.docx"]\n'
    (root / "dataset_info.json").write_bytes(b" " + raw_json(info()) + b"\n")
    (root / "hosted_files.json").write_bytes(legacy)
    (root / "hosted_files.structured.json").write_bytes(b" " + raw_json(objects[:2]) + b"\n")
    (root / "source_review.json").write_bytes(b'[{"reference_file":"a.xlsx","local_use_confirmed":false}]')
    (root / "ocb_qna_data.parquet").write_bytes(b"non-parquet-transmission-stub")
    (root / "ocb_file_sources.parquet").write_bytes(b"non-parquet-source-stub")
    snapshot = {"revision": REV, "entries": objects, "tree_sha256": dl.common.ohr.digest(objects),
                "pagination": {"revision": REV, "page_count": 2, "complete": True,
                    "pages": [{"item_count": 1, "had_next": True}, {"item_count": 2, "had_next": False}]}}
    dl.write_private(root / ".download/ocb-structured-tree.json", snapshot, root)
    (root / ".download/tree.json").write_bytes(b'{"legacy":"untouched-cache"}')
    results = root / "results/run-03"
    results.mkdir(parents=True)
    (results / "selection_plan.json").write_bytes(b'{"previous":"metadata-only-stub"}')
    frozen = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    return objects, legacy, frozen


def test_explicit_tree_replacement_preserves_originals_and_default_entry(tmp_path):
    """修复后默认入口复用已校验新缓存；备份精确保留旧表，审核/元数据/报告/旧缓存均不变。"""
    objects, legacy, frozen = replacement_inputs(tmp_path)
    forbidden = Mock(side_effect=AssertionError("已有同 SHA 完整树时不得重新联网/读取 Parquet"))
    args = dl.build_parser().parse_args(["--dataset", "ocb", "--scope", "hosted-tree", "--replace-hosted-tree"])
    assert dl.run(args, tmp_path, network(forbidden)) == 0
    target = tmp_path / "hosted_files.json"
    assert target.read_bytes() == frozen["hosted_files.structured.json"]
    backups = list((tmp_path / ".download/backups").glob("hosted_files-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == legacy
    assert dl.read_json(target) == objects[:2]
    assert dl.read_json(target)[0]["upstream_extra"] == {"untouched": True}
    for name, before in frozen.items():
        if name != "hosted_files.json":
            assert (tmp_path / name).read_bytes() == before
    assert dl.fixed_metadata(tmp_path, "ocb", network(forbidden), offline=True)[1] == objects
    before_stat = target.stat().st_mtime_ns
    assert dl.run(args, tmp_path, network(forbidden)) == 0
    assert target.stat().st_mtime_ns == before_stat
    assert len(list((tmp_path / ".download/backups").iterdir())) == 1
    forbidden.assert_not_called()


@pytest.mark.parametrize("fault", ["revision", "digest", "complete", "page_count", "page_sum", "page_chain", "lfs", "strings"])
def test_tree_replacement_validates_snapshot_before_backup(tmp_path, fault):
    """错误版本、摘要、分页或伪对象均先拒绝；旧表保留，不先写备份再尝试修补数据。"""
    _, legacy, _ = replacement_inputs(tmp_path)
    cache_path = tmp_path / ".download/ocb-structured-tree.json"
    cached = dl.read_json(cache_path)
    if fault == "revision":
        cached["revision"] = "c" * 40
    elif fault == "digest":
        cached["tree_sha256"] = "0" * 64
    elif fault == "complete":
        cached["pagination"]["complete"] = False
    elif fault == "page_count":
        cached["pagination"]["page_count"] = 1
    elif fault == "page_sum":
        cached["pagination"]["pages"][1]["item_count"] = 1
    elif fault == "page_chain":
        cached["pagination"]["pages"][0]["had_next"] = False
    elif fault == "lfs":
        cached["entries"][0]["lfs"]["size"] += 1
        cached["tree_sha256"] = dl.common.ohr.digest(cached["entries"])
    else:
        cached["entries"] = [item["path"] for item in cached["entries"]]
        cached["tree_sha256"] = dl.common.ohr.digest(cached["entries"])
    cache_path.write_bytes(raw_json(cached))
    with pytest.raises((dl.DownloadError, dl.common.BenchmarkError)):
        dl.replace_hosted_tree(tmp_path, REV)
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy
    assert not (tmp_path / ".download/backups").exists()


def test_replacement_refuses_sidecar_not_matching_official_snapshot(tmp_path):
    """格式虽有效但对象/上游字段不等于固定树时拒绝，不能只校验路径字符串就替换。"""
    objects, legacy, _ = replacement_inputs(tmp_path)
    sidecar = tmp_path / "hosted_files.structured.json"
    sidecar.write_bytes(raw_json([objects[0] | {"oid": "d" * 40}, objects[1]]))
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        dl.replace_hosted_tree(tmp_path, REV)
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy
    assert not (tmp_path / ".download/backups").exists()


def test_replacement_failure_keeps_old_target_and_verified_backup(tmp_path, monkeypatch):
    """原子替换失败不删除正式表；已建立的原字节备份保留，只清理本函数的临时文件。"""
    _, legacy, frozen = replacement_inputs(tmp_path)
    monkeypatch.setattr(dl.os, "replace", Mock(side_effect=OSError("replacement-stub")))
    with pytest.raises(OSError):
        dl.replace_hosted_tree(tmp_path, REV)
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy
    assert list((tmp_path / ".download/backups").iterdir())[0].read_bytes() == legacy
    assert not list(tmp_path.glob(".hosted-tree-replacement-*"))
    for name, before in frozen.items():
        assert (tmp_path / name).read_bytes() == before


def test_replacement_does_not_overwrite_conflicting_backup(tmp_path):
    """备份目标已存在但字节不一致时立即失败，不覆盖备份或正式表。"""
    _, legacy, _ = replacement_inputs(tmp_path)
    backup = tmp_path / ".download/backups" / ("hosted_files-" + hashlib.sha256(legacy).hexdigest() + ".json")
    backup.parent.mkdir()
    backup.write_bytes(b"preserve-existing-backup")
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        dl.replace_hosted_tree(tmp_path, REV)
    assert backup.read_bytes() == b"preserve-existing-backup"
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy


def test_tree_replacement_detects_user_edit_before_atomic_switch(tmp_path, monkeypatch):
    """备份后正式表被人修改时不覆盖新内容；旧原字节仍有备份，临时文件被清理。"""
    _, legacy, _ = replacement_inputs(tmp_path)
    target = tmp_path / "hosted_files.json"
    changed = b'["reference_files/user-new-list.xlsx"]'
    original_publish = dl.publish_bytes

    def publish_then_edit(path, data, root):
        """仅模拟备份完成后用户改表，不请求网络或创建文档样本。"""
        original_publish(path, data, root)
        target.write_bytes(changed)

    monkeypatch.setattr(dl, "publish_bytes", publish_then_edit)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        dl.replace_hosted_tree(tmp_path, REV)
    assert target.read_bytes() == changed
    assert list((tmp_path / ".download/backups").iterdir())[0].read_bytes() == legacy
    assert not list(tmp_path.glob(".hosted-tree-replacement-*"))


def test_replacement_refuses_different_existing_object_table(tmp_path):
    """只修复旧字符串表；不一致的用户对象表不在替换授权内，必须人工核对。"""
    objects, _, _ = replacement_inputs(tmp_path)
    target = tmp_path / "hosted_files.json"
    before = raw_json([objects[0]])
    target.write_bytes(before)
    with pytest.raises(dl.DownloadError, match="DOWNLOAD_EXISTS"):
        dl.replace_hosted_tree(tmp_path, REV)
    assert target.read_bytes() == before
    assert not (tmp_path / ".download/backups").exists()


def test_tree_download_and_replacement_share_fixed_revision(tmp_path):
    """从真实形状的多分页 HTTP Stub 到显式替换全程固定同 SHA，不请求 Dataset API/原件。"""
    legacy = b'["reference_files/a.xlsx","reference_files/b.docx"]'
    metadata = raw_json(info())
    (tmp_path / "dataset_info.json").write_bytes(metadata)
    (tmp_path / "hosted_files.json").write_bytes(legacy)
    objects = [entry("reference_files/a.xlsx"), entry("reference_files/b.docx")]
    calls = []

    def handler(request):
        """分页 Stub 保留完整 LFS；next Link 转官方地址时仍要求镜像请求同 SHA。"""
        calls.append(request)
        assert request.url.host == "hf-mirror.com"
        assert request.url.path == f"/api/datasets/{dl.REPOS['ocb']}/tree/{REV}"
        if "cursor" in request.url.params:
            return httpx.Response(200, json=[objects[1]])
        return httpx.Response(200, json=[objects[0]], headers={"Link":
            f'<https://huggingface.co/api/datasets/{dl.REPOS["ocb"]}/tree/{REV}?cursor=next>; rel="next"'})

    args = dl.build_parser().parse_args(["--dataset", "ocb", "--scope", "hosted-tree", "--replace-hosted-tree"])
    assert dl.run(args, tmp_path, network(handler)) == 0
    assert len(calls) == 2
    assert dl.read_json(tmp_path / "hosted_files.json") == objects
    assert (tmp_path / "dataset_info.json").read_bytes() == metadata
    assert list((tmp_path / ".download/backups").iterdir())[0].read_bytes() == legacy
    assert not (tmp_path / "source_review.json").exists()
    assert not (tmp_path / "ocb_qna_data.parquet").exists()


def test_failed_tree_pagination_never_replaces_canonical(tmp_path):
    """取第二页失败时禁止发布不完整副本/替换旧表；不触碰已验证 dataset_info。"""
    legacy, metadata = b'["reference_files/a.xlsx"]', raw_json(info())
    (tmp_path / "hosted_files.json").write_bytes(legacy)
    (tmp_path / "dataset_info.json").write_bytes(metadata)

    def handler(request):
        """首分页有真实对象 Stub，后页 HTTP 拒绝；不允许 main/其他 SHA 请求。"""
        assert request.url.path == f"/api/datasets/{dl.REPOS['ocb']}/tree/{REV}"
        if "cursor" in request.url.params:
            return httpx.Response(404)
        return httpx.Response(200, json=[entry("reference_files/a.xlsx")], headers={"Link":
            f'<https://hf-mirror.com/api/datasets/{dl.REPOS["ocb"]}/tree/{REV}?cursor=second>; rel="next"'})

    args = dl.build_parser().parse_args(["--dataset", "ocb", "--scope", "hosted-tree", "--replace-hosted-tree"])
    with pytest.raises(dl.DownloadError, match="HTTP 404"):
        dl.run(args, tmp_path, network(handler))
    assert (tmp_path / "hosted_files.json").read_bytes() == legacy
    assert (tmp_path / "dataset_info.json").read_bytes() == metadata
    assert not (tmp_path / "hosted_files.structured.json").exists()
    assert not (tmp_path / ".download").exists()


@pytest.mark.parametrize("extra", [["--verify-only"], ["--list-missing"], ["--dry-run"],
    ["--hosted-file", "hosted_files.json"]])
def test_read_only_modes_never_authorize_replacement(tmp_path, monkeypatch, extra):
    """只读/预览或非副本目标参数与替换互斥；参数失败发生于建目录和联网前。"""
    monkeypatch.setattr(dl, "PROJECT_ROOT", tmp_path)
    forbidden = Mock(side_effect=AssertionError("非法参数不得联网"))
    monkeypatch.setattr(dl, "Network", forbidden)
    assert dl.main(["--dataset", "ocb", "--scope", "hosted-tree", "--replace-hosted-tree", *extra]) == 1
    assert not (tmp_path / "docs").exists()
    forbidden.assert_not_called()
