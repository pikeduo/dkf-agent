"""用户执行的 Dense 参数/SQL/结果 Stub 测试；不连接数据库、导入框架或加载模型。"""

import copy
from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.models import Document, DocumentChunk
from app.services.rag.dense import (
    DEFAULT_TOP_K, MAX_DOC_IDS, MAX_QUERY_CHARS, DenseRetrievalError, dense_statement, model_snapshot,
    select_dense_hits, validate_doc_ids, validate_query, validate_scope,
)
from app.services.indexing import INDEX_VERSION


def model_identity():
    """内存身份契约 Stub，不读取实际模型文件或生成模型权重。"""
    return {"model_name": "BAAI/bge-m3", "model_fingerprint": "b" * 64, "dimension": 1024,
            "normalized": True, "version": "bge-m3-dense-v1", "max_length": 8192, "precision": "fp16"}


def stored_pair(kb_id=None, chunk_number=1):
    """只构造已有 ORM 字段，不绑定 Session、不写库或生成任何文档验收样本。"""
    kb_id = kb_id or uuid4()
    doc_id, parse_id, chunk_id, embed_id, index_id = (uuid4() for _ in range(5))
    config = {"version": INDEX_VERSION, "embedding_task_id": str(embed_id),
              "source_chunk_task_id": str(chunk_id), "source_parse_task_id": str(parse_id),
              "result": {"model": model_identity()}}
    document = Document(doc_id=doc_id, kb_id=kb_id, file_name="已有资料.pdf", file_type="pdf", status="READY",
                        processing_task_id=parse_id, chunk_task_id=chunk_id, embedding_task_id=embed_id,
                        index_task_id=index_id, index_config=config)
    metadata = {"source_parse_task_id": str(parse_id), "chunk_index": 1, "page_semantics": "physical_pdf",
                "source_spans": [{"block_id": str(uuid4()), "page": 3, "start_char": 0, "end_char": 8}]}
    chunk = DocumentChunk(chunk_id=UUID(int=chunk_number), doc_id=doc_id, kb_id=kb_id,
                          text="既有证据原文\n=A1+B1", page_start=3, page_end=4, section="原章节",
                          chunk_metadata=metadata)
    return chunk, document


class SqlSessionStub:
    """仅记录 SELECT 与返回预设行；不会计算向量、执行 SQL 或进行写入。"""

    def __init__(self, rows):
        """初始化只读内存状态，不创建引擎或事务。"""
        self.rows, self.statements = rows, []
        self.new, self.dirty, self.deleted = (), (), ()
        self.no_autoflush = nullcontext()

    def execute(self, statement):
        """保留待检查的 SQLAlchemy 表达式，断言是只读 SELECT。"""
        assert statement.is_select
        self.statements.append(statement)
        return self

    def all(self):
        """返回真实查询结果契约的行 Stub，不自动替生产逻辑排序或过滤。"""
        return list(self.rows)


def test_statement_filters_before_exact_top_k_and_binds_identity():
    """SQL 内强制跨库/READY/非空/实际模型及当前代次过滤，物化后稳定精确排序。"""
    kb_id, identity = uuid4(), model_identity()
    statement = dense_statement(kb_id, [1.0] + [0.0] * 1023, identity)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql, values = str(compiled), list(compiled.params.values())
    assert "dense_eligible AS MATERIALIZED" in sql and " <=> " in sql
    assert "documents.status =" in sql and "READY" in values and "ACTIVE" in values
    assert sql.count("kb_id =") >= 3 and kb_id in values
    assert "document_chunks.embedding IS NOT NULL" in sql
    assert identity in values and INDEX_VERSION in values
    assert "documents.embedding_task_id" in sql and "documents.chunk_task_id" in sql
    assert "documents.processing_task_id" in sql and "source_parse_task_id" in values
    assert sql.index("WHERE") < sql.index("LIMIT")
    assert "dense_eligible.chunk_id ASC" in sql and "document_chunks.chunk_id ASC" in sql
    assert DEFAULT_TOP_K in values and str(kb_id) not in sql
    assert "file_path" not in sql and "embedding_config" not in sql
    assert "CREATE" not in sql and "UPDATE" not in sql and "FOR UPDATE" not in sql


@pytest.mark.parametrize("multiple", [False, True])
def test_document_filter_is_bound_before_distance_and_limit(multiple):
    """单/多文档 UUID 在物化候选内参数化过滤，不先全库 Top-K 再删除证据。"""
    kb_id, first, second = uuid4(), uuid4(), uuid4()
    doc_ids = [str(first), second, first] if multiple else [str(first)]
    statement = dense_statement(kb_id, [1.0] + [0.0] * 1023, model_identity(), doc_ids=doc_ids)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.index("document_chunks.doc_id IN") < sql.index(" <=> ") < sql.index("LIMIT")
    assert any(value == list(validate_doc_ids(doc_ids)) for value in compiled.params.values() if isinstance(value, list))
    assert str(first) not in sql and str(second) not in sql
    assert sql.count("kb_id =") >= 3
    assert "READY" in compiled.params.values() and "ACTIVE" in compiled.params.values()
    assert "document_chunks.embedding IS NOT NULL" in sql and model_identity() in compiled.params.values()


def test_omitted_document_scope_keeps_stage17_full_kb_statement():
    """省略/None 不添加文档谓词，保留阶段17全库语义，但显式空范围不能被当作 None。"""
    kb_id, vector = uuid4(), [1.0] + [0.0] * 1023
    original = dense_statement(kb_id, vector, model_identity()).compile(dialect=postgresql.dialect())
    explicit = dense_statement(kb_id, vector, model_identity(), doc_ids=None).compile(dialect=postgresql.dialect())
    assert str(original) == str(explicit) and original.params == explicit.params
    assert "document_chunks.doc_id IN" not in str(original)
    assert validate_doc_ids(None) is None


@pytest.mark.parametrize("doc_ids", [[], (), "private-document-id", 123, {}, {UUID(int=1)},
    [None], [True], [UUID(int=0)], ["private-document-id"]])
def test_invalid_document_scope_is_rejected_safely(doc_ids):
    """空/非列表/非法 UUID 范围立即拒绝，异常不带原始参数，也不放宽到全库。"""
    with pytest.raises(DenseRetrievalError, match="DENSE_INPUT") as error:
        dense_statement(uuid4(), [1.0] + [0.0] * 1023, model_identity(), doc_ids=doc_ids)
    assert "private-document-id" not in str(error.value)


def test_document_scope_is_frozen_deduplicated_and_bounded():
    """字符串与 UUID 归一化为稳定元组，不改调用方列表；限制按输入数量生效。"""
    first, second = UUID(int=1), UUID(int=2)
    incoming = [second, str(first), first]
    before = incoming[:]
    assert validate_doc_ids(incoming) == (first, second) and incoming == before
    assert len(validate_doc_ids([UUID(int=n) for n in range(1, MAX_DOC_IDS + 1)])) == MAX_DOC_IDS
    with pytest.raises(DenseRetrievalError, match="DENSE_LIMIT"):
        validate_doc_ids([first] * (MAX_DOC_IDS + 1))


def test_out_of_document_scope_rejects_whole_result_not_python_filters():
    """同库的其他文档也不能混入；模拟错误查询返回混合行时全批失败，不事后删行冒充成功。"""
    first = stored_pair(chunk_number=1)
    other = stored_pair(first[0].kb_id, chunk_number=2)
    session = SqlSessionStub([(*first, 0.1), (*other, 0.2)])
    with pytest.raises(DenseRetrievalError, match="DENSE_RESULT"):
        select_dense_hits(session, first[0].kb_id, [1.0] + [0.0] * 1023, model_identity(), doc_ids=[first[1].doc_id])
    assert len(session.statements) == 1


def test_multiple_document_hits_keep_order_and_empty_scope_never_falls_back():
    """多文档仍按距离/Chunk ID排序；不存在/跨库/非READY等被SQL排除后为空，不扩大范围。"""
    first = stored_pair(chunk_number=1)
    second = stored_pair(first[0].kb_id, chunk_number=2)
    ids = [second[1].doc_id, first[1].doc_id]
    session = SqlSessionStub([(*first, 0.1), (*second, 0.1)])
    hits = select_dense_hits(session, first[0].kb_id, [1.0] + [0.0] * 1023, model_identity(), doc_ids=ids)
    assert [hit.chunk.chunk_id for hit in hits] == [first[0].chunk_id, second[0].chunk_id]
    empty = SqlSessionStub([])
    assert select_dense_hits(empty, first[0].kb_id, [1.0] + [0.0] * 1023, model_identity(), doc_ids=[uuid4()]) == []
    assert len(empty.statements) == 1 and "document_chunks.doc_id IN" in str(empty.statements[0])


@pytest.mark.parametrize("kb_id,top_k", [(None, 20), (True, 20), ("private_invalid_id", 20),
    (UUID(int=0), 20), (uuid4(), 0), (uuid4(), -1), (uuid4(), True), (uuid4(), 1.5), (uuid4(), "20")])
def test_invalid_scope_never_builds_sql(kb_id, top_k):
    """异常范围严格拒绝，不强制转换布尔/浮点或泄露原始参数。"""
    with pytest.raises(DenseRetrievalError, match="DENSE_INPUT") as error:
        validate_scope(kb_id, top_k)
    assert "private_invalid_id" not in str(error.value)


def test_top_k_limit_and_uuid_string_are_explicit():
    """默认 20、最多 100；合法 UUID 字符串可用，超过上限不能截断为成功。"""
    identity = uuid4()
    assert validate_scope(str(identity), 100) == identity
    with pytest.raises(DenseRetrievalError, match="DENSE_LIMIT"):
        validate_scope(identity, 101)


@pytest.mark.parametrize("query", [None, 123, "", " \n\t", "private\x00query", "private\x7fquery"])
def test_invalid_query_is_safe(query):
    """空/非字符串/控制符拒绝，不在异常写出 Query 原文。"""
    with pytest.raises(DenseRetrievalError, match="DENSE_INPUT") as error:
        validate_query(query)
    assert "private" not in str(error.value)


def test_query_is_not_trimmed_and_overlimit_is_not_truncated():
    """编码保留 Query 原文，超限直接拒绝而非截断前缀。"""
    query = "  原始问题\n保持原式  "
    assert validate_query(query) == query
    with pytest.raises(DenseRetrievalError, match="DENSE_LIMIT"):
        validate_query("x" * (MAX_QUERY_CHARS + 1))


@pytest.mark.parametrize("field,value", [("version", "other-encoder"), ("model_name", "other-model"),
    ("dimension", True), ("dimension", 768), ("normalized", False), ("model_fingerprint", "private-path"),
    ("precision", "other"), ("max_length", True), ("max_length", 8193)])
def test_query_identity_must_match_current_encoding_contract(field, value):
    """同维度不是同模型；版本、指纹、长度、精度与归一化均须明确有效。"""
    identity = model_identity()
    identity[field] = value
    with pytest.raises(DenseRetrievalError, match="DENSE_MODEL") as error:
        model_snapshot(identity)
    assert "private-path" not in str(error.value)


def test_sorted_rows_zero_negative_score_and_repeat_are_preserved():
    """距离升序/同分 UUID 顺序稳定；余弦分数为 1-distance，允许零/负值且不当概率。"""
    kb_id = uuid4()
    first, second, third = (stored_pair(kb_id, number) for number in (1, 2, 3))
    session = SqlSessionStub([(*first, 0.0), (*second, 1.0), (*third, 1.5)])
    vector, identity = [1.0] + [0.0] * 1023, model_identity()
    before = copy.deepcopy(first[0].chunk_metadata)
    results = select_dense_hits(session, kb_id, vector, identity)
    repeated = select_dense_hits(session, kb_id, vector, identity)
    assert [hit.chunk.chunk_id for hit in results] == [hit.chunk.chunk_id for hit in repeated]
    assert [hit.similarity for hit in results] == [1.0, 0.0, -0.5]
    assert first[0].chunk_metadata == before and len(session.statements) == 2


@pytest.mark.parametrize("change", ["cross_kb", "not_ready", "wrong_document", "model_fingerprint", "precision", "stale_parse", "stale_chunk"])
@pytest.mark.parametrize("scoped", [False, True])
def test_unsafe_returned_rows_are_rejected_not_repaired(change, scoped):
    """全库/指定文档均保留知识库、READY、模型及代次防线；不安全行全批拒绝不修复。"""
    chunk, document = stored_pair()
    kb_id = chunk.kb_id
    if change == "cross_kb":
        chunk.kb_id = uuid4()
    elif change == "not_ready":
        document.status = "INDEXING"
    elif change == "wrong_document":
        chunk.doc_id = uuid4()
    elif change in {"model_fingerprint", "precision"}:
        document.index_config["result"]["model"][change] = "c" * 64 if change == "model_fingerprint" else "fp32"
    elif change == "stale_parse":
        chunk.chunk_metadata["source_parse_task_id"] = str(uuid4())
    else:
        document.chunk_task_id = uuid4()
    with pytest.raises(DenseRetrievalError, match="DENSE_RESULT"):
        select_dense_hits(SqlSessionStub([(chunk, document, 0.1)]), kb_id, [1.0] + [0.0] * 1023,
                          model_identity(), doc_ids=[document.doc_id] if scoped else None)


@pytest.mark.parametrize("rows_case", ["distance_order", "tie_order", "duplicate", "nan", "over_top_k"])
def test_invalid_order_count_or_distance_does_not_return_partial_results(rows_case):
    """无序/同分逆序/重复/非有限距离或超量数据不能伪装正确 Top-K。"""
    kb_id = uuid4()
    first, second = stored_pair(kb_id, 1), stored_pair(kb_id, 2)
    cases = {"distance_order": [(*first, 0.5), (*second, 0.1)], "tie_order": [(*second, 0.1), (*first, 0.1)],
             "duplicate": [(*first, 0.1), (*first, 0.1)], "nan": [(*first, float("nan"))],
             "over_top_k": [(*first, 0.1), (*second, 0.2)]}
    with pytest.raises(DenseRetrievalError, match="DENSE_RESULT"):
        select_dense_hits(SqlSessionStub(cases[rows_case]), kb_id, [1.0] + [0.0] * 1023,
                          model_identity(), top_k=1 if rows_case == "over_top_k" else 20)


def test_empty_results_and_dirty_session_are_not_writes():
    """空结果保留为空，待写会话拒绝且不 flush/rollback/commit 调用方事务。"""
    session, kb_id = SqlSessionStub([]), uuid4()
    assert select_dense_hits(session, kb_id, [1.0] + [0.0] * 1023, model_identity()) == []
    session.dirty = [object()]
    with pytest.raises(DenseRetrievalError, match="DENSE_SESSION"):
        select_dense_hits(session, kb_id, [1.0] + [0.0] * 1023, model_identity())
    assert len(session.statements) == 1


def test_database_error_and_malformed_row_are_safe_at_sql_boundary():
    """SQL 边界自身也不得暴露连接/向量异常；行结构坏时返回固定结果错误。"""
    class FailingSession(SqlSessionStub):
        """故障接口 Stub，不实际连接或破坏数据库。"""

        def execute(self, statement):
            """只抛私有错误 Stub，验证原始 SQL 信息不会透传。"""
            raise SQLAlchemyError("PRIVATE_SQL_PASSWORD_VECTOR")

    kb_id = uuid4()
    with pytest.raises(DenseRetrievalError, match="DENSE_DATABASE") as error:
        select_dense_hits(FailingSession([]), kb_id, [1.0] + [0.0] * 1023, model_identity())
    assert "PRIVATE_SQL_PASSWORD_VECTOR" not in str(error.value)
    with pytest.raises(DenseRetrievalError, match="DENSE_RESULT"):
        select_dense_hits(SqlSessionStub([("__invalid_row_shape__",)]), kb_id,
                          [1.0] + [0.0] * 1023, model_identity())
