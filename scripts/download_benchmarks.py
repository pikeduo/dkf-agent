"""用户手动运行的公开基准下载器；不解析业务文档，不导入数据库，不授予云外传许可。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import benchmark_common as common
from scripts import prepare_ocb_preset as ocb
from scripts import prepare_omnidocbench_preset as omni

MIB = 1024**2
OWNER = "dkf-benchmark-download-v1"
REPOS = {"ohr": "opendatalab/OHR-Bench", "ocb": common.REPOS["ocb"],
         "omni": common.REPOS["omnidocbench"]}
FOLDERS = {"ohr": "ohr_bench_preset", "ocb": "ocb_preset", "omni": "omnidocbench_preset"}
SCOPES = {"ohr": {"required"}, "ocb": {"metadata", "reviewed", "hosted-tree", "native-library"},
          "omni": {"metadata", "candidate-images", "all-images"}}
ENDPOINTS = {"https://hf-mirror.com", "https://huggingface.co"}
ERRORS = {
    "NETWORK": "网络连接/读取失败，有限重试已耗尽；保留检查点后重跑同一命令",
    "HTTP": "HTTP 请求被拒绝；检查状态码、版本和访问权限，不继续下载依赖文件",
    "SCHEMA": "官方响应或本地元数据字段不可用；不伪造 JSON、版本或文件树",
    "REVISION": "固定 revision 缺失或不一致；不能继续拼接下载地址",
    "PATH": "路径、链接/Junction 或下载来源不安全，已拒绝",
    "EXISTS": "正式文件已存在但验证失败，禁止覆盖；请人工核对来源和完整性",
    "RESUME": "断点身份或 Range/ETag 不一致，保留未完成文件；需人工核对，禁止盲目追加",
    "FORMAT": "下载内容不完整或实际格式/官方摘要验证失败，未发布为正式输入",
    "MISSING": "缺少正式输入或下载检查点；先完成对应 metadata/required 下载",
    "DEPENDENCY": "缺少 httpx、PyArrow 或 Pillow；按 README 仅手动补齐缺少的依赖",
    "SPACE": "磁盘空间不足，保留已有文件；腾出空间后重跑同一命令",
    "REVIEW": "原件未明确审核、QA 依赖不完整或不是原生 Office；不替用户批准",
    "EXTERNAL": "该原件仅有外部来源，需用户按官方 manifest 手动取得；不自动转换或抓取任意 URL",
    "LIMIT": "参数、大小、候选数量或元数据预算超限；不自动扩大限制",
    "BUSY": "同一数据集已有下载进程或遗留锁；确认进程已停止后人工处理锁文件",
    "IO": "本地操作未完成，保留安全状态；不输出本机路径或底层异常",
    "CANCELLED": "用户中断下载；保留原件与本工具断点，显式 resume 后继续",
}


class DownloadError(ValueError):
    """只输出固定安全原因和 HTTP 数字，不附带正文、URL、凭据或底层异常。"""

    def __init__(self, code, status=None):
        """将未知错误收敛为 IO；状态码只允许有效整数。"""
        self.code = code if code in ERRORS else "IO"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        super().__init__(f"DOWNLOAD_{self.code}：{ERRORS[self.code]}"
                         + (f"（HTTP {self.status}）" if self.status else ""))


def endpoint_value(explicit=None):
    """显式参数优先于 HF_ENDPOINT，再用国内镜像；只接受两个 HTTPS 入口。"""
    value = (explicit if explicit is not None else os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")).rstrip("/")
    if value not in ENDPOINTS:
        raise DownloadError("PATH")
    return value


def revision_value(value):
    """正式下载仅接受已解析的 40 位 commit SHA，拒绝空版本或未冻结分支。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise DownloadError("REVISION")
    return value


def safe_relative(value):
    """复用现有跨平台相对路径检查，错误不泄露原始输入。"""
    try:
        return common.ohr.safe_name(value).as_posix()
    except common.ohr.PreparationError:
        raise DownloadError("PATH") from None


def safe_path(path, root):
    """固定保存边界并拒绝任一已有祖先的链接/Junction。"""
    try:
        return common.ohr.plain_path(path, beneath=root)
    except common.ohr.PreparationError:
        raise DownloadError("PATH") from None


def source_root(dataset, *, create=True):
    """不依赖工作目录；仅用户调用时按数据集递归创建目录，不创建空目标文件。"""
    root = safe_path(PROJECT_ROOT / "docs/test" / FOLDERS[dataset] / "source", PROJECT_ROOT)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        safe_path(root, PROJECT_ROOT)
        for relative in (["reference_files"] if dataset == "ocb" else ["images"] if dataset == "omni" else []):
            safe_path(root / relative, root).mkdir(parents=True, exist_ok=True)
    return root


def read_json(path, limit=96 * MIB):
    """有界读取原始 JSON，不输出正文；临时文件不算正式输入。"""
    try:
        data = common.read_bytes(path, limit)
        value = json.loads(data.decode("utf-8-sig"))
        common.ohr.encoded(value)
        return value
    except common.BenchmarkError as exc:
        raise DownloadError("MISSING" if exc.code == "MISSING" else "SCHEMA") from None
    except (ValueError, UnicodeError):
        raise DownloadError("SCHEMA") from None


def dataset_info(data, repo, expected=None):
    """验证官方 Dataset API 的 id/sha/cardData，返回原结构而非 SDK 对象字典。"""
    if (not isinstance(data, dict) or data.get("id") != repo or "cardData" not in data
            or data["cardData"] is not None and not isinstance(data["cardData"], dict)):
        raise DownloadError("SCHEMA")
    actual = revision_value(data.get("sha"))
    if expected is not None and actual != revision_value(expected):
        raise DownloadError("REVISION")
    return data


def write_private(path, value, root):
    """仅原子更新本下载器拥有的检查点；用户元数据、报告和许可文件绝不替换。"""
    path = safe_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and read_json(path).get("owner") != OWNER:
        raise DownloadError("EXISTS")
    payload = common.ohr.encoded({"owner": OWNER, **value}) + b"\n"
    if len(payload) > 96 * MIB:
        raise DownloadError("LIMIT")
    descriptor, temporary = tempfile.mkstemp(prefix=".download-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_bytes(path, data, root):
    """小元数据按原始字节独占发布；已有文件必须完全一致，不覆盖。"""
    path = safe_path(path, root)
    if path.exists():
        if common.read_bytes(path, 32 * MIB, root) != data:
            raise DownloadError("EXISTS")
        return
    try:
        common.ohr.publish_exclusive(path, data)
    except common.ohr.PreparationError:
        raise DownloadError("EXISTS") from None


@contextmanager
def dataset_lock(root):
    """独占每数据集下载目录；退出只删除本进程创建的锁，不处理其他进程的残留。"""
    path = safe_path(root / ".download.lock", root)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise DownloadError("BUSY") from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


class Network:
    """固定公开来源的有界 HTTP 客户端；不读取 HF Token，不记录 HTTP 原始日志。"""

    def __init__(self, endpoint, *, client=None, retries=3, timeout=60, sleep=time.sleep):
        """复用已声明的 httpx；Mock 可注入客户端，正式保持 TLS 校验、串行和有限重试。"""
        try:
            import httpx
        except ImportError:
            raise DownloadError("DEPENDENCY") from None
        self.httpx = httpx
        self.endpoint = endpoint_value(endpoint)
        self.retries = retries
        self.sleep = sleep
        self.last_http_status = None
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=False, trust_env=False, headers={"Accept-Encoding": "identity", "User-Agent": OWNER})
        # httpx INFO 默认会输出含签名的 URL；独立 CLI 不向业务日志添加这些请求。
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).disabled = True

    def close(self):
        """释放 HTTP 连接，不操作 HF 缓存、系统网络或用户服务。"""
        self.client.close()

    def check_url(self, url, *, binary=False):
        """只允许 HTTPS 官方入口及已知 HF 文件 CDN；签名地址仅驻留内存。"""
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = host in {urlsplit(self.endpoint).hostname, "api.github.com", "raw.githubusercontent.com"}
        if binary:
            allowed = allowed or host in {"cdn-lfs.huggingface.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co",
                "cdn-lfs-eu-1.hf.co", "cas-bridge.xethub.hf.co", "cdn-lfs.hf-mirror.com",
                "us.aws.cdn.hf.co", "eu.aws.cdn.hf.co", "us.gcp.cdn.hf.co"}
        if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise DownloadError("PATH")

    @contextmanager
    def stream(self, url, headers=None, *, binary=False):
        """手动检查每次重定向，不传鉴权，不把签名 URL 持久化或展示。"""
        current = url
        for _ in range(6):
            self.check_url(current, binary=binary)
            with self.client.stream("GET", current, headers=headers or {}) as response:
                self.last_http_status = response.status_code
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise DownloadError("HTTP", response.status_code)
                    current = urljoin(current, location)
                    continue
                yield response
                return
        raise DownloadError("PATH")

    def pause(self, attempt, response=None):
        """有限退避，支持 Retry-After 秒数/HTTP 日期且最多等 60 秒，不无限等待。"""
        raw = response.headers.get("retry-after", "") if response is not None else ""
        requested = 0
        if raw.isdecimal():
            requested = min(60, int(raw[:8]))
        elif raw:
            try:
                requested = max(0, parsedate_to_datetime(raw).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                requested = 0
        delay = min(60, max(2**attempt, requested))
        print(f"网络暂时失败：重试 {attempt + 1}/{self.retries}，等待 {delay} 秒。")
        self.sleep(delay)

    def json(self, url):
        """读取至多 32 MiB 的 API JSON，返回原字节/结构/分页 Link，不保存错误页。"""
        for attempt in range(self.retries + 1):
            response = None
            try:
                with self.stream(url) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        if attempt == self.retries:
                            raise DownloadError("HTTP", status)
                    elif status != 200:
                        raise DownloadError("HTTP", status)
                    else:
                        chunks, length = [], 0
                        for chunk in response.iter_bytes(64 * 1024):
                            length += len(chunk)
                            if length > 32 * MIB:
                                raise DownloadError("LIMIT")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        try:
                            data = json.loads(raw.decode("utf-8-sig"))
                            common.ohr.encoded(data)
                        except (ValueError, UnicodeError):
                            raise DownloadError("SCHEMA") from None
                        return raw, data, response.links.get("next", {}).get("url")
            except self.httpx.TransportError:
                if attempt == self.retries:
                    raise DownloadError("NETWORK") from None
            self.pause(attempt, response)
        raise DownloadError("NETWORK")


def tree_entries(network, repo, revision, *, pagination=None):
    """完整读取固定 SHA 的 HF Tree，保留原对象；另记录分页计数，不保存游标/签名 URL。"""
    revision_value(revision)
    base = f"/api/datasets/{repo}/tree/{revision}"
    url = network.endpoint + base + "?recursive=true&expand=false&limit=1000"
    seen_urls, seen_paths, result, pages = set(), set(), [], []
    while url:
        parsed = urlsplit(url)
        if parsed.path != base or len(seen_urls) >= 100 or url in seen_urls:
            raise DownloadError("SCHEMA")
        seen_urls.add(url)
        _, page, next_url = network.json(url)
        if not isinstance(page, list) or not page or len(page) > 1000:
            raise DownloadError("SCHEMA")
        pages.append({"item_count": len(page), "had_next": bool(next_url)})
        for item in page:
            if not isinstance(item, dict) or item.get("type") not in {"file", "directory"}:
                raise DownloadError("SCHEMA")
            name = safe_relative(item.get("path"))
            if name.casefold() in seen_paths or not re.fullmatch(r"[0-9a-f]{40}", str(item.get("oid", ""))):
                raise DownloadError("SCHEMA")
            if item["type"] == "file":
                if type(item.get("size")) is not int or not 0 <= item["size"] <= 4 * 1024 * MIB:
                    raise DownloadError("SCHEMA")
                lfs = item.get("lfs")
                if lfs is not None and (not isinstance(lfs, dict)
                        or not re.fullmatch(r"[0-9a-f]{64}", str(lfs.get("oid", "")))
                        or lfs.get("size") != item["size"]):
                    raise DownloadError("SCHEMA")
            seen_paths.add(name.casefold())
            result.append(item)
        if len(result) > 50000 or len(common.ohr.encoded(result)) > 32 * MIB:
            raise DownloadError("LIMIT")
        if next_url:
            next_url = urljoin(url, next_url)
            parts = urlsplit(next_url)
            if parts.scheme != "https" or parts.netloc not in {urlsplit(network.endpoint).netloc, "huggingface.co"}:
                raise DownloadError("PATH")
            # 镜像返回官方 Link 时，只替换同一 API 的来源，不回落官方 API。
            url = urlunsplit(("https", urlsplit(network.endpoint).netloc, parts.path, parts.query, ""))
        else:
            url = None
    if pagination is not None:
        pagination.update(revision=revision, page_count=len(pages), pages=pages, complete=True)
    return result


@dataclass(frozen=True)
class FileSpec:
    """固定官方身份与完整性约束；URL 只能是无凭据的稳定公开地址。"""

    local: str
    remote: str
    repo: str
    revision: str
    size: int
    sha256: str | None
    git_oid: str | None
    kind: str

    def url(self, endpoint):
        """从白名单仓库和固定 SHA 构建 resolve URL；GitHub QA 使用固定 commit。"""
        revision_value(self.revision)
        path = quote(safe_relative(self.remote), safe="/")
        if self.repo == "github:opendatalab/OHR-Bench":
            return f"https://raw.githubusercontent.com/opendatalab/OHR-Bench/{self.revision}/{path}"
        if self.repo not in REPOS.values():
            raise DownloadError("PATH")
        return f"{endpoint_value(endpoint)}/datasets/{self.repo}/resolve/{self.revision}/{path}"


def hf_spec(entry, dataset, revision, local=None):
    """官方文件大小和 LFS/Git Blob 摘要保持真实，不将 LFS 指针 OID 当内容 Hash。"""
    path = safe_relative(entry["path"])
    target = safe_relative(local or path)
    kind = Path(target).suffix.lower().lstrip(".")
    lfs = entry.get("lfs") or {}
    return FileSpec(target, path, REPOS[dataset], revision_value(revision), entry["size"],
                    lfs.get("oid"), None if lfs else entry["oid"], kind)


def validate_file(path, spec):
    """流式校验字节数和官方摘要，再检查实际容器；不调用 Parser/OCR 或生成样本。"""
    size = path.stat().st_size
    if size != spec.size or size <= 0:
        raise DownloadError("FORMAT")
    sha, blob = hashlib.sha256(), hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\x00")
    with path.open("rb") as stream:
        prefix = stream.read(512)
        if prefix.lstrip().lower().startswith((b"<html", b"<!doctype", b"version https://git-lfs")):
            raise DownloadError("FORMAT")
        stream.seek(0)
        while chunk := stream.read(MIB):
            sha.update(chunk)
            blob.update(chunk)
    if spec.sha256 and sha.hexdigest() != spec.sha256 or spec.git_oid and blob.hexdigest() != spec.git_oid:
        raise DownloadError("FORMAT")
    details = {}
    try:
        if spec.kind == "json":
            value = read_json(path, 32 * MIB if spec.local == "qas_v2.json" else 96 * MIB)
            if not isinstance(value, list) or not value:
                raise DownloadError("FORMAT")
            if spec.local == "qas_v2.json" and not common.ohr.qa_rows(value)[0]:
                raise DownloadError("SCHEMA")
        elif spec.kind == "parquet":
            with path.open("rb") as stream:
                start = stream.read(4)
                stream.seek(-8, os.SEEK_END)
                footer = stream.read(8)
            if start != b"PAR1" or footer[4:] != b"PAR1" or not 0 < int.from_bytes(footer[:4], "little") < size - 12:
                raise DownloadError("FORMAT")
            # 同离线准备器的真实 Parquet 解码与预算，不只看四字节魔数。
            common.rows_file(path)
        elif spec.kind in {"zip", "docx", "xlsx", "pptx"}:
            with ZipFile(path) as archive:
                pdfs, _ = common.ohr.archive_index(archive)
                if spec.kind == "zip" and not pdfs:
                    raise DownloadError("FORMAT")
                if spec.kind != "zip":
                    from app.core.file_formats import validate_file_content
                    with path.open("rb") as stream:
                        validate_file_content(stream, spec.kind)
        elif spec.kind in {"png", "jpg", "jpeg"}:
            try:
                from PIL import Image
            except ImportError:
                raise DownloadError("DEPENDENCY") from None
            with Image.open(path) as image:
                width, height = image.size
                if (not 0 < width * height <= 40_000_000
                        or image.format != ("PNG" if spec.kind == "png" else "JPEG")):
                    raise DownloadError("FORMAT")
                details.update(width=width, height=height)
                image.verify()
        else:
            raise DownloadError("FORMAT")
    except DownloadError:
        raise
    except common.BenchmarkError as exc:
        raise DownloadError("DEPENDENCY" if exc.code == "DEPENDENCY" else "FORMAT") from None
    except ImportError:
        raise DownloadError("DEPENDENCY") from None
    except (BadZipFile, ValueError, RuntimeError, common.ohr.PreparationError, OSError):
        raise DownloadError("FORMAT") from None
    return {"size_bytes": size, "sha256": sha.hexdigest(), "official_digest_checked": bool(spec.sha256 or spec.git_oid), **details}


class Downloader:
    """串行安全下载；仅完整且验证通过的文件才发布，续传不覆盖已有原件。"""

    def __init__(self, root, network, validator=validate_file, *, cancel_event=None):
        """检查点与数据同文件系统；可注入校验 Stub；可选取消信号只用于用户的全量素材下载。"""
        self.root, self.network, self.validator = root, network, validator
        self.cancel_event = cancel_event

    def check_cancelled(self):
        """在请求/写入边界安全停止，不删除原件或断点，不改变既有串行模式默认行为。"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DownloadError("CANCELLED")

    def state_path(self, spec):
        """检查点名称只取业务相对路径摘要，不包含 Token 或签名链接。"""
        return safe_path(self.root / ".download" / (common.ohr.digest(spec.local) + ".json"), self.root)

    def verify(self, spec):
        """验证已有正式文件，失败不覆盖；存在自己的回执时同时核对固定来源。"""
        path = safe_path(self.root / safe_relative(spec.local), self.root)
        if not path.is_file():
            raise DownloadError("MISSING")
        try:
            result = self.validator(path, spec)
            state_path = self.state_path(spec)
            if state_path.exists():
                state = read_json(state_path)
                if state.get("owner") != OWNER or state.get("spec") != asdict(spec):
                    raise DownloadError("EXISTS")
                if state.get("result") is not None and state["result"] != result:
                    raise DownloadError("EXISTS")
            return result
        except DownloadError as exc:
            if exc.code == "DEPENDENCY":
                raise
            raise DownloadError("EXISTS") from None

    def download(self, spec):
        """有限 HTTP 重试，验证 206/Range/ETag；中断保留本工具的 .part 及固定身份。"""
        target = safe_path(self.root / safe_relative(spec.local), self.root)
        self.check_cancelled()
        if target.exists():
            result = self.verify(spec)
            # 对用户已有且通过官方摘要的原件只补本工具回执，不改原字节。
            write_private(self.state_path(spec), {"spec": asdict(spec), "result": result}, self.root)
            print(f"验证通过，跳过：{spec.local}；字节={result['size_bytes']}。")
            return result
        part = safe_path(target.with_name(target.name + ".part"), self.root)
        state_path = self.state_path(spec)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(target.parent, self.root)
        state = read_json(state_path) if state_path.exists() else None
        if state is not None and (state.get("owner") != OWNER or state.get("spec") != asdict(spec)):
            raise DownloadError("RESUME")
        if part.exists() and (state is None or not part.is_file()):
            raise DownloadError("RESUME")
        if not state:
            state = {"spec": asdict(spec), "etag": None, "result": None}
            write_private(state_path, state, self.root)
        for attempt in range(self.network.retries + 1):
            self.check_cancelled()
            offset = part.stat().st_size if part.exists() else 0
            if offset > spec.size:
                raise DownloadError("RESUME")
            if offset == spec.size:
                break
            if shutil.disk_usage(target.parent).free < spec.size - offset + 64 * MIB:
                raise DownloadError("SPACE")
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            if offset and state.get("etag"):
                headers["If-Range"] = state["etag"]
            response = None
            try:
                with self.network.stream(spec.url(self.network.endpoint), headers, binary=True) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        if attempt == self.network.retries:
                            raise DownloadError("HTTP", status)
                    elif status not in {200, 206}:
                        raise DownloadError("HTTP", status)
                    else:
                        length = response.headers.get("content-length")
                        if length is not None and (not length.isdecimal() or int(length) != spec.size - offset):
                            raise DownloadError("RESUME")
                        if offset or status == 206:
                            expected = f"bytes {offset}-{spec.size - 1}/{spec.size}"
                            if status != 206 or response.headers.get("content-range") != expected:
                                raise DownloadError("RESUME")
                        if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                            raise DownloadError("RESUME")
                        if "text/html" in response.headers.get("content-type", "").lower():
                            raise DownloadError("FORMAT")
                        etag = response.headers.get("etag")
                        if offset and state.get("etag") and etag != state["etag"]:
                            raise DownloadError("RESUME")
                        if offset and not (spec.sha256 or spec.git_oid or state.get("etag")):
                            raise DownloadError("RESUME")
                        state["etag"] = etag if etag and not etag.startswith("W/") else None
                        write_private(state_path, state, self.root)
                        started, last, downloaded = time.monotonic(), 0.0, offset
                        # 本工具的零字节断点也可恢复；不截断任何已有数据。
                        with part.open("r+b" if part.exists() else "xb") as stream:
                            stream.seek(offset)
                            try:
                                for chunk in response.iter_bytes(MIB):
                                    self.check_cancelled()
                                    if downloaded + len(chunk) > spec.size:
                                        raise DownloadError("FORMAT")
                                    stream.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.monotonic()
                                    if now - last >= 1:
                                        speed = (downloaded - offset) / max(now - started, 0.001) / MIB
                                        print(f"下载 {spec.local}：{downloaded}/{spec.size} 字节；{speed:.2f} MiB/s。", flush=True)
                                        last = now
                            finally:
                                stream.flush()
                                os.fsync(stream.fileno())
                        if downloaded == spec.size:
                            break
                        if attempt == self.network.retries:
                            raise DownloadError("NETWORK")
            except self.network.httpx.TransportError:
                if attempt == self.network.retries:
                    raise DownloadError("NETWORK") from None
            self.network.pause(attempt, response)
        self.check_cancelled()
        result = self.validator(part, spec)
        self.check_cancelled()
        # 检查点先写结果，发布竞争失败时仍保留完整 .part；下次需核对已有目标。
        write_private(state_path, {"spec": asdict(spec), "result": result, "etag": state.get("etag")}, self.root)
        safe_path(target, self.root)
        safe_path(part, self.root)
        try:
            os.link(part, target, follow_symlinks=False)
        except FileExistsError:
            raise DownloadError("EXISTS") from None
        part.unlink()
        print(f"下载完成：{spec.local}；字节={result['size_bytes']}；SHA256={result['sha256']}。")
        return result


def fixed_metadata(root, dataset, network, explicit=None, *, offline=False, dry_run=False,
                   hosted_file="hosted_files.json", tree_only=False):
    """复用固定信息与完整树；默认不替换用户输入，树修复不重新获取 dataset_info。"""
    path = safe_path(root / "dataset_info.json", root)
    if path.exists():
        data = dataset_info(read_json(path), REPOS[dataset], explicit)
    else:
        if offline or tree_only:
            raise DownloadError("MISSING")
        base = f"{network.endpoint}/api/datasets/{REPOS[dataset]}"
        if explicit:
            revision_value(explicit)
            base += "/revision/" + explicit
        _, info, _ = network.json(base)
        info = dataset_info(info, REPOS[dataset], explicit)
        revision = info["sha"]
        raw, data, _ = network.json(f"{network.endpoint}/api/datasets/{REPOS[dataset]}/revision/{revision}")
        dataset_info(data, REPOS[dataset], revision)
        if not dry_run:
            publish_bytes(path, raw, root)
    revision = data["sha"]
    print(f"固定版本：{REPOS[dataset]} @ {revision}。")
    if hosted_file not in {"hosted_files.json", "hosted_files.structured.json"}:
        raise DownloadError("PATH")
    # 新副本使用独立缓存，旧下载/列表和检查点都不被覆盖；首次完整获取官方树。
    structured_cache = safe_path(root / ".download/ocb-structured-tree.json", root)
    use_structured = dataset == "ocb" and (hosted_file == "hosted_files.structured.json" or structured_cache.exists())
    tree_name = "ocb-structured-tree.json" if use_structured else "tree.json"
    tree_path = safe_path(root / ".download" / tree_name, root)
    if tree_path.exists():
        cached = read_json(tree_path)
        if not isinstance(cached, dict) or cached.get("owner") != OWNER or cached.get("revision") != revision or not isinstance(cached.get("entries"), list):
            raise DownloadError("REVISION")
        entries = cached["entries"]
        if cached.get("tree_sha256") != common.ohr.digest(entries):
            raise DownloadError("SCHEMA")
        if use_structured:
            validated_ocb_tree_snapshot(cached, revision)
    else:
        if offline:
            raise DownloadError("MISSING")
        pagination = {}
        entries = tree_entries(network, REPOS[dataset], revision, pagination=pagination)
        if use_structured:
            validated_ocb_tree_snapshot({"owner": OWNER, "revision": revision, "entries": entries,
                "tree_sha256": common.ohr.digest(entries), "pagination": pagination}, revision)
        if not dry_run:
            write_private(tree_path, {"revision": revision, "entries": entries,
                "tree_sha256": common.ohr.digest(entries), "pagination": pagination}, root)
    if dataset == "ocb":
        hosted = [entry for entry in entries if entry["path"].startswith("reference_files/")]
        ocb.hosted_index(hosted)
        path = safe_path(root / hosted_file, root)
        if path.exists():
            try:
                existing = read_json(path)
                ocb.hosted_index(existing)
            except common.BenchmarkError as exc:
                raise common.BenchmarkError(exc.code, input_id="ocb:" + hosted_file, stage=exc.stage or "hosted_tree") from None
            if existing != hosted:
                raise DownloadError("EXISTS")
        elif offline and tree_only:
            raise DownloadError("MISSING")
        elif not offline and not dry_run:
            publish_bytes(path, common.ohr.encoded(hosted) + b"\n", root)
    return data, entries


def validated_ocb_tree_snapshot(cached, revision):
    """校验同 SHA 的完整对象树及分页链；不补造字段，不接受缺页/旧字符串缓存。"""
    if not isinstance(cached, dict) or cached.get("owner") != OWNER or cached.get("revision") != revision_value(revision):
        raise DownloadError("REVISION")
    entries, pagination = cached.get("entries"), cached.get("pagination")
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 10000
            or cached.get("tree_sha256") != common.ohr.digest(entries)
            or not isinstance(pagination, dict) or pagination.get("revision") != revision
            or pagination.get("complete") is not True):
        raise DownloadError("SCHEMA")
    pages, count = pagination.get("pages"), pagination.get("page_count")
    if not isinstance(pages, list) or type(count) is not int or not 1 <= count <= 100 or len(pages) != count:
        raise DownloadError("SCHEMA")
    total = 0
    for index, page in enumerate(pages):
        if (not isinstance(page, dict) or type(page.get("item_count")) is not int
                or not 1 <= page["item_count"] <= 1000 or page.get("had_next") is not (index < count - 1)):
            raise DownloadError("SCHEMA")
        total += page["item_count"]
    if total != len(entries):
        raise DownloadError("SCHEMA")
    seen = set()
    for item in entries:
        if (not isinstance(item, dict) or item.get("type") not in {"file", "directory"}
                or not isinstance(item.get("oid"), str) or re.fullmatch(r"[0-9a-f]{40}", item["oid"]) is None):
            raise DownloadError("SCHEMA")
        name = safe_relative(item.get("path"))
        if name.casefold() in seen:
            raise DownloadError("SCHEMA")
        seen.add(name.casefold())
    # hosted_index 验证每个文件的 size/OID/LFS，保留供应商原字段；空托管集合不能冒充修复成功。
    if not ocb.hosted_index(entries):
        raise DownloadError("SCHEMA")
    return entries


def replace_hosted_tree(root, revision):
    """显式修复旧字符串表：完整校验副本与分页后备份原字节，再原子替换唯一目标。

    同内容重复执行不改文件；其他异常正式对象表拒绝覆盖。备份/替换失败保留旧表及已有副本。
    用户运行期间不得手工并发编辑 source；替换前会再次检查相关输入是否变化。
    """
    metadata_path = safe_path(root / "dataset_info.json", root)
    metadata = common.read_bytes(metadata_path, 32 * MIB, root)
    dataset_info(read_json(metadata_path), REPOS["ocb"], revision)
    cache_path = safe_path(root / ".download/ocb-structured-tree.json", root)
    cache_bytes = common.read_bytes(cache_path, 32 * MIB, root)
    entries = validated_ocb_tree_snapshot(read_json(cache_path), revision)
    expected = [item for item in entries if item["path"].startswith("reference_files/")]
    sidecar = safe_path(root / "hosted_files.structured.json", root)
    payload = common.read_bytes(sidecar, 32 * MIB, root)
    candidate = read_json(sidecar)
    ocb.hosted_index(candidate)
    if candidate != expected:
        raise DownloadError("EXISTS")
    target = safe_path(root / "hosted_files.json", root)
    old_bytes = common.read_bytes(target, 32 * MIB, root)
    old = read_json(target)
    if old == candidate:
        print("hosted_files.json 已是同版本完整对象树，跳过替换；无新增备份。")
        return
    if not isinstance(old, list) or not old or not all(isinstance(item, str) for item in old):
        raise DownloadError("EXISTS")
    for name in old:
        safe_relative(name)
    backup = safe_path(root / ".download/backups" / ("hosted_files-" + hashlib.sha256(old_bytes).hexdigest() + ".json"), root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    publish_bytes(backup, old_bytes, root)
    if common.read_bytes(backup, 32 * MIB, root) != old_bytes:
        raise DownloadError("EXISTS")
    descriptor, temporary = tempfile.mkstemp(prefix=".hosted-tree-replacement-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # 完整临时文件再校验一次；目标从不先删除，只允许用户显式要求的旧字符串表替换。
        if read_json(safe_path(temporary, root)) != expected:
            raise DownloadError("SCHEMA")
        for path, frozen in ((target, old_bytes), (sidecar, payload), (metadata_path, metadata), (cache_path, cache_bytes)):
            if common.read_bytes(safe_path(path, root), 32 * MIB, root) != frozen:
                raise DownloadError("EXISTS")
        os.replace(temporary, safe_path(target, root))
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"hosted_files.json 已校验并原子替换；旧原字节保留于 .download/backups/{backup.name}；文件对象数={len(ocb.hosted_index(candidate))}。")


def validate_tree_replacement_args(args):
    """替换许可只适用于 OCB hosted-tree 的普通运行；只读/预览模式绝不写备份或正式表。"""
    if getattr(args, "replace_hosted_tree", False) and (args.dataset != "ocb" or args.scope != "hosted-tree"
            or args.dry_run or args.verify_only or args.list_missing
            or args.hosted_file not in {None, "hosted_files.structured.json"}):
        raise DownloadError("LIMIT")


def github_qa(root, network, *, offline=False, dry_run=False):
    """通过官方 GitHub Contents API 冻结 QA commit/Git Blob；原始 JSON 字节由 raw 下载。"""
    path = root / ".download/github_qa.json"
    if path.exists():
        state = read_json(path)
        if state.get("owner") != OWNER:
            raise DownloadError("SCHEMA")
        spec = FileSpec(**state["spec"])
        if (spec.local != "qas_v2.json" or spec.remote != "data/qas_v2.json"
                or spec.repo != "github:opendatalab/OHR-Bench" or spec.kind != "json"
                or type(spec.size) is not int or not 0 < spec.size <= 32 * MIB
                or not re.fullmatch(r"[0-9a-f]{40}", str(spec.git_oid))):
            raise DownloadError("SCHEMA")
        revision_value(spec.revision)
        return spec
    if offline:
        raise DownloadError("MISSING")
    _, commit, _ = network.json("https://api.github.com/repos/opendatalab/OHR-Bench/commits/main")
    revision = revision_value(commit.get("sha") if isinstance(commit, dict) else None)
    _, item, _ = network.json("https://api.github.com/repos/opendatalab/OHR-Bench/contents/data/qas_v2.json?ref=" + revision)
    if (not isinstance(item, dict) or item.get("type") != "file" or item.get("path") != "data/qas_v2.json"
            or type(item.get("size")) is not int or not 0 < item["size"] <= 32 * MIB
            or not re.fullmatch(r"[0-9a-f]{40}", str(item.get("sha", "")))):
        raise DownloadError("SCHEMA")
    spec = FileSpec("qas_v2.json", "data/qas_v2.json", "github:opendatalab/OHR-Bench", revision,
                    item["size"], None, item["sha"], "json")
    if not dry_run:
        write_private(path, {"spec": asdict(spec)}, root)
    return spec


def required_specs(dataset, revision, entries, root, network, *, offline=False, dry_run=False):
    """固定官方文件到现有本地契约的映射，不改变 Parquet/QA/ZIP 原始字节。"""
    indexed = {entry["path"]: entry for entry in entries if entry["type"] == "file"}
    mappings = {"ohr": [("pdfs.zip", "pdfs.zip")],
        "ocb": [("data/ocb_qna_data.parquet", "ocb_qna_data.parquet"),
                ("data/ocb_source_urls.parquet", "ocb_file_sources.parquet")],
        "omni": [("OmniDocBench.json", "OmniDocBench.json")]}
    result = []
    for remote, local in mappings[dataset]:
        if remote not in indexed:
            raise DownloadError("MISSING")
        result.append(hf_spec(indexed[remote], dataset, revision, local))
    if dataset == "ohr":
        # QA 优先，GitHub 阻断时不先下载 1.5 GB ZIP；仍是两个独立官方版本。
        result.insert(0, github_qa(root, network, offline=offline, dry_run=dry_run))
    return result


def ocb_preview(root):
    """实际读取官方 QA，仅展示分类、依赖与兼容诊断，不展示问题/答案正文。"""
    rows = common.rows_file(root / "ocb_qna_data.parquet")
    tracks, formats, groups, rejected = Counter(), Counter(), [], []
    source_hash = hashlib.sha256(common.read_bytes(root / "ocb_qna_data.parquet")).hexdigest()
    for index, row in enumerate(rows):
        track = row.get("track")
        if not isinstance(track, str) or len(track) > 128 or re.search(r"[\x00-\x1f]", track):
            raise DownloadError("SCHEMA")
        tracks[track] += 1
        names = row.get("reference_files")
        names = [names] if isinstance(names, str) else names
        if isinstance(names, list):
            for name in names:
                safe_relative(name)
                formats[Path(name).suffix.lower()] += 1
        try:
            qa = ocb.qa_record(row, index, "hf", source_hash)
            groups.append({"source_row_index": index, "reference_files": qa["reference_files"],
                           "track": qa["track"], "feature": qa["feature"]})
        except common.BenchmarkError as exc:
            # 三种明确子类已支持；未知相似名字只记拒绝，不通过宽泛 startswith 猜分类。
            official_like = "file fidelity" in track.casefold() or re.sub(r"[ _]", "", track.casefold()) == "filefidelity"
            if official_like:
                rejected.append({"source_row_index": index, "reason": "qa_record_incompatible_" + exc.code,
                    "suggestion": "人工核对官方 Track/字段；单独确认后修改 qa_record，不改原始数据"})
    return {"tracks": dict(sorted(tracks.items())), "file_formats": dict(sorted(formats.items())),
            "dependency_groups": groups, "compatibility_gaps": rejected,
            "not_approved": True, "not_quality_verified": True}


def ocb_candidate_sources(preview, entries, revision):
    """为真实 QA 依赖提供可直接复制的固定官方地址，但不填写任何人工批准字段。"""
    hosted = ocb.hosted_index(entries)
    names = sorted({name for group in preview["dependency_groups"] for name in group["reference_files"]})
    result = []
    for name in names:
        entry = hosted.get(name)
        item = {"reference_file": name, "review_status": "not_reviewed",
                "native_office_candidate": Path(name).suffix.lower().lstrip(".") in ocb.TYPES}
        if entry:
            item.update(path=entry["path"], size_bytes=entry["size"],
                official_source_url=f"https://huggingface.co/datasets/{REPOS['ocb']}/resolve/{revision}/"
                                    + quote(entry["path"], safe="/"),
                official_lfs=entry.get("lfs"), official_git_blob_oid=entry["oid"])
        else:
            item.update(reason="external_source_requires_manual_manifest_review", official_source_url=None)
        result.append(item)
    return result


def reviewed_specs(root, revision, entries, preview):
    """只选人工审核的原生托管 Office；完整 QA 依赖必须全部审核，不自动批准或转换。"""
    rows = read_json(root / "source_review.json")
    if not isinstance(rows, list) or not rows:
        raise DownloadError("REVIEW")
    reviews = {}
    hosted = ocb.hosted_index(entries)
    for review in rows:
        if not isinstance(review, dict):
            raise DownloadError("REVIEW")
        name = safe_relative(review.get("reference_file"))
        if (name in reviews or Path(name).suffix.lower().lstrip(".") not in ocb.TYPES
                or review.get("original_native") is not True or review.get("local_use_confirmed") is not True
                or review.get("conversion_required") is not False or not isinstance(review.get("license"), str)
                or not review["license"].strip() or not isinstance(review.get("source_url"), str)):
            raise DownloadError("REVIEW")
        if name not in hosted:
            # 不允许人工记录注入任意 URL；外部 manifest 及其许可仍由既有离线准备器检查。
            raise DownloadError("EXTERNAL")
        expected_path = f"/datasets/{REPOS['ocb']}/resolve/{revision}/" + hosted[name]["path"]
        parsed = urlsplit(review.get("source_url", ""))
        if (parsed.scheme != "https" or parsed.netloc != "huggingface.co"
                or parsed.query or parsed.fragment or unquote(parsed.path) != expected_path):
            raise DownloadError("REVIEW")
        if hosted[name]["size"] > 20 * MIB:
            raise DownloadError("LIMIT")
        reviews[name] = review
    groups = [group for group in preview["dependency_groups"] if set(group["reference_files"]) & set(reviews)]
    if (not groups or any(not set(group["reference_files"]) <= set(reviews) for group in groups)
            or set(reviews) - {name for group in groups for name in group["reference_files"]}):
        raise DownloadError("REVIEW")
    if len(reviews) > 64 or sum(hosted[name]["size"] for name in reviews) > 256 * MIB:
        raise DownloadError("LIMIT")
    return [hf_spec(hosted[name], "ocb", revision) for name in sorted(reviews)]


def omni_candidates(root, entries, seed):
    """复用 page_record 的真实字段/覆盖类别，按完整 image_path 精确匹配，不猜 basename。"""
    rows = read_json(root / "OmniDocBench.json")
    if not isinstance(rows, list) or not 0 < len(rows) <= 10000:
        raise DownloadError("SCHEMA")
    files = {entry["path"]: entry for entry in entries if entry["type"] == "file"}
    identities = Counter(str(row.get("page_info", {}).get("image_path", "")).casefold()
                         for row in rows if isinstance(row, dict) and isinstance(row.get("page_info"), dict))
    candidates, rejected = [], []
    for row in rows:
        try:
            name, labels = omni.page_record(row)
            relative = name if name.startswith("images/") else "images/" + name
            if identities[name.casefold()] != 1:
                raise DownloadError("SCHEMA")
            if relative not in files:
                rejected.append({"identity": name, "reason": "missing_official_image"})
                continue
            entry = files[relative]
            if Path(relative).suffix.lower() not in {".png", ".jpg", ".jpeg"} or not 0 < entry["size"] <= 20 * MIB:
                rejected.append({"identity": name, "reason": "image_size_or_format_limit"})
                continue
            candidates.append({"identity": name, "labels": sorted(labels), "entry": entry,
                               "width": row["page_info"]["width"], "height": row["page_info"]["height"]})
        except (DownloadError, common.BenchmarkError, common.ohr.PreparationError, KeyError, TypeError):
            rejected.append({"reason": "invalid_or_ambiguous_annotation"})
    candidates.sort(key=omni.identity_key)
    selected, covered = [], set()
    # 对应准备器的检查优先级；字节大小仅用于预算，最终离线选择还会按真实大小打破平局。
    while candidates:
        def rank(item):
            """优先补当前缺少的 GT 类别，固定种子按完整原始身份打破平局。"""
            return -len(set(item["labels"]) - covered), common.ranked(seed, item["identity"])

        candidate = min(candidates, key=rank)
        candidates.remove(candidate)
        selected.append(candidate)
        covered.update(candidate["labels"])
    return selected, rejected


def path_key(item):
    """按官方完整相对路径稳定排序，不拆解名字猜测原文档身份。"""
    return item["path"]


def process_specs(specs, downloader, *, verify_only=False, list_missing=False, dry_run=False):
    """统一执行固定清单，任何下载失败立即停止；缺失检查只列状态不发请求。"""
    results, missing = {}, []
    print(f"清单文件数={len(specs)}；官方总字节={sum(spec.size for spec in specs)}（已有原件不会重复下载）。")
    for spec in specs:
        target = safe_path(downloader.root / spec.local, downloader.root)
        if list_missing or dry_run:
            status = "存在（尚未校验）" if target.is_file() else "未完成（可检查本工具断点）" if target.with_name(target.name + ".part").exists() else "缺失"
            print(f"{spec.local}：{status}；官方字节={spec.size}。")
            if not target.is_file():
                missing.append(spec.local)
            continue
        results[spec.local] = downloader.verify(spec) if verify_only else downloader.download(spec)
    if verify_only:
        print(f"完整性验证通过：{len(results)} 份；未联网、未下载。")
    return results, missing


def build_parser():
    """提供与 README 一致的入口；离线校验/缺失查看与在线 dry-run 明确区分。"""
    parser = argparse.ArgumentParser(description="公开基准串行下载；用户手动执行，不解析/导入/授予云权限")
    parser.add_argument("--dataset", choices=sorted(REPOS), required=True)
    parser.add_argument("--scope", choices=sorted(set.union(*SCOPES.values())), required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="native-library 离线生成报告；其他范围仅取元数据列计划；都不下载原件")
    modes.add_argument("--verify-only", action="store_true", help="离线验证该 scope 全部正式输入；native-library 更新自有校验报告，无网络")
    modes.add_argument("--list-missing", action="store_true", help="离线查看该 scope 缺失项，无网络")
    modes.add_argument("--execute", action="store_true", help="仅 OCB native-library：显式联网下载已逐文件获许可的原件")
    parser.add_argument("--revision", help="可选 40 位 HF SHA；缺省首次解析 main，以后复用本地冻结版本")
    parser.add_argument("--endpoint", help="优先于 HF_ENDPOINT；默认 https://hf-mirror.com")
    parser.add_argument("--hosted-file", choices=["hosted_files.json", "hosted_files.structured.json"],
        help="仅 OCB；hosted-tree 默认生成 structured 副本，其他模式默认 canonical 文件")
    parser.add_argument("--replace-hosted-tree", action="store_true",
        help="仅 OCB hosted-tree 普通运行；校验完整副本后备份旧字符串表并原子替换 hosted_files.json")
    parser.add_argument("--count", type=int, choices=range(4, 13), default=5, help="Omni 候选下载数 4～12，默认 5；不等于最终选样数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--retries", type=int, choices=range(0, 6), default=3, help="暂时错误额外重试次数 0～5")
    parser.add_argument("--timeout", type=int, choices=range(10, 301), default=60, help="读取超时秒数 10～300；连接超时 10 秒")
    parser.add_argument("--native-formats", nargs="+", choices=["docx", "xlsx", "pptx", "csv", "xlsm"],
        help="native-library 默认 docx xlsx pptx；csv/xlsm 仅供素材库，不扩展业务上传契约")
    parser.add_argument("--native-results-dir", type=Path, help="native-library 报告目录，必须位于 OCB results 下；不覆盖旧抽样报告")
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=3, help="native-library 并发数，默认/上限 3")
    parser.add_argument("--max-file-mb", type=int, default=200, help="native-library 单原件上限 MiB，默认 200，最大 512")
    parser.add_argument("--max-total-mb", type=int, default=8192, help="native-library 本轮计划容量预算 MiB，默认 8192")
    parser.add_argument("--allow-external", action="store_true", help="native-library execute 额外允许已核对官方 URL 且获许可的第三方原件")
    parser.add_argument("--resume", action="store_true", help="native-library execute 显式恢复本工具同身份断点/报告；不强制覆盖")
    return parser


def run(args, root, network):
    """取得固定来源，串行下载或离线核验，输出 QA/候选报告但不写抽样/许可结果。"""
    validate_tree_replacement_args(args)
    offline = args.verify_only or args.list_missing
    hosted_file = getattr(args, "hosted_file", None) or ("hosted_files.structured.json" if args.scope == "hosted-tree" else "hosted_files.json")
    info, entries = fixed_metadata(root, args.dataset, network, args.revision, offline=offline,
        dry_run=args.dry_run, hosted_file=hosted_file, tree_only=args.scope == "hosted-tree")
    if args.scope == "hosted-tree":
        mode = "拟生成" if args.dry_run else "离线校验通过" if args.verify_only else "已就绪"
        if getattr(args, "replace_hosted_tree", False):
            replace_hosted_tree(root, info["sha"])
        print(f"官方结构化树{mode}：{hosted_file}；固定 revision 不变；dataset_info/审核/Parquet/抽样结果未修改。")
        return 0
    specs = required_specs(args.dataset, info["sha"], entries, root, network, offline=offline, dry_run=args.dry_run)
    downloader = Downloader(root, network)
    _, missing = process_specs(specs, downloader, verify_only=args.verify_only, list_missing=args.list_missing, dry_run=args.dry_run)
    if args.dataset == "ocb":
        review_path = root / "source_review.json"
        if args.list_missing:
            for name in (hosted_file, "source_review.json"):
                exists = safe_path(root / name, root).is_file()
                print(f"{name}：{'存在（尚未校验）' if exists else '缺失'}。")
                if not exists:
                    missing.append(name)
        if not offline and not args.dry_run and not review_path.exists():
            publish_bytes(review_path, b"[]\n", root)
            print("source_review.json 已初始化为空数组；尚未审核任何文件，不代表许可批准。")
        if not (root / "ocb_qna_data.parquet").is_file():
            if args.dry_run or args.list_missing:
                print("需先完成 metadata 才能读取真实 QA 和完整依赖。")
                return 2 if args.list_missing else 0
            raise DownloadError("MISSING")
        preview = ocb_preview(root)
        preview["candidate_sources"] = ocb_candidate_sources(preview, entries, info["sha"])
        print("OCB 实际 Track 分布：" + json.dumps(preview["tracks"], ensure_ascii=False))
        print("OCB 实际格式分布：" + json.dumps(preview["file_formats"], ensure_ascii=False))
        print(f"可适配依赖组={len(preview['dependency_groups'])}；兼容缺口={len(preview['compatibility_gaps'])}。")
        if not offline and not args.dry_run:
            write_private(root / ".download/ocb_candidates.json", preview, root)
        if args.scope == "reviewed":
            selected = reviewed_specs(root, info["sha"], entries, preview)
            _, extra_missing = process_specs(selected, downloader, verify_only=args.verify_only, list_missing=args.list_missing, dry_run=args.dry_run)
            missing.extend(extra_missing)
        elif args.verify_only:
            if not isinstance(read_json(review_path), list):
                raise DownloadError("REVIEW")
            if read_json(root / hosted_file) != [item for item in entries if item["path"].startswith("reference_files/")]:
                raise DownloadError("EXISTS")
    if args.dataset == "omni" and args.scope != "metadata":
        if not (root / "OmniDocBench.json").is_file():
            if args.dry_run or args.list_missing:
                print("需先完成 metadata 才能读取真实图片路径。")
                return 2 if args.list_missing else 0
            raise DownloadError("MISSING")
        candidates, rejected = omni_candidates(root, entries, args.seed)
        if args.scope == "all-images":
            selected_entries = [item for item in entries if item["type"] == "file" and item["path"].startswith("images/")]
            if any(Path(item["path"]).suffix.lower() not in {".png", ".jpg", ".jpeg"} for item in selected_entries):
                raise DownloadError("FORMAT")
            selected_entries.sort(key=path_key)
            selected = [hf_spec(item, "omni", info["sha"]) for item in selected_entries]
            _, extra_missing = process_specs(selected, downloader, verify_only=args.verify_only, list_missing=args.list_missing, dry_run=args.dry_run)
            missing.extend(extra_missing)
        else:
            # 有界检查同准备器（64 个、按 20 MiB 预算最多 12 个）；不追下载到“通过”。
            selected_candidates = candidates[:args.count]
            results, extra_missing = process_specs([hf_spec(item["entry"], "omni", info["sha"]) for item in selected_candidates],
                downloader, verify_only=args.verify_only, list_missing=args.list_missing, dry_run=args.dry_run)
            missing.extend(extra_missing)
            hashes, valid, covered = set(), [], set()
            for item in selected_candidates:
                result = results.get(item["entry"]["path"])
                if result is None:
                    continue
                if (result.get("width") != item["width"] or result.get("height") != item["height"]
                        or result["sha256"] in hashes):
                    rejected.append({"identity": item["identity"], "reason": "geometry_mismatch_or_duplicate_sha"})
                    continue
                hashes.add(result["sha256"])
                valid.append(item["identity"])
                covered.update(item["labels"])
            gaps = {"candidate_shortfall": args.count - len(valid), "missing_gt_categories": sorted(omni.TARGETS - covered)}
            report = {"revision": info["sha"], "seed": args.seed, "requested_count": args.count,
                "valid_candidate_identities": valid, "gaps": gaps, "rejected": rejected,
                "suggested_next_paths": [item["entry"]["path"] for item in candidates[args.count:args.count + 7]],
                "not_final_selection": True, "not_quality_verified": True}
            if not offline and not args.dry_run:
                write_private(root / ".download/omni_candidates.json", report, root)
            if not args.dry_run and not args.list_missing and (gaps["candidate_shortfall"] or gaps["missing_gt_categories"]):
                print("Omni 候选存在真实数量/覆盖缺口；查看 .download/omni_candidates.json，增加 --count 后重跑，不冒充抽样成功。")
                return 2
    print("本轮清单处理结束；不等于原件许可、解析质量验收、知识库 READY 或问答准确率通过。")
    return 2 if args.list_missing and missing else 0


def main(argv=None):
    """用户入口统一安全退出；任何错误停止依赖下载，不自动清理已有素材或报告。"""
    args = build_parser().parse_args(argv)
    network = None
    try:
        if args.scope not in SCOPES[args.dataset] or not -2**31 <= args.seed < 2**31:
            raise DownloadError("LIMIT")
        if args.dataset != "ocb" and args.hosted_file is not None:
            raise DownloadError("LIMIT")
        validate_tree_replacement_args(args)
        if args.scope == "native-library":
            # 惰性接入统一下载器的配套模块；默认规划/检查不创建 HTTP 客户端。
            from scripts.ocb_native_download import run_native_library
            return run_native_library(args, sys.modules[__name__])
        if (args.execute or args.resume or args.allow_external or args.native_formats is not None
                or args.native_results_dir is not None):
            raise DownloadError("LIMIT")
        if args.revision is not None:
            revision_value(args.revision)
        endpoint = endpoint_value(args.endpoint)
        root = source_root(args.dataset, create=not args.dry_run)
        print(f"下载入口：{endpoint}；数据集={args.dataset}；范围={args.scope}；顺序执行。")
        if args.list_missing and not (root / "dataset_info.json").is_file():
            print("缺少 dataset_info.json 和固定文件树；先执行该数据集 metadata（OHR 为 required）。")
            return 2
        if args.verify_only or args.list_missing:
            # 只读模式不创建客户端，也不请求版本/文件元数据。
            return run(args, root, None)
        network = Network(endpoint, retries=args.retries, timeout=args.timeout)
        if args.dry_run:
            return run(args, root, network)
        with dataset_lock(root):
            return run(args, root, network)
    except DownloadError as exc:
        print(exc)
        return 1
    except common.BenchmarkError as exc:
        # 保留白名单输入和失败步骤，不能把 QA/托管树失败误报为 dataset_info 错误。
        print(exc)
        return 1
    except common.ohr.PreparationError:
        print(DownloadError("PATH"))
        return 1
    except KeyboardInterrupt:
        print("下载已中断：保留已完成原件及本工具 .part/检查点，重跑同一命令可安全恢复。")
        return 130
    except OSError as exc:
        print(DownloadError("SPACE" if exc.errno == 28 else "IO"))
        return 1
    except Exception:
        print(DownloadError("IO"))
        return 1
    finally:
        if network is not None:
            network.close()


if __name__ == "__main__":
    raise SystemExit(main())
