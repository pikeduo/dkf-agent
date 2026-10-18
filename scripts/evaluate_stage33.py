"""阶段33固定200文件/4087QA的全量导入、检索与报告；默认dry-run，不抽样。"""

import argparse
import math
import sys
from functools import partial
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stage33_common import (DATASET, Stage33Error, initialize, load_dataset, plain_path,
                                   read_json, run_lock, write_json)
from scripts.stage33_reports import generate
from scripts.stage33_runtime import API, import_all, read_snapshot
from scripts.stage33_retrieval import Backends, run_evaluation


def initial_plan(data):
    """登记全量范围和页数成本参考，不执行Router或把潜在云份数冒充实际费用。"""
    reference = sum(max(1, math.ceil(int(d["page_count"] or 1) / 200)) for d in data["documents"])
    return {"document_count": len(data["documents"]), "qa_count": len(data["questions"]),
        "file_issues": data.get("file_issues", {}),
        "fingerprint": data["fingerprint"], "documents": data["documents"],
        "oversized_pdf_file_ids": [d["file_id"] for d in data["documents"] if d["file_type"] == "pdf"
                                   and int(d["page_count"]) > 200],
        "if_all_files_use_cloud_page_based_file_count": reference,
        "actual_cloud_submissions": None, "actual_cost": None,
        "cost_note": "仅按页数的全云情形参考，不是上限/报价；Native与缓存可减少，实际分片字节可增加。"}


def build_parser():
    """正式CLI无count/limit/前N参数，inflight只控制资源，不缩小总范围。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=["plan", "import", "evaluate", "report"], default="plan")
    parser.add_argument("--run", default="stage33_run_01")
    parser.add_argument("--kb-id", type=UUID)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-cloud", action="store_true", help="确认全部200原件的使用/外传权限及所有分片费用")
    parser.add_argument("--retry-failed", action="store_true", help="显式普通Retry/重跑失败Query；不Force或重建索引")
    parser.add_argument("--retry-failed-only", action="store_true",
                        help="仅恢复原检查点FAILED且doc_id一致的文档，不上传尚未尝试文件；需import及resume")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--max-inflight", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=86400, help="导入CLI总等待秒数；不修改已有Job冻结窗口")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--bm25-max-chunks", type=int, default=100_000)
    parser.add_argument("--bm25-max-mb", type=int, default=256)
    parser.add_argument("--bm25-max-tokens", type=int, default=32_000_000)
    return parser


def main(argv=None):
    """只在显式执行后写新run/调用服务；所有完成/中断均尝试生成完整清单，不自动推进34。"""
    args = build_parser().parse_args(argv)
    data, run, error_code = None, None, None
    try:
        if (not args.run.startswith("stage33_run_") or not args.run.removeprefix("stage33_run_").isdigit()
                or not 1 <= args.max_inflight <= 8 or not 1 <= args.timeout <= 7 * 86400
                or not 1 <= args.poll_interval <= 60):
            raise Stage33Error("ARGUMENTS")
        if args.execute and args.kb_id is None:
            raise Stage33Error("KB_ID_REQUIRED")
        if args.execute and args.step == "import" and not args.allow_cloud:
            raise Stage33Error("ALL_FILES_CLOUD_PERMISSION_REQUIRED")
        if args.retry_failed_only and (args.step != "import" or not args.resume):
            raise Stage33Error("RETRY_FAILED_ONLY_REQUIRES_IMPORT_RESUME")
        # 仅读取冻结原件Hash/容器与元数据，绝不重新执行阶段32准备/提取脚本。
        data = load_dataset(DATASET)
        plan = initial_plan(data)
        if not args.execute:
            print(f"DRY_RUN：{plan['document_count']}份文件；{plan['qa_count']}条QA；"
                  f"全云按页参考{plan['if_all_files_use_cloud_page_based_file_count']}文件任务。"
                  f"输入错误={len(data.get('file_issues', {}))}；未上传/加载模型/连接数据库/写报告。")
            return 1 if data.get("file_issues") else 0
        from app.core.business_logging import configure_logging, log_event
        from app.core.config import get_settings
        configure_logging(get_settings(), role="dataset-evaluation-cli")
        run = plain_path(ROOT / "results/stage33" / args.run, ROOT / "results/stage33")
        existed = (run / "run.json").exists()
        if existed != args.resume:
            raise Stage33Error("RESUME_REQUIRED" if existed else "RUN_NOT_FOUND")
        with run_lock(run):
            initialize(run, data, args.kb_id)
            if not (run / "plan.json").exists():
                write_json(run / "plan.json", plan)
            timing = read_json(run / "timing.json") if (run / "timing.json").exists() else {"executions": []}
            started = perf_counter()
            log_event("dataset_evaluation.started", kb_id=args.kb_id, stage=args.step,
                      file_count=len(data["documents"]), item_count=len(data["questions"]))
            try:
                # 首次进入即登记全部QA，即使随后服务不可用也不会只剩成功子集。
                generate(data, run)
                if args.step == "import":
                    api = API(args.api_base_url)
                    try:
                        api.precheck(args.kb_id)
                        import_all(data, run, args.kb_id, api, max_inflight=args.max_inflight,
                            timeout=args.timeout, poll_interval=args.poll_interval, retry_failed=args.retry_failed,
                            retry_failed_only=args.retry_failed_only)
                    finally:
                        api.close()
                elif args.step == "evaluate":
                    from app.services.rag.bm25 import CorpusLimits
                    limits = CorpusLimits(args.bm25_max_chunks, args.bm25_max_mb * 1024**2, args.bm25_max_tokens)
                    run_evaluation(data, run, args.kb_id, partial(read_snapshot, detailed=False), retry_failed=args.retry_failed,
                                   backend_factory=partial(Backends, corpus_limits=limits))
            except BaseException as error:
                error_code = error.code if isinstance(error, Stage33Error) else (
                    "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "LOCAL_OR_SERVICE_ERROR")
            timing["executions"].append({"step": args.step, "finished_at": datetime.now(UTC).isoformat(),
                                        "duration_seconds": perf_counter() - started, "error_code": error_code})
            write_json(run / "timing.json", timing)
            report = generate(data, run, fatal_code=error_code)
            log_event("dataset_evaluation.finished", kb_id=args.kb_id, stage=args.step,
                status=report["status"], file_count=report["ready"], item_count=report["fixed_qa_count"],
                error_code=error_code, duration_ms=(perf_counter() - started) * 1000)
            print(f"{report['status']}：READY={report['ready']}/{report['fixed_document_count']}；"
                  f"PARTIAL_READY={report.get('partial_ready', 0)}；"
                  f"QA登记={report['fixed_qa_count']}；报告={run / 'summary.md'}；运行器错误={error_code or '-'}")
            if args.step == "import" and report["ready"] != report["fixed_document_count"]:
                print("存在失败或未完成文档；运行器无异常不代表恢复成功，逐文件原因见import_report.json。")
            if error_code:
                return 1
            return 0 if args.step in {"plan", "report"} or (args.step == "import" and report["ready"] == 200) or report["status"] == "PASS" else 1
    except (Stage33Error, OSError, ValueError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, Stage33Error) else "INPUT_OR_OUTPUT"
        print(f"STAGE33_STOPPED：{code}；未缩减固定范围或自动重试收费任务。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
