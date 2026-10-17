"""联合冒烟工具的元数据/HTTP/状态 Stub；仅用户执行，不生成真实 PDF/Office/图像。"""

import builtins
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


@pytest.mark.parametrize("failed", [False, True])
def test_managed_upload_only_observes_status_and_never_posts_legacy_stages(monkeypatch, failed):
    """新增编排由服务端接续；工具只查询/status，管理失败如实FAIL，不重复投递阶段。"""
    rows = {"a" * 64: document() | {"management_task_id": TASK}}
    args, reader, clock, _ = pipeline_stub(monkeypatch, rows)

    class ManagedApi(Api):
        """模拟已部署阶段26的状态接口，禁止旧逐阶段写请求。"""

        def request(self, method, path, **kwargs):
            """状态检查返回管理结论；成功时仅模拟后台完成，不执行任务主体。"""
            assert method == "GET"
            if path.endswith("/status"):
                self.calls.append((method, path))
                if not failed:
                    self.rows["a" * 64]["status"] = "READY"
                return Response(200, {"doc_id": DOC, "kb_id": str(KB), "management": {
                    "management_task_id": TASK, "state": "failed" if failed else "running"}})
            return super().request(method, path, **kwargs)

    api = ManagedApi(rows)
    result = tool.drive_pipeline(args, Path("virtual"), {"documents": [sample()]}, api,
        reader=reader, clock=clock.now, pause=clock.pause)
    assert result["status"] == ("FAIL" if failed else "PASS")
    assert any(path.endswith("/status") for _, path in api.calls)
    assert not any(method == "POST" for method, _ in api.calls)


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
    assert tool.safe_code("") is None
    assert tool.safe_code("   ") is None


def test_pdf_split_fee_report_counts_actual_parts_not_parent_job():
    """父Job当前batch不能代表整份费用；片级确认/未知提交分别统计且不输出私有配置。"""
    units = [{"batch_id": "batch-1", "submission_attempted": True},
             {"batch_id": "batch-2", "submission_attempted": True},
             {"batch_id": None, "submission_attempted": True}]
    jobs = [{"batch_id": None, "submission_attempted": True, "pdf_parts": units},
            {"batch_id": "batch-3", "submission_attempted": True},
            {"batch_id": None, "submission_attempted": False}]
    summary = tool.cloud_fee_summary(jobs)
    assert summary == {"confirmed_batch_ids": ["batch-1", "batch-2", "batch-3"],
        "distinct_confirmed_batches": 3, "file_jobs_with_batch": 3, "submission_outcome_unknown": 1}
    assert tool.safe_job_code("PDF_SPLIT_SINGLE_PAGE") == "PDF_SPLIT_SINGLE_PAGE"
    assert tool.safe_job_code("PDF_SPLIT_PRIVATE_SECRET") == "UNKNOWN_ERROR_CODE"


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("   ", None),
    ("EMPTY_BLOCK", "EMPTY_BLOCK"), ("CONTENT_LIST", "CONTENT_LIST"), ("NO_TEXT", "NO_TEXT"),
    ("DATABASE_SCHEMA", "DATABASE_SCHEMA"), ("HTTP_403", "HTTP_403"), ("HTTP_429", "HTTP_429"),
    ("A0202", "A0202"), ("-60018", "-60018"), ("-60001", "-60001"),
    ("https://example.invalid/?token=secret", "UNKNOWN_ERROR_CODE"),
    ("TOKEN_SECRET_VALUE", "UNKNOWN_ERROR_CODE"), ("EMPTY_BLOCK:secret", "UNKNOWN_ERROR_CODE"),
    ("HTTP_999", "UNKNOWN_ERROR_CODE"), (123, "UNKNOWN_ERROR_CODE"),
])
def test_job_error_codes_preserved_or_redacted(value, expected):
    """裸 Job 代码与 Document 文案分开处理；成功空值不产生 DOCUMENT_FAILED，未知输入不泄露。"""
    assert tool.safe_job_code(value) == expected


def test_document_error_uses_current_job_without_old_generation():
    """从当前 Job 还原 EMPTY_BLOCK；历史失败不污染新代次，入库错误仍优先保留。"""
    d = SimpleNamespace(processing_task_id=TASK, error_message="供应商固定安全文案")
    old = SimpleNamespace(task_id="previous-generation", error_code="EMPTY_BLOCK")
    current = SimpleNamespace(task_id=TASK, error_code="EMPTY_BLOCK")
    assert tool.document_error_code(d, [old, current]) == "EMPTY_BLOCK"
    assert tool.document_error_code(d, [old]) == "DOCUMENT_FAILED"
    d.error_message = None
    assert tool.document_error_code(d, [old, SimpleNamespace(task_id=TASK, error_code=None)]) is None
    d.error_message = "INDEX_INCOMPLETE：固定安全原因"
    assert tool.document_error_code(d, [current]) == "INDEX_INCOMPLETE"


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED"])
def test_dense_prerequisite_block_does_not_import_model_or_compute_metrics(monkeypatch, status):
    """前置失败冻结明确的未执行状态；框架/日志/模型不导入，未执行指标为 null 而非0。"""
    saved = []
    export = {"documents": [{"doc_id": DOC, "status": "FAILED"}]}
    original_import = builtins.__import__

    def verify(run, plan, kb_id):
        """仅返回未通过的内存验收快照，不实际连接 PostgreSQL。"""
        return export, {"status": status}

    def guarded_import(name, *args, **kwargs):
        """框架或模型相关导入即失败，防止前置阻塞仍加载资源。"""
        if name.startswith(("app.services.rag.llama_index", "app.services.embedding", "app.core.business_logging", "torch", "sentence_transformers")):
            pytest.fail("前置失败不应导入 Dense/模型/日志")
        return original_import(name, *args, **kwargs)

    def save(run, kind, payload):
        """捕获阻塞摘要，不写真实报告。"""
        saved.append((kind, payload))

    monkeypatch.setattr(tool, "verify_run", verify)
    monkeypatch.setattr(tool, "save_artifact", save)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(tool.SmokeError, match="SMOKE_RETRIEVAL_PREREQUISITE") as error:
        tool.dense_run(Path("virtual"), {"official_qa_count": 20}, KB)
    assert "GPU" not in str(error.value)
    kind, payload = saved[0]
    assert kind == "dense" and payload["status"] == "BLOCKED"
    assert payload["executed"] is False and payload["executed_qa_count"] == 0
    assert payload["qa_count"] == 20 and payload["items"] == []
    assert payload["verification_status"] == status
    assert payload["export_sha256"] == tool.ohr.digest(export)
    assert all(payload[key] is None for key in ("hit_at1", "hit_at5", "hit_at10", "mrr_at20",
        "page_hit_at1", "page_hit_at5", "page_hit_at10", "same_query_ids_stable", "no_data_or_task_change"))


def test_report_handles_unexecuted_null_metrics_and_preserves_failure(monkeypatch):
    """未执行的 Dense 不格式化 null、不展示零命中；三类正式结论不被展示修复改变。"""
    captured, failures = {}, {}
    d = exported_document() | {"status": "FAILED", "parse_jobs": []}
    export = {"documents": [d]}
    verification = {"status": "FAIL", "export_file": "reports/frozen-export.json",
        "export_sha256": tool.ohr.digest(export), "missing_documents": [],
        "validations": [{"file": sample()["file"], "doc_id": DOC, "document_status": "FAILED", "status": "FAIL",
            "failure_stage": "parse", "error_code": "EMPTY_BLOCK", "issues": ["NOT_READY"],
            "block_count": 0, "chunk_count": 0, "vector_count": 0}]}
    dense = {"status": "BLOCKED", "executed": False, "export_sha256": tool.ohr.digest(export),
             "hit_at1": None, "hit_at5": None, "hit_at10": None, "mrr_at20": None}

    def latest(run, kind):
        """全部输入均为内存 Stub，不读取用户原有报告。"""
        return {"verify": verification, "dense": dense, "process": {"status": "FAIL"}}[kind]

    def publish(path, payload):
        """捕获完整输出，不覆盖任何用户文件。"""
        captured[path.suffix] = payload

    def json_file(path, *args, **kwargs):
        """提供冻结导出与失败摘要，不进行文件 IO。"""
        if path.name == "frozen-export.json":
            return export
        return failures[path.name]

    def save(run, kind, payload):
        """捕获失败的恢复类别，只有用户后续验证才可解除，不虚构检索成功。"""
        assert kind == "failure"
        path = run / "reports/99991231T000000-failure.json"
        failures[path.name] = payload
        return path

    monkeypatch.setattr(tool, "latest_artifact", latest)
    monkeypatch.setattr(tool, "publish", publish)
    monkeypatch.setattr(tool, "save_artifact", save)
    monkeypatch.setattr(tool.common, "json_file", json_file)
    plan = {"documents": [sample()], "plan_sha256": "b" * 64, "preparation_gaps": {},
            "official_qa_count": 20, "omni_gt_count": 0}
    report = tool.generate_report(tool.TEST_ROOT / "unit-report-not-created", plan,
                                  error="SMOKE_RETRIEVAL_PREREQUISITE", error_step="retrieve")
    assert report["small_sample_smoke"] == "FAIL" and report["pipeline_status"] == "FAIL"
    assert report["dense_status"] == "BLOCKED" and report["dense_block_reason"] == "VERIFICATION_NOT_PASS"
    assert report["stage16_formal_acceptance"]["status"] == "BLOCKED"
    assert report["official_benchmark_full_evaluation"] == "NOT_APPLICABLE"
    assert failures["99991231T000000-failure.json"]["recovery_kind"] == "verify"
    markdown = captured[".md"].decode("utf-8")
    assert "EMPTY_BLOCK" in markdown and "parse" in markdown and "Dense 未执行" in markdown
    assert "Hit@1=" not in markdown and "GPU 或 Query 模型失败" in markdown


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


def test_merge_and_normalizer_codes_remain_traceable_without_error_body():
    """新合并/冻结版本错误在报告中保留安全代码，不把私有错误原文公开。"""

    assert tool.safe_job_code("MERGE_EVIDENCE") == "MERGE_EVIDENCE"
    assert tool.safe_job_code("CACHE_CONFIG_CHANGED") == "CACHE_CONFIG_CHANGED"
    assert tool.safe_job_code("MERGE_EVIDENCE\nprivate-error-body") == "UNKNOWN_ERROR_CODE"


def test_ocb_metrics_use_document_truth_and_keep_assertions_manual():
    """OCB 多文件 Top-K 和 MRR 仅依据官方依赖；不把断言字符串包含当答案正确。"""
    first = exported_document()
    second = copy.deepcopy(first)
    second.update(doc_id="second-document", file_hash="b" * 64)
    qa = {"reference_id": "unit-ocb", "qa_id": "archive-qa-unit", "source_row_index": 9,
          "reference_files": ["unit.docx", "unit.xlsx"],
          "dependencies": [{"sha256": "a" * 64}, {"sha256": "b" * 64}],
          "question": " 原样问题 ", "track": "Domain", "expected_assertions": ["断言甲", "断言乙"]}
    hits = [{"doc_id": DOC, "span_pages": [1]}] + [{"doc_id": "other"}] * 9 + [{"doc_id": "second-document"}]
    result = tool.score_ocb_retrieval(qa, {DOC: first, "second-document": second}, hits)
    assert result["hit_at1"] is True and result["reciprocal_rank_at20"] == 1
    assert result["all_dependencies_hit_at10"] is False
    assert result["page_status"] == "not_applicable" and result["page_hit_at10"] is None
    assert result["answer_evidence_status"] == "manual_pending"
    assert result["qa_id"] == "archive-qa-unit" and result["source_row_index"] == 9
    assert result["reference_id"] == qa["reference_id"]
    assert result["expected_assertions"] == qa["expected_assertions"] and result["question"] == qa["question"]


def test_unexecuted_queries_are_not_zero_score_metrics():
    """异常/未执行不进入命中分母，空结果的真实已执行查询仍计零分。"""
    failed = {"status": "FAIL", "hit_at1": False, "hit_at5": False, "hit_at10": False,
              "reciprocal_rank_at20": 0.0, "page_status": "not_applicable"}
    blocked = {"status": "BLOCKED", "error_code": "NOT_RUN_AFTER_FAILURE", "page_status": "not_applicable"}
    result = tool.retrieval_metrics([failed, blocked])
    assert result["qa_count"] == 2 and result["measured_qa_count"] == 1
    assert result["hit_at10"] == 0 and result["blocked_qa_count"] == 1
    assert tool.retrieval_metrics([blocked])["hit_at10"] is None


def test_default_now_requests_three_datasets_without_changing_old_plan():
    """新准备默认各二份 Office；旧计划不被增添 QA 或修改冻结摘要。"""
    args = tool.build_parser().parse_args([])
    assert args.pdf_count == 5 and args.image_count == 3 and args.ocb_per_format == 2
    plan = {"documents": [sample()], "official_qa_count": 20, "omni_gt_count": 3, "preparation_gaps": {}}
    before = copy.deepcopy(plan)
    reports = tool.dataset_reports(plan, None, None, {"documents": []})
    assert reports["ocb"]["status"] == "NOT_APPLICABLE" and plan == before


def test_ocb_download_does_not_grant_upload_permission(monkeypatch):
    """仅有真实下载/本地许可不能启动 auto 上传；必须明确全批云外传确认。"""
    def forbidden(*args, **kwargs):
        """权限检查之前不读运行计划、不连业务服务。"""
        pytest.fail("未确认外传不能上传")
    monkeypatch.setattr(tool, "run_lock", forbidden)
    assert tool.main(["--step", "process", "--execute", "--kb-id", str(KB), "--confirm-test-kb"]) == 2


def test_dataset_results_are_separate_and_omni_never_has_qa_metrics():
    """OHR/OCB 指标分别显示，Omni 只报告 GT 链路；断言质量保持 manual_pending。"""
    office = sample() | {"file": "samples/ocb/unit.xlsx", "source": "ocb", "file_type": "xlsx", "sha256": "b" * 64}
    image = sample() | {"file": "samples/omni/unit.png", "source": "omni", "file_type": "png", "sha256": "c" * 64}
    plan = {"documents": [sample(), office, image], "official_qa_count": 20, "ocb_qa_count": 12,
            "omni_gt_count": 3, "preparation_gaps": {}, "datasets_requested": ["ohr", "ocb", "omni"]}
    verification = {"status": "PASS", "export_sha256": "unit-hash",
                    "validations": [{"file": x["file"], "status": "PASS"} for x in plan["documents"]]}
    dense = {"status": "PASS", "export_sha256": "unit-hash", "executed": True,
             "datasets": {"ohr": {"status": "PASS", "hit_at10": 1}, "ocb": {"status": "PASS", "hit_at10": 0.8}}}
    export = {"documents": [{"file_hash": x["sha256"], "status": "READY"} for x in plan["documents"]]}
    reports = tool.dataset_reports(plan, verification, dense, export)
    assert reports["ohr"]["retrieval_metrics"]["hit_at10"] == 1
    assert reports["ocb"]["retrieval_metrics"]["hit_at10"] == 0.8
    assert reports["omni"]["retrieval_status"] == "NOT_APPLICABLE" and reports["omni"]["qa_count"] == 0
    assert reports["ocb"]["quality_status"] == "manual_pending"
    # 新快照或前置失败时不借用旧成功查询报告，避免将旧 OHR 成绩当作 OCB 验收。
    for changed in (verification | {"export_sha256": "new-snapshot"}, verification | {"status": "FAIL"}):
        pending = tool.dataset_reports(plan, changed, dense, export)
        assert pending["ocb"]["retrieval_status"] == "BLOCKED"
        assert pending["ocb"]["retrieval_metrics"] is None


@pytest.mark.parametrize("with_ocb", [False, True])
def test_dense_uses_original_question_and_keeps_tasks_unchanged(monkeypatch, with_ocb):
    """验收入口用注册的检索验收角色，仍复用唯一Retriever/原问题/分组指标，不重编码。"""
    from app.core import business_logging, config
    queries, artifacts, instances = [], [], []
    d = exported_document() | {"file_name": "metadata.pdf", "model_identity": {"stub": "identity"}, "parse_jobs": []}
    export = {"documents": [d]}
    qa = {"ID": "unit-only", "doc_name": "metadata_stub", "questions": " 原始Query\n不修剪 ",
          "answers": "不作为查询输入", "_ohr_reference": {"document_sha256": "a" * 64, "evidence_pages_1based": [1]}}
    office = copy.deepcopy(d)
    office.update(doc_id="f36f44a4-26b8-4daf-9c91-394508f62dcc", file_hash="b" * 64,
                  file_type="docx", file_name="unit.docx")
    office["chunks"][0].update(doc_id=office["doc_id"], chunk_id="fccce4e1-14f4-4051-b9a3-6f688cd7da1b")
    ocb_qa = {"reference_id": "unit-ocb", "reference_files": ["unit.docx"], "track": "File Fidelity Word",
              "dependencies": [{"sha256": "b" * 64}], "question": " 原始OCB问题\n保留 ",
              "expected_assertions": ["不能作为 Query 的断言甲", "不能合并的断言乙"]}
    if with_ocb:
        export["documents"].append(office)

    class Retriever:
        """替换唯一 Dense 边界，不加载 BGE-M3 或访问真实 SQL。"""

        def __init__(self, kb_id, top_k):
            """固定同 KB 与 Top-K，不创建另一模型。"""
            assert kb_id == KB and top_k == 20
            instances.append(self)

        def retrieve_results(self, query):
            """返回元数据 DTO Stub，保留已有 Chunk 来源字段。"""
            queries.append(query)
            expected = office if with_ocb and query == ocb_qa["question"] else d
            c = expected["chunks"][0]
            return [SimpleNamespace(chunk_id=c["chunk_id"], doc_id=expected["doc_id"], kb_id=KB, text=c["text"],
                page_start=1, page_end=1, section=None, metadata=c["metadata"],
                file_name=expected["file_name"], file_type=expected["file_type"],
                embedding_identity=expected["model_identity"], retrieval_score=0.7)]

    def verify(run, plan, kb_id):
        """返回内存结构已通过，不创建导出文件。"""
        return copy.deepcopy(export), {"status": "PASS"}

    def snapshot(plan, kb_id):
        """查询后返回完全相同的状态，任务与证据未发生改变。"""
        return copy.deepcopy(export), {"status": "PASS"}

    def references(path):
        """按 QA 文件返回两个明确标注为单元用例的原问题，不读取 Omni GT 作为问答。"""
        assert path.name in {"official_qa.jsonl", "ocb_qa.jsonl"}
        return [copy.deepcopy(qa if path.name == "official_qa.jsonl" else ocb_qa)]

    def save(run, kind, payload):
        """捕获报告，不写素材/报告文件。"""
        artifacts.append((kind, payload))

    def logging(*args, **kwargs):
        """禁用真实写盘，只在日志配置调用时核对验收角色；配置getter仍返回空Stub。"""
        if kwargs:
            assert kwargs == {"role": "retrieval-acceptance"}
        return None

    monkeypatch.setitem(sys.modules, "app.services.rag.llama_index.dense_retriever", SimpleNamespace(DenseRetriever=Retriever))
    monkeypatch.setattr(tool, "verify_run", verify)
    monkeypatch.setattr(tool, "export_snapshot", snapshot)
    monkeypatch.setattr(tool, "references_file", references)
    monkeypatch.setattr(tool, "save_artifact", save)
    monkeypatch.setattr(business_logging, "configure_logging", logging)
    monkeypatch.setattr(config, "get_settings", logging)
    result = tool.dense_run(Path("virtual"), {"documents": [sample()], "ocb_qa_count": int(with_ocb)}, KB)
    assert queries == [qa["questions"], qa["questions"]] + ([ocb_qa["question"], ocb_qa["question"]] if with_ocb else [])
    assert len(instances) == 1
    assert result["status"] == "PASS" and result["no_data_or_task_change"] is True
    assert result["hit_at1"] == result["page_hit_at10"] == result["mrr_at20"] == 1
    assert result["datasets"]["ohr"]["measured_qa_count"] == 1
    assert result["datasets"]["omni"]["status"] == "NOT_APPLICABLE"
    if with_ocb:
        assert result["datasets"]["ocb"]["measured_qa_count"] == 1
        assert result["datasets"]["ocb"]["hit_at10"] == 1
        assert result["items"][1]["expected_assertions"] == ocb_qa["expected_assertions"]
        assert result["items"][1]["answer_evidence_status"] == "manual_pending"
    assert artifacts[0][0] == "dense"
