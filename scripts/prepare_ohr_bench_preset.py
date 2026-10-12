"""用户手动运行的 OHR-Bench 离线抽样：真实身份/QA/ZIP 校验，不执行解析业务或云调用。"""

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "docs/test/ohr_bench_preset"
RESULT_ROOT = TEST_ROOT / "results"
PRESET_ROOT = PROJECT_ROOT / "data/knowledge_base/preset"
MIB = 1024 ** 2
VERSION = "ohr-selection-v1"
DOMAINS = {"finance", "academic", "textbook", "law", "manual", "newspaper", "administration"}
EVIDENCE = {"plain_text", "table", "formula", "chart", "reading_order"}
SOURCES = {
    "github": "https://github.com/opendatalab/OHR-Bench",
    "dataset": "https://huggingface.co/datasets/opendatalab/OHR-Bench",
    "pdf_zip": "https://huggingface.co/datasets/opendatalab/OHR-Bench/resolve/main/pdfs.zip",
    "qa": "https://raw.githubusercontent.com/opendatalab/OHR-Bench/main/data/qas_v2.json",
    "page_convention": "https://github.com/opendatalab/OHR-Bench/blob/main/src/retrievers/custom.py",
}
ERRORS = {
    "OHR_SOURCE": "需确认本地文件来自官方来源；来源文件缺失、改变或不符合已冻结身份",
    "OHR_PATH": "路径不安全、含链接/Junction、越界或与固定目录规范不符",
    "OHR_QA": "官方 QA 结构不可用，请核对下载文件和字段版本",
    "OHR_ZIP": "ZIP 目录不安全、损坏、重复或包含不支持的容器项",
    "OHR_PDF": "PDF 结构/页码/大小校验失败，未认定为有效样本",
    "OHR_DEPENDENCY": "缺少已有 PyMuPDF 依赖，请由用户手动补齐",
    "OHR_LIMIT": "资源限制或参数无效，不扩大预算或跳过限制",
    "OHR_PLAN": "抽样计划、引用或校验和不一致，请先重新预览并人工审核",
    "OHR_EXISTS": "目标文件已存在，禁止覆盖；请人工归档旧结果或使用新的结果子目录",
    "OHR_GAPS": "抽样存在数量/证据/领域缺口，需人工确认后显式接受缺口",
    "OHR_PERMISSION": "提取前需确认本地研究使用权限；这不构成云外传授权",
    "OHR_IO": "读写未完成，请人工核对结果与已创建文件，不覆盖或删除用户文件",
}


class PreparationError(Exception):
    """只输出固定安全错误码，不输出 QA 正文、原始路径或底层异常详情。"""

    def __init__(self, code):
        """未知类别归为 IO 错误，禁止把原始异常原文放进终端。"""

        self.code = code if code in ERRORS else "OHR_IO"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class Limits:
    """冻结离线预检/抽样预算，不代表 MinerU 已扣费或 Native 已完整解析。"""

    count: int = 7
    seed: int = 42
    max_file_bytes: int = 20 * MIB
    max_pages: int = 50
    max_total_pages: int = 200
    max_inspect_bytes: int = 256 * MIB
    max_inspect_files: int = 64
    qas_per_type: int = 3

    def validate(self):
        """默认选择 6～8 份，限制单文件/页数、累计检查量和每类 QA 数，避免无界读取。"""

        if (self.count not in {6, 7, 8} or type(self.seed) is not int
                or not 0 < self.max_file_bytes <= 200 * MIB
                or not 1 <= self.max_pages <= 200 or not 1 <= self.max_total_pages <= 1000
                or not 1 <= self.max_inspect_files <= 128
                or not self.max_file_bytes <= self.max_inspect_bytes <= 512 * MIB
                or not 1 <= self.qas_per_type <= 10):
            raise PreparationError("OHR_LIMIT")


def encoded(value):
    """使用稳定 JSON 编码冻结参数/身份，禁用 NaN，不包含运行时间或随机临时路径。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    """计算结构摘要，仅用于本地一致性，不将 Hash 宣称为官方签名。"""

    return hashlib.sha256(encoded(value)).hexdigest()


def safe_name(name, *, directory=False):
    """校验 ZIP/QA/目标相对路径，拒绝跨平台跳转、控制符、Windows 特殊文件名和歧义。"""

    if not isinstance(name, str) or not name or len(name) > 512 or "\\" in name:
        raise PreparationError("OHR_PATH")
    cleaned = name[:-1] if directory and name.endswith("/") else name
    path = PurePosixPath(cleaned)
    if (not path.parts or path.is_absolute() or path.as_posix() != cleaned
            or any(part in {".", ".."} or len(part) > 255 or part.endswith((" ", "."))
                   or re.search(r'[<>:"|?*\x00-\x1f\x7f]', part)
                   or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                       "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
                       "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"} for part in path.parts)):
        raise PreparationError("OHR_PATH")
    return path


def plain_path(path, *, beneath=None):
    """拒绝已有路径任一祖先的链接/Junction；目标必须位于指定真实目录下。"""

    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise PreparationError("OHR_PATH")
    resolved = path.resolve()
    if beneath is not None and not resolved.is_relative_to(Path(beneath).resolve()):
        raise PreparationError("OHR_PATH")
    return resolved


def qa_rows(value):
    """读取官方字段；保留问题/答案原值，拒绝非法或重复 ID，页码为官方 0-based int/list。"""

    if not isinstance(value, list) or not 1 <= len(value) <= 100000:
        raise PreparationError("OHR_QA")
    groups, rejected = defaultdict(list), Counter()
    ids = Counter(row.get("ID") for row in value if isinstance(row, dict) and isinstance(row.get("ID"), str))
    for row in value:
        try:
            if not isinstance(row, dict):
                raise ValueError
            required = ("doc_name", "ID", "questions", "answers", "doc_type", "evidence_source")
            if any(not isinstance(row.get(key), str) or not row[key].strip() for key in required):
                raise ValueError
            if ids[row["ID"]] != 1 or len(row["ID"]) > 256:
                rejected["duplicate_or_invalid_id"] += 1
                continue
            identity = safe_name(row["doc_name"])
            domain = row["doc_type"].casefold()
            if (domain not in DOMAINS or len(identity.parts) < 2
                    or identity.parts[0] != domain or row["doc_name"].lower().endswith(".pdf")):
                rejected["ambiguous_source_identity"] += 1
                continue
            category = row["evidence_source"].strip().casefold().replace(" ", "_")
            if category not in EVIDENCE:
                rejected["unknown_evidence_source"] += 1
                continue
            pages = row.get("evidence_page_no")
            pages = pages if isinstance(pages, list) else [pages]
            if not pages or len(pages) > 200 or any(type(page) is not int or page < 0 for page in pages):
                rejected["invalid_evidence_pages"] += 1
                continue
            groups[row["doc_name"]].append({"original": row, "category": category,
                                           "pages": tuple(pages), "domain": domain})
        except (ValueError, TypeError, PreparationError):
            rejected["invalid_qa_schema"] += 1
    for rows in groups.values():
        rows.sort(key=qa_identity)
    return dict(sorted(groups.items())), dict(sorted(rejected.items()))


def qa_identity(row):
    """按真实官方 ID 排序，输入 QA 数组顺序不会影响选择。"""

    return row["original"]["ID"]


def archive_index(archive):
    """只读取中央目录，不解压；全局拒绝跳转/链接/特殊类型/加密/重复及大小写冲突。"""

    infos = archive.infolist()
    if len(infos) > 50000:
        raise PreparationError("OHR_ZIP")
    names, pdfs, descriptors = set(), {}, []
    for info in infos:
        path = safe_name(info.filename, directory=info.is_dir())
        folded = path.as_posix().casefold()
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (folded in names or info.flag_bits & 1
                or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                or bool(info.external_attr & 0x400)):
            raise PreparationError("OHR_ZIP")
        names.add(folded)
        descriptors.append({"name": info.filename, "size": info.file_size, "compressed": info.compress_size,
                            "crc": info.CRC, "offset": info.header_offset, "attr": info.external_attr})
        if not info.is_dir() and path.suffix.lower() == ".pdf":
            pdfs[info.filename] = info
    descriptors.sort(key=descriptor_name)
    return dict(sorted(pdfs.items())), digest(descriptors)


def descriptor_name(value):
    """中央目录身份按原始路径排序，不把成员排序误当 PDF 阅读顺序。"""

    return value["name"]


def candidates(groups, pdfs, limits, domains):
    """按 domain/文档完整身份做唯一路径后缀匹配，不用 basename 猜来源或固定 ZIP 顶层。"""

    result, rejected = [], []
    for name, rows in groups.items():
        if rows[0]["domain"] not in domains:
            continue
        expected = name + ".pdf"
        matches = [member for member in pdfs if member == expected or member.endswith("/" + expected)]
        reason = None
        if len(matches) != 1:
            reason = "missing_pdf" if not matches else "ambiguous_pdf_path"
        else:
            info = pdfs[matches[0]]
            if not 0 < info.file_size <= limits.max_file_bytes:
                reason = "file_size_limit"
            elif info.file_size / max(1, info.compress_size) > 200:
                reason = "compression_ratio_limit"
            else:
                eligible = [row for row in rows if max(row["pages"]) < limits.max_pages]
                if not eligible:
                    reason = "qa_page_lower_bound_exceeds_limit"
                else:
                    result.append({"doc_name": name, "domain": rows[0]["domain"],
                        "zip_member": matches[0], "size_bytes": info.file_size,
                        "crc32": info.CRC, "qas": eligible})
        if reason:
            rejected.append({"doc_name": name, "reason": reason})
    return result, rejected


def read_member(archive, member, limit):
    """分块读取单个真实 PDF 到受限内存并触发 ZIP CRC 校验，不 extractall 或写样本文件。"""

    info = archive.getinfo(member)
    if not 0 < info.file_size <= limit:
        raise PreparationError("OHR_LIMIT")
    chunks, total = [], 0
    with archive.open(info) as stream:
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > limit or total > info.file_size:
                raise PreparationError("OHR_LIMIT")
            chunks.append(chunk)
    if total != info.file_size:
        raise PreparationError("OHR_ZIP")
    return b"".join(chunks)


def pdf_metadata(data, max_pages):
    """只检查真实 PDF 结构与页树，不抽正文、不渲染、不调用项目 Parser/OCR。

    拒绝加密、被修复、空页树或超限；不保证所有字体/内容对象可解析或文件无恶意内容。
    """

    try:
        import pymupdf
    except ImportError:
        raise PreparationError("OHR_DEPENDENCY") from None
    try:
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise PreparationError("OHR_PDF")
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or document.is_repaired or document.page_count < 1:
                raise PreparationError("OHR_PDF")
            if document.page_count > max_pages:
                raise PreparationError("OHR_LIMIT")
            for index in range(document.page_count):
                rect = document.load_page(index).rect
                if not all(math.isfinite(value) for value in rect) or rect.width <= 0 or rect.height <= 0:
                    raise PreparationError("OHR_PDF")
            return document.page_count
    except PreparationError:
        raise
    except Exception:
        raise PreparationError("OHR_PDF") from None


def keep_qas(rows, limits):
    """每份文档每种证据保留若干真实 QA，固定种子打破平局，不生成或改写问答。"""

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)

    def rank(row):
        """使用种子与真实 QA ID 的摘要排序，跨进程不依赖 Python 随机 Hash。"""

        return digest([limits.seed, row["original"]["ID"]]), row["original"]["ID"]

    kept = []
    for category in sorted(grouped):
        kept.extend(sorted(grouped[category], key=rank)[:limits.qas_per_type])
    return sorted(kept, key=qa_identity)


def select_documents(pool, archive, limits, *, inspector=pdf_metadata, reader=read_member):
    """确定性分层贪心：优先补齐五类 QA/多个问题和新领域，再偏好小页/小文件。

    仅检查少量候选的内存 PDF，坏文件/重复 SHA/无有效 QA 不入选；预算耗尽报告缺口。
    """

    remaining = sorted(pool, key=descriptor_name_doc)
    selected, rejected, hashes = [], [], set()
    counts, seen_domains = Counter(), set()
    spent_bytes = inspected = pages_total = 0
    while remaining and len(selected) < limits.count and inspected < limits.max_inspect_files:
        def rank(item):
            """缺口优先、小成本分桶、种子/真实身份平局排序，不依赖输入 ZIP/QA 顺序。"""

            available = Counter(row["category"] for row in item["qas"])
            new = sum(counts[key] == 0 for key in available)
            gain = sum(min(max(0, 2 - counts[key]), count) for key, count in available.items())
            lower_bound = max(max(row["pages"]) for row in item["qas"]) + 1
            return (-new, -(item["domain"] not in seen_domains), -gain, lower_bound // 10,
                    item["size_bytes"] // MIB, digest([limits.seed, item["doc_name"]]), item["doc_name"])

        candidate = min(remaining, key=rank)
        remaining.remove(candidate)
        if spent_bytes + candidate["size_bytes"] > limits.max_inspect_bytes:
            rejected.append({"doc_name": candidate["doc_name"], "reason": "inspection_bytes_budget"})
            continue
        inspected += 1
        spent_bytes += candidate["size_bytes"]
        try:
            data = reader(archive, candidate["zip_member"], limits.max_file_bytes)
            sha = hashlib.sha256(data).hexdigest()
            if sha in hashes:
                rejected.append({"doc_name": candidate["doc_name"], "reason": "duplicate_pdf_sha256"})
                continue
            page_count = inspector(data, limits.max_pages)
            qas = keep_qas([row for row in candidate["qas"] if max(row["pages"]) < page_count], limits)
            if not qas:
                rejected.append({"doc_name": candidate["doc_name"], "reason": "no_qa_within_real_pdf_pages"})
                continue
            if pages_total + page_count > limits.max_total_pages:
                rejected.append({"doc_name": candidate["doc_name"], "reason": "total_pages_budget"})
                continue
        except PreparationError as exc:
            if exc.code == "OHR_DEPENDENCY":
                raise
            rejected.append({"doc_name": candidate["doc_name"], "reason": exc.code})
            continue
        except (BadZipFile, OSError, RuntimeError, NotImplementedError):
            rejected.append({"doc_name": candidate["doc_name"], "reason": "OHR_ZIP"})
            continue
        item = {key: value for key, value in candidate.items() if key != "qas"}
        item.update(sha256=sha, page_count=page_count, qas=qas,
                    destination_file="ohr_bench/" + candidate["doc_name"] + ".pdf")
        selected.append(item)
        hashes.add(sha)
        pages_total += page_count
        seen_domains.add(item["domain"])
        counts.update(row["category"] for row in qas)
    return selected, rejected, {"inspected_files": inspected, "inspected_bytes": spent_bytes}


def descriptor_name_doc(value):
    """按官方完整文档身份排序，不按本地文件名猜领域或内容类别。"""

    return value["doc_name"]


def build_artifacts(selected, source, limits, domains, candidates_count, rejected, qa_rejected, inspection,
                    *, inventory=None):
    """发布真实 QA 参考和身份计划；已验证覆盖仅指官方 QA/结构，不宣称解析质量通过。"""

    references, documents = [], []
    counts, domain_counts = Counter(), Counter()
    for item in selected:
        rows = item["qas"]
        counts.update(row["category"] for row in rows)
        domain_counts[item["domain"]] += 1
        document = {key: value for key, value in item.items() if key != "qas"}
        document["qa_ids"] = [row["original"]["ID"] for row in rows]
        document["qa_sha256"] = [digest(row["original"]) for row in rows]
        document["qa_evidence_categories"] = sorted({row["category"] for row in rows})
        # 初始 tags 必须由人填写；不把 QA Plain Text 当成 text_pdf，更不猜 scan_pdf/mixed。
        document["manifest_candidate"] = {"file": document["destination_file"], "tags": []}
        documents.append(document)
        for row in rows:
            references.append(row["original"] | {"_ohr_reference": {
                "source_qa_sha256": source["qa_sha256"], "document_sha256": item["sha256"],
                "zip_member": item["zip_member"], "preset_file": item["destination_file"],
                "evidence_page_idx_0based": list(row["pages"]),
                "evidence_pages_1based": [page + 1 for page in row["pages"]],
                "category": row["category"]}})
    missing = {
        "document_count_shortfall": max(0, limits.count - len(documents)),
        "missing_evidence_types": sorted(EVIDENCE - set(counts)),
        "evidence_types_with_fewer_than_two_qas": sorted(key for key in EVIDENCE if counts[key] < 2),
        "domain_shortfall": max(0, min(limits.count, len(domains)) - len(domain_counts)),
    }
    references.sort(key=original_id)
    reference_bytes = b"".join(encoded(row) + b"\n" for row in references)
    payload = {"version": VERSION, "sources": source, "limits": vars(limits), "domains": sorted(domains),
               "candidate_inventory": inventory or [],
               "documents": documents, "gaps": missing, "qa_reference_sha256": hashlib.sha256(reference_bytes).hexdigest()}
    plan = payload | {"plan_sha256": digest(payload)}
    report = {"version": VERSION, "plan_sha256": plan["plan_sha256"], "candidates_count": candidates_count,
        "selected_count": len(documents), "gaps": missing, "inspection": inspection,
        "verified_coverage": {"official_qa_evidence_counts": dict(sorted(counts.items())),
                              "domains": dict(sorted(domain_counts.items())),
                              "pdf_structure_crc_sha_and_qa_page_ranges": bool(documents)},
        "documents": documents, "rejected_candidates": rejected, "rejected_qa_counts": qa_rejected,
        "not_verified": ["text_pdf/scan_pdf/mixed 标签", "表格/公式/图表/多栏真实视觉内容",
                         "Parser/OCR 阅读顺序及质量", "版权与云外传权限", "READY", "问答准确率"],
        "estimated_cloud_upper_bound": {"source_files": len(documents),
            "source_pages": sum(item["page_count"] for item in documents), "actual_cloud_calls": 0}}
    return plan, report, reference_bytes


def original_id(row):
    """输出按原始官方问题 ID 稳定排序，问题和答案内容原样保留在本地参考文件。"""

    return row["ID"]


def inventory_entry(item):
    """候选清单只含真实目录/QA 分类元数据；未检查候选不伪造页数或文件 SHA。"""

    return {"doc_name": item["doc_name"], "domain": item["domain"], "zip_member": item["zip_member"],
            "size_bytes": item["size_bytes"], "qa_count": len(item["qas"]),
            "qa_evidence_counts": dict(sorted(Counter(row["category"] for row in item["qas"]).items())),
            "minimum_pages_implied_by_qa": max(max(row["pages"]) for row in item["qas"]) + 1,
            "actual_pdf_page_count": None, "sha256": None}


def load_source(qa_path, zip_path):
    """读取用户已下载 QA，记录 SHA；ZIP 只在后续读取目录及少量候选，不散落复制源文件。"""

    qa_path, zip_path = plain_path(qa_path), plain_path(zip_path)
    if not qa_path.is_file() or not zip_path.is_file() or not 0 < qa_path.stat().st_size <= 32 * MIB:
        raise PreparationError("OHR_SOURCE")
    qa_bytes = qa_path.read_bytes()
    try:
        value = json.loads(qa_bytes.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise PreparationError("OHR_QA") from None
    groups, rejected = qa_rows(value)
    observed = sorted({row["evidence_source"] for row in value
                       if isinstance(row, dict) and isinstance(row.get("evidence_source"), str)
                       and len(row["evidence_source"]) <= 128})
    return groups, rejected, {"official_urls": SOURCES, "official_source_confirmed_by_user": True,
        "origin_verification": "user_declared_download; local hashes are not official signatures",
        "qa_path": str(qa_path), "zip_path": str(zip_path),
        "qa_sha256": hashlib.sha256(qa_bytes).hexdigest(), "zip_size_bytes": zip_path.stat().st_size,
        "evidence_page_base": 0, "observed_qa_evidence_source_values": observed}


def publish_exclusive(target, data):
    """先写唯一临时文件，再用独占硬链接发布完整文件；任何既有目标（包括同内容）都不覆盖。

    Windows NTFS/本地文件系统需支持硬链接；不支持时安全失败，不降级为覆盖式 rename。
    只清理本函数创建的随机临时文件，不删除用户目标或递归删除目录。
    """

    target = plain_path(target)
    if target.exists():
        raise PreparationError("OHR_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    plain_path(target.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".ohr-publish-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # os.link 不覆盖已有目标，防止预检后用户/其他进程创建同名文件的竞争。
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        raise PreparationError("OHR_EXISTS") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def preview(qa_path, zip_path, results, limits, domains):
    """只在用户执行时生成三份本地报告；不落地 PDF，不修改业务 manifest。

    少量候选会在内存中读取完整字节以核对 CRC/SHA 和页数，绝不解压整个 ZIP。
    """

    limits.validate()
    if not domains or not set(domains) <= DOMAINS:
        raise PreparationError("OHR_LIMIT")
    results = plain_path(results, beneath=RESULT_ROOT)
    targets = [results / name for name in ("selection_plan.json", "selection_report.json", "qa_reference.jsonl")]
    if any(target.exists() or target.is_symlink() for target in targets):
        raise PreparationError("OHR_EXISTS")
    groups, qa_rejected, source = load_source(qa_path, zip_path)
    with ZipFile(zip_path) as archive:
        pdfs, fingerprint = archive_index(archive)
        pool, rejected = candidates(groups, pdfs, limits, domains)
        source["zip_directory_sha256"] = fingerprint
        source["observed_zip_pdf_top_components"] = sorted({PurePosixPath(name).parts[0] for name in pdfs})
        selected, failures, inspection = select_documents(pool, archive, limits)
    plan, report, references = build_artifacts(selected, source, limits, domains, len(pool),
                                               rejected + failures, qa_rejected, inspection,
                                               inventory=[inventory_entry(item) for item in pool])
    if len(references) > 32 * MIB or len(encoded(plan)) > 16 * MIB or len(encoded(report)) > 16 * MIB:
        raise PreparationError("OHR_LIMIT")
    for target, data in zip(targets, (encoded(plan) + b"\n", encoded(report) + b"\n", references)):
        publish_exclusive(target, data)
    return report


def checked_plan(value):
    """校验冻结计划摘要和必要字段；摘要仅防意外变化，不证明数据授权或抵御同用户篡改。"""

    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise PreparationError("OHR_PLAN")
    payload = {key: data for key, data in value.items() if key != "plan_sha256"}
    if value.get("plan_sha256") != digest(payload):
        raise PreparationError("OHR_PLAN")
    try:
        limits = Limits(**value["limits"])
        limits.validate()
        source = value["sources"]
        if (source["official_urls"] != SOURCES or source["official_source_confirmed_by_user"] is not True
                or source["evidence_page_base"] != 0 or not isinstance(value["documents"], list)
                or not 0 < len(value["documents"]) <= limits.count
                or not isinstance(value["gaps"], dict)):
            raise PreparationError("OHR_PLAN")
        identities, hashes, destinations = set(), set(), set()
        for item in value["documents"]:
            identity = safe_name(item["doc_name"])
            safe_name(item["zip_member"])
            destination = "ohr_bench/" + item["doc_name"] + ".pdf"
            if (identity.parts[0] != item["domain"] or item["domain"] not in DOMAINS
                    or item["destination_file"] != destination or item["doc_name"] in identities
                    or item["sha256"] in hashes or destination.casefold() in destinations
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                    or type(item["page_count"]) is not int or not 1 <= item["page_count"] <= limits.max_pages
                    or type(item["size_bytes"]) is not int or not 0 < item["size_bytes"] <= limits.max_file_bytes
                    or not isinstance(item["qa_ids"], list) or not item["qa_ids"]
                    or len(item["qa_ids"]) != len(set(item["qa_ids"]))):
                raise PreparationError("OHR_PLAN")
            safe_name(destination)
            identities.add(item["doc_name"])
            hashes.add(item["sha256"])
            destinations.add(destination.casefold())
        if sum(item["page_count"] for item in value["documents"]) > limits.max_total_pages:
            raise PreparationError("OHR_PLAN")
    except (KeyError, TypeError, ValueError):
        raise PreparationError("OHR_PLAN") from None
    return limits


def has_gaps(gaps):
    """仅根据真实数量/QA/领域缺口判断是否需要用户显式接受，未知字段不补造覆盖。"""

    return any(bool(value) for value in gaps.values())


def extract(plan_file, *, accept_gaps=False):
    """用户确认后只复制冻结计划中的真实 PDF；先验证整批身份/QA/路径，再独占发布。

    不覆盖任何现有 PDF/报告/manifest；竞争或磁盘失败时可能已有少数完整 PDF 发布，
    需用户核对，不自动删除、续传、改名或复制重复版本。
    """

    plan_file = plain_path(plan_file, beneath=RESULT_ROOT)
    if not plan_file.is_file() or plan_file.stat().st_size > 16 * MIB:
        raise PreparationError("OHR_PLAN")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    limits = checked_plan(plan)
    if has_gaps(plan["gaps"]) and not accept_gaps:
        raise PreparationError("OHR_GAPS")
    qa_path, zip_path = Path(plan["sources"]["qa_path"]), Path(plan["sources"]["zip_path"])
    groups, _, actual = load_source(qa_path, zip_path)
    if actual != {key: value for key, value in plan["sources"].items()
                  if key not in {"zip_directory_sha256", "observed_zip_pdf_top_components"}}:
        raise PreparationError("OHR_SOURCE")
    reference = plain_path(plan_file.parent / "qa_reference.jsonl", beneath=RESULT_ROOT)
    if not reference.is_file() or reference.stat().st_size > 32 * MIB:
        raise PreparationError("OHR_PLAN")
    if hashlib.sha256(reference.read_bytes()).hexdigest() != plan["qa_reference_sha256"]:
        raise PreparationError("OHR_PLAN")
    # 应用配置可能比预览时更严格；提取不能用旧计划绕过当前上传大小保护。
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from app.core.config import get_settings

    if any(item["size_bytes"] > get_settings().max_upload_size_mb * MIB for item in plan["documents"]):
        raise PreparationError("OHR_LIMIT")
    targets = [plain_path(PRESET_ROOT / item["destination_file"], beneath=PRESET_ROOT) for item in plan["documents"]]
    if any(target.exists() or target.is_symlink() for target in targets):
        raise PreparationError("OHR_EXISTS")
    buffers, bytes_total = [], 0
    with ZipFile(zip_path) as archive:
        pdfs, fingerprint = archive_index(archive)
        if fingerprint != plan["sources"]["zip_directory_sha256"]:
            raise PreparationError("OHR_SOURCE")
        for item in plan["documents"]:
            rows = {row["original"]["ID"]: row for row in groups.get(item["doc_name"], [])}
            if (not item["qa_ids"] or len(item["qa_ids"]) != len(item["qa_sha256"])
                    or any(identifier not in rows or digest(rows[identifier]["original"]) != expected
                           for identifier, expected in zip(item["qa_ids"], item["qa_sha256"]))):
                raise PreparationError("OHR_PLAN")
            info = pdfs.get(item["zip_member"])
            expected = item["doc_name"] + ".pdf"
            matches = [name for name in pdfs if name == expected or name.endswith("/" + expected)]
            if (matches != [item["zip_member"]] or info is None
                    or info.file_size != item["size_bytes"] or info.CRC != item["crc32"]):
                raise PreparationError("OHR_SOURCE")
            bytes_total += item["size_bytes"]
            if bytes_total > limits.max_inspect_bytes:
                raise PreparationError("OHR_LIMIT")
            data = read_member(archive, item["zip_member"], limits.max_file_bytes)
            pages = pdf_metadata(data, limits.max_pages)
            if (hashlib.sha256(data).hexdigest() != item["sha256"] or pages != item["page_count"]
                    or any(max(rows[identifier]["pages"]) >= pages for identifier in item["qa_ids"])):
                raise PreparationError("OHR_SOURCE")
            buffers.append(data)
    for target, data in zip(targets, buffers):
        publish_exclusive(target, data)
    return len(targets)


def build_parser():
    """仅提供离线预览/显式提取选项，不提供下载、云上传、数据库或 Parser 模式。"""

    parser = argparse.ArgumentParser(description="OHR-Bench 离线抽样；默认仅生成本地候选报告，不落地 PDF")
    parser.add_argument("--extract", action="store_true", help="审核冻结计划后，提取少量真实 PDF")
    parser.add_argument("--confirm-official-source", action="store_true", help="人工确认 source 中的文件来自官方 URL")
    parser.add_argument("--confirm-local-use", action="store_true", help="确认本地研究使用权限；不代表允许云外传")
    parser.add_argument("--accept-gaps", action="store_true", help="已审核并接受真实抽样缺口，不伪造覆盖")
    parser.add_argument("--results-dir", type=Path, default=RESULT_ROOT, help="固定 results 目录或其子目录，不覆盖旧报告")
    parser.add_argument("--plan-file", type=Path, default=RESULT_ROOT / "selection_plan.json")
    parser.add_argument("--count", type=int, choices=[6, 7, 8], default=7)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-mb", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-total-pages", type=int, default=200)
    parser.add_argument("--max-inspect-mb", type=int, default=256)
    parser.add_argument("--qas-per-type", type=int, default=3)
    parser.add_argument("--domains", nargs="+", choices=sorted(DOMAINS), default=sorted(DOMAINS))
    return parser


def main(argv=None):
    """用户手动入口，只打印数量/固定安全提示，不输出问题答案、原始路径或异常堆栈。"""

    args = build_parser().parse_args(argv)
    try:
        if not args.confirm_official_source:
            raise PreparationError("OHR_SOURCE")
        if args.extract:
            if not args.confirm_local_use:
                raise PreparationError("OHR_PERMISSION")
            count = extract(args.plan_file, accept_gaps=args.accept_gaps)
            print(f"提取完成：真实 PDF 数={count}；未修改 manifest、报告或其他素材，未调用任何云服务。")
            return 0
        # 仅读取应用大小保护值，配置导入不会建立数据库/Redis/模型连接。
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from app.core.config import get_settings

        configured_bytes = get_settings().max_upload_size_mb * MIB
        requested_bytes = args.max_mb * MIB
        if requested_bytes <= 0:
            raise PreparationError("OHR_LIMIT")
        limits = Limits(count=args.count, seed=args.seed,
            max_file_bytes=min(requested_bytes, configured_bytes, 200 * MIB), max_pages=args.max_pages,
            max_total_pages=args.max_total_pages, max_inspect_bytes=args.max_inspect_mb * MIB,
            qas_per_type=args.qas_per_type)
        report = preview(TEST_ROOT / "source/qas_v2.json", TEST_ROOT / "source/pdfs.zip",
                         args.results_dir, limits, set(args.domains))
        print(f"候选报告完成：候选={report['candidates_count']} 选中={report['selected_count']}；"
              "仅内存结构/SHA/证据页校验，未将 PDF 放入预置目录。")
        print("请人工核对五类 QA、领域、版权、PDF 视觉内容及 manifest 标签；未验证解析质量或 READY。")
        return 2 if has_gaps(report["gaps"]) else 0
    except PreparationError as exc:
        print(str(exc))
        return 1
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, RuntimeError, NotImplementedError):
        print("OHR_IO：离线准备未完成，请核对输入/报告与已创建文件；不输出原始异常。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
