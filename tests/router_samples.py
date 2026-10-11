"""仅供离线路由测试的结构样本；不冒充真实 MinerU 人工验收结果。"""

from io import BytesIO
from zipfile import ZipFile

import pymupdf
from mineru_samples import png_bytes
from office_samples import office_bytes


def office_with_part(kind, name, content):
    """向有效 OOXML 内存包加入或替换结构部件，用于定位预检理由而非视觉真值。"""

    buffer = BytesIO()
    with ZipFile(BytesIO(office_bytes(kind))) as source, ZipFile(buffer, "w") as target:
        for item in source.infolist():
            if item.filename != name:
                target.writestr(item.filename, source.read(item.filename))
        target.writestr(name, content)
    return buffer.getvalue()


def docx_structure(tag):
    """在真实 DOCX 正文加入图片、数学或文本框结构，保持普通段落可读。"""

    with ZipFile(BytesIO(office_bytes("docx"))) as source:
        xml = source.read("word/document.xml").decode()
    xml = xml.replace("</w:body>", tag + "</w:body>")
    return office_with_part("docx", "word/document.xml", xml)


def router_pdf(kind="simple"):
    """构造物理页面明确的文本、扫描、混合、多栏、小 logo 和绘图 PDF。"""

    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        if kind != "scan":
            page.insert_text(
                (40, 50), "Native paragraph with enough readable characters."
            )
        if kind in {"scan", "same_page"}:
            rect = page.rect if kind == "scan" else pymupdf.Rect(50, 120, 400, 550)
            page.insert_image(rect, stream=png_bytes())
        elif kind == "logo":
            page.insert_image(pymupdf.Rect(550, 20, 560, 30), stream=png_bytes())
        elif kind == "columns":
            page.insert_text((40, 200), "Left column.")
            page.insert_text((350, 200), "Right column.")
        elif kind == "drawing":
            page.draw_rect(pymupdf.Rect(40, 100, 500, 600))
        elif kind == "mixed_pages":
            page = document.new_page(width=600, height=800)
            page.insert_image(page.rect, stream=png_bytes())
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 50), "Third page native text.")
        return document.tobytes()
