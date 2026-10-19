"""BM25 词项/评分及 SQL 来源的单元用例；用户运行，不生成文档、读取原件或连接数据库。"""

import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import visitors
from sqlalchemy.sql.functions import FunctionElement

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
    """按真实列映射验证摘要参数完整，避免ORM属性名及SQL字符串格式造成误判。"""
    kb_id, doc_id = uuid4(), uuid4()
    statement = tool.source_statement(kb_id, [doc_id], snapshot=True)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert tuple(statement.selected_columns.keys()) == ('chunk_id', 'text_bytes', 'text_hash', 'source_hash')
    assert sql.count('sha256(') == 2 and 'jsonb_build_array(' in sql
    assert 'document_chunks.doc_id IN' in sql and 'documents.status IN' in sql
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
