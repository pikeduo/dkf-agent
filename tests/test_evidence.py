"""用户执行的只读来源Mock；无数据库连接、真实文档、模型、API或解析任务。"""

import copy
import hashlib
import io
import logging
from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import LABEL_STYLE_TABLENAME_PLUS_COL
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from app.core import business_logging as logs
from app.models import Document, DocumentBlock, DocumentChunk, KnowledgeBase
from app.schemas.evidence import Citation, SourceSpan
from app.schemas.rag import Citation as CompatibleCitation, RAGAnswer
from app.schemas.retrieval import RetrievalResult
from app.services.rag import evidence as tool


def source_fixture(file_type="pdf", page=2, *, text="中文来源\n数值10%", kind="text"):
    """构造独立的Block/Chunk字段快照并复制可变坐标，不解析真实文档。"""
    kb_id, doc_id, chunk_id, parse_id, chunk_task_id, block_id = (uuid4() for _ in range(6))
    logical = file_type not in {"pdf", "jpg", "jpeg", "png"}
    bbox = None if logical else [10.0, 20.0, 30.0, 40.0]
    block = DocumentBlock(block_id=block_id, doc_id=doc_id, block_index=1, page=page,
        section="真实块章节", block_type=kind, text=text, bbox=bbox,
        source="mineru_cloud", confidence=None)
    # 两个数据库JSON字段应独立；共享bbox会让单侧损坏用例同时改动两端，失去负向意义。
    span = {"block_id": str(block_id), "block_index": 1, "page": page, "block_type": kind,
            "source": block.source, "bbox": copy.deepcopy(bbox), "block_text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "start_char": 0, "end_char": len(text), "chunk_start_char": 0, "chunk_end_char": len(text), "role": "content"}
    metadata = {"source_parse_task_id": str(parse_id), "file_hash": "a" * 64,
                "page_semantics": "logical" if logical else "physical_pdf" if file_type == "pdf" else "image_page",
                "source_spans": [span]}
    document = Document(doc_id=doc_id, kb_id=kb_id, file_name="已有资料." + file_type, file_type=file_type,
        file_hash="a" * 64, file_path="PRIVATE_PATH", source_type="uploaded", status="READY",
        processing_task_id=parse_id, chunk_task_id=chunk_task_id,
        chunk_config={"source_parse_task_id": str(parse_id), "options": {}}, index_config=None)
    chunk = DocumentChunk(chunk_id=chunk_id, kb_id=kb_id, doc_id=doc_id, text=text,
        page_start=page, page_end=page, section="Chunk原章节", chunk_metadata=metadata)
    return chunk, document, [block]


def result_for(chunk, document):
    """从已加载内存字段构造项目证据，分数仅模拟Query来源，不生成向量。"""
    return RetrievalResult(kb_id=chunk.kb_id, doc_id=chunk.doc_id, chunk_id=chunk.chunk_id,
        file_name=document.file_name, file_type=document.file_type, text=chunk.text, page_start=chunk.page_start,
        page_end=chunk.page_end, section=chunk.section, metadata=copy.deepcopy(chunk.chunk_metadata), retrieval_score=0.5)


def bound_filter_value(statement, column, *, operation=operators.eq, required=True):
    """按真实字段/运算符读取绑定值，不依赖联表列别名影响的自动参数编号。"""
    target = column.__clause_element__()
    values = [expression.right.value for expression in visitors.iterate(statement)
              if isinstance(expression, BinaryExpression) and expression.operator is operation
              and isinstance(expression.right, BindParameter) and expression.left.shares_lineage(target)]
    if not values and not required:
        return None
    # 缺失或重复过滤应让Mock报错，不能返回全范围数据来掩盖生产查询缺陷。
    assert len(values) == 1, "Mock查询缺失或重复指定字段的绑定过滤"
    return values[0]


class SessionStub:
    """模拟独立Session，仅记录只读SQL设置与SELECT，不执行数据库操作。"""

    def __init__(self, sources):
        """保存真实字段契约Stub，允许故障/待写变更/异常返回行测试。"""
        self.sources, self.statements, self.rows = sources, [], []
        self.new, self.dirty, self.deleted = (), (), ()
        self.no_autoflush = nullcontext()
        self.closed, self.active, self.failure, self.forced_rows = False, False, None, None
        self.options = None

    def __enter__(self):
        """进入纯内存上下文，不连接服务。"""
        return self

    def __exit__(self, *args):
        """只标记关闭，不提交/flush或更改源记录。"""
        self.closed = True

    def in_transaction(self):
        """可控模拟既有事务，生产边界必须拒绝复用它。"""
        return self.active

    def connection(self, *, execution_options):
        """记录事务隔离，不建立真实连接。"""
        self.options = execution_options

    def execute(self, statement):
        """按绑定字段返回内存行并检查范围/状态，故障用例可注入越界行，不猜参数名。"""
        self.statements.append(statement)
        if not statement.is_select:
            self.rows = []
            return self
        if self.failure is not None:
            raise self.failure
        if statement.column_descriptions[0]["entity"] is DocumentChunk:
            chunk_id = bound_filter_value(statement, DocumentChunk.chunk_id)
            kb_id = bound_filter_value(statement, DocumentChunk.kb_id)
            doc_id = bound_filter_value(statement, Document.doc_id, required=False)
            assert bound_filter_value(statement, Document.kb_id) == kb_id
            assert bound_filter_value(statement, KnowledgeBase.kb_id) == kb_id
            assert bound_filter_value(statement, KnowledgeBase.status) == "ACTIVE"
            status = bound_filter_value(statement, Document.status)
            assert status == "READY"
            self.rows = (self.forced_rows if self.forced_rows is not None else [
                (chunk, doc) for chunk, doc, _ in self.sources
                if chunk.chunk_id == chunk_id and chunk.kb_id == kb_id and doc.kb_id == kb_id
                and doc.status == status and (doc_id is None or doc.doc_id == doc_id)])
        else:
            assert statement.column_descriptions[0]["entity"] is DocumentBlock, "Mock不接受未知SELECT"
            doc_id = bound_filter_value(statement, DocumentBlock.doc_id)
            ids = set(bound_filter_value(statement, DocumentBlock.block_id, operation=operators.in_op))
            self.rows = [block for _, _, blocks in self.sources for block in blocks
                         if block.block_id in ids and block.doc_id == doc_id]
        return self

    def scalars(self):
        """Block SELECT已是对象列表，不模拟加载原件。"""
        return self

    def all(self):
        """返回内存快照，业务代码负责验证而非Stub伪装成功。"""
        return list(self.rows)

    def flush(self):
        """任何flush均违反只读契约。"""
        pytest.fail("禁止flush")

    def commit(self):
        """任何提交均违反只读契约。"""
        pytest.fail("禁止commit")


def service_for(*sources):
    """构建注入会话，构造不访问引擎、文件或模型。"""
    session = SessionStub(list(sources))

    def factory():
        """用户执行用例时返回纯内存Session。"""
        return session

    return tool.EvidenceService(session_factory=factory), session


def test_citation_contract_reexport_preserves_scores_identity_and_deep_copy():
    """旧导入路径仍是同一类型；工厂保留原字段、未知分数及独立metadata，不查询DB。"""
    chunk, document, _ = source_fixture()
    original = result_for(chunk, document)
    first, second = tool.make_citation(original, 1), tool.make_citation(original, 1)
    assert CompatibleCitation is Citation and first.model_dump() == second.model_dump()
    assert first.model_dump(exclude={"citation_id"}) == original.model_dump()
    assert tool.citation_to_result(first).model_dump() == original.model_dump()
    assert first.rerank_score is None and first.retrieval_score == 0.5
    first.metadata["source_spans"][0]["page"] = 99
    assert original.metadata["source_spans"][0]["page"] == 2


@pytest.mark.parametrize("file_type,page,semantic", [("pdf", 2, "physical_pdf"), ("png", 1, "image_page"),
    ("docx", 1, "logical"), ("xlsx", 1, "logical"), ("pptx", 95, "logical"), ("txt", 1, "logical")])
def test_real_source_fields_and_page_semantics_remain_readonly(file_type, page, semantic):
    """物理PDF/图片/逻辑Office页沿用已有语义，逐字符来源、章节和空置信度不伪造。"""
    source = source_fixture(file_type, page)
    chunk, document, blocks = source
    before = copy.deepcopy(chunk.chunk_metadata)
    service, session = service_for(source)
    assert session.statements == []
    trace = service.resolve(chunk.kb_id, chunk.chunk_id, doc_id=document.doc_id)
    assert trace.status == "verified" and trace.page_semantics == semantic
    assert trace.text == chunk.text and trace.section == chunk.section
    assert trace.source_spans[0].section == blocks[0].section and trace.source_spans[0].confidence is None
    assert trace.source_spans[0].text == chunk.text and trace.source_spans[0].bbox == blocks[0].bbox
    assert session.options == {"isolation_level": "REPEATABLE READ"} and session.closed
    assert [str(stmt) for stmt in session.statements[:3]] == ["SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '10s'", "SET LOCAL lock_timeout = '2s'"]
    assert before == chunk.chunk_metadata and "PRIVATE_PATH" not in trace.model_dump_json()


def test_scope_status_and_minimal_columns_are_pushed_into_sql():
    """编译只读表达式验证KB/doc/READY/ACTIVE过滤；不选路径或向量，不实际执行SQL。"""
    kb_id, chunk_id, doc_id = uuid4(), uuid4(), uuid4()
    compiled = tool.evidence_statement(kb_id, chunk_id, doc_id).compile(dialect=postgresql.dialect())
    sql, values = str(compiled), list(compiled.params.values())
    assert "READY" in values and "ACTIVE" in values and all(value in values for value in (kb_id, chunk_id, doc_id))
    assert sql.count("kb_id =") >= 3 and "LIMIT" in sql
    assert "file_path" not in sql and "document_chunks.embedding" not in sql
    assert all(word not in sql for word in ("UPDATE", "INSERT", "DELETE", "FOR UPDATE"))


@pytest.mark.parametrize("use_table_labels", [False, True])
def test_session_stub_reads_scope_independently_of_compiler_parameter_names(use_table_labels):
    """直接检查Mock路由，列标签变更不影响绑定范围；异常不被业务安全包装掩盖。"""
    source = source_fixture()
    chunk, document, _ = source
    session = SessionStub([source])
    statement = tool.evidence_statement(chunk.kb_id, chunk.chunk_id, document.doc_id)
    if use_table_labels:
        statement = statement.set_label_style(LABEL_STYLE_TABLENAME_PLUS_COL)
    assert session.execute(statement).all() == [(chunk, document)]
    for kb_id, chunk_id, doc_id in ((uuid4(), chunk.chunk_id, document.doc_id),
                                  (chunk.kb_id, uuid4(), document.doc_id),
                                  (chunk.kb_id, chunk.chunk_id, uuid4())):
        assert session.execute(tool.evidence_statement(kb_id, chunk_id, doc_id)).all() == []
    document.status = "FAILED"
    assert session.execute(statement).all() == []


@pytest.mark.parametrize("change", ["page", "bbox", "hash", "text", "offset", "unknown_semantic", "missing_spans", "missing_block", "file_hash", "generation"])
def test_corrupt_or_stale_sources_are_rejected_without_repair(change):
    """仅破坏指定一侧并断言准确错误码，防止共享坐标或Mock错误掩盖真实负向校验。"""
    chunk, document, blocks = source_fixture()
    span = chunk.chunk_metadata["source_spans"][0]
    if change == "page":
        span["page"] = 3
    elif change == "bbox":
        original_bbox = copy.deepcopy(blocks[0].bbox)
        assert span["bbox"] is not blocks[0].bbox
        span["bbox"][0] = 11.0
        assert blocks[0].bbox == original_bbox and span["bbox"] != blocks[0].bbox
    elif change == "hash":
        span["block_text_hash"] = "b" * 64
    elif change == "text":
        blocks[0].text = "PRIVATE_CHANGED_BODY"
    elif change == "offset":
        span["end_char"] += 1
        span["chunk_end_char"] += 1
    elif change == "unknown_semantic":
        chunk.chunk_metadata["page_semantics"] = "unknown"
    elif change == "missing_spans":
        chunk.chunk_metadata.pop("source_spans")
    elif change == "missing_block":
        blocks.clear()
    elif change == "file_hash":
        document.file_hash = "b" * 64
    else:
        document.processing_task_id = uuid4()
    service, session = service_for((chunk, document, blocks))
    with pytest.raises(tool.EvidenceError) as error:
        service.resolve(chunk.kb_id, chunk.chunk_id)
    expected_code = ("EVIDENCE_METADATA" if change in {"unknown_semantic", "missing_spans"}
                     else "EVIDENCE_STALE" if change in {"file_hash", "generation"} else "EVIDENCE_SOURCE")
    assert error.value.code == expected_code
    assert "PRIVATE" not in str(error.value) and session.closed


def test_cross_kb_doc_missing_and_not_ready_never_expand_scope():
    """没有匹配行与注入越界行均安全失败，不扩大到别的知识库或文档。"""
    source = source_fixture()
    chunk, document, _ = source
    service, session = service_for(source)
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_UNAVAILABLE"):
        service.resolve(uuid4(), chunk.chunk_id)
    document.status = "FAILED"
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_UNAVAILABLE"):
        service.resolve(chunk.kb_id, chunk.chunk_id)
    document.status = "READY"
    session.forced_rows = [(chunk, document)]
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SOURCE"):
        service.resolve(chunk.kb_id, chunk.chunk_id, doc_id=uuid4())


def test_repeated_block_context_is_preserved_and_whole_chunk_is_reconstructed():
    """同一Block的两个字符区间不按block_id去重；连接符不是伪造原文，范围逐段校验。"""
    chunk, document, blocks = source_fixture(text="标题正文")
    span = chunk.chunk_metadata["source_spans"][0]
    context, content = copy.deepcopy(span), copy.deepcopy(span)
    context.update(end_char=2, chunk_end_char=2, role="context")
    content.update(start_char=2, chunk_start_char=4, chunk_end_char=6)
    chunk.text = "标题\n\n正文"
    chunk.chunk_metadata["source_spans"] = [context, content]
    service, _ = service_for((chunk, document, blocks))
    trace = service.resolve(chunk.kb_id, chunk.chunk_id)
    assert len(trace.source_spans) == 2 and [span.text for span in trace.source_spans] == ["标题", "正文"]
    assert [span.role for span in trace.source_spans] == ["context", "content"]


def test_multi_page_sources_preserve_each_block_section_and_chunk_page_range():
    """跨页Chunk反查保留原来源顺序/各块章节，未知章节不补齐，不把context页排除。"""
    chunk, document, blocks = source_fixture(page=2, text="标题甲")
    other = source_fixture(page=5, text="正文乙")
    block = other[2][0]
    block.doc_id, block.block_index, block.section = document.doc_id, 2, None
    first = chunk.chunk_metadata["source_spans"][0]
    first["role"] = "context"
    second = copy.deepcopy(other[0].chunk_metadata["source_spans"][0])
    second.update(block_index=2, chunk_start_char=len(chunk.text) + 2,
                  chunk_end_char=len(chunk.text) + 2 + len(block.text))
    chunk.text += "\n\n" + block.text
    chunk.page_end = 5
    chunk.chunk_metadata["source_spans"].append(second)
    blocks.append(block)
    service, _ = service_for((chunk, document, blocks))
    trace = service.resolve(chunk.kb_id, chunk.chunk_id)
    assert (trace.page_start, trace.page_end) == (2, 5) and trace.section == chunk.section
    assert [span.block_id for span in trace.source_spans] == [item.block_id for item in blocks]
    assert [span.section for span in trace.source_spans] == ["真实块章节", None]
    assert trace.source_spans[0].role == "context" and trace.source_spans[1].page == 5


@pytest.mark.parametrize("file_type,page,source,bbox", [
    ("docx", 2, "native_parser", None), ("xlsx", 2, "native_parser", None),
    ("txt", 2, "native_parser", None), ("png", 2, "mineru_cloud", [10., 20., 30., 40.]),
    ("pptx", 2, "mineru_cloud", [10., 20., 30., 40.]),
])
def test_invalid_physical_or_logical_page_semantics_are_not_fabricated(file_type, page, source, bbox):
    """Native逻辑页/图片页及Office空坐标边界严格执行，错误不伪装为有效位置。"""
    chunk, document, blocks = source_fixture(file_type, page)
    blocks[0].source, blocks[0].bbox = source, bbox
    chunk.chunk_metadata["source_spans"][0].update(source=source, bbox=bbox)
    service, _ = service_for((chunk, document, blocks))
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SOURCE"):
        service.resolve(chunk.kb_id, chunk.chunk_id)


@pytest.mark.parametrize("kind", ["table", "formula"])
def test_table_formula_newlines_and_native_formula_text_are_not_modified(kind):
    """结构Block原式/换行与证据区间保持原样，不做公式规范化、计算或表格拆分。"""
    source = source_fixture("xlsx", 1, text="列甲\t10%\n=SUM(A1:B2)", kind=kind)
    service, _ = service_for(source)
    trace = service.resolve(source[0].kb_id, source[0].chunk_id)
    assert trace.text == trace.source_spans[0].text == source[2][0].text
    assert trace.source_spans[0].block_type == kind and trace.source_spans[0].bbox is None


def test_citation_resolve_rejects_rebinding_old_answer_to_changed_chunk():
    """旧引用不能依靠稳定Chunk ID接到新正文；错误不改写旧Citation或数据库。"""
    source = source_fixture()
    chunk, document, _ = source
    cite = tool.make_citation(result_for(chunk, document), 1)
    service, _ = service_for(source)
    assert service.resolve_citation(cite).chunk_id == cite.chunk_id
    chunk.text = "PRIVATE_NEW_TEXT"
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_STALE") as error:
        service.resolve_citation(cite)
    assert cite.text != chunk.text and "PRIVATE" not in str(error.value)


def test_citation_model_identity_and_unloaded_fields_fail_safely():
    """有模型身份的旧引用须匹配当前入库身份；未加载Block字段不做隐式读取。"""
    chunk, document, blocks = source_fixture()
    original = result_for(chunk, document)
    original.embedding_identity = {"model_fingerprint": "b" * 64}
    document.index_config = {"result": {"model": copy.deepcopy(original.embedding_identity)}}
    cite = tool.make_citation(original, 1)
    service, _ = service_for((chunk, document, blocks))
    assert service.resolve_citation(cite).chunk_id == chunk.chunk_id
    document.index_config = {"result": {"model": {"model_fingerprint": "c" * 64}}}
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_STALE"):
        service.resolve_citation(cite)
    del blocks[0].section
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SOURCE"):
        service.resolve(chunk.kb_id, chunk.chunk_id)


def test_answer_same_snapshot_all_or_nothing_and_insufficient_skips_session():
    """多引用在一个快照中核对，任一坏来源全批失败；不足结果不查询/补引用。"""
    one, two = source_fixture(), source_fixture()
    two[0].kb_id = two[1].kb_id = one[0].kb_id
    cites = [tool.make_citation(result_for(source[0], source[1]), number) for number, source in enumerate((one, two), 1)]
    answer = RAGAnswer(status="success", answer="事实甲 [1]\n事实乙 [2]", citations=cites, insufficient_evidence=False)
    service, session = service_for(one, two)
    traces = service.resolve_answer(answer)
    assert [trace.chunk_id for trace in traces] == [cite.chunk_id for cite in cites]
    assert sum(str(stmt) == "SET TRANSACTION READ ONLY" for stmt in session.statements) == 1
    two[2].clear()
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SOURCE"):
        service.resolve_answer(answer)
    session.statements.clear()
    from app.services.rag.synthesis import insufficient_answer
    assert service.resolve_answer(insufficient_answer()) == [] and session.statements == []


@pytest.mark.parametrize("bad", ["", "not-uuid", UUID(int=0), True, None])
def test_invalid_scope_fails_before_session(bad):
    """无效UUID不能触发数据库或作为日志/异常原文输出。"""
    service, session = service_for(source_fixture())
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_INPUT"):
        service.resolve(bad, uuid4())
    assert session.statements == []


def test_session_mutation_and_database_errors_are_safe_no_retry():
    """待写变更/既有事务拒绝，数据库故障不重试/读原件/透传连接和SQL。"""
    source = source_fixture()
    service, session = service_for(source)
    session.dirty = [object()]
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SESSION"):
        service.resolve(source[0].kb_id, source[0].chunk_id)
    session.dirty, session.active = (), True
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_SESSION"):
        service.resolve(source[0].kb_id, source[0].chunk_id)
    session.active, session.failure = False, SQLAlchemyError("PRIVATE_SQL PRIVATE_CONNECTION")
    with pytest.raises(tool.EvidenceError, match="EVIDENCE_DATABASE") as error:
        service.resolve(source[0].kb_id, source[0].chunk_id)
    assert "PRIVATE" not in str(error.value) and session.closed


def test_answer_markers_cannot_reference_unknown_or_unused_citations():
    """答案标记与本地Citation一一对应，未知/缺失编号不作为来源链成功。"""
    chunk, document, _ = source_fixture()
    cite = tool.make_citation(result_for(chunk, document), 1)
    for answer in ("事实无标记", "事实 [2]", "事实 [1][999]", "事实 [1][ 999 ]", "事实 [1][1-2]"):
        with pytest.raises(ValidationError):
            RAGAnswer(status="success", answer=answer, citations=[cite], insufficient_evidence=False)


def test_source_schema_rejects_invalid_intervals_and_coordinates():
    """来源区间/坐标无效不能靠宽松类型或负区间通过。"""
    span = source_fixture()[0].chunk_metadata["source_spans"][0]
    for change in ({"start_char": True}, {"end_char": 0}, {"chunk_start_char": 1}, {"bbox": [20., 0., 10., 5.]}):
        with pytest.raises(ValidationError):
            SourceSpan.model_validate(span | change)


def test_evidence_logs_contain_only_registered_role_ids_counts_and_safe_failure():
    """来源日志不记录原文/路径/metadata/SQL；安全失败不能输出核对成功事件。"""
    source = source_fixture(text="PRIVATE_BODY")
    service, _ = service_for(source)
    stream, logger = io.StringIO(), logs.LOGGER
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.ReadableFormatter("evidence-cli"))
    token = logs.CONTEXT.set(None)
    try:
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.INFO)
        service.resolve(source[0].kb_id, source[0].chunk_id)
        assert "Evidence CLI" in stream.getvalue() and "event=evidence.finished" in stream.getvalue()
        stream.seek(0)
        stream.truncate(0)
        source[2].clear()
        with pytest.raises(tool.EvidenceError):
            service.resolve(source[0].kb_id, source[0].chunk_id)
        output = stream.getvalue()
        assert "event=evidence.failed" in output and "evidence.finished" not in output and "PRIVATE" not in output
    finally:
        handler.close()
        logger.handlers, logger.propagate, logger.disabled = previous[0], previous[2], previous[3]
        logger.setLevel(previous[1])
        logs.CONTEXT.reset(token)
