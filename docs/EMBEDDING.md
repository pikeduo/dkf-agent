# BGE-M3 Embedding：范围与人工验收

阶段 14 仅本地 Dense 向量化文档 Chunk 和 Query；/embed 不写 pgvector、不建索引、不实现检索/RAG。文档结果完整发布后进入 INDEXING / awaiting_index，Chunk.embedding 仍为 NULL。随后显式 [阶段 15 /index](PGVECTOR_INDEX.md) 才把完整结果事务入库并推进 READY；本文件仍保留 /embed 的独立验收。

阶段25的[文档级Reindex/Retry](DOCUMENT_MANAGEMENT.md)自动投递既有GPU/入库任务，不改本节冻结参数或推理语义；旧/嵌套单阶段入口及默认上传不变。

## 统一 Service

```python
from app.services.embedding import embed_documents, embed_query

document_vectors = embed_documents(["第一段已有文本", "第二段已有文本"])
query_vector = embed_query("已有资料的问题")
```

上例只能由用户手动运行；首次调用会读取本地模型、加载到配置设备并消耗资源。函数返回按输入顺序对应的 list[list[float]] / list[float]，维度固定 1024、有限非零值、L2 归一化。两条路径同一个 Service/模型/Tokenizer，无 Query 指令前缀，Query 不持久化、不写日志正文，不新增 HTTP Query 接口。

采用环境已声明的 sentence-transformers Dense 实现，无新增依赖；[BGE-M3 官方模型卡](https://huggingface.co/BAAI/bge-m3) 支持此路径，模型维度 1024、最大序列 8192，Query 不需额外指令。加载使用 [SentenceTransformer 官方离线参数](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html) local_files_only=True / trust_remote_code=False，不自动下载或执行仓库代码，不启用 Sparse/ColBERT。

Service 导入/构造与 API 投递不加载 Torch/模型；每进程缓存一个惰性服务，首次编码加载并保留，进程内锁串行化加载/编码。跨进程不共享 CUDA 实例，Query CLI/API 与 GPU Worker 同时各加载一份可能造成显存不足；本阶段 Query 人工验证建议先停止 GPU Worker，CLI 验证完退出后再启动 Worker。CPU 只允许显式配置的直接 Service 对照，不用于文档 GPU 任务，也不自动回退。

配置及模型下载命令见 README。模型目录必须包含官方 config.json、modules.json、Tokenizer、完整权重与 Pooling/Normalize 部件；只下载一个权重文件不够。加载前核对 xlm-roberta/1024 配置并校验部署文件 SHA256，实际模型/Tokenizer/Pooling 文件形成 model_fingerprint；指纹用于识别版本，不证明模型的官方来源，部署者应使用官方仓库并保存下载版本。运行中修改大小/mtime 的文件明确失败，修改模型后停止所有相关进程并重新向量化，不原地热替换；该检测不防御拥有目录写权限者刻意保留时间戳的篡改。

## 长度与资源边界

- EMBEDDING_BATCH_SIZE 默认 16，允许 1～128；EMBEDDING_MAX_LENGTH 默认 8192，允许 8～8192；CUDA 默认 cuda:0、fp16=true。这不是最小显存保证，真实硬件需手动验收。
- 编码前用真实Tokenizer（含特殊token、truncation=False）检查长度。切片估算不作为模型真实长度；新`block-chunk-v3`可在CPU按完整HTML表格行及rowspan关联组切片，兼容表体前后的原表题/注释，见[Chunk](CHUNK.md)。剩余不可拆长行/公式或不支持的表格超限仍报EMBEDDING_TOO_LONG，不截断前缀、不改写原式、不伪造向量。
- 长度失败的`embedding.encode_failed`记录输入序号、真实Token数和限制数量；文档任务再以`embedding.chunk_rejected`关联真实chunk_id、全局1-based chunk_index、doc_id/task_id与`embedding.token_limit`阶段。日志只含受限整数及标识，不含正文/Tokenizer IDs/模型目录。后续management失败保留底层安全错误，不应仅按管理层通用码判断原因。
- 文档只读取已有 Chunk，不重读原件、不调用 Parser/MinerU。每文档一个 gpu_queue 任务，内部依次批处理，每张 GPU 一个 solo/concurrency=1 Worker，不每 Chunk 一个任务，不静默改为 CPU。
- 每次 Service/文档最多 2 万文本、总计 1000 万字符、单文本 100 万字符，单代次向量结果最多 512 MiB；超限明确失败。文档任务流式写数值批次，不一次物化整个文档的全部向量。
- 缺模型、GPU 不可用、推理/显存错误、数量/维度/非有限值/零向量异常为安全永久失败，不自动反复推理或缩小模型输入。临时数据库最多 3 次重试，5/10/20 秒。

## 检查点、状态与结果

迁移 0006_embedding_checkpoint 增加 Document.embedding_task_id/embedding_config，保持原处理/切片标识。POST `/embed` 无请求体，只从 EMBEDDING 或匹配的向量化 FAILED 恢复；INDEXING/READY 已完成请求不重投，入库检查点的 FAILED 应恢复 /index。冻结模型目录/设备/精度/批大小/长度、结果目录及 Chunk/解析代次、顺序/文本 Hash；普通重投不读取新默认值覆盖旧配置。配置信息只保存在私有数据库，不返回 HTTP/消息/日志。

Worker 用文档行锁核对代次/输入，旧任务不能覆盖新 Chunk。结果文件在冻结 EMBEDDINGS_DIR 中：

```text
data/embeddings/<doc_id>/<embedding_task_id>/
├── vectors.jsonl    # 每行 chunk_id、chunk_index、vector；不存正文
└── manifest.json    # 模型指纹、参数/输入 Hash、数量/维度、文件校验和
```

先写临时数值文件，批次全部通过后原子发布文件和 manifest，校验整个结果再事务推进 INDEXING；批次失败不发布完成标记，原 Chunk 不变。文件发布与 SQL 不原子：已验证文件可在 SQL 回滚后同代次恢复，不重复推理；没有 manifest 的中断只可能重算本地向量，不发生云调用。manifest/文件损坏、缺行/乱序/维度错误不能静默当普通 miss 重算。未完成暂存文件和历史代次没有自动清理。

重复消息只跳过，完成状态和业务数据以 Document 为准；Celery 结果可能被重复消息覆盖为 skipped。消息确认丢失时返回安全 503，但已经 INDEXING 的结果不改为 FAILED，应先查状态。没有 Outbox、自动补投或 exactly-once 保证；Worker 执行期间持有文档锁，维护请求可能等待任务结束。

成功重解析/缓存回放/切片替换会在相应事务失效旧 Embedding 检查点，不自动删除历史文件；失败回滚保留原 Chunk。活动 GPU 检查点不允许 Reindex/Reparse 改代次，向量完成后可维护。若因显存或部署参数需改 batch/model_dir/device，停止相关进程、更新配置，再用已有成功 Parse Cache 执行 Reindex→Chunk→Embed 建立新检查点；不为此 Force Reparse、不调用 MinerU。缓存缺失仍按阶段 12 规则失败，不绕过。

旧v1整表、v2带表题/注释但未拆开的Chunk发生EMBEDDING_TOO_LONG时，普通Retry只恢复旧向量输入，不会改用新行策略。待当前任务结束、手动重启CPU/GPU Worker使新代码生效后，使用文档级`POST /api/admin/documents/{doc_id}/reindex`复用成功解析缓存，重建v3 Chunk及本地向量并接续Index；无需MinerU请求，不重建其他文档。原解析质量仍保留，PARTIAL不会因此变成完整READY。单文档恢复示例：

```powershell
$base = "http://127.0.0.1:8000/api/admin/documents/da426abd-541e-45c0-a2b2-7cba49bc9d1a"
Invoke-RestMethod -Method Post -Uri "$base/reindex" -TimeoutSec 120 -ErrorAction Stop
# 202只表示受理；稍后查询到索引终态才表示下游完成。
Invoke-RestMethod -Uri "$base/status" -ErrorAction Stop |
    Select-Object status, error_message, block_count, chunk_count, vector_count, invalid_vector_count, management, parse_quality |
    ConvertTo-Json -Depth 8
```

向量文件与模型目录都需限制访问/备份；Hash 不是签名。入库/检索必须检查实际 model_fingerprint、1024 维、归一化和编码版本，不能只凭维度一致混用不同权重。/embed 本身不完成入库，后续 /index 已接入 HNSW/READY，尚待用户验收；没有检索/RAG。

## 用户手动自动测试

Codex 仅编写以下用例，未执行；不会自动生成文档样本、下载模型或启动 GPU。服务测试使用内存模型/Tokenizer Stub；结果测试只在用户运行时写临时数值文件；DB 使用隔离 schema/外层事务，模型/Broker Stub，不消耗云额度：

```powershell
conda activate dkf-agent
python -m pytest tests/test_embedding_service.py tests/test_embedding_processing.py tests/test_chunk_table_rows.py tests/test_embedding_artifacts.py tests/test_swagger_docs.py tests/test_business_logging.py -q
python -m pytest tests/embedding_db_smoke.py tests/chunk_db_smoke.py tests/knowledge_db_smoke.py -q
python -m pytest tests -q
python -m pytest tests/knowledge_admin_smoke.py tests/document_upload_smoke.py tests/document_processing_smoke.py tests/native_parser_db_smoke.py tests/mineru_db_smoke.py tests/parser_router_smoke.py tests/hybrid_smoke.py tests/parse_cache_smoke.py tests/business_logging_smoke.py tests/batch_upload_smoke.py -q
```

覆盖惰性加载、文档/Query 维度与归一化/同输入一致、批次顺序、真实长度拒绝、离线加载/GPU 不回退、异常脱敏、模型变更、结果原子发布/校验、一个文档一个任务、检查点重投、SQL 回滚复用、迟到消息、输入变更拒绝、维护保护、接口和日志。Mock 通过不证明真实模型或 CUDA 可用，不预报通过数量。

## 用户真实本地模型验收

仅使用已有 Chunk，不生成新文档、不重新解析或调用云端。

1. 手动按 README 准备官方模型、CUDA PyTorch 与配置。停止旧 API/Worker，备份数据库、原件、解析缓存与向量目录，再执行迁移：

```powershell
python -m alembic upgrade head
python -m alembic current
```

当前预期head=0008_document_management（包含原GPU/入库及新管理检查点）。在GPU Worker尚未占用显存时，由用户运行以下最小真实Service验证（只显示数量/维度/范数差异，不打印向量）：

```powershell
python -c "from app.services.embedding import embed_documents, embed_query; import math; text='知识库文档解析'; docs=embed_documents([text,'本地向量化服务']); query=embed_query(text); print('docs=',len(docs),'dims=',[len(v) for v in docs],'query_dim=',len(query),'norm=',math.sqrt(sum(x*x for x in query)),'same_input_diff=',max(abs(a-b) for a,b in zip(docs[0],query)))"
```

预期 docs=2、dims=[1024,1024]、query_dim=1024、范数约 1；同输入因精度/批次可有微小数值差异，不要求逐位相同。模型加载/吞吐/显存未由 Codex 验证。

2. 按 README 重启 API/CPU Worker，并在独立终端启动单进程 GPU Worker：

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

选择已有 EMBEDDING 文档，替换 UUID 后提交一次：

```powershell
$embeddingBase = "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID"
$embeddingRequest = Invoke-RestMethod -Method Post -Uri "$embeddingBase/embed"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$($embeddingRequest.embedding_task_id)"
Invoke-RestMethod -Uri $embeddingBase
```

若为 CHUNKING，先按 Chunk 文档执行 `/chunk` 并等待 EMBEDDING。预期 GPU Worker 仅接收一次 embed_document，任务内部有批次摘要；最终 Document=INDEXING、error_message=NULL、Celery awaiting_index，dimension=1024、chunk_count 等于该文档 Chunk 数。

3. 在 pgAdmin 只读核对（本阶段向量尚不入库）：

```sql
SELECT d.doc_id,d.file_name,d.status,d.error_message,d.processing_task_id,d.chunk_task_id,
       d.embedding_task_id,d.embedding_config->'result' AS embedding_result,
       count(c.chunk_id) AS chunk_count,count(c.embedding) AS pgvector_count
FROM documents d LEFT JOIN document_chunks c ON c.doc_id=d.doc_id
WHERE d.doc_id='文档UUID'::uuid GROUP BY d.doc_id;
```

预期状态 INDEXING，result.chunk_count 与 chunk_count 一致、dimension=1024、pgvector_count=0。这是阶段边界，不是漏写向量。

4. 用户在同环境校验完整向量文件，核对模型指纹/行数/所有向量数值与身份。以下只打印安全摘要，不加载模型、不打印向量：

```powershell
python -c "from uuid import UUID; from sqlalchemy.orm import Session; from app.db.session import get_engine; from app.models import Document; from app.services.embedding.processing import read_chunks; from app.services.embedding.artifacts import ArtifactStore,digest_json; s=Session(get_engine()); d=s.get(Document,UUID('文档UUID')); c=d.embedding_config; rows=read_chunks(s,d); m=ArtifactStore(c['output_dir']).ready(d.doc_id,d.embedding_task_id,c['source_hash'],digest_json(c['options']),[str(r.chunk_id) for r in rows]); print('count=',m['chunk_count'],'dimension=',m['model']['dimension'],'fingerprint=',m['model']['model_fingerprint']); s.close()"
```

指纹应与同部署 Query Service 的 identity.model_fingerprint 相同；检查时避免 GPU Worker 与 CLI 同卡同时加载两个模型。来源追溯继续使用 `/chunks` 和阶段 13 SQL，正文、公式、页码和字符证据不应改变。

5. 再次 POST `/embed` 应返回同一 embedding_task_id，不新增结果代次或推理；重复任务也不产生额外向量行。日志每天仍一份，成功事件在 SQL 提交后，不含 Query/Chunk 正文、向量或本机路径。

6. 故障优先运行 Stub 用例，不破坏共用模型/服务。真实缺模型、CUDA 不可用/显存不足、长表格超过真实 token 限制应明确 FAILED，原 Chunk 保留，无半份完成标记、不假装 READY。修复同一配置可恢复 `/embed`；改变冻结参数按上述 Reindex 流程建立新代次。

当前真实 CUDA/本地模型、FP16 精度、吞吐、显存和全部回归仍待用户执行确认。MinerU/Hybrid/Parse Cache 的既有真实待验收项继续保留，不能以离线用例替代。用户已授权阶段 15 实现，其入库验收独立记录；未经用户确认，不进入阶段 15A。
