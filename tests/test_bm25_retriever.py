"""用户执行的BM25框架/快照缓存/证据用例；无数据库、原文件、Embedding或云请求。"""

import asyncio
import copy
import io
import logging

import pytest

pytest.importorskip('llama_index.core.schema', reason='需已有core/NLP缓存；跳过不代表验收通过')

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import QueryBundle

from app.core import business_logging as logs
from app.services.rag import bm25 as core
from app.services.rag.llama_index import embedding_adapter, node_adapter
from app.services.rag.llama_index.bm25_retriever import BM25Retriever
from test_dense_retriever import factory_for
from test_dense_sql import stored_pair


def lexical_pair(kb_id=None, number=1, text='NASA RX-7800-XT 100'):
    """已有ORM字段的纯内存Stub，不保存/解析文档，不冒充真实公开基准素材。"""
    chunk, document = stored_pair(kb_id, number)
    chunk.text = text
    return chunk, document


def test_constructor_is_lazy_and_retrieval_never_calls_embedding(monkeypatch):
    """构造不读库/建索引；BM25只用READY Chunk，即使Query模型不可用也不调用Embedding。"""
    pair = lexical_pair()
    factory, sessions = factory_for([pair])

    def forbidden(*args, **kwargs):
        """模型getter一旦调用即失败，不实际生成向量或运行GPU。"""
        pytest.fail('BM25不得调用Embedding')

    monkeypatch.setattr(embedding_adapter, 'get_embedding_service', forbidden)
    retriever = BM25Retriever(pair[0].kb_id, doc_ids=[str(pair[1].doc_id)], session_factory=factory)
    assert isinstance(retriever, BaseRetriever) and retriever.top_k == 20
    assert sessions == [] and retriever._index is None
    nodes = retriever.retrieve(QueryBundle(query_str='NASA'))
    assert len(nodes) == 1 and nodes[0].node.embedding is None and nodes[0].score > 0
    assert sessions[0].closed and len(sessions[0].statements) == 1
    assert sessions[0].settings == ['SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout = '10s'",
                                     "SET LOCAL lock_timeout = '2s'"]


def test_nodes_and_dto_keep_complete_evidence_without_mutating_source():
    """统一NodeWithScore及DTO保留原ID/正文/页/章节/source_spans，不共享可变元数据。"""
    pair = lexical_pair()
    incoming = [str(pair[1].doc_id)]
    factory, sessions = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, doc_ids=incoming, session_factory=factory)
    incoming.clear()
    before = copy.deepcopy(pair[0].chunk_metadata)
    node = retriever.retrieve('RX-7800-XT')[0]
    assert node.node.node_id == str(pair[0].chunk_id) and node.node.ref_doc_id == str(pair[1].doc_id)
    assert node.node.metadata['doc_id'] == str(pair[1].doc_id)
    assert (node.node.metadata['page_start'], node.node.metadata['page_end']) == (3, 4)
    assert node.node.metadata['section'] == pair[0].section
    index = retriever._index
    node.node.text = 'PRIVATE_MUTATED_TEXT'
    node.node.metadata['chunk_metadata']['source_spans'][0]['page'] = 99
    result = retriever.retrieve_results('RX-7800-XT')[0]
    assert retriever._index is index and result.text == pair[0].text and result.metadata == before
    assert (result.chunk_id, result.doc_id, result.kb_id) == (pair[0].chunk_id, pair[1].doc_id, pair[0].kb_id)
    assert pair[0].chunk_metadata == before and len(sessions) == 2


def test_cache_refreshes_ready_membership_content_and_current_metadata():
    """每次查当前SQL快照；仅元数据变化复用词频，新正文/READY集合变化重建且不返回旧Node。"""
    pair = lexical_pair()
    rows = [pair]
    factory, _ = factory_for(rows)
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    retriever.retrieve('NASA')
    first_index = retriever._index
    pair[0].section = '新章节'
    pair[0].chunk_metadata['source_spans'][0]['page'] = 4
    refreshed = retriever.retrieve('NASA')[0]
    assert retriever._index is first_index and refreshed.node.metadata['section'] == '新章节'
    assert refreshed.node.metadata['chunk_metadata']['source_spans'][0]['page'] == 4
    pair[0].text = 'changed keyword ESA'
    assert retriever.retrieve('NASA') == []
    assert retriever._index is not first_index and len(retriever.retrieve('ESA')) == 1
    # 模拟SQL已排除非READY/已删除文档；不改真实数据库，也不能从缓存返回旧证据。
    rows.clear()
    assert retriever.retrieve('ESA') == [] and retriever._index.ids == ()


def test_source_failure_cannot_fall_back_to_existing_cache():
    """已有缓存也不能掩盖本次来源失败或非READY越界行，错误不返回半份证据。"""
    pair = lexical_pair()
    factory, _ = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    assert retriever.retrieve('NASA')
    pair[1].status = 'INDEXING'
    with pytest.raises(core.BM25RetrievalError, match='BM25_RESULT'):
        retriever.retrieve('NASA')


def test_multi_document_top_k_and_repeated_order_are_stable():
    """同分按Chunk UUID排序，文档范围在SQL绑定；没有新的文档、任务或框架存储。"""
    first = lexical_pair(number=1)
    second = lexical_pair(first[0].kb_id, number=2)
    factory, sessions = factory_for([first, second])
    retriever = BM25Retriever(first[0].kb_id, doc_ids=[first[1].doc_id, second[1].doc_id],
        top_k=1, session_factory=factory)
    one, again = retriever.retrieve('NASA'), retriever.retrieve('NASA')
    assert [node.node.node_id for node in one] == [str(first[0].chunk_id)]
    assert [node.model_dump() for node in one] == [node.model_dump() for node in again]
    assert all('document_chunks.doc_id IN' in str(s.statements[0]) for s in sessions)


def test_async_and_public_callback_boundaries_remain_safe(monkeypatch):
    """异步仍使用独立会话/固定范围；不调用含Query/Node payload的框架公共回调。"""
    def forbidden(*args, **kwargs):
        """框架公共retrieve被调用即失败，不产生追踪或递归检索。"""
        pytest.fail('BM25不得走含正文payload的基类入口')

    monkeypatch.setattr(BaseRetriever, 'retrieve', forbidden)
    pair = lexical_pair()
    factory, sessions = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, doc_ids=[pair[1].doc_id], session_factory=factory)
    nodes = asyncio.run(retriever.aretrieve('NASA'))
    assert len(nodes) == 1 and retriever.callback_manager.handlers == []
    assert len(sessions) == 1 and sessions[0].closed


@pytest.mark.parametrize('query', ['', None, 'PRIVATE_QUERY\x00', QueryBundle(query_str='PRIVATE_QUERY', embedding=[1.0])])
def test_invalid_query_is_rejected_before_sql(query):
    """无效Query/外部Embedding输入不读取来源、不建缓存或泄露原文。"""
    pair = lexical_pair()
    factory, sessions = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    with pytest.raises(core.BM25RetrievalError, match='BM25_INPUT') as error:
        retriever.retrieve(query)
    assert sessions == [] and retriever._index is None and 'PRIVATE_QUERY' not in str(error.value)


def test_query_term_limit_is_checked_before_loading_corpus():
    """长Query词项超限不先读取整个语料或构建缓存，不消耗模型/数据库资源。"""
    pair = lexical_pair()
    factory, sessions = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    query = ' '.join('unit' + str(n) for n in range(core.MAX_QUERY_TERMS + 1))
    with pytest.raises(core.BM25RetrievalError, match='BM25_LIMIT'):
        retriever.retrieve(query)
    assert sessions == [] and retriever._index is None


@pytest.mark.parametrize('ids', [[], (), 'PRIVATE_DOC', ['PRIVATE_DOC'], [None]])
def test_invalid_document_scope_is_rejected_before_sql(ids):
    """文档范围与Dense一致，空/非法范围不能悄悄变为全库检索。"""
    pair = lexical_pair()
    factory, sessions = factory_for([pair])
    with pytest.raises(core.BM25RetrievalError, match='BM25_INPUT'):
        BM25Retriever(pair[0].kb_id, doc_ids=ids, session_factory=factory)
    assert sessions == []


def test_logs_are_aggregated_sanitized_and_conversion_default_is_preserved(monkeypatch):
    """BM25语料转换不按每Chunk刷成功日志，原Adapter默认保持原行为，错误及摘要仍记录。"""
    pair = lexical_pair(text='PRIVATE_BODY NASA')
    factory, _ = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter())
    logger = logs.LOGGER
    before = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
    token = logs.CONTEXT.set(None)
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.INFO)
    try:
        retriever.retrieve('NASA PRIVATE_QUERY')
        retriever.retrieve('NASA')
        output = stream.getvalue()
        assert 'event=bm25.index_built' in output and 'event=bm25.index_reused' in output
        assert 'event=rag.node_converted' not in output
        assert 'PRIVATE_QUERY' not in output and 'PRIVATE_BODY' not in output
        assert 'BM25' in output
        node_adapter.chunk_to_node(*pair, include_embedding=False)
        assert 'event=rag.node_converted' in stream.getvalue()
        with pytest.raises(node_adapter.RagAdapterError, match='RAG_ADAPTER_INPUT'):
            node_adapter.chunk_to_node(*pair, include_embedding=False, log_success='PRIVATE_FLAG')
        assert 'PRIVATE_FLAG' not in stream.getvalue() and 'event=rag.adapter_failed' in stream.getvalue()
    finally:
        handler.close()
        logger.handlers, logger.level, logger.propagate, logger.disabled = before
        logs.CONTEXT.reset(token)
