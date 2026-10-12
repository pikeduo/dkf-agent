"""预置素材清单预检：仅检查路径、大小、Hash 与格式容器，不解析正文或生成样本。"""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile

from app.core.config import Settings
from app.services.document_upload import hash_upload, validate_content, validate_filename

TAGS = {"text_pdf", "scan_pdf", "mixed", "table", "formula", "chart", "reading_order"}
MODERN_FORMATS = {"txt", "md", "csv", "docx", "xlsx", "pptx", "pdf", "image"}
MAX_FILES = 1000
MAX_MANIFEST_BYTES = 1024 * 1024
ERRORS = {
    "PRESET_DIRECTORY": "预置目录不可用或含链接/越界路径",
    "PRESET_MANIFEST": "清单缺失、格式错误或与目录文件不一致",
    "PRESET_COVERAGE": "需至少十份不同内容、多类现代格式及 DOC/XLS/PPT，并覆盖全部内容标签",
    "PRESET_FILE": "预置文件为空、超限、格式不符或无法读取",
    "PRESET_CHANGED": "预检后的预置原件已变化，请重新预检",
    "PRESET_SOURCE_CONFLICT": "同库已有同内容但来源/格式不一致的文档，不自动覆盖或修改来源",
    "PRESET_DATABASE": "数据库不可用或提交结果不确定，请先核对文档再恢复",
    "PRESET_DISPATCH": "任务投递确认失败，保留已提交检查点，请先核对文档状态",
    "PRESET_FAILED": "文档处理失败，请查询 Document、Parse Job 和对应阶段安全日志",
    "PRESET_STATE": "文档状态或检查点不允许自动推进，请核对原任务",
    "PRESET_READY": "READY 文档的入库检查点或全量向量验证未通过",
    "PRESET_TIMEOUT": "等待已超时，已提交后台任务未取消，请查询状态后继续",
    "PRESET_UNKNOWN": "预置导入未完成，请核对安全日志与文档状态",
}


class PresetImportError(Exception):
    """固定安全错误，不输出目录、正文、连接串或底层异常原文。"""

    def __init__(self, code: str):
        """只允许已知类别进入终端或日志，未知原因归为通用错误。"""

        self.code = code if code in ERRORS else "PRESET_UNKNOWN"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class PresetFile:
    """进程内预检快照，路径不输出到日志/消息，标签只是人工内容声明。"""

    path: Path
    relative_name: str
    file_type: str
    file_hash: str
    tags: frozenset[str]


def is_link(path: Path) -> bool:
    """拒绝符号链接及 Windows Junction/Reparse Point，不递归跟随外部目录。"""

    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def scan_files(root: Path) -> dict[str, Path]:
    """确定性递归扫描，拒绝未知文件及任何链接，只忽略固定清单与目录占位文件。"""

    files = {}
    if not root.is_dir() or is_link(root):
        raise PresetImportError("PRESET_DIRECTORY")
    root = root.resolve()

    def walk_error(error):
        """扫描权限错误必须失败，不能静默漏掉一部分素材。"""

        raise PresetImportError("PRESET_DIRECTORY") from None

    for directory, subdirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in subdirs + names:
            entry = Path(directory) / name
            if is_link(entry) or not entry.resolve().is_relative_to(root):
                raise PresetImportError("PRESET_DIRECTORY")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json" or name == ".gitkeep":
                continue
            if not path.is_file():
                raise PresetImportError("PRESET_DIRECTORY")
            try:
                validate_filename(name)
            except HTTPException:
                raise PresetImportError("PRESET_FILE") from None
            files[relative] = path
            if len(files) > MAX_FILES:
                raise PresetImportError("PRESET_COVERAGE")
    return dict(sorted(files.items()))


def manifest_entries(value: dict) -> dict[str, frozenset[str]]:
    """严格读取版本化文件/标签映射，拒绝重复、路径跳转及供应商/模式等未定义参数。"""

    if (not isinstance(value, dict) or set(value) != {"version", "documents"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["documents"], list) or len(value["documents"]) > MAX_FILES):
        raise PresetImportError("PRESET_MANIFEST")
    result = {}
    for row in value["documents"]:
        if (not isinstance(row, dict) or set(row) != {"file", "tags"}
                or not isinstance(row["file"], str) or not row["file"]
                or "\\" in row["file"] or ":" in row["file"]
                or not isinstance(row["tags"], list)
                or any(not isinstance(tag, str) or tag not in TAGS for tag in row["tags"])):
            raise PresetImportError("PRESET_MANIFEST")
        relative = PurePosixPath(row["file"])
        if (relative.is_absolute() or any(part in {"..", "."} for part in relative.parts)
                or relative.as_posix() != row["file"] or row["file"] in result):
            raise PresetImportError("PRESET_MANIFEST")
        result[row["file"]] = frozenset(row["tags"])
    return result


def validate_coverage(files: list[PresetFile]) -> None:
    """检查十份不同内容及格式/标签覆盖；不能以标签证明真实 OCR/表格或阅读顺序质量。"""

    types = {"image" if item.file_type in {"png", "jpg", "jpeg"} else item.file_type for item in files}
    tags = set().union(*(item.tags for item in files))
    if (len({item.file_hash for item in files}) < 10 or not {"doc", "xls", "ppt"} <= types
            or len(types & MODERN_FORMATS) < 3 or tags != TAGS):
        raise PresetImportError("PRESET_COVERAGE")
    for item in files:
        if (item.tags & {"text_pdf", "scan_pdf"} and item.file_type != "pdf"
                or {"text_pdf", "scan_pdf"} <= item.tags):
            raise PresetImportError("PRESET_COVERAGE")


def preflight(root: Path, settings: Settings) -> list[PresetFile]:
    """全批写入前预检；不访问数据库/Redis，不执行 Parser/OCR，不创建或修改文件。"""

    try:
        paths = scan_files(root)
        manifest = root / "manifest.json"
        if not manifest.is_file() or is_link(manifest) or manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise PresetImportError("PRESET_MANIFEST")
        entries = manifest_entries(json.loads(manifest.read_text(encoding="utf-8-sig")))
        if set(entries) != set(paths):
            raise PresetImportError("PRESET_MANIFEST")
        files = []
        for relative, path in paths.items():
            with path.open("rb") as stream:
                upload = UploadFile(file=stream, filename=path.name)
                _, extension = validate_filename(path.name)
                digest = hash_upload(upload, settings.max_upload_size_mb * 1024 * 1024)
                validate_content(upload, extension)
            files.append(PresetFile(path, relative, extension[1:], digest, entries[relative]))
        validate_coverage(files)
        return files
    except PresetImportError:
        raise
    except (OSError, ValueError, TypeError, HTTPException):
        raise PresetImportError("PRESET_FILE") from None
