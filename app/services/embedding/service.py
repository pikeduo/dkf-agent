"""本地模型惰性加载、无截断长度校验和一致的 1024 维归一化 Dense 编码。"""

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from numbers import Real
from pathlib import Path
from threading import RLock
from time import perf_counter

from app.core.business_logging import log_event
from app.core.config import PROJECT_ROOT, Settings, get_settings

MODEL_ID = "BAAI/bge-m3"
DIMENSION = 1024
VERSION = "bge-m3-dense-v1"
MAX_TEXT_CHARS = 1_000_000
MAX_TOTAL_CHARS = 10_000_000
MAX_ITEMS = 20_000
ERRORS = {
    "EMBEDDING_CONFIG": "Embedding 配置或版本不可用",
    "EMBEDDING_MODEL_MISSING": "本地 BGE-M3 模型不完整，请手动下载官方模型",
    "EMBEDDING_MODEL_LOAD": "本地模型加载失败，请核对依赖与模型文件",
    "EMBEDDING_MODEL_CHANGED": "模型文件已变化，请停止 Worker 并核对部署版本",
    "EMBEDDING_GPU_REQUIRED": "文档向量化必须使用 CUDA GPU 配置",
    "EMBEDDING_GPU_UNAVAILABLE": "指定 CUDA GPU 不可用，请核对驱动与 PyTorch CUDA 版本",
    "EMBEDDING_INPUT": "Embedding 输入必须是非空文本列表",
    "EMBEDDING_LIMIT": "Embedding 输入超过本地资源上限",
    "EMBEDDING_TOO_LONG": "输入超过模型真实 token 上限，未截断或生成不完整向量",
    "EMBEDDING_OUTPUT": "模型输出数量、维度或数值无效",
    "EMBEDDING_INFERENCE": "本地模型推理失败，请检查 GPU 显存与运行环境",
    "EMBEDDING_EMPTY": "文档没有可向量化的 Chunk",
    "EMBEDDING_STATE": "文档尚未完成切片或当前状态不允许向量化",
    "EMBEDDING_BUSY": "文档仍有活动解析任务",
    "EMBEDDING_CHECKPOINT": "向量化检查点与当前 Chunk 代次不一致",
    "EMBEDDING_SOURCE": "Chunk 身份、顺序或内容已变化，请核对切片结果",
    "EMBEDDING_STORAGE": "本地向量结果保存失败，未发布半份结果",
    "EMBEDDING_ARTIFACT": "本地向量结果缺失、损坏或与当前任务不一致",
}


class EmbeddingError(Exception):
    """仅输出固定错误码及中文原因，禁止透传模型、正文或本机路径异常。"""

    def __init__(self, code: str, *, item_index=None, token_count=None, limit_count=None,
                 item_count=None, step=None):
        """长度/资源错误只附受限序号、计数和固定类别，不携带正文或模型原始异常。"""

        self.code = code if code in ERRORS else "EMBEDDING_INFERENCE"
        self.diagnostic = {key: value for key, value in {
            "item_index": item_index, "token_count": token_count, "limit_count": limit_count,
            "item_count": item_count}.items()
            if self.code in {"EMBEDDING_TOO_LONG", "EMBEDDING_LIMIT"}
            and type(value) is int and 0 < value <= 2**31 - 1}
        if (self.code == "EMBEDDING_LIMIT" and isinstance(step, str)
                and step in {"TEXT_CHAR_LIMIT", "TOTAL_CHAR_LIMIT", "ITEM_COUNT_LIMIT"}):
            self.diagnostic["step"] = step
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class EmbeddingOptions:
    """冻结本地部署参数；模型身份固定，维度和归一化策略不能随调用方任意改变。"""

    model_dir: str
    device: str = "cuda:0"
    batch_size: int = 16
    max_length: int = 8192
    use_fp16: bool = True
    version: str = VERSION

    @classmethod
    def from_settings(cls, settings: Settings) -> "EmbeddingOptions":
        """解析项目相对模型路径；不检查 GPU、不读模型文件，也不触发下载。"""

        try:
            path = settings.embedding_model_dir
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            options = cls(str(path.resolve()), settings.embedding_device, settings.embedding_batch_size,
                          settings.embedding_max_length, settings.embedding_use_fp16)
        except (OSError, ValueError, TypeError):
            raise EmbeddingError("EMBEDDING_CONFIG") from None
        options.validate()
        return options

    def validate(self) -> None:
        """拒绝多设备、未知算法和不安全长度；CPU 仅供显式 Service 调用，文档任务另行拒绝。"""

        if (not isinstance(self.model_dir, str) or not Path(self.model_dir).is_absolute()
                or not isinstance(self.device, str) or re.fullmatch(r"cpu|cuda:[0-9]+", self.device) is None
                or type(self.batch_size) is not int or not 1 <= self.batch_size <= 128
                or type(self.max_length) is not int or not 8 <= self.max_length <= 8192
                or type(self.use_fp16) is not bool or self.version != VERSION):
            raise EmbeddingError("EMBEDDING_CONFIG")

    def snapshot(self) -> dict:
        """返回私有数据库检查点配置，不应透传到日志、HTTP 响应或 Celery 消息。"""

        self.validate()
        return {"model_dir": self.model_dir, "device": self.device, "batch_size": self.batch_size,
                "max_length": self.max_length, "use_fp16": self.use_fp16, "version": self.version}


def validate_vectors(vectors, expected_count: int) -> list[list[float]]:
    """校验顺序对应的数量、1024 维、有限非零向量，并以双精度计算 L2 归一化。"""

    try:
        if hasattr(vectors, "tolist"):
            vectors = vectors.tolist()
        if not isinstance(vectors, list) or len(vectors) != expected_count:
            raise ValueError
        result = []
        for vector in vectors:
            if not isinstance(vector, (list, tuple)) or len(vector) != DIMENSION:
                raise ValueError
            if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) for value in vector):
                raise ValueError
            row = [float(value) for value in vector]
            norm = math.sqrt(math.fsum(value * value for value in row))
            if not math.isfinite(norm) or norm <= 1e-12:
                raise ValueError
            result.append([value / norm for value in row])
        return result
    except (ValueError, TypeError, OverflowError):
        raise EmbeddingError("EMBEDDING_OUTPUT") from None


def model_files(path: Path) -> list[Path]:
    """检查官方 SentenceTransformer 配置、Tokenizer 与权重；只接受本地目录内的标准部件。"""

    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        modules = json.loads((path / "modules.json").read_text(encoding="utf-8"))
        if config.get("hidden_size") != DIMENSION or config.get("model_type") != "xlm-roberta":
            raise ValueError
        if (not isinstance(modules, list) or not modules
                or any(module.get("type") not in {"sentence_transformers.models.Transformer",
                        "sentence_transformers.models.Pooling", "sentence_transformers.models.Normalize"}
                       or Path(module.get("path", "")).is_absolute()
                       or ".." in Path(module.get("path", "")).parts for module in modules)):
            raise ValueError
        if not (path / "tokenizer.json").is_file() or not (path / "tokenizer_config.json").is_file():
            raise ValueError
        files = sorted(file for file in path.rglob("*") if file.is_file()
                       and ".cache" not in file.relative_to(path).parts
                       and file.suffix in {".json", ".safetensors", ".bin", ".model"}
                       and "onnx" not in file.relative_to(path).parts)
        if not files or len(files) > 100 or not any(file.suffix in {".safetensors", ".bin"} for file in files):
            raise ValueError
        if any(not file.resolve().is_relative_to(path.resolve()) for file in files):
            raise ValueError
        return files
    except (OSError, ValueError, TypeError, AttributeError):
        raise EmbeddingError("EMBEDDING_MODEL_MISSING") from None


def model_stamp(files: list[Path]) -> tuple:
    """记录文件大小/修改时间/路径以检测运行期间模型被替换；不在日志中输出此私有快照。"""

    try:
        return tuple((str(file), file.stat().st_size, file.stat().st_mtime_ns) for file in files)
    except OSError:
        raise EmbeddingError("EMBEDDING_MODEL_CHANGED") from None


def load_local_model(options: EmbeddingOptions):
    """仅在首次编码时导入既有依赖，显式检查 CUDA；离线加载、禁止远程代码与自动 CPU 回退。"""

    try:
        import torch
        from sentence_transformers import SentenceTransformer

        if options.device.startswith("cuda:"):
            if not torch.cuda.is_available() or int(options.device.split(":")[1]) >= torch.cuda.device_count():
                raise EmbeddingError("EMBEDDING_GPU_UNAVAILABLE")
        model = SentenceTransformer(options.model_dir, device=options.device,
                                    local_files_only=True, trust_remote_code=False)
        if model.get_sentence_embedding_dimension() != DIMENSION:
            raise EmbeddingError("EMBEDDING_OUTPUT")
        model.max_seq_length = options.max_length
        if options.use_fp16 and options.device.startswith("cuda:"):
            model.half()
        model.eval()
        return model
    except EmbeddingError:
        raise
    except Exception:
        raise EmbeddingError("EMBEDDING_MODEL_LOAD") from None


class EmbeddingService:
    """文档与 Query 复用同一个模型/Tokenizer/归一化策略，不依赖数据库或任务队列。"""

    def __init__(self, options: EmbeddingOptions):
        """只保存参数和进程内锁；构造或导入服务不会导入 Torch、加载模型或联网。"""

        options.validate()
        self.options = options
        self._model = None
        self._files: list[Path] = []
        self._stamp: tuple = ()
        self._fingerprint: str | None = None
        self._lock = RLock()

    def _ensure_model(self) -> None:
        """首次读取标准文件校验和并加载模型；每次编码拒绝运行中变化的部署文件。"""

        if self._model is not None:
            if model_stamp(self._files) != self._stamp:
                raise EmbeddingError("EMBEDDING_MODEL_CHANGED")
            return
        started = perf_counter()
        path = Path(self.options.model_dir)
        files = model_files(path)
        before = model_stamp(files)
        digest = hashlib.sha256()
        try:
            for file in files:
                digest.update(file.relative_to(path).as_posix().encode())
                with file.open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
        except OSError:
            raise EmbeddingError("EMBEDDING_MODEL_MISSING") from None
        model = load_local_model(self.options)
        if model_stamp(files) != before:
            raise EmbeddingError("EMBEDDING_MODEL_CHANGED")
        self._files, self._stamp, self._fingerprint, self._model = files, before, digest.hexdigest(), model
        log_event("embedding.model_loaded", model_name=MODEL_ID, dimension=DIMENSION,
                  device=self.options.device, duration_ms=(perf_counter() - started) * 1000)

    def identity(self) -> dict:
        """获取实际模型文件指纹；首次调用会加载本地模型，不应在 API 启动/投递请求中调用。"""

        with self._lock:
            self._ensure_model()
            return {"model_name": MODEL_ID, "model_fingerprint": self._fingerprint,
                    "dimension": DIMENSION, "normalized": True, "version": VERSION,
                    "max_length": self.options.max_length,
                    "precision": "fp16" if self.options.use_fp16 and self.options.device.startswith("cuda:") else "fp32"}

    def _encode(self, texts: list[str], operation: str) -> list[list[float]]:
        """逐批核对字符资源和真实长度，记录具体类别/序号；不截断或返回半份向量。"""

        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            log_event("embedding.encode_failed", level=logging.WARNING, operation=operation, error_code="EMBEDDING_INPUT")
            raise EmbeddingError("EMBEDDING_INPUT")
        limit_error = None
        too_large = next((index for index, text in enumerate(texts, 1) if len(text) > MAX_TEXT_CHARS), None)
        if len(texts) > MAX_ITEMS:
            limit_error = EmbeddingError("EMBEDDING_LIMIT", step="ITEM_COUNT_LIMIT",
                                         item_count=len(texts), limit_count=MAX_ITEMS)
        elif too_large is not None:
            limit_error = EmbeddingError("EMBEDDING_LIMIT", step="TEXT_CHAR_LIMIT", item_index=too_large,
                                         item_count=len(texts[too_large - 1]), limit_count=MAX_TEXT_CHARS)
        elif sum(len(text) for text in texts) > MAX_TOTAL_CHARS:
            limit_error = EmbeddingError("EMBEDDING_LIMIT", step="TOTAL_CHAR_LIMIT",
                                         item_count=sum(len(text) for text in texts), limit_count=MAX_TOTAL_CHARS)
        if limit_error is not None:
            log_event("embedding.encode_failed", level=logging.WARNING, operation=operation,
                      error_code=limit_error.code, **limit_error.diagnostic)
            raise limit_error
        if not texts:
            log_event("embedding.encode_skipped", operation=operation, item_count=0)
            return []
        started = perf_counter()
        try:
            with self._lock:
                self._ensure_model()
                result = []
                for offset in range(0, len(texts), self.options.batch_size):
                    batch = texts[offset:offset + self.options.batch_size]
                    tokens = self._model.tokenizer(batch, add_special_tokens=True, padding=False,
                                                   truncation=False, return_attention_mask=False)
                    ids = tokens["input_ids"]
                    if len(ids) != len(batch):
                        raise EmbeddingError("EMBEDDING_OUTPUT")
                    for index, row in enumerate(ids):
                        if len(row) > self.options.max_length:
                            raise EmbeddingError("EMBEDDING_TOO_LONG", item_index=offset + index + 1,
                                token_count=len(row), limit_count=self.options.max_length)
                    vectors = self._model.encode(batch, batch_size=self.options.batch_size,
                        convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False,
                        prompt="", device=self.options.device)
                    result.extend(validate_vectors(vectors, len(batch)))
                if model_stamp(self._files) != self._stamp:
                    raise EmbeddingError("EMBEDDING_MODEL_CHANGED")
        except EmbeddingError as exc:
            log_event("embedding.encode_failed", level=logging.ERROR, operation=operation,
                      error_code=exc.code, **exc.diagnostic)
            raise
        except Exception:
            log_event("embedding.encode_failed", level=logging.ERROR, operation=operation, error_code="EMBEDDING_INFERENCE")
            raise EmbeddingError("EMBEDDING_INFERENCE") from None
        log_event("embedding.encoded", operation=operation, item_count=len(texts), dimension=DIMENSION,
                  batch_count=math.ceil(len(texts) / self.options.batch_size),
                  duration_ms=(perf_counter() - started) * 1000)
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """按输入顺序批量生成文档 Dense 向量；空列表不加载模型，异常不返回半份结果。"""

        return self._encode(texts, "documents")

    def embed_query(self, text: str) -> list[float]:
        """使用与文档完全一致的编码路径返回单个 1024 维 Query 向量，不持久化查询原文。"""

        return self._encode([text], "query")[0]


@lru_cache(maxsize=1)
def cached_service(options: EmbeddingOptions, process_id: int) -> EmbeddingService:
    """仅在当前进程缓存一个惰性服务，防止继承其他进程的活跃 CUDA 模型。"""

    return EmbeddingService(options)


def get_embedding_service(options: EmbeddingOptions | None = None) -> EmbeddingService:
    """获取进程内共享服务；读取配置不会加载模型，配置改变后应重启所有进程。"""

    return cached_service(options or EmbeddingOptions.from_settings(get_settings()), os.getpid())


def embed_documents(texts: list[str]) -> list[list[float]]:
    """统一函数入口，批量编码不创建任何 Celery 子任务。"""

    return get_embedding_service().embed_documents(texts)


def embed_query(text: str) -> list[float]:
    """统一 Query 入口，模型和维度策略与文档相同；未来检索调用此函数。"""

    return get_embedding_service().embed_query(text)
