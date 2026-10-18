"""内容寻址解析缓存：校验后发布、跨文档身份重绑、失败关闭，不承担索引或 HTTP。"""

import hashlib
import json
import os
import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings
from app.parsers.common import ParserError
from app.schemas.parsed_document import ParsedBlock, ParsedDocument
from app.services.cache_files import cache_guard

VERSION = "parse-cache-v1"
# v4基础来源/质量分层改变转换语义，隔离旧成功缓存；原ZIP/云参数不改、不自动收费。
NORMALIZER_VERSION = "mineru-adapter-v4"
CONTENT_POLICY_VERSION = "mineru-content-v4.1"
NATIVE_VERSION = "native-v1"
HASH = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = frozenset({"normalized_document.json", "raw/mineru_result.zip",
    "content_list.json", "full.md", "hybrid/bundle_manifest.json",
    "hybrid/visual_bundle.pdf", "hybrid/normalized_visual_blocks.json", "pdf_split/manifest.json"})
OPTION_NAMES = ("model_version", "language", "is_ocr", "enable_table", "enable_formula")


class CacheError(ParserError):
    """缓存错误可安全显示；缺失/损坏不得悄悄变成新的付费调用。"""

    def __init__(self, code: str = "CACHE_INVALID") -> None:
        """使用固定码，不包含磁盘地址、缓存正文或上游错误。"""

        allowed = {"CACHE_INVALID", "CACHE_IO", "CACHE_LIMIT", "CACHE_MISS",
                   "CACHE_CONFIG_CHANGED", "CACHE_REPARSE_REQUIRED"}
        self.code = code if code in allowed else "CACHE_INVALID"
        super().__init__(f"{self.code}：解析缓存不可复用，请核对缓存或显式 Reparse；未自动调用云端")


class ReadyRecord(BaseModel):
    """不可变结果的发布指针；只由本地存储解释，不信任任意文件路径。"""

    model_config = ConfigDict(extra="forbid")
    version: str
    key: str
    generation: UUID
    recipe: dict
    files: dict[str, str]


def canonical(value) -> bytes:
    """确定性 UTF-8 序列化，只用于内部 Hash/磁盘文件，不写入日志。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    """计算精确字节 Hash，不以相似文本或文件名判断缓存命中。"""

    return hashlib.sha256(value).hexdigest()


def cloud_options(settings: Settings) -> dict:
    """仅冻结解析身份参数；Token、URL、频控及机器路径不参与身份且不落缓存。"""

    return {"model_version": settings.mineru_model_version,
            "language": settings.mineru_language, "is_ocr": settings.mineru_is_ocr,
            "enable_table": settings.mineru_enable_table,
            "enable_formula": settings.mineru_enable_formula}


def recipe(document, settings: Settings, strategy: str, *, scope: str = "complete",
           manifest=None, pdf_split_policy=None) -> dict:
    """冻结原件/模型/本地策略；Native身份不变，云转换按内容策略修订隔离缓存。"""

    if strategy not in {"native", "mineru_whole", "hybrid_bundle"} or scope not in {"complete", "explicit_native_scope"}:
        raise CacheError()
    native = strategy == "native"
    config = {name: getattr(settings, name) for name in type(settings).model_fields
              if name.startswith(("router_", "hybrid_"))}
    value = {"version": VERSION, "file_hash": document.file_hash,
             "file_type": document.file_type, "provider": "native_parser" if native else "mineru_cloud",
             "route_strategy": strategy, "coverage_scope": scope,
             "normalizer_version": NATIVE_VERSION if native else NORMALIZER_VERSION,
             "routing_config_hash": digest(canonical(config)),
             **({"model_version": NATIVE_VERSION, "language": "", "is_ocr": False,
                 "enable_table": True, "enable_formula": True} if native else cloud_options(settings))}
    if not native:
        value["content_policy_version"] = CONTENT_POLICY_VERSION
    if document.file_type == "xlsx":
        value["native_primary_version"] = "xlsx-primary-v1"
    if pdf_split_policy is not None:
        from app.services.mineru.pdf_split import policy
        if document.file_type != "pdf" or strategy != "mineru_whole" or pdf_split_policy != policy():
            raise CacheError("CACHE_CONFIG_CHANGED")
        value["pdf_split"] = pdf_split_policy
    if strategy == "hybrid_bundle":
        if manifest is None:
            raise CacheError()
        # 去掉 doc_id、临时 PDF 页、结果状态及 Block ID；缓存命中不会改变原始映射身份。
        entries = [{name: getattr(entry, name) for name in (
            "source_type", "source_section", "sheet_index", "source_anchor", "position",
            "content_hash", "media_type", "width", "height", "filter_reason")}
            for entry in manifest.entries]
        value.update(visual_extractor_version=manifest.version,
            bundle_manifest_hash=digest(canonical({"policy": manifest.policy.model_dump(mode="json"),
                "sheet_names": manifest.sheet_names, "entries": entries})),
            visual_content_hash=digest(canonical(sorted({entry.content_hash for entry in manifest.entries
                if entry.source_type == "embedded_image" and entry.filter_reason is None}))))
    return value


def visual_recipe(content_hash: str, options: dict, extractor_version: str) -> dict:
    """以原始视觉字节 Hash 寻址，不依赖 Bundle、页号、Anchor、doc_id 或 Sheet 名。"""

    if not HASH.fullmatch(content_hash):
        raise CacheError()
    return {"version": VERSION, "provider": "mineru_cloud", "route_strategy": "hybrid_visual",
            "visual_content_hash": content_hash, "visual_extractor_version": extractor_version,
            "normalizer_version": NORMALIZER_VERSION,
            "content_policy_version": CONTENT_POLICY_VERSION,
            **{name: options[name] for name in OPTION_NAMES}}


def key_for(value: dict) -> str:
    """对完整参数身份计算固定长度键；原始正文与路径不作为可见名称。"""

    return digest(canonical(value))


def guard_cached(parsed: ParsedDocument, decision) -> None:
    """命中仍验证覆盖语义；缓存 Guard 失败属于损坏，绝不能触发 Native→云收费回退。"""

    from app.parsers.routing.guard import guard_result

    try:
        if decision.route_strategy == "hybrid_bundle":
            if any(b.page != 1 or b.bbox is not None or not b.section
                   or b.source not in {"native_parser", "mineru_cloud"} for b in parsed.blocks):
                raise CacheError()
            tables = [b for b in parsed.blocks if b.source == "native_parser" and b.block_type == "table"]
            if tables or decision.signals.nonempty_sheet_count:
                guard_result(parsed.model_copy(update={"blocks": tables}), decision.model_copy(update={
                    "parse_mode": "native", "route_strategy": "native", "reason_codes": ("EXPLICIT_NATIVE",)}))
        elif decision.file_type == "xlsx" and decision.route_strategy == "mineru_whole":
            cloud = [b for b in parsed.blocks if b.source == "mineru_cloud"]
            native = [b for b in parsed.blocks if b.source == "native_parser"]
            guard_result(parsed.model_copy(update={"blocks": cloud}), decision)
            if native or decision.signals.nonempty_sheet_count:
                guard_result(parsed.model_copy(update={"blocks": native}), decision.model_copy(update={
                    "parse_mode": "native", "route_strategy": "native", "reason_codes": ("EXPLICIT_NATIVE",)}))
        else:
            guard_result(parsed, decision)
    except ParserError:
        raise CacheError() from None


def atomic_write(path: Path, content: bytes) -> None:
    """同目录临时文件 fsync 后原子替换，发布指针永远最后写入；不删历史代次。"""

    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise CacheError("CACHE_IO") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class ParseCache:
    """单主机共享文件缓存；READY 指针与每个文件 Hash 一起校验，禁止损坏回退付费。"""

    def __init__(self, settings: Settings):
        """只计算配置路径和限额，不创建目录，不访问数据库或云端。"""

        self.root = settings.parsed_dir
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root
        self.root = self.root.resolve()
        self.limit = settings.parse_cache_max_entry_mb * 1024**2

    def path(self, *parts: str) -> Path:
        """只允许落在配置根目录内的固定路径，拒绝符号链接越界。"""

        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root):
            raise CacheError()
        return path

    def read_bytes(self, path: Path, limit: int) -> bytes:
        """有界读取，检查文件大小及读后增长；缺失 READY 内容视为损坏而非普通 miss。"""

        try:
            if path.stat().st_size > limit:
                raise CacheError("CACHE_LIMIT")
            with path.open("rb") as stream:
                value = stream.read(limit + 1)
            if len(value) > limit:
                raise CacheError("CACHE_LIMIT")
            return value
        except OSError:
            raise CacheError("CACHE_IO") from None

    def load(self, value: dict, record: ReadyRecord | None = None) -> tuple[ReadyRecord, dict[str, bytes]] | None:
        """与删除互斥后有界读取不可变代次；锁失败不当成缓存miss。"""
        try:
            with cache_guard(self.root):
                return self._load_locked(value, record)
        except OSError:
            raise CacheError("CACHE_IO") from None

    def _load_locked(self, value: dict, record: ReadyRecord | None = None) -> tuple[ReadyRecord, dict[str, bytes]] | None:
        """不存在时返回 miss；存在指针则全量验证，不忽略残缺、篡改或超量文件。"""

        key = key_for(value)
        pointer = self.path(".cache", key, "ready.json")
        try:
            if record is None:
                if not pointer.exists():
                    return None
                record = ReadyRecord.model_validate_json(self.read_bytes(pointer, 65536))
            if (record.version != VERSION or record.key != key or record.recipe != value
                or not record.files or set(record.files) - ARTIFACTS
                or any(not HASH.fullmatch(h) for h in record.files.values())):
                raise CacheError()
            files, remaining = {}, self.limit
            for name, expected in record.files.items():
                content = self.read_bytes(self.path(".cache", key, str(record.generation), name), remaining)
                remaining -= len(content)
                if digest(content) != expected:
                    raise CacheError()
                files[name] = content
            return record, files
        except (ValueError, ValidationError, TypeError):
            raise CacheError() from None

    def save(self, value: dict, files: dict[str, bytes]) -> ReadyRecord:
        """与清理互斥发布完整缓存，不允许删除与读取/写入交错。"""
        try:
            with cache_guard(self.root):
                return self._save_locked(value, files)
        except OSError:
            raise CacheError("CACHE_IO") from None

    def _save_locked(self, value: dict, files: dict[str, bytes]) -> ReadyRecord:
        """保存已经通过业务 Guard 的全量结果；只有所有文件落盘才发布 READY。"""

        if (not files or set(files) - ARTIFACTS
            or sum(len(content) for content in files.values()) > self.limit):
            raise CacheError("CACHE_LIMIT")
        key, generation = key_for(value), uuid4()
        record = ReadyRecord(version=VERSION, key=key, generation=generation, recipe=value,
                             files={name: digest(content) for name, content in files.items()})
        for name, content in files.items():
            atomic_write(self.path(".cache", key, str(generation), name), content)
        atomic_write(self.path(".cache", key, "ready.json"), record.model_dump_json().encode())
        log_event("cache.stored", cache_key=key, route_strategy=value["route_strategy"])
        return record

    def document(self, document, value: dict, record: ReadyRecord | None = None) -> ParsedDocument | None:
        """复用全量标准化结果并重绑 doc_id/文件名；同文档 ID/正文顺序保持稳定。"""

        loaded = self.load(value, record)
        if loaded is None:
            log_event("cache.miss", doc_id=document.doc_id, cache_key=key_for(value))
            return None
        record, files = loaded
        try:
            parsed = ParsedDocument.model_validate_json(files["normalized_document.json"])
            if parsed.file_type != document.file_type or not parsed.blocks or not any(b.text.strip() for b in parsed.blocks):
                raise CacheError()
            if parsed.doc_id != document.doc_id:
                parsed = parsed.model_copy(update={"doc_id": document.doc_id,
                    "blocks": [block.model_copy(update={"block_id": uuid5(document.doc_id,
                        f"{VERSION}:{record.key}:{index}:{block.block_type}")})
                        for index, block in enumerate(parsed.blocks, 1)]})
            parsed = parsed.model_copy(update={"file_name": document.file_name})
            self.materialize(document, record, files, parsed)
            log_event("cache.hit", doc_id=document.doc_id, cache_key=record.key,
                      block_count=len(parsed.blocks), parse_cache_hit=True, mineru_file_submit_count=0)
            return parsed
        except (KeyError, ValueError, ValidationError):
            raise CacheError() from None

    def materialize(self, document, record: ReadyRecord, files: dict[str, bytes], parsed: ParsedDocument) -> None:
        """文档目录保存有版本结果和 latest 指针；写入失败不把业务冒充成功。"""

        for name, content in files.items():
            if name == "normalized_document.json":
                content = parsed.model_dump_json().encode()
            atomic_write(self.path(str(document.doc_id), record.key, str(record.generation), name), content)
        atomic_write(self.path(str(document.doc_id), "latest.json"), record.model_dump_json().encode())

    def save_document(self, document, value: dict, parsed: ParsedDocument, artifacts=None,
                      *, job_id: UUID | None = None) -> None:
        """校验身份和有效结果后持久化；缓存先于数据库提交，回滚后可安全重放已验证结果。"""

        parsed = ParsedDocument.model_validate(parsed)
        if ((parsed.doc_id, parsed.file_name, parsed.file_type) !=
            (document.doc_id, document.file_name, document.file_type)
            or not parsed.blocks or not any(b.text.strip() for b in parsed.blocks)):
            raise CacheError()
        files = dict(artifacts or {}) | {"normalized_document.json": parsed.model_dump_json().encode()}
        record = self.save(value, files)
        if job_id is not None:
            atomic_write(self.path(".jobs", str(UUID(str(job_id))) + ".json"), record.model_dump_json().encode())
        self.materialize(document, record, files, parsed)

    def job_document(self, document, value: dict, job_id: UUID) -> tuple[ParsedDocument, dict[str, bytes]] | None:
        """恢复同一云 Job 已验证的不可变代次，DB 回滚后不重查/重下/重提交云任务。"""

        pointer = self.path(".jobs", str(UUID(str(job_id))) + ".json")
        if not pointer.exists():
            return None
        try:
            record = ReadyRecord.model_validate_json(self.read_bytes(pointer, 65536))
            parsed = self.document(document, value, record)
            return parsed, self.load(value, record)[1]
        except (ValueError, ValidationError):
            raise CacheError() from None

    def replay(self, document, settings: Settings) -> ParsedDocument:
        """Reindex 回放原件级完整缓存含分片策略；不重新生成分片或发起云请求。"""

        pointer = self.path(str(document.doc_id), "latest.json")
        if not pointer.exists():
            raise CacheError("CACHE_MISS")
        try:
            record = ReadyRecord.model_validate_json(self.read_bytes(pointer, 65536))
            current = recipe(document, settings, record.recipe["route_strategy"],
                             scope=record.recipe["coverage_scope"],
                             pdf_split_policy=record.recipe.get("pdf_split"),
                             manifest=None) if record.recipe["route_strategy"] != "hybrid_bundle" else None
            if current is None:
                # Hybrid 映射已冻结；此处只核对参数/配置，不能为了 Reindex 重跑 Extractor。
                current = recipe(document, settings, "mineru_whole", scope=record.recipe["coverage_scope"])
                current.update({name: record.recipe[name] for name in (
                    "route_strategy", "visual_extractor_version", "bundle_manifest_hash", "visual_content_hash")})
                from app.parsers.hybrid import VERSION as extractor_version
                if current["visual_extractor_version"] != extractor_version:
                    raise CacheError("CACHE_CONFIG_CHANGED")
            if record.recipe != current:
                raise CacheError("CACHE_CONFIG_CHANGED")
            # 必须使用该文档冻结的成功代次，不能被其他文档/Force 的全局 latest 改换 Block ID。
            parsed = self.document(document, current, record)
            if parsed is None:
                raise CacheError("CACHE_MISS")
            return parsed
        except (KeyError, ValueError, ValidationError):
            raise CacheError() from None

    def visual(self, value: dict) -> list[ParsedBlock] | None:
        """读取与原始图片绑定的标准化块；不保留旧 Bundle 页、bbox、Sheet 或文档身份。"""

        loaded = self.load(value)
        if loaded is None:
            return None
        try:
            rows = json.loads(loaded[1]["hybrid/normalized_visual_blocks.json"])
            blocks = [ParsedBlock.model_validate(row) for row in rows]
            if (not blocks or any(b.source != "mineru_cloud" or not b.text.strip()
                or b.page != 1 or b.bbox is not None or b.section for b in blocks)):
                raise CacheError()
            return blocks
        except (KeyError, ValueError, TypeError, ValidationError):
            raise CacheError() from None

    def save_visual(self, value: dict, blocks: list[ParsedBlock]) -> None:
        """只保存已证明完整的 Bundle 单页结果，不缓存失败/缺页/空 OCR，也不拆结构块。"""

        key = key_for(value)
        if not blocks or any(b.source != "mineru_cloud" or not b.text.strip() for b in blocks):
            raise CacheError()
        rows = [block.model_copy(update={"block_id": uuid5(NAMESPACE_URL, f"{VERSION}:{key}:{index}"),
                    "page": 1, "bbox": None, "section": ""}).model_dump(mode="json")
                for index, block in enumerate(blocks, 1)]
        self.save(value, {"hybrid/normalized_visual_blocks.json": canonical(rows)})


def archive_artifacts(archive: bytes) -> dict[str, bytes]:
    """Adapter 已校验 ZIP 后只读取白名单正文/可选 Markdown，不 extractall 或保存签名 URL。"""

    from app.services.mineru.adapter import read_content_list

    items = read_content_list(archive)
    files = {"raw/mineru_result.zip": archive, "content_list.json": canonical(items)}
    with ZipFile(BytesIO(archive)) as zipped:
        candidates = [info for info in zipped.infolist() if PurePosixPath(info.filename.replace("\\", "/")).name == "full.md"]
        if len(candidates) == 1 and candidates[0].file_size <= 64 * 1024**2:
            files["full.md"] = zipped.read(candidates[0])
    return files
