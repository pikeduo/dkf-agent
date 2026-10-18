"""超限 PDF 的连续页分片和原页合并；不调用云端、不创建子 Document。"""

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5

import pymupdf
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.business_logging import log_event
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import ParseCache, ReadyRecord

VERSION = "pdf-split-v1"
MAX_PAGES = 200
# 保守采用十进制200MB，不把项目其他MB配置的MiB换算误用于平台文件上限。
MAX_BYTES = 200_000_000
MAX_PARTS = 128


def policy() -> dict:
    """冻结生成器版本和平台限制，恢复时不悄悄生成另一份收费输入。"""
    return {"version": VERSION, "max_pages": MAX_PAGES, "max_bytes": MAX_BYTES, "max_parts": MAX_PARTS,
            "generator": pymupdf.VersionBind}


class PDFPart(BaseModel):
    """一段连续原页及其收费/结果检查点；不包含签名 URL 或正文。"""

    model_config = ConfigDict(extra="forbid")
    ordinal: int = Field(ge=1, le=MAX_PARTS)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    file_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=MAX_BYTES)
    data_id: str
    task_id: UUID
    state: str = "waiting-file"
    batch_id: str | None = None
    submission_attempted: bool = False
    upload_complete: bool = False
    submitted_at: str | None = None
    submission_started_at: str | None = None
    result: dict | None = None
    # 原始 ZIP 已取得与 Adapter/Guard 成功是两件事，失败时仍保留下载来源。
    raw_result: dict | None = None
    error_code: str | None = None


class SplitManifest(BaseModel):
    """数据库 JSONB 是收费检查点真相；磁盘 manifest 是可核对副本，不据副本重建收费身份。"""

    model_config = ConfigDict(extra="forbid")
    version: str = VERSION
    doc_id: UUID
    file_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    policy: dict
    parts: list[PDFPart] = Field(min_length=1, max_length=MAX_PARTS)

    @model_validator(mode="after")
    def continuous(self):
        """要求从第一页到末页连续无重叠；成功片必须构成前缀且有校验结果指针。"""
        expected, pending, identities = 1, False, set()
        for ordinal, part in enumerate(self.parts, 1):
            if (part.ordinal != ordinal or part.page_start != expected
                    or not 0 <= part.page_end - part.page_start < MAX_PAGES
                    or part.state not in {"waiting-file", "pending", "running", "converting", "done", "failed"}
                    or part.data_id in identities):
                raise ValueError("PDF 分片检查点不连续或身份重复")
            if part.state == "done":
                if pending or part.result is None:
                    raise ValueError("PDF 成功分片缺少完整结果")
            else:
                pending = True
            identities.add(part.data_id)
            expected = part.page_end + 1
        if self.version != VERSION or expected != self.page_count + 1:
            raise ValueError("PDF 分片检查点版本或总页数不一致")
        return self


def checked_manifest(document, job) -> SplitManifest:
    """验证持久化身份，不将磁盘副本当成可重新提交的授权。"""
    try:
        value = SplitManifest.model_validate(job.parse_options["_pdf_split"])
        if (value.doc_id != document.doc_id or value.file_hash != document.file_hash
                or value.file_hash != job.file_hash or value.policy != policy()):
            raise ValueError
        for part in value.parts:
            identity = uuid5(job.job_id, f"{VERSION}:{part.ordinal}:{part.page_start}:{part.page_end}:{part.file_hash}")
            if part.task_id != identity or part.data_id != f"dkf-{identity.hex}":
                raise ValueError
        return value
    except (KeyError, TypeError, ValueError):
        raise MinerUError("PDF_SPLIT_CHECKPOINT", "PDF 分片检查点无效，未重新提交云任务") from None


def part_path(cache: ParseCache, doc_id: UUID, job_id: UUID, ordinal: int) -> Path:
    """只构造当前文档/Job 自有目录，逐级拒绝符号链接及 Windows junction。"""
    import stat

    path = cache.path(str(doc_id), "pdf_split", str(job_id), f"part-{ordinal:04d}.pdf")
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise MinerUError("PDF_SPLIT_PATH", "PDF 分片目录包含不安全链接")
        if item == cache.root:
            break
    return path


def file_digest(path: Path) -> str:
    """流式校验实际文件字节，不用页均大小或文件名替代 Hash。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def render_range(pdf, first: int, last: int, target: Path) -> None:
    """按原1-based闭区间复制页面；MuPDF独占保存新路径，关闭后再独占发布目标。"""
    if target.exists() or target.is_symlink():
        raise MinerUError("PDF_SPLIT_CHANGED", "PDF 分片目标已存在，未覆盖文件")
    step = "temporary_directory"
    try:
        # MuPDF会把具名文件对象转回路径；保存期间不得由Python预先打开该文件。
        # 仍使用独立临时目录；最终xb发布防止检查后目标被其他进程创建时遭覆盖。
        with TemporaryDirectory(prefix=".pdf-write-", dir=target.parent) as scratch:
            generated = Path(scratch) / "piece.pdf"
            step = "copy_pages"
            with pymupdf.open() as piece:
                piece.insert_pdf(pdf, from_page=first - 1, to_page=last - 1)
                step = "save"
                piece.save(str(generated), garbage=4, deflate=True, no_new_id=True)
            step = "verify"
            with pymupdf.open(generated) as piece:
                if piece.page_count != last - first + 1 or piece.is_repaired or piece.needs_pass:
                    raise MinerUError("PDF_SPLIT_INVALID", "生成的 PDF 分片无法完整读取")
                for index, page in enumerate(piece):
                    original = pdf[first - 1 + index]
                    if page.rect != original.rect or page.cropbox != original.cropbox or page.rotation != original.rotation:
                        raise MinerUError("PDF_SPLIT_GEOMETRY", "PDF 分片改变原页面坐标，未提交云任务")
            step = "publish"
            # 此时所有MuPDF文件句柄均已关闭；只复制原始字节，不重新序列化或改变Hash。
            with target.open("xb") as stream, generated.open("rb") as source:
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(stream.fileno())
    except FileExistsError:
        raise MinerUError("PDF_SPLIT_CHANGED", "PDF 分片目标已存在，未覆盖文件") from None
    except (pymupdf.mupdf.FzErrorBase, OSError, RuntimeError, ValueError) as exc:
        code = "PDF_SPLIT_WRITE" if step in {"temporary_directory", "save", "publish"} else "PDF_SPLIT_IO"
        log_event("mineru.split_io_failed", error_code=code, error_type=type(exc).__name__,
                  stage="pdf_split.render_range", step=step)
        raise MinerUError(code, "PDF 分片文件写入失败，未提交云任务"
                         if code == "PDF_SPLIT_WRITE" else "PDF 分片生成或校验失败，未提交云任务") from None


def build_parts(page_count: int, directory: Path, render, *, max_bytes=MAX_BYTES,
                max_pages=MAX_PAGES) -> list[dict]:
    """优先试最大连续段，再有界二分；只按实际文件大小接受，单页超限立即失败。"""
    if page_count < 1 or not 1 <= max_pages <= MAX_PAGES or not 0 < max_bytes <= MAX_BYTES:
        raise MinerUError("PDF_SPLIT_LIMIT", "PDF 分片资源限制无效")
    directory.mkdir(parents=True, exist_ok=True)
    parts, first = [], 1
    while first <= page_count:
        if len(parts) >= MAX_PARTS:
            raise MinerUError("PDF_SPLIT_LIMIT", "PDF 分片数量超过安全上限，未提交云任务")
        with TemporaryDirectory(prefix=".split-", dir=directory) as scratch:
            low, high, best = 1, min(max_pages, page_count - first + 1), None
            count, attempt = high, 0
            while low <= high:
                attempt += 1
                if attempt > 10:  # 每片至多 1 + ceil(log2(200)) 次，非无限试探。
                    raise MinerUError("PDF_SPLIT_LIMIT", "PDF 分片生成次数超过安全上限")
                candidate = Path(scratch) / f"candidate-{attempt}.pdf"
                render(first, first + count - 1, candidate)
                size = candidate.stat().st_size
                if size <= 0:
                    raise MinerUError("PDF_SPLIT_INVALID", "PDF 分片为空")
                if size <= max_bytes:
                    if best is not None:
                        best[0].unlink()
                    best = (candidate, count, size)
                    low = count + 1
                else:
                    candidate.unlink()
                    high = count - 1
                count = (low + high) // 2
            if best is None:
                raise MinerUError("PDF_SPLIT_SINGLE_PAGE", "PDF 单页超过 MinerU 文件限制，未提交任何分片")
            candidate, count, size = best
            digest = file_digest(candidate)
            target = directory / f"part-{len(parts) + 1:04d}.pdf"
            if target.exists():
                if target.is_symlink() or target.stat().st_size != size or file_digest(target) != digest:
                    raise MinerUError("PDF_SPLIT_CHANGED", "已有 PDF 分片不匹配，未覆盖文件或重新收费")
            else:
                # 独占创建，不覆盖其他任务或管理员留下的文件。
                with target.open("xb") as stream, candidate.open("rb") as source:
                    shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            parts.append({"ordinal": len(parts) + 1, "page_start": first,
                          "page_end": first + count - 1, "file_hash": digest, "size_bytes": size})
            first += count
    return parts


def prepare_manifest(document, job, settings, path: Path) -> SplitManifest:
    """缓存miss后只读原件规划全片；MuPDF底层异常安全分类，失败不得开始云提交。"""
    cache = ParseCache(settings)
    directory = part_path(cache, document.doc_id, job.job_id, 1).parent
    request = job.parse_options["_pdf_split_request"]
    if request["policy"] != policy() or file_digest(path) != document.file_hash:
        raise MinerUError("PDF_SPLIT_CHANGED", "PDF 原件或分片生成配置已变化")
    try:
        with pymupdf.open(path) as pdf:
            if pdf.page_count != request["page_count"] or pdf.needs_pass or pdf.is_repaired:
                raise MinerUError("PDF_SPLIT_CHANGED", "PDF 原件页数或完整性发生变化")

            def render(first, last, target):
                """绑定只读原件供有界分片规划使用，不接触业务数据集。"""
                render_range(pdf, first, last, target)

            rows = build_parts(pdf.page_count, directory, render)
        if file_digest(path) != document.file_hash:
            raise MinerUError("FILE_HASH", "PDF 分片过程中原件 Hash 已变化")
        for row in rows:
            identity = uuid5(job.job_id, f"{VERSION}:{row['ordinal']}:{row['page_start']}:{row['page_end']}:{row['file_hash']}")
            row.update(data_id=f"dkf-{identity.hex}", task_id=identity)
        return SplitManifest(doc_id=document.doc_id, file_hash=document.file_hash,
                             page_count=request["page_count"], policy=policy(), parts=rows)
    except (pymupdf.mupdf.FzErrorBase, OSError, ValueError, RuntimeError) as exc:
        log_event("mineru.split_io_failed", error_code="PDF_SPLIT_IO", error_type=type(exc).__name__,
                  stage="pdf_split.prepare_manifest", step="read_or_plan")
        raise MinerUError("PDF_SPLIT_IO", "PDF 分片生成或读取失败，未提交云任务") from None


def piece_recipe(job, part: PDFPart) -> dict:
    """片缓存不含 doc_id/原页偏移，可复用完全相同的分片；不与整份成功缓存混淆。"""
    value = dict(job.parse_options.get("_adapter_recipe") or job.parse_options["_cache_recipe"])
    value.update(file_hash=part.file_hash, route_strategy="mineru_pdf_part",
                 pdf_split=policy(), coverage_scope="pdf_part", page_count=part.page_end - part.page_start + 1)
    return value


def load_piece(cache: ParseCache, job, part: PDFPart) -> ParsedDocument | None:
    """按不可变代次核对片结果；已成功片缺失/损坏绝不当作新收费 miss。"""
    try:
        record = ReadyRecord.model_validate(part.result) if part.result is not None else None
        value = piece_recipe(job, part)
        if record is not None and record.recipe.get("normalizer_version") == "mineru-adapter-v3" and part.state == "done":
            legacy = value | {"normalizer_version": "mineru-adapter-v3"}
            if record.recipe != legacy:
                raise ValueError
            value = legacy  # 已完成且来源有效的旧片不重算，也不把旧结果冒称为v4。
        loaded = cache.load(value, record)
    except (TypeError, ValueError):
        raise MinerUError("PDF_SPLIT_RESULT", "PDF 分片结果指针无效，未重新收费") from None
    if loaded is None:
        if part.result is not None or part.state == "done":
            raise MinerUError("PDF_SPLIT_RESULT", "已成功 PDF 分片的缓存缺失，未重新收费")
        return None
    try:
        parsed = ParsedDocument.model_validate_json(loaded[1]["normalized_document.json"])
        count = part.page_end - part.page_start + 1
        empty_partial = (parsed.quality is not None and parsed.quality.content == "PARTIAL"
                         and (parsed.quarantined_content or parsed.quality.visual_only_count))
        if (parsed.file_type != "pdf" or not parsed.blocks and not empty_partial
                or any(b.page > count or b.source != "mineru_cloud" or not b.text.strip() for b in parsed.blocks)):
            raise ValueError
        part.result = loaded[0].model_dump(mode="json")
        return parsed
    except (KeyError, TypeError, ValueError):
        raise MinerUError("PDF_SPLIT_RESULT", "PDF 分片结果不符合冻结页范围") from None


def merge_parts(document, manifest: SplitManifest, parsed_parts: list[ParsedDocument]) -> ParsedDocument:
    """仅全片成功才合并：原物理页排序、同页稳定原序，结构块整体保留且 ID 幂等。"""
    if len(parsed_parts) != len(manifest.parts) or any(p.state != "done" for p in manifest.parts):
        raise MinerUError("PDF_SPLIT_INCOMPLETE", "PDF 分片尚未全部成功，未写入部分 Block")
    blocks = []
    for part, parsed in zip(manifest.parts, parsed_parts, strict=True):
        count = part.page_end - part.page_start + 1
        for index, block in sorted(enumerate(parsed.blocks), key=lambda item: item[1].page):
            if not 1 <= block.page <= count or block.source != "mineru_cloud":
                raise MinerUError("PDF_SPLIT_PAGE", "PDF 分片局部页码不在原页映射范围")
            values = block.model_dump()
            values.update(page=part.page_start + block.page - 1,
                          block_id=uuid5(document.doc_id, f"{VERSION}:{manifest.file_hash}:{part.result['key']}:{part.page_start}:{index}:{block.page}:{block.block_type}"))
            if values.get("provenance"):
                values["provenance"]["pointer"] = f"part/{part.ordinal}/" + values["provenance"]["pointer"]
            blocks.append(ParsedBlock.model_validate(values))
    from app.schemas.parsed_document import ParseQuality
    qualities = [p.quality for p in parsed_parts if p.quality is not None]
    quality = None
    if qualities:
        quality = ParseQuality(content="PARTIAL" if any(q.content == "PARTIAL" for q in qualities) else "COMPLETE",
            structure="DEGRADED" if any(q.structure == "DEGRADED" for q in qualities) else "BASE",
            evidence="PARTIAL" if any(q.evidence == "PARTIAL" for q in qualities) else "VERIFIED",
            warnings=list(dict.fromkeys(w for q in qualities for w in q.warnings))[:128],
            diagnostics=[entry | {"part_ordinal": part.ordinal} for part, parsed in zip(manifest.parts, parsed_parts)
                if parsed.quality for entry in parsed.quality.diagnostics][:128],
            required_pages=sorted({part.page_start + p - 1 for part, parsed in zip(manifest.parts, parsed_parts)
                if parsed.quality for p in parsed.quality.required_pages}),
            missing_pages=sorted({part.page_start + p - 1 for part, parsed in zip(manifest.parts, parsed_parts)
                if parsed.quality for p in parsed.quality.missing_pages}),
            covered_pages=sorted({b.page for b in blocks}), quarantined_count=sum(q.quarantined_count for q in qualities),
            visual_only_count=sum(q.visual_only_count for q in qualities), base_block_count=len(blocks))
    quarantine = [value | {"part_ordinal": part.ordinal} for part, parsed in zip(manifest.parts, parsed_parts)
                  for value in parsed.quarantined_content]
    return ParsedDocument(doc_id=document.doc_id, file_name=document.file_name, file_type="pdf",
                          title=parsed_parts[0].title, blocks=blocks, quality=quality, quarantined_content=quarantine)
