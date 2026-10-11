"""Whole XLSX 主结果保护的纯 Stub 测试；只编写，不生成或解析文档。"""

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid5

import pytest

from app.parsers.common import ParserError
from app.parsers.hybrid import digest
from app.parsers.routing.contracts import Inspection, StructuralSignals
from app.parsers.routing.router import decide_route
from app.services import xlsx_primary
from app.services.mineru.errors import MinerUError
from test_hybrid import metadata_plan, visual_result


@pytest.fixture
def primary_context(monkeypatch):
    """以元数据和内存流隔离原件、预检与 Parser，不创建 XLSX/PDF/图片样本。"""

    plan = metadata_plan()
    document = SimpleNamespace(doc_id=plan.native.doc_id, file_name="source.xlsx",
                               file_type="xlsx", file_hash=digest(b"source-stub"))
    signals = StructuralSignals(sheet_count=2, nonempty_sheet_count=2, formula_count=1)
    context = SimpleNamespace(document=document, native=plan.native, signals=signals)

    class SourceStub:
        """提供源 Hash 流，不持有磁盘原件。"""

        def open(self, mode):
            """每次返回新的固定内存流。"""
            return BytesIO(b"source-stub")

    def source_stub(document):
        """替代原件定位，不打开本机文件。"""
        return SourceStub()

    def inspection_stub(path, kind, settings):
        """返回固定覆盖摘要，不运行 OOXML 预检。"""
        return Inspection(signals=signals, risks=("XLSX_HAS_DRAWING",))

    def parse_stub(self, **kwargs):
        """返回独立的公式原式结果，允许测试检查点变更。"""
        return deepcopy(context.native)

    monkeypatch.setattr(xlsx_primary, "source_path", source_stub)
    monkeypatch.setattr(xlsx_primary, "inspect_document", inspection_stub)
    monkeypatch.setattr(xlsx_primary.XlsxParser, "parse", parse_stub)
    context.checkpoint = xlsx_primary.prepare_primary(document, None)
    context.decision = decide_route("xlsx", "mineru", Inspection(signals=signals))
    cloud = visual_result(plan)
    context.cloud = cloud.model_copy(update={"file_type": "xlsx", "blocks": [
        block.model_copy(update={"bbox": None, "block_id": uuid5(document.doc_id, f"cloud:{index}")})
        for index, block in enumerate(cloud.blocks)
    ]})
    return context


def test_whole_preserves_original_formulas_and_ids(primary_context):
    """云计算值不能覆盖 Native 原式；重复合并 ID 稳定，云逻辑页/来源保持不变。"""

    context = primary_context
    context.cloud.blocks[0].text = "246"
    before = context.native.model_dump_json()
    parsed = xlsx_primary.complete_primary(context.document, context.checkpoint,
                                           context.cloud, context.decision)
    repeated = xlsx_primary.complete_primary(context.document, context.checkpoint,
                                             context.cloud, context.decision)
    assert parsed == repeated and context.native.model_dump_json() == before
    assert parsed.blocks[:2] == context.native.blocks
    assert "=B2*2" in parsed.blocks[0].text and parsed.blocks[2].text == "246"
    assert [b.section for b in parsed.blocks[:2]] == ["第一表", "第二表"]
    assert all(b.source == "mineru_cloud" and b.bbox is None for b in parsed.blocks[2:])
    assert [b.page for b in parsed.blocks[2:]] == [1, 2]
    assert len({b.block_id for b in parsed.blocks}) == len(parsed.blocks)
    assert "text" not in context.checkpoint and "blocks" not in context.checkpoint


@pytest.mark.parametrize("change", ["native", "source", "identity", "cloud_bbox", "cloud_empty"])
def test_primary_or_cloud_damage_rejects_all(primary_context, change):
    """主结果变化、原件变化、错误身份、假坐标和空云结果必须安全失败。"""

    context = primary_context
    if change == "native":
        context.native.blocks[0].text = "changed-private-body"
    elif change == "source":
        context.document.file_hash = "f" * 64
    elif change == "identity":
        context.cloud.file_name = "wrong-private-name.xlsx"
    elif change == "cloud_bbox":
        context.cloud.blocks[0].bbox = [0.0, 0.0, 1.0, 1.0]
    else:
        context.cloud.blocks = []
    with pytest.raises(MinerUError) as caught:
        xlsx_primary.complete_primary(context.document, context.checkpoint,
                                      context.cloud, context.decision)
    assert caught.value.code.startswith("XLSX_PRIMARY")
    assert "changed-private-body" not in str(caught.value)
    assert "wrong-private-name" not in str(caught.value)


def test_primary_precheck_failure_does_not_allow_paid_whole(primary_context, monkeypatch):
    """Native 原式无法可靠读取时提交前失败，不让 Whole 结果假装保留公式。"""

    def invalid_native(self, **kwargs):
        """用受控 ParserError 模拟坏主结果，不处理真实文档。"""
        raise ParserError("受控解析失败")

    monkeypatch.setattr(xlsx_primary.XlsxParser, "parse", invalid_native)
    with pytest.raises(MinerUError) as caught:
        xlsx_primary.prepare_primary(primary_context.document, None)
    assert caught.value.code == "XLSX_PRIMARY_INVALID"


def test_visual_only_workbook_has_no_fabricated_native_cells(primary_context):
    """无单元格但有真实视觉风险时主结果可为空；成功仍由非空云结果保证。"""

    context = primary_context
    context.native.blocks = []
    context.signals = StructuralSignals(sheet_count=2, nonempty_sheet_count=0, image_count=2)
    context.checkpoint = xlsx_primary.PrimaryCheckpoint(doc_id=context.document.doc_id,
        source_hash=context.document.file_hash, native_hash=digest(context.native.model_dump_json().encode()),
        signals=context.signals).model_dump(mode="json")
    decision = decide_route("xlsx", "mineru", Inspection(signals=context.signals))
    parsed = xlsx_primary.complete_primary(context.document, context.checkpoint, context.cloud, decision)
    assert len(parsed.blocks) == 2 and all(block.source == "mineru_cloud" for block in parsed.blocks)
