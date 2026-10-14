"""阶段22真实RAG验收：在项目根目录执行，无PowerShell多行粘贴或YES确认。"""

import argparse
import re
import sys
from pathlib import Path
from time import perf_counter
from uuid import UUID

DEFAULT_KB = "80bf4946-c552-49c6-89b7-f9895cbb433d"
DEFAULT_DOC = "f5d85573-2971-4d01-a3cb-365f4df97aaa"


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段22：真实 DeepSeek RAG 问答验收")
    parser.add_argument("--question", help="测试问题；不填写则在终端输入")
    parser.add_argument("--kb-id", default=DEFAULT_KB, help="知识库UUID")
    parser.add_argument("--doc-id", default=DEFAULT_DOC, help="READY文档UUID")
    args = parser.parse_args()

    if not (Path.cwd() / "app").is_dir():
        print("ERROR: 请先切换到 dkf-agent 项目根目录（包含 app 文件夹）。")
        return 2

    try:
        kb_id = UUID(args.kb_id)
        doc_id = UUID(args.doc_id)
    except ValueError:
        print("ERROR: --kb-id 或 --doc-id 不是合法 UUID。")
        return 2

    try:
        question = args.question if args.question is not None else input("请输入真实文档问题：").strip()
    except EOFError:
        print("ERROR: 无法读取终端输入；可使用 --question 参数。")
        return 2
    if not question or not question.strip():
        print("ERROR: 测试问题不能为空。")
        return 2

    from app.core.business_logging import configure_logging
    from app.core.config import get_settings
    from app.services.deepseek import DeepSeekError
    from app.services.rag.query import query_knowledge_base
    from app.services.rag.synthesis import RAGError

    settings = get_settings()
    if not settings.deepseek_api_key.get_secret_value().strip():
        print("ERROR: .env 中的 DEEPSEEK_API_KEY 尚未配置。")
        return 2

    configure_logging(settings, role="rag-cli")

    # 不会触发检索、加载GPU模型或请求DeepSeek：先验证未授权保护。
    try:
        query_knowledge_base(kb_id, question, doc_ids=[doc_id], allow_cloud=False)
    except RAGError as exc:
        if exc.code != "RAG_CLOUD_PERMISSION":
            print("PERMISSION_CHECK_FAILED =", exc.code)
            return 1
        print("RAG_CLOUD_PERMISSION_PASS")
    else:
        print("PERMISSION_CHECK_FAILED: 拒绝云调用的边界未生效。")
        return 1

    print("开始一次真实问答（可能产生API费用）...")
    start = perf_counter()
    try:
        result = query_knowledge_base(
            kb_id,
            question,
            doc_ids=[doc_id],
            top_k=30,
            allow_cloud=True,
        )
    except (DeepSeekError, RAGError) as exc:
        print("STAGE22_API_OR_RAG_FAILED =", exc.code)
        print("ELAPSED_SECONDS =", round(perf_counter() - start, 2))
        return 1

    try:
        assert result.status in {"success", "insufficient_evidence"}
        assert result.insufficient_evidence == (result.status == "insufficient_evidence")

        print("RAG_STATUS =", result.status)
        print("CITATION_COUNT =", len(result.citations))

        if result.status == "success":
            assert 1 <= len(result.citations) <= 5
            assert [c.citation_id for c in result.citations] == list(
                range(1, len(result.citations) + 1)
            )
            assert len({c.chunk_id for c in result.citations}) == len(result.citations)
            for citation in result.citations:
                assert citation.kb_id == kb_id
                assert citation.doc_id == doc_id
                assert citation.metadata.get("source_spans")
                assert citation.retrieval_score is not None
                assert citation.fusion_score is not None
                assert citation.rerank_score is not None
                assert 1 <= citation.page_start <= citation.page_end
                print(
                    "CITATION", citation.citation_id,
                    "CHUNK", citation.chunk_id,
                    "PAGES", citation.page_start, citation.page_end,
                    "SECTION", citation.section,
                )

            actual_markers = set(re.findall(r"\[(\d+)\]", result.answer))
            expected_markers = {str(c.citation_id) for c in result.citations}
            assert actual_markers == expected_markers
            print("RAG_CITATIONS_PASS")
            print("RAG_EVIDENCE_STRUCTURE_PASS")
        else:
            assert not result.citations
            assert result.answer == "当前检索到的证据不足以回答该问题。"
            print("RAG_INSUFFICIENT_STRUCTURE_PASS")

    except AssertionError:
        print("STAGE22_STRUCTURE_FAILED: 返回结果未满足引用或回答结构约束。")
        return 1

    print("ANSWER_BEGIN")
    print(result.answer)
    print("ANSWER_END")
    print("RAG_TOTAL_SECONDS =", round(perf_counter() - start, 2))
    print("STAGE22_STRUCTURE_PASS")
    print("注意：结构通过不代表回答事实正确；还需要核对原文证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
