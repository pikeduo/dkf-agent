# 文档管理与动态增量入库（阶段25/26）

阶段25/26验收基线保留；阶段33新增MinerU v4质量分层和PARTIAL_READY，本轮只编写代码/Mock，由用户执行迁移及验收，不调用云端。此前阶段未验收事项不自动通过；阶段26补充见[专页](STAGE26_SUPPLEMENT.md)。

## v4质量与部分索引状态

`0009_parse_quality`新增可空Document.parse_quality、Block.provenance及PARTIAL_READY状态约束，需备份、停止API/Worker并手动执行`python -m alembic upgrade head`，不修改历史行。迁移后重启所有进程，不能只更新CLI。

- `/status`与详情返回`parse_quality`，`/blocks`返回`provenance`；质量只包含固定原因、计数及页范围，不含正文/Token/私有路径。
- 结构增强降级但内容完整：原链路可到READY；内容部分可用：原Chunk→Embedding→Index校验全部可靠块向量后到PARTIAL_READY，management仍done/done，但正式全量验收不计为READY。空内容、ZIP/Hash/身份错误仍FAILED。
- PARTIAL_READY重复Retry仅补编排done，不隐式重解析/补收费；显式Reindex回放原质量，不会把部分覆盖改成完整。Dense/BM25/Evidence可读取该状态的可靠块，仍执行原范围、代次、来源及Hash校验。
- 原v3本地失败Job的Retry核对原件/分片/ZIP/Batch后升级本地配方为v4，原云配方和Batch不改；强制本地、不需Token、不访问MinerU。旧Retry换了本地代次时，只接受唯一同Hash原付费Job；歧义、坏原ZIP或缺后续片结果停止，不自动新建Batch。

只读批量回放及单文档恢复的完整PowerShell步骤见[MinerU v4](MINERU_CLOUD.md#v4基础提取与质量分层)。Codex未执行恢复；本轮不覆盖用户run、报告、原ZIP或QA。

## 接口与观察

统一前缀：`/api/admin/documents/{doc_id}`。文档UUID全局唯一，详情包含真实kb_id；目前无鉴权/RBAC，只允许本机/可信内网访问，不能把ID唯一当成权限控制。

| 方法/后缀 | 行为 |
|---|---|
| GET 空后缀 | 原Document信息、各阶段标识、管理状态、最近20个安全Parse Job（含batch/upload_complete/submission_attempted） |
| GET /status | 同一SQL快照的状态及Block/Chunk/向量/非法向量数量，原件/缓存指针存在性 |
| GET /blocks | 原block_index升序、UUID同序裁决；历史空序号置后，不猜阅读顺序 |
| GET /chunks | 原chunk_index升序、UUID同序裁决；原page/section/source_spans不变，不返回或读取向量数组 |
| POST /retry | 恢复现有最下游检查点或同一活动云Job；失败Force不沿用Force |
| POST /reindex | 保留原件，成功Parse Cache→Block→新Chunk→新GPU代次→pgvector→READY |
| POST /reparse?force=true | 新解析代次绕过整份/视觉缓存，再推进到READY；可能增加MinerU费用 |
| DELETE 空后缀 | 202接收异步清理，完成后GET为404；并非收到202就已经删除 |

列表使用`limit=20&offset=0`，limit为1～100。不存在404、非法UUID/参数422、活动状态/检查点冲突409、未明确Force400、数据库/迁移/投递不可用503。状态/详情不依赖Redis查询业务结果，不返回私有配置、磁盘路径、Token或签名URL。正文只由可信管理员Block/Chunk接口显式读取，不进入日志。

`/status`中`chunk_count=vector_count>0`且`invalid_vector_count=0`可观察向量数量、1024维和范数，但READY/文件存在不证明解析正确、模型来源或问答准确。`parse_cache_present`仅指本文档latest指针存在，真实回放仍会校验全部文件/身份/版本，不跳过损坏。

## 编排、Retry与收费边界

迁移`0008_document_management`新增可空`management_task_id/management_config`及成对/唯一约束，历史文档保持空值。私有配置冻结操作、解析代次、解析模式、阶段投递意图、截止时间；不塞进Chunk/Embedding/Index配置或外部Provider参数。原有四阶段的独立任务/检查点仍是业务事实。

新上传及管理员显式维护启动`app.tasks.management.manage_document`。每个CPU短步骤读取本地状态、准备一个既有阶段并提交检查点，然后投递原任务；每文档一个GPU任务，队列/参数/幂等性不变。不直接调用任务主体、Parser或模型，不重新实现处理链；新上传自动接续READY，历史无编排文档及预置导入CLI仍保留原流程。

新上传管理operation为`ingest`，默认auto，管理员native/mineru覆盖不变。普通上传先持久化首阶段发送意图，再用原processing_task_id投递process_document；独立管理UUID只负责接续。Router创建MinerU Job会换解析代次，必须在同事务换绑管理配置的source_parse_task_id与cloud Job，管理UUID不变。显式云新上传在原Job事务建立编排，仍只投递一次原submit/check；缓存命中直接接续Chunk，不请求云端。响应task_id仍是解析代次，不能拿它当作整条链路完成状态。

只处理当前doc_id，既有Index事务保证向量与READY同时提交。新增READY文档自动进入同ACTIVE库/同模型Dense SQL快照；已运行BM25实例按新READY集合重建派生词频，不重建业务Chunk/向量。既有知识库、其他文档和HNSW均不重建，不新增第二套真相表。Cloud失败、下游失败或模型配置问题不伪装READY；管理投递失败保留原件和已有消息，503不意味着文件未上传。

等待用5秒countdown，不阻塞sleep/while轮询、不另查MinerU HTTP；API与管理检查采用NOWAIT文档行锁，原Parser/GPU持锁时409或短任务RETRY。正常编排截止时间冻结为至少600秒及当前MINERU_JOB_TIMEOUT_SECONDS中的较大值，Celery重试另有24小时硬上限。超时不取消后台工作，不保证正在推理的阶段实时可见。

投递意图在发送前提交；确认丢失或进程在提交/投递间中断存在缺口，没有Outbox或exactly-once保证。管理任务不会因为一直等不到结果就盲目重发子任务；用户检查后显式Retry可重投编排/原阶段检查点。重复同操作沿用活动management_task_id，不建立新业务代次。已READY的维护结果若最终编排标记丢失，Retry仅补done，不重新推理。

失败阶段优先恢复Index→Embedding→Chunk→Parse，不把普通解析失败的旧Block当成当前已完成输入。活动云任务沿用job_id/batch_id及upload_complete决定submit/check；当前代次已上传Batch的JOB_TIMEOUT允许普通Retry续看原批次，不视为远端失败。

本地Adapter/Guard错误（如MERGE_EVIDENCE、EMPTY_BLOCK、CONTENT_TYPE、COMPLETENESS）在同Job/Batch/原件Hash/冻结版本及原ZIP可校验时允许Retry，持久标记`_local_replay_only=true`。入口不构造云客户端，服务层也禁止GET/PUT/POST/下载；重复修复后可重放同一原ZIP，不需重新授权云访问。已成功片复用缓存，后续片保留原Batch；缺本地结果返回LOCAL_RESULT_REQUIRED，整份不写部分Block/READY，绝不自动提交尚未取得结果的下一片。缺归档/坏归档或来源不符继续拒绝；新费用只能来自另行明确授权的云处理，不是修Adapter的前提。

其他远端失败、未知POST及坏ZIP终态不因本地重放扩大恢复范围。失败Reparse的Retry改为普通ingest并复用业务代次；只有再次显式Force Reparse允许绕过收费历史，但不得把Force当成Adapter问题的解决方案。

管理员可另行显式调用`/retry?allow_new_split_parts=true`，确认冻结PDF账本中从未提交的剩余片外传与新增费用。当前已收费片仍本地重放；只有没有Batch/提交意图/上传完成/结果的片可续交，不能重投旧Batch。默认false，普通新一次Retry撤销未使用许可；活动任务409，不运行中换许可。不适用于Whole/Office，也不允许因缺旧ZIP再收费。该可选参数不改变原有不带参数的API行为。

MANAGEMENT_STAGE_FAILED保留原外部错误码；若原文档错误具有合法ROUTER_FAILED前缀，cause_code另保留注册Router原因（如PREFLIGHT_LIMIT），不覆盖原文档错误。历史记录不批量回填；阶段33只读快照也能从原有安全前缀恢复原因。新增PDF预检拒绝事件只记资源限制种类、页和计数，不放宽预检预算。

Reindex缓存缺失/损坏/原件Hash/配置/版本改变明确FAILED，不运行Router预检/Parser或MinerU，不自动Force。成功回放保持原Block身份与阅读顺序，再按当前配置建立新切片/GPU/入库检查点。改变Embedding冻结参数后应使用Reindex，不用重复/embed期待新参数生效。

旧`/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process /chunk /embed /index /reindex /reparse`保留兼容语义，其中旧reindex仍只停在CHUNKING。推荐新文档级接口进行完整维护。存在活动管理/删除检查点时旧写入口409，不能旁路重新激活待删文档。

## Delete所有权与失败恢复

1. 行锁下拒绝活动云任务、实际占用的Parser/GPU/入库，以及有未完成检查点的处理阶段；不杀系统进程、不撤销供应商批次或退款。
2. 接收后换解析代次、清除下游任务标识，文档以FAILED/`DOCUMENT_DELETE_PENDING`退出检索；原消息及失败回调无法覆盖新代次。管理状态为running/delete，不能当成普通解析失败重投。
3. CPU清理服务自有`<doc_id>.<file_type>`上传副本、`PARSED_DIR/<doc_id>/`、原件目录`.hybrid/<doc_id>/`、本文档Job指针及当前/旧冻结GPU根下的`<doc_id>/`。不删除`data/knowledge_base/preset`原素材、docs/test资料、模型、日志或全局HNSW索引。
4. 在缓存读取/发布/清理共享的固定`.cache-cleanup.lock`内重新查其他文档相同Hash引用：可确认独占的整份缓存清理，其他文档（包括跨库）使用的结果保留。视觉缓存按视觉内容Hash跨不同原件共享，当前没有完整引用所有权表，保守保留；未知/损坏的全局缓存身份不据猜测删除。任务结果明确`retained_shared_cache_count`和`shared_visual_cache_retained`，不是宣称全部全局缓存已清空。
5. 所有目标先做绝对边界、符号链接/Windows junction及全树预检，再逐项重验删除，不使用递归根目录目标或extractall。存储目录只由受信任进程写入；这不是抵御特权用户恶意热替换文件的沙箱。最多10万路径，异常安全失败。
6. 自有文件清理全部成功后，数据库同事务FK级联删除Document、Blocks、Chunks/向量、Parse Jobs。无新的向量真相表。文件与SQL不原子，权限/磁盘/提交错误可能留下部分已清理文件；Document及删除配置保留，管理标记failed，用Retry或重复DELETE恢复缺失文件可幂等跳过。未完成不能返回deleted。

原件/缓存目录改动前应备份；可清理当前配置根与当前GPU检查点的旧冻结输出根，但更早迁出且已丢失路径记录的历史GPU目录无法凭空发现，需要管理员另外核对，不扫描磁盘找文件。共享视觉缓存GC、取消/Outbox及鉴权不是本轮能力；Redis任务结果沿用原24小时TTL，不批量清Redis或队列。BM25派生快照按当前READY集合重建，已发出的引用不能被删除操作追溯撤回。

固定缓存锁只在本机本地文件系统保证互斥，不支持多主机共享盘。读取/发布期间清理不得插入；锁最多等5秒，冲突/失败为安全缓存I/O失败而不是miss，不会因此自动付费。大缓存读取串行化成本仍待人工确认，运行中不得删除锁文件。

## 用户手动Mock/数据库验收

以下命令仅供用户执行；Codex没有执行。Mock只创建内存元数据及临时存储哨兵/缓存元数据，不生成PDF/Office/图片验收文档，不连接真实业务数据库、运行模型或调用云API：

```powershell
conda activate dkf-agent
python -m pytest tests/test_admin_documents.py tests/test_document_management.py tests/test_document_cleanup.py tests/test_management_task.py tests/test_parse_cache.py tests/test_business_logging.py -q -ra
```

隔离数据库用例需要用户已有PostgreSQL/pgvector与独立schema权限；使用保存点，Broker/Provider/GPU均Stub，不写public或真实导入：

```powershell
python -m pytest tests/document_management_db_smoke.py tests/knowledge_db_smoke.py tests/knowledge_admin_smoke.py tests/parse_cache_smoke.py tests/chunk_db_smoke.py tests/embedding_db_smoke.py tests/indexing_db_smoke.py -q -ra
```

覆盖八个接口、范围/分页/原证据、缓存只读编排、下游恢复、Force不继承、一次阶段投递、旧代次、NOWAIT锁、错误脱敏、截止时间、删除部分失败/恢复/所有权/共享保护、数据库级联及迁移一致性。符号链接测试若用户权限不足skip，不代表通过。Mock不能替代真实文件清理/Worker/数据库/GPU/MinerU验收。

## 最小真实人工验收

仅用已有测试库文档，不自动生成/下载素材。先备份数据库及原件/解析缓存/向量目录，停止API和所有Worker，由用户手动迁移：

```powershell
conda activate dkf-agent
python -m alembic upgrade head
python -m alembic current
```

当前预期head为`0009_parse_quality`。按README重启API/CPU/GPU Worker；CPU应注册管理任务，GPU仍solo/concurrency=1。XLSX Hybrid沿用CPU solo/concurrency=1，不改变部署约束。

选择有成功Parse Cache的已有READY测试文档，替换一次UUID，在PowerShell中：

```powershell
$docId = '实际测试文档UUID'
$docBase = "http://127.0.0.1:8000/api/admin/documents/$docId"
Invoke-RestMethod -Uri $docBase
Invoke-RestMethod -Uri "$docBase/status"
Invoke-RestMethod -Uri "$docBase/blocks?limit=20&offset=0"
Invoke-RestMethod -Uri "$docBase/chunks?limit=20&offset=0"

# 仅重建本地切片/向量/索引，无MinerU新提交；等待期间由用户再次GET/status。
$rebuild = Invoke-RestMethod -Method Post -Uri "$docBase/reindex"
Invoke-RestMethod -Uri "$docBase/status"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$($rebuild.management_task_id)"
```

202后观察UPLOADED/CHUNKING/EMBEDDING/INDEXING至READY；阶段可能快速经过或因原任务行锁不实时可见。预期原件、doc_id、Block原ID/页/阅读顺序不变，同输入/参数Chunk ID稳定；最终chunk_count=vector_count>0且invalid_vector_count=0。GPU/索引检查点为新代次，无新增MinerU Job/batch或费用。失败时查document.error_message、management.error_code/cause_code及parse_jobs，不盲目Force。

确认修复同一阶段配置/队列后才恢复（冻结GPU参数改动需Reindex，不是Retry）：

```powershell
Invoke-RestMethod -Method Post -Uri "$docBase/retry"
Invoke-RestMethod -Uri "$docBase/status"
```

另选一份确认可丢弃的测试文档，先记录其ID/计数，再手动执行DELETE。不要对唯一真实素材直接验收删除：

```powershell
$deleteDocId = '确认可删除的测试文档UUID'
$deleteBase = "http://127.0.0.1:8000/api/admin/documents/$deleteDocId"
$deletion = Invoke-RestMethod -Method Delete -Uri $deleteBase
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$($deletion.management_task_id)"
# 完成后预期404；未完成仍返回删除检查点及安全原因。
Invoke-RestMethod -Uri "$deleteBase/status"
```

在pgAdmin只读确认无该文档/Block/Chunk/向量/Job，其他文档不变，日志只有提交后management.deleted。人工核对自有上传/解析/GPU目录已清理，正式preset和其他文档共享缓存未动。文件占用/权限/路径拒绝优先由Mock验证，不为测试破坏共用文件；真实失败需先修复再Retry，不能当删除完成。

真实Reparse会再次消耗云配额，本轮不默认要求调用；只有另行确认原件可外传、账号额度和新增费用后才执行：

```powershell
Invoke-RestMethod -Method Post -Uri "$docBase/reparse?force=true&parse_mode=auto"
```

确认有新管理/解析代次、最终READY及原证据契约保持；按真实MinerU批次核对费用。无需为了验证管理流程重复调用收费LLM；等待用户反馈后再推进其他阶段。

## 阶段26动态增量人工验收

Codex未执行以下命令、上传、模型或API。现有上传回归夹具隔离新增编排，继续验证历史逐阶段语义；独立增量用例验证真实新调度边界、批量独立性、云换绑、缓存命中、失败及同实例查询可见性，不生成验收文档。

### 1. 用户执行Mock回归

```powershell
conda activate dkf-agent
python -m pytest tests/test_incremental_ingestion.py tests/test_document_management.py tests/test_management_task.py tests/test_admin_documents.py tests/test_smoke_stage16_17.py tests/test_dense_retriever.py tests/test_bm25_retriever.py tests/test_business_logging.py -q -ra
```

已有PostgreSQL/pgvector隔离schema权限时，用户另行执行上传/Router/云幂等回归（Broker/Provider为Mock，不是真实外传）：

```powershell
python -m pytest tests/document_upload_smoke.py tests/document_processing_smoke.py tests/batch_upload_smoke.py tests/parser_router_smoke.py tests/mineru_db_smoke.py -q -ra
```

### 2. 启动现有服务

确认已部署PostgreSQL17/pgvector、Redis、BGE-M3及GPU，当前迁移head为`0009_parse_quality`。先按README备份/停止进程，由用户手动升级；阶段26历史基线保留。重启API和所有Worker，三个独立PowerShell终端分别执行：

```powershell
conda activate dkf-agent
python -m uvicorn app.main:app --reload
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

CPU须注册manage_document及原解析/Chunk/Index/MinerU任务，GPU须注册embed_document。仅CPU可推进至EMBEDDING，GPU缺席不能READY。API搜索还需要现有Reranker部署；8GB显存注意GPU Worker与在线Embedding/Reranker进程共存，沿用README的显存建议，不自动替换冻结参数或降级CPU。

### 3. 新增扫描PDF，仅上传一次

使用已有ACTIVE测试库及一份用户拥有的、允许外传、未在同库上传过的扫描PDF；先确认MinerU Token/Redis/额度、应用大小和200页限制。不要生成样本或为测试反复Force。上传前记录同库已有READY文档的ID、各任务ID、Block/Chunk/向量数量，便于最后对比。

```powershell
$kbId = '实际ACTIVE测试知识库UUID'
$base = 'http://127.0.0.1:8000'
$filePath = '已有且允许外传的扫描PDF绝对路径'
$upload = curl.exe --fail-with-body --noproxy "*" -X POST "$base/api/admin/knowledge-bases/$kbId/documents" -F "file=@$filePath"
# 只有HTTP成功且响应是Document时才继续；503/409先按detail查询已保存记录，不重传。
if ($LASTEXITCODE -ne 0) { throw '上传未确认成功，请检查detail和文档列表，不要直接重传' }
$saved = $upload | ConvertFrom-Json
$docId = $saved.doc_id
$docBase = "$base/api/admin/documents/$docId"
Invoke-RestMethod -Uri "$docBase/status"
Invoke-RestMethod -Uri $docBase
```

无需调用`/chunk /embed /index`或知识库Reindex。由用户稍后重复GET/status，预期UPLOADED→PARSING（云端阶段）→CHUNKING→EMBEDDING→INDEXING→READY；中间阶段可能快速经过或原任务持锁暂不可见。最终`chunk_count=vector_count>0`、`invalid_vector_count=0`，management为done/done；READY提交后管理终态可能最多晚一个检查周期，不改变查询资格。

详情Parse Job应保留一个当前job_id/batch_id，终态done；云JSON或缓存失败不得落半份Block或置READY。日志按doc_id关联upload.stored、parse/router/mineru、management.advanced、chunk/embedding/index.persisted及management.finished，最后index.persisted才证明READY提交。业务摘要不含正文/密钥，不把重复日志次数当费用，按实际批次及平台核对。

### 4. 证据、向量与增量Query

```powershell
Invoke-RestMethod -Uri "$docBase/blocks?limit=20&offset=0"
Invoke-RestMethod -Uri "$docBase/chunks?limit=20&offset=0"
$search = @{ kb_id=$kbId; question='该扫描件中实际存在的问题或关键词'; doc_ids=@($docId); top_k=20 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/api/knowledge/search" -ContentType 'application/json' -Body $search
```

对照原件人工确认OCR、阅读顺序、物理页、bbox、Block/source_spans。单文档搜索Evidence只含新doc_id，保留原Chunk/页/章节；去掉请求的doc_ids再查询同库，应自动纳入新文档，无需重启查询进程或重建KB，但Top5命中效果仍须问题/候选质量对照，不保证所有问题必命中。`/search`不调用LLM，Query仅生成查询向量，不重编码已有文档；问答另依现有明确云许可验收，不把搜索成功等同回答准确。

可在pgAdmin对当前新文档执行只读SQL（替换UUID），不修改状态或向量：

```sql
SELECT d.doc_id, d.kb_id, d.status, count(c.chunk_id) AS chunk_count,
       count(c.embedding) AS vector_count,
       count(c.chunk_id) FILTER (WHERE c.embedding IS NOT NULL AND
         (vector_dims(c.embedding) <> 1024 OR abs(vector_norm(c.embedding) - 1) > 0.0001)) AS invalid_vectors
FROM documents d LEFT JOIN document_chunks c ON c.doc_id=d.doc_id AND c.kb_id=d.kb_id
WHERE d.doc_id='实际新文档UUID'
GROUP BY d.doc_id;
```

最后对照上传前快照，既有文档ID、Block/Chunk ID、数量、解析/切片/GPU/Index代次不变。同库重传同内容应409，仅返回已有doc_id，不生成第二套任务或额外云批次；不要通过跨库重复付费验证幂等。

### 5. 未完成及恢复

FAILED查Document.error_message和Parse Job；管理失败/超时查management.error_code/cause_code。缺GPU、队列或模型部署按原README人工修复；检查点冻结后参数改变要明确Reindex，普通Retry不偷偷替换参数。确认原因已解决后：

```powershell
Invoke-RestMethod -Method Post -Uri "$docBase/retry"
Invoke-RestMethod -Uri "$docBase/status"
```

普通Retry沿用原检查点和活动云Job/batch，不继承Force。收费历史无可用结果、SUBMIT_UNCERTAIN须先人工核对，不反复重传或申请新批次。没有Outbox；发送意图提交后崩溃可能造成等待缺口，显式Retry/管理员核对恢复，不能声称exactly-once。当前有界编排不是无限期后台恢复器，不自动处理历史文档。

联合冒烟脚本会检测management_task_id，新上传仅只读状态，不再争抢旧逐阶段POST；旧无编排计划仍使用原有逐阶段接口，不改计划/报告。阶段26完成标准是上述单文档增量链路及旧文档不变，不等于阶段16完整素材/质量验收或官方基准通过。本轮等待用户实测，不进入27。
