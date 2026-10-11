"""保留标准 Swagger 使用方式，仅为上传文件数组安装本地多选控件。"""

from fastapi import APIRouter, Request
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

from app.core.config import get_settings

router = APIRouter(include_in_schema=False)
# 插件使用 Swagger 的组件扩展点，固定版本避免 CDN 大版本内升级改变内部契约。
SWAGGER_VERSION = "5.32.0"
SWAGGER_ASSETS = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_VERSION}"


@router.get("/docs")
def swagger_docs(request: Request) -> HTMLResponse:
    """提供单控件文件多选页面；仅公开上传大小限制，兼容反向代理 root_path。"""

    root_path = request.scope.get("root_path", "").rstrip("/")
    response = get_swagger_ui_html(
        openapi_url=f"{root_path}{request.app.openapi_url}",
        title=f"{request.app.title} - Swagger UI",
        swagger_js_url=f"{SWAGGER_ASSETS}/swagger-ui-bundle.js",
        swagger_css_url=f"{SWAGGER_ASSETS}/swagger-ui.css",
        oauth2_redirect_url=f"{root_path}/docs/oauth2-redirect",
        swagger_ui_parameters={
            "dkfUploadMaxBytes": get_settings().max_upload_size_mb * 1024 * 1024,
            "showExtensions": False,
        },
    )
    html = response.body.decode("utf-8")
    initializer = "    <script>\n    const ui = SwaggerUIBundle({"
    presets = "    presets: ["
    # FastAPI 模板变化时明确失败，不能静默退回逐行文件控件并误称支持多选。
    if html.count(initializer) != 1 or html.count(presets) != 1:
        raise RuntimeError("Swagger 页面模板不兼容，请核对文档控件集成")
    html = html.replace(
        initializer,
        f'    <script src="{root_path}/static/docs/swagger_upload.js"></script>\n'
        + initializer,
        1,
    ).replace(presets, "    plugins: [DkfFilePickerPlugin],\n" + presets, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/docs/oauth2-redirect")
def swagger_oauth_redirect() -> HTMLResponse:
    """保留 Swagger 标准授权回跳页面；当前并未新增管理员鉴权。"""

    return get_swagger_ui_oauth2_redirect_html()
