"""Swagger 多选插件的页面集成测试，不写库、不投递任务或调用云 API。"""

import asyncio

import httpx
import pytest

from app.api.docs import SWAGGER_VERSION
from app.core.config import Settings
from app.core.file_formats import FILE_TYPES, MINERU_TYPES
from app.main import app


def docs_request(path, root_path=""):
    """通过 ASGI 获取页面与静态资源，模拟 root_path 而不启动额外服务。"""

    async def request():
        """隔离 HTTP 客户端生命周期，避免真实网络及本地监听端口依赖。"""

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, root_path=root_path),
            base_url="http://test",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


@pytest.mark.parametrize("root_path", ["", "/knowledge", "/knowledge/"])
def test_docs_load_plugin_before_initialization_and_support_root_path(root_path):
    """固定 Swagger 版本并在初始化前加载插件，代理前缀覆盖静态/OpenAPI/回跳。"""

    response = docs_request("/docs", root_path)
    html = response.text
    prefix = root_path.rstrip("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f"swagger-ui-dist@{SWAGGER_VERSION}/swagger-ui-bundle.js" in html
    assert f"swagger-ui-dist@{SWAGGER_VERSION}/swagger-ui.css" in html
    assert f'src="{prefix}/static/docs/swagger_upload.js"' in html
    assert html.index("swagger_upload.js") < html.index("const ui = SwaggerUIBundle")
    assert "plugins: [DkfFilePickerPlugin]" in html
    assert f"url: '{prefix}/openapi.json'" in html
    assert f"window.location.origin + '{prefix}/docs/oauth2-redirect'" in html


def test_docs_expose_only_upload_size_configuration(monkeypatch):
    """页面只暴露客户端初检必需的大小，不泄露密钥、连接串或真实目录。"""

    settings = Settings(
        max_upload_size_mb=7,
        mineru_api_token="SECRET_TOKEN_FOR_TEST",
        database_url="postgresql+psycopg://secret:SECRET_PASSWORD@localhost/test",
        upload_dir="D:/private-upload-dir",
    )

    def configured():
        """返回隔离配置，避免读取或修改本机真实环境文件。"""

        return settings

    monkeypatch.setattr("app.api.docs.get_settings", configured)
    html = docs_request("/docs").text
    assert '"dkfUploadMaxBytes": 7340032' in html
    for secret in ["SECRET_TOKEN_FOR_TEST", "SECRET_PASSWORD", "private-upload-dir"]:
        assert secret not in html


def test_static_plugin_is_available_and_docs_routes_stay_hidden():
    """插件由本地 API 提供，文档与静态路径不污染业务 OpenAPI 接口。"""

    response = docs_request("/static/docs/swagger_upload.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "function DkfFilePickerPlugin(system)" in response.text
    assert "multiple: true" in response.text
    paths = docs_request("/openapi.json").json()["paths"]
    assert "/docs" not in paths and "/docs/oauth2-redirect" not in paths
    assert not any("/static/" in path or "/batch" in path for path in paths)


def test_cache_maintenance_contract_explicit_force_and_no_body():
    """Reindex 无解析模式/请求体，Reparse 默认不确认收费，保持普通上传 auto 契约。"""

    schema = docs_request("/openapi.json").json()
    prefix = "/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}"
    reindex = schema["paths"][prefix + "/reindex"]["post"]
    reparse = schema["paths"][prefix + "/reparse"]["post"]
    assert "requestBody" not in reindex and "requestBody" not in reparse
    assert {p["name"] for p in reindex["parameters"]} == {"kb_id", "doc_id"}
    parameters = {p["name"]: p for p in reparse["parameters"]}
    assert parameters["force"]["schema"]["default"] is False
    assert parameters["parse_mode"]["schema"]["default"] == "auto"
    assert docs_request("/docs/oauth2-redirect").status_code == 200
    assert docs_request("/redoc").status_code == 200


@pytest.mark.parametrize(
    "suffix, allowed", [("documents", FILE_TYPES), ("mineru/documents", MINERU_TYPES)]
)
def test_only_original_upload_arrays_are_marked_for_picker(suffix, allowed):
    """保留 file 多选标记；普通上传额外暴露可选模式，显式云入口契约不变。"""

    schema = docs_request("/openapi.json").json()
    operation = schema["paths"][f"/api/admin/knowledge-bases/{{kb_id}}/{suffix}"][
        "post"
    ]
    reference = operation["requestBody"]["content"]["multipart/form-data"]["schema"][
        "$ref"
    ]
    body = schema["components"]["schemas"][reference.rsplit("/", 1)[1]]
    assert list(body["properties"]) == (
        ["file", "parse_mode"] if suffix == "documents" else ["file"]
    )
    assert body["required"] == ["file"]
    if suffix == "documents":
        mode = body["properties"]["parse_mode"]
        assert mode["enum"] == ["auto", "native", "mineru"]
        assert mode["default"] == "auto"
    file = body["properties"]["file"]
    assert file["type"] == "array"
    assert file["items"] == {"type": "string", "format": "binary"}
    assert file["x-dkf-file-picker"] == {"extensions": sorted(allowed), "maxFiles": 50}
