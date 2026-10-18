# pgvector 入库：范围与用户验收

阶段 15 只消费阶段 14 的完整本地向量，写入已有 document_chunks.embedding（vector(1024)），全部成功后 Document=READY。不实现 Query 检索、BM25、LlamaIndex Adapter、Reranker 或 RAG。不重新解析、下载/加载模型、调用 MinerU/Embedding 云 API；无新增依赖/环境变量。

本节保留入库独立验收；阶段25的[文档级管理](DOCUMENT_MANAGEMENT.md)仅自动投递本节既有任务，索引/向量真相与READY事务语义不变。

## 入口与事务

阶段33补充：用户已确认新增`0009_parse_quality`，未代跑迁移/测试。原Index仍完整校验所有可靠Chunk向量；解析内容COMPLETE提交READY，PARTIAL提交PARTIAL_READY。两类终态同样幂等且可按原知识库/文档/模型/代次检索，PARTIAL不计入正式完整验收。向量失败仍整体回滚，不降低1024维、范数或身份校验。

- POST `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/index`，无请求体；INDEXING 才能首次提交，FAILED 仅恢复已有入库检查点。未完成 Embedding/普通解析失败返回 409，跨知识库返回 404。
- 返回 202、doc_id/kb_id/index_task_id/status。独立 index_task_id/index_config 冻结解析、切片、GPU 代次、输入及选项 Hash、向量文件校验和、实际模型指纹。API 不读取向量或加载模型，提交检查点后投递 default_queue，每文档一个 CPU 任务。
- CPU 先校验完整 manifest/文件，再核对 Chunk 原顺序/文本 Hash、模型名/指纹/编码版本/精度/长度、1024 维有限 L2 单位向量。固定 256 行参数化批量更新原 Chunk，不插入新 Chunk，不改变正文、页码、section 或 metadata，不静默重新归一化。
- 流式入库再次校验文件身份和校验和，避免预检后文件变化。文档及 Chunk 行锁保护代次，全量向量、模型来源摘要和 READY 同一个事务；任一批次/文件/数据库错误全部回滚，再独立保存安全 INDEX_* 原因。旧结果保留但不声称新代次 READY。
- 数据库临时错误最多重试 3 次，退避 5/10/20 秒；文件/输入/完整性失败永久 FAILED，由用户修复后重投 `/index`。不重新推理、不重传云端、不自动补投，不承诺 exactly-once。
- READY 请求返回同一编号、不再次投递，重复旧消息跳过；迟到错误不覆盖 READY。确认丢失可能返回 503 但已完成，先查文档。READY 重复 /chunk、/embed 不重算，入库 FAILED 应恢复 /index 而非 /embed。
- 活动入库拒绝 Reindex/Reparse；READY 可显式维护，换代次失效旧检查点并离开 READY，旧向量仅供诊断，直到解析/缓存回放成功整体替换。历史本地结果文件不自动删除。

READY 完整性由应用写入事务保证，不防御管理员直接 SQL 破坏数据；向量文件和数据库必须限制权限。Hash 不是签名。后续查询必须限定 READY、实际 model_fingerprint 和编码版本，不能把不同权重的 1024 维向量混用；本阶段尚未提供查询服务。

## HNSW 与手动迁移

迁移 0007_pgvector_index 在 0006 之后增加私有入库检查点及 HNSW cosine 部分索引：

```sql
-- 仅说明迁移定义，正常部署通过 Alembic 创建，不重复手动建索引。
CREATE INDEX ix_document_chunks_embedding_hnsw
ON document_chunks USING hnsw (embedding vector_cosine_ops)
WHERE embedding IS NOT NULL;
```

采用 [pgvector 官方 HNSW/cosine 定义](https://github.com/pgvector/pgvector#hnsw)，不需要 IVFFlat 训练数据；vector 索引支持 2000 维以内，满足 1024 维。需要支持 HNSW 的扩展版本（0.5.0 起）；系统软件与扩展升级只能由用户决定和执行。

迁移为普通事务 DDL，会扫描已有非空向量并可能阻塞写入，已有数据较多时安排维护窗口及磁盘/内存预算。应用只校验有效的索引/算子/列/部分条件，不自动创建或修复。部分索引不自行排除 FAILED 文档，后续检索必须显式过滤 READY。小库查询规划器可能选择顺序扫描，不能以未走索引就判断索引不存在。

回滚迁移会删除 HNSW/入库检查点，保留向量和文档状态；回滚前停止相关进程并备份，旧版本不能将其作为阶段 15 已验收状态继续服务。不要在共用库随意降级。

## 自动测试代码（仅用户手动执行）

以下测试只在用户运行时创建临时纯数值结果；不生成原始文档、PDF/Office/图片，不加载真实模型。数据库使用现有隔离 schema/保存点，GPU/Broker Stub，不访问真实云端。

```powershell
conda activate dkf-agent
python -m pytest tests/test_indexing.py tests/test_swagger_docs.py tests/test_business_logging.py -q
python -m pytest tests/indexing_db_smoke.py tests/embedding_db_smoke.py tests/chunk_db_smoke.py tests/knowledge_db_smoke.py -q
# 按资源情况由用户决定是否执行历史完整回归。
python -m pytest tests -q
```

覆盖：严格向量值/维度/范数、文件变更、批次大小和顺序、全量写入与 READY、SQL/批次失败原子性、模型及输入身份、旧消息、重复请求、跨库隔离、投递确认丢失、有限重试、活动维护、READY 维护失效、HNSW 和迁移/模型一致性、日志脱敏及接口契约。编写用例不代表运行通过。

## 真实人工验收：仅使用已有结果

1. 备份数据库与原件/缓存/向量目录，停止 API 和 CPU/GPU Worker。在 pgAdmin 只读确认扩展与 HNSW 能力：

```sql
SELECT extname, extversion FROM pg_extension WHERE extname='vector';
SELECT amname FROM pg_am WHERE amname='hnsw';
SELECT opcname FROM pg_opclass WHERE opcname='vector_cosine_ops';
```

预期三项都有记录。缺失时停止部署，人工核对扩展，不让 Codex 安装/编译/升级。

2. 由用户在 dkf-agent 环境执行迁移；不必重新下载模型或执行 GPU 验证：

```powershell
python -m alembic upgrade head
python -m alembic current
```

当前head预期为0008_document_management，包含原0007索引。启动方式继续用README，CPU仍注册app.tasks.indexing.index_document到default_queue；只验收已有INDEXING文档无需GPU Worker。确认实际冻结EMBEDDINGS_DIR可访问，不用新默认目录替换原结果。

3. 选择已有完整本地结果且 Document=INDEXING 的文档，替换两个 UUID，只提交一次：

```powershell
$indexBase = 'http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID'
$indexRequest = Invoke-RestMethod -Method Post -Uri "$indexBase/index"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$($indexRequest.index_task_id)"
Invoke-RestMethod -Uri $indexBase
```

预期 CPU 一条文档任务，任务结果 ready、dimension=1024；最终 Document=READY、error_message=NULL，独立 index_task_id 与响应一致。INDEXING 文档不会自动补投，202 或 Celery SUCCESS 本身不替代数据库检查。

4. 在 pgAdmin 只读核对全量与模型来源，不输出向量或正文：

```sql
SELECT d.doc_id,d.status,d.error_message,d.index_task_id,
       count(c.chunk_id) AS chunk_count,count(c.embedding) AS vector_count,
       count(*) FILTER (WHERE c.embedding IS NOT NULL AND
           (vector_dims(c.embedding)<>1024 OR abs(vector_norm(c.embedding)-1)>0.0001)) AS invalid_count,
       d.index_config->'result'->'model' AS model_identity
FROM documents d LEFT JOIN document_chunks c ON c.doc_id=d.doc_id AND c.kb_id=d.kb_id
WHERE d.doc_id='文档UUID'::uuid GROUP BY d.doc_id;

SELECT indexname,indexdef FROM pg_indexes
WHERE schemaname=current_schema() AND tablename='document_chunks'
  AND indexname='ix_document_chunks_embedding_hnsw';
```

预期 chunk_count=vector_count>0、invalid_count=0、状态 READY、model.dimension=1024、model.normalized=true、指纹与阶段 14 结果一致；索引定义含 hnsw/vector_cosine_ops 与非空条件。已有页码、source_spans、XLSX 原式不改变。若只有部分向量有值，不接受 READY 验收。

5. 再 POST `/index`，预期同一 index_task_id、READY、无新入库消息或 GPU 推理；重复 /chunk、/embed 同样不重算。日志当日仍一份，index.batch_written 仅执行，index.persisted 才是全部向量及 READY 已提交，不含正文/向量/SQL/路径。

6. 故障、并发、损坏及维护优先用隔离自动用例，不破坏共用向量文件或索引。真实失败检查 INDEX_* 与 Document=FAILED；修复原代次结果后 /index 可恢复，不用 Force Reparse。只在需要重建时显式 Reindex→Chunk→Embed→Index，已有成功 Parse Cache 不增加云提交。

本轮仅实现和编写用例：未执行迁移/pytest/Smoke、没有模型加载、文档解析、MinerU 请求或服务启动。真实 pgvector 版本/HNSW、Windows CPU Worker、批量写入、性能和全部回归待用户手动确认；此前阶段的真实模型/解析待验收项仍保留。验收后等待用户选择下一阶段，不自动开发 15A 或 Retrieval。
