"""Provider、限流和结果 Adapter 的离线测试，不连接真实 MinerU。"""

import hashlib
import json
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from mineru_samples import content_items, pdf_bytes, png_bytes, result_zip
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.services.mineru.adapter import (
    MinerUResultAdapter,
    page_geometry,
    read_content_list,
)
from app.services.mineru.errors import MinerUError, api_error
from app.services.mineru.jobs import parse_options
from app.services.mineru.provider import (
    LocalFile,
    MinerUCloudProvider,
    validate_local_file,
    validate_signed_url,
)
from app.services.mineru.rate_limit import MinerURateLimiter

SIGNED_URL = "https://mineru.oss-cn-shanghai.aliyuncs.com/mock?signature=private"
RESULT_URL = "https://cdn-mineru.openxlab.org.cn/mock.zip?signature=private"


class StubLimiter:
    """替代真实 Redis，记录提交文件数及结果查询次数。"""

    def __init__(self):
        """为每个测试建立独立调用记录。"""

        self.calls = []

    def reserve(self, kind, files=1, pages=0):
        """记录调用，不等待、不写共享 Redis。"""

        self.calls.append((kind, files, pages))
        return False


@pytest.fixture
def settings():
    """显式禁用本机 .env，使用不可用于真实鉴权的测试 Token。"""

    return Settings(_env_file=None, mineru_api_token="unit-secret-not-real")


@pytest.fixture
def source(tmp_path):
    """保存真实 PDF 到测试临时目录，并附加内容 Hash。"""

    content = pdf_bytes()
    path = tmp_path / "测试.pdf"
    path.write_bytes(content)
    return LocalFile(path, path.name, "dkf-test", hashlib.sha256(content).hexdigest())


def test_precision_api_contract_and_signed_upload_security(
    settings, source, caplog, monkeypatch
):
    """默认客户端统一直连 API、上传和 CDN，并验证协议及敏感日志边界。"""

    limiter = StubLimiter()
    seen = []

    def handler(request):
        """模拟官方 HTTP 协议，并在对象存储边界拒绝任何 Token 与 Content-Type。"""

        seen.append(request.method)
        if request.url.host == "mineru.net":
            assert request.headers["Authorization"] == "Bearer unit-secret-not-real"
            if request.method == "POST":
                assert request.url.path == "/api/v4/file-urls/batch"
                assert json.loads(request.content) == {
                    "files": [
                        {"name": source.name, "data_id": source.data_id, "is_ocr": True}
                    ],
                    "model_version": "vlm",
                    "language": "ch",
                    "enable_table": True,
                    "enable_formula": True,
                }
                data = {"batch_id": "batch-1", "file_urls": [SIGNED_URL]}
            else:
                assert request.url.path == "/api/v4/extract-results/batch/batch-1"
                data = {
                    "batch_id": "batch-1",
                    "extract_result": [
                        {
                            "data_id": source.data_id,
                            "state": "done",
                            "full_zip_url": RESULT_URL,
                        }
                    ],
                }
            return httpx.Response(
                200, json={"code": 0, "trace_id": "trace-1", "data": data}
            )
        assert "authorization" not in request.headers
        assert "content-type" not in request.headers
        if request.method == "PUT":
            assert request.content == source.path.read_bytes()
        return httpx.Response(
            200, content=result_zip() if request.method == "GET" else b""
        )

    original_client = httpx.Client

    def create_direct_client(**kwargs):
        """保留生产客户端的直连参数，仅替换传输层，避免测试触发真实云调用。"""

        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == settings.mineru_request_timeout_seconds
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        "app.services.mineru.provider.httpx.Client", create_direct_client
    )
    provider = MinerUCloudProvider(
        settings,
        limiter=limiter,
    )
    batch = provider.request_upload_urls([source], parse_options(settings))
    provider.upload(batch.urls[0], source)
    result = provider.query(batch.batch_id, source.data_id)
    assert (
        read_content_list(provider.download_result(result["full_zip_url"]))
        == content_items()
    )
    assert limiter.calls == [("submit", 1, 2), ("query", 1, 0)]
    assert seen == ["POST", "PUT", "GET", "GET"]
    assert "unit-secret" not in caplog.text and "signature=private" not in caplog.text
    provider.close()


def test_default_client_ignores_proxy_and_certificate_environment(
    settings, monkeypatch
):
    """即使存在代理和自定义 CA 环境，默认客户端也不发现系统代理或关闭校验。"""

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "missing-mineru-test-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "missing-mineru-test-ca-directory")

    def reject_proxy_lookup():
        """代理发现入口被调用即失败，覆盖环境代理及 Windows 系统代理读取路径。"""

        pytest.fail("MinerU 默认客户端不应查询环境或系统代理")

    monkeypatch.setattr("httpx._client.get_environment_proxies", reject_proxy_lookup)
    provider = MinerUCloudProvider(settings, limiter=StubLimiter())
    try:
        assert provider.client.trust_env is False
    finally:
        provider.close()


@pytest.mark.parametrize(
    "status,code,retry",
    [(429, "HTTP_429", True), (503, "HTTP_503", True), (401, "HTTP_401", False)],
)
def test_http_errors_safe_and_classified(settings, source, status, code, retry):
    """429 和服务错误可重试；鉴权错误永久失败，响应原文及 URL 不进入异常。"""

    def handler(request):
        """返回带敏感噪声的失败响应，验证错误白名单而不是上游原文透传。"""

        return httpx.Response(
            status, headers={"Retry-After": "60"}, text="unit-secret-not-real"
        )

    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=StubLimiter(),
    )
    with pytest.raises(MinerUError) as error:
        provider.request_upload_urls([source], parse_options(settings))
    assert error.value.code == code and error.value.retryable is retry
    assert "unit-secret" not in str(error.value)
    if retry:
        assert error.value.retry_after == 60
    provider.close()


@pytest.mark.parametrize(
    "code,retry",
    [
        ("A0202", False),
        ("A0211", False),
        (-60002, False),
        (-60003, False),
        (-60004, False),
        (-60005, False),
        (-60006, False),
        (-60013, False),
        (-60017, False),
        (-60018, False),
        (-10001, True),
        (-60001, True),
        (-60007, True),
        (-60009, True),
        (-60010, True),
    ],
)
def test_official_business_error_classification(code, retry):
    """官方临时和永久错误使用受控解释，不猜测未知码、不自动刷新 Token。"""

    assert api_error(code).retryable is retry
    assert "unit-secret" not in str(api_error("unit-secret-not-real"))


@pytest.mark.parametrize(
    "exception,retry", [(httpx.ConnectError, True), (httpx.ReadTimeout, False)]
)
def test_post_uncertainty_is_not_blindly_retried(settings, source, exception, retry):
    """连接尚未建立可重试；POST 后读取超时结果不确定，禁止重复创建批次。"""

    def handler(request):
        """模拟带原始凭据的网络异常，异常边界必须去除上下文。"""

        raise exception("unit-secret-not-real", request=request)

    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=StubLimiter(),
    )
    with pytest.raises(MinerUError) as error:
        provider.request_upload_urls([source], parse_options(settings))
    assert error.value.retryable is retry
    assert "unit-secret" not in str(error.value)
    provider.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://mineru.net/file",
        "https://127.0.0.1/file",
        "https://evil.net/file",
        "https://mineru.net.evil.net/file",
        "https://user:pass@mineru.net/file",
        "https://mineru.net:8080/file",
    ],
)
def test_signed_url_is_restricted(url):
    """拒绝任意域名、私网 IP、明文协议、内嵌凭据与异常端口，不跟随跳转。"""

    with pytest.raises(MinerUError):
        validate_signed_url(url)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"file_hash": "a" * 64}, "FILE_CHANGED"),
        ({"name": "原件.txt"}, "FILE_TYPE"),
        ({"data_id": "../../bad"}, "BAD_RESPONSE"),
    ],
)
def test_preflight_hash_type_identifier(source, changes, code):
    """在云端额度预留前拒绝原件变更、格式不支持和无效业务标识。"""

    from dataclasses import replace

    with pytest.raises(MinerUError) as error:
        validate_local_file(replace(source, **changes))
    assert error.value.code == code


def test_preflight_pages_images_empty_and_damaged(source, tmp_path):
    """PDF 超过 200 页及损坏文件不得提交，真实图片可以读取并得到实际像素尺寸。"""

    source.path.write_bytes(pdf_bytes(201))
    with pytest.raises(MinerUError, match="200 页"):
        validate_local_file(LocalFile(source.path, source.name, "pages"))
    source.path.write_bytes(b"%PDF-invalid")
    with pytest.raises(MinerUError):
        validate_local_file(LocalFile(source.path, source.name, "damaged"))
    image = tmp_path / "图.png"
    image.write_bytes(png_bytes())
    assert validate_local_file(LocalFile(image, image.name, "image")) == 1
    assert page_geometry(image, "png") == {0: (600.0, 800.0)}
    image.write_bytes(b"")
    with pytest.raises(MinerUError, match="为空"):
        validate_local_file(LocalFile(image, image.name, "empty"))


def test_adapter_order_geometry_types_context_and_stable_ids():
    """五类块保留文本、章节和输入顺序；物理页码与坐标统一且未知置信度为空。"""

    identifier = uuid4()
    arguments = {
        "doc_id": identifier,
        "file_name": "测试.pdf",
        "file_type": "pdf",
        "geometry": {0: (600, 800)},
        "identity": "same-options",
    }
    first = MinerUResultAdapter().normalize(result_zip(), **arguments)
    second = MinerUResultAdapter().normalize(result_zip(), **arguments)
    assert first == second
    assert [block.block_type for block in first.blocks] == [
        "title",
        "text",
        "table",
        "formula",
        "image_text",
    ]
    assert all(
        block.source == "mineru_cloud" and block.page == 1 and block.confidence is None
        for block in first.blocks
    )
    assert first.blocks[0].bbox == [0, 0, 300, 80]
    assert (
        first.blocks[2].text == "测试表\n<table><tr><td>42</td></tr></table>\n单位：元"
    )
    assert first.blocks[3].text == "$$E=mc^2$$"
    assert "图片中的实际文字" in first.blocks[4].text
    assert all(block.section == "测试标题" for block in first.blocks)
    assert first != MinerUResultAdapter().normalize(
        result_zip(), **(arguments | {"identity": "new-options"})
    )


@pytest.mark.parametrize(
    "override,code",
    [
        ({"page_idx": 2}, "PAGE"),
        ({"page_idx": True}, "PAGE"),
        ({"bbox": [0, 0, 1001, 10]}, "BBOX"),
        ({"bbox": [10, 0, 0, 10]}, "BBOX"),
        ({"type": "unknown"}, "CONTENT_TYPE"),
        ({"text": ""}, "EMPTY_BLOCK"),
        ({"confidence": 2}, "CONTENT_LIST"),
    ],
)
def test_adapter_rejects_unknown_or_invalid_data(override, code):
    """不伪造页码、正文、置信度或未知类型；错误时不产生半份统一结果。"""

    item = {"type": "text", "text": "正文", "page_idx": 0} | override
    with pytest.raises(MinerUError) as error:
        MinerUResultAdapter().normalize(
            result_zip([item]),
            doc_id=uuid4(),
            file_name="测试.pdf",
            file_type="pdf",
            geometry={0: (600, 800)},
            identity="x",
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "items,name",
    [
        ([], "content_list.json"),
        ([[{"type": "text"}]], "content_list.json"),
        ([{"type": "text"}], "../content_list.json"),
        ([{"type": "text"}], "full.md"),
    ],
)
def test_zip_and_structured_result_fail_closed(items, name):
    """拒绝空结果、不支持的 V2、路径穿越和只有 Markdown 的结果，不读取任意文件。"""

    with pytest.raises(MinerUError):
        MinerUResultAdapter().normalize(
            result_zip(items, name),
            doc_id=uuid4(),
            file_name="测试.pdf",
            file_type="pdf",
            geometry={0: (600, 800)},
            identity="x",
        )


def test_zip_ambiguity_and_invalid_archive():
    """重复内容列表和非 ZIP 结果必须明确失败，不随意选取一个文件。"""

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("a_content_list.json", "[]")
        archive.writestr("b_content_list.json", "[]")
    for content in (buffer.getvalue(), b"not a zip"):
        with pytest.raises(MinerUError):
            read_content_list(content)


class StubRedis:
    """注入 Lua 执行响应，验证传入的计量单位和安全边界。"""

    def __init__(self, result):
        """保存独立返回值或 Redis 异常。"""

        self.result, self.arguments = result, None

    def eval(self, *args):
        """捕获 Lua 参数，不连接真实服务。"""

        self.arguments = args
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_shared_limiter_counts_files_and_safety(settings):
    """批量提交按文件数计量，查询按一次计量，安全余量不能被大配置绕过。"""

    client = StubRedis([1, 0, 1])
    limiter = MinerURateLimiter(settings, client)
    assert limiter.reserve("submit", files=30, pages=1200)
    assert client.arguments[4:7] == (30, 45, 5000)
    assert client.arguments[8:] == (1200, 1000)
    limiter.reserve("query", files=30)
    assert client.arguments[4:6] == (1, 900)
    with pytest.raises(MinerUError, match="拆分"):
        limiter.reserve("submit", files=46)


@pytest.mark.parametrize(
    "result,code,retry",
    [
        ([0, 31, 0], "RATE_LIMIT", True),
        ([-1, 0, 0], "DAILY_LIMIT", False),
        (RedisConnectionError("private"), "QUOTA_UNAVAILABLE", True),
    ],
)
def test_shared_limiter_failure_classification(settings, result, code, retry):
    """分钟限流延时重试，日额度耗尽永久失败，Redis 不可用时关闭云端调用。"""

    with pytest.raises(MinerUError) as error:
        MinerURateLimiter(settings, StubRedis(result)).reserve("submit")
    assert error.value.code == code and error.value.retryable is retry


def test_missing_token_does_not_leak_settings():
    """缺少 Token 时给出配置提示，且真实密钥的配置表示被 SecretStr 遮盖。"""

    with pytest.raises(MinerUError, match="MINERU_API_TOKEN"):
        MinerUCloudProvider(Settings(_env_file=None))
    assert "unit-secret-not-real" not in repr(
        Settings(_env_file=None, mineru_api_token="unit-secret-not-real")
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"batch_id": "batch-1", "file_urls": []},
        {"batch_id": "bad/token", "file_urls": [SIGNED_URL]},
    ],
)
def test_provider_malformed_envelope(settings, source, data):
    """返回字段缺失或标识无效时拒绝继续上传，不能猜测 batch 或链接对应关系。"""

    def handler(request):
        """模拟形状不完整的成功响应。"""

        return httpx.Response(200, json={"code": 0, "data": data})

    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=StubLimiter(),
    )
    with pytest.raises(MinerUError):
        provider.request_upload_urls([source], parse_options(settings))
    provider.close()


def test_batch_file_count_and_hard_max(settings, source):
    """两份本地文件计为两份和四页；超过 50 份或重复 data_id 不发送请求。"""

    from dataclasses import replace

    limiter = StubLimiter()

    def handler(request):
        """为批次内每份文件返回一个签名 URL。"""

        count = len(json.loads(request.content)["files"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"batch_id": "batch-1", "file_urls": [SIGNED_URL] * count},
            },
        )

    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=limiter,
    )
    files = [replace(source, data_id=f"file-{index}") for index in range(51)]
    provider.request_upload_urls(files[:2], parse_options(settings))
    assert limiter.calls == [("submit", 2, 4)]
    for batch in (files, [source, source]):
        with pytest.raises(MinerUError):
            provider.request_upload_urls(batch, parse_options(settings))
    assert len(limiter.calls) == 1
    provider.close()


def test_result_download_rejects_redirect_and_oversize(settings):
    """下载不跟随跳转，也不能突破配置的压缩 ZIP 字节数上限。"""

    for status, content in ((302, b""), (200, b"x" * (1024**2 + 1))):

        def handler(request, status=status, content=content):
            """返回跳转或超量流，验证安全下载边界。"""

            return httpx.Response(
                status, headers={"Location": "https://evil.net/"}, content=content
            )

        provider = MinerUCloudProvider(
            settings.model_copy(update={"mineru_result_max_mb": 1}),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            limiter=StubLimiter(),
        )
        with pytest.raises(MinerUError):
            provider.download_result(SIGNED_URL)
        provider.close()


def test_geometry_respects_pdf_crop_and_rotation(tmp_path):
    """PDF 坐标以实际渲染视口为基准，不能用原始 MediaBox 错算旋转裁剪页面。"""

    import pymupdf

    with pymupdf.open(stream=pdf_bytes(1), filetype="pdf") as pdf:
        pdf[0].set_cropbox(pymupdf.Rect(0, 0, 400, 500))
        pdf[0].set_rotation(90)
        path = tmp_path / "裁剪旋转.pdf"
        path.write_bytes(pdf.tobytes())
    assert page_geometry(path, "pdf") == {0: (500.0, 400.0)}
