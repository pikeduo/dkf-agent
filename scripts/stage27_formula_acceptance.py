"""阶段27显式人工验收：默认dry-run，用户批准后才查来源及请求一次所选LLM。"""

import argparse
import json
from uuid import UUID


def main(argv=None) -> int:
    """读取真实业务ID；未同时确认执行/云许可时不连接数据库、加载模型或请求API。"""
    parser = argparse.ArgumentParser(description="阶段27公式语义结构化（不绑定参数、不计算）")
    parser.add_argument("--kb-id", help="已有ACTIVE知识库UUID")
    parser.add_argument("--doc-id", help="已有READY文档UUID，可额外限制文档")
    parser.add_argument("--chunk-id", help="含完整公式来源的已有Chunk UUID")
    parser.add_argument("--block-id", help="该Chunk中的MinerU formula Block UUID")
    parser.add_argument("--execute", action="store_true", help="显式执行真实来源反查及单次LLM请求")
    parser.add_argument("--allow-cloud", action="store_true", help="确认公式/上下文外传权限及一次LLM费用")
    parser.add_argument("--show-content", action="store_true", help="仅在可信终端显示完整定义与来源正文，不写报告")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：不会查数据库或调用LLM；填写真实ID并同时指定--execute --allow-cloud才执行。")
        return 0
    if not args.allow_cloud:
        print("FORMULA_CLOUD_PERMISSION：未确认外传及费用，没有连接数据库或请求模型。")
        return 2
    try:
        values = {name: UUID(getattr(args, name)) for name in ("kb_id", "chunk_id", "block_id")}
        if args.doc_id is not None:
            values["doc_id"] = UUID(args.doc_id)
        if any(value.int == 0 for value in values.values()):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        print("FORMULA_INPUT：请提供真实的非零业务UUID，未执行来源查询或模型请求。")
        return 2

    # 仅显式执行路径导入项目服务，不使用命令行参数覆盖提供方、密钥或模型配置。
    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.formula import FormulaError, extract_formula
    from app.services.llm import LLMError
    from app.services.rag.evidence import EvidenceError

    try:
        configure_logging(get_settings(), role="formula-cli")
        result = extract_formula(**values, allow_cloud=True)
    except (FormulaError, LLMError, EvidenceError) as exc:
        print("STAGE27_FAILED =", exc.code, "；没有自动重试，网络不确定时请先核对账户费用。")
        return 1
    except Exception:
        print("STAGE27_FAILED：配置或运行未完成，未输出异常原文，也不会自动重试。")
        return 1
    print(json.dumps({"status": result.status, "formula_id": str(result.formula_id),
        "doc_id": str(result.source.doc_id), "chunk_id": str(result.source.chunk_id),
        "formula_block_id": str(result.formula_block_id), "variable_count": len(result.variables),
        "page_semantics": result.source.page_semantics, "semantic_validation": result.semantic_validation},
        ensure_ascii=False))
    if args.show_content:
        # 明确同意后只向本机终端显示JSON正文；不交给Logger、不生成验收素材或报告。
        print(result.model_dump_json(indent=2))
    print("STRUCTURE_ONLY：请人工核对原式、运算/百分比、变量含义及引用；没有执行SymPy计算。")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
