"""Build a Stage-16 preset manifest from an already-PASS Stage-16/17 smoke run.

Offline only: never calls the API, database, Redis, MinerU, or embedding models.
Copy sources only when --execute is provided.  Content labels remain human claims;
never silently infer text_pdf/scan_pdf/mixed/visual semantic truth.

Run from the dkf-agent project root after placing this file into scripts/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath

ALLOWED = {"pdf", "txt", "md", "csv", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "jpg", "jpeg", "png"}
TAGS = {"text_pdf", "scan_pdf", "mixed", "table", "formula", "chart", "reading_order"}
LEGACY = ("doc", "xls", "ppt")
MODERN = {"txt", "md", "csv", "docx", "xlsx", "pptx", "pdf", "image"}
OHR_TAG_CANDIDATES = {"table": "table", "formula": "formula", "chart": "chart", "reading_order": "reading_order"}
MAX_BYTES = 200 * 1024 * 1024


class BuildError(RuntimeError):
    """Stop on unverified input, overwrite risk, or an unexpected state."""


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def is_link(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def check_plain_file(path: Path) -> Path:
    if not path.is_file() or is_link(path) or path.stat().st_size < 1 or path.stat().st_size > MAX_BYTES:
        raise BuildError(f"Not a valid, bounded, regular source file: {path}")
    return path


def rel_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise BuildError(f"Unsafe filename: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in (".", "..") for p in path.parts):
        raise BuildError(f"Unsafe relative filename: {value!r}")
    if any(p.startswith(".") and p != ".gitkeep" for p in path.parts):
        raise BuildError(f"Unexpected hidden path: {value!r}")
    if path.suffix.lower().lstrip(".") not in ALLOWED:
        raise BuildError(f"Unsupported document suffix: {value!r}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_run(run: Path) -> tuple[dict, dict]:
    plan_file = check_plain_file(run / "plan.json")
    plan = read_json(plan_file)
    if not isinstance(plan, dict) or plan.get("version") != "stage16-17-smoke-v1":
        raise BuildError("Expected a Stage-16/17 three-dataset smoke plan")
    identity = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if hashlib.sha256(canonical(identity)).hexdigest() != plan.get("plan_sha256"):
        raise BuildError("Smoke plan SHA256 does not match")
    rows = plan.get("documents")
    if not isinstance(rows, list) or not 0 < len(rows) <= 20:
        raise BuildError("Smoke plan has an invalid documents array")
    reports = sorted((run / "reports").glob("*-report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    report = None
    for path in reports:
        item = read_json(path)
        if (isinstance(item, dict) and item.get("plan_sha256") == plan["plan_sha256"]
                and item.get("small_sample_smoke") == "PASS" and item.get("ready_count") == len(rows)
                and item.get("pipeline_status") == "PASS" and item.get("dense_status") == "PASS"):
            report = item
            break
    if report is None:
        raise BuildError(f"No completed PASS report matches plan in {run}")
    names, hashes = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise BuildError("Invalid smoke document entry")
        name = rel_name(row.get("file"))
        if not name.startswith("samples/") or name in names:
            raise BuildError("Duplicate/unsafe smoke sample path")
        path = (run / name)
        check_plain_file(path)
        # A selected sample never escapes this run tree (even via parent directories).
        for parent in (path, *path.parents):
            if parent == run.parent:
                break
            if is_link(parent):
                raise BuildError(f"Linked component: {parent}")
        if not path.resolve().is_relative_to(run.resolve()):
            raise BuildError("Smoke file escapes run directory")
        digest = sha256(path)
        if digest != row.get("sha256") or path.stat().st_size != row.get("size_bytes"):
            raise BuildError(f"Sample file changed from frozen plan: {name}")
        if digest in hashes:
            raise BuildError("Repeated sample content cannot count as independent documents")
        hashes.add(digest)
        names.add(name)
    return plan, report


def auto_run(smoke_dir: Path, requested: str | None) -> Path:
    if requested:
        if requested in (".", "..") or "/" in requested or "\\" in requested or ":" in requested:
            raise BuildError("Run must be a simple directory name")
        run = smoke_dir / requested
        if not run.is_dir() or is_link(run):
            raise BuildError(f"Smoke run does not exist: {run}")
        return run
    candidates = []
    for run in smoke_dir.iterdir() if smoke_dir.is_dir() else ():
        if not run.is_dir() or is_link(run) or not (run / "plan.json").is_file():
            continue
        try:
            validate_run(run)
        except (BuildError, ValueError, OSError, KeyError, TypeError):
            continue
        candidates.append(run)
    if not candidates:
        raise BuildError("No matching PASS smoke run; use --run with an existing run name")
    return max(candidates, key=lambda p: (p / "plan.json").stat().st_mtime)


def ole_valid(path: Path, extension: str) -> bool:
    if path.suffix.lower() != "." + extension or path.stat().st_size < 512:
        return False
    with path.open("rb") as stream:
        if stream.read(8) != bytes.fromhex("d0cf11e0a1b11ae1"):
            return False
    try:
        import olefile
        with olefile.OleFileIO(str(path)) as ole:
            streams = {name[-1].casefold() for name in ole.listdir(streams=True) if name}
        return bool(streams & {"doc": {"worddocument"}, "xls": {"workbook", "book"}, "ppt": {"powerpoint document"}}[extension])
    except (ImportError, OSError, ValueError, TypeError):
        return False


def legacy_candidates(root: Path, args: argparse.Namespace) -> tuple[dict[str, Path], list[str]]:
    sources = [root / "docs/stage11a_multiformat_test_files", root / "docs/test/stage11a_multiformat_test_files"]
    sources.extend(Path(p).expanduser().resolve() for p in args.legacy_dir)
    chosen: dict[str, Path] = {}
    notes = []
    for extension in LEGACY:
        explicit = getattr(args, extension)
        if explicit:
            path = check_plain_file(Path(explicit).expanduser().resolve())
            if not ole_valid(path, extension):
                raise BuildError(f"Explicit --{extension} is not a recognized native OLE {extension.upper()}: {path}")
            chosen[extension] = path
            continue
        candidates = []
        for directory in sources:
            if not directory.is_dir() or is_link(directory):
                continue
            for path in directory.rglob("*." + extension):
                if is_link(path) or any(is_link(parent) for parent in path.parents if parent == directory or directory in parent.parents):
                    continue
                if (path.is_file() and 512 <= path.stat().st_size <= MAX_BYTES
                        and not any(text in path.name.casefold() for text in ("invalid", "corrupt", "fake", "broken", "empty"))
                        and ole_valid(path, extension)):
                    candidates.append(path)
        if candidates:
            chosen[extension] = sorted(candidates, key=lambda p: (p.stat().st_size, str(p).lower()))[0]
            if len(candidates) > 1:
                notes.append(f"Found {len(candidates)} valid {extension.upper()} files, chose the smallest; review suitability")
        else:
            notes.append(f"Missing true {extension.upper()} source; give --{extension} /path/to/file.{extension}")
    return chosen, notes


def scan_preset(preset: Path) -> dict[str, Path]:
    existing: dict[str, Path] = {}
    if not preset.exists():
        return existing
    if is_link(preset):
        raise BuildError("Preset root cannot be a link or junction")
    for base, dirs, files in os.walk(preset, followlinks=False):
        for item in dirs + files:
            if is_link(Path(base) / item):
                raise BuildError(f"Preset contains a linked path: {Path(base) / item}")
        for name in files:
            path = Path(base) / name
            relative = path.relative_to(preset).as_posix()
            if relative == "manifest.json" or name == ".gitkeep":
                continue
            rel_name(relative)
            check_plain_file(path)
            existing[relative] = path
    return existing


def previous_tags(preset: Path) -> dict[str, list[str]]:
    manifest = preset / "manifest.json"
    if not manifest.is_file():
        return {}
    obj = read_json(manifest)
    if not isinstance(obj, dict) or set(obj) != {"version", "documents"} or obj["version"] != 1:
        raise BuildError("Existing manifest has an unsupported schema; not overwriting it")
    result = {}
    for row in obj["documents"]:
        if not isinstance(row, dict) or set(row) != {"file", "tags"}:
            raise BuildError("Existing manifest contains unsupported entry fields")
        name = rel_name(row["file"])
        if name in result or not isinstance(row["tags"], list) or set(row["tags"]) - TAGS:
            raise BuildError("Existing manifest has duplicate paths or invalid tags")
        result[name] = list(row["tags"])
    return result


def reviewed_tags(path: str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    content = read_json(Path(path))
    if not isinstance(content, dict):
        raise BuildError("--tags-file must be a JSON mapping: relative filename -> list of confirmed tags")
    result = {}
    for name, tags in content.items():
        rel_name(name)
        if not isinstance(tags, list) or any(tag not in TAGS for tag in tags):
            raise BuildError("Invalid confirmed tag map entry")
        result[name] = sorted(set(tags))
    return result


def ohr_hints(run: Path) -> dict[str, set[str]]:
    refs = run / "official_qa.jsonl"
    hints: dict[str, set[str]] = {}
    if not refs.is_file() or is_link(refs):
        return hints
    with refs.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            entry = json.loads(line)
            info = entry.get("_ohr_reference", {})
            category = info.get("category")
            sample = info.get("preset_file")
            if category in OHR_TAG_CANDIDATES and isinstance(sample, str):
                hints.setdefault(sample, set()).add(OHR_TAG_CANDIDATES[category])
    return hints


def install_file(source: Path, target: Path, expected_hash: str) -> bool:
    if target.exists():
        check_plain_file(target)
        if sha256(target) != expected_hash:
            raise BuildError(f"Existing target differs; refusing to overwrite: {target}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # A new file is exclusive: failed copies are removed; never overwrites user content.
    try:
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if sha256(target) != expected_hash:
            raise BuildError(f"Copied file checksum mismatch: {target}")
    except Exception:
        if target.is_file():
            target.unlink()
        raise
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Stage-16 preset builder; never executes the importer")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--run", help="An already-PASS run under docs/test/stage16_smoke; default finds latest PASS")
    parser.add_argument("--legacy-dir", action="append", default=[], help="Additional directory to look for real legacy Office files")
    for ext in LEGACY:
        parser.add_argument("--" + ext, help=f"Explicit existing legacy .{ext} file")
    parser.add_argument("--extra", action="append", default=[], help="Extra verified real document to copy under extra/")
    parser.add_argument("--tags-file", help="JSON mapping of file -> tags you have checked against originals")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default); no copying or manifest changes")
    mode.add_argument("--execute", action="store_true", help="Copy existing files and write manifest.json locally")
    args = parser.parse_args(argv)
    try:
        root = args.project_root.resolve()
        if not (root / "scripts/import_knowledge_base.py").is_file():
            raise BuildError("--project-root is not a dkf-agent checkout")
        preset = root / "data/knowledge_base/preset"
        run = auto_run(root / "docs/test/stage16_smoke", args.run)
        plan, _report = validate_run(run)
        legacy, notes = legacy_candidates(root, args)
        sources: dict[str, tuple[Path, str]] = {}
        for entry in plan["documents"]:
            name = rel_name(entry["file"])
            sources[name] = (run / name, entry["sha256"])
        for ext, path in legacy.items():
            name = rel_name("legacy/" + path.name)
            sources[name] = (path, sha256(path))
        for name in args.extra:
            path = check_plain_file(Path(name).expanduser().resolve())
            if is_link(path):
                raise BuildError("Extra input is a linked path")
            destination = rel_name("extra/" + path.name)
            if destination in sources:
                raise BuildError("Duplicate extra basename; rename only when content identity remains clear")
            sources[destination] = (path, sha256(path))
        current = scan_preset(preset)
        old_tags = previous_tags(preset)
        approved = reviewed_tags(args.tags_file)
        if set(old_tags) - (set(current) | set(sources)):
            raise BuildError("Existing manifest lists missing source files; not rewriting it")
        if set(approved) - (set(current) | set(sources)):
            raise BuildError("--tags-file includes names not in selected or existing preset")
        for name, (source, digest) in sources.items():
            if name in current and sha256(current[name]) != digest:
                raise BuildError(f"Existing preset file conflicts with selected source: {name}")
            if not source.is_file() or sha256(source) != digest:
                raise BuildError(f"Source changed while preparing: {name}")
        names = sorted(set(current) | set(sources))
        manifests = [{"file": name, "tags": sorted(set(approved.get(name, old_tags.get(name, []))))} for name in names]
        manifest = {"version": 1, "documents": manifests}
        hashes = {name: sha256(current[name]) if name in current else sources[name][1] for name in names}
        types = {"image" if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"} else Path(name).suffix.lower()[1:] for name in names}
        tags = set().union(*(set(item["tags"]) for item in manifests))
        gaps = {"missing_legacy": sorted(set(LEGACY) - types),
                "missing_modern_count": max(0, 3 - len(types & MODERN)),
                "missing_distinct_sha_count": max(0, 10 - len(set(hashes.values()))),
                "unconfirmed_content_tags": sorted(TAGS - tags)}
        hint_map = ohr_hints(run)
        review = {"smoke_run": run.name,
                  "warning": "Tag suggestions are benchmark QA evidence hints, not verified content truth. Review originals first.",
                  "candidate_entries": [{"file": item["file"], "current_tags": item["tags"],
                                        "candidate_from_official_ohr_qa": sorted(hint_map.get(item["file"], set()))}
                                       for item in manifests],
                  "gaps": gaps,
                  "notes": notes,
                  "sources_count": len(sources), "distinct_sha256_count": len(set(hashes.values())),
                  "files_count": len(names), "contains_cloud_permissions": False}
        print(f"Selected PASS smoke run: {run.name}; smoke files: {len(plan['documents'])}")
        print(f"Planned preset: {len(names)} files, distinct SHA256: {len(set(hashes.values()))}")
        print(f"Real legacy sources: {', '.join(sorted(legacy)) or 'none'}")
        print("Coverage gaps (not a PASS until verified):", json.dumps(gaps, ensure_ascii=False))
        for note in notes:
            print("Note:", note)
        if not args.execute:
            print("DRY RUN: No data copied, no manifest written, no API/DB/MinerU calls.")
            return 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        records = root / "docs/test/stage16_preset_build" / stamp
        records.mkdir(parents=True, exist_ok=False)
        old_manifest = preset / "manifest.json"
        if old_manifest.is_file():
            shutil.copy2(old_manifest, records / "old_manifest_backup.json")
        copied = 0
        for name, (source, digest) in sources.items():
            copied += bool(install_file(source, preset / name, digest))
        preset.mkdir(parents=True, exist_ok=True)
        # Write outside preset first, then replace manifest atomically (same checkout/volume).
        next_manifest = records / "generated_manifest.json"
        next_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # If preset changes unexpectedly in the interim, fail rather than advertise a stale manifest.
        if set(scan_preset(preset)) != set(names):
            raise BuildError("Unexpected files appeared in preset; manifest was not replaced")
        os.replace(next_manifest, old_manifest)
        (records / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Optional starting point for a HUMAN-CONFIRMED --tags-file. This is deliberately
        # not applied to the actual manifest and not placed in the formal preset folder.
        proposals = {item["file"]: item["current_tags"] or item["candidate_from_official_ohr_qa"]
                     for item in review["candidate_entries"]
                     if item["current_tags"] or item["candidate_from_official_ohr_qa"]}
        (records / "tag_candidates_NOT_APPROVED.json").write_text(
            json.dumps(proposals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Generated: {old_manifest}")
        print(f"Added files: {copied}; reused existing: {len(names)-copied}")
        print(f"Review notes: {records / 'review.json'}")
        print(f"Unapproved tag proposals: {records / 'tag_candidates_NOT_APPROVED.json'}")
        print("Only local copying was performed. Do NOT run --allow-cloud before rights and labels are confirmed.")
        return 0
    except (BuildError, FileNotFoundError, OSError, KeyError, ValueError, TypeError) as error:
        print(f"PRESET_BUILD_STOP: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
