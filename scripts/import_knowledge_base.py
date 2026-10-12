"""预置知识库命令行入口，默认只预检；正式导入与费用确认必须由用户显式执行。"""

import argparse
import sys
from pathlib import Path
from uuid import UUID

# 同时支持 python scripts/import_knowledge_base.py 与 python -m scripts.import_knowledge_base。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.business_logging import configure_logging
from app.core.config import PROJECT_ROOT, get_settings
from app.db.seed import DEFAULT_KB_ID
from app.services.preset_manifest import PresetImportError, preflight


def build_parser() -> argparse.ArgumentParser:
    """提供只读默认模式、显式费用确认和有界等待选项，不接受 Force Reparse 开关。"""

    parser = argparse.ArgumentParser(description="预置知识库预检/导入；不生成样本、不安装或启动服务")
    parser.add_argument("--directory", type=Path, default=PROJECT_ROOT / "data/knowledge_base/preset")
    parser.add_argument("--kb-id", type=UUID, default=DEFAULT_KB_ID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅预检（默认），不连接基础服务")
    mode.add_argument("--execute", action="store_true", help="实际导入并等待 READY，仅用户执行")
    parser.add_argument("--allow-cloud", action="store_true", help="确认 auto 可外传原件/视觉内容并消耗 MinerU 额度")
    parser.add_argument("--timeout", type=int, default=1800, help="最多等待秒数，1～86400")
    parser.add_argument("--poll-interval", type=int, default=5, help="数据库检查间隔秒数，1～60")
    return parser


def main(argv: list[str] | None = None) -> int:
    """默认不写数据库/文件；执行时只打印安全汇总，失败非零退出且不展示异常原文。"""

    args = build_parser().parse_args(argv)
    if args.execute and not args.allow_cloud:
        print("正式导入需同时指定 --allow-cloud，确认自动路由的外传与费用风险。")
        return 2
    if not 1 <= args.timeout <= 86400 or not 1 <= args.poll_interval <= 60:
        print("等待时间或检查间隔超出允许范围。")
        return 2
    try:
        settings = get_settings()
        files = preflight(args.directory, settings)
        print(f"预检通过：文件数={len(files)}，不同内容数={len({file.file_hash for file in files})}，"
              f"格式={','.join(sorted({file.file_type for file in files}))}；内容标签仍需人工核对。")
        if not args.execute:
            print("只读预检结束：未写数据库/文件，未投递任务，未解析文档或调用云端。")
            return 0
        configure_logging(settings, role="preset-cli")
        from app.services.preset_import import run_import

        items = run_import(files, settings, args.kb_id, timeout=args.timeout, poll_interval=args.poll_interval)
        for item in items:
            # 不打印文件内容、完整路径、Hash、上游错误或签名链接。
            print(f"doc_id={item.doc_id or '-'} 状态={item.status} 错误码={item.error_code or '-'}")
        ready = sum(item.status == "READY" and item.error_code is None for item in items)
        print(f"导入结束：总数={len(items)} READY={ready} 未完成={len(items) - ready}")
        return 0 if ready == len(items) else 1
    except PresetImportError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("等待已中止：已提交后台任务不会被取消，请先核对数据库状态。")
        return 130
    except Exception:
        print("PRESET_UNKNOWN：导入未完成，请核对配置、数据库、文档状态及安全日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
