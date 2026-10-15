# 阶段26补充：新云批次与跨语言检索对照

## 状态与执行边界

用户已确认阶段26增量主链通过：文档 `fd154f01-6f14-4042-8a6e-a01b228f2d89` 命中已有 MinerU Whole 缓存，30 Block、20 Chunk/向量、READY、ingest/done，新增检索及旧文档不变性已验证。**这不等于新收费批次/缓存未命中已验收。** 阶段16正式验收、公式质量等其他待办不变。

本次只改本地超时的必要恢复边界、补Mock/隔离DB用例和只读检索实验工具。不改上传/RAG协议、模型、表格切片、向量/索引、阶段27公式或费用限流；不增加依赖、环境变量或数据库迁移。没有真实上传、云请求、生产SQL状态修改、模型运行或知识库重建。

## 工作项一：缓存miss与原批次恢复

### 定位与最小修复

既有实现已具备：完整身份缓存/advisory锁、POST前意图、PUT前batch、data_id、异步单次GET、ZIP适配、缓存先发布/SQL原子写块、当前代次及终态幂等；Worker同批次重试及损坏结果/回滚已有用例。此前增量编排用例仅验证云结果接续Chunk，现扩展至原Chunk→Embedding Stub→pgvector→READY→ingest/done。

`check_deadline` 原来在GET之前用 `now-created_at` 判断。Worker停机也计入时间，已在远端完成的Batch可能尚未查询就被标为 `JOB_TIMEOUT`，且普通Retry不恢复这种终态。现在：

- 未提交/未上传：仍有原截止限制，不因恢复而申请第二个Batch。
- 已上传：迟到submit只转check；先对原Batch做一次GET，done仍可回收，remote failed仍按远端失败；未完成且窗口过期才停止本地调度。GET也受既有频控/网络超时约束，不是无限轮询。
- `JOB_TIMEOUT` 指**本地观察停止**，不证明平台失败，不撤销平台任务。
- 管理员显式 `/retry` 可恢复当前解析代次、同Hash、已上传、已有Batch的本地超时Job；沿用job_id/task_id/batch_id/data_id/options_hash，保留created_at/submitted_at及累计retry_count。在私有JSON新增 `_local_wait_started_at` 记录一次新的有界观察窗口，不进入供应商参数、Cache Key或公开协议。
- 活动云Job的过期窗口也仅在显式Retry续期。旧无此字段的Job沿用created_at，不后台扫描/复活历史文档；旧归一化版本检查点仍拒绝。
- 不恢复旧代次、远端明确失败、SUBMIT_UNCERTAIN、未完成上传或损坏ZIP终态；不绕过收费历史保护。不自动Force Reparse、不重置费用身份/重试预算。
- 管理编排自身仍有独立截止，超时不会取消下游；云结果完成后若管理已停止，需要显式Retry接续，不保证跨进程exactly-once。

### 已补测试与执行范围

本轮获准执行纯Mock测试，实际结果见交付回复。新 `test_mineru_recovery.py` 覆盖停机后done回收、pending超时、正常Retry同批次续看、身份缺失/旧状态拒绝、坏观察窗口、网络重试耗尽、损坏ZIP不发布及费用前置只读查询。相邻编排/RRF/BM25/Reranker/Provider/缓存用例一起回归。

本轮最终实际运行13个相关纯Mock测试文件：**330 passed，6.29秒**。pytest默认临时目录受Windows权限限制，改用仓库内全新隔离目录；日志根作为临时目录时又触发Delete路径保护，改用独立上传临时子目录后全部通过。未放宽权限或删除保护，未删除既有目录。

`mineru_db_smoke.py` 的增量用例现覆盖缓存miss、一次POST/PUT、Batch/Data/意图持久化、pending再done、实际Adapter及Parse Cache、原三阶段任务到READY和ingest/done、重复消息不增批次；新增正常Retry API恢复JOB_TIMEOUT用例。模型/HTTP/Broker均Stub；独立schema+外层回滚，不替代真实云/GPU验收。**本轮未执行数据库用例**，不对正在使用的业务库运行测试迁移。

用户自行运行：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_recovery.py tests/test_document_management.py tests/test_incremental_ingestion.py tests/test_management_task.py tests/test_mineru_provider.py tests/test_parse_cache.py -q -ra
# 另行确认已有PostgreSQL、隔离schema权限后执行，不启动任何服务，不请求真实MinerU/GPU。
python -m pytest tests/mineru_db_smoke.py -q -ra
```

### 真实缓存未命中：用户手动，可能计费

先按README准备并启动已有API/CPU/GPU Worker。选一份**真正没有处理过、允许外传的扫描/复杂PDF**，不是仅改名、加空白或修改原件来制造miss。小于应用/平台体积及200页限制；本地预检只核对字节身份，不解析PDF、不判断页数/版权/平台预算。不要用既有READY文件，不删除缓存。

```powershell
Set-Location 'D:\python project\dkf-agent'
conda activate dkf-agent
$pdf = Read-Host '新扫描/复杂PDF绝对路径'
python scripts/mineru_cache_preflight.py --file "$pdf" --check-history
```

只有 `status=ELIGIBLE_SNAPSHOT`、当前Cache Key目录不存在、全库同Hash Document/Job/Batch/不确定提交均为0，才适合作为首次提交样本。否则换另一份真实新文件或先核对原历史，**不删原数据、不Force**。此工具只SELECT统计，不调用会materialize文件的 `ParseCache.document()`，不申请链接或上传。检查只是快照，不是分布式预留；并发任务仍以正式事务锁/云Job为准。平台其他客户端历史不能由本地SQL证明，需另在MinerU平台核对。

新建或选择独立ACTIVE测试库，确保原件可外传及平台额度足够。下面最后一条才会上传并可能新增收费文件任务，**仅由用户确认后执行**：

```powershell
$base = 'http://127.0.0.1:8000'
$kbId = Read-Host '独立ACTIVE测试知识库UUID'
Invoke-RestMethod -Uri "$base/api/admin/knowledge-bases/$kbId"
$consent = Read-Host '确认原件可外传、MinerU额度/费用，输入 CONFIRM-MINERU-COST'
if ($consent -ne 'CONFIRM-MINERU-COST') { throw '未确认费用，禁止上传' }
$uploaded = curl.exe --silent --show-error --fail-with-body -X POST "$base/api/admin/knowledge-bases/$kbId/documents" -F "file=@$pdf" -F 'parse_mode=auto'
if ($LASTEXITCODE -ne 0) { throw '上传响应异常：先查询本库同Hash文件，不自动重传' }
$docId = ($uploaded | ConvertFrom-Json).doc_id
$docBase = "$base/api/admin/documents/$docId"
Invoke-RestMethod -Uri $docBase | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri "$docBase/status" | ConvertTo-Json -Depth 8
```

间隔观察上述两个GET（不重新POST）：应有 `mineru_whole` 路由、cache.miss、一条Job、非空data_id/batch_id、submission_attempted/upload_complete=true、pending/running→done、Document→READY、management ingest/done。瞬态状态可能来不及观察。若auto选Native，此样本不适合云miss验收，记not_applicable，不能谎称已验证云提交。若发生并发缓存命中，也不是本项通过，不删除有效缓存来强行重测。

在pgAdmin执行以下只读SQL，替换 `本次doc_id`。不要查询upload_url、签名结果链接、Token或整份parse_options：

```sql
SELECT doc_id, status, error_message, processing_task_id, chunk_task_id,
       embedding_task_id, index_task_id, management_task_id,
       management_config->>'operation' AS operation,
       management_config->>'stage' AS stage,
       management_config->>'state' AS management_state,
       management_config->>'error_code' AS management_error
FROM documents WHERE doc_id = '本次doc_id';

SELECT job_id, task_id, data_id, batch_id, state, is_active,
       submission_attempted, upload_complete, retry_count, error_code, error_message,
       created_at, submitted_at, finished_at,
       parse_options ? '_local_wait_started_at' AS observation_renewed
FROM document_parse_jobs WHERE doc_id = '本次doc_id' ORDER BY created_at;

SELECT count(*) AS job_count, count(DISTINCT batch_id) AS confirmed_batches
FROM document_parse_jobs WHERE doc_id = '本次doc_id';

SELECT count(*) AS block_count, count(DISTINCT block_index) AS ordered_count,
       min(page) AS first_page, max(page) AS last_page
FROM document_blocks WHERE doc_id = '本次doc_id';

SELECT count(*) AS chunk_count, count(embedding) AS vector_count,
       count(*) FILTER (WHERE embedding IS NOT NULL AND
         (vector_dims(embedding) <> 1024 OR abs(vector_norm(embedding)-1) > 0.0001)) AS invalid_vectors
FROM document_chunks WHERE doc_id = '本次doc_id';
```

预期一个confirmed batch，Block>0且序号完整、chunk_count=vector_count>0、invalid_vectors=0、READY及ingest/done。Parse Cache本地保留原ZIP/content_list/标准化结果；用/status观察指针不等于完整质量验收，页/表格/公式仍需人工对照。费用以平台实际batch/data核对，同Job日志重复出现不能累计成第二个收费文件。不要额外调用收费LLM来验证入库。

#### 停机/超时/异常

- 停机后重启原Worker；已上传旧Batch先GET，远端done可回收，不再POST。管理超时但下游已完成时，查询状态后 `/retry` 仅接续尚未完成阶段或补done。
- 本地 `JOB_TIMEOUT` 且当前Job已有Batch、upload_complete=true：人工在平台核对后可执行以下普通Retry。前后job_id/batch_id/data_id应完全不变，新增confirmed batch=0；该GET/下载可能仍占用平台查询额度，但不会提交新文件任务。
- SUBMIT_UNCERTAIN：不能证明没有费用，先在平台核对，禁止重复上传/Force；没有自动“认领未知Batch”能力。
- 未完成PUT且链接失效、平台Batch已过期/无权限、远端failed、坏ZIP、版本不兼容或网络重试耗尽：保留Job/文件/缓存，记录固定原因并人工处理。当前同批次恢复只放开本地等待超时，不任意复活这些失败；不可通过SQL改状态。若确需新收费任务，另行明确授权后单独处理。

```powershell
# 只在核对失败阶段及原批次后执行；不是/reparse，不带force。
Invoke-RestMethod -Method Post -Uri "$docBase/retry"
Invoke-RestMethod -Uri $docBase | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri "$docBase/status" | ConvertTo-Json -Depth 8
```

未中断的正常新增链预期 `operation=ingest,state=done`；若人工显式Retry建立了新管理代次，沿用既有协议显示 `operation=retry,state=done`，不是第二次解析收费，须比较原Job/Batch/Data ID而非只看管理UUID。

## 工作项二：跨语言检索定位与受控实验

### 已确认与不能推断的结论

- BM25是字面词项：NFKC/casefold、中文单字/二元片段、英文数字/型号完整词；没有翻译/跨语言词典。纯中文词项与纯英文语料可能零交集，这是能力边界，不是索引漏读。语料先读完整合格READY快照，最多10000 Chunk；评分后Top-K截断，不是先截20份语料。原Dense/文档范围/KB隔离不变。
- 原Dense/BM25各Top20，等权RRF按Chunk ID融合，不混加不同尺度分数；最终候选通常30，再全量原文重排Top5。提供的三次结果中，目标在Dense7/8仍被召回；英文BM251增加第二路RRF贡献。**这些数据不能还原Fusion确切名次或证明Reranker单独有bug。**
- Reranker使用原问题+完整Chunk，真实成对token超限报错，不静默截断。当前FP16模型logit先低精度计算再转float，可能存在相等分数；转FP32输出不会恢复丢失精度。也可能是模型本来给同分，未运行同候选/精度对照前不能断言原因。相等logit目前按UUID裁决，保持既有策略，不加任意标题/日期惩罚或业务阈值。
- 长表格 `oversized=true` 是保结构的切片结果；3637是本地估算，不是实际Tokenizer长度。长候选可能有注意力/显存/相关性边界，但不以此直接拆表、截断公式或修改来源。报告保留estimated_token_count/oversized及同分组大小供人工分析。

### 实验策略与安全边界

新增 `scripts/retrieval_quality.py`，**不改变正式HybridRetriever/API默认路径**。用户显式运行后比较：

1. `baseline`：原问题，两路原召回→原问题Reranker。
2. `parallel_recall_original_rerank`：原问题+最多两个人工确认的平行表达，各走同范围Dense/BM25；按稳定Chunk ID做实验级等权RRF合并→原问题重排。
3. `parallel_recall_parallel_rerank`：相同扩展候选池，分别用原/平行表达重排，再按排名RRF合并。另记录**相同baseline候选池**上的原/平行表达重排，隔离候选变化的影响。

不需要付费翻译、网络、额外模型或词典。平行表达不能含答案/新增条件，必须人工确认语义等价；不为本题硬编码翻译规则。本题仅作为用户给定的回归真值初始化，算法不识别其文件/数字/Chunk。实验分和每表达来源留在独立报告，不冒充现有两支FusionProvenance、不写回DB/Node证据；若要正式接入自动扩展，须在通用真实集证明收益、评估额外本地编码/重排开销后另行确认。**目前只能交付可量化实验，尚不能宣称真实跨语言质量已提升。**

每个案例需真实kb_id、可选doc_ids、原question、人工variants、查询/文档语言及targets（chunk_id/doc_id/pages/answer）；页码为项目1-based，命中页还须出现在source_spans，不能仅落在宽泛page_start/end中。answer只保存在本地真值集中，不用于召回/重排或字符串包含验真。无答案案例以no_answer=true、targets=[]表示：不纳入Recall/MRR，记not_applicable，不能把检索候选存在当作模型可答或RAG拒答成功。

报告含每语言方向Recall@5/Hit@1/MRR、正确Chunk、每表达Dense/BM25/Fusion/Rerank排名、同候选对照、同分/长表格摘要、分阶段及总耗时；只导出标识/分数/数量，不导出正文、问题、答案或私有路径。工具固定同kb/doc范围，跨查询证据冲突/越界全批阻塞，失败保留安全错误码。双语表达去重、有界候选、独占创建输出，默认dry-run。并发维护会改变SQL快照，因此应在测试期间暂停对目标文档的维护操作，而非加生产长锁。

### 用户手动真实对比（只读、无云费用，但占用GPU）

使用已有READY/模型身份匹配文档。不要重新解析/向量化。先按README停止同GPU Worker，避免两套进程占满8GB显存；查询进程仍需同时驻留BGE-M3和Reranker，长表格/多个表达可能耗时或OOM，失败记blocked，不自动降精度/CPU/截断。

```powershell
conda activate dkf-agent
python scripts/retrieval_quality.py --init-suite
code docs/test/stage26_quality/suite.json
# 核对用户给定案例，并补齐中文→中文、英文→中文、不同统计数值及无答案的真实案例。
# 文件已存在时不会覆盖，请继续编辑或用--suite指定新版本。
python scripts/retrieval_quality.py --suite docs/test/stage26_quality/suite.json
# 显式本地模型/只读SQL，不请求MinerU/DeepSeek，不写业务库。
python scripts/retrieval_quality.py --suite docs/test/stage26_quality/suite.json --execute --output docs/test/stage26_quality/report-01.json
code docs/test/stage26_quality/report-01.json
```

不在项目根启动时使用脚本绝对路径；脚本内部数据路径按项目根计算。`code`不可用改用notepad。重复运行换新output文件名，不覆盖旧结果。默认branch_top_k=20、candidate_limit=30；另用新报告追加`--branch-top-k 40 --candidate-limit 60`可评估候选截断/成本，不自动采用该预算。

至少人工核对四个语言方向、表格数值、相似统计干扰、无答案/零匹配、同库指定文档及跨库范围；真实targets不能使用单测中的合成UUID。初始化仅有用户提供的三次案例，不覆盖全部真实通用条件。Mock里的六组通用双语/表格词项、负向范围与无答案检查，只证明词项扩展及评测算法可用，**不是正式模型指标**。真实改进/退化必须分语言方向与原问题逐项比较；不因一个样本提升自动启用新生产策略，不把检索Hit当问答准确率。

```powershell
python -m pytest tests/test_retrieval_quality.py tests/test_bm25.py tests/test_hybrid_retriever.py tests/test_reranker_adapter.py tests/test_dense_sql.py -q -ra
```
