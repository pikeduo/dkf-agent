"""阶段28人工验收：默认dry-run，显式执行后仅反查当前来源并绑定参数，绝不请求LLM或计算。"""

import argparse
import json
from pathlib import Path


def main(argv=None):
    """读取受限本地JSON中的阶段27结果和显式参数；不写报告，默认隐藏公式/参数正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="用户保存的FormulaBindingRequest JSON，含来源正文，请勿提交Git")
    parser.add_argument("--execute", action="store_true", help="确认只读PostgreSQL来源核对及本地参数绑定")
    parser.add_argument("--show-content", action="store_true", help="仅在可信终端显示完整值/来源，不写业务日志")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：未读取正文、连接数据库、请求LLM或计算；--input与--execute才执行绑定。")
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
        from app.schemas.formula_binding import FormulaBindingRequest
        from app.services.llm import strict_json
        request = FormulaBindingRequest.model_validate(strict_json(content.decode("utf-8-sig", errors="strict")))
    except Exception:
        print("FORMULA_BINDING_INPUT：输入缺失、超限或不符合严格JSON契约，未输出路径/原文，也未连接数据库。")
        return 2

    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.formula_binding import FormulaBindingError, bind_formula_variables
    from app.services.rag.evidence import EvidenceError

    try:
        configure_logging(get_settings(), role="formula-binding-cli")
        result = bind_formula_variables(request.formula, user_inputs=request.user_inputs, evidence_inputs=request.evidence_inputs)
    except (FormulaBindingError, EvidenceError) as exc:
        print("STAGE28_FAILED =", exc.code, "；未计算、未请求模型或自动重试。")
        return 1
    except Exception:
        print("STAGE28_FAILED：配置或来源读取未完成，未输出上游异常原文。")
        return 1
    print(json.dumps({"status": result.status, "formula_id": str(result.formula.formula_id),
        "bound_count": len(result.variables), "required_variables": result.required_variables,
        "semantic_validation": result.semantic_validation, "unit_validation": result.unit_validation,
        "calculation": result.calculation}, ensure_ascii=False))
    if args.show_content:
        print(result.model_dump_json(indent=2))
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
