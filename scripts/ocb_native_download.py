"""统一下载器的 OCB 原生素材库实现；无独立 CLI，不接入业务解析、抽样、数据库或云端。"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import os
import re
import socket
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from urllib.parse import parse_qsl, quote, urljoin, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from scripts import benchmark_common as common
from scripts import prepare_ocb_preset as ocb

VERSION = "ocb-native-library-v1"
FORMATS = {"docx", "xlsx", "pptx", "csv", "xlsm"}
REPORT_NAMES = ("native_download_plan.json", "native_download_report.json", "license_review_candidates.json")
CSV_MAX_BYTES = 32 * 1024**2


class NativeError(ValueError):
    """只有固定类别和 HTTP 数字进入报告，绝不保存原始异常或重定向签名。"""

    def __init__(self, reason, status=None):
        """收敛不可识别的原因，避免正文、密码及本机路径成为动态错误信息。"""
        allowed = {"unsafe_source", "insecure_or_authenticated_source", "source_redirect_not_reviewed",
            "http_forbidden", "http_not_found", "login_required", "http_error", "unknown_content_length",
            "file_too_large", "total_budget_exceeded", "network_failed", "format_mismatch", "resume_rejected",
            "existing_file_unverified", "local_io", "license_review_pending", "external_not_authorized"}
        self.reason = reason if reason in allowed else "local_io"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        super().__init__("OCB_NATIVE：" + self.reason + (f"（HTTP {self.status}）" if self.status else ""))


def native_name(value, api):
    """按完整官方相对身份去重，仅移除明确的 reference_files 前缀，不按 basename 猜匹配。"""
    name = api.safe_relative(value)
    return name.removeprefix("reference_files/")


def conversion_value(value):
    """官方当前 convert 为 yes/no；同时接受明确布尔/false/true，不把空值或整数当许可。"""
    if type(value) is bool:
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"no", "false"}:
            return False
        if lowered in {"yes", "true"}:
            return True
    return None


def public_url(url, *, redirect=False):
    """仅接收无登录凭据的公开 HTTPS；原始查询仅允许无密钥的下载开关，签名跳转只留内存。"""
    if (not isinstance(url, str) or len(url) > 4096 or url != url.strip()
            or re.search(r"[\x00-\x1f\x7f]", url)):
        raise NativeError("unsafe_source")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise NativeError("unsafe_source") from None
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or port not in {None, 443} or parsed.fragment):
        raise NativeError("insecure_or_authenticated_source")
    if not redirect and any(key not in {"download", "raw", "format"} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)):
        raise NativeError("insecure_or_authenticated_source")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise NativeError("unsafe_source")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise NativeError("unsafe_source") from None
    if address is not None and not address.is_global:
        raise NativeError("unsafe_source")
    return parsed


def qa_dependencies(rows, source_hash, api):
    """收集全部 QA 的完整依赖（含 Domain/未知 track）；只保留行身份，不保存问题答案。"""
    references, groups, invalid = defaultdict(list), [], []
    for index, row in enumerate(rows):
        values = row.get("reference_files")
        values = [values] if isinstance(values, str) else values
        try:
            if not isinstance(values, list) or not values or len(values) > 100:
                raise ValueError
            names = [native_name(value, api) for value in values]
            if len(names) != len(set(names)):
                raise ValueError
        except (ValueError, common.ohr.PreparationError):
            invalid.append({"source_row_index": index, "reason": "invalid_qa_dependencies"})
            continue
        track = row.get("track")
        track = track if isinstance(track, str) and len(track) <= 128 and not re.search(r"[\x00-\x1f]", track) else "unknown_track"
        group = {"qa_id": source_hash + ":hf_row:" + str(index), "source_row_index": index,
                 "track": track, "reference_files": names}
        groups.append(group)
        for name in names:
            references[name].append(group["qa_id"])
    return references, groups, invalid


def build_native_plan(root, args, api):
    """离线冻结全部 QA/来源/同 revision 树；先按官方文件身份并集，再区分原生与真实缺口。"""
    hosted_file = args.hosted_file or "hosted_files.json"
    info, entries = api.fixed_metadata(root, "ocb", None, args.revision, offline=True, hosted_file=hosted_file)
    source = common.source_record(root, "ocb", [hosted_file, "ocb_qna_data.parquet", "ocb_file_sources.parquet"])
    if source["revision"] != info["sha"]:
        raise api.DownloadError("REVISION")
    qa = common.rows_file(root / "ocb_qna_data.parquet")
    manifest = common.rows_file(root / "ocb_file_sources.parquet")
    references, groups, invalid = qa_dependencies(qa, source["metadata_sha256"]["ocb_qna_data.parquet"], api)
    hosted = ocb.hosted_index(entries)
    sources = defaultdict(list)
    for index, row in enumerate(manifest):
        if not {"filename", "url", "original_format", "required_format", "convert"} <= set(row):
            raise api.DownloadError("SCHEMA")
        name = native_name(row["filename"], api)
        sources[name].append((index, row))
    names = sorted(set(hosted) | set(sources) | set(references))
    if len(names) > 10000 or len({name.casefold() for name in names}) != len(names):
        raise api.DownloadError("PATH")
    requested = sorted(set(args.native_formats or ["docx", "xlsx", "pptx"]))
    candidates = []
    for name in names:
        kind = Path(name).suffix.lower().lstrip(".")
        rows = sources.get(name, [])
        candidate = {"candidate_id": common.ohr.digest([source["revision"], name]), "reference_file": name,
            "file_type": kind, "qa_ids": references.get(name, []), "manifest_row_indices": [index for index, _ in rows],
            "hosted_available": name in hosted, "official_manifest_records": len(rows), "reason": None,
            "source_kind": "hf" if name in hosted else "external", "source_url": None,
            "local_file": ("reference_files/" if name in hosted else "external_files/") + name,
            "size_bytes": hosted[name]["size"] if name in hosted else None, "official_tree_object": hosted.get(name)}
        signatures = {(row["url"], str(row["original_format"]).casefold().lstrip("."),
            str(row["required_format"]).casefold().lstrip("."), conversion_value(row["convert"])) for _, row in rows}
        if kind not in requested:
            candidate["reason"] = "outside_requested_formats"
        elif not rows:
            candidate["reason"] = "native_status_unknown"
        elif len(signatures) != 1:
            candidate["reason"] = "ambiguous_official_manifest"
        else:
            url, original, required, conversion = next(iter(signatures))
            candidate.update(original_format=original, required_format=required, conversion_required=conversion)
            if conversion is True or original != required:
                candidate["reason"] = "conversion_required"
            elif conversion is not False or original != kind:
                candidate["reason"] = "native_status_unknown"
            elif name in hosted:
                # 同名真实 HF 原件优先，绝不再下载 manifest 的第三方替代版本。
                candidate["source_url"] = f"https://huggingface.co/datasets/{api.REPOS['ocb']}/resolve/{source['revision']}/" + quote(hosted[name]["path"], safe="/")
                limit = min(args.max_file_mb * api.MIB, CSV_MAX_BYTES) if kind == "csv" else args.max_file_mb * api.MIB
                if not 0 < candidate["size_bytes"] <= limit:
                    candidate["reason"] = "file_too_large"
            else:
                try:
                    public_url(url)
                    candidate["source_url"] = url
                except NativeError as exc:
                    candidate["reason"] = exc.reason
                    candidate["source_url_sha256"] = hashlib.sha256(str(url).encode()).hexdigest()
        candidates.append(candidate)
    plan = {"version": VERSION, "source": source, "formats": requested,
        "parameters": {"max_file_bytes": args.max_file_mb * api.MIB, "max_total_bytes": args.max_total_mb * api.MIB,
                       "csv_max_file_bytes": min(CSV_MAX_BYTES, args.max_file_mb * api.MIB)},
        "qa_row_count": len(qa), "manifest_row_count": len(manifest), "qa_groups": groups,
        "invalid_qa_rows": invalid, "candidates": candidates,
        "estimated_size": {"known_hf_bytes": sum(item["size_bytes"] for item in candidates if item["reason"] is None and item["source_kind"] == "hf"),
            "external_files_with_unknown_size": sum(item["reason"] is None and item["source_kind"] == "external" for item in candidates),
            "maximum_candidate_bytes": sum((item["size_bytes"] if item["source_kind"] == "hf" else
                min(CSV_MAX_BYTES, args.max_file_mb * api.MIB) if item["file_type"] == "csv" else args.max_file_mb * api.MIB)
                for item in candidates if item["reason"] is None),
            "execution_budget_bytes": args.max_total_mb * api.MIB,
            "external_bytes": None, "note": "第三方大小仅执行时读响应头；未猜测全量字节"}}
    return plan | {"plan_sha256": common.ohr.digest(plan)}


def load_reviews(root, api):
    """审核表只读；缺失等于未审核，不新建、覆盖或替用户批准任何记录。"""
    path = api.safe_path(root / "source_review.json", root)
    if not path.exists():
        return {}
    rows = api.read_json(path)
    if not isinstance(rows, list):
        raise api.DownloadError("REVIEW")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise api.DownloadError("REVIEW")
        name = native_name(row.get("reference_file"), api)
        if name in result:
            raise api.DownloadError("REVIEW")
        result[name] = row
    return result


def approved(candidate, reviews):
    """许可必须逐文件且精确绑定已选择来源；下载成功不能变成许可或云外传授权。"""
    row = reviews.get(candidate["reference_file"], {})
    return (row.get("source_url") == candidate["source_url"] and row.get("original_native") is True
        and row.get("local_use_confirmed") is True and row.get("conversion_required") is False
        and isinstance(row.get("license"), str) and bool(row["license"].strip()))


def candidate_review(plan):
    """生成待人工复制审核的候选模板；所有批准字段默认 false，不作为程序许可来源。"""
    return {"version": VERSION, "plan_sha256": plan["plan_sha256"], "not_approved": True,
        "source": plan["source"],
        "instructions": "仅把人工核对后的条目合并到 source/source_review.json；此文件不是审核记录",
        "candidates": [{"candidate_id": item["candidate_id"], "reference_file": item["reference_file"],
            "source_kind": item["source_kind"], "source_url": item["source_url"], "qa_ids": item["qa_ids"],
            "file_type": item["file_type"], "size_bytes": item["size_bytes"],
            "manifest_row_indices": item["manifest_row_indices"],
            "native_criteria": {"original_format": item.get("original_format"),
                "required_format": item.get("required_format"), "conversion_required": item.get("conversion_required")},
            "review_template": {"reference_file": item["reference_file"], "source_url": item["source_url"],
                "original_native": False, "local_use_confirmed": False, "conversion_required": False,
                "license": "", "allowed_redirect_hosts": []}}
            for item in plan["candidates"] if item["reason"] is None]}


def validate_native_file(path, spec, *, api):
    """复用正式 Office 下载校验；CSV/XLSM 只验证素材完整性，不扩展业务格式或执行宏。"""
    with path.open("rb") as stream:
        prefix = stream.read(512).lstrip().lower()
    if prefix.startswith((b"<html", b"<!doctype")):
        raise NativeError("login_required" if re.search(rb"login|sign[ -]?in|authentication", prefix) else "format_mismatch")
    if spec.kind not in {"csv", "xlsm"}:
        return api.validate_file(path, spec)
    size = path.stat().st_size
    if spec.kind == "csv" and size > CSV_MAX_BYTES:
        raise NativeError("file_too_large")
    if size != spec.size or size <= 0:
        raise api.DownloadError("FORMAT")
    sha, blob = hashlib.sha256(), hashlib.sha1(b"blob " + str(size).encode() + b"\x00")
    with path.open("rb") as stream:
        prefix = stream.read(512).lstrip().lower()
        if prefix.startswith((b"<html", b"<!doctype", b"version https://git-lfs")):
            raise NativeError("login_required" if b"login" in prefix or b"sign in" in prefix else "format_mismatch")
        stream.seek(0)
        while chunk := stream.read(api.MIB):
            sha.update(chunk)
            blob.update(chunk)
    if spec.sha256 and sha.hexdigest() != spec.sha256 or spec.git_oid and blob.hexdigest() != spec.git_oid:
        raise api.DownloadError("FORMAT")
    try:
        if spec.kind == "xlsm":
            with ZipFile(path) as archive:
                common.ohr.archive_index(archive)
                infos = archive.infolist()
                if len(infos) > 10000 or sum(item.file_size for item in infos) > 256 * api.MIB:
                    raise ValueError
                for name in ("[Content_Types].xml", "xl/workbook.xml"):
                    if archive.getinfo(name).file_size > 64 * api.MIB or not archive.read(name):
                        raise ValueError
                content = archive.read("[Content_Types].xml")
                if b"<!DOCTYPE" in content.upper():
                    raise ValueError
                types = ElementTree.fromstring(content)
                if not any(item.get("PartName") == "/xl/workbook.xml" and item.get("ContentType") ==
                    "application/vnd.ms-excel.sheet.macroEnabled.main+xml" for item in types):
                    raise ValueError
        else:
            # 有界严格编码和 CSV 语法检查，不执行公式、不调用项目 Parser；不声称语义完整。
            data = common.read_bytes(path, min(32 * api.MIB, spec.size), beneath=path.parent)
            text = None
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    text = data.decode(encoding, errors="strict")
                    break
                except UnicodeError:
                    continue
            if text is None or "\x00" in text:
                raise ValueError
            count = 0
            for index, row in enumerate(csv.reader(io.StringIO(text), strict=True)):
                if len(row) > 16384 or index >= 1000000:
                    raise ValueError
                if any(value.strip() for value in row):
                    count += 1
            if not count:
                raise ValueError
    except (ValueError, KeyError, OSError, BadZipFile, csv.Error, ElementTree.ParseError, common.ohr.PreparationError, common.BenchmarkError):
        raise api.DownloadError("FORMAT") from None
    return {"size_bytes": size, "sha256": sha.hexdigest(), "official_digest_checked": bool(spec.sha256 or spec.git_oid)}


@dataclass(frozen=True)
class ExternalSpec:
    """绑定官方来源 URL/版本与服务器真实长度；无官方 Hash 时不伪造摘要字段。"""

    local: str
    remote: str
    repo: str
    revision: str
    size: int
    kind: str
    source_url: str
    sha256: str | None = None
    git_oid: str | None = None

    def url(self, endpoint):
        """第三方走原始官方 manifest URL，不通过 HF 镜像改写第三方身份。"""
        return self.source_url


class ExternalNetwork:
    """基于既有 HTTP 客户端的第三方传输；每次跳转都限制人工许可域名并拒绝内网解析。"""

    def __init__(self, base, candidate, review, resolver=socket.getaddrinfo):
        """每原件独立客户端/HTTP 状态，不共享可变 Range 或响应；域名清单不能使用通配符。"""
        self.base, self.resolver = base, resolver
        self.httpx, self.endpoint, self.retries = base.httpx, base.endpoint, base.retries
        host = public_url(candidate["source_url"]).hostname
        additional = review.get("allowed_redirect_hosts", [])
        if (not isinstance(additional, list) or len(additional) > 10
                or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9.-]+", item) for item in additional)):
            raise NativeError("source_redirect_not_reviewed")
        self.hosts = {host, *additional}
        self.last_http_status, self.redirect_count = None, 0

    def check_url(self, url):
        """拒绝未经审核的跳转和私有/回环/链路本地地址；真实 DNS 仍受操作系统解析行为限制。"""
        parsed = public_url(url, redirect=True)
        if parsed.hostname not in self.hosts:
            raise NativeError("source_redirect_not_reviewed")
        try:
            answers = self.resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
                raise NativeError("unsafe_source")
        except (socket.gaierror, ValueError):
            raise NativeError("unsafe_source") from None

    @contextmanager
    def stream(self, url, headers=None, *, binary=False):
        """手动跟随至多五次已许可 HTTPS 跳转，不带 Cookie/Token，不持久化跳转签名。"""
        current = url
        for _ in range(6):
            self.check_url(current)
            # 公开素材下载不接受登录态；清除服务器可能设置的 Cookie，不通过登录绕过限制。
            self.base.client.cookies.clear()
            with self.base.client.stream("GET", current, headers=headers or {}) as response:
                self.last_http_status = response.status_code
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise NativeError("http_error", response.status_code)
                    destination = urljoin(current, location)
                    if re.search(r"/(login|signin|sign-in)(/|\?|$)", urlsplit(destination).path, re.I):
                        raise NativeError("login_required", response.status_code)
                    self.check_url(destination)
                    current = destination
                    self.redirect_count += 1
                    continue
                if response.status_code == 200 and "text/html" in response.headers.get("content-type", "").casefold():
                    prefix = next(response.iter_bytes(512), b"").lower()
                    reason = "login_required" if re.search(rb"login|sign[ -]?in|authentication", prefix) else "format_mismatch"
                    raise NativeError(reason, response.status_code)
                yield response
                return
        raise NativeError("source_redirect_not_reviewed", self.last_http_status)

    def pause(self, attempt, response=None):
        """复用统一有限退避，不新增无限循环或取消 TLS 校验。"""
        self.base.pause(attempt, response)


class ByteBudget:
    """并发原子预留本轮计划总容量，未知第三方大小由响应头取得后再计入。"""

    def __init__(self, limit):
        """初始化进程内锁；此预算由持有数据集锁的单 CLI 使用，不冒充跨主机配额。"""
        self.limit, self.total, self.lock, self.reserved = limit, 0, threading.Lock(), {}

    def reserve(self, name, size):
        """同一文件只预留一次，失败预留不自动转给其他文件以避免超量重试。"""
        with self.lock:
            if name in self.reserved:
                if self.reserved[name] != size:
                    raise NativeError("resume_rejected")
                return
            if self.total + size > self.limit:
                raise NativeError("total_budget_exceeded")
            self.reserved[name] = size
            self.total += size


def external_spec(candidate, plan, root, api, network=None):
    """离线沿用自己的来源回执；首次执行只读响应头取真实长度，不猜大小或覆盖未知文件。"""
    local = candidate["local_file"]
    state_path = api.safe_path(root / ".download" / (common.ohr.digest(local) + ".json"), root)
    if state_path.exists():
        state = api.read_json(state_path)
        try:
            spec = ExternalSpec(**state["spec"])
        except (KeyError, TypeError):
            raise NativeError("resume_rejected") from None
        if (state.get("owner") != api.OWNER or spec.local != local or spec.remote != candidate["reference_file"]
                or spec.source_url != candidate["source_url"] or spec.revision != plan["source"]["revision"]
                or spec.repo != "external:ocb" or spec.kind != candidate["file_type"]
                or type(spec.size) is not int or not 0 < spec.size <= plan["parameters"]["csv_max_file_bytes" if spec.kind == "csv" else "max_file_bytes"]
                or spec.sha256 is not None or spec.git_oid is not None):
            raise NativeError("resume_rejected")
        return spec
    if api.safe_path(root / local, root).exists():
        raise NativeError("existing_file_unverified")
    if network is None:
        raise api.DownloadError("MISSING")
    for attempt in range(network.retries + 1):
        response = None
        try:
            with network.stream(candidate["source_url"], binary=True) as response:
                status = response.status_code
                if status == 429 or 500 <= status <= 599:
                    if attempt == network.retries:
                        raise api.DownloadError("HTTP", status)
                elif status != 200:
                    raise api.DownloadError("HTTP", status)
                else:
                    if "text/html" in response.headers.get("content-type", "").casefold():
                        raise NativeError("login_required", status)
                    length = response.headers.get("content-length", "")
                    if not length.isdecimal() or int(length) <= 0:
                        raise NativeError("unknown_content_length", status)
                    if int(length) > plan["parameters"]["csv_max_file_bytes" if candidate["file_type"] == "csv" else "max_file_bytes"]:
                        raise NativeError("file_too_large", status)
                    return ExternalSpec(local, candidate["reference_file"], "external:ocb", plan["source"]["revision"],
                                        int(length), candidate["file_type"], candidate["source_url"])
        except network.httpx.TransportError:
            if attempt == network.retries:
                raise NativeError("network_failed") from None
        network.pause(attempt, response)
    raise NativeError("network_failed")


def failure_record(exc, api, http_status=None):
    """区分真实 HTTP/登录/断点/格式/大小原因；不抄录服务器错误正文。"""
    if isinstance(exc, NativeError):
        return exc.reason, exc.status or http_status
    if isinstance(exc, api.DownloadError):
        status = exc.status or http_status
        if exc.code == "HTTP":
            return {401: "login_required", 403: "http_forbidden", 404: "http_not_found"}.get(status, "http_error"), status
        return {"FORMAT": "format_mismatch", "RESUME": "resume_rejected", "EXISTS": "existing_file_unverified",
            "NETWORK": "network_failed", "LIMIT": "file_too_large", "MISSING": "not_downloaded",
            "PATH": "unsafe_source", "SPACE": "disk_space_insufficient", "DEPENDENCY": "dependency_missing",
            "CANCELLED": "interrupted"}.get(exc.code, "local_io"), status
    return "local_io", http_status


def transfer_one(candidate, plan, reviews, root, args, api, budget, *, network_factory=None, validator=None,
                 resolver=socket.getaddrinfo, cancel_event=None):
    """单原件任务：先许可/身份，成功复用既有验证/断点；只有 execute 才创建网络客户端。"""
    started = time.monotonic()
    record = {"candidate_id": candidate["candidate_id"], "reference_file": candidate["reference_file"],
        "source_kind": candidate["source_kind"], "source_url": candidate["source_url"], "local_file": candidate["local_file"],
        "qa_ids": candidate["qa_ids"], "http_status": None, "result": None, "status": "pending", "reason": candidate["reason"]}
    if candidate["reason"] is not None:
        record["status"] = "blocked"
        return record
    if args.execute and not approved(candidate, reviews):
        record.update(status="blocked", reason="license_review_pending")
        return record
    if args.execute and candidate["source_kind"] == "external" and not args.allow_external:
        record.update(status="blocked", reason="external_not_authorized")
        return record
    base = network = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise api.DownloadError("CANCELLED")
        part = api.safe_path(root / (candidate["local_file"] + ".part"), root)
        if args.execute and part.exists() and not args.resume:
            raise NativeError("resume_rejected")
        if args.execute:
            base = (network_factory or api.Network)(api.endpoint_value(args.endpoint), retries=args.retries, timeout=args.timeout)
            network = base
            if candidate["source_kind"] == "external":
                network = ExternalNetwork(base, candidate, reviews[candidate["reference_file"]], resolver)
        if candidate["source_kind"] == "hf":
            spec = api.hf_spec(candidate["official_tree_object"], "ocb", plan["source"]["revision"])
        else:
            spec = external_spec(candidate, plan, root, api, network)
        budget.reserve(candidate["local_file"], spec.size)
        downloader = api.Downloader(root, network, validator or partial(validate_native_file, api=api), cancel_event=cancel_event)
        target_exists = api.safe_path(root / spec.local, root).exists()
        if candidate["source_kind"] == "external" and target_exists:
            # 无官方 Hash 的既有原件必须有本工具冻结本地 Hash 的完整回执，不能只验大小。
            state = api.read_json(downloader.state_path(spec))
            if not isinstance(state.get("result"), dict) or not re.fullmatch(r"[0-9a-f]{64}", str(state["result"].get("sha256", ""))):
                raise NativeError("existing_file_unverified")
        result = downloader.download(spec) if args.execute else downloader.verify(spec)
        record.update(status="verified_existing" if target_exists else "downloaded", result=result, reason=None)
    except (NativeError, api.DownloadError, common.BenchmarkError, common.ohr.PreparationError, OSError, ValueError) as exc:
        reason, status = failure_record(exc, api, getattr(network, "last_http_status", None))
        record.update(status="missing" if reason == "not_downloaded" else "failed", reason=reason, http_status=status)
    except Exception:
        # 第三方客户端/文件格式的未知异常不附原文；单项失败仍可追溯，其他原件不被撤销。
        record.update(status="failed", reason="local_io", http_status=getattr(network, "last_http_status", None))
    finally:
        if network is not None and hasattr(network, "last_http_status"):
            record["http_status"] = network.last_http_status
        if base is not None:
            base.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


def report_summary(plan, records, reviews):
    """按文件身份统计而非按 QA 条数凑数量；许可和下载分别计数，失败保留原因。"""
    by_format = {}
    indexed = {item["candidate_id"]: item for item in records}
    for kind in plan["formats"]:
        items = [item for item in plan["candidates"] if item["file_type"] == kind]
        native = [item for item in items if item["reason"] is None]
        states = [indexed.get(item["candidate_id"], {}).get("status", "pending") for item in items]
        by_format[kind] = {"total_candidates": len(items), "native_candidates": len(native),
            "hf_hosted": sum(item["source_kind"] == "hf" for item in native),
            "external_native": sum(item["source_kind"] == "external" for item in native),
            "duplicate_qa_references": sum(max(0, len(item["qa_ids"]) - 1) for item in items),
            "hf_manifest_overlap_identities": sum(item["hosted_available"] and item["official_manifest_records"] > 0 for item in native),
            "conversion_required": sum(item["reason"] == "conversion_required" for item in items),
            "license_pending": sum(not approved(item, reviews) for item in native),
            "download_success": sum(state in {"downloaded", "verified_existing"} for state in states),
            "download_failed": states.count("failed"),
            "pending_download": sum(indexed.get(item["candidate_id"], {}).get("status", "pending")
                not in {"downloaded", "verified_existing", "failed"} for item in native),
            "not_successfully_obtained": sum(state not in {"downloaded", "verified_existing"} for state in states)}
    acquired = {item["reference_file"] for item in plan["candidates"]
        if indexed.get(item["candidate_id"], {}).get("status") in {"downloaded", "verified_existing"}}
    return {"by_format": by_format, "qa_rows": plan["qa_row_count"],
        "totals": {key: sum(value[key] for value in by_format.values()) for key in next(iter(by_format.values()), {})},
        "qa_dependency_groups": len(plan["qa_groups"]), "invalid_qa_rows": len(plan["invalid_qa_rows"]),
        "qa_groups_with_all_files_verified": sum(set(group["reference_files"]) <= acquired for group in plan["qa_groups"]),
        "reason_counts": dict(sorted(Counter(item.get("reason") for item in records if item.get("reason")).items())),
        "not_license_or_quality_acceptance": True}


def run_native_library(args, api):
    """仅用户运行时创建报告/原件；离线规划默认安全，执行报告仅更新本工具同计划状态。"""
    if (not 1 <= args.max_file_mb <= 512 or not args.max_file_mb <= args.max_total_mb <= 102400
            or args.replace_hosted_tree or args.resume and not args.execute or args.allow_external and not args.execute):
        raise api.DownloadError("LIMIT")
    root = api.source_root("ocb", create=True)
    api.safe_path(root / "external_files", root).mkdir(parents=True, exist_ok=True)
    base_results = api.safe_path(api.PROJECT_ROOT / "docs/test/ocb_preset/results", api.PROJECT_ROOT)
    requested_output = args.native_results_dir or base_results
    if not requested_output.is_absolute():
        requested_output = api.PROJECT_ROOT / requested_output
    output = api.safe_path(requested_output, base_results)
    with api.dataset_lock(root):
        plan = build_native_plan(root, args, api)
        reviews = load_reviews(root, api)
        output.mkdir(parents=True, exist_ok=True)
        plan_path, report_path, license_path = [api.safe_path(output / name, base_results) for name in REPORT_NAMES]
        # 不覆盖旧计划/模板；新范围/预算/来源必须使用另一个 results 子目录。
        api.publish_bytes(plan_path, common.ohr.encoded(plan) + b"\n", base_results)
        if license_path.exists():
            existing_license = api.read_json(license_path)
            if not isinstance(existing_license, dict) or existing_license.get("plan_sha256") != plan["plan_sha256"]:
                raise api.DownloadError("EXISTS")
        else:
            api.publish_bytes(license_path, common.ohr.encoded(candidate_review(plan)) + b"\n", base_results)
        previous = None
        if report_path.exists():
            previous = api.read_json(report_path)
            if (not isinstance(previous, dict) or previous.get("owner") != api.OWNER
                    or previous.get("plan_sha256") != plan["plan_sha256"]):
                raise api.DownloadError("EXISTS")
            if args.execute and previous.get("mode") in {"execute", "interrupted"} and not args.resume:
                raise api.DownloadError("RESUME")
            if not isinstance(previous.get("records"), list) or any(not isinstance(item, dict) or
                    item.get("candidate_id") not in {row["candidate_id"] for row in plan["candidates"]}
                    for item in previous["records"]):
                raise api.DownloadError("SCHEMA")
            for item in previous["records"]:
                history = item.get("history", [])
                if not isinstance(history, list) or len(history) > 50 or any(not isinstance(event, dict) for event in history):
                    raise api.DownloadError("SCHEMA")
        prior = {item["candidate_id"]: item for item in (previous or {}).get("records", [])}
        records = []
        mode = "execute" if args.execute else "verify-only" if args.verify_only else "list-missing" if args.list_missing else "dry-run"
        if mode == "dry-run":
            records = [{"candidate_id": item["candidate_id"], "reference_file": item["reference_file"],
                "source_kind": item["source_kind"], "source_url": item["source_url"], "local_file": item["local_file"],
                "qa_ids": item["qa_ids"], "result": None, "http_status": None,
                "status": "blocked" if item["reason"] or not approved(item, reviews) else "pending",
                "reason": item["reason"] or (None if approved(item, reviews) else "license_review_pending")}
                for item in plan["candidates"]]
            # 规划只读取元数据，不验证或修改任何本地原件；已有运行报告保持原样。
            if previous is not None:
                print("已有下载报告保留；本次只显示重新计算的候选/许可摘要。")
        budget = ByteBudget(plan["parameters"]["max_total_bytes"])
        review_hash = hashlib.sha256(common.read_bytes(root / "source_review.json")).hexdigest() if (root / "source_review.json").exists() else None

        def save_report(current_mode):
            """主线程发布同计划下载状态，不触碰用户许可、抽样报告或业务 manifest。"""
            api.write_private(report_path, {"version": VERSION, "plan_sha256": plan["plan_sha256"],
                "mode": current_mode, "summary": report_summary(plan, records, reviews),
                "records": sorted(records, key=lambda item: item["candidate_id"]),
                "review_file_sha256": review_hash}, base_results)

        def keep_history(record):
            """保留最多五十次安全状态/HTTP/Hash 事件；离线复核不抹掉之前失败或成功证据。"""
            old = prior.get(record["candidate_id"], {})
            history = list(old.get("history", []))
            if old and not history:
                history.append({key: old.get(key) for key in ("status", "reason", "http_status", "result")})
            history.append({"mode": mode, "status": record["status"], "reason": record.get("reason"),
                "http_status": record.get("http_status"), "result": record.get("result"),
                "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return record | {"history": history[-50:]}

        if mode != "dry-run":
            pending = {item["candidate_id"]: item for item in plan["candidates"]}
            # 新一轮初始化只保留历史，不把未经本轮核对的旧成功冒充当前完整性通过。
            records = [{"candidate_id": item["candidate_id"], "reference_file": item["reference_file"],
                "source_kind": item["source_kind"], "source_url": item["source_url"], "local_file": item["local_file"],
                "qa_ids": item["qa_ids"], "status": "pending", "reason": None,
                "http_status": None, "result": None, "history": prior.get(item["candidate_id"], {}).get("history", [])}
                for item in plan["candidates"]]
            save_report(mode)
            cancel_event = threading.Event()
            executor = ThreadPoolExecutor(max_workers=args.workers)
            futures = {}
            remaining = iter(plan["candidates"])

            def submit_next():
                """只保留并发窗口的在途任务，Ctrl+C 后不让整仓剩余文件继续联网。"""
                item = next(remaining, None)
                if item is not None and not cancel_event.is_set():
                    future = executor.submit(transfer_one, item, plan, reviews, root, args, api, budget, cancel_event=cancel_event)
                    futures[future] = item

            def accept_record(record):
                """主线程替换单项运行状态并立即持久化，未完成项和历史均保留。"""
                updated = keep_history(record)
                for index, old in enumerate(records):
                    if old["candidate_id"] == record["candidate_id"]:
                        records[index] = updated
                        break
                pending.pop(record["candidate_id"], None)

            try:
                for _ in range(args.workers):
                    submit_next()
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        futures.pop(future)
                        record = future.result()
                        accept_record(record)
                        save_report(mode)
                        print(f"OCB 原件：{record['reference_file']}；{record['status']}；原因={record['reason'] or '-'}；HTTP={record['http_status'] or '-'}。")
                        submit_next()
            except KeyboardInterrupt:
                cancel_event.set()
                for future in futures:
                    future.cancel()
                # 只等待最多三个在途请求释放并刷盘；受读超时限制，不继续排队发请求。
                executor.shutdown(wait=True, cancel_futures=True)
                for future in futures:
                    if future.done() and not future.cancelled():
                        accept_record(future.result())
                for record in records:
                    if record["candidate_id"] in pending:
                        record.update(status="pending", reason="interrupted")
                save_report("interrupted")
                raise
            except BaseException:
                # 报告写盘失败等非单文件故障停止窗口扩展，不能留下仍在全仓排队的下载任务。
                cancel_event.set()
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        elif previous is None:
            save_report(mode)
        summary = report_summary(plan, records, reviews)
        print("OCB 全量原生候选：" + str(summary["by_format"]))
        print(f"HF 已知大小={plan['estimated_size']['known_hf_bytes']} 字节；第三方未知大小文件数={plan['estimated_size']['external_files_with_unknown_size']}；全部 QA={plan['qa_row_count']}。")
        print("报告仅为原始素材准备，不代表许可、完整 QA 依赖、解析质量、READY 或问答准确率验收。")
        if any(item["status"] == "failed" for item in records):
            return 1
        if mode != "dry-run" and (plan["invalid_qa_rows"] or any(item["status"] not in {"downloaded", "verified_existing"}
                and item.get("reason") != "outside_requested_formats" for item in records)):
            return 2
        return 0
