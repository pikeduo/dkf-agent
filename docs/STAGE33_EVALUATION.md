# 阶段33：固定200文档与4087条QA的全量检索评测

代码已实现，用户已开始真实导入，尚未全量通过。本轮Codex只读定位已有失败并修复代码，**未执行pytest、Adapter回放、真实上传、数据库写入、GPU或云端评测**。阶段32原件、manifest、3588条OHR及499条OCB官方QA不改写、不重采样。没有前N份/前N题参数；`--max-inflight`只控制在途任务，始终登记全部200份和4087题。仅使用OHR/OCB，不处理OmniDocBench，不调用DeepSeek或其他LLM。

## v4原ZIP回放、质量报告与恢复

MinerU v4先提取可靠内容，再尝试可选严格增强；MERGE_EVIDENCE不再自动等于整份FAILED。新增`0009_parse_quality`需由用户迁移。`PARTIAL_READY`表示可靠块及全部对应向量可用，不计入完整READY；缺页不删除、不伪造Block。当前代码/测试未由Codex运行，25份ZIP实际结果仍待用户执行。

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_content.py tests/test_mineru_content_pipeline.py tests/test_mineru_local_replay.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split_jobs.py tests/test_parse_cache.py tests/test_stage33_pipeline.py -q -ra
$reportPath = "results/stage33_v4_replay_$(Get-Date -Format yyyyMMdd_HHmmss).json"
python scripts/mineru_adapter_replay.py --checkpoint results/stage33/stage33_run_01/import_checkpoint.json --kb-id 158030a1-42d6-42ee-835d-02fdbff23f81 --output $reportPath --timeout 600
Get-Content $reportPath -Raw -Encoding UTF8
```

批量工具登记checkpoint内全部MERGE_EVIDENCE，不限前N份；无当前唯一Job/缺原ZIP/Hash不符/预算耗尽均逐项列出，不假称25份通过。只读DB和本地ZIP、预览原Chunk器数量，不请求MinerU或加载模型，不写业务库/成功缓存。迁移前也可执行该只读工具。单文件命令对冻结v3另加`--allow-normalizer-upgrade`，仅比较、不恢复。

审阅报告后备份数据库/PARSED_DIR、停止API/Worker，手动升级到0009并按README重启。然后只对**一份**离线通过的FAILED文档手动POST原`/api/admin/documents/{doc_id}/retry`，不Force、不加allow-cloud。原Job新增v4本地配方，Batch/原ZIP不变，后续缺片则LOCAL_RESULT_REQUIRED停止。命令见[MinerU本地恢复](MINERU_CLOUD.md#v4基础提取与质量分层)。不得以恢复为由再次上传这200份。

导入checkpoint单列PARTIAL_READY且不再当WAITING/自动重传；`summary.json/md`分别记录ready、partial_ready、degraded_documents，新增`parse_quality_report.json/csv`保留每份覆盖/缺页/固定诊断。原200行/4087题和正式分母不变，仍须全200完整READY才运行正式四策略评测。OHR计分只读可靠`source_spans[].page`，不使用Chunk页范围填补未知页证据。PARTIAL的可检索性不代表正式全量验收通过。

## 失败定位与原知识库恢复（历史严格增强排查）

下列v3结构兼容历史记录仍可查阅；当前基础入库/部分质量以本节v4规则为准，严格子原因只限制增强，不再默认阻断可靠正文。

**更新源码不等于运行进程已更新。** API和Celery Worker会保留已导入模块；执行恢复前必须在原终端停止并重新启动API/CPU Worker，确认PID/启动时间已改变，不能仅启动新版导入CLI。已有进程可用`Get-Process -Id <日志中的PID> | Select-Object Id,StartTime`只读核对。旧API可能把MERGE失败送回普通Parse，再由CACHE_REPARSE_REQUIRED阻止重复收费；这不是新Adapter拒绝，也不是云端新失败。

Retry的HTTP409是明确拒绝，不是上传去重成功；新版CLI保留`retry_http_status/retry_request_error`，结束时逐项显示恢复目标的当前失败原因。末尾“运行器错误=-”仅指脚本未抛全局异常，不代表任何一份已恢复。若此前运行已写`retry_intent=true`，直接重跑同一命令不会再次提交；不要删除检查点或批量清标记。先修正服务版本，核对当前文档/原Job后，通过既有单文档`/retry`显式恢复并观察，结果由后续只读报告接管，不能Force绕过原付费结果保护。

先停止导入CLI继续调度（Ctrl+C不撤销已在Worker执行的任务），保留run/知识库/原ZIP。用户更新代码后按README重启API/CPU Worker；不清Redis/缓存、不删除FAILED文档、不Force。先执行纯Mock：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_stage33_text_compat.py tests/test_mineru_stage33_regressions.py tests/test_mineru_table_merge.py tests/test_mineru_merge.py tests/test_mineru_merge_boundaries.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_recovery.py tests/test_mineru_pdf_split_jobs.py tests/test_document_management.py tests/test_stage33_pipeline.py tests/test_business_logging.py -q -ra
```

本地重放补充用例：`python -m pytest tests/test_mineru_local_replay.py -q -ra`。覆盖空Token/禁止构造Provider、所有已知Adapter错误、缺失/损坏归档、迟到submit、同ZIP多次失败后成功，以及下一片缺结果不得提交。均由用户执行，Codex未运行。跨页断词兼容只保留V1原字符，不通过补字放宽来源校验。

当前382页文档第二片的离线检查（不访问MinerU、不写业务库/成功缓存；报告使用新文件名）：

```powershell
python scripts/mineru_merge_diagnostic.py --job-id 5d59aa4a-64b4-42bd-a359-8e61b2fd5af8 --batch-id 762280d9-3ff4-4f8a-b291-a4b3da2ad5bc --output "results/stage33_part2_local_$(Get-Date -Format yyyyMMdd_HHmmss).json"
```

该分片首个已定位条件为TEXT_CHARACTER：layout在跨页接缝保留英文断词连字符，而V1已省略并续词。兼容修复及真实ZIP通过状态待用户运行确认，不预先宣称本片或全部失败项已通过。此命令自动找本地原ZIP，不需要`--allow-cloud`、下载链接或新Batch。

对每份已有原ZIP做只读离线验证。Whole支持从归档回执自动定位；此命令仅GET数据库只读快照、本地转换/Guard和写新报告，不恢复任务、不调用MinerU：

```powershell
python scripts/mineru_merge_diagnostic.py --job-id 46929a5f-b866-4c90-9ea6-8df70d0e4289 --batch-id 59d163c7-815f-46aa-83a3-7f527e23a98b --output results/stage33_adapter_check_02.json
Get-Content -Encoding UTF8 results/stage33_adapter_check_02.json
```

报告已存在则换新文件名，绝不覆盖。批量诊断当前检查点的合并失败（保留所有200条输入）：

```powershell
$audit = "results/stage33_adapter_check_$(Get-Date -Format yyyyMMdd_HHmmss)"
$checkpoint = Get-Content -Encoding UTF8 results/stage33/stage33_run_01/import_checkpoint.json -Raw | ConvertFrom-Json
foreach ($item in $checkpoint.PSObject.Properties.Value) {
    if ($item.error_code -ne "MERGE_EVIDENCE") { continue }
    foreach ($job in $item.parse_jobs | Where-Object error_code -eq "MERGE_EVIDENCE") {
        $unit = $job.units | Where-Object batch_id | Select-Object -Last 1
        if (-not $unit) { Write-Warning "缺少原Batch：$($item.doc_id)"; continue }
        $zip = @(Get-ChildItem "data/parsed/$($item.doc_id)/mineru_raw/$($job.job_id)" -File -Filter "part-*-$($unit.batch_id).zip" -ErrorAction SilentlyContinue)
        if ($zip.Count -ne 1) { Write-Warning "缺少唯一原ZIP：$($item.doc_id)"; continue }
        python scripts/mineru_merge_diagnostic.py --job-id $job.job_id --batch-id $unit.batch_id --zip $zip[0].FullName --output "$audit/$($item.doc_id).json"
    }
}
```

PARSED_DIR非默认data/parsed时，按配置替换查找根；不复制/改名归档。预期只有`ADAPTER_PASS_NOT_RECOVERED`且Guard=PASS才能考虑恢复；FAIL/BLOCKED仍保留原失败。新拒绝按`_adapter_failure`记录固定子码/源码阶段/局部0-based页/数组1-based位置，stage33 JSON/CSV增加adapter_diagnostic及parser_reason_codes。旧Job没有细诊断时读旧日志或运行上述只读命令，不批量改数据库。

`MANAGEMENT_STAGE_FAILED`原始原因需读Document及Worker日志，不能当作根因。已核对的da426abd-541e-45c0-a2b2-7cba49bc9d1a在创建云Job前被PREFLIGHT_LIMIT拒绝；旧日志未保留具体页/预算种类。用户可重新只读预检冻结原件以取得新诊断，不上传：

```powershell
$item = $checkpoint.PSObject.Properties.Value | Where-Object doc_id -eq "da426abd-541e-45c0-a2b2-7cba49bc9d1a"
python -c "import sys; from pathlib import Path; from app.core.config import get_settings; from app.core.business_logging import configure_logging; from app.parsers.routing.inspector import inspect_pdf; s=get_settings(); configure_logging(s,role='mineru-diagnostic-cli'); r=inspect_pdf(Path(sys.argv[1]),s); print('risks=',r.risks,'uncertain=',r.uncertain)" $item.file_path
```

新事件router.preflight_rejected只记PDF_PAGE_LIMIT/PDF_LINE_LIMIT/PDF_OBJECT_LIMIT及页/计数。不要为了这份文件直接提高预算或删掉绘图检查。

本轮新增严格兼容：原样图表/旁注间隔的唯一布局关联、文字/行内公式多Span的原序核对、跨栏边界二维相交检查，以及已证实为空的页眉原项保留。Normalizer仍为v3：只扩大此前拒绝的结构，已通过输入的V1字符/原位置/ID规则不变；不自动升级旧v2任务或缓存。空边界不算正文/必需页覆盖，也不生成空Chunk。缺失附注/行、未知Span及来源歧义仍失败，**并非所有失败都已修复，新增用例与真实ZIP回放待用户执行**。Whole报告中已进入Adapter再失败会记`adapter_normalize=FAIL`，不会再误记NOT_EXECUTED；Guard独立计状态。

先检查全部离线报告。仅对诊断通过的同BatchJob显式Retry，例如替换为报告中的doc_id：

```powershell
$docId = "填写离线报告通过的doc_id"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/admin/documents/$docId/retry"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/admin/documents/$docId/status"
```

本地Adapter/Guard错误的Retry现已强制离线：后续片也只能复用已有本地结果，缺片报LOCAL_RESULT_REQUIRED并停止，不自动上传/查询/下载。普通单文档`/retry`无须云授权参数，也不应为修Adapter新增云费用。全部Adapter/Guard通过后才继续Chunk/Embedding/Index，不因ZIP存在直接置READY。其他尚未完成的云任务及新文档导入仍有独立费用边界。

若本轮只要恢复原失败项，不要执行一般的全量续传命令。用户确认Mock/离线报告及费用、更新并重启API/CPU Worker后，沿用原run和知识库执行：

```powershell
python scripts/evaluate_stage33.py --step import --run stage33_run_01 --kb-id 158030a1-42d6-42ee-835d-02fdbff23f81 --execute --resume --retry-failed-only --allow-cloud --max-inflight 2 --timeout 604800 --poll-interval 10
```

此模式固定启动时checkpoint中FAILED的范围，对照数据库同Hash、同doc_id，只通过原`/retry`恢复；绝不调用上传接口，不处理那166份未尝试文件，不改READY文档或阶段32数据。所有200条和4087题依然保留在报告，故恢复批次结束仍可能显示INCOMPLETE/退出码1，不代表这批恢复全部失败，请查看逐文档状态。其他已有在途任务照常由Worker推进，且占用并发额度。无checkpoint/身份变更停止；旧`retry_intent=true`不清除、不盲重投，需人工核对上次请求。每次运行每份最多请求一次；超时不取消后台任务。PREFLIGHT_LIMIT未解决可能再次失败；本模式不是跳过预检。本CLI同时覆盖尚未完成的云任务，因此保留--allow-cloud门槛；**只修Adapter请先用上面的离线诊断和单文档Retry，不需要这个云许可参数**。Adapter失败重放已被强制限制为本地，不会据此提交下一片。

修复剩余阻塞、确认全部潜在费用后，沿用原run/原知识库恢复全范围调度（READY只读取接管，不重导入/重建）：

```powershell
python scripts/evaluate_stage33.py --step import --run stage33_run_01 --kb-id 158030a1-42d6-42ee-835d-02fdbff23f81 --execute --resume --allow-cloud --max-inflight 2 --timeout 604800 --poll-interval 10
```

上式不自动重试FAILED；通过的失败项已用单文档Retry恢复后由原checkpoint接管。`--retry-failed`只在用户审核所有失败原因/费用后另加；已有retry_intent的未知提交不能靠删除检查点重试。全部200份READY后才运行下述4087题四策略评测，不把部分成功作为全量完成。

## 复用与输入校验

入口：`python scripts/evaluate_stage33.py`。默认dry-run：只读文件存在性、大小、格式容器、SHA256和冻结元数据，不渲染/解析正文、不连接数据库/API/模型、不写报告。

固定输入为`data/dataset/stage32_run_01/`的两个documents CSV、两个QA JSONL及`qa_file_mapping.csv`。核对200个不同Hash、4087个唯一QA身份、双向依赖、完整原始行摘要、OHR原0-based→1-based页码、OCB原始reference_files/断言/weights/track。单份缺失/Hash/格式问题逐项报告，不把文件及其QA从范围删除；元数据身份/总量/映射错误则阻断执行。不要重新运行阶段32提取脚本。

导入使用**原上传API，parse_mode=auto**，由原管理任务推进Router→Native/MinerU Whole/Hybrid→Block→Chunk→Embedding→pgvector→READY。超限云PDF复用现有连续分片器/短任务/缓存，不重写或直接调用任务主体。原file_id、SHA和官方QA映射在本run绑定实际doc_id；同名不同内容不混用。

`import_knowledge_base.py`的十份/旧Office/七标签标准保持不变。本工具是独立正式评测集导入，不伪造preset标签、不写正式preset manifest、不建第二套业务索引。

## 运行前置条件

- 现有PostgreSQL 17/pgvector、Redis、当前数据库迁移已由用户部署；本工具不安装/迁移/启动服务。
- 独立ACTIVE知识库，仅允许本次200种Hash，不能使用默认库或混入其他文件。评测前必须全部200份READY、Block>0、chunk_count=vector_count>0、无非法向量、模型/代次有效一致。
- 已部署本地BGE-M3和BGE Reranker，Query编码身份须与已入库向量一致。无自动下载、CPU回退或重编码文档。
- 有效MinerU Token/共享Redis限流；用户确认**全部200份文件**的合法使用、外传权限及所有潜在分片费用。公开下载不等于获得云端外传授权。
- 8GB显存：导入时仅一个GPU Worker，batch建议用户在新任务建立前评估1～2；既有冻结参数不能直接改配置后期待生效。评测时停止GPU Worker及其他模型进程，避免另载一份BGE。Reranker仍保留完整原文，超模型长度明确失败，不截断表格凑通过。
- 全量BM25/证据报告需要较多系统内存和磁盘。建议预留至少数GB报告空间及足够缓存/向量磁盘；实际大小和耗时须由真实执行测量，不能给未经测量的完成时间保证。

## 正式操作命令

全部命令由用户确认后手动执行。下列两个步骤覆盖**全200份、全4087题**，没有小样本阶段。

### 1. 服务及目标库

按README确认基础服务已Running，分别在三个PowerShell窗口启动现有入口：

```powershell
conda activate dkf-agent
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -n "cpu@%h" -Q default_queue --pool=solo --concurrency=1
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -n "gpu@%h" -Q gpu_queue --pool=solo --concurrency=1
```

在项目根目录的新窗口创建独立测试库；不要重复创建同名库，重开窗口时从run.json恢复原ID：

```powershell
Set-Location "D:\python project\dkf-agent"
conda activate dkf-agent
$kb = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/admin/knowledge-bases" -ContentType "application/json" -Body '{"name":"stage33-full-01","description":"Frozen OHR and OCB full retrieval evaluation"}'
$kbId = $kb.kb_id
python scripts/evaluate_stage33.py
```

预览应显示200文件/4087题及输入错误数。OHR清单中257/217/382页三份PDF，若所有文件都走云且大小只需按页拆分，则参考**203个文件任务**（OHR103+OCB100）。这不是实际提交数、绝对上限或报价：Native/完整缓存/片缓存可减少调用；实际分片字节、失败及不确定提交影响结果。费用不能仅按父Job数量或重复日志累加。

### 2. 全量导入

确认上述权限/资源/全部文件预算后：

```powershell
python scripts/evaluate_stage33.py --step import --run stage33_run_01 --kb-id $kbId --execute --allow-cloud --max-inflight 2 --timeout 604800 --poll-interval 10
```

首次写入全新`results/stage33/stage33_run_01/`，不覆盖旧run。启动上传前只读检查API、目标库、Redis、双队列Worker、Token、本地Embedding目录。`--timeout`仅是CLI总等待预算，**不会延长已有Job冻结的超时窗口**。超时退出不取消后台任务。

默认每轮最多2个在途文档，其余仍在同一个200份任务清单，所有可调度文件最终都会尝试；一份失败不会被删除或冒充成功。HTTP响应丢失按同库Hash查询接管，不盲目再次上传。重复运行已READY文件沿用原doc_id、Chunk和向量，不调用Reindex/Reparse/Force。

中断后继续相同run/相同KB（先确认原进程已退出）：

```powershell
$kbId = (Get-Content results/stage33/stage33_run_01/run.json -Raw -Encoding UTF8 | ConvertFrom-Json).kb_id
python scripts/evaluate_stage33.py --step import --run stage33_run_01 --kb-id $kbId --execute --allow-cloud --resume --max-inflight 2 --timeout 604800
```

已FAILED不会默认重投。用户定位并修复问题后，可在上述续跑命令额外加`--retry-failed`，只调用既有普通`/retry`，每份最多一次且事先记录意图；不继承Force。提交未知、已有收费失败、损坏缓存或普通Retry仍失败须人工核对平台/安全错误，工具不会新建收费批次来“解决”。`retry_intent`已存在但未确认时不盲目再投，先核对原管理任务/API。输入/索引/模型身份变化不可混入既有评测run，可在新run复用同KB已READY文档，不重新上传。

### 3. 全4087题、四策略评测

先确认`import_report.csv`中200份均READY。停止GPU Worker/其他推理进程，保留PostgreSQL；评测本身不需要API、CPU Worker、Redis或MinerU。进入独立CLI进程加载本地两模型：

```powershell
python scripts/evaluate_stage33.py --step evaluate --run stage33_run_01 --kb-id $kbId --execute --resume
```

每条原始官方question仅执行一次Dense和一次BM25，供四种对照复用：

| 策略 | 固定预算 |
|---|---|
| Dense Only | 全库Top20 |
| BM25 Only | 全库Top20 |
| Hybrid | 原等权RRF(k=60)，两路各20→最多30候选，取前20 |
| Hybrid + BGE Reranker | 同一Hybrid最多30候选→重排Top20，便于Recall@10/MRR@20 |

业务API仍默认重排Top5，不更改生产行为。**QA的相关文件/答案/证据页只用于事后评分，不传为doc_ids过滤、Query扩展或排序参数**。使用独立库的全库范围，不突破Retriever原最多100个显式doc_ids限制。

原BM25生产默认10,000 Chunk/32MiB/4百万词项不变；评测实例显式预算默认100,000 Chunk/256MiB/32百万词项，可通过`--bm25-max-chunks / --bm25-max-mb / --bm25-max-tokens`设置。它们是整库安全上限，不是前N条语料；超限必须失败，绝不截断再宣称全库。最大受控预算为20万Chunk/512MiB/6400万词项，词频索引实际RAM远大于原文大小，需人工评估。

每题有原子结果检查点；再次执行同一评测续跑命令复用已完成题。检索失败题默认保留，用户显式加`--retry-failed`才重新做本地Query（不重编码文档、不调用云LLM）。输入、200份索引代次及实际模型身份变化拒绝混算；每题前后检查快照，不持有长数据库事务。

## 指标口径与分母

- 固定总题数始终4087（OHR3588+OCB499），逐题逐策略显示SUCCESS/FAILED/NOT_EXECUTED。未运行不是零分，不发布伪造指标；索引/模型变化或中断标记NOT_CERTIFIED。
- QA JSONL每题一行，共4087行；QA CSV每题每策略一行，共16348行，是4087×4的执行记录，不是新增题目或评分分母。
- **OHR正式证据指标**：真值单位为官方`(file_id, physical_page)`，页码0-based准确+1；只按返回Chunk真实`source_spans[].page`命中，不靠覆盖很宽的page_start/end猜命中。Recall@5/10为已覆盖真值单位比例，重复Chunk不重复计数；MRR@20为前20内首个真实单位命中的倒数排名。不是凭答案字符串包含判定正确。
- **OCB无官方相关Chunk/证据页标签**：expected_assertions及weights不能转换成相关性标签，正式证据Recall/MRR为null、不可评分数499（以实际输入/校验为准），断言保留manual_pending。
- **另列OHR/OCB文档代理指标**：按官方doc_name/reference_files对应的文件集合计算文档Recall@5/10/MRR@20，OCB多文件依赖保持整组，分母为全部依赖文件，重复Chunk只计一次文件。它仅反映是否找到任务依赖文档，**不是OCB答案证据充分、断言正确或问答准确率**。
- 四策略分别展示总题目、成功执行、可评分总分母、成功评分、失败、未执行、不可评分、manual_pending及成功条件指标。完整执行后，失败题以0进入固定可评分总分母的保守端到端指标，另给仅成功执行的条件指标供诊断，不用后者掩盖失败。未执行题/未完成范围的正式指标保持null。
- 总体证据指标只合并具备同口径官方证据的题目（当前主要OHR）；总体文档代理指标单列。不能把不同相关性单位混合后当作官方全量准确率。耗时含原检索/融合/重排各阶段，复用分支不重复运行但组合耗时按组成阶段累加；首次模型/索引冷启动计入记录。

## 产出及查看

所有报告位于`results/stage33/<run>/`，原始输入只读且受Git忽略保护：

| 文件 | 用途 |
|---|---|
| plan.json / run.json | 全200清单、成本参考、输入Hash及KB绑定 |
| import_checkpoint.json / import_report.json / import_report.csv | 200份逐文件状态、路线/分片Batch、Block/Chunk/向量、管理错误及恢复意图 |
| qa_execution.jsonl / qa_execution.csv | 全4087题原字段/断言/依赖、file_id→doc_id、四路状态与评分资格 |
| retrieval_results.jsonl / retrieval_results.csv | 所有成功检索的Top20原Chunk/Doc/File ID、页/section、原分数及来源；JSONL保留字符来源与Hash，不复制正文 |
| metrics.json / metrics_ohr.json / metrics_ocb.json / metrics_overall.json / strategy_comparison.csv / summary.json / summary.md | OHR/OCB/整体分母、四路指标和耗时 |
| failures_and_unscorable.json / .csv | 失败、未执行、不可评分及待人工审查清单 |
| costs_and_timing.json / baseline.json / timing.json | 去重文件提交数、开始后新观察提交、缓存命中可观测部分及总步骤耗时 |
| cases/ / evaluation_identity.json / evaluation_validity.json | 逐题断点、实际模型/索引身份及快照有效性 |

```powershell
Get-Content results/stage33/stage33_run_01/summary.md -Encoding UTF8
Import-Csv results/stage33/stage33_run_01/import_report.csv | Group-Object execution_status
Import-Csv results/stage33/stage33_run_01/strategy_comparison.csv | Format-Table dataset,strategy,executed_successfully,scorable_denominator,unscorable,recall_at_5,recall_at_10,mrr_at_20
```

本地重新汇总（不连接服务或运行模型）：

```powershell
python scripts/evaluate_stage33.py --step report --run stage33_run_01 --kb-id $kbId --execute --resume
```

费用报告不猜金额：只有Batch/Data ID去重后的文件提交可观测，超时/提交未知单列；原生缓存命中若没有持久化观测就记unknown，不从“没有新Batch”推断。无提交且done的云Job/成功片可证明缓存回放；缓存指针存在只表示存在，不是命中。真实账单仍须用户核对，历史提交与本run新观察次数分开。

## 测试及后续

仅编写Mock，用户手动执行；用例不访问真实冻结数据/数据库/模型/云端：

```powershell
python -m pytest tests/test_stage33_dataset.py tests/test_stage33_pipeline.py tests/test_bm25.py tests/test_bm25_retriever.py tests/test_business_logging.py -q -ra
```

覆盖固定200/4087、依赖与原字段/页码、全范围分批/断点/提交未知、重复导入、隔离与模型、去重Recall及失败分母、OCB不可评分、报告未执行null、原BM25排序兼容及显式预算拒绝截断。测试尚未运行；真实Worker/分片/缓存/超时、GPU长证据、SQL成本和正式指标待用户验收。

`PASS`仅指全范围文档及四策略执行完毕，并非每题召回成功、解析语义完全正确或RAG通过。阶段34可直接读取本run的qa_execution.jsonl、检索JSONL与file_id/doc_id映射，在独立授权后做答案/断言评测；当前不实现或运行阶段34。
