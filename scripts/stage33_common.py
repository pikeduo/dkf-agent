"""阶段33冻结输入校验、可复现QA抽样及本地报告；不解析正文或连接服务。"""

import csv
import hashlib
import io
import json
import os
import stat
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import NAMESPACE_URL, UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/dataset/stage32_run_01"
VERSION = "stage33-full-v1"
COUNTS = {"ohr": (100, 3588), "ocb": (100, 499)}
STRATEGIES = ("dense", "bm25", "hybrid", "hybrid_reranker")


class Stage33Error(ValueError):
    """只传播内部固定错误类别，不展示问题、正文、凭据或上游异常原文。"""

    def __init__(self, code):
        """保存本工具内部定义的安全分类。"""
        self.code = code
        super().__init__(f"STAGE33_{code}")


def encoded(value):
    """严格JSON用于冻结指纹及报告，不容许NaN/Infinity。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def digest(value):
    """对结构化身份生成可重现指纹，不把答案用于检索策略。"""
    return hashlib.sha256(encoded(value)).hexdigest()


def sha_file(path):
    """流式读取已有文件的SHA256，不改写或解析原文。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_path(path, beneath):
    """检查原始路径每级链接/junction及绝对边界，不允许跳出指定输入/输出根。"""
    path, beneath = Path(os.path.abspath(path)), Path(os.path.abspath(beneath))
    if not path.is_relative_to(beneath):
        raise Stage33Error("PATH")
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise Stage33Error("PATH")
    return path


def read_json(path):
    """读取本地JSON；禁止非有限JSON常量。"""
    def reject(value):
        """拒绝JSON标准之外的数字，不回显输入值。"""
        raise Stage33Error("JSON")
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject)


def write_json(path, value):
    """仅更新当前运行目录的检查点；复用同目录原子发布，不触碰冻结输入。"""
    from app.services.parse_cache import atomic_write
    atomic_write(path, encoded(value) + b"\n")


def write_csv(path, rows, fields):
    """UTF-8标准CSV；结构化列严格JSON编码，报告不进入Git。"""
    from app.services.parse_cache import atomic_write
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False)
                         if isinstance(value, (dict, list)) else value for key, value in row.items()})
    atomic_write(path, stream.getvalue().encode("utf-8"))


def publish_stream(path, stream):
    """按块发布大型检索明细，避免全部Top-K结果驻留RAM；只清理本函数创建的临时文件。"""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream.seek(0)
        with NamedTemporaryFile(dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            while block := stream.read(1024**2):
                target.write(block.encode("utf-8") if isinstance(block, str) else block)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_dataset(root=DATASET, *, counts=COUNTS, verify_files=True):
    """全量核对200文件/4087QA、原字段、依赖、页索引、双向映射及Hash；无前N参数。"""
    from app.core.file_formats import FILE_TYPES, FileFormatError, validate_file_content

    root = plain_path(root, ROOT / "data/dataset")
    documents, questions, metadata_hashes = [], [], {}
    for dataset, (file_count, qa_count) in counts.items():
        manifest = plain_path(root / f"manifests/{dataset}_documents.csv", root)
        qa_path = plain_path(root / f"qa/{dataset}_qa.jsonl", root)
        with manifest.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        with qa_path.open(encoding="utf-8-sig") as stream:
            qas = [json.loads(line) for line in stream if line.strip()]
        if len(rows) != file_count or len(qas) != qa_count:
            raise Stage33Error("FROZEN_COUNTS")
        if any(row["dataset"] != dataset for row in rows + qas):
            raise Stage33Error("DATASET_IDENTITY")
        documents.extend(rows)
        questions.extend(qas)
        metadata_hashes[manifest.relative_to(root).as_posix()] = sha_file(manifest)
        metadata_hashes[qa_path.relative_to(root).as_posix()] = sha_file(qa_path)
    by_id = {row["file_id"]: row for row in documents}
    if (len(by_id) != len(documents) or len({row["sha256"] for row in documents}) != len(documents)
            or len({(row["dataset"], row["source_identity"]) for row in documents}) != len(documents)
            or len({row["file_path"].casefold() for row in documents}) != len(documents)
            or len({(q["dataset"], q["qa_id"]) for q in questions}) != len(questions)):
        raise Stage33Error("DUPLICATE_IDENTITY")
    file_issues = {}
    for row in documents:
        identity = str(uuid5(NAMESPACE_URL, f"dkf:stage32:{row['dataset']}:{row['source_identity']}:{row['sha256']}"))
        if row["file_id"] != identity or row["file_type"] not in FILE_TYPES:
            raise Stage33Error("FILE_IDENTITY")
        try:
            path = plain_path(ROOT / row["file_path"], root / "documents" / row["dataset"])
            if (not path.is_file() or path.suffix.lower() != "." + row["file_type"]
                    or path.stat().st_size != int(row["size_bytes"])):
                raise Stage33Error("FILE_MISSING_OR_SIZE")
            if verify_files:
                if sha_file(path) != row["sha256"]:
                    raise Stage33Error("FILE_HASH")
                with path.open("rb") as stream:
                    validate_file_content(stream, row["file_type"])
        except (Stage33Error, FileFormatError, OSError) as error:
            file_issues[row["file_id"]] = error.code if isinstance(error, Stage33Error) else (
                "FILE_FORMAT" if isinstance(error, FileFormatError) else "FILE_IO")
    actual = {p.relative_to(ROOT).as_posix().casefold() for p in (root / "documents").rglob("*") if p.is_file()}
    if actual - {row["file_path"].casefold() for row in documents}:
        raise Stage33Error("DOCUMENT_SET")
    seen, links = Counter(), set()
    for qa in questions:
        original, ids = qa["original"], qa["source_file_ids"]
        if (not ids or len(ids) != len(set(ids)) or len(ids) != len(qa["source_file_paths"])
                or set(ids) != set(qa["source_file_hashes"]) or not isinstance(qa["question"], str)
                or not qa["question"].strip() or digest(original) != qa["original_record_sha256"]
                or qa["validation_status"] != "validated_dependencies_and_official_basis"):
            raise Stage33Error("QA_IDENTITY")
        identities = []
        for fid, file_path in zip(ids, qa["source_file_paths"], strict=True):
            row = by_id.get(fid)
            if (row is None or row["dataset"] != qa["dataset"] or row["file_path"] != file_path
                    or qa["source_file_hashes"][fid] != row["sha256"]):
                raise Stage33Error("QA_DEPENDENCY")
            identities.append(row["source_identity"])
            links.add((qa["dataset"], qa["qa_id"], fid, file_path, row["sha256"], len(ids)))
            seen[fid] += 1
        for key in ("weights", "track", "reference_files", "evidence_source", "evidence_context", "evidence_page_no"):
            if qa.get(key) != original.get(key):
                raise Stage33Error("OFFICIAL_FIELDS")
        if qa["dataset"] == "ohr":
            pages = original["evidence_page_no"]
            pages = pages if isinstance(pages, list) else [pages]
            if (qa["qa_id"] != original["ID"] or qa["question"] != original["questions"]
                    or qa["expected_answer"] != original["answers"] or identities != [original["doc_name"]]
                    or qa["evidence_page"] != {"official_0based": pages, "system_1based": [p + 1 for p in pages]}
                    or any(type(p) is not int or not 0 <= p < int(by_id[ids[0]]["page_count"]) for p in pages)):
                raise Stage33Error("OHR_PAGES_OR_FIELDS")
        elif (qa["question"] != original["question"] or qa["expected_answer"] is not None
              or qa["expected_assertions"] != original.get("expected_assertions")
              or identities != original.get("reference_files")):
            raise Stage33Error("OCB_FIELDS")
    mapping = root / "manifests/qa_file_mapping.csv"
    with plain_path(mapping, root).open(encoding="utf-8-sig", newline="") as stream:
        disk_links = [(r["dataset"], r["qa_id"], r["file_id"], r["file_path"], r["sha256"], int(r["dependency_count"]))
                      for r in csv.DictReader(stream)]
    if len(disk_links) != len(links) or set(disk_links) != links:
        raise Stage33Error("MAPPING")
    if any(seen[row["file_id"]] != int(row["qa_count"]) or seen[row["file_id"]] < 1 for row in documents):
        raise Stage33Error("ORPHAN_OR_QA_COUNT")
    metadata_hashes["manifests/qa_file_mapping.csv"] = sha_file(mapping)
    return {"documents": documents, "questions": questions, "fingerprint": digest(metadata_hashes),
            "metadata_hashes": metadata_hashes, "root": str(root), "file_issues": file_issues}


def select_questions(data, *, per_dataset=None, seed=33):
    """完整输入校验后按身份Hash固定抽题，保留整个文档库；不按答案、质量或检索成绩挑题。"""
    if per_dataset is None:
        return data
    if (type(per_dataset) is not int or per_dataset < 1
            or type(seed) is not int or not 0 <= seed <= 2**32 - 1):
        raise Stage33Error("QA_SAMPLE_ARGUMENTS")
    pools = {}
    for qa in data["questions"]:
        pools.setdefault(qa["dataset"], []).append(qa)
    if not pools or any(len(pool) < per_dataset for pool in pools.values()):
        raise Stage33Error("QA_SAMPLE_TOO_LARGE")

    def rank(qa):
        """排序只使用种子、数据集和官方QA身份，输入行序及答案均不影响抽样。"""
        return digest(["stage33-qa-sample-v1", seed, qa["dataset"], qa["qa_id"]]), str(qa["qa_id"])

    selected = [qa for dataset in sorted(pools)
                for qa in sorted(pools[dataset], key=rank)[:per_dataset]]
    selection = {"version": "stage33-qa-sample-v1", "method": "sha256_identity_rank",
        "seed": seed, "per_dataset": per_dataset, "parent_dataset_fingerprint": data["fingerprint"],
        "population_counts": {dataset: len(pool) for dataset, pool in sorted(pools.items())},
        "selected_counts": {dataset: per_dataset for dataset in sorted(pools)},
        "qa_ids": [{"dataset": qa["dataset"], "qa_id": qa["qa_id"]} for qa in selected]}
    # 新身份隔离全量/不同种子的run，但原件、QA行及其全部依赖保持原样。
    return data | {"questions": selected, "fingerprint": digest(selection), "qa_selection": selection}


@contextmanager
def run_lock(run):
    """复用系统文件锁，进程退出自动释放；不需要恢复审计框架或清理他人锁。"""
    from app.core.business_logging import lock_file
    run = plain_path(run, ROOT / "results/stage33")
    run.mkdir(parents=True, exist_ok=True)
    with (run / "run.lock").open("a+b") as stream:
        try:
            lock_file(stream.fileno())
        except OSError:
            raise Stage33Error("RUN_BUSY") from None
        try:
            yield
        finally:
            lock_file(stream.fileno(), unlock=True)


def initialize(run, data, kb_id):
    """绑定输入、抽样政策及知识库；保存可复验选题清单，拒绝与其他run或种子混算。"""
    value = {"version": VERSION, "dataset_fingerprint": data["fingerprint"], "kb_id": str(UUID(str(kb_id))),
             "document_count": len(data["documents"]), "qa_count": len(data["questions"])}
    selection = data.get("qa_selection")
    if selection is not None:
        value["qa_selection_fingerprint"] = digest(selection)
    path = run / "run.json"
    if path.exists() and read_json(path) != value:
        raise Stage33Error("RUN_IDENTITY_CHANGED")
    if not path.exists():
        if any(p.name != "run.lock" for p in run.iterdir()):
            raise Stage33Error("OUTPUT_EXISTS")
        write_json(path, value)
    if selection is not None:
        selected_path = run / "qa_selection.json"
        if selected_path.exists() and read_json(selected_path) != selection:
            raise Stage33Error("QA_SELECTION_CHANGED")
        if not selected_path.exists():
            write_json(selected_path, selection)
    return value
