"""BM25 词项/评分及 SQL 来源的单元用例；用户运行，不生成文档、读取原件或连接数据库。"""

import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import visitors
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import CTE, Join

from app.models import Document, DocumentChunk
from app.services.rag import bm25 as tool
from bm25_stubs import StreamingResultStub, StreamingSessionStub as SqlSessionStub
from test_dense_sql import stored_pair


def test_tokenizer_preserves_numbers_models_and_acronyms():
    """数字不做子串匹配，大小写/全角归一化但原式百分比及完整型号保留。"""
    assert tool.tokenize('ＮＡＳＡ RX-7800-XT BGE-M3 1,234.50 12.5% -10 .5 802.11ax') == [
        'nasa', 'rx-7800-xt', 'bge-m3', '1234.50', '12.5%', '-10', '.5', '802.11ax']
    assert tool.tokenize('100 1000 100A') == ['100', '1000', '100a']
    assert tool.tokenize('深度求索') == ['深', '度', '求', '索', '深度', '度求', '求索']


@pytest.mark.parametrize('query', ['NASA', 'RX-7800-XT', 'BGE-M3', '100', '12.5%'])
def test_exact_english_model_numeric_keywords_retrieve_true_overlap(query):
    """内存词项用例只返回真实匹配Chunk，型号/数字不能靠相似子串凑命中。"""
    index = tool.LocalBM25Index([
        (UUID(int=1), 'NASAA RX-7900-XT BGE-M4 1000 12.50%'),
        (UUID(int=2), 'NASA RX-7800-XT BGE-M3 100 12.5%'),
        (UUID(int=3), 'other document 110 13.5%'),
    ])
    hits = index.search(query, 20)
    assert len(hits) == 1 and hits[0][0] == 1 and hits[0][1] > 0


def test_single_chunk_and_chinese_name_have_positive_scores():
    """BM25Plus在单Chunk及高频词上仍可检索；中文片段不依赖外部分词模型。"""
    single = tool.LocalBM25Index([(UUID(int=1), 'NASA NASA')])
    assert single.search('NASA', 20)[0][1] > 0
    chinese = tool.LocalBM25Index([(UUID(int=1), '深度求索推出模型'), (UUID(int=2), '天地玄黄宇宙洪荒')])
    assert [position for position, score in chinese.search('深度求索', 20)] == [0]


def test_no_keyword_overlap_is_empty_not_bm25plus_background_score():
    """部分查询词出现在其他Chunk时，也不允许背景delta分给零重叠Chunk。"""
    index = tool.LocalBM25Index([(UUID(int=1), 'NASA'), (UUID(int=2), 'unrelated text')])
    assert [position for position, score in index.search('NASA', 20)] == [0]
    assert index.search('uniqueabsentkeyword', 20) == []
    assert index.search('!!!', 20) == []
    assert tool.LocalBM25Index([]).search('NASA', 20) == []
    assert tool.LocalBM25Index([(UUID(int=1), '!!!')]).search('NASA', 20) == []


def test_top_k_score_and_chunk_id_ties_are_stable():
    """同文本同分时按UUID稳定排序，Top-K不扩大范围，重查分数/顺序一致。"""
    index = tool.LocalBM25Index([(UUID(int=1), 'NASA 100'), (UUID(int=2), 'NASA 100')])
    first = index.search('NASA', 1)
    assert first == index.search('NASA', 1)
    assert first[0][0] == 0 and len(first) == 1
    assert [position for position, score in index.search('NASA', 2)] == [0, 1]


def test_corpus_fingerprint_tracks_content_identity_and_tokenizer_version(monkeypatch):
    """同内容重建身份一致；Chunk/正文/分词版本变化使派生索引失效，不写磁盘。"""
    items = [(UUID(int=1), 'NASA 100')]
    key = tool.corpus_fingerprint(items)
    assert key == tool.corpus_fingerprint(copy.deepcopy(items))
    assert key != tool.corpus_fingerprint([(UUID(int=1), 'NASA 101')])
    assert key != tool.corpus_fingerprint([(UUID(int=2), 'NASA 100')])
    monkeypatch.setattr(tool, 'TOKENIZER_VERSION', 'unit-new-version')
    assert key != tool.corpus_fingerprint(items)


def test_limits_fail_without_silently_truncating_corpus(monkeypatch):
    """预算在中文批量扩展前及构建前检查，不把超限输入截断为虚假的Top-K。"""
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.tokenize('测试中文', max_terms=2)
    monkeypatch.setattr(tool, 'MAX_CORPUS_TOKENS', 2)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.LocalBM25Index([(UUID(int=1), 'one two three')])
    monkeypatch.setattr(tool, 'MAX_CORPUS_BYTES', 4)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.corpus_fingerprint([(UUID(int=1), 'long text')])
    monkeypatch.setattr(tool, 'MAX_CORPUS_CHUNKS', 0)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.corpus_fingerprint([(UUID(int=1), 'a')])


def test_query_and_scoring_limits_are_explicit():
    """无效Query/Top-K和词项超限安全拒绝，错误不含用户原文。"""
    index = tool.LocalBM25Index([(UUID(int=1), 'NASA')])
    for query in ('', '  ', 'PRIVATE_QUERY\x00'):
        with pytest.raises(tool.BM25RetrievalError, match='BM25_INPUT') as error:
            index.search(query, 20)
        assert 'PRIVATE_QUERY' not in str(error.value)
    for top_k in (0, True, 1.5, 101):
        with pytest.raises(tool.BM25RetrievalError):
            index.search('NASA', top_k)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        index.search(' '.join('term' + str(n) for n in range(tool.MAX_QUERY_TERMS + 1)), 20)


def test_source_query_filters_kb_ready_docs_and_generations_without_vectors():
    """只读SQL在LIMIT前绑定范围和READY/PARTIAL_READY，不读向量或新建真相表。"""
    kb_id, doc_id = uuid4(), uuid4()
    compiled = tool.source_statement(kb_id, [doc_id]).compile(dialect=postgresql.dialect())
    sql, params = str(compiled), list(compiled.params.values())
    assert sql.count('kb_id =') >= 3 and kb_id in params and [doc_id] in params
    assert ['READY', 'PARTIAL_READY'] in params and 'ACTIVE' in params
    assert sql.index('document_chunks.doc_id IN') < sql.index('LIMIT')
    assert 'document_chunks.embedding' not in sql and ' <=> ' not in sql
    assert 'file_path' not in sql and 'embedding_config' not in sql
    assert 'source_parse_task_id' in params and 'source_chunk_task_id' in params
    assert tool.MAX_CORPUS_CHUNKS + 1 in params
    assert 'UPDATE' not in sql and 'CREATE' not in sql and 'FOR UPDATE' not in sql


@pytest.mark.parametrize('change', ['kb', 'scope', 'ready', 'parse', 'chunk', 'index', 'duplicate'])
def test_unsafe_source_rows_are_rejected_not_cached_or_deleted(change):
    """错误来源/非READY/旧代次/重复Chunk全批失败，不用Python删行或旧缓存补结果。"""
    chunk, document = stored_pair()
    kb_id, doc_ids = chunk.kb_id, [document.doc_id]
    rows = [(chunk, document)]
    if change == 'kb':
        chunk.kb_id = uuid4()
    elif change == 'scope':
        doc_ids = [uuid4()]
    elif change == 'ready':
        document.status = 'INDEXING'
    elif change == 'parse':
        chunk.chunk_metadata['source_parse_task_id'] = str(uuid4())
    elif change == 'chunk':
        document.chunk_task_id = uuid4()
    elif change == 'index':
        document.index_task_id = None
    else:
        rows *= 2
    with pytest.raises(tool.BM25RetrievalError, match='BM25_RESULT'):
        tool.select_sources(SqlSessionStub(rows), kb_id, doc_ids)


def test_readonly_sources_and_sql_error_do_not_expose_secrets():
    """来源只做SELECT；待写会话、超限语料及数据库错误不触发落库/重试或泄露SQL。"""
    chunk, document = stored_pair()
    session = SqlSessionStub([(chunk, document)])
    assert tool.select_sources(session, chunk.kb_id) == [(chunk, document)]
    session.dirty = [object()]
    with pytest.raises(tool.BM25RetrievalError, match='BM25_SESSION'):
        tool.select_sources(session, chunk.kb_id)
    assert len(session.statements) == 1

    class Failure(SqlSessionStub):
        """只抛私有错误的只读接口Stub，不连接PostgreSQL。"""

        def execute(self, statement):
            """返回异常而非执行SQL，检查异常原文不会透传。"""
            raise SQLAlchemyError('PRIVATE_PASSWORD_SQL')

    with pytest.raises(tool.BM25RetrievalError, match='BM25_DATABASE') as error:
        tool.select_sources(Failure([]), chunk.kb_id)
    assert 'PRIVATE_PASSWORD_SQL' not in str(error.value)


def test_explicit_corpus_limits_preserve_default_rank_and_fingerprint():
    """实例预算不是另一种分词/打分；足够预算下同语料身份及稳定排名保持不变。"""
    items = [(UUID(int=1), "NASA 100 USD"), (UUID(int=2), "NASA 200 USD")]
    limits = tool.CorpusLimits(100_000, 256 * 1024**2, 32_000_000)
    assert tool.corpus_fingerprint(items) == tool.corpus_fingerprint(items, limits=limits)
    assert tool.LocalBM25Index(items).search("NASA 100", 20) == tool.LocalBM25Index(items, limits=limits).search("NASA 100", 20)


def test_explicit_resource_budget_never_silently_truncates():
    """超出显式语料预算仍整批拒绝；默认10000/32MiB上限不被全局修改。"""
    items = [(UUID(int=1), "NASA"), (UUID(int=2), "USD")]
    with pytest.raises(tool.BM25RetrievalError) as error:
        tool.LocalBM25Index(items, limits=tool.CorpusLimits(1, 1024, 100))
    assert error.value.code == "BM25_LIMIT"
    assert tool.MAX_CORPUS_CHUNKS == 10_000 and tool.MAX_CORPUS_BYTES == 32 * 1024**2


@pytest.mark.parametrize("value", [True, 0, -1, 200_001])
def test_explicit_chunk_budget_is_bounded_and_not_boolean(value):
    """拒绝无效/无界预算，不把资源参数当作正式样本前N限制。"""
    with pytest.raises(tool.BM25RetrievalError):
        tool.CorpusLimits(value, 1024, 100)


def test_streamed_index_preserves_eager_bm25plus_scores_and_shares_terms(monkeypatch):
    """逐块建索引保持原分词及精确评分，重复词项只驻留一个字符串对象。"""
    import rank_bm25

    items = [(UUID(int=1), 'repeat_keyword repeat_keyword NASA 深度求索 100'),
             (UUID(int=2), 'repeat_keyword ESA 深度求索 100'), (UUID(int=3), '!!!')]
    eager = rank_bm25.BM25Plus([tool.tokenize(text) for _, text in items], k1=1.5, b=0.75, delta=1)
    original = rank_bm25.BM25Plus

    def streamed(corpus, **kwargs):
        """只接收单次迭代器，防止重新积攒全库词项数组。"""
        assert iter(corpus) is corpus
        return original(corpus, **kwargs)

    monkeypatch.setattr(rank_bm25, 'BM25Plus', streamed)
    index = tool.LocalBM25Index(items)
    assert index._engine.doc_freqs == eager.doc_freqs and index._engine.idf == eager.idf
    assert index._engine.doc_len == eager.doc_len and index._engine.avgdl == eager.avgdl
    terms = [next(term for term in frequencies if term == 'repeat_keyword')
             for frequencies in index._engine.doc_freqs[:2]]
    assert terms[0] is terms[1]
    for query in ('NASA', '深度求索 100', 'repeat_keyword ESA', 'unknown'):
        tokens = list(dict.fromkeys(tool.tokenize(query)))
        matches = [position for position, frequencies in enumerate(eager.doc_freqs)
                   if not set(tokens).isdisjoint(frequencies)]
        expected = sorted(zip(matches, eager.get_batch_scores(tokens, matches)),
                          key=lambda hit: (-hit[1], items[hit[0]][0].int))[:2]
        assert index.search(query, 2) == expected


def test_source_iteration_batches_and_closes_when_consumer_stops():
    """调用方提前停止也可关闭服务端游标，不依赖GC释放连接或all()物化。"""
    pair = stored_pair()
    session = SqlSessionStub([pair])
    sources = tool.iter_sources(session, pair[0].kb_id)
    assert next(sources) == pair and not session.results[0].closed
    sources.close()
    assert session.results[0].closed
    assert session.statements[0].get_execution_options()['yield_per'] == tool.SOURCE_BATCH_SIZE


def test_fetch_failure_closes_cursor_and_hides_private_error():
    """分批拉取中途失败不返回半份来源，游标关闭且数据库错误原文不外泄。"""
    class FailedFetch(StreamingResultStub):
        """首行成功后模拟驱动取下一批时抛错。"""

        def __iter__(self):
            """保留一次成功读取，随后返回受控数据库故障。"""
            yield self.rows[0]
            raise SQLAlchemyError('PRIVATE_FETCH_PASSWORD')

    pair = stored_pair()
    session = SqlSessionStub([pair])
    session.result_type = FailedFetch
    with pytest.raises(tool.BM25RetrievalError, match='BM25_DATABASE') as error:
        tool.select_sources(session, pair[0].kb_id)
    assert session.results[0].closed and 'PRIVATE_FETCH_PASSWORD' not in str(error.value)


@pytest.mark.parametrize('budget', [tool.CorpusLimits(1, 4096, 100), tool.CorpusLimits(10, 1, 100)])
def test_streamed_limit_failure_closes_cursor_without_truncating(budget):
    """行数或字节在后续批次超限时整批失败，不把已读前缀作为完整语料。"""
    first = stored_pair()
    second = stored_pair(first[0].kb_id, 2)
    session = SqlSessionStub([first, second])
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.select_sources(session, first[0].kb_id, limits=budget)
    assert session.results[0].closed


def test_cursor_close_failure_uses_safe_database_error():
    """关闭游标异常也必须安全映射，不把驱动内部连接信息透传调用方。"""
    class FailedClose(StreamingResultStub):
        """模拟已读完来源后驱动释放失败。"""

        def close(self):
            """记录尝试关闭，再抛含私有信息的驱动异常。"""
            super().close()
            raise SQLAlchemyError('PRIVATE_CLOSE_CONNECTION')

    pair = stored_pair()
    session = SqlSessionStub([pair])
    session.result_type = FailedClose
    with pytest.raises(tool.BM25RetrievalError, match='BM25_DATABASE') as error:
        tool.select_sources(session, pair[0].kb_id)
    assert session.results[0].closed and 'PRIVATE_CLOSE_CONNECTION' not in str(error.value)


def test_snapshot_projection_covers_full_sources_without_returning_corpus_text():
    """摘要字段经批次代理仍完整；正文与来源仅计算Hash，不返回全库正文或JSON。"""
    kb_id, doc_id = uuid4(), uuid4()
    statement = tool.source_statement(kb_id, [doc_id], snapshot=True, chunk_ids=[UUID(int=1)])
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert tuple(statement.selected_columns.keys()) == ('chunk_id', 'text_bytes', 'text_hash', 'source_hash')
    assert sql.count('sha256(') == 2 and 'jsonb_build_array(' in sql
    assert 'bm25_source_batch.doc_id IN' in sql and 'documents.status IN' in sql
    # chunk_metadata对应数据库metadata列；直接检查摘要参数，不把WHERE出现字段算作摘要覆盖。
    source_array = next(value for value in visitors.iterate(statement.selected_columns['source_hash'])
        if isinstance(value, FunctionElement) and value.name == 'jsonb_build_array')
    expected = [getattr(DocumentChunk, name).property.columns[0]
        for name in tool.SOURCE_CHUNK_FIELDS if name != 'text']
    expected += [getattr(Document, name).property.columns[0] for name in tool.SOURCE_DOCUMENT_FIELDS]
    actual = tuple(source_array.clauses)
    assert len(actual) == len(expected)
    assert all(column.shares_lineage(mapped) for column, mapped in zip(actual, expected))
    # 正文独立参与text_hash，来源摘要无需再次放入整段正文。
    text_conversion = next(value for value in visitors.iterate(statement.selected_columns['text_hash'])
        if isinstance(value, FunctionElement) and value.name == 'convert_to')
    assert tuple(text_conversion.clauses)[0].shares_lineage(DocumentChunk.text.property.columns[0])
    assert 'document_chunks.embedding' not in sql and 'file_path' not in sql


def test_source_snapshot_is_complete_stable_and_invalidates_metadata_only_changes():
    """全量摘要包含未命中行的元数据/页码/模型身份，不仅核对Chunk数量及任务ID。"""
    pair = stored_pair()
    first, items = tool.read_source_snapshot(SqlSessionStub([pair]), pair[0].kb_id)
    again, same_items = tool.read_source_snapshot(SqlSessionStub([pair]), pair[0].kb_id)
    assert first == again and items == same_items
    pair[0].chunk_metadata['source_spans'][0]['page'] = 4
    changed, unchanged_text = tool.read_source_snapshot(SqlSessionStub([pair]), pair[0].kb_id)
    assert changed != first and unchanged_text == items
    assert all(isinstance(text_hash, bytes) and len(text_hash) == 32 for _, text_hash in items)


@pytest.mark.parametrize('budget', [tool.CorpusLimits(1, 4096, 100), tool.CorpusLimits(10, 1, 100)])
def test_source_snapshot_enforces_full_corpus_budgets_and_closes_cursor(budget):
    """摘要路径也检查总行数/原文字节预算，拒绝前缀成功并关闭游标。"""
    first = stored_pair()
    session = SqlSessionStub([first, stored_pair(first[0].kb_id, 2)])
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT'):
        tool.read_source_snapshot(session, first[0].kb_id, limits=budget)
    assert session.results[0].closed


@pytest.mark.parametrize('streaming_statement', [False, True], ids=['base', 'streaming'])
def test_snapshot_bounds_id_batch_before_hashing_full_sources(streaming_statement):
    """流式选项保留Chunk和文档两道屏障，Hash外层只读受限CTE，不连接原始全表。"""
    kb_id, doc_id = uuid4(), uuid4()
    chunk_ids = [UUID(int=number) for number in range(1, tool.SOURCE_BATCH_SIZE + 1)]
    candidates = tool.source_statement(kb_id, [doc_id], ids_only=True)
    candidate_sql = str(candidates.compile(dialect=postgresql.dialect()))
    assert tuple(candidates.selected_columns.keys()) == ('chunk_id',)
    assert 'sha256(' not in candidate_sql and 'jsonb_build_array(' not in candidate_sql
    assert 'document_chunks.doc_id IN' in candidate_sql and 'documents.status IN' in candidate_sql
    assert candidates._limit_clause.value == tool.MAX_CORPUS_CHUNKS + 1
    statement = tool.source_statement(kb_id, [doc_id], snapshot=True, chunk_ids=chunk_ids)
    if streaming_statement:
        # source_cursor只通过生成式执行选项复制语句；底层深遍历会干扰ORM的CTE列绑定。
        original_sql = str(statement.compile(dialect=postgresql.dialect()))
        statement = statement.execution_options(yield_per=tool.SOURCE_BATCH_SIZE)
        assert statement.get_execution_options()['yield_per'] == tool.SOURCE_BATCH_SIZE
    compiled = statement.compile(dialect=postgresql.dialect())
    if streaming_statement:
        assert str(compiled) == original_sql
    batch = next(value for value in visitors.iterate(statement)
        if isinstance(value, CTE) and value.name == 'bm25_source_batch')
    batch_sql = str(batch.element.compile(dialect=postgresql.dialect()))
    documents = next(value for value in visitors.iterate(statement)
        if isinstance(value, CTE) and value.name == 'bm25_documents')
    assert 'bm25_source_batch AS MATERIALIZED' in str(compiled)
    expected = [getattr(DocumentChunk, name).property.columns[0] for name in tool.SOURCE_CHUNK_FIELDS]
    actual = tuple(batch.element.selected_columns)
    assert len(actual) == len(expected)
    assert all(column.shares_lineage(mapped) for column, mapped in zip(actual, expected))
    assert 'sha256(' not in batch_sql and 'jsonb_build_array(' not in batch_sql
    assert 'document_chunks.chunk_id IN' in batch_sql and 'JOIN' not in batch_sql
    assert 'bm25_source_batch.doc_id IN' in str(compiled)
    # 检查最外层关系树；遍历整个AST会误把CTE内部合法的主键读取也算作外层全表连接。
    relations, pending = [], list(statement.get_final_froms())
    while pending:
        relation = pending.pop()
        if isinstance(relation, Join):
            pending.extend((relation.left, relation.right))
        else:
            relations.append(relation)
    # ORM生成FROM时可能克隆CTE；核对表达式结构，不能依赖Python对象身份。
    assert len(relations) == 2
    assert any(relation.compare(batch) for relation in relations)
    assert any(relation.compare(documents) for relation in relations)
    assert all(relation.compare(batch) or relation.compare(documents) for relation in relations)
    source_array = next(value for value in visitors.iterate(statement.selected_columns['source_hash'])
        if isinstance(value, FunctionElement) and value.name == 'jsonb_build_array')
    assert all(column.table.compare(batch) for column in tuple(source_array.clauses)[:len(expected) - 1])
    text_conversion = next(value for value in visitors.iterate(statement.selected_columns['text_hash'])
        if isinstance(value, FunctionElement) and value.name == 'convert_to')
    assert tuple(text_conversion.clauses)[0].table.compare(batch)
    assert 'OFFSET' not in str(compiled) and chunk_ids in compiled.params.values()
    assert ['READY', 'PARTIAL_READY'] in compiled.params.values() and [doc_id] in compiled.params.values()
    assert {'source_parse_task_id', 'source_chunk_task_id', 'embedding_task_id'} <= set(
        value for value in compiled.params.values() if isinstance(value, str))
    assert 'document_chunks.embedding' not in str(compiled)


@pytest.mark.parametrize('snapshot', [False, True], ids=['ids', 'hashes'])
def test_document_generation_checks_are_materialized_before_chunk_checks(snapshot):
    """文档级代次检查不随Chunk重复；摘要文档屏障仅从当前256项批次选父文档。"""
    doc_id = uuid4()
    options = {'snapshot': True, 'chunk_ids': [UUID(int=1)]} if snapshot else {'ids_only': True}
    statement = tool.source_statement(uuid4(), [doc_id], **options)
    documents = next(value for value in visitors.iterate(statement)
        if isinstance(value, CTE) and value.name == 'bm25_documents')
    sql = str(statement.compile(dialect=postgresql.dialect()))
    document_where = str(documents.element.whereclause.compile(dialect=postgresql.dialect()))
    outer_where = str(statement.whereclause.compile(dialect=postgresql.dialect()))
    assert 'bm25_documents AS MATERIALIZED' in sql
    assert 'documents.index_config' in document_where and 'documents.status IN' in document_where
    assert 'documents.doc_id IN' in document_where and 'documents.kb_id =' in document_where
    assert 'bm25_documents.processing_task_id' in outer_where and 'documents.index_config' not in outer_where
    if snapshot:
        assert 'SELECT bm25_source_batch.doc_id' in document_where
        expected = [getattr(Document, name).property.columns[0] for name in tool.SOURCE_DOCUMENT_FIELDS]
        assert len(documents.element.selected_columns) == len(expected)
        assert all(column.shares_lineage(mapped)
            for column, mapped in zip(documents.element.selected_columns, expected))
    else:
        assert tuple(documents.c.keys()) == ('doc_id', 'kb_id', 'processing_task_id')
        assert 'document_chunks' not in document_where
    # BM25不依赖当前Query模型，不能把Dense的模型资格限制带入关键词分支。
    assert 'model' not in statement.compile().params.values()


@pytest.mark.parametrize('count', [0, 1, 5])
def test_source_timings_are_aggregated_once_per_query_not_per_batch(monkeypatch, count):
    """空库不伪造Hash耗时，多批核对仅汇总两条事件且不泄露候选或正文。"""
    monkeypatch.setattr(tool, 'SOURCE_BATCH_SIZE', 2)
    kb_id, events = uuid4(), []

    def log(event, **fields):
        """收集当前调用的安全数值，确认没有逐批成功日志。"""
        events.append((event, fields))

    monkeypatch.setattr(tool, 'log_event', log)
    _, items = tool.read_source_snapshot(SqlSessionStub([
        stored_pair(kb_id, number) for number in range(1, count + 1)]), kb_id)
    expected = ['bm25.source_ids_observed'] + (['bm25.source_hashes_observed'] if count else [])
    assert [event for event, _ in events] == expected and len(items) == count
    assert all(fields['duration_ms'] >= 0 and fields['status'] == 'SUCCESS'
        and fields['chunk_count'] == count for _, fields in events)
    assert all(set(fields) == {'kb_id', 'status', 'duration_ms', 'chunk_count'} for _, fields in events)


@pytest.mark.parametrize('count', [0, 1, 2, 3, 4, 5])
def test_snapshot_scans_ids_once_and_hashes_each_exact_batch(monkeypatch, count):
    """空库及整批边界均正确结束；仅一次候选ID查询，Hash批次不重复筛选全库。"""
    kb_id = uuid4()
    rows = [stored_pair(kb_id, number) for number in range(1, count + 1)]
    expected = tool.read_source_snapshot(SqlSessionStub(rows), kb_id)
    monkeypatch.setattr(tool, 'SOURCE_BATCH_SIZE', 2)
    session = SqlSessionStub(rows)
    assert tool.read_source_snapshot(session, kb_id) == expected
    assert len(session.statements) == 1 + (count + 1) // 2
    assert tuple(session.statements[0].selected_columns.keys()) == ('chunk_id',)
    assert all(result.closed for result in session.results)
    assert all(len(result.rows) <= 2 for result in session.results[1:])
    for position, statement in enumerate(session.statements[1:]):
        params = statement.compile().params
        assert [row[0].chunk_id for row in rows[position * 2:position * 2 + 2]] in params.values()


@pytest.mark.parametrize('budget_kind', ['chunks', 'bytes'])
def test_snapshot_budgets_accumulate_across_sql_pages(monkeypatch, budget_kind):
    """后续批次超过累计行数或字节预算仍整批失败；候选扫描和Hash均关闭游标。"""
    monkeypatch.setattr(tool, 'SOURCE_BATCH_SIZE', 2)
    kb_id = uuid4()
    rows = [stored_pair(kb_id, number) for number in range(1, 5)]
    byte_limit = sum(len(row[0].text.encode('utf-8')) for row in rows[:2])
    limits = tool.CorpusLimits(2 if budget_kind == 'chunks' else 10,
        4096 if budget_kind == 'chunks' else byte_limit, 100)
    session = SqlSessionStub(rows)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_LIMIT') as caught:
        tool.read_source_snapshot(session, kb_id, limits=limits)
    assert caught.value.source_progress == {'batch_index': 2, 'chunk_count': 2,
        'operation': 'SOURCE_IDS' if budget_kind == 'chunks' else 'SOURCE_HASH_BATCH'}
    assert len(session.results) == (2 if budget_kind == 'chunks' else 3)
    assert all(result.closed for result in session.results)


def test_snapshot_late_page_metadata_change_invalidates_full_fingerprint(monkeypatch):
    """非首批且未命中项仅改来源也使完整摘要失效，正文Hash列表仍保持原样。"""
    monkeypatch.setattr(tool, 'SOURCE_BATCH_SIZE', 2)
    kb_id = uuid4()
    rows = [stored_pair(kb_id, number) for number in range(1, 6)]
    before, items = tool.read_source_snapshot(SqlSessionStub(rows), kb_id)
    rows[-1][0].chunk_metadata['source_spans'][0]['page'] = 5
    changed, same_items = tool.read_source_snapshot(SqlSessionStub(rows), kb_id)
    assert changed != before and same_items == items and len(items) == len(rows)


@pytest.mark.parametrize('options', [
    {'snapshot': True}, {'snapshot': True, 'ids_only': True},
    {'ids_only': 'PRIVATE_MODE'}, {'ids_only': True, 'chunk_ids': [UUID(int=1)]},
    {'snapshot': True, 'chunk_ids': [UUID(int=1)] * 2},
    {'snapshot': True, 'chunk_ids': [UUID(int=n) for n in range(1, tool.SOURCE_BATCH_SIZE + 2)]},
])
def test_snapshot_rejects_unbounded_or_conflicting_modes(options):
    """摘要必须绑定有界唯一ID，候选扫描不能混用摘要或Top-K模式，不回显非法参数。"""
    with pytest.raises(tool.BM25RetrievalError, match='BM25_INPUT') as caught:
        tool.source_statement(uuid4(), **options)
    assert 'PRIVATE_MODE' not in str(caught.value)


def test_full_evidence_keeps_original_top_k_id_limit():
    """摘要可读256个ID不放宽完整证据Top-K的100项上限。"""
    with pytest.raises(tool.BM25RetrievalError, match='BM25_INPUT'):
        tool.source_statement(uuid4(), chunk_ids=[UUID(int=n) for n in range(1, 102)])


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'reordered', 'unexpected'])
def test_snapshot_requires_exact_candidate_and_hash_row_alignment(damage):
    """摘要缺行、重复、错序或混入其他ID均整题失败，不能发布看似完整的前缀摘要。"""
    first = stored_pair()
    rows = [first, stored_pair(first[0].kb_id, 2)]

    class MisalignedSession(SqlSessionStub):
        """仅破坏Hash返回行，候选ID仍来自完整受控列表。"""

        def execute(self, statement):
            """模拟驱动/返回结构偏差，不在Stub里替生产代码纠正结果。"""
            result = super().execute(statement)
            if 'source_hash' in statement.selected_columns.keys():
                if damage == 'missing':
                    result.rows = result.rows[:1]
                elif damage == 'duplicate':
                    result.rows = [result.rows[0], result.rows[0]]
                elif damage == 'reordered':
                    result.rows = list(reversed(result.rows))
                else:
                    result.rows = [(UUID(int=99), *result.rows[0][1:])]
            return result

    session = MisalignedSession(rows)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_RESULT'):
        tool.read_source_snapshot(session, first[0].kb_id)
    assert all(result.closed for result in session.results)


@pytest.mark.parametrize('ids', [[UUID(int=0)], ['PRIVATE_ID'], [UUID(int=2), UUID(int=1)],
                                 [UUID(int=1), UUID(int=1)]])
def test_candidate_ids_are_validated_before_hash_batch(ids):
    """候选ID无效、逆序或重复时不执行Hash查询，不泄露内部ID或返回旧索引。"""
    class InvalidCandidateSession(SqlSessionStub):
        """模拟候选投影不符合数据库契约。"""

        def execute(self, statement):
            """只替换ID投影结果，并记录游标以检查失败释放。"""
            result = super().execute(statement)
            if tuple(statement.selected_columns.keys()) == ('chunk_id',):
                result.rows = [(value,) for value in ids]
            return result

    session = InvalidCandidateSession([])
    with pytest.raises(tool.BM25RetrievalError, match='BM25_RESULT') as caught:
        tool.read_source_snapshot(session, uuid4())
    assert len(session.statements) == 1 and session.results[0].closed
    assert caught.value.source_progress['operation'] == 'SOURCE_IDS'
    assert 'PRIVATE_ID' not in str(caught.value)


def test_candidate_fetch_failure_after_hash_batch_preserves_sqlstate_and_closes_cursors(monkeypatch):
    """续读取消保留SQLSTATE/进度/部分耗时，关闭次生错误和计时事件均不得覆盖根因。"""
    from test_retrieval_database import driver_error

    monkeypatch.setattr(tool, 'SOURCE_BATCH_SIZE', 2)
    first = stored_pair()
    rows = [first, stored_pair(first[0].kb_id, 2), stored_pair(first[0].kb_id, 3)]

    class FailedCandidates(StreamingResultStub):
        """仅候选投影第二次拉取失败，Hash批次仍可完整读取。"""

        def __iter__(self):
            """候选前两项可读，下一批模拟取消；Hash结果不注入故障。"""
            if self.rows and len(self.rows[0]) == 1:
                yield from self.rows[:2]
                raise driver_error('57014')
            yield from self.rows

        def close(self):
            """关闭候选时再模拟连接异常，验证清理不会替换首次取消原因。"""
            super().close()
            if self.rows and len(self.rows[0]) == 1:
                raise driver_error('08006')

    session = SqlSessionStub(rows)
    session.result_type = FailedCandidates
    events = []

    def log(event, **fields):
        """捕获失败前已完成及中断阶段的聚合计时，不写磁盘。"""
        events.append((event, fields))

    monkeypatch.setattr(tool, 'log_event', log)
    with pytest.raises(tool.BM25RetrievalError, match='BM25_DATABASE_QUERY_CANCELED') as caught:
        tool.read_source_snapshot(session, first[0].kb_id)
    assert caught.value.database_diagnostic == {'reason': 'QUERY_CANCELED', 'sqlstate': '57014'}
    assert caught.value.source_progress == {'operation': 'SOURCE_IDS', 'batch_index': 2, 'chunk_count': 2}
    assert len(session.results) == 2 and all(result.closed for result in session.results)
    assert [event for event, _ in events] == ['bm25.source_ids_observed', 'bm25.source_hashes_observed']
    assert all(fields['status'] == 'FAILED' and fields['duration_ms'] >= 0 for _, fields in events)
    assert 'PRIVATE' not in str(caught.value)
