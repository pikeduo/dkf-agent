"""公开基准离线准备的安全边界；复用 OHR 工具，不修改它，也不连接业务服务。"""

import argparse
import hashlib
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from scripts import prepare_ohr_bench_preset as ohr

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024**2
FORMATS = ("pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "txt", "md", "jpg", "jpeg", "png")
REPOS = {"ocb": "microsoft/OfficeComprehensionBenchmark", "omnidocbench": "opendatalab/OmniDocBench"}
ERRORS = {
    "MISSING": "必需本地输入不存在；请先按 PRESET_IMPORT 的手动下载步骤准备，不自动下载",
    "EMPTY": "本地输入为空；请人工检查下载是否完整，不覆盖原文件或跳过校验",
    "SOURCE": "官方元数据、版本、身份或关联不可用，请人工核对来源",
    "SCHEMA": "输入字段或映射不符合已声明契约，不猜测缺失数据",
    "PATH": "路径越界、链接、压缩成员或身份歧义，已安全拒绝",
    "LIMIT": "文件、内存、数量或评测运算超过限制",
    "FORMAT": "实际文件容器或图像结构校验失败",
    "DEPENDENCY": "缺少可选读取依赖，仅由用户手动安装",
    "PLAN": "计划、参考文件或原件已改变，请重新预览并审核",
    "EXISTS": "输出已存在，禁止覆盖；请使用新的 results 子目录",
    "PERMISSION": "需要明确确认来源、本地使用及真实缺口；不包含云外传授权",
    "IO": "离线操作未完成，请人工核对已发布文件；不输出底层异常",
}
INPUT_NAMES = {
    "dataset_info.json": "官方 HF 元数据（dataset_info.json）",
    "hosted_files.json": "官方托管文件目录（hosted_files.json）",
    "hosted_files.structured.json": "重新取得的官方对象树（hosted_files.structured.json）",
    "source_review.json": "用户来源/许可审核（source_review.json，可先手动填写空数组 []）",
    "ocb_qna_data.parquet": "官方问答数据（ocb_qna_data.parquet）",
    "ocb_file_sources.parquet": "官方外部来源目录（ocb_file_sources.parquet）",
    "OmniDocBench.json": "官方区域标注（OmniDocBench.json）",
    "page_sources.json": "用户核对的原 PDF 映射（page_sources.json）",
}
INPUT_LABELS = {dataset + ":" + name: dataset + " / " + label
                for dataset in REPOS for name, label in INPUT_NAMES.items()}
STAGES = {
    "json_read": "JSON 文件读取", "json_utf8": "UTF-8 解码", "json_syntax": "JSON 语法",
    "json_strict": "JSON 严格编码（有限浮点数及 Unicode）",
    "source_identity": "source_record 仓库/版本", "source_hash": "source_record 输入摘要",
    "source_card": "source_record cardData 类型", "hosted_tree": "托管文件树对象结构",
    "review_schema": "人工审核表结构", "qa_read": "QA 文件读取",
    "parquet_decode": "Parquet 解码", "qa_rows_schema": "QA 行结构",
    "qa_strict": "QA 严格编码", "qa_track": "QA Track 分类",
}


class BenchmarkError(ValueError):
    """终端只展示固定错误，不泄露正文、答案、本机路径或底层异常。"""

    def __init__(self, code, *, input_id=None, stage=None):
        """保存白名单错误/输入/步骤；不把正文、任意路径或底层异常作为排查提示。"""
        self.code = code if code in ERRORS else "IO"
        self.input_id = input_id if input_id in INPUT_LABELS else None
        self.stage = stage if stage in STAGES else None
        label = f"；输入项：{INPUT_LABELS[self.input_id]}" if self.input_id is not None else ""
        step = f"；步骤：{self.stage}（{STAGES[self.stage]}）" if self.stage is not None else ""
        super().__init__(f"BENCH_{self.code}：{ERRORS[self.code]}{label}{step}")


def read_bytes(path, limit=96 * MIB, beneath=None):
    """有界读取本地普通文件，明确区分缺失/空文件/超限/IO，不扩大预算或输出底层异常。"""
    if type(limit) is not int or limit <= 0:
        raise BenchmarkError("LIMIT")
    path = ohr.plain_path(path, beneath=beneath)
    try:
        size = path.stat().st_size
        if not path.is_file():
            raise BenchmarkError("SOURCE")
        if size == 0:
            raise BenchmarkError("EMPTY")
        if size > limit:
            raise BenchmarkError("LIMIT")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except FileNotFoundError:
        raise BenchmarkError("MISSING") from None
    except OSError:
        raise BenchmarkError("IO") from None
    if not data:
        raise BenchmarkError("EMPTY")
    if len(data) > limit:
        raise BenchmarkError("LIMIT")
    return data


def json_file(path, beneath=None):
    """严格读取有限 JSON，分开标识读取/UTF-8/语法/严格编码失败，不清洗原始数据。"""
    try:
        data = read_bytes(path, beneath=beneath)
    except BenchmarkError as exc:
        raise BenchmarkError(exc.code, input_id=exc.input_id, stage="json_read") from None
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeError:
        raise BenchmarkError("SCHEMA", stage="json_utf8") from None
    try:
        value = json.loads(decoded)
    except ValueError:
        raise BenchmarkError("SCHEMA", stage="json_syntax") from None
    try:
        ohr.encoded(value)
    except (ValueError, UnicodeError):
        raise BenchmarkError("SCHEMA", stage="json_strict") from None
    return value


def rows_file(path):
    """读取官方 JSON/NDJSON/Parquet，区分行结构和严格编码失败，不替换 NaN 或丢字段。"""
    try:
        data = read_bytes(path)
    except BenchmarkError as exc:
        raise BenchmarkError(exc.code, stage="qa_read") from None
    stage = "parquet_decode" if Path(path).suffix.lower() == ".parquet" else "json_syntax"
    try:
        if Path(path).suffix.lower() == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError:
                raise BenchmarkError("DEPENDENCY") from None
            reader = pq.ParquetFile(io.BytesIO(data))
            decoded_bytes = sum(reader.metadata.row_group(index).total_byte_size for index in range(reader.metadata.num_row_groups))
            if reader.metadata.num_rows > 100000 or reader.metadata.num_columns > 128 or decoded_bytes > 192 * MIB:
                raise BenchmarkError("LIMIT")
            rows = reader.read().to_pylist()
        elif Path(path).suffix.lower() in {"jsonl", ".jsonl", ".ndjson"}:
            rows = [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]
        else:
            rows = json.loads(data.decode("utf-8-sig"))
        stage = "qa_rows_schema"
        if not isinstance(rows, list) or not 0 < len(rows) <= 100000 or not all(isinstance(row, dict) for row in rows):
            raise BenchmarkError("SCHEMA", stage=stage)
        stage = "qa_strict"
        ohr.encoded(rows)
        return rows
    except BenchmarkError:
        raise
    except (ValueError, UnicodeError):
        raise BenchmarkError("SCHEMA", stage=stage) from None


def source_record(root, dataset, metadata_names):
    """冻结 HF 仓库 revision 与全部输入摘要；本地摘要不等于官方签名或授权。"""
    try:
        info = json_file(root / "dataset_info.json", beneath=root)
    except BenchmarkError as exc:
        raise BenchmarkError(exc.code, input_id=dataset + ":dataset_info.json", stage=exc.stage) from None
    if (not isinstance(info, dict) or info.get("id") != REPOS[dataset] or not isinstance(info.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", info["sha"]) is None):
        raise BenchmarkError("SOURCE", input_id=dataset + ":dataset_info.json", stage="source_identity")
    fingerprints = {}
    for name in ["dataset_info.json", *metadata_names]:
        ohr.safe_name(name)
        try:
            fingerprints[name] = hashlib.sha256(read_bytes(root / name, beneath=root)).hexdigest()
        except BenchmarkError as exc:
            raise BenchmarkError(exc.code, input_id=dataset + ":" + name, stage="source_hash") from None
    card = info.get("cardData") or {}
    if not isinstance(card, dict):
        raise BenchmarkError("SCHEMA", input_id=dataset + ":dataset_info.json", stage="source_card")
    return {"repo": REPOS[dataset], "revision": info["sha"], "metadata_sha256": fingerprints,
            "license_metadata": card.get("license"),
            "official_url": "https://huggingface.co/datasets/" + REPOS[dataset],
            "origin_verification": "user_confirmed_official_download; hashes_are_not_signatures"}


def asset(root, relative, dataset, max_bytes=20 * MIB):
    """检查真实容器、SHA 与图像尺寸；不提取正文、不运行 Parser/OCR。"""
    relative = ohr.safe_name(relative).as_posix()
    data = read_bytes(root / relative, max_bytes, root)
    kind = Path(relative).suffix.lower().lstrip(".")
    if kind not in {"docx", "xlsx", "pptx", "png", "jpg", "jpeg", "pdf"}:
        raise BenchmarkError("FORMAT")
    details = {}
    if kind in {"docx", "xlsx", "pptx"}:
        # 同时复用项目上传容器检查和 OHR 的全成员链接/路径检查，不读取业务正文。
        from app.core.file_formats import validate_file_content
        with ZipFile(io.BytesIO(data)) as archive:
            ohr.archive_index(archive)
        validate_file_content(io.BytesIO(data), kind)
    elif kind == "pdf":
        details["page_count"] = ohr.pdf_metadata(data, 50)
    else:
        try:
            from PIL import Image
        except ImportError:
            raise BenchmarkError("DEPENDENCY") from None
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if (not 0 < width * height <= 40_000_000 or image.format != ("PNG" if kind == "png" else "JPEG")):
                raise BenchmarkError("FORMAT")
            details.update(width=width, height=height)
            image.verify()
    return {"source_file": relative, "destination_file": dataset + "/" + relative,
            "file_type": kind, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), **details}


def ranked(seed, identity):
    """固定种子与稳定来源身份打破平局，输入数组排列不影响结果。"""
    return ohr.digest([seed, identity])


def format_matrix(documents):
    """逐十三种格式报告真实已检查文件数，零样本不宣称已覆盖或通过质量验收。"""
    counts = Counter(item["file_type"] for item in documents)
    return {kind: {"selected_files": counts[kind], "coverage": "selected_not_quality_verified" if counts[kind] else "not_covered"}
            for kind in FORMATS}


def artifacts(dataset, source, documents, references, inventory, gaps, parameters):
    """生成独立 GT、冻结计划和报告；清单候选仅 file/tags，标签等待人审。"""
    reference_bytes = b"".join(ohr.encoded(row) + b"\n" for row in references)
    for item in documents:
        item["manifest_candidate"] = {"file": item["destination_file"], "tags": []}
    payload = {"version": "benchmark-selection-v1", "dataset": dataset, "source": source,
               "parameters": parameters, "documents": documents, "gaps": gaps,
               "references_sha256": hashlib.sha256(reference_bytes).hexdigest()}
    plan = payload | {"plan_sha256": ohr.digest(payload)}
    report = {"plan_sha256": plan["plan_sha256"], "candidate_inventory": inventory,
              "documents": documents, "gaps": gaps, "format_coverage": format_matrix(documents),
              "selected_count": len(documents), "actual_cloud_calls": 0,
              "not_verified": ["人工语义标签", "解析质量", "云外传授权", "READY", "问答准确率"]}
    if dataset == "omnidocbench":
        known = {item.get("original_pdf_identity") if item["file_type"] != "pdf" else item.get("source_identity")
                 for item in documents}
        report["confirmed_original_source_count"] = len(known) if None not in known else None
        report["representation_count_not_independent_documents"] = len(documents)
        report["cloud_warning"] = "图片与同来源PDF各导入可能分别收费，不能算多份独立来源；用户须先审核是否同时导入"
    return plan, report, reference_bytes


def publish_reports(dataset, results, plan, report, references):
    """全部报告仅进入固定 results 子树，输出存在时拒绝，不写业务 manifest。"""
    results_root = ROOT / "docs/test" / (dataset + "_preset") / "results"
    results = ohr.plain_path(results, beneath=results_root)
    outputs = [(results / "selection_plan.json", ohr.encoded(plan)),
               (results / "selection_report.json", ohr.encoded(report)),
               (results / "qa_reference.jsonl", references)]
    if any(path.exists() or path.is_symlink() for path, _ in outputs):
        raise BenchmarkError("EXISTS")
    for path, data in outputs:
        if len(data) > 96 * MIB:
            raise BenchmarkError("LIMIT")
    for path, data in outputs:
        ohr.publish_exclusive(path, data + (b"\n" if path.suffix == ".json" else b""))


def extract(dataset, plan_path, accept_gaps=False):
    """用户显式提取：先复核整批来源/参考/SHA/容器，再独占复制；不覆盖或改 manifest。

    文件发布与整批不是原子事务，磁盘错误可能留下少数完整文件；不自动删除或重复改名。
    """
    base = ROOT / "docs/test" / (dataset + "_preset")
    plan_path = ohr.plain_path(plan_path, beneath=base / "results")
    plan = json_file(plan_path)
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (plan.get("version") != "benchmark-selection-v1" or plan.get("dataset") != dataset
            or ohr.digest(payload) != plan.get("plan_sha256")):
        raise BenchmarkError("PLAN")
    if any(bool(value) for value in plan["gaps"].values()) and not accept_gaps:
        raise BenchmarkError("PERMISSION")
    root = base / "source"
    current = source_record(root, dataset, [name for name in plan["source"]["metadata_sha256"] if name != "dataset_info.json"])
    if current != plan["source"]:
        raise BenchmarkError("PLAN")
    refs = read_bytes(plan_path.parent / "qa_reference.jsonl", beneath=base / "results")
    if hashlib.sha256(refs).hexdigest() != plan["references_sha256"]:
        raise BenchmarkError("PLAN")
    documents = plan["documents"]
    if not 0 < len(documents) <= 24 or sum(item["size_bytes"] for item in documents) > 256 * MIB:
        raise BenchmarkError("LIMIT")
    buffers, hashes, destinations = [], set(), set()
    preset = ROOT / "data/knowledge_base/preset"
    for item in documents:
        checked = asset(root, item["source_file"], dataset, plan["parameters"]["max_bytes"])
        if any(checked.get(key) != item.get(key) for key in checked):
            raise BenchmarkError("PLAN")
        name = ohr.safe_name(item["destination_file"]).as_posix()
        if not name.startswith(dataset + "/") or name.casefold() in destinations or item["sha256"] in hashes:
            raise BenchmarkError("PLAN")
        hashes.add(item["sha256"])
        destinations.add(name.casefold())
        target = ohr.plain_path(preset / name, beneath=preset)
        if target.exists():
            raise BenchmarkError("EXISTS")
        data = read_bytes(root / item["source_file"], plan["parameters"]["max_bytes"], root)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise BenchmarkError("PLAN")
        buffers.append((target, data))
    for target, data in buffers:
        ohr.publish_exclusive(target, data)
    return len(buffers)


def cli(dataset):
    """统一命令参数；默认预览，只有显式 extract 才复制真实素材。"""
    base = ROOT / "docs/test" / (dataset + "_preset")
    parser = argparse.ArgumentParser(description="离线官方基准准备；不联网、不调用解析业务")
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--confirm-official-source", action="store_true")
    parser.add_argument("--confirm-local-use", action="store_true")
    parser.add_argument("--accept-gaps", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=base / "results")
    parser.add_argument("--plan-file", type=Path, default=base / "results/selection_plan.json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-mb", type=int, default=20)
    return parser


def run(dataset, prepare, argv=None, *, diagnose=None):
    """只打印数量/安全步骤；OCB 诊断模式不生成报告、不读取文档原件或联网。"""
    parser = cli(dataset)
    if dataset == "ocb":
        parser.add_argument("--qa-file", default="ocb_qna_data.parquet")
        parser.add_argument("--schema", choices=["hf", "ndjson"], default="hf")
        parser.add_argument("--per-format", type=int, choices=[1, 2], default=2)
        parser.add_argument("--hosted-file", choices=["hosted_files.json", "hosted_files.structured.json"],
                            default="hosted_files.json")
        parser.add_argument("--diagnose-inputs", action="store_true", help="只读定位输入步骤，不生成计划或解析文档")
    else:
        parser.add_argument("--count", type=int, choices=[4, 5, 6], default=5)
    args = parser.parse_args(argv)
    try:
        if not args.confirm_official_source or not 1 <= args.max_mb <= 20:
            raise BenchmarkError("PERMISSION")
        if getattr(args, "diagnose_inputs", False):
            if args.extract or diagnose is None:
                raise BenchmarkError("PERMISSION")
            diagnose(args)
            return 0
        if args.extract:
            if not args.confirm_local_use:
                raise BenchmarkError("PERMISSION")
            count = extract(dataset, args.plan_file, args.accept_gaps)
            print(f"离线提取完成：{count} 份；未导入、未提交云任务。")
        else:
            plan, report, references = prepare(args)
            publish_reports(dataset, args.results_dir, plan, report, references)
            print(f"候选报告完成：{report['selected_count']} 份；请人工审核缺口及权限。")
        return 0
    except ohr.PreparationError as exc:
        print(BenchmarkError("PATH" if exc.code in {"OHR_PATH", "OHR_ZIP"} else "IO"))
    except BenchmarkError as exc:
        print(exc)
    except Exception:
        print(BenchmarkError("IO"))
    return 1
