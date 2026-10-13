"""用户执行的 LlamaIndex Dense Retriever Stub 用例；无真实模型、数据库、文件或云调用。"""

import asyncio
import copy
import io
import logging

import pytest

pytest.importorskip("llama_index.core.schema", reason="需用户准备已有 llama-index-core；跳过不代表验收通过")

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import QueryBundle
from sqlalchemy.exc import SQLAlchemyError

from app.core import business_logging as logs
from app.services.rag.dense import DenseRetrievalError
from app.services.rag.llama_index import embedding_adapter
from app.services.rag.llama_index import dense_retriever as retrievers
from app.services.rag.llama_index.dense_retriever import DenseRetriever
from app.services.rag.llama_index.embedding_adapter import DKFEmbeddingAdapter
from app.services.rag.llama_index.node_adapter import node_to_result
from test_dense_sql import SqlSessionStub, model_identity, stored_pair


class QueryServiceStub:
    """只提供 Query 编码与实际身份接口，不创建模型；文档编码永远视为错误。"""

    def __init__(self):
        """记录内存调用次数，不生成验收文档或连接基础服务。"""
        self.queries, self.identity_calls = [], 0
        self.model = model_identity()
        self.change_on_second_identity = False
        self.failure = None

    def identity(self):
        """模拟同一现有服务实际文件身份；可注入编码期间模型变化。"""
        self.identity_calls += 1
        identity = copy.deepcopy(self.model)
        if self.change_on_second_identity and self.identity_calls > 1:
            identity["model_fingerprint"] = "c" * 64
        return identity

    def embed_query(self, query):
        """返回已归一化数值 Stub，不执行真实 BGE-M3 或添加 Query 前缀。"""
        self.queries.append(query)
        if self.failure:
            raise self.failure
        return [1.0] + [0.0] * 1023

    def embed_documents(self, texts):
        """防止 Retriever 重新生成文档向量或进入第二套 Ingestion。"""
        pytest.fail("Dense 不得重新编码文档")


class ReadonlySessionStub(SqlSessionStub):
    """模拟独立短会话的只读设置与有界 SELECT，不连接实际 PostgreSQL。"""

    def __init__(self, rows, *, failure=None):
        """保存纯结果行及可注入的安全数据库错误，不执行真实 SQL。"""
        super().__init__(rows)
        self.settings, self.closed, self.failure = [], False, failure

    def __enter__(self):
        """模拟短会话开启，不加载文件或创建事务。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """模拟自动释放会话，不提交、不修改 Document，也不打印异常原文。"""
        self.closed = True

    def in_transaction(self):
        """会话工厂须返回无既有事务的独立 Session。"""
        return False

    def execute(self, statement):
        """记录事务只读/超时设置；只有 SELECT 返回结果，可注入数据库异常。"""
        if not statement.is_select:
            self.settings.append(str(statement))
            return self
        if self.failure:
            raise self.failure
        return super().execute(statement)


def factory_for(rows, *, failure=None):
    """创建可观察的短会话工厂，每个查询独立实例，不共享线程绑定 Session。"""
    sessions = []
    def factory():
        """用户运行测试时创建纯内存会话，无连接或写库副作用。"""
        session = ReadonlySessionStub(rows, failure=failure)
        sessions.append(session)
        return session
    return factory, sessions


def test_constructor_is_lazy_and_default_service_is_shared(monkeypatch):
    """构造不取模型或会话，Query 只委托同一个项目服务，不创建另一套模型。"""
    chunk, document = stored_pair()
    service, requests = QueryServiceStub(), []
    def shared():
        """模拟已存在的进程共享 getter，不建立第二个 EmbeddingService。"""
        requests.append(True)
        return service
    monkeypatch.setattr(embedding_adapter, "get_embedding_service", shared)
    factory, sessions = factory_for([(chunk, document, 0.0)])
    retriever = DenseRetriever(chunk.kb_id, session_factory=factory)
    assert isinstance(retriever, BaseRetriever) and retriever.top_k == 20
    assert requests == [] and sessions == [] and service.queries == []
    nodes = retriever.retrieve("  PRIVATE_QUERY\n原始问题  ")
    assert len(nodes) == 1 and service.queries == ["  PRIVATE_QUERY\n原始问题  "]
    assert len(requests) == 3 and service.identity_calls == 2
    assert sessions[0].closed and len(sessions[0].statements) == 1
    assert sessions[0].settings == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '10s'",
                                    "SET LOCAL lock_timeout = '2s'"]


def test_top_k_nodes_are_stable_and_metadata_roundtrip_is_complete():
    """Top-K 与重复查询顺序/ID 稳定，页码、正文、Sheet/章节、source_spans 与模型身份不丢失。"""
    first = stored_pair(chunk_number=1)
    second = stored_pair(first[0].kb_id, chunk_number=2)
    factory, sessions = factory_for([(*first, 0.25), (*second, 1.25)])
    service = QueryServiceStub()
    retriever = DenseRetriever(first[0].kb_id, top_k=2, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    before = copy.deepcopy(first[0].chunk_metadata)
    nodes = retriever.retrieve(QueryBundle(query_str="PRIVATE_QUERY"))
    again = retriever.retrieve("PRIVATE_QUERY")
    assert [node.model_dump() for node in nodes] == [node.model_dump() for node in again]
    assert [node.score for node in nodes] == [0.75, -0.25]
    assert all(node.node.embedding is None for node in nodes)
    result = node_to_result(nodes[0])
    assert (result.chunk_id, result.doc_id, result.kb_id) == (first[0].chunk_id, first[0].doc_id, first[0].kb_id)
    assert (result.file_name, result.page_start, result.page_end, result.section) == (first[1].file_name, 3, 4, "原章节")
    assert result.text == first[0].text and result.metadata == before
    assert result.embedding_identity == model_identity()
    nodes[0].node.metadata["chunk_metadata"]["source_spans"][0]["page"] = 99
    assert first[0].chunk_metadata == before
    assert len(sessions) == 2 and all(session.closed for session in sessions)
    assert service.queries == ["PRIVATE_QUERY", "PRIVATE_QUERY"]


@pytest.mark.parametrize("multiple", [False, True])
def test_document_scope_is_immutable_and_nodes_keep_original_evidence(multiple):
    """SQL文档范围冻结后不受原列表修改影响；单/多文档 Node/DTO 保留原 ID、页与来源。"""
    first = stored_pair(chunk_number=1)
    second = stored_pair(first[0].kb_id, chunk_number=2)
    pairs = [first, second] if multiple else [first]
    incoming = [str(pair[1].doc_id) for pair in pairs]
    expected = tuple(sorted(pair[1].doc_id for pair in pairs))
    service, (factory, sessions) = QueryServiceStub(), factory_for([(*pair, 0.1) for pair in pairs])
    retriever = DenseRetriever(first[0].kb_id, doc_ids=incoming,
        embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    incoming.clear()
    assert retriever.doc_ids == expected and service.queries == [] and sessions == []
    nodes = retriever.retrieve("PRIVATE_QUERY")
    results = retriever.retrieve_results("PRIVATE_QUERY")
    assert [result.doc_id for result in results] == [pair[1].doc_id for pair in pairs]
    for node, (chunk, document) in zip(nodes, pairs):
        assert node.node.node_id == str(chunk.chunk_id) and node.node.ref_doc_id == str(document.doc_id)
        assert node.node.metadata["doc_id"] == str(document.doc_id)
        assert node.node.metadata["chunk_id"] == str(chunk.chunk_id)
        assert (node.node.metadata["page_start"], node.node.metadata["page_end"]) == (3, 4)
        assert node.node.metadata["chunk_metadata"]["source_spans"][0]["page"] == 3
        assert node.node.metadata["chunk_metadata"] == chunk.chunk_metadata
    for session in sessions:
        compiled = session.statements[0].compile()
        assert any(value == list(expected) for value in compiled.params.values() if isinstance(value, list))
        assert session.closed
    assert service.queries == ["PRIVATE_QUERY", "PRIVATE_QUERY"] and service.identity_calls == 4


@pytest.mark.parametrize("doc_ids", [[], (), "PRIVATE_DOC", ["PRIVATE_DOC"], [None], [True]])
def test_invalid_document_scope_never_loads_model_or_database(doc_ids):
    """构造拒绝非法文档范围，无 Query/模型身份读取、数据库查询或隐式全库退回。"""
    chunk, _ = stored_pair()
    service, (factory, sessions) = QueryServiceStub(), factory_for([])
    with pytest.raises(DenseRetrievalError, match="DENSE_INPUT") as error:
        DenseRetriever(chunk.kb_id, doc_ids=doc_ids, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    assert sessions == [] and service.queries == [] and service.identity_calls == 0
    assert "PRIVATE_DOC" not in str(error.value)


def test_scoped_empty_result_never_retries_full_kb():
    """合法UUID但SQL没有同库同模型READY证据时返回空集，只执行原限定范围的一次查询。"""
    chunk, document = stored_pair()
    service, (factory, sessions) = QueryServiceStub(), factory_for([])
    retriever = DenseRetriever(chunk.kb_id, doc_ids=[document.doc_id],
        embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    assert retriever.retrieve_results("PRIVATE_QUERY") == []
    assert len(sessions) == 1 and len(sessions[0].statements) == 1
    assert "document_chunks.doc_id IN" in str(sessions[0].statements[0])
    assert service.queries == ["PRIVATE_QUERY"]


def test_other_document_evidence_is_rejected_and_scope_logs_are_safe(monkeypatch):
    """模拟同库越界证据，全批拒绝；既有日志仅保存目标UUID与数量，不记录 Query 或ID数组。"""
    target = stored_pair(chunk_number=1)
    other = stored_pair(target[0].kb_id, chunk_number=2)
    service, (factory, _) = QueryServiceStub(), factory_for([(*other, 0.1)])
    events = []

    def log(event, **fields):
        """捕获本模块安全摘要，不配置或写入真实日志。"""
        events.append((event, fields))

    monkeypatch.setattr(retrievers, "log_event", log)
    retriever = DenseRetriever(target[0].kb_id, doc_ids=[target[1].doc_id],
        embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    with pytest.raises(DenseRetrievalError, match="DENSE_RESULT"):
        retriever.retrieve("PRIVATE_QUERY")
    assert [event for event, fields in events] == ["dense.started", "dense.failed"]
    assert all(fields["doc_id"] == target[1].doc_id and fields["source_document_count"] == 1 for event, fields in events)
    assert "PRIVATE_QUERY" not in repr(events) and all("doc_ids" not in fields for event, fields in events)


def test_model_change_stops_before_sql_and_never_falls_back():
    """身份前后改变时不执行数据库查询、不使用缓存外部向量或重编码文档。"""
    chunk, _ = stored_pair()
    service = QueryServiceStub()
    service.change_on_second_identity = True
    factory, sessions = factory_for([])
    retriever = DenseRetriever(chunk.kb_id, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    with pytest.raises(DenseRetrievalError, match="DENSE_MODEL_CHANGED"):
        retriever.retrieve("PRIVATE_QUERY")
    assert sessions == [] and service.queries == ["PRIVATE_QUERY"]


@pytest.mark.parametrize("bundle", [QueryBundle(query_str="PRIVATE_QUERY", embedding=[1.0] * 1024),
    QueryBundle(query_str="PRIVATE_QUERY", custom_embedding_strs=["OTHER_QUERY"]),
    QueryBundle(query_str="PRIVATE_QUERY", image_path="PRIVATE_PATH"), " ", None, 123])
def test_untrusted_query_inputs_never_load_model_or_query_database(bundle):
    """不接受缺模型身份的预计算 Query 向量、变换文本、图像路径或异常输入。"""
    chunk, _ = stored_pair()
    service, (factory, sessions) = QueryServiceStub(), factory_for([])
    retriever = DenseRetriever(chunk.kb_id, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    with pytest.raises(DenseRetrievalError, match="DENSE_INPUT") as error:
        retriever.retrieve(bundle)
    assert sessions == [] and service.queries == [] and service.identity_calls == 0
    assert "PRIVATE_QUERY" not in str(error.value) and "PRIVATE_PATH" not in str(error.value)


def test_empty_query_results_do_not_retry_or_enlarge_scope():
    """模型不匹配或没有 READY 数据时保持空集，不盲目重试、扩大知识库或补任务。"""
    chunk, _ = stored_pair()
    service, (factory, sessions) = QueryServiceStub(), factory_for([])
    retriever = DenseRetriever(chunk.kb_id, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    assert retriever.retrieve_results("PRIVATE_QUERY") == []
    assert len(sessions) == 1 and len(sessions[0].statements) == 1 and len(service.queries) == 1


def test_async_uses_separate_session_and_same_service():
    """异步入口保留同一文档范围/共享服务，在执行线程建立独立只读会话。"""
    chunk, document = stored_pair()
    service, (factory, sessions) = QueryServiceStub(), factory_for([(chunk, document, 1.0)])
    retriever = DenseRetriever(chunk.kb_id, doc_ids=[document.doc_id],
        embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    nodes = asyncio.run(retriever.aretrieve("PRIVATE_QUERY"))
    assert len(nodes) == 1 and nodes[0].score == 0.0
    assert len(sessions) == 1 and sessions[0].closed and len(service.queries) == 1
    assert "document_chunks.doc_id IN" in str(sessions[0].statements[0])


def test_framework_public_callback_payload_is_bypassed(monkeypatch):
    """不进入基类携带 Query/Node payload 的公共路径或递归 Query Engine。"""
    def forbidden(*args, **kwargs):
        """任何框架基类公共调用都代表违反日志边界，立即断言失败。"""
        pytest.fail("不得调用含原文 payload 的基类入口")
    monkeypatch.setattr(BaseRetriever, "retrieve", forbidden)
    chunk, document = stored_pair()
    service, (factory, _) = QueryServiceStub(), factory_for([(chunk, document, 0.0)])
    retriever = DenseRetriever(chunk.kb_id, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    assert len(retriever.retrieve("PRIVATE_QUERY")) == 1
    assert retriever.callback_manager.handlers == []


@pytest.mark.parametrize("failure_stage", ["database", "embedding", "node"])
def test_errors_and_business_logs_are_sanitized_without_partial_success(failure_stage):
    """失败只记录固定摘要，不出现 Query/正文/向量/SQL/路径，也不记录成功结束事件。"""
    chunk, document = stored_pair()
    service = QueryServiceStub()
    failure = SQLAlchemyError("PRIVATE_SQL PASSWORD PRIVATE_PATH") if failure_stage == "database" else None
    if failure_stage == "embedding":
        service.failure = RuntimeError("PRIVATE_MODEL_PATH")
    elif failure_stage == "node":
        chunk.text = " "
    factory, _ = factory_for([(chunk, document, 0.0)], failure=failure)
    retriever = DenseRetriever(chunk.kb_id, embedding_adapter=DKFEmbeddingAdapter(service), session_factory=factory)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter())
    logger = logs.LOGGER
    before = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
    token = logs.CONTEXT.set(None)
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(DenseRetrievalError) as error:
            retriever.retrieve("PRIVATE_QUERY")
        combined = str(error.value) + stream.getvalue()
        for secret in ("PRIVATE_QUERY", "PRIVATE_SQL", "PASSWORD", "PRIVATE_PATH", "PRIVATE_MODEL_PATH", "既有证据原文"):
            assert secret not in combined
        assert "event=dense.failed" in stream.getvalue()
        assert "event=dense.finished" not in stream.getvalue()
        assert "Dense 检索安全失败" in stream.getvalue()
    finally:
        handler.close()
        logger.handlers, logger.level, logger.propagate, logger.disabled = before
        logs.CONTEXT.reset(token)
