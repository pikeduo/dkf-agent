"""用户执行的缓存单测；纯元数据/临时 JSON，不生成或解析 Office、PDF、图片样本。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.business_logging import safe_fields
from app.core.config import Settings
from app.parsers.hybrid import merge_hybrid, VERSION
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services import parse_cache as module
from app.services.parse_cache import (CacheError, ParseCache, canonical, cloud_options,
    digest, key_for, recipe, visual_recipe)
from test_hybrid import metadata_plan, visual_result


@pytest.fixture
def cache_context(tmp_path):
    """隔离缓存目录，仅构造数据库元信息与统一结果，绝不运行文档 Parser。"""

    settings = Settings(_env_file=None, parsed_dir=tmp_path / "parsed")
    document = SimpleNamespace(doc_id=uuid4(), file_name="原始资料.txt", file_type="txt", file_hash="a" * 64)
    parsed = ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type="txt",
        blocks=[ParsedBlock(block_id=uuid4(), page=1, block_type="text", text="私有缓存正文",
                            source="native_parser", bbox=None)])
    return SimpleNamespace(settings=settings, document=document, parsed=parsed,
        cache=ParseCache(settings), identity=recipe(document, settings, "native"))


@pytest.mark.parametrize("field,value", [("mineru_model_version", "pipeline"),
    ("mineru_language", "en"), ("mineru_is_ocr", False),
    ("mineru_enable_table", False), ("mineru_enable_formula", False)])
def test_cloud_identity_options_isolation(cache_context, field, value):
    """五个解析选项均参与 Whole/视觉身份，不因临时 Bundle Hash 相同误命中。"""

    ctx = cache_context
    original = recipe(ctx.document, ctx.settings, "mineru_whole")
    changed = ctx.settings.model_copy(update={field: value})
    assert key_for(original) != key_for(recipe(ctx.document, changed, "mineru_whole"))
    assert key_for(visual_recipe("b"*64, cloud_options(ctx.settings), VERSION)) != key_for(
        visual_recipe("b"*64, cloud_options(changed), VERSION))


def test_scope_provider_route_and_source_isolation(cache_context):
    """Native 对照不能冒充 auto 完整覆盖；原件/格式/Provider/Route 变化均不复用。"""

    ctx = cache_context
    identities = [ctx.identity, recipe(ctx.document, ctx.settings, "native", scope="explicit_native_scope"),
                  recipe(ctx.document, ctx.settings, "mineru_whole")]
    changed = deepcopy(ctx.document)
    changed.file_hash = "b"*64
    identities.append(recipe(changed, ctx.settings, "native"))
    assert len({key_for(value) for value in identities}) == 4
    assert "api_token" not in str(identities) and "parsed_dir" not in str(identities)


def test_document_roundtrip_rebind_and_stable_ids(cache_context):
    """跨文档复用保持正文/顺序/页码，重绑文件名和稳定块 ID；旧文档数据不被改写。"""

    ctx = cache_context
    assert ctx.cache.document(ctx.document, ctx.identity) is None
    ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    assert ctx.cache.replay(ctx.document, ctx.settings) == ctx.parsed
    target = deepcopy(ctx.document)
    target.doc_id, target.file_name = uuid4(), "另一个文件名.txt"
    first = ctx.cache.document(target, ctx.identity)
    second = ctx.cache.document(target, ctx.identity)
    assert first == second and first.doc_id == target.doc_id and first.file_name == target.file_name
    assert first.blocks[0].text == ctx.parsed.blocks[0].text
    assert first.blocks[0].block_id != ctx.parsed.blocks[0].block_id
    assert ctx.cache.document(ctx.document, ctx.identity) == ctx.parsed
    # 全局 READY 被其他文档发布后，首个文档回放仍使用自己的不可变成功代次。
    ctx.cache.save_document(target, ctx.identity, first)
    assert ctx.cache.replay(ctx.document, ctx.settings) == ctx.parsed


def test_reindex_miss_and_config_change_fail_closed(cache_context):
    """Reindex 无成功缓存或保护配置变更明确失败，不运行 Parser 或云 Provider。"""

    ctx = cache_context
    with pytest.raises(CacheError, match="CACHE_MISS"):
        ctx.cache.replay(ctx.document, ctx.settings)
    ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    changed = ctx.settings.model_copy(update={"router_pdf_drawing_count": 99})
    with pytest.raises(CacheError, match="CACHE_CONFIG_CHANGED"):
        ctx.cache.replay(ctx.document, changed)


def test_merge_normalizer_version_isolates_whole_and_visual_cache(cache_context, monkeypatch):
    """新旧归一化隔离 Whole/视觉键；旧成功代次保留，Reindex 不静默重算或收费。"""

    ctx = cache_context
    current_version = module.NORMALIZER_VERSION
    assert current_version == "mineru-adapter-v2"
    monkeypatch.setattr(module, "NORMALIZER_VERSION", "mineru-adapter-v1")
    old_whole = recipe(ctx.document, ctx.settings, "mineru_whole")
    old_visual = visual_recipe("b" * 64, cloud_options(ctx.settings), VERSION)
    ctx.cache.save_document(ctx.document, old_whole, ctx.parsed)
    pointer = ctx.cache.root / str(ctx.document.doc_id) / "latest.json"
    original_pointer = pointer.read_bytes()
    monkeypatch.setattr(module, "NORMALIZER_VERSION", current_version)
    assert key_for(old_whole) != key_for(recipe(ctx.document, ctx.settings, "mineru_whole"))
    assert key_for(old_visual) != key_for(visual_recipe("b" * 64, cloud_options(ctx.settings), VERSION))
    with pytest.raises(CacheError, match="CACHE_CONFIG_CHANGED"):
        ctx.cache.replay(ctx.document, ctx.settings)
    assert pointer.read_bytes() == original_pointer
    assert ctx.cache.load(old_whole) is not None


@pytest.mark.parametrize("damage", ["bytes", "missing", "pointer"])
def test_corrupt_cache_is_not_normal_miss(cache_context, damage):
    """指针/正文残缺或被改动均安全拒绝，错误不含缓存原文或机器路径。"""

    ctx = cache_context
    ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    record, _ = ctx.cache.load(ctx.identity)
    path = ctx.cache.path(".cache", record.key, str(record.generation), "normalized_document.json")
    if damage == "missing":
        path.unlink()
    elif damage == "bytes":
        module.atomic_write(path, b"broken private bytes")
    else:
        module.atomic_write(ctx.cache.path(".cache", record.key, "ready.json"), b"not-json")
    with pytest.raises(CacheError) as caught:
        ctx.cache.document(ctx.document, ctx.identity)
    assert "private" not in str(caught.value) and str(path) not in str(caught.value)


def test_failed_publish_no_ready_pointer(cache_context, monkeypatch):
    """写盘异常不得发布 READY；已有成功代次不被半次 Force Reparse 删除。"""

    ctx = cache_context
    original = module.atomic_write

    def fail_pointer(path, content):
        """注入发布指针故障，不触发任何真实系统故障。"""

        if path.name == "ready.json":
            raise CacheError("CACHE_IO")
        original(path, content)

    monkeypatch.setattr(module, "atomic_write", fail_pointer)
    with pytest.raises(CacheError, match="CACHE_IO"):
        ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    assert ctx.cache.load(ctx.identity) is None
    monkeypatch.setattr(module, "atomic_write", original)
    ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    changed = ctx.parsed.model_copy(deep=True)
    changed.blocks[0].text = "尚未发布的新结果"
    monkeypatch.setattr(module, "atomic_write", fail_pointer)
    with pytest.raises(CacheError):
        ctx.cache.save_document(ctx.document, ctx.identity, changed)
    assert ctx.cache.document(ctx.document, ctx.identity) == ctx.parsed


def test_entry_resource_limit_cannot_publish(cache_context):
    """缓存总体字节超限在发布前失败，不留下可复用成功指针。"""

    ctx = cache_context
    ctx.cache.limit = 1
    with pytest.raises(CacheError, match="CACHE_LIMIT"):
        ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed)
    assert ctx.cache.load(ctx.identity) is None


def test_job_restore_frozen_generation_even_after_force_publish(cache_context):
    """同 Job 结果指针绑定不可变代次，之后 Force 的新输出不能覆盖恢复检查点。"""

    ctx = cache_context
    job_id = uuid4()
    ctx.cache.save_document(ctx.document, ctx.identity, ctx.parsed, job_id=job_id)
    changed = ctx.parsed.model_copy(deep=True)
    changed.blocks[0].text = "新解析正文"
    ctx.cache.save_document(ctx.document, ctx.identity, changed)
    restored, _ = ctx.cache.job_document(ctx.document, ctx.identity, job_id)
    assert restored == ctx.parsed


def test_visual_key_and_output_independent_of_bundle_document_anchor(cache_context):
    """图片原字节与模型选项决定复用；旧页/bbox/Sheet 不污染新文档 Anchor 或公式。"""

    ctx = cache_context
    plan = metadata_plan()
    cloud = visual_result(plan, (1,))
    value = visual_recipe(plan.manifest.entries[0].content_hash, cloud_options(ctx.settings), VERSION)
    ctx.cache.save_visual(value, cloud.blocks)
    cached = ctx.cache.visual(value)
    assert cached[0].page == 1 and cached[0].bbox is None and cached[0].section == ""
    new_plan = metadata_plan(page_count=0)
    new_plan.bundle = b""
    for entry in new_plan.manifest.entries:
        entry.content_hash = plan.manifest.entries[0].content_hash
        entry.bundle_page, entry.state, entry.cache_hit = None, "reused", True
    new_plan.cached = {entry.content_hash: cached for entry in new_plan.manifest.entries}
    merged = merge_hybrid(new_plan, None)
    assert "=B2*2" in merged.blocks[0].text
    assert [b.section for b in merged.blocks] == ["第一表", "第一表", "第二表", "第二表"]
    assert all(b.page == 1 and b.bbox is None for b in merged.blocks)


def test_manifest_key_stable_across_cache_state_and_doc_id(cache_context):
    """Bundle 临时身份/命中状态不影响整份缓存键，原 Anchor/视觉字节变化仍使其失效。"""

    ctx = cache_context
    plan = metadata_plan()
    document = SimpleNamespace(doc_id=plan.native.doc_id, file_type="xlsx", file_hash="c"*64)
    original = recipe(document, ctx.settings, "hybrid_bundle", manifest=plan.manifest)
    copied = deepcopy(plan.manifest)
    for entry in copied.entries:
        entry.source_doc_id, entry.visual_id = uuid4(), uuid4()
        entry.cache_hit, entry.state, entry.bundle_page = True, "reused", None
    copied.bundle_hash, copied.bundle_page_count = digest(b""), 0
    assert original == recipe(document, ctx.settings, "hybrid_bundle", manifest=copied)
    copied.entries[0].source_anchor = (19, 0, 0, 0)
    assert key_for(original) != key_for(recipe(document, ctx.settings, "hybrid_bundle", manifest=copied))


def test_cache_log_allowlist_excludes_payload_and_paths():
    """缓存日志只展示命中/数量/固定 Hash/操作，不输出正文、原始结果或本地目录。"""

    fields = safe_fields({"cache_key": "a"*64, "parse_cache_hit": True,
        "visual_cache_hit_count": 2, "operation": "reindex", "text": "secret",
        "manifest": {"token": "secret"}, "path": "secret-path"})
    assert set(fields) == {"cache_key", "parse_cache_hit", "visual_cache_hit_count", "operation"}
