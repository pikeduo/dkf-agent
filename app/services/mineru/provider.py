"""唯一 MinerU HTTP 边界：精准解析 V4、本地签名上传及受限结果下载。"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pymupdf

from app.core.config import Settings
from app.services.mineru.errors import MinerUError, api_error
from app.services.mineru.rate_limit import MinerURateLimiter

IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
EXTERNAL_STATES = {"waiting-file", "pending", "running", "converting", "done", "failed"}


@dataclass(frozen=True, repr=False)
class LocalFile:
    """已存储原件及业务标识；页数未知时不伪造 PDF 页数。"""

    path: Path
    name: str
    data_id: str
    file_hash: str | None = None


@dataclass(frozen=True, repr=False)
class UploadBatch:
    """签名链接仅在 Provider 和私有恢复检查点流转，不作为任务返回值。"""

    batch_id: str
    urls: list[str] = field(repr=False)
    trace_id: str | None = None


def safe_identifier(value: object, *, optional: bool = False) -> str | None:
    """只接受官方允许的安全标识，不持久化上游任意字符串或凭据。"""

    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not IDENTIFIER.fullmatch(value)
    ):
        raise MinerUError("BAD_RESPONSE", "MinerU 返回的任务标识格式无效")
    return value


def validate_signed_url(url: object) -> str:
    """只允许官方对象存储 HTTPS 地址，不允许凭据、私网 IP、跳转或任意域名。"""

    if not isinstance(url, str):
        raise MinerUError("BAD_URL", "MinerU 未返回有效的签名资源地址")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        approved = (
            hostname == "mineru.oss-cn-shanghai.aliyuncs.com"
            or hostname in {"mineru.net", "openxlab.org.cn"}
            or hostname.endswith((".mineru.net", ".openxlab.org.cn"))
        )
        if (
            parsed.scheme != "https"
            or not approved
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        raise MinerUError("BAD_URL", "MinerU 签名地址不在官方安全域名范围") from None
    return url


def validate_local_file(source: LocalFile) -> int:
    """检查类型、大小、实际可读性与 Hash；PDF 读取页数，返回优先级计数估计。"""

    if Path(source.name).suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise MinerUError("FILE_TYPE", "当前 MinerU 入口仅接受 PDF、JPG、JPEG、PNG")
    safe_identifier(source.data_id)
    try:
        size = source.path.stat().st_size
        if not size or not source.path.is_file():
            raise MinerUError("EMPTY_FILE", "MinerU 原件为空或不是普通文件")
        if size > 200 * 1024 * 1024:
            raise MinerUError("FILE_SIZE", "MinerU 文件大小超过 200 MB")
        digest = hashlib.sha256()
        with source.path.open("rb") as stream:
            header = stream.read(16)
            digest.update(header)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        extension = Path(source.name).suffix.lower()
        signatures = {
            ".pdf": header.startswith(b"%PDF-"),
            ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": header.startswith(b"\xff\xd8\xff"),
            ".jpeg": header.startswith(b"\xff\xd8\xff"),
        }
        if not signatures[extension]:
            raise MinerUError("FILE_FORMAT", "MinerU 原件标识与文件类型不匹配")
        if source.file_hash and digest.hexdigest() != source.file_hash:
            raise MinerUError(
                "FILE_CHANGED", "原件内容与上传 Hash 不一致，停止云端提交"
            )
        if extension == ".pdf":
            with pymupdf.open(source.path) as pdf:
                if pdf.needs_pass or pdf.is_repaired:
                    raise MinerUError("FILE_DAMAGED", "MinerU 原件为加密或损坏 PDF")
                if pdf.page_count == 0:
                    raise MinerUError("EMPTY_FILE", "MinerU PDF 不包含页面")
                if pdf.page_count > 200:
                    raise MinerUError("PAGE_LIMIT", "MinerU PDF 超过 200 页")
                return pdf.page_count
        pymupdf.Pixmap(str(source.path))
        return 1
    except FileNotFoundError:
        raise MinerUError("FILE_MISSING", "MinerU 原件不存在，请检查存储目录") from None
    except (pymupdf.FileDataError, ValueError, RuntimeError):
        raise MinerUError("FILE_DAMAGED", "MinerU PDF 原件无法读取") from None
    except OSError:
        raise MinerUError(
            "FILE_IO", "MinerU 原件暂时无法读取", retryable=True
        ) from None


class MinerUCloudProvider:
    """集中执行官方精准解析 HTTP，调用方负责持久化和 Celery 延时调度。"""

    def __init__(self, settings: Settings, *, client=None, limiter=None) -> None:
        """校验配置并创建直连客户端；可注入测试客户端，Token 不进入日志。

        默认客户端不读取环境或 Windows 系统代理，API、签名上传和结果下载
        共用该连接策略；仍验证 HTTPS 证书，不改变系统路由或 VPN 设置。
        """

        self.settings = settings
        if settings.mineru_api_base_url.rstrip("/") != "https://mineru.net/api/v4":
            raise MinerUError("CONFIG", "MinerU 仅使用官方 HTTPS 精准解析 API V4")
        if not settings.mineru_api_token.get_secret_value().strip():
            raise MinerUError("TOKEN_MISSING", "请先配置本地 MINERU_API_TOKEN")
        if settings.mineru_model_version not in {"vlm", "pipeline"}:
            raise MinerUError("CONFIG", "MinerU 模型版本只能为 vlm 或 pipeline")
        self.limiter = limiter or MinerURateLimiter(settings)
        # 统一绕过自动代理发现，避免 API 可用但结果 CDN 被本机代理拦截。
        self.client = client or httpx.Client(
            timeout=settings.mineru_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        # httpx INFO 会记录签名 URL；降低库日志级别，业务日志只记录安全标识。
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    def close(self) -> None:
        """释放短任务使用的 HTTP 客户端，不管理任何系统服务。"""

        self.client.close()
        if isinstance(self.limiter, MinerURateLimiter):
            self.limiter.close()

    def request_upload_urls(self, files: list[LocalFile], options: dict) -> UploadBatch:
        """按文件数量预留额度并申请本地批量上传链接，不在此 PUT 或轮询。"""

        if not files or len(files) > self.settings.mineru_upload_batch_max_files:
            raise MinerUError("BATCH_LIMIT", "MinerU 上传批次必须包含 1～50 个文件")
        if len({source.data_id for source in files}) != len(files):
            raise MinerUError("DATA_ID", "同一 MinerU 批次的 data_id 不能重复")
        pages = sum(validate_local_file(source) for source in files)
        self.limiter.reserve("submit", files=len(files), pages=pages)
        data = self._api(
            "POST",
            "/file-urls/batch",
            json={
                "files": [
                    {
                        "name": source.name,
                        "data_id": source.data_id,
                        "is_ocr": options["is_ocr"],
                    }
                    for source in files
                ],
                "model_version": options["model_version"],
                "language": options["language"],
                "enable_table": options["enable_table"],
                "enable_formula": options["enable_formula"],
            },
        )
        urls = data["data"].get("file_urls")
        if not isinstance(urls, list) or len(urls) != len(files):
            raise MinerUError("BAD_RESPONSE", "MinerU 上传链接数量与本次文件数量不一致")
        return UploadBatch(
            safe_identifier(data["data"].get("batch_id")),
            [validate_signed_url(url) for url in urls],
            safe_identifier(data.get("trace_id"), optional=True),
        )

    def upload(self, url: str, source: LocalFile) -> None:
        """以同一签名 URL 幂等 PUT 原件，不向对象存储发送 API Token 或 Content-Type。"""

        validate_local_file(source)
        try:
            with source.path.open("rb") as stream:
                response = self.client.put(validate_signed_url(url), content=stream)
            self._http_status(response)
        except (httpx.HTTPError, OSError):
            raise MinerUError(
                "UPLOAD_NETWORK", "MinerU 文件上传暂时失败", retryable=True
            ) from None

    def query(self, batch_id: str, data_id: str) -> dict:
        """只查询一次，并按 data_id 选择结果，不能把批次中另一份文档写入当前文档。"""

        safe_identifier(batch_id)
        safe_identifier(data_id)
        self.limiter.reserve("query")
        envelope = self._api("GET", f"/extract-results/batch/{batch_id}")
        payload = envelope["data"]
        if payload.get("batch_id") != batch_id:
            raise MinerUError("BAD_RESPONSE", "MinerU 返回的批次标识不匹配")
        results = payload.get("extract_result")
        if not isinstance(results, list):
            raise MinerUError("BAD_RESPONSE", "MinerU 结果列表格式无效")
        matches = [
            item
            for item in results
            if isinstance(item, dict) and item.get("data_id") == data_id
        ]
        if len(matches) != 1 or matches[0].get("state") not in EXTERNAL_STATES:
            raise MinerUError("BAD_RESPONSE", "MinerU 返回的文档标识或状态不匹配")
        return dict(matches[0]) | {
            "trace_id": safe_identifier(envelope.get("trace_id"), optional=True)
        }

    def download_result(self, url: str) -> bytes:
        """流式限长下载官方结果 ZIP，不解压到磁盘、不发送 Token、不跟随重定向。"""

        chunks, size = [], 0
        limit = self.settings.mineru_result_max_mb * 1024 * 1024
        try:
            with self.client.stream("GET", validate_signed_url(url)) as response:
                self._http_status(response)
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise MinerUError(
                            "ZIP_SIZE", "MinerU 结果 ZIP 超过下载大小限制"
                        )
                    chunks.append(chunk)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise MinerUError(
                "NETWORK", "MinerU 网络暂时无法连接", retryable=True
            ) from None
        except httpx.HTTPError:
            raise MinerUError(
                "DOWNLOAD_NETWORK", "MinerU 结果下载暂时失败", retryable=True
            ) from None
        return b"".join(chunks)

    def _api(self, method: str, path: str, **kwargs) -> dict:
        """发送鉴权 API 请求并验证业务 envelope，永不抛出响应原文或 Token。"""

        try:
            response = self.client.request(
                method,
                self.settings.mineru_api_base_url.rstrip("/") + path,
                headers={
                    "Authorization": "Bearer "
                    + self.settings.mineru_api_token.get_secret_value()
                },
                **kwargs,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # 未建立连接时可以重试；已发送 POST 后的读取超时按不确定提交处理。
            raise MinerUError(
                "NETWORK", "MinerU 网络暂时无法连接", retryable=True
            ) from None
        except httpx.HTTPError:
            # 申请链接是非幂等 POST，响应丢失时不确认成功与否，禁止盲目重复提交。
            raise MinerUError(
                "SUBMIT_UNCERTAIN" if method == "POST" else "NETWORK",
                "MinerU 提交结果不确定，请人工核对后再操作"
                if method == "POST"
                else "MinerU 查询网络暂时不可用",
                retryable=method != "POST",
            ) from None
        self._http_status(response)
        try:
            envelope = response.json()
        except ValueError:
            raise MinerUError("BAD_RESPONSE", "MinerU 返回非 JSON 响应") from None
        if not isinstance(envelope, dict) or "code" not in envelope:
            raise MinerUError("BAD_RESPONSE", "MinerU 响应结构无效")
        if envelope["code"] != 0:
            raise api_error(envelope["code"])
        if not isinstance(envelope.get("data"), dict):
            raise MinerUError("BAD_RESPONSE", "MinerU 响应缺少结构化 data")
        return envelope

    def _http_status(self, response: httpx.Response) -> None:
        """分类 HTTP 错误，429 尊重有界 Retry-After，其余失败不给出原始 URL。"""

        status = response.status_code
        if 200 <= status < 300:
            return
        delay = response.headers.get("Retry-After", "")
        delay = min(int(delay), 86400) if delay.isdigit() else None
        if status == 429 or status >= 500:
            raise MinerUError(
                f"HTTP_{status}",
                "MinerU 服务限流或暂时不可用",
                retryable=True,
                retry_after=delay,
            )
        raise MinerUError(f"HTTP_{status}", "MinerU 请求被拒绝，请检查鉴权、文件或权限")
