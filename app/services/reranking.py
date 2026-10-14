"""项目统一的本地BGE Reranker服务：惰性共享、原文对批处理、有限原始logit输出。"""

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

MODEL_ID = "BAAI/bge-reranker-v2-m3"
VERSION = "bge-reranker-v2-m3-logit-v1"
MAX_CANDIDATES = 100
MAX_PASSAGE_CHARS = 1_000_000
MAX_TOTAL_CHARS = 2_000_000
MAX_QUERY_CHARS = 32768
ERRORS = {
    "RERANK_CONFIG": "Reranker部署参数无效",
    "RERANK_INPUT": "Query或候选证据无效",
    "RERANK_LIMIT": "重排候选数量或文本资源超过安全上限",
    "RERANK_MODEL_MISSING": "本地BGE Reranker模型不完整，请手动下载官方Safetensors模型",
    "RERANK_MODEL_LOAD": "本地Reranker加载失败，请核对已有依赖和模型部署",
    "RERANK_MODEL_CHANGED": "Reranker模型文件或身份已变化，本次未返回结果",
    "RERANK_GPU_UNAVAILABLE": "指定CUDA GPU不可用，不自动回退CPU",
    "RERANK_TOO_LONG": "Query与候选的真实token长度超限，未截断正文",
    "RERANK_OUTPUT": "Reranker输出数量、形状或数值无效",
    "RERANK_INFERENCE": "本地重排推理失败，请检查显存和部署环境",
    "RERANK_RESULT": "候选的来源、原分数或证据无效，未返回部分结果",
}


class RerankError(Exception):
    """安全中文错误，不携带正文、Query、模型路径或上游异常原文。"""

    def __init__(self, code: str):
        """把未知分类映射为固定失败，避免外部异常文本污染输出。"""
        self.code = code if code in ERRORS else "RERANK_INFERENCE"
        super().__init__(f"{self.code}：{ERRORS[self.code]}")


@dataclass(frozen=True)
class RerankerOptions:
    """冻结本地推理配置；设备显式选择，长正文不被自动截断。"""

    model_dir: str
    device: str = "cuda:0"
    batch_size: int = 2
    max_length: int = 8192
    use_fp16: bool = True

    def validate(self) -> None:
        """拒绝非本地模型路径、非法设备、布尔整数及过大的批次/长度。"""
        if (not isinstance(self.model_dir, str) or not Path(self.model_dir).is_absolute()
                or not isinstance(self.device, str) or re.fullmatch(r"cpu|cuda:[0-9]+", self.device) is None
                or type(self.batch_size) is not int or not 1 <= self.batch_size <= 32
                or type(self.max_length) is not int or not 8 <= self.max_length <= 8192
                or type(self.use_fp16) is not bool):
            raise RerankError("RERANK_CONFIG")

    @classmethod
    def from_settings(cls, settings: Settings) -> "RerankerOptions":
        """相对目录固定到项目根；仅解析配置，不读权重或检查CUDA。"""
        try:
            path = settings.reranker_model_dir
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            options = cls(str(path.resolve()), settings.reranker_device, settings.reranker_batch_size,
                          settings.reranker_max_length, settings.reranker_use_fp16)
        except (OSError, ValueError, TypeError, AttributeError):
            raise RerankError("RERANK_CONFIG") from None
        options.validate()
        return options


def validate_inputs(query: str, passages: list[str]) -> None:
    """校验完整原文对和资源预算；不修剪、改写、记录或拆分Query/正文。"""
    if (not isinstance(query, str) or not query.strip()
            or any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in query)
            or not isinstance(passages, list)
            or any(not isinstance(text, str) or not text.strip() for text in passages)):
        raise RerankError("RERANK_INPUT")
    if (len(query) > MAX_QUERY_CHARS or len(passages) > MAX_CANDIDATES
            or any(len(text) > MAX_PASSAGE_CHARS for text in passages)
            or len(query) * len(passages) + sum(len(text) for text in passages) > MAX_TOTAL_CHARS):
        raise RerankError("RERANK_LIMIT")


def validate_scores(values, count: int) -> list[float]:
    """严格保留每对原始有限logit及输入顺序，接受负分/零，不做sigmoid或概率化。"""
    if (not isinstance(values, list) or len(values) != count
            or any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
                   for value in values)):
        raise RerankError("RERANK_OUTPUT")
    return [float(value) for value in values]


def model_files(path: Path) -> list[Path]:
    """检查官方单标签XLM-R分类结构、Tokenizer及完整Safetensors分片，只接受目录内文件。"""
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        if (config.get("model_type") != "xlm-roberta" or config.get("hidden_size") != 1024
                or config.get("architectures") != ["XLMRobertaForSequenceClassification"]
                or config.get("id2label") != {"0": "LABEL_0"}):
            raise ValueError
        required = [path / name for name in ("config.json", "tokenizer.json", "tokenizer_config.json")]
        index = path / "model.safetensors.index.json"
        if index.is_file():
            names = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
            if (not names or len(names) > 64 or any(not isinstance(name, str)
                    or re.fullmatch(r"model-[0-9]+-of-[0-9]+\.safetensors", name) is None for name in names)):
                raise ValueError
            required.extend([index] + [path / name for name in sorted(names)])
        else:
            required.append(path / "model.safetensors")
        files = sorted(file for file in path.iterdir()
                       if file.is_file() and file.suffix in {".json", ".safetensors", ".model"})
        if (len(files) > 100 or any(not file.is_file() or file.stat().st_size == 0 for file in required)
                or any(not file.resolve().is_relative_to(path.resolve()) for file in files)):
            raise ValueError
        return files
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RerankError("RERANK_MODEL_MISSING") from None


def model_stamp(files: list[Path]) -> tuple:
    """记录私有文件大小/时间以检测部署替换，不把路径或快照写日志。"""
    try:
        return tuple((str(file), file.stat().st_size, file.stat().st_mtime_ns) for file in files)
    except OSError:
        raise RerankError("RERANK_MODEL_CHANGED") from None


def load_local_model(options: RerankerOptions):
    """首次使用才导入已有Torch/Transformers；离线可信代码加载，设备固定，无下载/CPU回退。"""
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        if options.device.startswith("cuda:") and (not torch.cuda.is_available()
                or int(options.device.split(":")[1]) >= torch.cuda.device_count()):
            raise RerankError("RERANK_GPU_UNAVAILABLE")
        tokenizer = AutoTokenizer.from_pretrained(options.model_dir, local_files_only=True, trust_remote_code=False)
        model, loading = AutoModelForSequenceClassification.from_pretrained(options.model_dir,
            local_files_only=True, trust_remote_code=False, use_safetensors=True, output_loading_info=True)
        # 缺失分类头不能让Transformers随机初始化后冒充官方重排权重。
        if any(loading.get(key) for key in ("missing_keys", "mismatched_keys", "error_msgs")):
            raise RerankError("RERANK_MODEL_LOAD")
        if model.config.num_labels != 1 or model.config.model_type != "xlm-roberta":
            raise RerankError("RERANK_OUTPUT")
        if options.use_fp16 and options.device.startswith("cuda:"):
            model.half()
        model.to(options.device)
        model.eval()
        return tokenizer, model
    except RerankError:
        raise
    except Exception:
        raise RerankError("RERANK_MODEL_LOAD") from None


def infer_batch(backend, pairs: list[tuple[str, str]], options: RerankerOptions) -> list[float]:
    """先检查真实成对token总长再padding推理；只返回单标签原始logit，不截断候选。"""
    import torch

    tokenizer, model = backend
    encoded = tokenizer(pairs, add_special_tokens=True, padding=False, truncation=False)
    ids = encoded["input_ids"]
    if len(ids) != len(pairs) or any(not row for row in ids):
        raise RerankError("RERANK_OUTPUT")
    if any(len(row) > options.max_length for row in ids):
        raise RerankError("RERANK_TOO_LONG")
    inputs = tokenizer.pad(encoded, padding=True, return_tensors="pt").to(options.device)
    with torch.inference_mode():
        logits = model(**inputs, return_dict=True).logits
        if tuple(logits.shape) != (len(pairs), 1):
            raise RerankError("RERANK_OUTPUT")
        return validate_scores(logits[:, 0].float().cpu().tolist(), len(pairs))


class RerankerService:
    """项目模型唯一管理边界，Adapter只引用服务；锁内批处理，失败不返回部分分数。"""

    def __init__(self, options: RerankerOptions):
        """保存配置及进程锁，构造不读取权重、不导入Torch或启动模型。"""
        options.validate()
        self.options = options
        self._backend, self._files, self._stamp, self._fingerprint = None, [], (), None
        self._lock = RLock()

    def _ensure_model(self) -> None:
        """首次冻结实际文件指纹并加载本地模型；后续拒绝同进程部署文件被修改。"""
        if self._backend is not None:
            if model_stamp(self._files) != self._stamp:
                raise RerankError("RERANK_MODEL_CHANGED")
            return
        started, path = perf_counter(), Path(self.options.model_dir)
        files = model_files(path)
        before, digest = model_stamp(files), hashlib.sha256()
        try:
            for file in files:
                digest.update(file.relative_to(path).as_posix().encode())
                with file.open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
            backend = load_local_model(self.options)
            if model_stamp(files) != before:
                raise RerankError("RERANK_MODEL_CHANGED")
        except OSError:
            raise RerankError("RERANK_MODEL_MISSING") from None
        self._backend, self._files, self._stamp, self._fingerprint = backend, files, before, digest.hexdigest()
        log_event("rerank.model_loaded", model_name=MODEL_ID, device=self.options.device,
                  duration_ms=(perf_counter() - started) * 1000)

    def identity(self) -> dict:
        """返回实际部署指纹及logit版本，首次调用可能加载模型，不暴露本地目录。"""
        with self._lock:
            self._ensure_model()
            return {"model_name": MODEL_ID, "model_fingerprint": self._fingerprint, "version": VERSION,
                    "max_length": self.options.max_length,
                    "precision": "fp16" if self.options.use_fp16 and self.options.device.startswith("cuda:") else "fp32",
                    "score_type": "logit"}

    def score(self, query: str, passages: list[str]) -> list[float]:
        """同步对完整原文对分批评分；空候选不取模型，任何错误不返回半份列表或静默原排序。"""
        started = perf_counter()
        try:
            validate_inputs(query, passages)
            passages = list(passages)
            if not passages:
                log_event("rerank.skipped", item_count=0)
                return []
            with self._lock:
                self._ensure_model()
                scores = []
                for offset in range(0, len(passages), self.options.batch_size):
                    batch = passages[offset:offset + self.options.batch_size]
                    pairs = [(query, text) for text in batch]
                    scores.extend(validate_scores(infer_batch(self._backend, pairs, self.options), len(batch)))
                if model_stamp(self._files) != self._stamp:
                    raise RerankError("RERANK_MODEL_CHANGED")
            scores = validate_scores(scores, len(passages))
        except Exception as exc:
            error = exc if isinstance(exc, RerankError) else RerankError("RERANK_INFERENCE")
            log_event("rerank.score_failed", level=logging.WARNING, error_code=error.code,
                      duration_ms=(perf_counter() - started) * 1000)
            raise error from None
        log_event("rerank.scored", item_count=len(scores), model_name=MODEL_ID,
                  batch_count=math.ceil(len(scores) / self.options.batch_size),
                  duration_ms=(perf_counter() - started) * 1000)
        return scores


_SERVICE_LOCK = RLock()


@lru_cache(maxsize=1)
def cached_service(options: RerankerOptions, process_id: int) -> RerankerService:
    """缓存当前进程惰性服务，PID参与键以避免复用父进程CUDA对象。"""
    return RerankerService(options)


def get_reranker_service(options: RerankerOptions | None = None) -> RerankerService:
    """序列化首次缓存创建；默认Adapter/项目调用共享同一服务，不加载第二份Reranker。"""
    with _SERVICE_LOCK:
        return cached_service(options or RerankerOptions.from_settings(get_settings()), os.getpid())
