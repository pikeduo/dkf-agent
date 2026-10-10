"""只在内存生成 MinerU Mock 原件和结果，不上传真实云端、不写 Parse Cache。"""

import json
from io import BytesIO
from zipfile import ZipFile

import pymupdf


def pdf_bytes(pages=2):
    """生成真实可读取的 PDF，供页数、原件 Hash 及坐标测试使用。"""

    with pymupdf.open() as pdf:
        for _ in range(pages):
            page = pdf.new_page(width=600, height=800)
            page.insert_text((30, 40), "MinerU cloud mock fixture")
        return pdf.tobytes()


def png_bytes():
    """生成 600×800 像素的真实 PNG，不依赖 Pillow 或额外模型。"""

    with pymupdf.open(stream=pdf_bytes(1), filetype="pdf") as pdf:
        return pdf[0].get_pixmap().tobytes("png")


def content_items():
    """返回官方 ContentListV1 字段形状的五类块，包含标题、表格、公式和图片文字。"""

    return [
        {
            "type": "text",
            "text": "测试标题",
            "text_level": 1,
            "page_idx": 0,
            "bbox": [0, 0, 500, 100],
        },
        {"type": "text", "text": "正文证据", "page_idx": 0},
        {
            "type": "table",
            "table_caption": ["测试表"],
            "table_body": "<table><tr><td>42</td></tr></table>",
            "table_footnote": ["单位：元"],
            "page_idx": 0,
        },
        {"type": "equation", "text": "$$E=mc^2$$", "page_idx": 0},
        {
            "type": "image",
            "image_caption": ["示意图"],
            "content": "图片中的实际文字",
            "image_footnote": ["图注"],
            "page_idx": 0,
        },
    ]


def result_zip(items=None, name="result/sample_content_list.json"):
    """生成只有结构化内容和预览 Markdown 的 ZIP，所有字节仅供测试使用。"""

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(name, json.dumps(content_items() if items is None else items))
        archive.writestr("result/full.md", "不把此预览作为唯一解析输入")
    return buffer.getvalue()
