"""Hybrid 私有检查点与结果恢复；只在正常文档任务运行时生成临时 Bundle。"""

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from app.core.config import Settings, get_settings
from app.parsers.common import ParserError
from app.parsers.hybrid import (
    VERSION, BundleManifest, HybridPlan, HybridPolicy, HybridPreflightError,
    digest, merge_hybrid, prepare_hybrid,
)
from app.parsers.routing.contracts import RouteDecision
from app.parsers.routing.guard import guard_result
from app.schemas.parsed_document import ParsedDocument
from app.services.document_processing import source_path
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import ParseCache, CacheError, cloud_options, visual_recipe, key_for


def policy_from_settings(settings: Settings) -> HybridPolicy:
    """冻结本代次保护阈值；Bundle 大小同时服从应用与供应商上限。"""

    return HybridPolicy(
        max_candidates=settings.hybrid_max_candidates,
        max_pages=settings.hybrid_max_bundle_pages,
        min_side=settings.hybrid_min_image_side,
        max_pixels=settings.hybrid_max_image_pixels,
        max_image_bytes=settings.hybrid_max_image_mb * 1024**2,
        max_bundle_bytes=min(settings.max_upload_size_mb, 200) * 1024**2,
        xml_limit=settings.router_xml_part_max_mb * 1024**2,
    )


def validate_primary(plan: HybridPlan, decision: RouteDecision) -> None:
    """校验 Native Sheet；无单元格的视觉工作表仅允许暂存，最终仍必须有视觉正文。"""

    if (not plan.native.blocks and decision.signals.nonempty_sheet_count == 0
        and decision.signals.formula_count == 0 and plan.manifest.entries):
        return

    native_decision = decision.model_copy(update={
        "route_strategy": "native", "parse_mode": "native", "reason_codes": ("EXPLICIT_NATIVE",)
    })
    guard_result(plan.native, native_decision)


def checkpoint_path(document, context: dict) -> Path:
    """只用数据库原件目录及受检 UUID 推导路径，不信任检查点内任意文件路径。"""

    task_id = UUID(context["routing_task_id"])
    path = source_path(document).parent / ".hybrid" / str(document.doc_id) / str(task_id)
    return path / "visual_bundle.pdf"


def write_checkpoint(path: Path, content: bytes) -> None:
    """同目录临时文件加原子替换；不会删除其他代次或用户验收资料。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_checkpoint(document, task_id: UUID, settings: Settings, decision: RouteDecision,
                       *, force_reparse: bool = False):
    """先查视觉缓存再冻结缺失图片 Bundle；强制 Reparse 跳过缓存但恢复不扩大提交范围。"""

    cache, options = ParseCache(settings), cloud_options(settings)

    def lookup(content_hash):
        """只读取精确图片结果，损坏缓存停止本代次，不悄悄重新花费额度。"""

        return cache.visual(visual_recipe(content_hash, options, VERSION))

    plan = prepare_hybrid(document.doc_id, source_path(document), document.file_name,
                          policy_from_settings(settings), visual_lookup=None if force_reparse else lookup)
    validate_primary(plan, decision)
    with source_path(document).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
            raise HybridPreflightError("HYBRID_INVALID_PACKAGE")
    context = {
        "version": VERSION, "source_hash": document.file_hash,
        "routing_task_id": str(task_id), "manifest": plan.manifest.model_dump(mode="json"),
        "cache_options": options,
    }
    if plan.bundle:
        path = checkpoint_path(document, context)
        write_checkpoint(path, plan.bundle)
        write_checkpoint(path.with_name("bundle_manifest.json"),
                         json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return plan, context


def checked_context(document, context: dict) -> BundleManifest:
    """核对当前源文档身份和有界 manifest，错误只返回固定安全说明。"""

    try:
        if (set(context) not in ({"version", "source_hash", "routing_task_id", "manifest"},
                                {"version", "source_hash", "routing_task_id", "manifest", "cache_options"})
            or context["version"] != VERSION or context["source_hash"] != document.file_hash
            or document.file_type != "xlsx"):
            raise ValueError
        UUID(context["routing_task_id"])
        manifest = BundleManifest.model_validate(context["manifest"])
        if (not manifest.bundle_page_count
            or len(manifest.entries) > manifest.policy.max_candidates
            or manifest.bundle_page_count > manifest.policy.max_pages
            or any(e.source_doc_id != document.doc_id for e in manifest.entries)):
            raise ValueError
        with source_path(document).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != document.file_hash:
                raise ValueError
        return manifest
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MinerUError("HYBRID_CHECKPOINT", "Hybrid 源文档或映射检查点不一致，停止云端处理") from None


def regenerate_plan(document, context: dict) -> HybridPlan:
    """按冻结配置/缓存命中集合恢复相同映射；新缓存不得改页，不调用云 API。"""

    manifest = checked_context(document, context)
    try:
        cached_hashes = {entry.content_hash for entry in manifest.entries if entry.cache_hit}

        def lookup(content_hash):
            """恢复只读本代次冻结命中，其他新缓存不能改变原 Bundle 页号或提交身份。"""

            if content_hash not in cached_hashes:
                return None
            blocks = ParseCache(get_settings()).visual(visual_recipe(content_hash, context["cache_options"], VERSION))
            if blocks is None:
                raise CacheError("CACHE_MISS")
            return blocks

        plan = prepare_hybrid(document.doc_id, source_path(document), document.file_name, manifest.policy,
                              visual_lookup=lookup if cached_hashes else None)
        if plan.manifest != manifest:
            raise HybridPreflightError()
        return plan
    except ParserError:
        raise MinerUError("HYBRID_CHECKPOINT", "Hybrid 原件或映射无法恢复，请人工核对") from None


def bundle_file(document, context: dict) -> tuple[Path, str]:
    """验证源 Hash 和 Bundle Hash；缺失时仅重建相同确定性 Bundle，不创建新云身份。"""

    manifest = checked_context(document, context)
    path = checkpoint_path(document, context)
    if not path.exists():
        plan = regenerate_plan(document, context)
        write_checkpoint(path, plan.bundle)
    if path.stat().st_size > manifest.policy.max_bundle_bytes:
        raise MinerUError("HYBRID_CHECKPOINT", "Hybrid Bundle 超出已保存安全限制")
    if digest(path.read_bytes()) != manifest.bundle_hash:
        raise MinerUError("HYBRID_CHECKPOINT", "Hybrid Bundle 内容已变化，禁止继续提交")
    return path, manifest.bundle_hash


def complete_hybrid(document, context: dict, visual: ParsedDocument, decision: RouteDecision):
    """整份视觉页齐全后恢复主结果并合并；调用方随后一次事务替换所有 Block。"""

    try:
        plan = regenerate_plan(document, context)
        validate_primary(plan, decision)
        parsed = merge_hybrid(plan, visual)
        return parsed, plan.manifest.model_dump(mode="json")
    except ParserError:
        raise MinerUError("HYBRID_MERGE", "Hybrid 视觉映射或完整性检查失败，未写入部分 Block") from None


def metrics(manifest: BundleManifest) -> dict[str, int]:
    """返回日志与任务结果白名单计数，绝不返回单元格、Anchor 文本或 manifest。"""

    return {
        "hybrid_visual_candidate_count": len(manifest.entries),
        "hybrid_visual_filtered_count": sum(e.state == "filtered" for e in manifest.entries),
        "hybrid_visual_dedup_count": sum(e.reused_from is not None for e in manifest.entries),
        "hybrid_bundle_page_count": manifest.bundle_page_count,
        "hybrid_chart_count": sum(e.source_type == "chart" for e in manifest.entries),
        "visual_cache_hit_count": sum(e.cache_hit and e.reused_from is None for e in manifest.entries),
    }
