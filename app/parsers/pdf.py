"""仅解析 PDF 原生文本，保留物理页码和未旋转页面的点数坐标。"""

from pathlib import Path
from uuid import UUID

import pymupdf

from app.parsers.base import BaseParser
from app.parsers.common import NoTextError, OCRRequiredError, ParserError, make_block
from app.schemas.parsed_document import FileType, ParsedBlock, ParsedDocument


class PdfParser(BaseParser):
    """输出原生文本块；不猜测标题，也不把需要 OCR 的页面当作空白跳过。"""

    def _parse(
        self,
        *,
        doc_id: UUID,
        file_path: Path,
        file_name: str,
        file_type: FileType,
    ) -> ParsedDocument:
        """按物理页抽取文本；格式问题安全失败，文件访问错误交由上层重试。"""

        if file_type != "pdf":
            raise ParserError("PDF Parser 只支持 pdf 文件类型")

        # 先由 Python 读取原件，保留 OSError 供任务层判断临时错误；
        # 使用内存流也避免底层解析异常包含本机路径。
        content = file_path.read_bytes()
        blocks: list[ParsedBlock] = []
        try:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                if document.needs_pass or (document.metadata or {}).get("encryption"):
                    raise ParserError("不支持加密 PDF，请上传未加密原件")
                if document.is_repaired:
                    raise ParserError("PDF 结构损坏，请重新导出完整原件")

                # 图片元信息不是原生正文，不让它混入 text Block。
                flags = pymupdf.TEXTFLAGS_BLOCKS & ~pymupdf.TEXT_PRESERVE_IMAGES
                for page_index, page in enumerate(document, start=1):
                    text_blocks = [
                        block
                        for block in page.get_text("blocks", sort=True, flags=flags)
                        if block[6] == 0 and block[4].strip()
                    ]
                    if not text_blocks:
                        if (
                            page.get_image_info()
                            or page.get_drawings()
                            or page.first_annot is not None
                        ):
                            # 整份文档失败，不能把混合 PDF 的部分原生页标为完整解析。
                            raise OCRRequiredError(
                                f"PDF 第 {page_index} 页没有原生文本，"
                                "存在图像或图形，需 OCR"
                            )
                        continue

                    for block in text_blocks:
                        blocks.append(
                            make_block(
                                doc_id,
                                len(blocks) + 1,
                                page=page_index,
                                block_type="text",
                                text=block[4].rstrip("\n"),
                                bbox=[float(value) for value in block[:4]],
                            )
                        )
        except (pymupdf.FileDataError, RuntimeError, ValueError):
            raise ParserError("PDF 无法解析，文件可能损坏或不受支持") from None

        if not blocks:
            raise NoTextError("PDF 未包含可提取的原生文本")
        return ParsedDocument(
            doc_id=doc_id,
            file_name=file_name,
            file_type=file_type,
            blocks=blocks,
        )
