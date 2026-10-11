"""统一 Office 内容校验、显式云端协议和 Office 坐标语义的离线测试。"""

import hashlib
import json
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from mineru_samples import result_zip
from office_samples import office_bytes, ole_bytes
from test_mineru_provider import RESULT_URL, SIGNED_URL, StubLimiter

from app.core.config import Settings
from app.core.file_formats import OFFICE_TYPES, FileFormatError, validate_file_content
from app.services.mineru.adapter import MinerUResultAdapter, page_geometry
from app.services.mineru.errors import MinerUError
from app.services.mineru.jobs import parse_options
from app.services.mineru.provider import (
    LocalFile,
    MinerUCloudProvider,
    validate_local_file,
)


@pytest.mark.parametrize("file_type", sorted(OFFICE_TYPES))
def test_valid_office_container(file_type):
    """六种 Office 通过真实 ZIP/CFB 目录结构检查，并复位输入流。"""

    stream = BytesIO(office_bytes(file_type))
    validate_file_content(stream, file_type)
    assert stream.tell() == 0


@pytest.mark.parametrize("actual", sorted(OFFICE_TYPES))
@pytest.mark.parametrize("claimed", sorted(OFFICE_TYPES))
def test_office_suffix_structure_mismatch(actual, claimed):
    """同签名的不同 Office 主部件不能通过改后缀绕过校验。"""

    if actual == claimed:
        validate_file_content(BytesIO(office_bytes(actual)), claimed)
        return
    with pytest.raises(FileFormatError):
        validate_file_content(BytesIO(office_bytes(actual)), claimed)


@pytest.mark.parametrize("claimed", ["doc", "xls", "ppt"])
@pytest.mark.parametrize(
    "payload",
    [
        b"not ole",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512,
        ole_bytes(marker="UnknownStream"),
    ],
)
def test_invalid_ole_fails(claimed, payload):
    """仅 OLE 魔数或未知主流不足以证明 Office 文件格式。"""

    with pytest.raises(FileFormatError):
        validate_file_content(BytesIO(payload), claimed)


@pytest.mark.parametrize("file_type", sorted(OFFICE_TYPES))
def test_office_mineru_http_and_result_contract(tmp_path, file_type):
    """六种 Office Mock 申请/PUT/查询/ZIP，页数未知、坐标为空、正文与 ID 顺序稳定。"""

    path = tmp_path / f"中文样本.{file_type}"
    content = office_bytes(file_type)
    path.write_bytes(content)
    source = LocalFile(
        path, path.name, "dkf-office", hashlib.sha256(content).hexdigest()
    )
    assert validate_local_file(source) is None
    assert page_geometry(path, file_type) is None
    settings = Settings(_env_file=None, mineru_api_token="unit-secret-not-real")
    limiter = StubLimiter()
    seen = []

    def handler(request):
        """模拟官方契约；API 保留鉴权，对象存储与结果 CDN 不携带 Token。"""

        seen.append(request.method)
        if request.method == "POST":
            assert json.loads(request.content)["files"][0]["name"] == path.name
            assert request.headers["Authorization"] == "Bearer unit-secret-not-real"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"batch_id": "office-batch", "file_urls": [SIGNED_URL]},
                },
            )
        if request.method == "PUT":
            assert (
                "Authorization" not in request.headers
                and "Content-Type" not in request.headers
            )
            assert request.content == content
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "office-batch",
                        "extract_result": [
                            {
                                "data_id": "dkf-office",
                                "state": "done",
                                "full_zip_url": RESULT_URL,
                            }
                        ],
                    },
                },
            )
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            content=result_zip(
                [
                    {
                        "type": "table",
                        "table_body": "表格正文",
                        "bbox": [0, 0, 500, 500],
                        "page_idx": 2,
                    },
                    {"type": "equation", "text": "x=1", "page_idx": 0},
                ]
            ),
        )

    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=limiter,
    )
    try:
        batch = provider.request_upload_urls([source], parse_options(settings))
        provider.upload(batch.urls[0], source)
        result = provider.query(batch.batch_id, source.data_id)
        archive = provider.download_result(result["full_zip_url"])
        doc_id = uuid4()
        adapter = MinerUResultAdapter()
        parsed = adapter.normalize(
            archive,
            doc_id=doc_id,
            file_name=path.name,
            file_type=file_type,
            geometry=None,
            identity="office-hash",
        )
        assert [block.page for block in parsed.blocks] == [3, 1]
        assert [block.block_type for block in parsed.blocks] == ["table", "formula"]
        assert all(
            block.bbox is None and block.source == "mineru_cloud"
            for block in parsed.blocks
        )
        assert parsed == adapter.normalize(
            archive,
            doc_id=doc_id,
            file_name=path.name,
            file_type=file_type,
            geometry={0: (800, 1000)},
            identity="office-hash",
        )
        assert limiter.calls == [("submit", 1, 0), ("query", 1, 0)]
        assert seen == ["POST", "PUT", "GET", "GET"]
    finally:
        provider.close()


def test_csv_never_submitted_to_mineru(tmp_path):
    """CSV 即使可读也不得进入云端提交或结果转换。"""

    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2", encoding="utf-8")
    with pytest.raises(MinerUError, match="CSV"):
        validate_local_file(LocalFile(path, path.name, "csv-id"))


@pytest.mark.parametrize("file_type", sorted(OFFICE_TYPES))
def test_invalid_office_never_consumes_quota_or_sends_http(tmp_path, file_type):
    """伪装 Office 在共享校验阶段拒绝，不预留云额度或泄露原件内容。"""

    path = tmp_path / f"fake.{file_type}"
    path.write_bytes(b"invalid office")
    limiter = StubLimiter()

    def forbidden_http(request):
        """无效原件不应触发任何外部 HTTP，即使是 Mock 请求。"""

        raise AssertionError("无效格式触发云调用")

    settings = Settings(_env_file=None, mineru_api_token="unit-secret-not-real")
    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(forbidden_http)),
        limiter=limiter,
    )
    try:
        with pytest.raises(MinerUError) as caught:
            provider.request_upload_urls(
                [LocalFile(path, path.name, "office-id")], parse_options(settings)
            )
        assert caught.value.code == "FILE_FORMAT" and not caught.value.retryable
        assert limiter.calls == []
    finally:
        provider.close()


def test_office_platform_page_limit_is_permanent_and_safe(tmp_path):
    """未知 Office 页数由平台最终拒绝，安全映射为不可重试错误而不伪造本地页数。"""

    path = tmp_path / "limit.xlsx"
    path.write_bytes(office_bytes("xlsx"))
    limiter = StubLimiter()

    def page_limit(request):
        """模拟官方超页数错误，原始错误消息不得直接暴露。"""

        return httpx.Response(
            200, json={"code": -60006, "msg": "private signed-url secret"}
        )

    settings = Settings(_env_file=None, mineru_api_token="unit-secret-not-real")
    provider = MinerUCloudProvider(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(page_limit)),
        limiter=limiter,
    )
    try:
        with pytest.raises(MinerUError) as caught:
            provider.request_upload_urls(
                [LocalFile(path, path.name, "office-id")], parse_options(settings)
            )
        assert caught.value.code == "-60006" and not caught.value.retryable
        assert "private" not in str(caught.value)
        assert limiter.calls == [("submit", 1, 0)]
    finally:
        provider.close()


@pytest.mark.parametrize("page", [-1, True, "1", None, 200])
def test_office_rejects_invalid_logical_page(page):
    """Office 逻辑页仍需真实整数和平台范围，不因未知几何信息放松基本契约。"""

    with pytest.raises(MinerUError) as caught:
        MinerUResultAdapter().normalize(
            result_zip([{"type": "text", "text": "正文", "page_idx": page}]),
            doc_id=uuid4(),
            file_name="sample.docx",
            file_type="docx",
            geometry=None,
            identity="hash",
        )
    assert caught.value.code == "PAGE"
