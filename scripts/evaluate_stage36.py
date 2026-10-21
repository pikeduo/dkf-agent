"""阶段36性能入口：默认只预览，显式执行才查询现有数据库和本地模型。"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stage33_common import digest, read_json, write_json
from scripts.stage36_performance import Stage36Error, execute, load_plan, make_plan, report, run_directory, safe_error


def build_parser():
    """预算默认小批，收费接口无入口；预览不连接服务、不加载模型或写文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=["plan", "evaluate", "report"], default="plan")
    parser.add_argument("--run", default="stage36_run_01")
    parser.add_argument("--source-run", default="stage34_run_03")
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 50, 200])
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--questions", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--sample-seed", type=int, default=36)
    parser.add_argument("--limit-groups", type=int, default=1)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv=None):
    """先冻结计划、再手动小批测量；错误只报安全码，绝不自动补跑失败批次。"""
    args = build_parser().parse_args(argv)
    configured = False
    try:
        run = run_directory(args.run)
        plan = (make_plan(args.source_run, sizes=args.sizes, concurrency=args.concurrency,
                questions=args.questions, repeats=args.repeats, warmup=args.warmup, seed=args.sample_seed)
                if args.step == "plan" else load_plan(run))
        if not 1 <= args.limit_groups <= 15:
            raise Stage36Error("GROUP_LIMIT")
        if not args.execute:
            print(f"DRY_RUN：规模{[s['documents'] for s in plan['scopes']]}；并发{plan['concurrency']}；"
                  f"每批{len(plan['cases']) * plan['repeats']}次查询，另有{plan['warmup']}次预热；"
                  "未连接数据库/加载模型/调用云端/写报告。")
            return 0
        from scripts.stage34_evaluation import run_lock
        with run_lock(run):
            if args.step == "plan":
                path = run / "plan.json"
                if path.exists():
                    if not args.resume or read_json(path) != {"sha256": digest(plan), "plan": plan}:
                        raise Stage36Error("PLAN_EXISTS_OR_CHANGED")
                else:
                    if args.resume or any(p.name != "run.lock" for p in run.iterdir()):
                        raise Stage36Error("RUN_EXISTS")
                    write_json(path, {"sha256": digest(plan), "plan": plan})
                print("PLAN_READY：请检查plan.json；尚未执行真实查询。")
                return 0
            if args.step == "evaluate":
                if (run / "runtime.json").exists() and not args.resume:
                    raise Stage36Error("RESUME_REQUIRED")
                from app.core.business_logging import configure_logging, log_event
                from app.core.config import get_settings
                from scripts.stage36_performance import Runtime
                configure_logging(get_settings(), role="performance-cli")
                configured = True
                runtime = Runtime(plan)
                try:
                    execute(run, plan, runtime, resume=args.resume, limit_groups=args.limit_groups)
                except (Exception, KeyboardInterrupt):
                    # 中断仍保留STARTED及已落盘明细；生成报告不执行任何补发或模型调用。
                    try:
                        report(run, plan)
                    except Exception:
                        # 报告来源缺失等次生问题不能掩盖最初的执行错误。
                        pass
                    raise
            result = report(run, plan)
            groups = result["groups"]
            failed = any(g["status"] not in {"COMPLETED", "NOT_RUN"} for g in groups)
            print(f"REPORT：完成{sum(g['status'] == 'COMPLETED' for g in groups)}/{len(groups)}批；"
                  f"{result['status']}；详见当前run的summary.md和report.json。")
            return 1 if failed or result["validity"] == "CHANGED_OR_UNAVAILABLE" else 0
    except (Exception, KeyboardInterrupt) as exc:
        code = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else safe_error(exc)
        if configured:
            log_event("performance.failed", level=logging.WARNING, error_code=code)
        print(f"STAGE36_STOPPED：{code}；保留旧记录，不自动重试。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
