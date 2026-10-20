"""阶段34：默认预览；用户显式授权后固定题单问答，人工评分与离线报告分离。"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stage33_common import read_json, write_json
from scripts.stage34_evaluation import (Stage34Error, execute, load_plan, make_plan, make_retry_plan, plan_id,
                                        report, run_directory, run_lock)


def build_parser():
    """默认只读预览，不连接数据库、加载模型或发送云请求。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=["plan", "retry-plan", "evaluate", "report"], default="plan")
    parser.add_argument("--run", default="stage34_run_01")
    parser.add_argument("--source-run", default="stage33_run_03")
    parser.add_argument("--retry-source-run", help="仅retry-plan：从旧阶段34运行提取未发起LLM的检索失败题")
    parser.add_argument("--single-count", type=int, default=10)
    parser.add_argument("--office-count", type=int, default=5)
    parser.add_argument("--sample-seed", type=int, default=34)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-cloud", action="store_true", help="确认题单问题与最终证据外传及费用")
    parser.add_argument("--max-cloud-calls", type=int, default=0, help="本run累计请求上限；含主备各次请求及中断保守预留")
    parser.add_argument("--limit", type=int, default=1, help="本次最多新执行题数，默认先验证1题")
    return parser


def main(argv=None):
    """许可和预算前置；补跑只另建未收费检索失败题计划，旧记录与代码身份保持冻结。"""
    args = build_parser().parse_args(argv)
    try:
        run = run_directory(args.run)
        if args.step == "retry-plan":
            if not args.retry_source_run or args.retry_source_run == args.run:
                raise Stage34Error("RETRY_SOURCE_RUN")
        elif args.retry_source_run is not None:
            raise Stage34Error("RETRY_SOURCE_RUN")
        if not 1 <= args.limit <= 200 or not 0 <= args.max_cloud_calls <= 400:
            raise Stage34Error("BUDGET_ARGUMENTS")
        if args.step == "evaluate" and args.execute and not args.allow_cloud:
            raise Stage34Error("CLOUD_PERMISSION_REQUIRED")
        if args.step == "evaluate" and args.execute and args.max_cloud_calls < 1:
            raise Stage34Error("CLOUD_BUDGET")
        if args.step == "plan":
            plan = make_plan(args.source_run, single_count=args.single_count,
                office_count=args.office_count, seed=args.sample_seed)
        elif args.step == "retry-plan":
            plan = make_retry_plan(args.retry_source_run)
        else:
            plan = load_plan(run)
        if not args.execute:
            print(f"DRY_RUN：固定{len(plan['cases'])}题；最多每题{plan['policy']['llm_calls_per_case_max']}次LLM请求；"
                  f"Fallback={'开启' if plan['policy']['fallback'] else '关闭'}；"
                  "未请求云端/加载模型/连接数据库/写报告。")
            return 0
        with run_lock(run):
            if args.step in {"plan", "retry-plan"}:
                path = run / "plan.json"
                if path.exists():
                    if not args.resume or read_json(path) != {"sha256": plan_id(plan), "plan": plan}:
                        raise Stage34Error("PLAN_EXISTS_OR_CHANGED")
                else:
                    if args.resume or any(p.name != "run.lock" for p in run.iterdir()):
                        raise Stage34Error("RUN_EXISTS")
                    write_json(path, {"sha256": plan_id(plan), "plan": plan})
                report(run, plan)
                print(f"PLAN_READY：{len(plan['cases'])}题；先检查plan.json，再显式授权问答。")
                return 0
            if args.step == "evaluate":
                if any((run / "cases").glob("*.json")) and not args.resume:
                    raise Stage34Error("RESUME_REQUIRED")
                from app.core.business_logging import configure_logging, log_event
                from app.core.config import get_settings
                from scripts.stage34_runtime import Runtime
                settings = get_settings()
                configure_logging(settings, role="rag-cli")
                log_event("rag_evaluation.started", kb_id=plan["kb_id"], item_count=len(plan["cases"]))
                runtime = Runtime(settings)
                execute(run, plan, runtime, max_cloud_calls=args.max_cloud_calls, limit=args.limit)
            result = report(run, plan)
            if args.step == "evaluate":
                log_event("rag_evaluation.finished", kb_id=plan["kb_id"], status=result["status"],
                          item_count=result["completed"])
            print(f"REPORT：完成{result['completed']}/{result['planned']}；失败{result['failed']}；"
                  f"中断{result['interrupted']}；{result['status']}。")
            return 1 if (result["failed"] or result["interrupted"] or result["source_failed_answers"]
                         or result["snapshot_limitations"]) else 0
    except Exception as exc:
        from scripts.stage33_common import Stage33Error
        if isinstance(exc, (Stage34Error, Stage33Error)):
            code = exc.code
        else:
            from scripts.stage34_evaluation import safe_error
            code = safe_error(exc)
        print(f"STAGE34_STOPPED：{code}；不自动重发收费请求；原检查点保留。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
