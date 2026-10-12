"""纯内存切片用例，不创建文件、不调用任何文档 Parser、模型或外部服务。"""

from dataclasses import replace
from uuid import uuid4
import pytest

from app.core.config import Settings
from app.services.chunk.splitter import ChunkError, ChunkOptions, SourceBlock, build_chunks, token_ranges


def block(index, text, kind="text", page=1, section="章节"):
    """构造已有 Block 的内存快照，不生成 PDF、Office、图片或验收文件。"""

    return SourceBlock(uuid4(), index, page, section, kind, text, "native_parser")


def chunks(blocks, options=None, file_type="pdf", doc_id=None, kb_id=None):
    """使用固定输入调用纯切片函数，不经过解析和消息队列。"""

    return build_chunks(doc_id or uuid4(), kb_id or uuid4(), file_type, blocks, options)


def assert_evidence(result, blocks):
    """校验每段证据可逐字符反查，且所有非空 Block 的主内容覆盖恰好一次。"""

    lookup = {str(source.block_id): source for source in blocks}
    content = {str(source.block_id): [] for source in blocks if source.text.strip()}
    for chunk in result:
        for span in chunk.metadata["source_spans"]:
            source = lookup[span["block_id"]]
            expected = source.text[span["start_char"]:span["end_char"]]
            assert expected == chunk.text[span["chunk_start_char"]:span["chunk_end_char"]]
            assert span["page"] == source.page
            if span["role"] == "content":
                content[span["block_id"]].append((span["start_char"], span["end_char"]))
    for identifier, spans in content.items():
        offset = 0
        for start, end in spans:
            assert start == offset
            offset = end
        assert offset == len(lookup[identifier].text)


def test_titles_and_sections_are_boundaries_with_heading_context():
    """标题不与上一章节混合，长正文切片复用标题，不跨章节保留 overlap。"""

    sources = [block(1, "第一标题", "title"), block(2, "甲" * 1800),
               block(3, "第二标题", "title"), block(4, "乙" * 800),
               block(5, "新 Sheet 内容", section="第二Sheet")]
    result = chunks(sources)
    assert len(result) > 4
    for chunk in result:
        assert not ("甲" in chunk.text and "第二标题" in chunk.text)
        assert not ("乙" in chunk.text and "新 Sheet" in chunk.text)
        if "甲" in chunk.text:
            assert chunk.text.startswith("第一标题")
        if "乙" in chunk.text:
            assert chunk.text.startswith("第二标题")
    assert_evidence(result, sources)


def test_paragraph_boundary_overlap_and_character_coverage():
    """短段落整体放入，长段落按长度分割；重叠为软目标，原文空白和尾部不丢失。"""

    sources = [block(1, "甲" * 350 + "\n\n" + "乙" * 350 + "\n\n" + "丙" * 1200 + "\n尾部")]
    result = chunks(sources)
    assert "甲" * 350 in result[0].text and "乙" not in result[0].text
    overlaps = [span for chunk in result for span in chunk.metadata["source_spans"] if span["role"] == "overlap"]
    assert overlaps
    for chunk in result:
        assert chunk.metadata["estimated_token_count"] <= 600
        assert chunk.metadata["tokenizer"] == "unicode-estimate-v1"
    assert_evidence(result, sources)


def test_table_and_formula_remain_whole_and_formula_keeps_explanation():
    """完整表格、公式与邻近解释不被拆散；保留原生 XLSX 公式，不计算、不改写。"""

    table = "|项目|公式|\n|收入|=SUM(A1:A10)|\n" + "表" * 900
    sources = [block(1, "本节", "title"), block(2, table, "table"),
               block(3, "公式前解释"), block(4, "=(B2-C2)/C2*100%", "formula"), block(5, "公式后解释")]
    result = chunks(sources, file_type="xlsx")
    tables = [chunk for chunk in result if table in chunk.text]
    assert len(tables) == 1 and tables[0].metadata["oversized"]
    formula = next(chunk for chunk in result if sources[3].text in chunk.text)
    assert "公式前解释" in formula.text and "公式后解释" in formula.text
    assert "=SUM(A1:A10)" in tables[0].text
    assert all(chunk.metadata["page_semantics"] == "logical" for chunk in result)
    assert_evidence(result, sources)


def test_oversized_formula_and_following_text_keep_heading():
    """超长公式完整保留且标记超目标，后续正文仍能携带标题上下文。"""

    sources = [block(1, "公式章节", "title"), block(2, "变量" * 600, "formula"), block(3, "后续解释")]
    result = chunks(sources)
    assert any(chunk.metadata["oversized"] and sources[1].text in chunk.text for chunk in result)
    assert result[-1].text.startswith("公式章节")
    assert_evidence(result, sources)


def test_pages_use_source_evidence_not_sorting_or_fake_coordinates():
    """跨页范围来自原证据，切片只遵守 block_index，不按页码或 bbox 全局排序。"""

    sources = [block(1, "第一页正文", page=1), block(2, "第二页正文", page=2), block(3, "返回第一页的原序", page=1)]
    result = chunks(sources)
    assert len(result) == 1
    assert (result[0].page_start, result[0].page_end) == (1, 2)
    assert [span["page"] for span in result[0].metadata["source_spans"]] == [1, 2, 1]
    assert all(span["bbox"] is None for span in result[0].metadata["source_spans"])
    assert_evidence(result, sources)


def test_stable_ids_change_only_with_identity_input_or_options():
    """同输入重试稳定，参数/正文/来源变化或跨文档复用必须生成不同 Chunk ID。"""

    doc_id, kb_id = uuid4(), uuid4()
    sources = [block(1, "正文" * 900)]
    first = chunks(sources, doc_id=doc_id, kb_id=kb_id)
    assert first == chunks(sources, doc_id=doc_id, kb_id=kb_id)
    identifiers = {chunk.chunk_id for chunk in first}
    variants = [chunks(sources, ChunkOptions(700, 100), doc_id=doc_id, kb_id=kb_id),
                chunks([replace(sources[0], text="改变" + sources[0].text)], doc_id=doc_id, kb_id=kb_id),
                chunks(sources, doc_id=uuid4(), kb_id=kb_id)]
    assert all(not identifiers.intersection(chunk.chunk_id for chunk in variant) for variant in variants)
    assert_evidence(first, sources)


@pytest.mark.parametrize("indexes", [[None], [0], [2], [1, 1]])
def test_unknown_or_duplicate_reading_order_safely_fails(indexes):
    """历史未知序号、重复序号和缺失序号不能以 UUID 或页坐标伪造阅读顺序。"""

    with pytest.raises(ChunkError, match="CHUNK_SOURCE_ORDER"):
        chunks([block(index, "有正文") for index in indexes])


def test_long_heading_and_mixed_languages_are_not_truncated():
    """长标题、英文长词、中文与空白保留完整字符证据，不依赖模型下载。"""

    sources = [block(1, "标题" * 550, "title"), block(2, "abcdefghij" * 700 + " 中文\n\n尾部")]
    result = chunks(sources)
    assert len(result) > 2
    assert_evidence(result, sources)
    assert len(token_ranges("中文 abcdefgh 1234")) == 5


@pytest.mark.parametrize("sources", [[], [block(1, " \n\t")]])
def test_empty_source_is_not_success(sources):
    """无正文不能生成成功的空切片列表。"""

    with pytest.raises(ChunkError, match="CHUNK_EMPTY"):
        chunks(sources)


def test_configuration_ranges_are_explicit():
    """部署配置限定初始范围，未知切片算法不能沿用旧检查点。"""

    assert ChunkOptions().size == 600
    with pytest.raises(ValueError):
        Settings(_env_file=None, chunk_overlap=101)
    with pytest.raises(ChunkError, match="CHUNK_CONFIG_INVALID"):
        ChunkOptions(tokenizer="unknown").validate()
