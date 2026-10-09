"""无需文件、数据库、Redis 或模型服务的统一解析契约测试。"""

import json
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.parsers import BaseParser
from app.schemas.parsed_document import (
    BlockType,
    FileType,
    ParsedBlock,
    ParsedDocument,
)

DOC_ID = UUID("7f2044ca-2041-42ce-977d-40bf5c79ed40")
BLOCK_ID = UUID("710c7509-4d2c-4c2a-9681-532013732546")
SECOND_BLOCK_ID = UUID("bbbd50d0-c7bb-4b4d-b2e6-0c1ead1e159c")


def block_payload(**changes) -> dict:
    """提供有效块字典，并允许单个测试替换待验证字段。"""

    payload = {
        "block_id": str(BLOCK_ID),
        "page": 1,
        "block_type": "text",
        "text": "  中文正文\n第二行  ",
        "source": "native_parser",
        "confidence": 1.0,
    }
    payload.update(changes)
    return payload


def document_payload(**changes) -> dict:
    """提供符合上传文档元信息的解析结果，不访问真实文件。"""

    payload = {
        "doc_id": str(DOC_ID),
        "file_name": "示例.pdf",
        "file_type": "pdf",
        "blocks": [block_payload()],
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize("file_type", get_args(FileType))
@pytest.mark.parametrize("block_type", get_args(BlockType))
def test_all_format_and_block_labels_share_the_contract(file_type, block_type):
    """七种文件标识和五种块类型都使用同一模型，不代表真实 Parser 已实现。"""

    parsed = ParsedDocument.model_validate(
        document_payload(
            file_type=file_type,
            file_name=f"示例.{file_type}",
            blocks=[block_payload(block_type=block_type)],
        )
    )
    assert parsed.file_type == file_type
    assert parsed.blocks[0].block_type == block_type
    assert parsed.title == ""
    assert parsed.blocks[0].section == ""
    assert parsed.blocks[0].bbox is None


def test_json_round_trip_preserves_order_evidence_and_original_text():
    """JSON 往返应保留中文原文、UUID、块顺序和原始页面坐标。"""

    parsed = ParsedDocument.model_validate(
        document_payload(
            title="文档标题",
            blocks=[
                block_payload(bbox=[0, 1, 20, 30], section="第一节"),
                block_payload(
                    block_id=str(SECOND_BLOCK_ID),
                    page=2,
                    source="volcengine_ocr",
                    confidence=0.85,
                    block_type="image_text",
                ),
            ],
        )
    )
    payload = parsed.model_dump(mode="json")
    assert set(payload) == {"doc_id", "file_name", "file_type", "title", "blocks"}
    assert set(payload["blocks"][0]) == {
        "block_id",
        "page",
        "section",
        "block_type",
        "text",
        "bbox",
        "source",
        "confidence",
    }
    assert payload["doc_id"] == str(DOC_ID)
    assert payload["blocks"][0]["text"] == "  中文正文\n第二行  "
    assert payload["blocks"][0]["bbox"] == [0.0, 1.0, 20.0, 30.0]
    assert [block["block_id"] for block in payload["blocks"]] == [
        str(BLOCK_ID),
        str(SECOND_BLOCK_ID),
    ]
    assert json.loads(parsed.model_dump_json()) == payload
    assert ParsedDocument.model_validate_json(parsed.model_dump_json()) == parsed


def test_unknown_confidence_empty_content_and_empty_document_are_explicit():
    """未知置信度、空正文和待 OCR 空结果允许表达，但不推导成功状态。"""

    payload = block_payload(text="")
    payload.pop("confidence")
    block = ParsedBlock.model_validate(payload)
    assert block.confidence is None
    assert block.text == ""
    assert ParsedDocument.model_validate(document_payload(blocks=[])).blocks == []
    assert "status" not in ParsedDocument.model_fields


@pytest.mark.parametrize("page", [None, 0, -1, True, False, "1", 1.0])
def test_invalid_or_unknown_page_is_rejected(page):
    """页码必须是从 1 开始的整数，与现有非空数据库约束一致。"""

    with pytest.raises(ValidationError):
        ParsedBlock.model_validate(block_payload(page=page))


@pytest.mark.parametrize(
    "bbox",
    [
        [],
        [0, 1, 2],
        [0, 1, 2, 3, 4],
        [2, 0, 1, 3],
        [0, 3, 2, 1],
        [0, 0, float("nan"), 1],
        [0, 0, float("inf"), 1],
        [0, float("-inf"), 1, 1],
        [False, 0, 1, 1],
        ["0", 0, 1, 1],
        {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
        (0, 0, 1, 1),
    ],
)
def test_invalid_bbox_is_rejected(bbox):
    """拒绝维度错误、方向颠倒、非有限数值和不统一的坐标表达。"""

    with pytest.raises(ValidationError):
        ParsedBlock.model_validate(block_payload(bbox=bbox))


@pytest.mark.parametrize("bbox", [None, [0, 0, 0, 0], [-2, -1, 3, 4]])
def test_bbox_without_page_geometry_does_not_invent_bounds(bbox):
    """契约只校验矩形方向，原始坐标是否超出页面由具体 Parser 检查。"""

    assert ParsedBlock.model_validate(block_payload(bbox=bbox)).bbox == bbox


@pytest.mark.parametrize(
    "confidence", [-0.1, 1.1, True, "0.8", float("nan"), float("inf"), float("-inf")]
)
def test_invalid_confidence_is_rejected(confidence):
    """拒绝越界、隐式类型转换和非有限置信度。"""

    with pytest.raises(ValidationError):
        ParsedBlock.model_validate(block_payload(confidence=confidence))


@pytest.mark.parametrize("confidence", [None, 0, 1, 0.75])
def test_confidence_boundaries_and_unknown_value_are_allowed(confidence):
    """允许未知置信度和闭区间内的数值，包括两个端点。"""

    assert (
        ParsedBlock.model_validate(block_payload(confidence=confidence)).confidence
        == confidence
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", ""),
        ("source", " \n"),
        ("source", "x" * 65),
        ("source", 1),
        ("block_type", "chunk"),
        ("text", None),
        ("text", 123),
        ("section", None),
        ("block_id", "invalid"),
        ("block_id", None),
    ],
)
def test_invalid_block_metadata_is_rejected(field, value):
    """来源、块类型、正文和标识均需明确且符合约定。"""

    with pytest.raises(ValidationError):
        ParsedBlock.model_validate(block_payload(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_name", ""),
        ("file_name", " \n"),
        ("file_name", "x" * 256),
        ("file_name", 123),
        ("file_type", "PDF"),
        ("file_type", "html"),
        ("title", None),
        ("doc_id", "invalid"),
        ("blocks", None),
        ("blocks", ()),
    ],
)
def test_invalid_document_metadata_is_rejected(field, value):
    """顶层结构禁止无效文件元信息和非列表的块集合。"""

    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(document_payload(**{field: value}))


@pytest.mark.parametrize("field", ["block_id", "page", "block_type", "text", "source"])
def test_required_block_fields_cannot_be_omitted(field):
    """必填字段不能由契约猜测或自动生成，尤其不能随机创建块 ID。"""

    payload = block_payload()
    payload.pop(field)
    with pytest.raises(ValidationError):
        ParsedBlock.model_validate(payload)


@pytest.mark.parametrize("field", ["doc_id", "file_name", "file_type", "blocks"])
def test_required_document_fields_cannot_be_omitted(field):
    """缺失核心输出字段时立即失败，不静默返回空文档。"""

    payload = document_payload()
    payload.pop(field)
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(payload)


def test_duplicate_ids_and_extra_fields_are_rejected():
    """同文档块 ID 必须唯一，顶层和块内都不能混入 RAG 或私有字段。"""

    for payload in (
        document_payload(blocks=[block_payload(), block_payload()]),
        document_payload(answer="不属于解析结果"),
        document_payload(blocks=[block_payload(embedding=[0.1])]),
    ):
        with pytest.raises(ValidationError):
            ParsedDocument.model_validate(payload)


class StubParser(BaseParser):
    """仅用于契约验收的替身，不读取或伪装解析真实文件。"""

    def __init__(self, result: ParsedDocument):
        """保存待返回结果，便于验证公共入口的结构和归属检查。"""

        self.result = result
        self.last_call = None

    def _parse(
        self, *, doc_id: UUID, file_path: Path, file_name: str, file_type: FileType
    ) -> ParsedDocument:
        """记录调用参数并返回测试结果，不触碰文件或基础服务。"""

        self.last_call = (doc_id, file_path, file_name, file_type)
        return self.result


def invoke_parser(parser: BaseParser) -> ParsedDocument:
    """用固定元信息调用公共入口，路径仅作为参数而不对应真实原件。"""

    return parser.parse(
        doc_id=DOC_ID,
        file_path=Path("unused.pdf"),
        file_name="示例.pdf",
        file_type="pdf",
    )


def test_base_parser_is_abstract_and_public_entry_validates_output():
    """基类不能直接实例化，子类经公共入口返回相同契约并保留输入参数。"""

    with pytest.raises(TypeError):
        BaseParser()
    parser = StubParser(ParsedDocument.model_validate(document_payload()))
    assert invoke_parser(parser) == parser.result
    assert parser.last_call == (DOC_ID, Path("unused.pdf"), "示例.pdf", "pdf")


@pytest.mark.parametrize(
    ("field", "value"),
    [("doc_id", SECOND_BLOCK_ID), ("file_name", "其他.pdf"), ("file_type", "txt")],
)
def test_parser_output_cannot_change_document_identity(field, value):
    """结构虽合法，返回的文档 ID、文件名或类型与输入不符仍必须失败。"""

    parser = StubParser(
        ParsedDocument.model_validate(document_payload(**{field: value}))
    )
    with pytest.raises(ValueError, match="输出.*不一致"):
        invoke_parser(parser)


@pytest.mark.parametrize("mutation", ["page", "duplicate_id", "extra_field"])
def test_parser_revalidates_models_mutated_after_construction(mutation):
    """返回模型实例也需要重新校验，防止构造后修改污染输出边界。"""

    parsed = ParsedDocument.model_validate(document_payload())
    if mutation == "page":
        parsed.blocks[0].page = 0
    elif mutation == "duplicate_id":
        parsed.blocks.append(parsed.blocks[0])
    else:
        parsed.blocks.append(
            {**block_payload(block_id=str(SECOND_BLOCK_ID)), "chunk": 1}
        )
    with pytest.raises(ValidationError):
        invoke_parser(StubParser(parsed))


def test_parser_errors_are_not_converted_to_fake_success():
    """具体解析错误原样交给上层任务，不返回虚构的成功文档。"""

    class FailingParser(StubParser):
        """模拟读取原件失败的 Parser。"""

        def _parse(
            self, *, doc_id: UUID, file_path: Path, file_name: str, file_type: FileType
        ) -> ParsedDocument:
            """抛出原件访问异常，供公共入口透传行为验收。"""

            raise OSError("原件读取失败")

    with pytest.raises(OSError, match="原件读取失败"):
        invoke_parser(FailingParser(ParsedDocument.model_validate(document_payload())))
