"""统一格式能力和原件容器校验；不解析正文、不执行 Office 宏或外部链接。"""

from pathlib import PurePosixPath
from struct import error as StructError
from typing import BinaryIO, Literal, get_args
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

import olefile

FileType = Literal[
    "pdf",
    "doc",
    "docx",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "csv",
    "txt",
    "md",
    "jpg",
    "jpeg",
    "png",
]
FILE_TYPES = frozenset(get_args(FileType))
OFFICE_TYPES = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})
MINERU_TYPES = OFFICE_TYPES | {"pdf", "jpg", "jpeg", "png"}
# 新云分页输入预留20页余量；平台硬上限及历史已收费200页账本不改写。
MINERU_SAFE_PAGE_COUNT = 180
OOXML_PARTS = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}


class FileFormatError(ValueError):
    """原件内容与声明格式不匹配；只能向外返回安全的固定错误。"""


def validate_ooxml(stream: BinaryIO, file_type: str) -> None:
    """检查 OOXML 类型主部件、重复项与解压规模，不向磁盘解压或读取外部资源。"""

    with ZipFile(stream) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (
            len(infos) > 10000
            or sum(info.file_size for info in infos) > 256 * 1024**2
            or len(set(names)) != len(names)
            or "[Content_Types].xml" not in names
            or {part for part in OOXML_PARTS.values() if part in names}
            != {OOXML_PARTS[file_type]}
        ):
            raise FileFormatError("Office 容器结构或规模无效")
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if (
                path.is_absolute()
                or ".." in path.parts
                or ":" in info.filename
                or info.flag_bits & 1
            ):
                raise FileFormatError("Office 容器包含不安全或加密部件")
        # 读取必要部件验证 CRC 和压缩数据，不能仅接受伪造的中央目录文件名。
        for name in ("[Content_Types].xml", OOXML_PARTS[file_type]):
            if archive.getinfo(name).file_size > 64 * 1024**2 or not archive.read(name):
                raise FileFormatError("Office 主部件为空或超量")


def validate_ole(stream: BinaryIO, file_type: str) -> None:
    """严格读取 CFB 目录，以根级 Office 主流区分旧 DOC/XLS/PPT，不执行二进制内容。"""

    if not olefile.isOleFile(stream):
        raise FileFormatError("原件不是 OLE 容器")
    stream.seek(0)
    with olefile.OleFileIO(stream, raise_defects=olefile.DEFECT_INCORRECT) as container:
        roots = {
            parts[0].casefold(): parts[0]
            for parts in container.listdir()
            if len(parts) == 1
        }
        matches = {
            kind
            for kind, markers in {
                "doc": {"worddocument"},
                "xls": {"workbook", "book"},
                "ppt": {"powerpoint document"},
            }.items()
            if markers & roots.keys()
        }
        if matches != {file_type} or "encryptedpackage" in roots:
            raise FileFormatError("OLE 主流与 Office 扩展名不匹配")
        marker = next(
            name
            for key, name in roots.items()
            if key in {"worddocument", "workbook", "book", "powerpoint document"}
        )
        with container.openstream(marker) as content:
            if container.get_size(marker) == 0 or not content.read(1):
                raise FileFormatError("Office 主流为空或损坏")


def validate_file_content(stream: BinaryIO, file_type: str) -> None:
    """按实际签名和容器主部件检查格式，结束时复位流；文本编码由 Native Parser 校验。"""

    try:
        stream.seek(0)
        header = stream.read(1024)
        if file_type not in FILE_TYPES:
            raise FileFormatError("文件格式不受支持")
        if file_type in OOXML_PARTS:
            stream.seek(0)
            validate_ooxml(stream, file_type)
        elif file_type in {"doc", "xls", "ppt"}:
            stream.seek(0)
            validate_ole(stream, file_type)
        elif file_type == "pdf" and b"%PDF-" not in header:
            raise FileFormatError("PDF 标识无效")
        elif file_type == "png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise FileFormatError("PNG 标识无效")
        elif file_type in {"jpg", "jpeg"} and not header.startswith(b"\xff\xd8\xff"):
            raise FileFormatError("JPEG 标识无效")
    except (
        BadZipFile,
        UnicodeError,
        ValueError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        StructError,
        ZlibError,
    ):
        raise FileFormatError("文件内容与扩展名不匹配或格式损坏") from None
    except OSError as exc:
        # 底层真实磁盘故障仍交给调用方的 IO 重试/存储错误，不伪装成永久格式错误。
        if exc.errno is not None:
            raise
        raise FileFormatError("文件内容与扩展名不匹配或格式损坏") from None
    finally:
        stream.seek(0)
