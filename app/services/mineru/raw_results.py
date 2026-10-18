"""原始云结果独立归档：先保全 ZIP，再做 Adapter；不发布成功缓存或改变业务状态。"""

import hashlib
import json
import stat
import zlib
from io import BytesIO
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from app.core.business_logging import log_event
from app.services.cache_files import cache_guard
from app.services.mineru.adapter import read_content_list
from app.services.mineru.errors import MinerUError
from app.services.mineru.provider import safe_identifier
from app.services.parse_cache import CacheError, ParseCache, atomic_write, canonical


def validate_archive(archive: bytes, limit: int) -> int:
    """有界验证全部成员 CRC 和唯一非空 V1 列表；不代替 Adapter 的文字/来源完整性校验。"""
    if not archive or len(archive) > limit:
        raise MinerUError("ZIP_SIZE", "MinerU 原始结果大小无效")
    try:
        items = read_content_list(archive, allow_invalid_items=True)
        # 局部条目/page损坏交给Adapter隔离，不能因此丢弃已经付费取得的整个原ZIP。
        if not items or not any(isinstance(item, dict) and isinstance(item.get("type"), str)
                                and item["type"].strip() for item in items):
            raise MinerUError("CONTENT_LIST", "MinerU 原始结果没有有效的结构化条目")
        with ZipFile(BytesIO(archive)) as zipped:
            names = zipped.namelist()
            if len(set(names)) != len(names) or zipped.testzip() is not None:
                raise MinerUError("ZIP_INVALID", "MinerU 原始 ZIP 成员重复或 CRC 校验失败")
    except (BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error):
        raise MinerUError("ZIP_INVALID", "MinerU 原始 ZIP 不完整或无法读取") from None
    return len(items)


def result_paths(cache, doc_id, job_id, ordinal, batch_id):
    """按内部 UUID、片号及安全 Batch 定位；逐级拒绝符号链接和 Windows junction。"""
    doc_id, job_id = UUID(str(doc_id)), UUID(str(job_id))
    if type(ordinal) is not int or not 1 <= ordinal <= 128:
        raise MinerUError("RAW_RESULT_IDENTITY", "MinerU 原始结果片号无效")
    safe_identifier(batch_id)
    path = cache.path(str(doc_id), "mineru_raw", str(job_id), f"part-{ordinal:04d}-{batch_id}.zip")
    for parent in (path, path.with_suffix(".json"), *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise MinerUError("RAW_RESULT_PATH", "MinerU 原始结果目录包含不安全链接")
        if parent == cache.root:
            break
    return path, path.with_suffix(".json")


def load_raw_result(settings, *, doc_id, job_id, ordinal, batch_id, data_id, file_hash):
    """只读核对同批次原始ZIP/回执；缺失返回None，单边/损坏/身份不符明确拒绝。"""
    cache, limit = ParseCache(settings), settings.mineru_result_max_mb * 1024**2
    identity = {"doc_id": str(doc_id), "job_id": str(job_id), "ordinal": ordinal,
                "batch_id": safe_identifier(batch_id), "data_id": safe_identifier(data_id),
                "source_file_hash": file_hash}
    try:
        with cache_guard(cache.root):
            path, receipt = result_paths(cache, doc_id, job_id, ordinal, batch_id)
            if path.exists() or receipt.exists():
                # 中断留下单边文件也不当成 miss，更不能因此追加收费任务。
                record = json.loads(cache.read_bytes(receipt, 16384))
                archive = cache.read_bytes(path, limit)
                if (any(record.get(key) != value for key, value in identity.items())
                        or record.get("sha256") != hashlib.sha256(archive).hexdigest()
                        or record.get("size_bytes") != len(archive)
                        or record.get("zip_path") != path.relative_to(cache.root).as_posix()
                        or record.get("validation") != "validated"
                        or record.get("content_items") != validate_archive(archive, limit)):
                    raise MinerUError("RAW_RESULT_CHANGED", "MinerU 原始 ZIP 或来源记录不一致，未覆盖")
                log_event("mineru.raw_result_reused", doc_id=doc_id, job_id=job_id,
                          batch_id=batch_id, file_index=ordinal, size_bytes=len(archive))
                return archive, record
        return None
    except MinerUError:
        raise
    except (CacheError, OSError, ValueError, TypeError, AttributeError):
        raise MinerUError("RAW_RESULT_IO", "MinerU 原始结果无法安全读取，未下载或覆盖") from None


def obtain_raw_result(provider, url, settings, *, doc_id, job_id, ordinal, batch_id, data_id,
                      file_hash):
    """同Batch优先读归档；首次下载校验后在发布锁内确定路径及回执，不发布成功缓存。"""
    cache, limit = ParseCache(settings), settings.mineru_result_max_mb * 1024**2
    identity = {"doc_id": str(doc_id), "job_id": str(job_id), "ordinal": ordinal,
                "batch_id": safe_identifier(batch_id), "data_id": safe_identifier(data_id),
                "source_file_hash": file_hash}
    try:
        saved = load_raw_result(settings, doc_id=doc_id, job_id=job_id, ordinal=ordinal,
            batch_id=batch_id, data_id=data_id, file_hash=file_hash)
        if saved is not None:
            return saved
        # 网络不占用文件锁；发布时再次检查，避免并发覆盖。
        archive = provider.download_result(url)
        log_event("mineru.result_downloaded", doc_id=doc_id, job_id=job_id,
                  batch_id=batch_id, size_bytes=len(archive))
        count = validate_archive(archive, limit)
        with cache_guard(cache.root):
            path, receipt = result_paths(cache, doc_id, job_id, ordinal, batch_id)
            # load_raw_result的局部路径不属于当前函数；首次发布须先安全定位，再构造回执。
            record = identity | {"zip_path": path.relative_to(cache.root).as_posix(),
                "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive),
                "content_items": count, "validation": "validated"}
            if path.exists() or receipt.exists():
                if (not path.exists() or not receipt.exists() or cache.read_bytes(path, limit) != archive
                        or json.loads(cache.read_bytes(receipt, 16384)) != record):
                    raise MinerUError("RAW_RESULT_CHANGED", "MinerU 原始结果归档冲突，未覆盖")
            else:
                atomic_write(path, archive)
                atomic_write(receipt, canonical(record))
        log_event("mineru.raw_result_saved", doc_id=doc_id, job_id=job_id, batch_id=batch_id,
                  file_index=ordinal, size_bytes=len(archive), item_count=count)
        return archive, record
    except MinerUError as error:
        log_event("mineru.raw_result_rejected", doc_id=doc_id, job_id=job_id,
                  batch_id=batch_id, file_index=ordinal, error_code=error.code)
        raise
    except (CacheError, OSError, ValueError, TypeError, AttributeError):
        log_event("mineru.raw_result_rejected", doc_id=doc_id, job_id=job_id,
                  batch_id=batch_id, file_index=ordinal, error_code="RAW_RESULT_IO")
        raise MinerUError("RAW_RESULT_IO", "MinerU 原始结果无法安全归档或复用，未覆盖已有文件") from None
