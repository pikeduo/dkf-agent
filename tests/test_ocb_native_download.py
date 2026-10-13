"""OCB 全量原生下载的纯元数据/HTTP Stub 用例；只编写，由用户执行，不生成真实文档。"""

import copy
import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from scripts import download_benchmarks as api
from scripts import ocb_native_download as native

REVISION = "a" * 40
PAYLOAD = b"metadata-transport-stub-not-an-office-document"


def options(*extra):
    """使用统一下载器真实参数定义，不维护第二套实验 CLI。"""
    return api.build_parser().parse_args(["--dataset", "ocb", "--scope", "native-library", *extra])


def tree_file(name):
    """建立有可计算 LFS/Git Blob 摘要的传输 Stub，不能视为真实 Office 文件。"""
    return {"type": "file", "path": "reference_files/" + name, "size": len(PAYLOAD),
        "oid": hashlib.sha1(b"blob " + str(len(PAYLOAD)).encode() + b"\x00" + PAYLOAD).hexdigest(),
        "lfs": {"oid": hashlib.sha256(PAYLOAD).hexdigest(), "size": len(PAYLOAD), "pointerSize": 128}}


def source_row(name, *, conversion="no", original=None, required=None, url=None):
    """精确使用已核对的五个官方来源字段，yes/no 为真实列类型，不编造 URL manifest 字段。"""
    kind = Path(name).suffix[1:]
    return {"filename": name, "url": url or "https://files.example.org/" + name,
        "original_format": original or kind, "required_format": required or kind, "convert": conversion}


@pytest.fixture
def metadata_inputs(tmp_path, monkeypatch):
    """只建立 JSON/Parquet 字节占位及行读取 Stub，不请求网络或解码真实文档/数据集。"""
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "docs/test/ocb_preset/source"
    root.mkdir(parents=True)
    info = {"id": api.REPOS["ocb"], "sha": REVISION, "cardData": {"license": "metadata-only"}}
    entries = [tree_file("a.docx"), tree_file("b.xlsx")]
    qa = [{"track": "Domain", "reference_files": ["a.docx", "b.xlsx"], "question": "private-question"},
          {"track": "File Fidelity Word", "reference_files": ["a.docx"]},
          {"track": "File Fidelity PowerPoint", "reference_files": ["c.pptx", "converted.docx"]}]
    sources = [source_row("a.docx"), source_row("b.xlsx"), source_row("c.pptx"),
               source_row("converted.docx", conversion="yes", original="pdf")]
    (root / "dataset_info.json").write_text(json.dumps(info), encoding="utf-8")
    (root / "hosted_files.json").write_text(json.dumps(entries), encoding="utf-8")
    (root / "ocb_qna_data.parquet").write_bytes(b"qa-metadata-stub")
    (root / "ocb_file_sources.parquet").write_bytes(b"url-metadata-stub")
    (root / "source_review.json").write_bytes(b"[]\n")

    def read_rows(path):
        """按技术文件名返回行 Stub，任何非元数据访问都使测试失败。"""
        if Path(path).name == "ocb_qna_data.parquet":
            return qa
        assert Path(path).name == "ocb_file_sources.parquet"
        return sources

    monkeypatch.setattr(native.common, "rows_file", read_rows)
    monkeypatch.setattr(api, "fixed_metadata", Mock(return_value=(info, entries)))
    return root, qa, sources, entries


def permitted(plan):
    """仅在用例中显式创建用户审核 Stub；下载实现本身不得填写批准值。"""
    return {item["reference_file"]: {"reference_file": item["reference_file"], "source_url": item["source_url"],
        "original_native": True, "local_use_confirmed": True, "conversion_required": False,
        "license": "explicit-test-review"} for item in plan["candidates"] if item["reason"] is None}


def payload_validator(path, spec):
    """只校验传输 Stub 的大小/摘要，不冒充 OOXML/CSV/XLSM 格式解析。"""
    raw = path.read_bytes()
    if raw != PAYLOAD or len(raw) != spec.size or spec.sha256 and hashlib.sha256(raw).hexdigest() != spec.sha256:
        raise api.DownloadError("FORMAT")
    return {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "official_digest_checked": bool(spec.sha256 or spec.git_oid)}


def http_factory(handler):
    """为每原件返回独立 MockTransport 客户端，禁止真实 HTTP 和等待。"""
    def factory(endpoint, **kwargs):
        """保持正式 Network 参数形状；Mock 请求仅进入当前用例 Handler。"""
        return api.Network(endpoint, client=httpx.Client(transport=httpx.MockTransport(handler),
            headers={"Accept-Encoding": "identity"}), sleep=Mock(), **kwargs)
    return factory


def public_dns(host, port, **kwargs):
    """返回固定公共地址 Stub，不调用操作系统 DNS 或网络。"""
    return [(2, 1, 6, "", ("8.8.8.8", port))]


def candidate(plan, name):
    """按官方完整文件身份取候选，不按后缀或 basename 偷换来源。"""
    return next(item for item in plan["candidates"] if item["reference_file"] == name)


def test_all_tracks_and_multi_dependencies_are_preserved(metadata_inputs):
    """全部 QA 包括 Domain/多文件依赖；重复引用去重，HF 与外部同身份只下载 HF 一次。"""
    root, qa, sources, _ = metadata_inputs
    before = copy.deepcopy((qa, sources))
    plan = native.build_native_plan(root, options("--dry-run"), api)
    assert plan["qa_row_count"] == 3 and len(plan["qa_groups"]) == 3
    assert plan["qa_groups"][0]["track"] == "Domain"
    assert plan["qa_groups"][0]["reference_files"] == ["a.docx", "b.xlsx"]
    assert candidate(plan, "a.docx")["source_kind"] == "hf"
    assert candidate(plan, "a.docx")["local_file"] == "reference_files/a.docx"
    assert len(candidate(plan, "a.docx")["qa_ids"]) == 2
    assert candidate(plan, "c.pptx")["local_file"] == "external_files/c.pptx"
    assert candidate(plan, "converted.docx")["reason"] == "conversion_required"
    assert "private-question" not in json.dumps(plan)
    assert (qa, sources) == before
    assert plan["estimated_size"]["external_bytes"] is None
    assert plan["estimated_size"]["external_files_with_unknown_size"] == 1


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), ("no", False), ("yes", True),
    ("false", False), ("true", True), (None, None), ("", None), (0, None), ("unknown", None)])
def test_conversion_flag_never_guesses_missing_status(value, expected):
    """缺状态不当作 no，显式 yes/no 和布尔值映射保持确定性。"""
    assert native.conversion_value(value) is expected


def test_optional_formats_do_not_expand_business_contract(metadata_inputs):
    """CSV/XLSM 仅加入素材候选；默认不包含它们，也不导入业务注册表或解析器。"""
    root, qa, sources, _ = metadata_inputs
    sources.extend([source_row("rows.csv"), source_row("macros.xlsm")])
    qa.append({"track": "Domain", "reference_files": ["rows.csv", "macros.xlsm"]})
    default = native.build_native_plan(root, options(), api)
    assert candidate(default, "macros.xlsm")["reason"] == "outside_requested_formats"
    expanded = native.build_native_plan(root, options("--native-formats", "docx", "xlsx", "pptx", "csv", "xlsm"), api)
    assert candidate(expanded, "rows.csv")["reason"] is None
    assert candidate(expanded, "macros.xlsm")["reason"] is None
    assert set(expanded["formats"]) == native.FORMATS


def test_complete_tree_strings_are_still_rejected(metadata_inputs):
    """即使版本和 Parquet 正常，字符串列表也不能替代官方 type/path/size/OID 文件对象。"""
    root, _, _, entries = metadata_inputs
    entries[:] = [item["path"] for item in entries]
    with pytest.raises(native.common.BenchmarkError, match="hosted_tree"):
        native.build_native_plan(root, options(), api)


def test_hosted_file_still_requires_explicit_native_manifest(metadata_inputs):
    """HF 托管不意味着未经转换；来源状态缺失或需要转换时仍拒绝。"""
    root, _, sources, _ = metadata_inputs
    sources[0]["convert"] = None
    sources[1].update(original_format="zip", convert="yes")
    plan = native.build_native_plan(root, options(), api)
    assert candidate(plan, "a.docx")["reason"] == "native_status_unknown"
    assert candidate(plan, "b.xlsx")["reason"] == "conversion_required"


def test_ambiguous_source_and_missing_url_are_gaps(metadata_inputs):
    """不根据同名来源任选 URL；来源冲突、缺链接与状态不明分开报告。"""
    root, _, sources, _ = metadata_inputs
    sources.append(source_row("a.docx", url="https://different.example.org/a.docx"))
    sources[2]["url"] = None
    plan = native.build_native_plan(root, options(), api)
    assert candidate(plan, "a.docx")["reason"] == "ambiguous_official_manifest"
    assert candidate(plan, "c.pptx")["source_url"] is None
    assert candidate(plan, "c.pptx")["reason"] == "unsafe_source"


def test_licenses_are_not_approved_by_candidates_or_success(metadata_inputs):
    """模板不填批准，成功状态与人工许可统计彼此独立。"""
    root, _, _, _ = metadata_inputs
    plan = native.build_native_plan(root, options(), api)
    review = native.candidate_review(plan)
    assert review["not_approved"] is True
    assert all(row["review_template"]["local_use_confirmed"] is False for row in review["candidates"])
    item = candidate(plan, "a.docx")
    summary = native.report_summary(plan, [{"candidate_id": item["candidate_id"], "status": "verified_existing"}], {})
    assert summary["by_format"]["docx"]["download_success"] == 1
    assert summary["by_format"]["docx"]["license_pending"] == 1
    assert not native.approved(item, permitted(plan) | {"a.docx": {"source_url": "https://wrong.example.org"}})


def test_unapproved_source_never_creates_network(metadata_inputs):
    """执行标志不替代逐文件许可，未批准文件不会建立 HTTP 客户端。"""
    root, _, _, _ = metadata_inputs
    plan = native.build_native_plan(root, options(), api)
    forbidden = Mock(side_effect=AssertionError("未审核不得联网"))
    result = native.transfer_one(candidate(plan, "a.docx"), plan, {}, root, options("--execute"), api,
        native.ByteBudget(10**8), network_factory=forbidden, validator=payload_validator)
    assert result["status"] == "blocked" and result["reason"] == "license_review_pending"
    forbidden.assert_not_called()


def test_hf_download_and_resume_skip_complete_file(metadata_inputs):
    """固定 revision+LFS 身份完整下载后同命令复用，不再次请求；保留真实 HTTP 数字。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--resume")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "a.docx")
    calls = []

    def handler(request):
        """只允许镜像 resolve 同一 SHA 原件，返回字节 Stub 而非文档。"""
        calls.append(request)
        assert request.url.path.endswith("/" + REVISION + "/reference_files/a.docx")
        return httpx.Response(200, content=PAYLOAD)

    budget = native.ByteBudget(10**8)
    first = native.transfer_one(item, plan, permitted(plan), root, args, api, budget,
        network_factory=http_factory(handler), validator=payload_validator)
    assert first["status"] == "downloaded" and first["http_status"] == 200
    second = native.transfer_one(item, plan, permitted(plan), root, args, api, budget,
        network_factory=http_factory(handler), validator=payload_validator)
    assert second["status"] == "verified_existing" and len(calls) == 1
    assert not (root / "reference_files/a.docx.part").exists()


def test_external_download_has_only_local_sha_and_offline_verification(metadata_inputs):
    """第三方长度取响应头；完成后只有本地 SHA，不冒充官方摘要，离线复核无 HTTP。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "c.pptx")
    calls = Mock(return_value=httpx.Response(200, content=PAYLOAD, headers={"ETag": '"v1"'}))
    first = native.transfer_one(item, plan, permitted(plan), root, args, api, native.ByteBudget(10**8),
        network_factory=http_factory(calls), validator=payload_validator, resolver=public_dns)
    assert first["status"] == "downloaded" and first["http_status"] == 200
    assert first["result"]["official_digest_checked"] is False
    assert calls.call_count == 2  # 第一次只读响应头，第二次下载原字节。
    forbidden = Mock(side_effect=AssertionError("离线复核不得联网"))
    second = native.transfer_one(item, plan, {}, root, options("--verify-only"), api, native.ByteBudget(10**8),
        network_factory=forbidden, validator=payload_validator)
    assert second["status"] == "verified_existing"
    forbidden.assert_not_called()


def test_external_execution_requires_additional_authorization(metadata_inputs):
    """逐文件许可仍不能取代显式第三方执行授权，缺 allow-external 时不创建客户端。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute")
    plan = native.build_native_plan(root, args, api)
    forbidden = Mock(side_effect=AssertionError("第三方执行未授权"))
    result = native.transfer_one(candidate(plan, "c.pptx"), plan, permitted(plan), root, args, api,
        native.ByteBudget(10**8), network_factory=forbidden)
    assert result["reason"] == "external_not_authorized" and result["status"] == "blocked"
    forbidden.assert_not_called()


def test_unknown_remote_size_does_not_guess_or_publish(metadata_inputs):
    """无 Content-Length 的第三方响应明确列缺口，不按已读取前缀或默认数字伪造总长度。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    response = httpx.Response(200, stream=httpx.ByteStream(PAYLOAD))
    result = native.transfer_one(candidate(plan, "c.pptx"), plan, permitted(plan), root, args, api,
        native.ByteBudget(10**8), network_factory=http_factory(Mock(return_value=response)),
        validator=payload_validator, resolver=public_dns)
    assert result["reason"] == "unknown_content_length" and not (root / "external_files/c.pptx").exists()


@pytest.mark.parametrize("macro_enabled", [True, False])
def test_xlsm_validation_uses_container_metadata_without_macro_execution(tmp_path, monkeypatch, macro_enabled):
    """仅 Mock ZIP 元数据验证宏工作簿类型，不生成 XLSM、运行 Excel 或执行 VBA。"""
    path = tmp_path / "transport.bin"
    path.write_bytes(PAYLOAD)
    main_type = "application/vnd.ms-excel.sheet.macroEnabled.main+xml" if macro_enabled else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    types = ('<Types><Override PartName="/xl/workbook.xml" ContentType="' + main_type + '"/></Types>').encode()
    archive = Mock()
    archive.infolist.return_value = [Mock(file_size=128)]
    archive.getinfo.return_value = Mock(file_size=128)
    archive.read.side_effect = [types, b"workbook-metadata-stub", types]
    manager = Mock()
    manager.__enter__ = Mock(return_value=archive)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(native, "ZipFile", Mock(return_value=manager))
    monkeypatch.setattr(native.common.ohr, "archive_index", Mock(return_value=({}, "metadata-only")))
    spec = api.FileSpec("macro.xlsm", "macro.xlsm", api.REPOS["ocb"], REVISION, len(PAYLOAD), None, None, "xlsm")
    if macro_enabled:
        assert native.validate_native_file(path, spec, api=api)["official_digest_checked"] is False
    else:
        with pytest.raises(api.DownloadError, match="DOWNLOAD_FORMAT"):
            native.validate_native_file(path, spec, api=api)


@pytest.mark.parametrize("status,reason", [(403, "http_forbidden"), (404, "http_not_found"), (401, "login_required")])
def test_real_http_errors_are_separate(metadata_inputs, status, reason):
    """状态码原样保留，但服务器私有正文不进入错误/报告或正式原件。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    result = native.transfer_one(candidate(plan, "c.pptx"), plan, permitted(plan), root, args, api,
        native.ByteBudget(10**8), network_factory=http_factory(Mock(return_value=httpx.Response(status, text="private-secret-body"))),
        validator=payload_validator, resolver=public_dns)
    assert result["status"] == "failed" and result["reason"] == reason and result["http_status"] == status
    assert "private-secret-body" not in json.dumps(result)
    assert not (root / "external_files/c.pptx").exists()


@pytest.mark.parametrize("headers,body,reason", [({"Content-Type": "text/html"}, b"<html>Sign in</html>", "login_required"),
    ({"Content-Type": "text/html"}, b"<html>error page</html>", "format_mismatch"),
    ({"Content-Length": str(201 * 1024**2)}, b"", "file_too_large")])
def test_login_format_and_size_are_not_reported_as_success(metadata_inputs, headers, body, reason):
    """登录页、格式伪装和过大文件各有安全原因，不发布成功原件。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    result = native.transfer_one(candidate(plan, "c.pptx"), plan, permitted(plan), root, args, api,
        native.ByteBudget(10**9), network_factory=http_factory(Mock(return_value=httpx.Response(200, headers=headers, content=body))),
        validator=payload_validator, resolver=public_dns)
    assert result["reason"] == reason and not (root / "external_files/c.pptx").exists()


@pytest.mark.parametrize("unsafe", ["http://files.example.org/a.xlsx", "https://localhost/a.xlsx",
    "https://127.0.0.1/a.xlsx", "https://user:password@files.example.org/a.xlsx",
    "https://files.example.org/a.xlsx?token=secret"])
def test_unsafe_external_source_rejected_without_secret_output(unsafe):
    """无 TLS、内网、凭据或签名 URL 不能作为原始公开素材来源，不打印敏感原值。"""
    with pytest.raises(native.NativeError) as failure:
        native.public_url(unsafe)
    assert unsafe not in str(failure.value) and "password" not in str(failure.value)


def test_dns_and_redirects_are_checked_before_request(metadata_inputs):
    """拒绝解析到私网的来源或未经许可的跳转，不自动带登录态或信任任意 CDN。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "c.pptx")
    handler = Mock(return_value=httpx.Response(200, content=PAYLOAD))
    resolver = Mock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))])
    result = native.transfer_one(item, plan, permitted(plan), root, args, api, native.ByteBudget(10**8),
        network_factory=http_factory(handler), validator=payload_validator, resolver=resolver)
    assert result["reason"] == "unsafe_source"
    handler.assert_not_called()
    redirect = Mock(return_value=httpx.Response(302, headers={"Location": "https://unreviewed.example.org/file"}))
    result = native.transfer_one(item, plan, permitted(plan), root, args, api, native.ByteBudget(10**8),
        network_factory=http_factory(redirect), validator=payload_validator, resolver=public_dns)
    assert result["reason"] == "source_redirect_not_reviewed" and redirect.call_count == 1


def external_checkpoint(root, plan, item, *, etag='"stable"'):
    """只建立本下载器的传输断点 Stub，绑定官方 URL、版本、大小与强 ETag。"""
    spec = native.ExternalSpec(item["local_file"], item["reference_file"], "external:ocb", REVISION,
                               len(PAYLOAD), item["file_type"], item["source_url"])
    downloader = api.Downloader(root, None, payload_validator)
    api.write_private(downloader.state_path(spec), {"spec": asdict(spec), "etag": etag, "result": None}, root)
    part = root / (item["local_file"] + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(PAYLOAD[:5])
    return spec, part


@pytest.mark.parametrize("reply", ["valid", "ignored_range", "changed_etag"])
def test_external_resume_requires_range_and_unchanged_etag(metadata_inputs, reply):
    """仅 206 且精确 Content-Range/强 ETag 匹配才续传，拒绝后保留原断点。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--resume", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "c.pptx")
    _, part = external_checkpoint(root, plan, item)

    def handler(request):
        """检查只沿同一来源恢复；错误分支不提供可安全追加的字节。"""
        assert request.headers["Range"] == "bytes=5-"
        assert request.headers["If-Range"] == '"stable"'
        if reply == "ignored_range":
            return httpx.Response(200, content=PAYLOAD)
        return httpx.Response(206, content=PAYLOAD[5:], headers={"Content-Range": f"bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}",
            "ETag": '"changed"' if reply == "changed_etag" else '"stable"'})

    result = native.transfer_one(item, plan, permitted(plan), root, args, api, native.ByteBudget(10**8),
        network_factory=http_factory(handler), validator=payload_validator, resolver=public_dns)
    if reply == "valid":
        assert result["status"] == "downloaded" and (root / item["local_file"]).read_bytes() == PAYLOAD
    else:
        assert result["reason"] == "resume_rejected" and part.read_bytes() == PAYLOAD[:5]
        assert not (root / item["local_file"]).exists()


def test_external_without_strong_etag_does_not_resume(metadata_inputs):
    """第三方无官方摘要且无强 ETag 时拒绝拼接，即使服务器返回 206 也不能冒充完整。"""
    root, _, _, _ = metadata_inputs
    args = options("--execute", "--resume", "--allow-external")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "c.pptx")
    _, part = external_checkpoint(root, plan, item, etag=None)
    response = httpx.Response(206, content=PAYLOAD[5:], headers={"Content-Range": f"bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})
    result = native.transfer_one(item, plan, permitted(plan), root, args, api, native.ByteBudget(10**8),
        network_factory=http_factory(Mock(return_value=response)), validator=payload_validator, resolver=public_dns)
    assert result["reason"] == "resume_rejected" and part.read_bytes() == PAYLOAD[:5]


def test_unknown_existing_external_is_not_overwritten(metadata_inputs):
    """没有来源/Hash 回执的用户原件保留，不悄悄覆盖或宣称其来源校验成功。"""
    root, _, _, _ = metadata_inputs
    args = options("--verify-only")
    plan = native.build_native_plan(root, args, api)
    item = candidate(plan, "c.pptx")
    target = root / item["local_file"]
    target.parent.mkdir()
    target.write_bytes(b"user-original-untouched")
    forbidden = Mock(side_effect=AssertionError("只读复核不联网"))
    result = native.transfer_one(item, plan, {}, root, args, api, native.ByteBudget(10**8),
        network_factory=forbidden, validator=payload_validator)
    assert result["reason"] == "existing_file_unverified" and target.read_bytes() == b"user-original-untouched"
    forbidden.assert_not_called()


def test_byte_budget_atomic_and_cancellation_preserves_part(metadata_inputs):
    """容量重复预留幂等、超限失败；取消只停止请求，不删除既有断点。"""
    budget = native.ByteBudget(10)
    budget.reserve("a", 6)
    budget.reserve("a", 6)
    with pytest.raises(native.NativeError, match="total_budget_exceeded"):
        budget.reserve("b", 5)
    root, _, _, _ = metadata_inputs
    plan = native.build_native_plan(root, options(), api)
    item = candidate(plan, "c.pptx")
    spec, part = external_checkpoint(root, plan, item)
    event = threading.Event()
    event.set()
    downloader = api.Downloader(root, None, payload_validator, cancel_event=event)
    with pytest.raises(api.DownloadError, match="DOWNLOAD_CANCELLED"):
        downloader.download(spec)
    assert part.read_bytes() == PAYLOAD[:5]


def test_dry_run_default_creates_only_metadata_reports(metadata_inputs, monkeypatch):
    """默认与显式 dry-run 都不联网；报告与许可模板分离，原 Parquet/审核/抽样计划不变。"""
    root, _, _, _ = metadata_inputs
    forbidden = Mock(side_effect=AssertionError("规划不创建网络客户端"))
    monkeypatch.setattr(api, "Network", forbidden)
    frozen = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert native.run_native_library(options("--dry-run"), api) == 0
    results = root.parent / "results"
    assert all((results / name).is_file() for name in native.REPORT_NAMES)
    report_bytes = (results / "native_download_report.json").read_bytes()
    assert native.run_native_library(options(), api) == 0
    assert (results / "native_download_report.json").read_bytes() == report_bytes
    assert not list((root / "reference_files").iterdir()) and not list((root / "external_files").iterdir())
    for path, before in frozen.items():
        assert path.read_bytes() == before
    forbidden.assert_not_called()


def test_missing_mode_is_offline_and_preserves_selection_reports(metadata_inputs, monkeypatch):
    """缺失列表只核对本地，全部未下载/转换项各自报告；既有抽样计划不改。"""
    root, _, _, _ = metadata_inputs
    results = root.parent / "results"
    results.mkdir()
    existing_plan = results / "selection_plan.json"
    existing_plan.write_bytes(b'{"user":"previous-selection-stub"}')
    monkeypatch.setattr(api, "Network", Mock(side_effect=AssertionError("缺失检查不联网")))
    assert native.run_native_library(options("--list-missing"), api) == 2
    report = api.read_json(results / "native_download_report.json")
    assert any(item["status"] == "missing" for item in report["records"])
    assert existing_plan.read_bytes() == b'{"user":"previous-selection-stub"}'


def test_failure_history_survives_offline_recheck(metadata_inputs, monkeypatch):
    """只用结果 Stub 验证下载失败/真实 HTTP 数字不会被后续离线复核抹掉，许可表不变。"""
    root, _, _, _ = metadata_inputs
    plan = native.build_native_plan(root, options(), api)
    review_bytes = json.dumps(list(permitted(plan).values())).encode()
    (root / "source_review.json").write_bytes(review_bytes)

    def outcome(item, plan, reviews, root, args, api, budget, **kwargs):
        """模拟单项失败后已由用户处理的结果，不读/写业务原件、不发请求或解析。"""
        if item["reason"]:
            status, reason, http = "blocked", item["reason"], None
        elif item["reference_file"] == "a.docx" and args.execute:
            status, reason, http = "failed", "http_forbidden", 403
        else:
            status, reason, http = "verified_existing", None, None
        return {"candidate_id": item["candidate_id"], "reference_file": item["reference_file"],
            "source_kind": item["source_kind"], "source_url": item["source_url"], "local_file": item["local_file"],
            "qa_ids": item["qa_ids"], "status": status, "reason": reason, "http_status": http, "result": None}

    monkeypatch.setattr(native, "transfer_one", outcome)
    monkeypatch.setattr(api, "Network", Mock(side_effect=AssertionError("结果 Stub 不联网")))
    assert native.run_native_library(options("--execute", "--allow-external"), api) == 1
    assert native.run_native_library(options("--verify-only"), api) == 2  # 转换项缺口仍在，非完整验收。
    report = api.read_json(root.parent / "results/native_download_report.json")
    row = next(item for item in report["records"] if item["reference_file"] == "a.docx")
    assert row["status"] == "verified_existing"
    assert any(event["http_status"] == 403 and event["reason"] == "http_forbidden" for event in row["history"])
    assert (root / "source_review.json").read_bytes() == review_bytes
    assert report["summary"]["qa_groups_with_all_files_verified"] == 2


def test_worker_limit_is_explicit():
    """默认/最大三并发；不允许命令行无界放大线程数或间接启动业务 Worker。"""
    assert options().workers == 3
    with pytest.raises(SystemExit):
        options("--workers", "4")


def test_report_path_is_project_relative_and_user_report_not_overwritten(metadata_inputs, monkeypatch, tmp_path):
    """从其他工作目录运行仍写固定项目 results；非本工具报告拒绝覆盖，不碰原内容。"""
    root, _, _, _ = metadata_inputs
    other = tmp_path / "unrelated-working-directory"
    other.mkdir()
    monkeypatch.chdir(other)
    args = options("--dry-run", "--native-results-dir", "docs/test/ocb_preset/results/native-02")
    assert native.run_native_library(args, api) == 0
    target = root.parent / "results/native-02/native_download_report.json"
    target.write_bytes(b'{"user":"do-not-overwrite"}')
    with pytest.raises(api.DownloadError, match="DOWNLOAD_EXISTS"):
        native.run_native_library(args, api)
    assert target.read_bytes() == b'{"user":"do-not-overwrite"}'
    assert not (other / "docs").exists()


@pytest.mark.parametrize("extra", [["--resume"], ["--dry-run", "--allow-external"], ["--max-file-mb", "513"]])
def test_invalid_native_flags_fail_before_creating_runtime_files(tmp_path, monkeypatch, extra):
    """参数非法时拒绝，不因所谓部署/实现授权创建运行原件或联网。"""
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    assert api.main(["--dataset", "ocb", "--scope", "native-library", *extra]) == 1
    assert not (tmp_path / "docs").exists()


def test_formats_and_scope_must_not_leak_to_existing_modes(tmp_path, monkeypatch):
    """只有新增 OCB 范围能显式 execute；不改变 OHR/Omni 既有流程的授权含义。"""
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    assert api.main(["--dataset", "ohr", "--scope", "required", "--execute"]) == 1
    assert api.main(["--dataset", "omni", "--scope", "native-library", "--dry-run"]) == 1
    assert not (tmp_path / "docs").exists()
