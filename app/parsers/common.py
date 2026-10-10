"""原生解析器共用的安全错误与确定性 Block 构造，不依赖数据库。"""

from uuid import UUID, uuid5

from app.schemas.parsed_document import BlockType, ParsedBlock


class ParserError(Exception):
    """不可重试的格式或内容错误，消息必须是可对外展示的安全中文说明。"""


class NoTextError(ParserError):
    """原件存在但没有可用原生文本，不能假装解析或入库成功。"""


class OCRRequiredError(ParserError):
    """原件包含必须通过 OCR 获取的内容，本阶段不执行或伪造 OCR。"""


def make_block(
    doc_id: UUID,
    index: int,
    *,
    page: int,
    block_type: BlockType,
    text: str,
    section: str = "",
    bbox: list[float] | None = None,
) -> ParsedBlock:
    """按文档、版本及块位置生成稳定 ID，重试同一原件不会随机创建新标识。"""

    if index < 1:
        raise ValueError("原生块序号必须从 1 开始")
    return ParsedBlock(
        block_id=uuid5(doc_id, f"native-v1:{page}:{index}:{block_type}"),
        page=page,
        section=section,
        block_type=block_type,
        text=text,
        bbox=bbox,
        source="native_parser",
        confidence=1.0,
    )
