"""CSV 负向样本与二进制验收素材生成；不改变任何生产 Parser。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


def invalid_encoding_bytes() -> bytes:
    """返回无 BOM 的截断 GB18030 四字节序列，同时无法被 UTF-8 严格解码。"""

    return b"\x81\x30\x81"


def regenerate_manual_fixture(sample_dir: Path, archive_path: Path) -> None:
    """机械重生成非法编码 CSV 与现有 ZIP 对应条目，保留其他素材字节及 ZIP 元信息。"""

    name = "04_negative_invalid_encoding.csv"
    replacements = {
        name: invalid_encoding_bytes(),
        "README_TEST_EXPECTATIONS.md": (
            sample_dir / "README_TEST_EXPECTATIONS.md"
        ).read_bytes(),
    }
    # 这是生成二进制测试素材，不使用文本编码写入，避免编辑器再添加 BOM。
    buffer = BytesIO()
    with ZipFile(archive_path) as original, ZipFile(buffer, "w") as generated:
        entries = original.infolist()
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("验收 ZIP 存在重名条目，拒绝更新")
        names = {entry.filename for entry in entries}
        prefix = "" if name in names else f"{sample_dir.name}/"
        replacements = {prefix + key: value for key, value in replacements.items()}
        if not replacements.keys() <= names:
            raise ValueError("验收 ZIP 缺少目标素材，拒绝更新")
        for entry in entries:
            content = replacements.get(entry.filename)
            generated.writestr(
                entry, original.read(entry) if content is None else content
            )
        generated.comment = original.comment
    (sample_dir / name).write_bytes(invalid_encoding_bytes())
    archive_path.write_bytes(buffer.getvalue())


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    regenerate_manual_fixture(
        project_root / "docs/stage11a_multiformat_test_files",
        project_root / "docs/stage11a_multiformat_test_files.zip",
    )
