"""阶段35人工演示入口：默认dry-run；只读现有文档，显式选页才检查原PDF/图片。"""

import argparse
from pathlib import Path


def main(argv=None):
    """用户显式执行后选择真实候选并生成派生报告，不启动模型、Worker或请求云服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", help="已有ACTIVE知识库UUID")
    parser.add_argument("--doc-id", help="已有READY/PARTIAL_READY文档UUID；省略时按case选择真实候选")
    parser.add_argument("--case", choices=("overview", "outline", "formula", "complexity", "coverage", "visual_review"),
        default="overview", help="仅用于选择候选，不代表该文档已经通过专项验收")
    parser.add_argument("--visual-pages", type=int, nargs="+", default=[], help="明确允许读取原PDF/图片的1～5个页码")
    parser.add_argument("--execute", action="store_true", help="允许本次只读数据库评估及明确选页的本地像素观测")
    parser.add_argument("--output", help="新JSON报告路径；含标题片段，不覆盖已有文件")
    parser.add_argument("--show-content", action="store_true", help="在可信终端显示完整报告及标题片段")
    args = parser.parse_args(argv)
    if not args.execute:
        print("DRY-RUN：未查询数据库、读取原件、渲染页面、加载模型或写报告；无云调用。")
        return 0
    from uuid import UUID
    try:
        kb_id = UUID(args.kb_id)
        if (kb_id.int == 0 or len(args.visual_pages) > 5
                or len(set(args.visual_pages)) != len(args.visual_pages) or any(page < 1 for page in args.visual_pages)):
            raise ValueError
        output = Path(args.output) if args.output else None
        if output is not None and (output.exists() or output.is_symlink() or output.suffix.lower() != ".json"):
            raise ValueError
        doc_id = UUID(args.doc_id) if args.doc_id else None
        if doc_id is not None and doc_id.int == 0:
            raise ValueError
    except Exception:
        print("ANALYSIS_INPUT：参数或新报告路径无效，未查询来源或覆盖文件。")
        return 2
    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.schemas.document_analysis import DocumentAnalysisRequest
    from app.services.document_assessment import DocumentAssessmentService
    from app.services.specialty_errors import SpecialtyError
    try:
        configure_logging(get_settings(), role="document-analysis-cli")
        service = DocumentAssessmentService()
        doc_id = doc_id or service.choose_document(kb_id, case_name=args.case)
        request = DocumentAnalysisRequest(kb_id=kb_id, doc_id=doc_id, visual_pages=args.visual_pages)
        report = service.analyze(request)
        encoded = report.model_dump_json(indent=2)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            # 排他创建，保留历史评估，不将本轮观察覆盖成旧报告的验收结果。
            with output.open("x", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
        print(f"ANALYZED：doc_id={doc_id}；Block={report.block_count}；标题={len(report.outline.nodes)}；"
              f"复杂度={report.complexity.level}；质量提示={report.quality.status}；manual_pending")
        if args.show_content:
            print(encoded)
        print("派生评估已生成；未改变原文、解析质量、索引或READY状态，语义效果仍需人工核验。")
        return 0
    except SpecialtyError as error:
        print(f"STAGE35_FAILED：{error.code}；{error.message}")
        return 1
    except Exception:
        print("STAGE35_FAILED：配置、输入或报告保存未完成；未输出路径、正文或上游异常。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
