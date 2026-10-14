"""用户执行的本地Reranker服务测试；只提供内存文件/Tokenizer/模型Stub，不运行真实模型。"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.core.config import PROJECT_ROOT, Settings
from app.services import reranking as tool
from test_embedding_service import MemoryFile


@pytest.fixture
def stub_service(monkeypatch, tmp_path):
    """用内存文件和批次函数替代权重/模型，记录调用次数，不生成测试素材。"""
    file, loads, calls = MemoryFile(), [], []

    def files(path):
        """只返回内存元数据，不读取或生成本地模型文件。"""
        return [file]

    def load(options):
        """记录惰性加载，由纯占位对象表示既有后端。"""
        loads.append(options)
        return object()

    def score(backend, pairs, options):
        """返回按原文长度区分的logit，包含负分，不做实际token化或推理。"""
        calls.append(pairs)
        return [float(len(text) - 3) for query, text in pairs]

    monkeypatch.setattr(tool, "model_files", files)
    monkeypatch.setattr(tool, "load_local_model", load)
    monkeypatch.setattr(tool, "infer_batch", score)
    service = tool.RerankerService(tool.RerankerOptions(str(tmp_path / "unused"), batch_size=2))
    return service, file, loads, calls


def test_empty_is_lazy_and_batches_keep_original_order_and_logits(stub_service):
    """空候选不取模型；多批候选顺序对应且负/零原logit保留，身份不含目录。"""
    service, _, loads, calls = stub_service
    assert service.score("原始Query", []) == [] and loads == []
    texts = ["甲", "中文", "第三段", "第四内容", "第五个文本"]
    assert service.score("原始Query", texts) == [float(len(text) - 3) for text in texts]
    assert calls == [[("原始Query", text) for text in batch] for batch in (texts[:2], texts[2:4], texts[4:])]
    assert len(loads) == 1
    identity = service.identity()
    assert identity["model_name"] == tool.MODEL_ID and identity["score_type"] == "logit"
    assert len(identity["model_fingerprint"]) == 64 and "model_dir" not in identity


@pytest.mark.parametrize("query,passages", [("", []), (None, []), ("PRIVATE\x00", ["text"]),
    ("question", "PRIVATE_BODY"), ("question", [None]), ("question", [""])])
def test_invalid_inputs_never_load_model(stub_service, query, passages):
    """无效输入先安全拒绝，不能读取模型或泄露正文。"""
    service, _, loads, _ = stub_service
    with pytest.raises(tool.RerankError, match="RERANK_INPUT") as error:
        service.score(query, passages)
    assert loads == [] and "PRIVATE" not in str(error.value)


def test_candidate_limit_and_model_changes_are_rejected(stub_service):
    """候选预算拒绝发生在加载前；后续文件变化不得继续声称相同模型。"""
    service, file, loads, _ = stub_service
    with pytest.raises(tool.RerankError, match="RERANK_LIMIT"):
        service.score("question", ["text"] * 101)
    assert loads == []
    service.score("question", ["text"])
    file.modified += 1
    with pytest.raises(tool.RerankError, match="RERANK_MODEL_CHANGED"):
        service.score("question", ["text"])


@pytest.mark.parametrize("values", [[], [None], [True], ["PRIVATE"], [float("nan")], [float("inf")], [[1.0]]])
def test_invalid_output_cannot_become_probability_or_partial_success(values):
    """数量/类型/非有限/多标签输出拒绝，异常不透传原始分数对象。"""
    with pytest.raises(tool.RerankError, match="RERANK_OUTPUT") as error:
        tool.validate_scores(values, 1)
    assert "PRIVATE" not in str(error.value)


def test_late_batch_failure_returns_no_partial_result(stub_service, monkeypatch):
    """后续批次OOM等失败只能整体错误，不返回先前批次或原检索排序。"""
    service, _, _, calls = stub_service

    def failing(backend, pairs, options):
        """记录已到达批次，第二批注入私有异常，不执行任何模型。"""
        calls.append(pairs)
        if len(calls) == 2:
            raise RuntimeError("PRIVATE_BODY C:/private/model")
        return [1.0] * len(pairs)

    monkeypatch.setattr(tool, "infer_batch", failing)
    with pytest.raises(tool.RerankError, match="RERANK_INFERENCE") as error:
        service.score("question", ["一", "二", "三"])
    assert len(calls) == 2 and "PRIVATE_BODY" not in str(error.value) and "private" not in str(error.value)


def test_default_getter_is_shared_and_pid_partitioned(monkeypatch, tmp_path):
    """默认工厂锁内复用同配置服务，PID变化创建新惰性服务而非继承CUDA对象。"""
    options = tool.RerankerOptions(str(tmp_path / "unused"))
    state = SimpleNamespace(pid=123)

    def process_id():
        """返回可控进程编号，不实际启动子进程或GPU。"""
        return state.pid

    monkeypatch.setattr(tool.os, "getpid", process_id)
    tool.cached_service.cache_clear()
    try:
        first = tool.get_reranker_service(options)
        assert first is tool.get_reranker_service(options) and first._backend is None
        state.pid = 124
        assert tool.get_reranker_service(options) is not first
    finally:
        tool.cached_service.cache_clear()


def test_configuration_paths_are_project_relative_and_cpu_is_explicit():
    """模型目录不依赖当前工作目录，CPU仅由明确配置选择，没有CUDA隐式回退。"""
    settings = Settings(_env_file=None, reranker_device="cpu", reranker_batch_size=1)
    options = tool.RerankerOptions.from_settings(settings)
    assert Path(options.model_dir) == PROJECT_ROOT / "models/bge-reranker-v2-m3"
    assert options.device == "cpu" and options.batch_size == 1


class ModelStub:
    """纯状态模型，用于校验离线加载/设备和精度约束，不运行计算。"""

    def __init__(self):
        """保存单标签结构与调用记录，不配置真实GPU。"""
        self.config = SimpleNamespace(num_labels=1, model_type="xlm-roberta")
        self.calls = []

    def half(self):
        """记录半精度设置，不能实际转换Tensor。"""
        self.calls.append("half")

    def to(self, device):
        """记录显式设备，不检查或占用显存。"""
        self.calls.append(device)

    def eval(self):
        """记录推理状态，无真实权重。"""
        self.calls.append("eval")


def test_loader_uses_local_safe_weights_and_never_falls_back(monkeypatch, tmp_path):
    """Transformers只读本地Safetensors、禁止远程代码；CUDA不可用立即错误。"""
    model, calls = ModelStub(), []

    def tokenizer(path, **kwargs):
        """替代Tokenizer加载，不读权重或下载资源。"""
        assert kwargs == {"local_files_only": True, "trust_remote_code": False}
        calls.append(path)
        return object()

    def classifier(path, **kwargs):
        """替代分类模型，必须使用安全离线权重参数。"""
        assert kwargs == {"local_files_only": True, "trust_remote_code": False,
                          "use_safetensors": True, "output_loading_info": True}
        calls.append(path)
        return model, {"missing_keys": [], "mismatched_keys": [], "error_msgs": []}

    state = SimpleNamespace(available=True)

    def available():
        """返回模拟CUDA状态，不连接本机驱动。"""
        return state.available

    def device_count():
        """Stub固定一张可用GPU，不读取硬件。"""
        return 1

    torch_stub = SimpleNamespace(cuda=SimpleNamespace(is_available=available, device_count=device_count))
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer),
        AutoModelForSequenceClassification=SimpleNamespace(from_pretrained=classifier)))
    options = tool.RerankerOptions(str(tmp_path / "unused"))
    assert tool.load_local_model(options)[1] is model
    assert model.calls == ["half", "cuda:0", "eval"] and len(calls) == 2
    state.available = False
    with pytest.raises(tool.RerankError, match="RERANK_GPU_UNAVAILABLE"):
        tool.load_local_model(options)
    assert len(calls) == 2

    state.available = True

    def missing_classifier(path, **kwargs):
        """模拟错误权重缺少分类头，禁止随机初始化后返回看似有效的分数。"""
        return model, {"missing_keys": ["classifier.out_proj.weight"], "mismatched_keys": [], "error_msgs": []}

    sys.modules["transformers"].AutoModelForSequenceClassification.from_pretrained = missing_classifier
    with pytest.raises(tool.RerankError, match="RERANK_MODEL_LOAD"):
        tool.load_local_model(options)


def test_pair_token_limit_rejects_before_inference(monkeypatch, tmp_path):
    """真实Query+正文+特殊token总长度超限不得截断，Tokenizer.pad/模型均不得调用。"""
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))

    def tokenizer(pairs, **kwargs):
        """返回含配对特殊token的模拟长度，显式要求关闭截断。"""
        assert kwargs["truncation"] is False and kwargs["padding"] is False
        return {"input_ids": [[0] * 9]}

    options = tool.RerankerOptions(str(tmp_path / "unused"), max_length=8)
    with pytest.raises(tool.RerankError, match="RERANK_TOO_LONG"):
        tool.infer_batch((tokenizer, object()), [("question", "passage")], options)


def test_inference_pair_padding_device_and_single_logit_contract(monkeypatch, tmp_path):
    """用纯映射/数值Stub覆盖成对token化、设备、单标签形状和顺序，不调用真实Torch。"""
    calls, shape = [], SimpleNamespace(value=(2, 1))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))

    class InputsStub(dict):
        """仅模拟BatchEncoding的映射及设备方法，不包含真实Tensor。"""

        def to(self, device):
            """记录指定设备，返回同一纯字典供模型参数展开。"""
            calls.append(device)
            return self

    class LogitsStub:
        """模拟单标签列的有限分数及必要转换方法，不进行浮点计算。"""

        @property
        def shape(self):
            """返回可控形状，第二轮模拟不合法多标签输出。"""
            return shape.value

        def __getitem__(self, key):
            """必须读取整批第0标签列，不把不同标签展平充当多份候选。"""
            assert key == (slice(None), 0)
            return self

        def float(self):
            """模拟FP32输出转换，不加载dtype运行时。"""
            return self

        def cpu(self):
            """模拟数值回CPU，不触发设备拷贝。"""
            return self

        def tolist(self):
            """返回输入对应的负分和零分，不执行sigmoid。"""
            return [-2.0, 0.0]

    def tokenizer(pairs, **kwargs):
        """确认原Query/正文配对不变，返回每对含特殊token的完整长度。"""
        assert pairs == [("query", "first"), ("query", "second")]
        assert kwargs == {"add_special_tokens": True, "padding": False, "truncation": False}
        return {"input_ids": [[0, 1, 2], [0, 1, 2, 3]]}

    def pad(encoded, **kwargs):
        """模拟批次padding，不再执行截断或隐式设备选择。"""
        assert kwargs == {"padding": True, "return_tensors": "pt"}
        return InputsStub(encoded)

    def model(**kwargs):
        """确认按原批次完整输入推理，返回可控单标签输出Stub。"""
        assert kwargs["return_dict"] is True and len(kwargs["input_ids"]) == 2
        return SimpleNamespace(logits=LogitsStub())

    tokenizer.pad = pad
    options = tool.RerankerOptions(str(tmp_path / "unused"), device="cpu", max_length=8)
    pairs = [("query", "first"), ("query", "second")]
    assert tool.infer_batch((tokenizer, model), pairs, options) == [-2.0, 0.0]
    assert calls == ["cpu"]
    shape.value = (2, 2)
    with pytest.raises(tool.RerankError, match="RERANK_OUTPUT"):
        tool.infer_batch((tokenizer, model), pairs, options)


def test_missing_local_model_fails_before_dependency_import(tmp_path):
    """缺本地目录只能给出手动部署提示，不用仓库ID自动下载。"""
    service = tool.RerankerService(tool.RerankerOptions(str(tmp_path / "missing")))
    with pytest.raises(tool.RerankError, match="RERANK_MODEL_MISSING"):
        service.score("question", ["passage"])
