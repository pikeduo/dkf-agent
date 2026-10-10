"""纯 Python 原生文本解析验收，仅在 pytest 临时目录生成样本。"""

import codecs
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.parsers.common import NoTextError, OCRRequiredError, ParserError, make_block
from app.parsers.text import MarkdownParser, TextParser

DOC_ID = UUID("7f2044ca-2041-42ce-977d-40bf5c79ed40")


def parse_sample(tmp_path: Path, content: bytes, file_type: str = "txt", doc_id=DOC_ID):
    """写入临时原件并调用公共入口，不接触上传目录、数据库或队列。"""

    path = tmp_path / f"sample.{file_type}"
    path.write_bytes(content)
    parser = MarkdownParser() if file_type == "md" else TextParser()
    return parser.parse(
        doc_id=doc_id,
        file_path=path,
        file_name=f"示例.{file_type}",
        file_type=file_type,
    )


@pytest.mark.parametrize(
    "encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32", "gbk", "gb18030"]
)
def test_text_encoding_and_paragraph_evidence(tmp_path, encoding):
    """中文常见编码生成相同文本块，原文缩进、换行和逻辑页信息保持一致。"""

    text = "  第一段中文\r\n段内换行  \r\n\r\n第二段。"
    parsed = parse_sample(tmp_path, text.encode(encoding))
    assert parsed.doc_id == DOC_ID
    assert parsed.file_name == "示例.txt"
    assert parsed.file_type == "txt"
    assert parsed.title == ""
    assert [block.text for block in parsed.blocks] == [
        "  第一段中文\r\n段内换行  ",
        "第二段。",
    ]
    for block in parsed.blocks:
        assert block.page == 1
        assert block.block_type == "text"
        assert block.section == ""
        assert block.source == "native_parser"
        assert block.confidence == 1.0
        assert block.bbox is None


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_blank_line_separators_and_duplicate_text_remain_separate(tmp_path, newline):
    """不同平台换行都能分段，重复正文不能因为文本相同而丢失块位置。"""

    parsed = parse_sample(tmp_path, f"相同正文{newline} \t{newline}相同正文".encode())
    assert [block.text for block in parsed.blocks] == ["相同正文", "相同正文"]
    assert parsed.blocks[0].block_id != parsed.blocks[1].block_id


@pytest.mark.parametrize("content", [b"", b" \n\t\r\n", codecs.BOM_UTF8])
@pytest.mark.parametrize("file_type", ["txt", "md"])
def test_empty_native_text_is_not_success(tmp_path, content, file_type):
    """空文件、只有空白和单独 BOM 都必须报告无有效原生文本。"""

    with pytest.raises(NoTextError, match="没有可用原生文本"):
        parse_sample(tmp_path, content, file_type)


@pytest.mark.parametrize(
    "content", [b"\xff", codecs.BOM_UTF16_LE + b"\x00", b"text\x00binary", b"text\x01"]
)
def test_bad_encoding_and_binary_controls_fail_safely(tmp_path, content):
    """非法编码或二进制控制字符不得用替换字符掩盖，错误不包含文件路径。"""

    with pytest.raises(ParserError) as error:
        parse_sample(tmp_path, content)
    assert str(tmp_path) not in str(error.value)
    assert "sample.txt" not in str(error.value)


def test_markdown_heading_hierarchy_and_raw_body(tmp_path):
    """常用标题生成 title 块与章节层级，正文列表、表格和公式语法不被改写。"""

    text = (
        "# 总标题\n\n首段正文\n段内换行\n\n## 二级标题 ##\n\n"
        "- 列表项\n| A | B |\n|---|---|\n$x+y$\n\n# 新章\n下一章。"
    )
    parsed = parse_sample(tmp_path, text.encode(), "md")
    assert parsed.title == "总标题"
    assert [block.block_type for block in parsed.blocks] == [
        "title",
        "text",
        "title",
        "text",
        "title",
        "text",
    ]
    assert [block.section for block in parsed.blocks] == [
        "总标题",
        "总标题",
        "总标题 / 二级标题",
        "总标题 / 二级标题",
        "新章",
        "新章",
    ]
    assert parsed.blocks[1].text == "首段正文\n段内换行"
    assert parsed.blocks[3].text == "- 列表项\n| A | B |\n|---|---|\n$x+y$"


def test_setext_headings_and_skipped_levels(tmp_path):
    """下划线标题及跨级标题仍保持可追溯的章节，不生成不存在的中间标题。"""

    parsed = parse_sample(
        tmp_path,
        "总标题\n=====\n\n正文\n\n子标题\n-----\n\n#### 跨级\n内容".encode(),
        "md",
    )
    assert parsed.title == "总标题"
    assert [block.text for block in parsed.blocks if block.block_type == "title"] == [
        "总标题",
        "子标题",
        "跨级",
    ]
    assert parsed.blocks[-1].section == "总标题 / 子标题 / 跨级"


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_code_fence_preserves_false_heading_and_blank_lines(tmp_path, fence):
    """围栏内井号和空行保留为代码原文，不改写为标题或段落分隔。"""

    code = f"{fence}python\n# 不是标题\n\nprint(1)\n{fence}"
    parsed = parse_sample(tmp_path, f"# 真标题\n\n{code}\n\n普通正文".encode(), "md")
    assert [block.block_type for block in parsed.blocks] == ["title", "text", "text"]
    assert parsed.blocks[1].text == code
    assert parsed.blocks[2].section == "真标题"


def test_markdown_without_headings_and_unclosed_code_is_preserved(tmp_path):
    """无标题或未闭合围栏仍保留原生文本，不额外猜测标题。"""

    parsed = parse_sample(tmp_path, "普通内容\n\n```\n# 原文\n".encode(), "md")
    assert parsed.title == ""
    assert [block.text for block in parsed.blocks] == ["普通内容", "```\n# 原文"]
    assert all(block.block_type == "text" for block in parsed.blocks)


@pytest.mark.parametrize("file_type", ["txt", "md"])
def test_block_identity_is_stable_and_scoped_to_document(tmp_path, file_type):
    """重复解析同一原件保留 UUID，不同文档不能共用块标识。"""

    content = "# 标题\n\n正文\n\n正文".encode()
    first = parse_sample(tmp_path, content, file_type)
    assert parse_sample(tmp_path, content, file_type) == first
    other = parse_sample(tmp_path, content, file_type, doc_id=uuid4())
    assert not {block.block_id for block in first.blocks} & {
        block.block_id for block in other.blocks
    }


def test_parser_checks_format_and_does_not_hide_os_error(tmp_path):
    """错误格式明确拒绝，文件访问错误保留给上层任务重试。"""

    for parser, file_type in [(TextParser(), "md"), (MarkdownParser(), "txt")]:
        with pytest.raises(ParserError):
            parser.parse(
                doc_id=DOC_ID,
                file_path=tmp_path / "unused",
                file_name="示例",
                file_type=file_type,
            )
    with pytest.raises(FileNotFoundError):
        TextParser().parse(
            doc_id=DOC_ID,
            file_path=tmp_path / "missing.txt",
            file_name="缺失.txt",
            file_type="txt",
        )


def test_factory_and_parser_registry_are_explicit():
    """工厂保留标准来源和稳定标识，分派入口拒绝图片及未支持格式。"""

    from app.parsers.registry import get_parser

    with pytest.raises(ValueError):
        make_block(DOC_ID, 0, page=1, block_type="text", text="正文")
    assert isinstance(get_parser("txt"), TextParser)
    assert isinstance(get_parser("md"), MarkdownParser)
    assert type(get_parser("docx")).__name__ == "DocxParser"
    assert type(get_parser("pdf")).__name__ == "PdfParser"
    for file_type in ["jpg", "jpeg", "png"]:
        with pytest.raises(OCRRequiredError):
            get_parser(file_type)
    with pytest.raises(ParserError):
        get_parser("html")
