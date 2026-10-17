"""阶段33复用上传/管理API及只读观察；不实现Parser、任务主体或另一套索引。"""

from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import UUID

from scripts.stage33_common import ROOT, Stage33Error, digest, plain_path, sha_file, write_json


def cache_route(document):
    """仅读取有界latest身份摘要确认解析路线；文件存在不冒充缓存命中或完整缓存校验。"""
    from app.core.config import get_settings
    from app.services.parse_cache import ParseCache, ReadyRecord, key_for
    try:
        cache = ParseCache(get_settings())
        path = cache.path(str(document.doc_id), "latest.json")
        if not path.is_file():
            return None, False
        record = ReadyRecord.model_validate_json(cache.read_bytes(path, 65536))
        if (record.recipe.get("file_hash") == document.file_hash
                and record.recipe.get("file_type") == document.file_type and key_for(record.recipe) == record.key
                and record.recipe.get("route_strategy") in {"native", "mineru_whole", "hybrid_bundle"}):
            return record.recipe["route_strategy"], True
    except Exception:
        pass
    return None, None


def read_snapshot(kb_id, *, detailed=True):
    """读取整个独立知识库的短一致快照及统计；不截断200份、不导出向量/私有配置。"""
    from sqlalchemy import func, or_, select, text
    from sqlalchemy.orm import Session
    from app.db.seed import DEFAULT_KB_ID
    from app.db.session import get_engine
    from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseJob, KnowledgeBase
    from app.services.document_management import public_management
    from app.services.document_processing import router_failure_codes
    from app.services.mineru.adapter import safe_merge_diagnostic
    from scripts.smoke_stage16_17 import safe_job_code

    kb_id = UUID(str(kb_id))
    if kb_id == DEFAULT_KB_ID:
        raise Stage33Error("DEDICATED_KB_REQUIRED")
    try:
        with Session(get_engine(), autoflush=False, expire_on_commit=False) as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '30s'"))
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            kb = session.get(KnowledgeBase, kb_id)
            if kb is None or kb.status != "ACTIVE":
                raise Stage33Error("ACTIVE_KB_REQUIRED")
            documents = list(session.scalars(select(Document).where(Document.kb_id == kb_id)))
            block_counts = dict(session.execute(select(DocumentBlock.doc_id, func.count()).join(
                Document, Document.doc_id == DocumentBlock.doc_id).where(Document.kb_id == kb_id)
                .group_by(DocumentBlock.doc_id)).all())
            invalid = or_(func.vector_dims(DocumentChunk.embedding) != 1024,
                func.abs(func.vector_norm(DocumentChunk.embedding) - 1) > 1e-4)
            counts = {r[0]: r[1:] for r in session.execute(select(DocumentChunk.doc_id, func.count(),
                func.count(DocumentChunk.embedding), func.count().filter(invalid)).join(Document,
                    Document.doc_id == DocumentChunk.doc_id).where(Document.kb_id == kb_id,
                        DocumentChunk.kb_id == kb_id).group_by(DocumentChunk.doc_id))}
            jobs = list(session.scalars(select(DocumentParseJob).join(Document,
                Document.doc_id == DocumentParseJob.doc_id).where(Document.kb_id == kb_id))) if detailed else []
            by_doc, result = {}, {}
            for job in jobs:
                options = job.parse_options or {}
                parts = options.get("_pdf_split", {}).get("parts")
                units = parts if parts else [{"batch_id": job.batch_id, "data_id": job.data_id,
                    "submission_attempted": job.submission_attempted}]
                failure = options.get("_adapter_failure")
                failure = failure if isinstance(failure, dict) and job.error_code == "MERGE_EVIDENCE" else {}
                summary = {"job_id": str(job.job_id), "task_id": str(job.task_id), "state": job.state,
                    "error_code": safe_job_code(job.error_code),
                    "adapter_diagnostic": safe_merge_diagnostic(failure.get("diagnostic")),
                    "route": (options.get("_route_decision") or options.get("_cache_guard") or {}).get("route_strategy"),
                    "units": [{"ordinal": p.get("ordinal"), "page_start": p.get("page_start"),
                        "page_end": p.get("page_end"), "batch_id": p.get("batch_id"),
                        "data_id": p.get("data_id"), "state": p.get("state"),
                        "submission_attempted": bool(p.get("submission_attempted"))} for p in units]}
                summary["cached_pieces_observed"] = sum(p.get("state") == "done" and p.get("result") is not None
                    and not p.get("batch_id") and not p.get("submission_attempted") for p in (parts or []))
                summary["whole_cache_replay_observed"] = (job.state == "done" and not parts
                    and not job.batch_id and not job.submission_attempted)
                by_doc.setdefault(job.doc_id, []).append(summary)
            for doc in documents:
                if doc.file_hash in result:
                    raise Stage33Error("DUPLICATE_TARGET_HASH")
                chunks, vectors, bad = counts.get(doc.doc_id, (0, 0, 0))
                management = public_management(doc)
                current = [j for j in by_doc.get(doc.doc_id, []) if j["task_id"] == str(doc.processing_task_id)]
                error = next((j["error_code"] for j in current if j["error_code"]), None)
                parser_causes = router_failure_codes(doc.error_message)
                route, present = cache_route(doc) if detailed else (None, None)
                result[doc.file_hash] = {"doc_id": str(doc.doc_id), "kb_id": str(doc.kb_id),
                    "file_hash": doc.file_hash, "file_type": doc.file_type, "file_name": doc.file_name,
                    "status": doc.status, "block_count": block_counts.get(doc.doc_id, 0),
                    "chunk_count": chunks, "vector_count": vectors, "invalid_vector_count": bad,
                    "management": management.model_dump(mode="json") if management else None,
                    "error_code": error or (parser_causes[0] if parser_causes else None)
                        or (management.cause_code or management.error_code if management else None),
                    "parser_reason_codes": parser_causes,
                    "adapter_diagnostic": next((j["adapter_diagnostic"] for j in current if j["adapter_diagnostic"]), {}),
                    "parse_jobs": by_doc.get(doc.doc_id, []),
                    "route_strategy": route or next((j["route"] for j in current if j["route"]), None),
                    "parse_cache_present": present,
                    "parse_cache_hit": True if any(j["whole_cache_replay_observed"] for j in current) else None,
                    "model_identity": (doc.index_config or {}).get("result", {}).get("model"),
                    "index_identity": digest({"index": doc.index_config, "parse": str(doc.processing_task_id),
                        "chunk": str(doc.chunk_task_id), "embed": str(doc.embedding_task_id),
                        "index_task": str(doc.index_task_id)})}
            return result
    except Stage33Error:
        raise
    except Exception:
        raise Stage33Error("DATABASE_SNAPSHOT") from None


def check_scope(data, rows, kb_id, *, ready=False):
    """独立库不得混入额外文档；评测要求全部200份READY及完整向量，不取部分成功子集。"""
    expected = {d["sha256"]: d for d in data["documents"]}
    if ready and data.get("file_issues"):
        raise Stage33Error("FROZEN_FILES_INVALID")
    if set(rows) - set(expected) or ready and set(rows) != set(expected):
        raise Stage33Error("KB_FILE_SET")
    for sha, row in rows.items():
        if (row["kb_id"] != str(kb_id) or row["file_hash"] != sha
                or row["file_type"] != expected[sha]["file_type"]):
            raise Stage33Error("TARGET_IDENTITY")
        if ready and not is_ready(row):
            raise Stage33Error("ALL_200_READY_REQUIRED")
    if ready and (len({digest(row["model_identity"]) for row in rows.values()}) != 1
                  or any(not row.get("model_identity") for row in rows.values())):
        raise Stage33Error("INDEX_MODEL_IDENTITY")


def is_ready(row):
    """READY必须同时具备Block、完整1024维单位向量及结束的已有编排。"""
    management = row.get("management")
    return (row["status"] == "READY" and row["block_count"] > 0
        and row["chunk_count"] == row["vector_count"] > 0 and row["invalid_vector_count"] == 0
        and (management is None or management["state"] == "done"))


def scope_identity(rows):
    """冻结文档集合/代次/向量数量，避免恢复后混算不同索引快照。"""
    return digest({sha: {k: row[k] for k in ("doc_id", "status", "block_count", "chunk_count",
        "vector_count", "invalid_vector_count", "index_identity")} for sha, row in sorted(rows.items())})


class API:
    """调用现有HTTP业务入口；不自动重试任何写操作或切换为内部任务主体。"""

    def __init__(self, base_url, *, client=None):
        """默认只连本机HTTP，其他受信服务要求HTTPS；不读取代理或跟随跳转。"""
        import httpx
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise Stage33Error("API_URL")
        self.client = client or httpx.Client(base_url=base_url.rstrip("/") + "/", trust_env=False,
            follow_redirects=False, timeout=120)

    def upload(self, kb_id, document):
        """一次单文件auto上传，最终Hash由服务端重验；文件流关闭不修改冻结原件。"""
        path = plain_path(ROOT / document["file_path"], ROOT / "data/dataset/stage32_run_01/documents")
        if sha_file(path) != document["sha256"]:
            raise Stage33Error("FILE_CHANGED")
        with path.open("rb") as stream:
            return self._post(f"api/admin/knowledge-bases/{kb_id}/documents",
                files={"file": (path.name, stream)}, data={"parse_mode": "auto"})

    def precheck(self, kb_id):
        """首份上传前只读检查API、Redis、双队列Worker、Token及本地权重目录；不加载模型。"""
        from redis import Redis
        from app.core.config import PROJECT_ROOT, get_settings
        from app.core.celery_app import celery_app
        settings = get_settings()
        try:
            if self.client.get("health").status_code != 200:
                raise Stage33Error("API_NOT_READY")
            response = self.client.get(f"api/admin/knowledge-bases/{kb_id}")
            if response.status_code != 200 or response.json().get("status") != "ACTIVE":
                raise Stage33Error("ACTIVE_KB_REQUIRED")
            with Redis.from_url(settings.redis_url) as redis:
                if not redis.ping():
                    raise Stage33Error("REDIS_NOT_READY")
            queues = celery_app.control.inspect(timeout=3).active_queues() or {}
            if not {"default_queue", "gpu_queue"}.issubset(
                    {q.get("name") for rows in queues.values() for q in rows}):
                raise Stage33Error("WORKERS_NOT_READY")
            if not settings.mineru_api_token.get_secret_value().strip():
                raise Stage33Error("MINERU_TOKEN_REQUIRED")
            folder = settings.embedding_model_dir
            folder = folder if folder.is_absolute() else PROJECT_ROOT / folder
            if not folder.is_dir():
                raise Stage33Error("EMBEDDING_MODEL_NOT_DEPLOYED")
        except Stage33Error:
            raise
        except Exception:
            raise Stage33Error("PRECHECK_FAILED") from None

    def retry(self, doc_id):
        """仅用户显式许可的普通Retry；不发送Force/Reparse或改变解析策略。"""
        return self._post(f"api/admin/documents/{doc_id}/retry")

    def _post(self, path, **kwargs):
        """只返回HTTP分类，不导出供应商正文；确认丢失由只读数据库身份核对。"""
        try:
            response = self.client.post(path, **kwargs)
            if response.status_code not in {200, 201, 202, 409}:
                raise Stage33Error(f"HTTP_{response.status_code}")
            return response.status_code
        except Stage33Error:
            raise
        except Exception:
            raise Stage33Error("API_WRITE_UNCONFIRMED") from None

    def close(self):
        """释放本CLI的HTTP连接，不停止任何服务。"""
        self.client.close()


def import_all(data, run, kb_id, api, *, max_inflight=2, timeout=86400, poll_interval=10,
               retry_failed=False, reader=read_snapshot, clock=monotonic, pause=sleep):
    """全200范围的资源有界调度；先落意图、后只读确认，恢复不重传已有Hash或盲目补投。"""
    state_path = run / "import_checkpoint.json"
    from scripts.stage33_common import read_json
    state = read_json(state_path) if state_path.exists() else {d["file_id"]: {
        "file_id": d["file_id"], "dataset": d["dataset"], "file_path": d["file_path"],
        "sha256": d["sha256"], "file_type": d["file_type"], "execution_status": "NOT_ATTEMPTED"}
        for d in data["documents"]}
    if set(state) != {d["file_id"] for d in data["documents"]}:
        raise Stage33Error("IMPORT_CHECKPOINT")
    deadline = clock() + timeout
    retried = set()
    write_json(state_path, state)
    while clock() < deadline:
        rows = reader(kb_id)
        check_scope(data, rows, kb_id)
        if not (run / "baseline.json").exists():
            initial_cloud = sorted({(u["batch_id"], u["data_id"]) for r in rows.values()
                for j in r.get("parse_jobs", []) for u in j["units"] if u.get("batch_id") and u.get("data_id")})
            write_json(run / "baseline.json", {"confirmed_cloud_files": initial_cloud})
        active = sum(r["status"] not in {"FAILED", "READY"} for r in rows.values())
        changed = False
        for doc in data["documents"]:
            item = state[doc["file_id"]]
            if doc["file_id"] in data.get("file_issues", {}):
                item.update(execution_status="INPUT_FAILED", error_code=data["file_issues"][doc["file_id"]])
                continue
            row = rows.get(doc["sha256"])
            if row:
                item.update(row)
                item["execution_status"] = "READY" if is_ready(row) else (
                    "FAILED" if row["status"] in {"FAILED", "READY"} or (row.get("management") or {}).get("state") == "failed"
                    else "WAITING")
                if row["status"] == "READY" and not is_ready(row):
                    item["error_code"] = "READY_VALIDATION_FAILED"
                if (retry_failed and item["execution_status"] == "FAILED" and doc["file_id"] not in retried
                        and active < max_inflight and not item.get("retry_intent")):
                    retried.add(doc["file_id"])
                    item["retry_intent"] = True
                    write_json(state_path, state)
                    try:
                        api.retry(row["doc_id"])
                        active += 1
                        item["execution_status"] = "WAITING"
                    except Stage33Error as error:
                        item.update(execution_status="RETRY_UNCONFIRMED", error_code=error.code)
                continue
            if item.get("upload_intent"):
                item.update(execution_status="UPLOAD_UNCONFIRMED", error_code="UPLOAD_IDENTITY_UNCONFIRMED")
                continue
            if active >= max_inflight:
                continue
            item["upload_intent"] = True
            write_json(state_path, state)
            try:
                api.upload(kb_id, doc)
                item["execution_status"] = "WAITING_CONFIRMATION"
            except Stage33Error as error:
                item.update(execution_status="UPLOAD_UNCONFIRMED", error_code=error.code)
            # 即使HTTP返回503/超时也可能已提交；确认前保留占位，下一轮按同库Hash观察。
            active += 1
            changed = True
        write_json(state_path, state)
        terminal = {"READY", "FAILED", "UPLOAD_UNCONFIRMED", "RETRY_UNCONFIRMED", "INPUT_FAILED"}
        # 本轮刚发过请求时再观察一轮，HTTP超时但已落库的文档不能被过早判为失联。
        if not changed and all(item["execution_status"] in terminal for item in state.values()):
            break
        if changed:
            print(f"导入已登记：{sum(bool(x.get('upload_intent')) for x in state.values())}/{len(state)}；"
                  f"READY={sum(x['execution_status'] == 'READY' for x in state.values())}")
        pause(min(poll_interval, max(0, deadline - clock())))
    # 所有文件始终保留；超时不是后台取消，也不把未调度文件当作成功。
    for item in state.values():
        if item["execution_status"] not in {"READY", "FAILED", "UPLOAD_UNCONFIRMED", "RETRY_UNCONFIRMED", "INPUT_FAILED"}:
            item["execution_status"] = "TIMEOUT_PENDING"
    write_json(state_path, state)
    return state
