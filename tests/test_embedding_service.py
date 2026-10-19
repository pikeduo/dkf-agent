"""仅用内存模型/Tokenizer Stub 验证服务，不下载或加载模型，不创建文档验收样本。"""

from io import BytesIO
from math import isclose, sqrt
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from app.core.config import Settings
from app.services.embedding import service as module
from app.services.embedding.service import DIMENSION, EmbeddingError, EmbeddingOptions, EmbeddingService, VERSION


class MemoryFile:
    """供指纹检查读取的内存部件，不在磁盘生成权重文件。"""

    def __init__(self):
        """保存可控修改时间，用于测试部署变更检测。"""

        self.modified = 1

    def relative_to(self, parent):
        """模拟标准模型文件的相对名称。"""

        return Path("config.json")

    def open(self, mode):
        """每次返回新的固定内存流，不执行模型读取或文件解析。"""

        return BytesIO(b"model-metadata-stub")

    def stat(self):
        """提供固定大小与可控时间，不访问本机文件。"""

        return SimpleNamespace(st_size=19, st_mtime_ns=self.modified)


class ModelStub:
    """确定性编码 Stub，绝不表示真实 BGE-M3 语义效果已验收。"""

    def __init__(self):
        """收集调用顺序与精度设置。"""

        self.calls = []
        self.half_calls = 0
        self.max_seq_length = None

    def tokenizer(self, batch, **kwargs):
        """按字符数模拟包含特殊 token 的长度，检查必须禁用截断。"""

        assert kwargs["truncation"] is False and kwargs["add_special_tokens"] is True
        return {"input_ids": [[0] * (len(text) + 2) for text in batch]}

    def encode(self, batch, **kwargs):
        """给不同文本返回可核对的向量，保留批次输入顺序，不添加 Query 指令。"""

        assert kwargs["prompt"] == "" and kwargs["normalize_embeddings"] is True
        assert kwargs["show_progress_bar"] is False
        self.calls.append(list(batch))
        return [[float(len(text)), 1.0] + [0.0] * (DIMENSION - 2) for text in batch]

    def get_sentence_embedding_dimension(self):
        """声明 Stub 维度以验证生产加载器对维度的保护。"""

        return DIMENSION

    def half(self):
        """收集显式 GPU 半精度调用，不调用任何计算库。"""

        self.half_calls += 1

    def eval(self):
        """模拟设置推理状态，不加载真实权重。"""

        return self


@pytest.fixture
def stub_service(monkeypatch, tmp_path):
    """用内存部件替换模型构造，只允许惰性服务在用户执行用例时调用 Stub。"""

    file, model, loads = MemoryFile(), ModelStub(), []

    def files(path):
        """只返回内存元数据，不生成模型或文档文件。"""

        return [file]

    def loader(options):
        """记录首次加载次数，返回同一内存模型。"""

        loads.append(options)
        return model

    monkeypatch.setattr(module, "model_files", files)
    monkeypatch.setattr(module, "load_local_model", loader)
    options = EmbeddingOptions(str(tmp_path / "unused-model"), batch_size=2, max_length=32)
    return EmbeddingService(options), model, file, loads


def test_lazy_loading_and_empty_documents(stub_service):
    """构造和空文档列表不加载模型，不创建目录或联网。"""

    service, _, _, loads = stub_service
    assert loads == []
    assert service.embed_documents([]) == []
    assert loads == []


def test_real_token_preflight_includes_special_tokens_without_encoding(stub_service):
    """预检测完整长度（含特殊Token），允许返回超限计数但绝不调用模型编码。"""
    service, model, _, loads = stub_service
    assert service.token_counts([]) == [] and loads == []
    assert service.token_counts(["甲" * 30, "乙" * 31]) == [32, 33]
    assert model.calls == [] and len(loads) == 1


def test_batch_order_dimension_normalization_and_query_consistency(stub_service):
    """批次为 2/2/1，文档和 Query 使用相同模型/维度/归一化，顺序不乱。"""

    service, model, _, loads = stub_service
    texts = ["甲", "中文", "第三段", "第四内容", "第五个文本"]
    vectors = service.embed_documents(texts)
    assert model.calls == [texts[:2], texts[2:4], texts[4:]]
    assert len(vectors) == len(texts) and len(loads) == 1
    for text, vector in zip(texts, vectors):
        assert len(vector) == DIMENSION
        assert isclose(sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-12)
        assert isclose(vector[0], len(text) / sqrt(len(text) ** 2 + 1))
    assert service.embed_query(texts[1]) == vectors[1]
    assert model.calls[-1] == [texts[1]] and len(loads) == 1
    identity = service.identity()
    assert identity["dimension"] == 1024 and identity["version"] == VERSION
    assert len(identity["model_fingerprint"]) == 64


@pytest.mark.parametrize("texts", ["正文", [""], [" \n"], [None], [1]])
def test_invalid_inputs_are_rejected_before_model_load(stub_service, texts):
    """非法输入不读取模型，不把原始值写到错误信息。"""

    service, _, _, loads = stub_service
    with pytest.raises(EmbeddingError, match="EMBEDDING_INPUT"):
        service.embed_documents(texts)
    assert loads == []


@pytest.mark.parametrize("texts,max_items,max_chars,max_total,expected", [
    (["正常", "SECRET_BODY"], 10, 8, 100,
     {"step": "TEXT_CHAR_LIMIT", "item_index": 2, "item_count": 11, "limit_count": 8}),
    (["甲", "乙", "丙"], 2, 8, 100,
     {"step": "ITEM_COUNT_LIMIT", "item_count": 3, "limit_count": 2}),
    (["abcde", "abcdef"], 10, 8, 10,
     {"step": "TOTAL_CHAR_LIMIT", "item_count": 11, "limit_count": 10}),
])
def test_resource_limits_report_exact_safe_counts_before_model_load(stub_service, monkeypatch, caplog,
                                                                  texts, max_items, max_chars, max_total, expected):
    """分别定位单片、总批字符及数量门槛，日志/异常无正文，资源失败不加载模型。"""
    service, _, _, loads = stub_service
    monkeypatch.setattr(module, "MAX_ITEMS", max_items)
    monkeypatch.setattr(module, "MAX_TEXT_CHARS", max_chars)
    monkeypatch.setattr(module, "MAX_TOTAL_CHARS", max_total)
    with pytest.raises(EmbeddingError) as failure:
        service.embed_documents(texts)
    assert failure.value.code == "EMBEDDING_LIMIT" and failure.value.diagnostic == expected
    assert loads == [] and "SECRET" not in str(failure.value) and "SECRET" not in caplog.text


def test_true_token_limit_refuses_truncation(stub_service):
    """超限仍拒绝截断，并提供安全的输入序号及含特殊token的实际长度。"""

    service, model, _, _ = stub_service
    with pytest.raises(EmbeddingError, match="EMBEDDING_TOO_LONG") as error:
        service.embed_documents(["长" * 31])
    assert error.value.diagnostic == {"item_index": 1, "token_count": 33, "limit_count": 32}
    assert model.calls == []
    with pytest.raises(EmbeddingError, match="EMBEDDING_TOO_LONG"):
        service.embed_query("长" * 31)


def test_token_limit_diagnostic_uses_order_across_service_batches(stub_service):
    """跨内部批次的超限序号对应原输入，不因批次切换回到1，也不返回部分向量。"""
    service, model, _, _ = stub_service
    with pytest.raises(EmbeddingError) as error:
        service.embed_documents(["甲", "乙", "长" * 31])
    assert error.value.code == "EMBEDDING_TOO_LONG"
    assert error.value.diagnostic["item_index"] == 3 and model.calls == [["甲", "乙"]]


def test_row_chunks_can_reach_embedding_without_truncating_the_source(stub_service):
    """带表题/注释的长表经新切片逐片编码；Stub核对来源及长度，不代表真实GPU验收。"""
    from dataclasses import replace

    from test_chunk_splitter import assert_evidence, block, chunks
    from test_chunk_table_rows import html_table

    service, model, _, _ = stub_service
    service.options = replace(service.options, max_length=8192)
    source = block(1, "原始表题\n" + html_table(rows=300) + "\n原始注释" * 50, "table")
    with pytest.raises(EmbeddingError, match="EMBEDDING_TOO_LONG"):
        service.embed_documents([source.text])
    assert model.calls == []
    drafts = chunks([source])
    vectors = service.embed_documents([draft.text for draft in drafts])
    assert len(vectors) == len(drafts) > 1
    assert [text for batch in model.calls for text in batch] == [draft.text for draft in drafts]
    assert_evidence(drafts, [source])


@pytest.mark.parametrize("value", ["SECRET_BODY", True, -1, 0, 1.5, 2**63])
def test_length_error_diagnostic_rejects_untrusted_counts(value):
    """新增诊断只能携带受限正整数，不能借错误对象泄漏原文或路径。"""
    error = EmbeddingError("EMBEDDING_TOO_LONG", item_index=value, token_count=value, limit_count=value)
    assert error.diagnostic == {} and "SECRET" not in str(error)


@pytest.mark.parametrize("vectors", [[], [[1.0] * 3], [[0.0] * DIMENSION],
    [[float("nan")] + [1.0] * (DIMENSION - 1)], [[float("inf")] + [1.0] * (DIMENSION - 1)],
    [[True] + [1.0] * (DIMENSION - 1)], [["secret"] + [1.0] * (DIMENSION - 1)]])
def test_invalid_output_is_safe(vectors):
    """数量、维度、零范数、非有限值及非数字不得进入结果文件。"""

    with pytest.raises(EmbeddingError, match="EMBEDDING_OUTPUT") as error:
        module.validate_vectors(vectors, 1)
    assert "secret" not in str(error.value)


def test_inference_error_does_not_expose_raw_message(stub_service, monkeypatch):
    """模型原始异常包含私有正文/路径时，只返回固定安全错误。"""

    service, model, _, _ = stub_service

    def failed(*args, **kwargs):
        """模拟 GPU 推理失败，原始异常不能进入前端或任务结果。"""

        raise RuntimeError("SECRET_BODY C:/private/model")

    monkeypatch.setattr(model, "encode", failed)
    with pytest.raises(EmbeddingError, match="EMBEDDING_INFERENCE") as error:
        service.embed_query("中文问题")
    assert "SECRET" not in str(error.value) and "private" not in str(error.value)


def test_model_changes_in_running_process_are_rejected(stub_service):
    """运行中改权重/Tokenizer 元数据不能继续声称使用同一模型指纹。"""

    service, _, file, _ = stub_service
    service.embed_query("查询")
    file.modified += 1
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL_CHANGED"):
        service.embed_query("查询")


def test_loader_is_explicitly_offline_and_gpu_does_not_fallback(tmp_path, monkeypatch):
    """构造器强制本地文件/禁止远程代码，CUDA 不可用时不偷偷改用 CPU。"""

    model, calls = ModelStub(), []

    def factory(path, **kwargs):
        """代替已有第三方依赖构造器，检查本地加载参数，不读任何真实权重。"""

        assert kwargs == {"device": "cuda:0", "local_files_only": True, "trust_remote_code": False}
        calls.append(path)
        return model

    def available():
        """模拟当前 CUDA 设备可用，不访问驱动。"""

        return True

    def unavailable():
        """模拟 CUDA 设备不可用，不回退真实 CPU 推理。"""

        return False

    def one_device():
        """声明 Stub 中只有一张可选 GPU。"""

        return 1

    torch_stub = SimpleNamespace(cuda=SimpleNamespace(is_available=available, device_count=one_device))
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    options = EmbeddingOptions(str(tmp_path / "unused"))
    assert module.load_local_model(options) is model and model.half_calls == 1
    torch_stub.cuda = SimpleNamespace(is_available=unavailable, device_count=one_device)
    with pytest.raises(EmbeddingError, match="EMBEDDING_GPU_UNAVAILABLE"):
        module.load_local_model(options)
    assert len(calls) == 1


def test_missing_local_directory_does_not_download(tmp_path):
    """本地模型缺失时在导入模型依赖前失败，不用仓库 ID 自动拉取模型。"""

    service = EmbeddingService(EmbeddingOptions(str(tmp_path / "missing")))
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL_MISSING"):
        service.embed_query("问题")


def test_config_dimension_cannot_be_changed(tmp_path):
    """配置只允许已批准设备与长度，不提供可破坏数据库维度的任意维度设置。"""

    assert Settings(_env_file=None, embedding_batch_size=2).embedding_batch_size == 2
    with pytest.raises(ValueError):
        Settings(_env_file=None, embedding_device="cuda:0,cuda:1")
    with pytest.raises(ValueError):
        Settings(_env_file=None, embedding_max_length=8193)
    with pytest.raises(EmbeddingError, match="EMBEDDING_CONFIG"):
        EmbeddingOptions(str(tmp_path), batch_size=0).validate()


def test_dense_dimension_matches_existing_pgvector_column():
    """服务输出维度与已有 Chunk 向量列一致，只检查 ORM 类型，不访问数据库。"""

    from app.models import DocumentChunk

    assert DocumentChunk.__table__.c.embedding.type.dim == DIMENSION == 1024
