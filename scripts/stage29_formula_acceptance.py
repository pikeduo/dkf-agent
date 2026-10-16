"""阶段29人工验收：默认dry-run，显式执行仅只读绑定已有公式并进行有界精确计算。"""

import argparse
import json
from pathlib import Path


def main(argv=None):
    """复用原阶段28请求JSON；不写报告/业务记录，不请求LLM，完整内容须单独允许显示。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="已有阶段28 FormulaBindingRequest JSON，不是终端日志")
    parser.add_argument("--execute", action="store_true", help="确认来源只读查询和本地计算；请先人工核对公式/单位")
    parser.add_argument("--show-content", action="store_true", help="在可信终端显示公式、参数、步骤、结果及来源")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：未读取输入/配置、查询数据库、导入SymPy或执行计算；无任何云请求。")
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
        print("FORMULA_CALC_INPUT：输入缺失、超限或不符合契约，未回显路径/正文，也未计算。")
        return 2

    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.formula_binding import FormulaBindingError
    from app.services.formula_calculation import FormulaCalculationError, calculate_formula
    from app.services.rag.evidence import EvidenceError

    try:
        configure_logging(get_settings(), role="formula-calculation-cli")
        result = calculate_formula(request)
    except (FormulaCalculationError, FormulaBindingError, EvidenceError) as exc:
        print("STAGE29_FAILED =", exc.code, "；未请求模型、写入业务数据或自动重试。")
        return 1
    except Exception:
        print("STAGE29_FAILED：配置或计算未完成，未输出上游异常原文。")
        return 1
    print(json.dumps({"status": result.status, "calculation_id": str(result.calculation_id) if result.calculation_id else None,
        "formula_id": str(result.formula.formula_id), "required_variables": result.required_variables,
        "bound_count": len(result.variables), "step_count": len(result.steps), "engine_version": result.engine_version,
        "calculation_scope": result.calculation_scope, "semantic_validation": result.semantic_validation,
        "unit_validation": result.unit_validation}, ensure_ascii=False))
    if args.show_content:
        print(result.model_dump_json(indent=2))
    print("ARITHMETIC_ONLY：精确算术不证明原公式语义或单位/期间正确；未自动换算或推断结果单位。")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
