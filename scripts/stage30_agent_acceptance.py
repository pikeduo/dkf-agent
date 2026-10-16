"""Knowledge Agent人工单次验收入口；默认dry-run，不自动生成样本、部署或请求服务。"""

import argparse
from pathlib import Path


def main(argv=None):
    """显式执行才读取受限请求；云许可在请求内另行确认，默认不输出敏感业务内容。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="KnowledgeAgentRequest JSON；不是已有Agent State或日志")
    parser.add_argument("--execute", action="store_true", help="确认本次真实业务Tool调用；云调用仍需allow_cloud=true")
    parser.add_argument("--show-content", action="store_true", help="在可信终端显示回答/证据/公式结果")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：未读取输入/配置、构造图、加载模型、查库或请求云服务。")
        return 0
    try:
        if not args.input:
            raise ValueError
        path = Path(args.input)
        if not path.is_file() or path.is_symlink():
            raise ValueError
        with path.open("rb") as stream:
            content = stream.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError
        from app.schemas.knowledge_agent import KnowledgeAgentRequest
        from app.services.llm import strict_json
        request = KnowledgeAgentRequest.model_validate(strict_json(content.decode("utf-8-sig", errors="strict")))
    except Exception:
        print("AGENT_INPUT：请求缺失、超限或无效，未显示原始内容/路径，未调用Tool。")
        return 2
    try:
        from app.agents.knowledge import run_knowledge_agent
        from app.core.business_logging import configure_logging
        from app.core.config import get_settings
        configure_logging(get_settings(), role="knowledge-agent-cli")
        result = run_knowledge_agent(request)
    except Exception:
        print("AGENT_INTERNAL：配置或运行未完成，未回显异常原文，也未自动重试。")
        return 1
    print(f"agent_run_id={result.agent_run_id} status={result.status} route={result.selected_route} "
          f"step_count={result.step_count} error_code={result.error_code}")
    if args.show_content:
        print(result.model_dump_json(indent=2))
    print("单次调用已结束；无自动重试/持久化恢复。结果及来源的业务含义仍须人工核验。")
    return 0 if result.status in {"success", "empty", "insufficient_evidence"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
