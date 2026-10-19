"""原子发布本地 Dense 向量文件，供阶段 15 接入；当前不写 pgvector 或创建索引。"""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from app.services.embedding.service import DIMENSION, MODEL_ID, VERSION, EmbeddingError, validate_vectors

MAX_BYTES = 512 * 1024 * 1024
MAX_ROW_BYTES = 128 * 1024


def digest_json(value) -> str:
    """生成不含时间戳的规范摘要，仅用于冻结参数和输入身份校验。"""

    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ArtifactStore:
    """每个文档/代次保存一个向量 JSONL 和一个完成 manifest，不按 Chunk 创建 Celery 任务。"""

    def __init__(self, root: str | Path):
        """只保存本地根目录，不创建目录、不访问模型；路径属于私有运行配置。"""

        self.root = Path(root).resolve()

    def directory(self, doc_id: UUID, task_id: UUID) -> Path:
        """仅用合法 UUID 构建代次目录，运行结果与其他文档及旧代次相互隔离。"""

        return self.root / str(UUID(str(doc_id))) / str(UUID(str(task_id)))

    def ready(self, doc_id: UUID, task_id: UUID, source_hash: str, options_hash: str,
              chunk_ids: list[str]) -> dict | None:
        """逐行校验完整结果；v2按真实Chunk数推导文件边界，旧v1保留512MiB限制。"""

        directory = self.directory(doc_id, task_id)
        manifest = directory / "manifest.json"
        try:
            if not manifest.exists():
                return None
            if manifest.stat().st_size > 64 * 1024:
                raise ValueError
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (data.get("doc_id") != str(doc_id) or data.get("task_id") != str(task_id)
                    or data.get("source_hash") != source_hash or data.get("options_hash") != options_hash
                    or data.get("chunk_count") != len(chunk_ids)
                    or data.get("format") not in {"dense-jsonl-v1", "dense-jsonl-stream-v2"}
                    or data.get("model", {}).get("dimension") != DIMENSION
                    or data["model"].get("model_name") != MODEL_ID or data["model"].get("version") != VERSION
                    or data["model"].get("normalized") is not True
                    or not isinstance(data["model"].get("model_fingerprint"), str)
                    or len(data["model"]["model_fingerprint"]) != 64):
                raise ValueError
            vectors_file = directory / "vectors.jsonl"
            limit = len(chunk_ids) * MAX_ROW_BYTES if data["format"] == "dense-jsonl-stream-v2" else MAX_BYTES
            if not 0 < vectors_file.stat().st_size <= limit or vectors_file.stat().st_size != data["size_bytes"]:
                raise ValueError
            digest = hashlib.sha256()
            count = total_bytes = 0
            with vectors_file.open("rb") as stream:
                # 先限制单行读取量，再解析；损坏文件不能一次把超大行全部读入内存。
                while line := stream.readline(MAX_ROW_BYTES + 1):
                    total_bytes += len(line)
                    if len(line) > MAX_ROW_BYTES or count >= len(chunk_ids) or total_bytes > limit:
                        raise ValueError
                    row = json.loads(line)
                    if row.get("chunk_id") != chunk_ids[count] or row.get("chunk_index") != count + 1:
                        raise ValueError
                    validate_vectors([row["vector"]], 1)
                    digest.update(line)
                    count += 1
            if count != len(chunk_ids) or total_bytes != data["size_bytes"] or digest.hexdigest() != data["vectors_sha256"]:
                raise ValueError
            return data
        except (OSError, ValueError, TypeError, KeyError, AttributeError, EmbeddingError):
            raise EmbeddingError("EMBEDDING_ARTIFACT") from None

    @contextmanager
    def writer(self, doc_id: UUID, task_id: UUID, *, streaming=False):
        """v2逐行限制工作内存而非整份文件大小；仍只在全部完成后原子发布，不删旧结果。"""

        directory = self.directory(doc_id, task_id)
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="wb", dir=directory, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                writer = VectorWriter(stream, streaming=streaming)
                yield writer
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "vectors.jsonl")
            temporary = None
        except EmbeddingError:
            raise
        except OSError:
            raise EmbeddingError("EMBEDDING_STORAGE") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 仅残留没有完成标记的暂存文件；不让清理错误掩盖原失败，也不发布成功。
                    pass

    def publish(self, doc_id: UUID, task_id: UUID, manifest: dict) -> None:
        """在向量文件完成后原子发布完成标记；文件与 SQL 非同事务，重试需重新校验标记。"""

        directory = self.directory(doc_id, task_id)
        temporary = None
        try:
            payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            with NamedTemporaryFile(mode="wb", dir=directory, prefix=".manifest-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "manifest.json")
            temporary = None
        except (OSError, ValueError, TypeError):
            raise EmbeddingError("EMBEDDING_STORAGE") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class VectorWriter:
    """按源 Chunk 顺序追加有限数值，仅在任务内使用，不写日志或 Celery 返回值。"""

    def __init__(self, stream, *, streaming=False):
        """保存流式策略及小型计数，不收集向量；默认仍采用旧v1预算。"""

        self.stream = stream
        self.streaming = streaming
        self.count = 0
        self.size_bytes = 0
        self.digest = hashlib.sha256()

    def append(self, chunk_id: str, vector: list[float]) -> None:
        """严格校验后写单行；新策略不会因累计字节数拒绝，单行仍有界且不包含原文。"""

        vector = validate_vectors([vector], 1)[0]
        payload = json.dumps({"chunk_id": chunk_id, "chunk_index": self.count + 1, "vector": vector},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(payload) > MAX_ROW_BYTES or not self.streaming and self.size_bytes + len(payload) > MAX_BYTES:
            raise EmbeddingError("EMBEDDING_LIMIT")
        self.stream.write(payload)
        self.digest.update(payload)
        self.size_bytes += len(payload)
        self.count += 1
