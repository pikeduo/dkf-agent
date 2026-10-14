# 阶段25：Admin文档管理与人工验收

代码与测试用例已编写，未执行迁移、pytest、文件清理、Worker、GPU或MinerU真实请求。以前阶段的待验收事项不自动通过，不进入阶段26。没有新依赖或环境变量。

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

只有管理员显式维护才启动`app.tasks.management.manage_document`。每个CPU短步骤读取本地状态、准备一个既有阶段并提交检查点，然后投递原任务；每文档一个GPU任务，队列/参数/幂等性不变。不直接调用任务主体、Parser或模型，不重新实现处理链；默认上传仍保留此前逐阶段语义，未提前实现动态增量阶段26。

等待用5秒countdown，不阻塞sleep/while轮询、不另查MinerU HTTP；API与管理检查采用NOWAIT文档行锁，原Parser/GPU持锁时409或短任务RETRY。正常编排截止时间冻结为至少600秒及当前MINERU_JOB_TIMEOUT_SECONDS中的较大值，Celery重试另有24小时硬上限。超时不取消后台工作，不保证正在推理的阶段实时可见。

投递意图在发送前提交；确认丢失或进程在提交/投递间中断存在缺口，没有Outbox或exactly-once保证。管理任务不会因为一直等不到结果就盲目重发子任务；用户检查后显式Retry可重投编排/原阶段检查点。重复同操作沿用活动management_task_id，不建立新业务代次。已READY的维护结果若最终编排标记丢失，Retry仅补done，不重新推理。

失败阶段优先恢复Index→Embedding→Chunk→Parse，不把普通解析失败的旧Block当成当前已完成输入。活动云任务沿用job_id/batch_id及upload_complete决定submit/check；终态收费失败没有普通Retry自动新批次路径。失败Reparse的Retry改为普通ingest并复用业务代次，受现有收费历史保护；只有再次显式Force Reparse允许新云请求。

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

预期head为`0008_document_management`。按README重启API/CPU/GPU Worker；CPU应注册管理任务，GPU仍solo/concurrency=1。XLSX Hybrid Reparse依照原README使用CPU solo/concurrency=1，不改变部署约束。

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

确认有新管理/解析代次、最终READY及原证据契约保持；按真实MinerU批次核对费用。无需重新验证LLM问答或扩展阶段26，本轮结束后等待用户反馈。
