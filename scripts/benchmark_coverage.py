"""仅汇总三个已有抽样计划的真实格式矩阵；不降低阶段 16 业务预检标准。"""

import argparse
import sys
from collections import Counter
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import benchmark_common as common


def coverage(plans):
    """同 SHA 不能重复计数，图像页与原 PDF 来源独立性未知时报告而非确认十份独立文档。"""
    result = {kind: {} for kind in common.FORMATS}
    hashes, counts, warnings = {}, Counter(), []
    for dataset, plan in plans.items():
        if dataset not in {"ohr", "ocb", "omnidocbench"}:
            raise common.BenchmarkError("SCHEMA")
        for document in plan["documents"]:
            kind = "pdf" if dataset == "ohr" else document["file_type"]
            sha = document["sha256"]
            if sha in hashes:
                warnings.append({"reason": "duplicate_sha_across_plans", "datasets": [hashes[sha], dataset]})
                continue
            hashes[sha] = dataset
            counts[(kind, dataset)] += 1
    for kind in common.FORMATS:
        for dataset in ("ohr", "ocb", "omnidocbench"):
            result[kind][dataset] = {"selected_count": counts[(kind, dataset)],
                                    "quality_verified": False,
                                    "ground_truth": "qa_evidence" if dataset == "ohr" else "qa_assertions" if dataset == "ocb" else "region_annotation"}
    return {"format_coverage": result, "distinct_sha_count": len(hashes), "warnings": warnings,
            "stage16_preflight_passed": None,
            "requires_user": ["DOC/XLS/PPT 合法真实素材", "TXT/MD/CSV 官方 GT 缺口", "text_pdf/scan_pdf/mixed 人工确认",
                              "至少十份独立来源（不是十个不同表示）", "现有严格 dry-run", "版权及云外传权限"],
            "note": "选样矩阵不是全格式质量验收或官方问答得分"}


def main(argv=None):
    """用户手动读取三份计划并独占写出本地矩阵，不下载/提取/导入或评分。"""
    parser = argparse.ArgumentParser(description="十三种格式的真实抽样覆盖报告")
    parser.add_argument("--ohr-plan", type=Path, required=True)
    parser.add_argument("--ocb-plan", type=Path, required=True)
    parser.add_argument("--omni-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = common.ROOT / "docs/test"
        plans = {"ohr": common.json_file(common.ohr.plain_path(args.ohr_plan, beneath=root)),
                 "ocb": common.json_file(common.ohr.plain_path(args.ocb_plan, beneath=root)),
                 "omnidocbench": common.json_file(common.ohr.plain_path(args.omni_plan, beneath=root))}
        report = coverage(plans)
        target = common.ohr.plain_path(args.output, beneath=root)
        common.ohr.publish_exclusive(target, common.ohr.encoded(report) + b"\n")
        print(f"覆盖矩阵完成：{report['distinct_sha_count']} 个不同 SHA；独立来源与预检仍需人工确认。")
        return 0
    except Exception:
        print(common.BenchmarkError("IO"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
