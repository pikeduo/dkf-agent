"""超限 PDF 规划/合并的离线 Mock；用户执行，不读取或改写阶段32素材。"""

import hashlib
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from app.core.config import Settings
from app.parsers.routing.contracts import Inspection, StructuralSignals
from app.parsers.routing.router import decide_route
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.mineru import pdf_split as tool
from app.services.mineru import pdf_split_jobs as workflow
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import CacheError, ParseCache, key_for, recipe


def byte_renderer(bytes_per_page=1):
    """只创建可预测字节占位来验证真实 stat 大小；不生成/解析实际 PDF。"""
    calls = []

    def render(first, last, target):
        """记录实际请求区间并输出确定字节，不模拟平均页大小估算。"""
        calls.append((first, last))
        target.write_bytes(b"x" * ((last - first + 1) * bytes_per_page))

    return render, calls


@pytest.mark.parametrize("pages,last", [(257, 57), (217, 17), (382, 182)])
def test_page_limit_planner_is_continuous_and_keeps_original(tmp_path, pages, last):
    """三个页数不依赖文件名；优先200页，余页连续无遗漏/重复，原件只读。"""
    original = tmp_path / "original.bin"
    original.write_bytes(b"PRIVATE_ORIGINAL")
    before = tool.file_digest(original)
    render, calls = byte_renderer()
    parts = tool.build_parts(pages, tmp_path / "parts", render)
    assert [(p["page_start"], p["page_end"]) for p in parts] == [(1, 200), (201, pages)]
    assert [p["size_bytes"] for p in parts] == [200, last]
    assert [page for p in parts for page in range(p["page_start"], p["page_end"] + 1)] == list(range(1, pages + 1))
    assert calls == [(1, 200), (201, pages)]
    assert tool.file_digest(original) == before


@pytest.mark.parametrize("pages,byte_limit", [(180, 70), (382, 70), (200, 200)])
def test_actual_byte_limit_and_combined_limit_are_bounded(tmp_path, pages, byte_limit):
    """字节/页数可同时超限；每片都 stat/Hash 验证，尝试次数有上限。"""
    render, calls = byte_renderer()
    parts = tool.build_parts(pages, tmp_path, render, max_bytes=byte_limit)
    for part in parts:
        path = tmp_path / f"part-{part['ordinal']:04d}.pdf"
        assert part["size_bytes"] == path.stat().st_size <= byte_limit
        assert part["file_hash"] == tool.file_digest(path)
        assert part["page_end"] - part["page_start"] + 1 <= 200
    assert sum(p["page_end"] - p["page_start"] + 1 for p in parts) == pages
    assert len(calls) <= 10 * len(parts)


def test_large_single_page_is_rejected_without_infinite_attempts(tmp_path):
    """单页仍超限须明确失败，而非无限试探或偷偷丢页。"""
    render, calls = byte_renderer(201)
    with pytest.raises(MinerUError, match="单页") as error:
        tool.build_parts(257, tmp_path, render, max_bytes=200)
    assert error.value.code == "PDF_SPLIT_SINGLE_PAGE"
    assert len(calls) <= 10
    assert not list(tmp_path.glob("part-*.pdf"))


def test_existing_piece_is_never_overwritten(tmp_path):
    """恢复只接受完全相同的派生文件，用户文件/损坏片不被覆盖。"""
    target = tmp_path / "part-0001.pdf"
    target.write_bytes(b"KEEP_ME")
    render, _ = byte_renderer()
    with pytest.raises(MinerUError) as error:
        tool.build_parts(2, tmp_path, render)
    assert error.value.code == "PDF_SPLIT_CHANGED"
    assert target.read_bytes() == b"KEEP_ME"


def merge_fixture():
    """构造两片的合法身份/标准化块；全部使用内存元数据，不读取文档。"""
    document = SimpleNamespace(doc_id=uuid4(), file_name="original.pdf", file_type="pdf", file_hash="a" * 64)
    job = SimpleNamespace(job_id=uuid4(), file_hash=document.file_hash, parse_options={})
    parts, results = [], []
    for ordinal, start, end in [(1, 1, 200), (2, 201, 257)]:
        file_hash = hashlib.sha256(str(ordinal).encode()).hexdigest()
        task = uuid5(job.job_id, f"{tool.VERSION}:{ordinal}:{start}:{end}:{file_hash}")
        part = tool.PDFPart(ordinal=ordinal, page_start=start, page_end=end, file_hash=file_hash,
                           size_bytes=100, data_id=f"dkf-{task.hex}", task_id=task, state="done", result={"key": file_hash})
        parts.append(part)
        # 故意交错局部页；同页顺序必须稳定，table/formula 不可拆坏。
        blocks = [ParsedBlock(block_id=uuid4(), page=page, block_type=kind,
                              text=f"PRIVATE-{ordinal}-{index}", bbox=[1, 2, 30, 40],
                              section="原章节", source="mineru_cloud")
                  for index, (page, kind) in enumerate([(2, "text"), (1, "title"), (1, "table"), (1, "formula")])]
        results.append(ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type="pdf", blocks=blocks))
    manifest = tool.SplitManifest(doc_id=document.doc_id, file_hash=document.file_hash,
                                 page_count=257, policy=tool.policy(), parts=parts)
    job.parse_options["_pdf_split"] = manifest.model_dump(mode="json")
    return document, job, manifest, results


def test_merge_uses_original_pages_stable_ids_and_atomic_structure():
    """原物理页稳定排序且同页保序；Block ID 幂等，不泄漏局部页或拆表/公式。"""
    document, job, manifest, results = merge_fixture()
    before = deepcopy([p.model_dump() for p in results])
    merged = tool.merge_parts(document, manifest, results)
    again = tool.merge_parts(document, manifest, results)
    assert (merged.doc_id, merged.file_name) == (document.doc_id, document.file_name)
    assert [b.page for b in merged.blocks] == [1, 1, 1, 2, 201, 201, 201, 202]
    assert [b.block_type for b in merged.blocks[:4]] == ["title", "table", "formula", "text"]
    assert merged == again
    assert len({b.block_id for b in merged.blocks}) == len(merged.blocks)
    assert all(b.bbox == [1, 2, 30, 40] and b.section == "原章节" and b.source == "mineru_cloud" for b in merged.blocks)
    assert [p.model_dump() for p in results] == before
    assert tool.checked_manifest(document, job) == manifest


def test_official_zero_based_qa_page_does_not_shift_at_boundary():
    """官方 page_idx=200 对应原件201页，即第二片局部1页，而非正式引用1页。"""
    document, _, manifest, results = merge_fixture()
    merged = tool.merge_parts(document, manifest, results)
    official_qa = {"evidence_page_no": [200], "question": "官方问题原样保留"}
    assert merged.blocks[4].page == official_qa["evidence_page_no"][0] + 1 == 201
    assert official_qa["evidence_page_no"] == [200]


def test_existing_chunker_keeps_original_page_in_source_spans():
    """复用原切片器验证证据页透传，不修改 Chunk/Evidence 或生成向量。"""
    from app.services.chunk.splitter import SourceBlock, build_chunks

    document, _, manifest, results = merge_fixture()
    merged = tool.merge_parts(document, manifest, results)
    sources = [SourceBlock(b.block_id, index, b.page, b.section, b.block_type,
                           b.text, b.source, b.bbox) for index, b in enumerate(merged.blocks, 1)]
    chunks = build_chunks(document.doc_id, uuid4(), "pdf", sources)
    by_id = {str(b.block_id): b for b in merged.blocks}
    assert chunks and any(c.page_end >= 201 for c in chunks)
    for chunk in chunks:
        assert chunk.doc_id == document.doc_id and chunk.metadata["page_semantics"] == "physical_pdf"
        for span in chunk.metadata["source_spans"]:
            assert span["page"] == by_id[span["block_id"]].page
            assert span["bbox"] == by_id[span["block_id"]].bbox


@pytest.mark.parametrize("change", ["gap", "overlap", "hash", "identity", "version", "incomplete"])
def test_corrupt_manifest_or_partial_result_is_rejected(change):
    """断页/重页、原件 Hash/任务身份/策略变化、缺片不冒充完整解析。"""
    document, job, manifest, results = merge_fixture()
    value = job.parse_options["_pdf_split"]
    if change == "gap":
        value["parts"][1]["page_start"] += 1
    elif change == "overlap":
        value["parts"][1]["page_start"] -= 1
    elif change == "hash":
        value["file_hash"] = "b" * 64
    elif change == "identity":
        value["parts"][0]["data_id"] = "untrusted"
    elif change == "version":
        value["policy"]["version"] = "unknown"
    else:
        with pytest.raises(MinerUError) as error:
            tool.merge_parts(document, manifest, results[:1])
        assert error.value.code == "PDF_SPLIT_INCOMPLETE"
        return
    with pytest.raises(MinerUError) as error:
        tool.checked_manifest(document, job)
    assert error.value.code == "PDF_SPLIT_CHECKPOINT"


def test_local_page_out_of_range_is_never_mapped_to_another_part():
    """片内越界不能恰好映射到下一片的合法原页并绕过检查。"""
    document, _, manifest, results = merge_fixture()
    results[0].blocks[0].page = 201
    with pytest.raises(MinerUError) as error:
        tool.merge_parts(document, manifest, results)
    assert error.value.code == "PDF_SPLIT_PAGE"


def test_native_route_does_not_split_large_page_count():
    """可完整 Native 的257页 PDF 仍由 Router 选择 Native，分片不接管 Native。"""
    inspection = Inspection(signals=StructuralSignals(page_count=257))
    decision = decide_route("pdf", "auto", inspection)
    assert decision.route_strategy == "native"


def test_split_cache_recipe_and_reindex_replay_without_cloud(tmp_path):
    """分片结果仍以原文件身份缓存；Reindex只回放，不重新分片/云解析。"""
    document, _, manifest, results = merge_fixture()
    settings = Settings(_env_file=None, parsed_dir=tmp_path)
    cache = ParseCache(settings)
    value = recipe(document, settings, "mineru_whole", pdf_split_policy=tool.policy())
    assert value != recipe(document, settings, "mineru_whole")
    merged = tool.merge_parts(document, manifest, results)
    cache.save_document(document, value, merged)
    assert cache.replay(document, settings) == merged
    invalid = tool.policy() | {"version": "changed"}
    with pytest.raises(CacheError):
        recipe(document, settings, "mineru_whole", pdf_split_policy=invalid)
    office = SimpleNamespace(**(vars(document) | {"file_type": "docx"}))
    with pytest.raises(CacheError):
        recipe(office, settings, "mineru_whole", pdf_split_policy=tool.policy())


def test_piece_cache_identity_is_not_original_page_or_doc_identity():
    """完全相同片可跨原件复用，模型/参数/策略变化则隔离。"""
    document, job, manifest, _ = merge_fixture()
    settings = Settings(_env_file=None)
    job.parse_options["_cache_recipe"] = recipe(document, settings, "mineru_whole", pdf_split_policy=tool.policy())
    first = tool.piece_recipe(job, manifest.parts[0])
    same = manifest.parts[0].model_copy(update={"page_start": 201, "page_end": 400})
    assert key_for(first) == key_for(tool.piece_recipe(job, same))
    job.parse_options["_cache_recipe"]["enable_formula"] = False
    assert key_for(first) != key_for(tool.piece_recipe(job, same))


def test_fee_history_survives_next_unsubmitted_piece():
    """父表当前 Batch 清空并不表示整个源文件从未收费。"""
    _, job, manifest, _ = merge_fixture()
    job.submission_attempted, job.batch_id = False, None
    manifest.parts[0].batch_id = "batch-1"
    job.parse_options["_pdf_split"] = manifest.model_dump(mode="json")
    assert workflow.has_submission_history(job)
    assert workflow.submitted_count(job) == 1
