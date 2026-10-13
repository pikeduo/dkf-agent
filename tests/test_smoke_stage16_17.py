"""联合冒烟工具的元数据/HTTP/状态 Stub；仅用户执行，不生成真实 PDF/Office/图像。"""

import copy
import hashlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import smoke_stage16_17 as tool

KB = UUID("1b8ce262-7a44-4cf0-8d35-fec9310df720")
DOC = "c140ae7d-a2df-445a-94ef-6a015677f667"
TASK = "b80a6eb5-3e20-445f-a595-7c61cf6d0017"


def sample():
    """只声明候选元信息，无真实原件或编造的公开数据来源。"""
    return {"file": "samples/ohr/metadata_stub.pdf", "sha256": "a" * 64, "file_type": "pdf",
            "source": "ohr", "identity": "metadata_stub", "size_bytes": 1, "page_count": 2}


def document(status="CHUNKING"):
    """仅表示数据库状态快照，不创建 ORM 记录或投递 Celery。"""
    return {"doc_id": DOC, "kb_id": str(KB), "file_type": "pdf", "file_hash": "a" * 64,
            "status": status, "error_code": None, "processing_task_id": TASK,
            "chunk_task_id": None, "embedding_task_id": None, "index_task_id": None}


class Clock:
    """内存时间 Stub，测试有界等待时不真正 sleep。"""

    def __init__(self):
        """初始化单调时间。"""
        self.value = 0

    def now(self):
        """返回模拟秒数。"""
        return self.value

    def pause(self, seconds):
        """仅推进模拟时钟，不等待真实 Worker。"""
        self.value += seconds


class Response:
    """纯 HTTP 结构 Stub，无真实联网。"""

    def __init__(self, code, body):
        """保存状态及契约字典。"""
        self.status_code, self.body = code, body

    def json(self):
        """返回内存响应，不读取正文文件。"""
        return self.body


class Api:
    """用状态机模拟既有接口，不调用任何内部任务主体。"""

    def __init__(self, rows, fail_upload=False):
        """保存可观察调用和是否模拟已提交但响应丢失。"""
        self.rows, self.calls, self.fail_upload = rows, [], fail_upload

    def request(self, method, path, **kwargs):
        """POST 推进元数据状态，GET 只读；上传始终要求 auto，单文件表单。"""
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/documents"):
            assert kwargs["data"] == {"parse_mode": "auto"}
            assert set(kwargs["files"]) == {"file"}
            row = document()
            self.rows["a" * 64] = row
            if self.fail_upload:
                raise TimeoutError("模拟不可对外输出的响应原文")
            return Response(201, row.copy())
        row = self.rows["a" * 64]
        if method == "POST":
            steps = {"chunk": ("EMBEDDING", "chunk_task_id"),
                     "embed": ("INDEXING", "embedding_task_id"), "index": ("READY", "index_task_id")}
            operation = path.rsplit("/", 1)[1]
            row["status"], field = steps[operation]
            row[field] = TASK
            return Response(202, row.copy())
        return Response(200, row.copy())


def pipeline_stub(monkeypatch, rows, intents=None):
    """用内存日志/流替代文件 IO；只验证 API 行为，不写测试素材、审核或报告。"""
    events = list(intents or [])

    def journal(run, operation, sha, phase, **summary):
        """收集提交意图与确认摘要，不创建日志文件。"""
        events.append((operation, sha, phase))

    def attempted(run, operation, sha):
        """从内存意图判断是否禁止重复提交。"""
        return any(event[:2] == (operation, sha) for event in events)

    def reader(kb_id, plan, **kwargs):
        """读取内存状态并断言始终在指定测试库。"""
        assert kb_id == KB
        return copy.deepcopy(rows)

    def stream(path, *args, **kwargs):
        """提供空流替代普通文件句柄，HTTP Stub 不解析它。"""
        return io.BytesIO()

    def sample_bytes(run, item):
        """跳过真实文件读取，提供只被 HTTP Stub 消费的空流，不作为文档解析样本。"""
        return b""

    monkeypatch.setattr(tool, "journal", journal)
    monkeypatch.setattr(tool, "attempted", attempted)
    monkeypatch.setattr(Path, "open", stream)
    monkeypatch.setattr(tool, "sample_bytes", sample_bytes)
    args = SimpleNamespace(kb_id=KB, timeout=20, poll_interval=1)
    return args, reader, Clock(), events


def test_default_and_all_dry_run_steps_never_execute(monkeypatch, capsys):
    """默认及每个 dry-run 不创建目录、不导入业务服务、不连接数据库/API/GPU。"""
    def forbidden(*args, **kwargs):
        """任一实际操作都会使 Stub 用例失败。"""
        pytest.fail("dry-run 不能执行")

    for name in ("prepare_samples", "run_lock", "load_plan", "precheck", "drive_pipeline", "verify_run", "dense_run"):
        monkeypatch.setattr(tool, name, forbidden)
    assert tool.main([]) == 0
    for step in ("prepare", "process", "verify", "retrieve", "report"):
        assert tool.main(["--step", step, "--dry-run"]) == 0
    assert "DRY-RUN" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--run", "../outside"], ["--pdf-count", "0"],
                                  ["--timeout", "0"], ["--image-count", "4"]])
def test_invalid_cli_returns_nonzero_without_execution(argv):
    """非法目录和预算安全拒绝，不打印用户路径或启动业务。"""
    assert tool.main(argv) == 2


def test_prepare_requires_local_permission(monkeypatch):
    """执行开关不等于本地许可，更不构成云外传许可。"""
    def forbidden(*args, **kwargs):
        """权限未确认时不能创建目录或素材。"""
        pytest.fail("未确认许可")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert tool.main(["--step", "prepare", "--execute", "--run", "permission_stub"]) == 2


def test_http_retains_root_path_and_hides_error_body(capsys):
    """请求路径相对于 base_url；错误只打印 HTTP 状态而非 Token/上游错误。"""
    calls = []

    def request(method, path, **kwargs):
        """返回敏感正文 Stub，验证它不进入输出。"""
        calls.append(path)
        return Response(403, {"detail": "secret-token-and-source-text"})

    with pytest.raises(tool.SmokeError, match="SMOKE_HTTP"):
        tool.api_request(SimpleNamespace(request=request), "POST", "/api/admin/x")
    assert calls == ["api/admin/x"]
    assert "secret-token" not in capsys.readouterr().out
    assert tool.checked_api_url("http://127.0.0.1:8000/prefix/") == "http://127.0.0.1:8000/prefix"
    for value in ("https://outside.example", "http://user:secret@localhost", "http://localhost/?token=x"):
        with pytest.raises(tool.SmokeError):
            tool.checked_api_url(value)


def test_pipeline_posts_each_existing_endpoint_once(monkeypatch):
    """上传→切片→Embedding→Index 都通过 API；重复运行只读 READY，无新 POST。"""
    rows = {}
    args, reader, clock, events = pipeline_stub(monkeypatch, rows)
    api = Api(rows)
    plan = {"documents": [sample()]}
    result = tool.drive_pipeline(args, Path("virtual"), plan, api, reader=reader, clock=clock.now, pause=clock.pause)
    assert result["status"] == "PASS"
    assert [path.rsplit("/", 1)[1] for method, path in api.calls if method == "POST"] == ["documents", "chunk", "embed", "index"]
    assert [event[0] for event in events if event[2] == "intent"] == ["upload", "chunk", "embed", "index"]
    api.calls.clear()
    assert tool.drive_pipeline(args, Path("virtual"), plan, api, reader=reader, clock=clock.now, pause=clock.pause)["status"] == "PASS"
    assert not any(method == "POST" for method, path in api.calls)


def test_upload_response_lost_recovers_without_second_upload(monkeypatch):
    """响应丢失保留意图；同 Hash 已落库后复用身份，不重复收费上传。"""
    rows = {}
    args, reader, clock, events = pipeline_stub(monkeypatch, rows)
    api = Api(rows, fail_upload=True)
    with pytest.raises(tool.SmokeError, match="SMOKE_HTTP"):
        tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
                            reader=reader, clock=clock.now, pause=clock.pause)
    assert ("upload", "a" * 64, "intent") in events
    api.fail_upload = False
    result = tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
                                 reader=reader, clock=clock.now, pause=clock.pause)
    assert result["status"] == "PASS"
    assert sum(method == "POST" and path.endswith("/documents") for method, path in api.calls) == 1


def test_uncertain_upload_without_record_is_blocked(monkeypatch):
    """无落库记录的旧提交意图不自动重传，不使用 /process/force 规避风险。"""
    rows = {}
    args, reader, clock, events = pipeline_stub(monkeypatch, rows, [("upload", "a" * 64, "intent")])
    api = Api(rows)
    with pytest.raises(tool.SmokeError, match="SMOKE_UNCERTAIN"):
        tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
                            reader=reader, clock=clock.now, pause=clock.pause)
    assert api.calls == []


def test_existing_checkpoint_times_out_without_redispatch(monkeypatch):
    """已有切片任务无人消费时等待至超时，不能再次 POST /chunk 或伪装 READY。"""
    rows = {"a" * 64: document() | {"chunk_task_id": TASK}}
    args, reader, clock, events = pipeline_stub(monkeypatch, rows)
    api = Api(rows)
    result = tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
                                 reader=reader, clock=clock.now, pause=clock.pause)
    assert result == {"status": "BLOCKED", "reason": "TIMEOUT", "background_tasks_cancelled": False}
    assert not any(method == "POST" for method, path in api.calls)


def test_failed_document_is_fail_and_never_retried(monkeypatch):
    """FAILED 是失败，不触发重新解析、缓存失效或新的任务代次。"""
    rows = {"a" * 64: document("FAILED")}
    args, reader, clock, events = pipeline_stub(monkeypatch, rows)
    api = Api(rows)
    result = tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
                                 reader=reader, clock=clock.now, pause=clock.pause)
    assert result["status"] == "FAIL"
    assert not any(method == "POST" for method, path in api.calls)


def exported_document():
    """构造字符跨度元数据，不生成或运行文档解析验收样本。"""
    text = "内存断言文本"
    block = {"block_id": "block-stub", "doc_id": DOC, "block_index": 1, "page": 1,
             "section": None, "block_type": "text", "text": text, "bbox": None, "source": "mineru_cloud"}
    span = {"block_id": block["block_id"], "page": 1, "block_index": 1, "start_char": 0,
            "end_char": len(text), "chunk_start_char": 0, "chunk_end_char": len(text),
            "source": block["source"], "block_type": block["block_type"], "bbox": None,
            "block_text_hash": hashlib.sha256(text.encode()).hexdigest()}
    chunk = {"chunk_id": "chunk-stub", "kb_id": str(KB), "doc_id": DOC, "text": text,
             "page_start": 1, "page_end": 1, "section": None,
             "metadata": {"source_spans": [span], "source_parse_task_id": TASK,
                          "chunk_index": 1, "page_semantics": "physical_pdf"}}
    return document("READY") | {"blocks": [block], "chunks": [chunk]}


def test_export_validates_cloud_source_order_and_exact_evidence():
    """真实 source=mineru_cloud 可接受；乱序、错页、跨库、丢失/改动字符证据不可 PASS。"""
    d = exported_document()
    assert tool.validate_export(d, sample(), KB) == []
    modified = copy.deepcopy(d)
    modified["blocks"][0]["block_index"] = None
    assert "BLOCK_ORDER" in tool.validate_export(modified, sample(), KB)
    modified = copy.deepcopy(d)
    modified["chunks"][0]["kb_id"] = str(UUID(int=2))
    assert "CHUNK_EVIDENCE" in tool.validate_export(modified, sample(), KB)
    modified = copy.deepcopy(d)
    modified["chunks"][0]["text"] = "已被改动"
    assert "SOURCE_SPANS" in tool.validate_export(modified, sample(), KB)
    modified = copy.deepcopy(d)
    modified["blocks"][0]["page"] = 3
    assert "BLOCK_EVIDENCE" in tool.validate_export(modified, sample(), KB)
    modified = copy.deepcopy(d)
    del modified["chunks"][0]["metadata"]["source_spans"][0]["source"]
    assert "SPAN_SOURCE_FIELDS" in tool.validate_export(modified, sample(), KB)


def test_official_zero_based_pages_and_multi_rejection():
    """复用 OHR 契约，0-based 页正确转换；multi 不拆成编造的多个 QA。"""
    row = {"ID": "unit-only", "doc_name": "academic/unit-only", "doc_type": "Academic",
           "questions": "原样 Query Stub", "answers": "原样 Answer Stub", "evidence_source": "text", "evidence_page_no": [0]}
    groups, rejected = tool.ohr.qa_rows([row, row | {"ID": "multi-only", "evidence_source": "multi"}])
    record = groups[row["doc_name"]][0]
    assert record["pages"] == (0,) and record["category"] == "plain_text"
    assert record["original"] == row
    assert rejected["multi_evidence_unsupported"] == 1
    qa = row | {"_ohr_reference": {"evidence_pages_1based": [page + 1 for page in record["pages"]]}}
    hit = {"doc_id": DOC, "span_pages": [1]}
    scored = tool.score_retrieval(qa, exported_document(), [hit])
    assert scored["hit_at1"] is True and scored["page_hit_at1"] is True


def test_page_metric_uses_spans_not_broad_page_range():
    """Chunk 广泛页范围不能代替真实页证据；逻辑页不得冒充物理页。"""
    qa = {"ID": "unit-only", "doc_name": "metadata_stub", "_ohr_reference": {"evidence_pages_1based": [2]}}
    d = exported_document()
    hit = {"doc_id": DOC, "span_pages": [1], "page_start": 1, "page_end": 10}
    scored = tool.score_retrieval(qa, d, [hit])
    assert scored["hit_at10"] is True and scored["page_hit_at10"] is False
    d["chunks"][0]["metadata"]["page_semantics"] = "logical"
    scored = tool.score_retrieval(qa, d, [hit])
    assert scored["page_status"] == "not_applicable" and scored["page_hit_at10"] is None


def test_ranking_metrics_are_document_hits_not_answer_accuracy():
    """第六名仅 Hit@10，MRR@20 正确；答案不被当 Query 或送入 LLM。"""
    qa = {"ID": "unit-only", "doc_name": "metadata_stub", "_ohr_reference": {"evidence_pages_1based": [1]}}
    hits = [{"doc_id": "other-document", "span_pages": [1]} for _ in range(5)] + [{"doc_id": DOC, "span_pages": [1]}]
    scored = tool.score_retrieval(qa, exported_document(), hits)
    assert scored["hit_at1"] is False and scored["hit_at5"] is False and scored["hit_at10"] is True
    assert scored["reciprocal_rank_at20"] == pytest.approx(1 / 6)


def test_question_selection_is_deterministic_and_preserves_original():
    """每文档至少一条且覆盖优先，种子固定，不编辑 Stub 里的原始问答字段。"""
    selected = []
    for index in range(5):
        selected.append({"qas": [{"category": kind, "original": {"ID": f"unit-{index}-{kind}",
            "questions": "不改写", "answers": "不送模型"}} for kind in ("plain_text", "table", "formula", "chart", "reading_order")]})
    before = copy.deepcopy(selected)
    result = tool.pick_questions(selected, 20, 42)
    assert len(result) == 20 and len({row["category"] for item, row in result}) == 5
    assert {id(item) for item, row in result} == {id(item) for item in selected}
    assert tool.pick_questions(selected, 20, 42) == result
    assert before == selected


def test_formal_requirements_remain_pending_for_pdf_images():
    """小样本不放宽十份、多格式/旧 Office 和七类真实标签，不生成正式 manifest。"""
    plan = {"documents": [sample()]}
    result = tool.formal_gaps(plan)
    assert result["status"] == "BLOCKED"
    assert result["missing_legacy_formats"] == ["doc", "ppt", "xls"]
    assert result["missing_to_ten_smoke_hashes"] == 9
    assert len(result["tags_pending"]) == 7


def test_report_preparation_does_not_claim_real_pass(monkeypatch):
    """只生成离线报告时所有真实链路仍 BLOCKED，Omni GT 数不合并官方 QA 数。"""
    captured = {}

    def publish(path, value):
        """捕获 JSON/Markdown，不写真实报告文件。"""
        captured[path.suffix] = value

    def latest(run, kind):
        """表示尚无真实上传、验证或检索快照。"""
        return None

    monkeypatch.setattr(tool, "publish", publish)
    monkeypatch.setattr(tool, "latest_artifact", latest)
    plan = {"documents": [sample()], "plan_sha256": "b" * 64, "preparation_gaps": {},
            "official_qa_count": 20, "omni_gt_count": 3}
    report = tool.generate_report(Path("virtual-run-metadata-only"), plan)
    assert report["small_sample_smoke"] == "BLOCKED"
    assert report["stage16_formal_acceptance"]["status"] == "BLOCKED"
    assert report["official_benchmark_full_evaluation"] == "NOT_APPLICABLE"
    assert report["official_qa_count"] == 20 and report["omni_gt_count_not_qa"] == 3
    assert set(captured) == {".json", ".md"}


def test_plan_tampering_refused_before_business_access(monkeypatch):
    """计划摘要改变即拒绝，无服务连接、不修补计划或自动提取。"""
    def json_file(path, **kwargs):
        """返回摘要不匹配的纯元数据 Stub。"""
        return {"version": tool.VERSION, "documents": [], "plan_sha256": "0" * 64}

    monkeypatch.setattr(tool.common, "json_file", json_file)
    with pytest.raises(tool.SmokeError, match="SMOKE_PLAN"):
        tool.load_plan(tool.TEST_ROOT / "metadata-only")


def test_unknown_error_text_not_logged():
    """错误码提取不把路径、密码或上游异常正文带进报告。"""
    assert tool.safe_code("INDEX_INCOMPLETE：安全文案") == "INDEX_INCOMPLETE"
    assert tool.safe_code("postgresql://password@host/local-path") == "DOCUMENT_FAILED"
    assert tool.safe_code(None) is None


def test_readonly_session_sets_constraints_and_never_commits(monkeypatch):
    """用会话 Stub 检查只读事务/超时，无真实数据库，不 flush/commit。"""
    import sqlalchemy.orm
    from app.db import session as db
    commands = []

    class Session:
        """不提供写入方法的只读会话 Stub。"""

        def __init__(self, engine, **kwargs):
            """验证禁用 autoflush，不创建真实连接。"""
            assert kwargs == {"autoflush": False, "expire_on_commit": False}

        def __enter__(self):
            """返回自身。"""
            return self

        def __exit__(self, *args):
            """模拟退出只读事务，不提交。"""
            return False

        def execute(self, statement):
            """记录编译后的固定 SQL，不执行。"""
            commands.append(str(statement))

    def engine():
        """占位引擎，绝不实际连接。"""
        return object()

    monkeypatch.setattr(db, "get_engine", engine)
    monkeypatch.setattr(sqlalchemy.orm, "Session", Session)
    with tool.readonly_session():
        pass
    assert commands == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                        "SET LOCAL statement_timeout = '10s'", "SET LOCAL lock_timeout = '2s'"]


def test_dense_uses_original_question_and_keeps_tasks_unchanged(monkeypatch):
    """Retriever Stub 只接收原始 question，不用答案，不触发 Parse/文档 Embedding/索引。"""
    from app.core import business_logging, config
    queries, artifacts = [], []
    d = exported_document() | {"file_name": "metadata.pdf", "model_identity": {"stub": "identity"}, "parse_jobs": []}
    export = {"documents": [d]}
    qa = {"ID": "unit-only", "doc_name": "metadata_stub", "questions": " 原始Query\n不修剪 ",
          "answers": "不作为查询输入", "_ohr_reference": {"document_sha256": "a" * 64, "evidence_pages_1based": [1]}}

    class Retriever:
        """替换唯一 Dense 边界，不加载 BGE-M3 或访问真实 SQL。"""

        def __init__(self, kb_id, top_k):
            """固定同 KB 与 Top-K，不创建另一模型。"""
            assert kb_id == KB and top_k == 20

        def retrieve_results(self, query):
            """返回元数据 DTO Stub，保留已有 Chunk 来源字段。"""
            queries.append(query)
            c = d["chunks"][0]
            return [SimpleNamespace(chunk_id=c["chunk_id"], doc_id=DOC, kb_id=KB, text=c["text"],
                page_start=1, page_end=1, section=None, metadata=c["metadata"],
                file_name=d["file_name"], file_type="pdf", embedding_identity=d["model_identity"], retrieval_score=0.7)]

    def verify(run, plan, kb_id):
        """返回内存结构已通过，不创建导出文件。"""
        return copy.deepcopy(export), {"status": "PASS"}

    def snapshot(plan, kb_id):
        """查询后返回完全相同的状态，任务与证据未发生改变。"""
        return copy.deepcopy(export), {"status": "PASS"}

    def references(path):
        """只提供一个明确标注为单元用例的 QA。"""
        return [copy.deepcopy(qa)]

    def save(run, kind, payload):
        """捕获报告，不写素材/报告文件。"""
        artifacts.append((kind, payload))

    def logging(*args, **kwargs):
        """禁用真实写盘日志，只检查检索调用。"""
        return None

    monkeypatch.setitem(sys.modules, "app.services.rag.llama_index.dense_retriever", SimpleNamespace(DenseRetriever=Retriever))
    monkeypatch.setattr(tool, "verify_run", verify)
    monkeypatch.setattr(tool, "export_snapshot", snapshot)
    monkeypatch.setattr(tool, "references_file", references)
    monkeypatch.setattr(tool, "save_artifact", save)
    monkeypatch.setattr(business_logging, "configure_logging", logging)
    monkeypatch.setattr(config, "get_settings", logging)
    result = tool.dense_run(Path("virtual"), {"documents": [sample()]}, KB)
    assert queries == [qa["questions"], qa["questions"]]
    assert result["status"] == "PASS" and result["no_data_or_task_change"] is True
    assert result["hit_at1"] == result["page_hit_at10"] == result["mrr_at20"] == 1
    assert artifacts[0][0] == "dense"
