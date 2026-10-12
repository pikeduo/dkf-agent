# Chunk：策略、证据与人工验收

阶段 13 仅把已有完整 DocumentBlock 转为 DocumentChunk；不读原件、不调用 MinerU、不下载 tokenizer、不生成向量或索引。解析流程保持在 CHUNKING，管理员显式 POST `/chunk` 启动 CPU 任务，成功停在 EMBEDDING。历史结果不会自动重建。

## 策略与计量边界

- 按从 1 开始的连续 block_index 阅读；未知历史顺序安全失败，不按页码/bbox/UUID 排序。
- 标题/section 边界优先，不把旧章节的正文重叠带入新章节。后续切片尽量带当前标题（最多估算 100 token）；长标题主内容仍完整保留。
- 普通正文/image_text 优先空行段落，超长段落按字符/token 区间切分。段落拼接用两个换行；source_spans 精确标识哪些字符来自 Block，拼接分隔符不是原文。
- 表格/公式原样整体保留。公式与相邻同章节解释在预算内组成不可拆单元，过长解释不能保证同一 Chunk，仍可通过相邻 source block_index 反查。XLSX 原式不计算、不清洗、不被云计算值覆盖。
- 默认目标 600、重叠 80；允许范围 400～700 / 50～100。重叠只取同章节普通正文尾部，不复用整表/公式；预算不足时可减少/不重叠。末尾/短章节可小于目标。超长结构块不拆、不截断，metadata.oversized=true，后续向量化必须另行处理模型输入限制。
- `unicode-estimate-v1` 为确定性本地估算：中文/非 ASCII 字符逐字，ASCII 词每四字符估一 token，空白不计。不是 bge-m3 精确 tokenizer，也不能保证模型 token 上限；本轮无新依赖/模型部署。计量方式和参数写入 metadata，改变算法应升级 chunker_version。
- 本地保护：源文本最多 1000 万字符、10 万 Block、2 万 Chunk；超限固定 CHUNK_LIMIT，不部分成功。

## 证据与稳定身份

每条 Chunk 保存 kb_id/doc_id/chunk_id/text/page_start/page_end/section/metadata；embedding=NULL。metadata 包含 chunk_index、算法/计量版本、冻结参数、输入块 Hash、file_hash、来源解析代次、估算数量、超目标标记、page_semantics 和 source_spans。

每个 source_span 保存 block_id/block_index/page/block_type/source/bbox、原 Block 文本 Hash，以及原文 `[start_char,end_char)`、Chunk `[chunk_start_char,chunk_end_char)`；偏移为 Python Unicode 字符，不是 UTF-8 字节。role 为 content/context/overlap，重复上下文不是新增原文。page_start/end 覆盖全部实际引用（含重复标题）；PDF 为物理页，图片为第 1 页，Office/文本为已有逻辑页，不发明物理坐标。section 保留源章节，不跨 Sheet 合并。

Chunk UUID5 由 doc_id、kb_id、输入内容/Block 身份/顺序、配置和该切片证据生成，不包含切片/解析 task_id 或时间戳。相同 Block 和参数重试/缓存回放 ID 稳定；输入、版本、参数改变或跨文档重绑定会改变 ID。

## 检查点与失败原子性

迁移 `0005_chunk_checkpoint` 在 Document 保存独立 chunk_task_id/chunk_config；不覆盖 processing_task_id。POST 只允许解析完成的 CHUNKING、持有匹配切片检查点的 FAILED，或已完成的 EMBEDDING/INDEXING/READY；活动云任务拒绝。再次请求沿用同一检查点，可恢复投递中断；已完成不重复投递，不宣称 exactly-once。

Worker 持有文档行锁，检查切片代次和来源解析代次，全部切片生成后全量替换 Chunk 并推进 EMBEDDING，一次事务提交。失败整体回滚，独立事务记录安全原因；数据库临时错误最多 3 次重试，5/10/20 秒。旧代次、已完成状态不能被迟到失败回调覆盖。进程失联没有自动 Outbox/补投，用户查询后显式恢复。

成功重新解析/缓存回放替换 Block 时在同一事务清除旧 Chunk 和切片检查点，避免引用已删除 Block；失败保留旧 Block/Chunk，必须结合 Document 状态辨别旧结果。活动切片检查点未完成时不允许 Reindex/Reparse；切片成功后可 Reindex→缓存 Block→显式 Chunk。改变 CHUNK_SIZE/OVERLAP 不修改正在执行或已完成的冻结参数；用成功 Reindex 建立新的切片检查点，不需要 Force Reparse，不消耗云端。

## 用户手动验收

Codex 本轮仅写代码与用例，未执行以下命令；使用已有解析完成的文档，不生成新样本。执行前备份数据库，在项目根目录激活 dkf-agent，停止旧 Worker，手动迁移并重启 API/CPU Worker（命令见 README）：

```powershell
python -m alembic upgrade head
python -m alembic current
```

当前 head=0007_pgvector_index（包含原 0005 切片与 0006 GPU 检查点迁移），Worker 注册 app.tasks.chunks.chunk_document 到 default_queue。

由用户执行自动用例（隔离数据库 schema 需要项目用户 CREATE 权限和已启用 vector；不操作 public 数据）：

```powershell
python -m pytest tests/test_chunk_splitter.py -q
python -m pytest tests/chunk_db_smoke.py tests/knowledge_db_smoke.py -q
# 用户决定执行既有回归；此命令包括历史文档 Mock/数据库验收，不是 Codex 授权。
python -m pytest tests -q
python -m pytest tests/knowledge_admin_smoke.py tests/document_upload_smoke.py tests/document_processing_smoke.py tests/native_parser_db_smoke.py tests/mineru_db_smoke.py tests/parser_router_smoke.py tests/parse_cache_smoke.py tests/business_logging_smoke.py tests/batch_upload_smoke.py -q
```

尚未执行测试，不预报通过数量。用例覆盖标题/段落/重叠、表格与公式原式、长标题、跨页来源、不丢字符、稳定身份、无序历史拒绝、快照重投、事务回滚、有限重试、投递确认丢失、旧代次、Block 替换失效、接口分页及归属。

对已有 CHUNKING 文档，用自己的 UUID 替换变量，以下请求不重新解析或调用云 API：

```powershell
$chunkKbId = "你的知识库UUID"
$chunkDocId = "你的文档UUID"
$chunkBase = "http://127.0.0.1:8000/api/admin/knowledge-bases/$chunkKbId/documents/$chunkDocId"
$chunkRequest = Invoke-RestMethod -Method Post -Uri "$chunkBase/chunk"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$($chunkRequest.chunk_task_id)"
Invoke-RestMethod -Uri $chunkBase
Invoke-RestMethod -Uri "$chunkBase/chunks?limit=20&offset=0"
```

等待用户确认任务完成，预期文档 EMBEDDING、error_message=NULL、任务 awaiting_embedding、chunk_count>0；GET items 数量/序号符合分页，每条能查 kb/doc/页码及字符来源，表格/公式原式不变。重复 POST 返回同一 chunk_task_id，任务重复投递不增加 Chunk 数量/改变 ID，无新 MinerU batch。日志为每天同一文件中的 chunk.started/persisted，只有提交后成功事件；不含正文/原件路径。

pgAdmin 中执行（替换一次文档 UUID）：

```sql
SELECT d.doc_id, d.file_name, d.status, d.error_message, d.processing_task_id,
       d.chunk_task_id, d.chunk_config, count(c.chunk_id) AS chunk_count,
       count(c.embedding) AS embedding_count
FROM documents d LEFT JOIN document_chunks c ON c.doc_id=d.doc_id
WHERE d.doc_id='你的文档UUID'::uuid GROUP BY d.doc_id;

SELECT c.chunk_id, c.kb_id, c.doc_id, d.file_name, c.page_start, c.page_end,
       c.section, c.metadata->>'chunk_index' AS chunk_index,
       c.metadata->>'oversized' AS oversized, c.text, c.metadata
FROM document_chunks c JOIN documents d ON d.doc_id=c.doc_id AND d.kb_id=c.kb_id
WHERE c.doc_id='你的文档UUID'::uuid
ORDER BY (c.metadata->>'chunk_index')::integer;

-- 对每段字符证据验证 Block 属于该文档，来源文本与 Chunk 对应区间逐字符相同。
SELECT c.chunk_id, s->>'role' AS role, b.block_id, b.block_index, b.page,
       b.block_type, b.source, b.bbox,
       substring(b.text FROM (s->>'start_char')::integer+1
                 FOR (s->>'end_char')::integer-(s->>'start_char')::integer) AS source_text,
       b.block_id IS NOT NULL AND
       substring(b.text FROM (s->>'start_char')::integer+1
                 FOR (s->>'end_char')::integer-(s->>'start_char')::integer) =
       substring(c.text FROM (s->>'chunk_start_char')::integer+1
                 FOR (s->>'chunk_end_char')::integer-(s->>'chunk_start_char')::integer) AS evidence_matches
FROM document_chunks c
CROSS JOIN LATERAL jsonb_array_elements(c.metadata->'source_spans') s
LEFT JOIN document_blocks b ON b.block_id=(s->>'block_id')::uuid AND b.doc_id=c.doc_id
WHERE c.doc_id='你的文档UUID'::uuid
ORDER BY (c.metadata->>'chunk_index')::integer, (s->>'chunk_start_char')::integer;
```

预期 embedding_count=0、所有 evidence_matches=true。挑选已有 PDF、TXT/MD/DOCX、CSV/XLSX 和云结果，人工对照标题、段落、表格、公式、物理/逻辑页；跨页标题上下文会扩大页范围，这是可追溯证据而非正文跨页猜测。历史空 block_index 文档应明确 FAILED/CHUNK_SOURCE_ORDER，不能伪造成功。

可选 Reindex 验收仅用已有成功缓存：记下 chunk_id→POST reindex→等 CHUNKING→POST chunk，预期同 Block/参数下 ID 不变、无新增 MinerU 提交。缓存缺失则按既有规则失败且保留旧结果，不自动 Force Reparse。真实 MinerU、Hybrid/Cache 的既有待验收事项仍由用户手动验证，本轮 Chunk 用例不能代替外部 API 的真实验收；不要为验收切片触发新增云费用。

## 尚未验证与后续边界

阶段 13/14 新增测试仅编写，未运行真实 Worker、迁移、文档处理或模型。切片质量、真实 token 数和检索效果待用户验收。[阶段 14 Embedding](EMBEDDING.md) 已接入独立 GPU 批处理，超模型真实长度明确失败，不截断本节保留的结构块；[阶段 15 入库](PGVECTOR_INDEX.md) 已编写但待人工验收，尚无 Retrieval/RAG。
