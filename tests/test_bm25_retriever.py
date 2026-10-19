"""用户执行的BM25框架/快照缓存/证据用例；无数据库、原文件、Embedding或云请求。"""

import asyncio
import copy
import io
import logging
import weakref

import pytest

pytest.importorskip('llama_index.core.schema', reason='需已有core/NLP缓存；跳过不代表验收通过')

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import QueryBundle

from app.core import business_logging as logs
from app.services.rag import bm25 as core
from app.services.rag.llama_index import bm25_retriever as retrievers, embedding_adapter, node_adapter
from app.services.rag.llama_index.bm25_retriever import BM25Retriever
from bm25_stubs import factory_for
from test_dense_sql import stored_pair


def lexical_pair(kb_id=None, number=1, text='NASA RX-7800-XT 100'):
    """已有ORM字段的纯内存Stub，不保存/解析文档，不冒充真实公开基准素材。"""
    chunk, document = stored_pair(kb_id, number)
    chunk.text = text
    return chunk, document


def test_constructor_is_lazy_and_retrieval_never_calls_embedding(monkeypatch):
    """构造不读库；冷启动同快照读取摘要、完整验证及命中证据，始终不调用Embedding。"""
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
    assert sessions[0].closed and len(sessions[0].statements) == 3
    assert sessions[0].settings == ['SET TRANSACTION READ ONLY',
        'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ', "SET LOCAL statement_timeout = '10s'",
        "SET LOCAL lock_timeout = '2s'"]
    assert all(result.closed for result in sessions[0].results)


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


def test_existing_bm25_instance_includes_incremental_ready_document_without_business_rebuild():
    """新READY文档进入当前快照时仅重建本地派生词频；已有Chunk及业务任务代次不变。"""
    first = lexical_pair(number=1)
    second = lexical_pair(first[0].kb_id, number=2)
    rows = [first]
    factory, _ = factory_for(rows)
    retriever = BM25Retriever(first[0].kb_id, session_factory=factory)
    before = copy.deepcopy(first[0].chunk_metadata), first[1].processing_task_id, first[1].index_task_id
    assert len(retriever.retrieve('NASA')) == 1
    old_index = retriever._index
    rows.append(second)
    nodes = retriever.retrieve('NASA')
    assert {node.node.metadata['doc_id'] for node in nodes} == {str(first[1].doc_id), str(second[1].doc_id)}
    assert retriever._index is not old_index
    assert (first[0].chunk_metadata, first[1].processing_task_id, first[1].index_task_id) == before


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


def test_full_corpus_nodes_are_not_retained_and_evidence_select_is_top_k(monkeypatch):
    """冷启动逐行完整验证后释放，第三次SQL只绑定Top-K，摘要扫描不构造Node。"""
    first = lexical_pair(number=1)
    rows = [first] + [lexical_pair(first[0].kb_id, number=number) for number in range(2, 10)]
    factory, sessions = factory_for(rows)
    refs, alive_counts = [], []
    convert = retrievers.chunk_to_node

    def tracked(*args, **kwargs):
        """用弱引用观察Node存活，不以易抖动的进程RSS阈值替代保留对象检查。"""
        node = convert(*args, **kwargs)
        refs.append(weakref.ref(node))
        alive_counts.append(sum(ref() is not None for ref in refs))
        return node

    monkeypatch.setattr(retrievers, 'chunk_to_node', tracked)
    retriever = BM25Retriever(first[0].kb_id, top_k=2, session_factory=factory)
    hits = retriever.retrieve('NASA')
    assert [hit.node.node_id for hit in hits] == [str(row[0].chunk_id) for row in rows[:2]]
    assert max(alive_counts) <= 2 and sum(ref() is not None for ref in refs) == 2
    statements = sessions[0].statements
    assert all(statement.get_execution_options()['yield_per'] == core.SOURCE_BATCH_SIZE for statement in statements)
    assert 'document_chunks.chunk_id IN' not in str(statements[0])
    assert 'document_chunks.chunk_id IN' not in str(statements[1])
    assert 'document_chunks.chunk_id IN' in str(statements[2])
    assert [row[0].chunk_id for row in rows[:2]] in statements[2].compile().params.values()
    assert all(result.closed for result in sessions[0].results)


def test_nonmatching_corrupt_evidence_still_fails_entire_query():
    """未命中证据变化使摘要失效并重做完整校验，已有词频不能掩盖坏页码。"""
    first = lexical_pair(number=1)
    second = lexical_pair(first[0].kb_id, number=2, text='ESA')
    rows = [first, second]
    factory, sessions = factory_for(rows)
    retriever = BM25Retriever(first[0].kb_id, session_factory=factory)
    assert retriever.retrieve('NASA')
    second[0].page_end = 0
    with pytest.raises(core.BM25RetrievalError, match='BM25_RESULT'):
        retriever.retrieve('NASA')
    assert len(sessions[-1].statements) == 2 and sessions[-1].closed
    assert all(result.closed for result in sessions[-1].results)


@pytest.mark.parametrize('damage', ['missing', 'text'])
def test_missing_or_changed_winner_evidence_cannot_return_partial_result(monkeypatch, damage):
    """命中取回异常不能缩减Top-K或混用新正文，即使快照Stub违规也明确整批失败。"""
    pair = lexical_pair()
    factory, sessions = factory_for([pair])
    select = retrievers.select_sources

    def damaged(*args, **kwargs):
        """只损坏命中证据阶段，不改变首次全库评分输入。"""
        rows = select(*args, **kwargs)
        if damage == 'missing':
            return []
        rows[0][0].text = 'PRIVATE_CHANGED_TEXT'
        return rows

    monkeypatch.setattr(retrievers, 'select_sources', damaged)
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    with pytest.raises(core.BM25RetrievalError, match='BM25_RESULT') as error:
        retriever.retrieve('NASA')
    assert 'PRIVATE_CHANGED_TEXT' not in str(error.value) and sessions[0].closed
    assert all(result.closed for result in sessions[0].results)


def test_rebuilding_releases_stale_index_before_allocating_replacement(monkeypatch):
    """内容变化先清理失效词频，建索引失败不会保留或返回旧评分结果。"""
    pair = lexical_pair()
    factory, _ = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    assert retriever.retrieve('NASA')
    previous = weakref.ref(retriever._index)
    pair[0].text = 'ESA updated'

    def failed_rebuild(*args, **kwargs):
        """检查旧对象已释放，再模拟新索引构建失败。"""
        assert retriever._index is None and previous() is None
        raise core.BM25RetrievalError('BM25_INDEX')

    monkeypatch.setattr(retrievers, 'LocalBM25Index', failed_rebuild)
    with pytest.raises(core.BM25RetrievalError, match='BM25_INDEX'):
        retriever.retrieve('ESA')
    assert retriever._index is None and retriever._index_scope is None


def test_unchanged_full_snapshot_skips_repeated_node_validation(monkeypatch):
    """热查询完整核对全部摘要，仅构造命中证据Node，不重复全库ORM与Adapter工作。"""
    first = lexical_pair(number=1)
    rows = [first, lexical_pair(first[0].kb_id, number=2, text='ESA')]
    factory, sessions = factory_for(rows)
    calls = []
    convert = retrievers.corpus_item

    def tracked(chunk, document):
        """只记录完整语料校验次数，不省略生产证据校验。"""
        calls.append(chunk.chunk_id)
        return convert(chunk, document)

    monkeypatch.setattr(retrievers, 'corpus_item', tracked)
    retriever = BM25Retriever(first[0].kb_id, session_factory=factory)
    before = retriever.retrieve('NASA')
    index = retriever._index
    after = retriever.retrieve('NASA')
    assert len(calls) == len(rows) and retriever._index is index
    assert [node.model_dump() for node in before] == [node.model_dump() for node in after]
    assert len(sessions[0].statements) == 3 and len(sessions[1].statements) == 2
    assert 'source_hash' in sessions[1].statements[0].selected_columns.keys()
    assert 'document_chunks.chunk_id IN' in str(sessions[1].statements[1])
    assert set(node_adapter.CHUNK_FIELDS) <= set(core.SOURCE_CHUNK_FIELDS)
    assert set(node_adapter.DOCUMENT_FIELDS) <= set(core.SOURCE_DOCUMENT_FIELDS)


@pytest.mark.parametrize('change', ['text', 'source_spans', 'page', 'file_name', 'model'])
def test_nonwinning_source_changes_require_full_validation(monkeypatch, change):
    """未命中项的正文/来源/页/文件名/模型元数据变化均重新全量验证，不能凭数量或代次复用。"""
    first = lexical_pair(number=1)
    second = lexical_pair(first[0].kb_id, number=2, text='ESA')
    factory, sessions = factory_for([first, second])
    calls = []
    convert = retrievers.corpus_item

    def tracked(chunk, document):
        """记录每次完整校验，确认来源变化没有被Top-K未命中掩盖。"""
        calls.append(chunk.chunk_id)
        return convert(chunk, document)

    monkeypatch.setattr(retrievers, 'corpus_item', tracked)
    retriever = BM25Retriever(first[0].kb_id, top_k=1, session_factory=factory)
    retriever.retrieve('NASA')
    previous = retriever._index
    if change == 'text':
        second[0].text = 'ESA changed'
    elif change == 'source_spans':
        second[0].chunk_metadata['source_spans'][0]['page'] = 4
    elif change == 'page':
        second[0].page_end = 5
    elif change == 'file_name':
        second[1].file_name = '更新后的来源.pdf'
    else:
        second[1].index_config['result']['model']['model_fingerprint'] = 'c' * 64
    assert retriever.retrieve('NASA')[0].node.node_id == str(first[0].chunk_id)
    assert len(calls) == 4 and len(sessions[1].statements) == 3
    assert (retriever._index is previous) == (change != 'text')


def test_source_snapshot_database_failure_never_returns_cached_results(monkeypatch):
    """热查询摘要SQL失败仍明确失败，携带真实SQLSTATE，不返回上一题的词频命中。"""
    from test_retrieval_database import driver_error

    pair = lexical_pair()
    factory, _ = factory_for([pair])
    retriever = BM25Retriever(pair[0].kb_id, session_factory=factory)
    assert retriever.retrieve('NASA')
    failing_factory, sessions = factory_for([pair], failure=driver_error('55P03'))
    retriever._session_factory = failing_factory
    events = []

    def log(event, **fields):
        """只捕获当前模块白名单摘要，不写真实日志文件。"""
        events.append((event, fields))

    monkeypatch.setattr(retrievers, 'log_event', log)
    with pytest.raises(core.BM25RetrievalError, match='BM25_DATABASE_LOCK_UNAVAILABLE') as caught:
        retriever.retrieve('NASA')
    assert caught.value.database_diagnostic == {'reason': 'LOCK_UNAVAILABLE', 'sqlstate': '55P03'}
    assert sessions[0].closed
    fields = next(fields for event, fields in events if event == 'bm25.failed')
    assert fields['step'] == '55P03' and fields['stage'] == 'source_snapshot'
    assert 'PRIVATE' not in repr(events) + str(caught.value)
