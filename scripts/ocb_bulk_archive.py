#!/usr/bin/env python3
"""Best-effort local archive of all OCB native Office candidates and linked official QA.

Runs independently from the preset import and license review mechanisms. It performs
technical source/format/integrity checks; it does NOT certify rights or authorize
upload to cloud parsers. Default is offline dry-run. All networking is explicit.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
from threading import Lock
import tempfile
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

FORMATS = ("docx", "xlsx", "pptx")
PARTS = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}
MAX_REDIRECTS = 5
MAX_ZIP_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 10000
STATUS_OK = {"downloaded", "verified_existing"}


def digest_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_oid(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(b"blob " + str(path.stat().st_size).encode("ascii") + b"\0")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_local_path(base: Path, item: dict) -> Path:
    kind = item["file_type"]
    name = item["reference_file"]
    relative = item.get("local_file")
    if (not isinstance(name, str) or not name or len(name) > 230
            or name in (".", "..") or name.endswith((" ", "."))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or Path(name).suffix.lower() != "." + kind):
        raise ValueError("unsafe_filename")
    if (not isinstance(relative, str) or relative not in
            ("reference_files/" + name, "external_files/" + name)):
        raise ValueError("unexpected_local_path")
    if item.get("source_kind") == "hf" and not relative.startswith("reference_files/"):
        raise ValueError("hf_path_mismatch")
    if item.get("source_kind") == "external" and not relative.startswith("external_files/"):
        raise ValueError("external_path_mismatch")
    path = base / Path(relative)
    if path.is_symlink() or path.parent.is_symlink() or base.is_symlink():
        raise ValueError("symlink_not_allowed")
    return path


def validate_public_https(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("invalid_https_url")
    host = parsed.hostname.rstrip(".").lower()
    if (host in ("localhost",) or host.endswith((".local", ".internal", ".localhost"))
            or host.startswith("xn--") and len(host) > 253):
        raise ValueError("nonpublic_hostname")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("dns_lookup_failed") from exc
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
        raise ValueError("nonpublic_address")
    return host


def mirrored_url(official: str) -> str:
    parsed = urlsplit(official)
    endpoint = os.getenv("HF_ENDPOINT", "").rstrip("/")
    if parsed.hostname != "huggingface.co" or not endpoint:
        return official
    if endpoint not in ("https://hf-mirror.com", "https://huggingface.co"):
        raise ValueError("unsupported_HF_ENDPOINT")
    host = urlsplit(endpoint).netloc
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def inspect_office(path: Path, kind: str, expected_size: int | None,
                   official_tree: dict | None = None) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ValueError("missing_or_empty_file")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise ValueError("official_size_mismatch")
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if not 0 < len(infos) <= MAX_ZIP_MEMBERS:
                raise ValueError("unsafe_office_member_count")
            if sum(x.file_size for x in infos) > MAX_ZIP_UNCOMPRESSED:
                raise ValueError("unsafe_office_expansion")
            for member in infos:
                name = member.filename.replace("\\", "/")
                if (name.startswith("/") or ".." in name.split("/") or
                        name.startswith("\\") or ":" in name):
                    raise ValueError("unsafe_office_member_path")
            names = {m.filename for m in infos}
            if "[Content_Types].xml" not in names or PARTS[kind] not in names:
                raise ValueError("office_main_part_missing")
            if archive.testzip() is not None:
                raise ValueError("office_crc_failure")
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError("invalid_office_container") from exc
    local_sha = digest_file(path)
    if official_tree is not None:
        lfs = official_tree.get("lfs") or {}
        if isinstance(lfs, dict) and lfs.get("oid"):
            if local_sha.lower() != str(lfs["oid"]).removeprefix("sha256:").lower():
                raise ValueError("official_lfs_sha_mismatch")
        elif official_tree.get("oid"):
            if git_blob_oid(path).lower() != str(official_tree["oid"]).lower():
                raise ValueError("official_git_blob_mismatch")
        else:
            raise ValueError("hf_official_checksum_missing")
    return local_sha


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    # Report files are owned by this script only. A temporary file prevents torn JSON writes.
    fd, name = tempfile.mkstemp(prefix=".ocb-report-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_plan(project: Path) -> tuple[dict, list[dict]]:
    path = project / "docs/test/ocb_preset/results/native_download_plan.json"
    if not path.is_file():
        raise ValueError("缺少 native_download_plan.json；先执行 native-library --dry-run")
    plan = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(plan.get("candidates"), list) or not isinstance(plan.get("qa_groups"), list):
        raise ValueError("invalid_native_plan_structure")
    source = plan.get("source") or {}
    if source.get("repo") != "microsoft/OfficeComprehensionBenchmark":
        raise ValueError("wrong_dataset_repository")
    if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))):
        raise ValueError("missing_fixed_revision")
    names = [x.get("reference_file") for x in plan["candidates"]]
    if len(names) != len(set(names)):
        raise ValueError("duplicate_file_identity")
    out = []
    for item in plan["candidates"]:
        if item.get("reason") is not None:
            continue
        kind = item.get("file_type")
        if not (kind in FORMATS and item.get("conversion_required") is False
                and item.get("original_format") == kind and item.get("required_format") == kind
                and item.get("source_kind") in ("hf", "external")):
            raise ValueError("plan_eligible_candidate_inconsistent")
        url = item.get("source_url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise ValueError("candidate_missing_public_https_url")
        out.append(item)
    return plan, sorted(out, key=lambda x: (FORMATS.index(x["file_type"]), x["reference_file"]))


def validate_metadata(project: Path, plan: dict) -> None:
    source = project / "docs/test/ocb_preset/source"
    for name, expected in (plan.get("source", {}).get("metadata_sha256") or {}).items():
        if name not in ("dataset_info.json", "hosted_files.json", "ocb_qna_data.parquet",
                        "ocb_file_sources.parquet"):
            raise ValueError("unexpected_metadata_name")
        file = source / name
        if not file.is_file() or digest_file(file) != expected:
            raise ValueError("metadata_missing_or_changed:" + name)


class ByteBudget:
    def __init__(self, value: int):
        self.remaining = value
        self.lock = Lock()

    def take(self, count: int) -> bool:
        with self.lock:
            if count > self.remaining:
                return False
            self.remaining -= count
            return True


def get_with_redirect_checks(client, url: str):
    """Return streaming response; validate each public HTTPS redirect before requesting it."""
    current = url
    hosts = []
    for _ in range(MAX_REDIRECTS + 1):
        host = validate_public_https(current)
        hosts.append(host)
        request = client.build_request("GET", current, headers={"Accept-Encoding": "identity"})
        response = client.send(request, stream=True, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            response.close()
            if not location:
                raise ValueError("redirect_without_location")
            current = urljoin(current, location)
            continue
        return response, sorted(set(hosts))
    raise ValueError("too_many_redirects")


def record_base(item: dict) -> dict:
    return {
        "reference_file": item["reference_file"],
        "file_type": item["file_type"],
        "source_kind": item["source_kind"],
        "local_file": item["local_file"],
        "source_url": item["source_url"],
        "status": "pending",
        "technical_source_verified": False,
        "source_license_verified": False,
        "cloud_transfer_allowed": False,
        "sha256": None,
        "size_bytes": None,
        "redirect_hosts": [],
        "qa_ids": item.get("qa_ids", []),
    }


def archive_one(item: dict, source: Path, previous: dict, budget: ByteBudget,
                max_file_bytes: int, timeout: float, retries: int) -> dict:
    result = record_base(item)
    kind = item["file_type"]
    dest = safe_local_path(source, item)
    expected = item.get("size_bytes")
    official_tree = item.get("official_tree_object") if item["source_kind"] == "hf" else None
    if expected is not None and expected > max_file_bytes:
        return result | {"status": "file_too_large"}
    if dest.is_file():
        try:
            digest = inspect_office(dest, kind, expected, official_tree)
            if item["source_kind"] == "external":
                if (previous.get("sha256") != digest or
                        previous.get("status") not in STATUS_OK or
                        previous.get("source_url") != item["source_url"]):
                    return result | {"status": "existing_external_without_receipt"}
            return result | {"status": "verified_existing", "technical_source_verified": True,
                             "sha256": digest, "size_bytes": dest.stat().st_size}
        except (ValueError, OSError):
            return result | {"status": "existing_file_invalid_preserved"}
    if dest.exists() or dest.is_symlink():
        return result | {"status": "existing_path_not_regular"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("未安装 httpx: python -m pip show httpx") from exc
    url = mirrored_url(item["source_url"])
    last_error = "network_failed"
    for attempt in range(retries + 1):
        temp: Path | None = None
        try:
            # New temp filename each time; old user data is never overwritten.
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout, connect=10.0),
                              headers={"User-Agent": "OCB-local-benchmark-archive/1.0"}) as client:
                response, redirect_hosts = get_with_redirect_checks(client, url)
                # Client.send(stream=True) returns a Response without __enter__/__exit__ in httpx 0.28.
                # Ensure the streaming response is closed on every path.
                with closing(response):
                    code = response.status_code
                    if code != 200:
                        if code in (401, 403, 404):
                            return result | {"status": "http_" + str(code)}
                        if code == 429 or 500 <= code <= 599:
                            raise RuntimeError("temporary_http_" + str(code))
                        return result | {"status": "http_" + str(code)}
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type or "application/json" in content_type:
                        return result | {"status": "non_office_content_type"}
                    length_text = response.headers.get("content-length", "")
                    if length_text:
                        try:
                            length = int(length_text)
                        except ValueError:
                            return result | {"status": "invalid_content_length"}
                        if length < 0 or length > max_file_bytes:
                            return result | {"status": "file_too_large"}
                        if expected is not None and length != expected:
                            return result | {"status": "official_size_mismatch"}
                    fd, name = tempfile.mkstemp(prefix=".ocb-", suffix=".part", dir=dest.parent)
                    temp = Path(name)
                    downloaded = 0
                    with os.fdopen(fd, "wb") as output:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > max_file_bytes:
                                return result | {"status": "file_too_large"}
                            if not budget.take(len(chunk)):
                                return result | {"status": "total_download_budget_exceeded"}
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if length_text and downloaded != int(length_text):
                        return result | {"status": "download_length_mismatch"}
            digest = inspect_office(temp, kind, expected, official_tree)
            # Never replace a user file in an archive. Hardlink works on NTFS.
            try:
                os.link(temp, dest)
            except FileExistsError:
                return result | {"status": "destination_created_concurrently"}
            return result | {"status": "downloaded", "technical_source_verified": True,
                             "sha256": digest, "size_bytes": downloaded,
                             "redirect_hosts": redirect_hosts}
        except ValueError as exc:
            last_error = str(exc).split(":", 1)[0]
            # Invalid source, invalid container, DNS/private host, checksum failure: don't retry.
            return result | {"status": last_error}
        except (OSError, httpx.RequestError, RuntimeError) as exc:
            name = str(exc)
            last_error = name if name.startswith("temporary_http_") else type(exc).__name__
            if attempt < retries:
                time.sleep(min(2 ** attempt, 4))
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
    return result | {"status": "network_failed:" + last_error}


def qa_coverage(plan: dict, ready: set[str]) -> tuple[int, list[dict]]:
    covered = [q for q in plan["qa_groups"]
               if q.get("reference_files") and set(q["reference_files"]).issubset(ready)]
    return len(covered), covered


def export_qa(project: Path, plan: dict, records: list[dict], results: Path) -> int:
    ready = {r["reference_file"] for r in records if r["status"] in STATUS_OK}
    count, groups = qa_coverage(plan, ready)
    if not groups:
        atomic_json(results / "qa_full.json", [])
        atomic_json(results / "qa_smoke.json", [])
        return 0
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow: python -m pip show pyarrow") from exc
    rows = pq.read_table(project / "docs/test/ocb_preset/source/ocb_qna_data.parquet").to_pylist()
    full = []
    by_file: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        index = group["source_row_index"]
        if type(index) is not int or not 0 <= index < len(rows):
            raise ValueError("qa_source_row_out_of_bounds")
        row = rows[index]
        if not isinstance(row, dict):
            raise ValueError("qa_parquet_row_invalid")
        item = {"qa_id": group["qa_id"], "source_row_index": index,
                "reference_files": group["reference_files"], "track": group["track"],
                "question": row.get("question"),
                "expected_assertions": row.get("expected_assertions"),
                "original_qa": row}
        full.append(item)
        if len(group["reference_files"]) == 1:
            by_file[group["reference_files"][0]].append(item)
    atomic_json(results / "qa_full.json", full)
    # Repeatable balanced sample: 2 files x up to 4 questions per Office format.
    sample = []
    for kind in FORMATS:
        files = [r["reference_file"] for r in records if r["status"] in STATUS_OK
                 and r["file_type"] == kind and by_file.get(r["reference_file"])]
        files.sort(key=lambda file: (-len(by_file[file]), file))
        for file in files[:2]:
            sample.extend(by_file[file][:4])
    atomic_json(results / "qa_smoke.json", sample)
    return count


def summary_for(plan: dict, candidates: list[dict], records: list[dict], qa_count: int) -> dict:
    passed = sum(r["status"] in STATUS_OK for r in records)
    return {
        "eligible_files": len(candidates), "downloaded_or_verified": passed,
        "missing_or_failed": len(candidates) - passed,
        "formats": {fmt: {"target": sum(c["file_type"] == fmt for c in candidates),
                          "available": sum(r["file_type"] == fmt and r["status"] in STATUS_OK
                                           for r in records)} for fmt in FORMATS},
        "candidate_qa_with_all_deps": qa_coverage(
            plan, {x["reference_file"] for x in candidates})[0],
        "available_qa_with_all_deps": qa_count,
        "total_official_qa": len(plan["qa_groups"]),
        "failed_by_status": dict(Counter(r["status"] for r in records
                                        if r["status"] not in STATUS_OK)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="OCB: archive all native Office sources + official QA locally")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Only inspect the frozen plan; no network/writes")
    mode.add_argument("--execute", action="store_true", help="Attempt every native candidate download")
    mode.add_argument("--verify-only", action="store_true", help="Offline recheck already archived files")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent sources, 1-3")
    parser.add_argument("--max-file-mb", type=int, default=200)
    parser.add_argument("--max-total-mb", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--project-root", type=Path,
                        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if not (1 <= args.workers <= 3 and 1 <= args.max_file_mb <= 512
            and 1 <= args.max_total_mb <= 102400 and 10 <= args.timeout <= 180
            and 0 <= args.retries <= 3):
        parser.error("invalid worker, size, timeout, or retry limits")
    project = args.project_root.resolve()
    plan, candidates = load_plan(project)
    validate_metadata(project, plan)
    source = project / "docs/test/ocb_preset/source"
    results = project / "docs/test/ocb_preset/results/bulk_archive"
    for item in candidates:
        safe_local_path(source, item)
    all_qa, _ = qa_coverage(plan, {x["reference_file"] for x in candidates})
    print(f"Plan: {len(candidates)} native originals (" + ", ".join(
        f"{kind}={sum(i['file_type'] == kind for i in candidates)}" for kind in FORMATS) + ")")
    print(f"Official QA: {len(plan['qa_groups'])}, with complete dependencies if all files succeed: {all_qa}")
    print("Automatic technical screening only; no source license or MinerU cloud rights are approved.")
    if args.dry_run:
        return 0
    old_report = results / "archive_report.json"
    previous = {}
    if old_report.exists():
        old = json.loads(old_report.read_text(encoding="utf-8"))
        if old.get("plan_sha256") != plan.get("plan_sha256"):
            raise ValueError("previous_report_plan_conflict")
        previous = {r["reference_file"]: r for r in old.get("records", [])}
    if args.verify_only:
        checked = []
        for item in candidates:
            r = record_base(item)
            dest = safe_local_path(source, item)
            if dest.exists():
                try:
                    tree = item.get("official_tree_object") if item["source_kind"] == "hf" else None
                    digest = inspect_office(dest, item["file_type"], item.get("size_bytes"), tree)
                    known = previous.get(item["reference_file"], {})
                    if item["source_kind"] == "hf" or (known.get("sha256") == digest
                                                        and known.get("source_url") == item["source_url"]
                                                        and known.get("status") in STATUS_OK):
                        r.update(status="verified_existing", technical_source_verified=True,
                                 sha256=digest, size_bytes=dest.stat().st_size)
                    else:
                        r["status"] = "existing_external_without_receipt"
                except (OSError, ValueError) as exc:
                    r["status"] = str(exc).split(":", 1)[0]
            else:
                r["status"] = "missing"
            checked.append(r)
        valid_set = {r["reference_file"] for r in checked if r["status"] in STATUS_OK}
        qc = qa_coverage(plan, valid_set)[0]
        print(json.dumps(summary_for(plan, candidates, checked, qc), ensure_ascii=False, indent=2))
        return 0 if len(valid_set) == len(candidates) else 2
    budget = ByteBudget(args.max_total_mb * 1024 * 1024)
    records = {}
    results.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(archive_one, item, source,
                               previous.get(item["reference_file"], {}),
                               budget, args.max_file_mb * 1024 * 1024,
                               float(args.timeout), args.retries): item
                   for item in candidates}
        for number, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                r = future.result()
            except Exception as exc:
                r = record_base(item)
                r["status"] = "internal_error:" + type(exc).__name__
            records[item["reference_file"]] = r
            print(f"[{number}/{len(candidates)}] {r['file_type']:4} {r['reference_file']} -> {r['status']}", flush=True)
            ordered = [records[x["reference_file"]] for x in candidates if x["reference_file"] in records]
            atomic_json(old_report, {"dataset": "OCB", "revision": plan["source"]["revision"],
                                     "plan_sha256": plan["plan_sha256"],
                                     "rights_note": "Licenses and cloud-transfer permission NOT verified. Local technical archive only.",
                                     "records": ordered})
    ordered = [records[x["reference_file"]] for x in candidates]
    qa_count = export_qa(project, plan, ordered, results)
    summary = summary_for(plan, candidates, ordered, qa_count)
    atomic_json(old_report, {"dataset": "OCB", "revision": plan["source"]["revision"],
                             "plan_sha256": plan["plan_sha256"],
                             "finished_utc": datetime.now(timezone.utc).isoformat(),
                             "rights_note": "Licenses and cloud-transfer permission NOT verified. Local technical archive only.",
                             "summary": summary, "records": ordered})
    atomic_json(results / "missing_files.json", [r for r in ordered if r["status"] not in STATUS_OK])
    print("\nFinal report:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Outputs: " + str(results))
    print("  archive_report.json / missing_files.json / qa_full.json / qa_smoke.json")
    print("Downloaded originals: " + str(source / "reference_files") + " and " + str(source / "external_files"))
    return 0 if summary["missing_or_failed"] == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print("FAILED:", str(exc), file=sys.stderr)
        sys.exit(1)
