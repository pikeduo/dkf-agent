# 阶段33：固定200文档与4087条QA的全量检索评测

代码已实现，用户已开始真实导入，尚未全量通过。本轮Codex只读定位已有失败并修复代码，**未执行pytest、Adapter回放、真实上传、数据库写入、GPU或云端评测**。阶段32原件、manifest、3588条OHR及499条OCB官方QA不改写、不重采样。没有前N份/前N题参数；`--max-inflight`只控制在途任务，始终登记全部200份和4087题。仅使用OHR/OCB，不处理OmniDocBench，不调用DeepSeek或其他LLM。

## 11:52单文档大HTML表回退巨片与v7修复（待人工验证）

`082ebed3-a4e9-4b61-934b-4025b472192e`的原Job为`410b876c-d66a-4ed3-ae92-e969ffc929d4`、Batch为`17b673c7-d9d1-4d21-8f97-377dfaea2d8c`。日志确认v6已运行，12块/36009492字符生成5614片；其中三张HTML表仍因旧TableHTML整表预算回退为巨片，最终在11:52:40报EMBEDDING_LIMIT，不是旧Worker或MinerU/Adapter失败。

只读SQL和日志共同定位：三个云表分别76564/73266/58392行，均超过2万行；第三张467136个单元格也超过40万。第5610/5612/5614片为5770985/5520582/15942556字符，均超过Service单片100万字符门槛。前2804批/5608片已编码但未发布完整manifest，因此vector_count=0；不能把失败临时结果当作可接续复用的向量。

本轮新增v7两遍HTML行扫描，保留矩形、有限标签/属性及rowspan传递组，先完整验证再yield，不构建全表DOM/展开网格，也不通过调大预算解决。旧v1～v6配方冻结，v6/v7后续均流式读取/分批向量/事务索引。首次全量输入Hash还会预检巨片，记录精确Chunk及字符数，先于GPU加载。Block/来源/页码/parse_quality及真实8192Token保护不变。无新依赖、环境变量或迁移；本轮未运行测试或恢复。预计新Chunk数及GPU/磁盘耗时增加，不预称能READY。

由用户先执行以下定向回归（Codex未执行）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_chunk_html_streaming.py tests/test_streaming_pipeline.py tests/test_chunk_splitter.py tests/test_chunk_table_rows.py tests/test_embedding_service.py tests/test_embedding_processing.py tests/test_embedding_artifacts.py tests/test_indexing.py tests/test_document_management.py tests/test_stage33_office_recovery.py tests/test_mineru_remote_status.py tests/test_business_logging.py -q -ra
```

通过后暂停新投递，先检查队列，确认相关任务安全结束再正常停止并按README重启API/所有CPU/GPU Worker。inspect未响应不能当作空队列；solo忙时须结合其终端日志等待本次任务结束。仅对这一份执行一次Reindex，原缓存身份/Hash仍验证；普通Retry继续旧v6不会启用v7。缓存缺失停止，不Force或重新提交云批次：

```powershell
$base = 'http://127.0.0.1:8000/api/admin/documents/082ebed3-a4e9-4b61-934b-4025b472192e'
python -m celery -A app.core.celery_app:celery_app inspect active
python -m celery -A app.core.celery_app:celery_app inspect reserved
python -m celery -A app.core.celery_app:celery_app inspect scheduled
# 确认安全结束并手动重启进程后，POST一次：
Invoke-RestMethod -Method Post "$base/reindex" -TimeoutSec 120 -ErrorAction Stop
# 受理后只GET，不重复POST；大文档可能持续运行较长时间。
Invoke-RestMethod "$base/status" -TimeoutSec 30 -ErrorAction Stop |
    Select-Object status,block_count,chunk_count,vector_count,invalid_vector_count,error_message,management |
    ConvertTo-Json -Depth 8
# 新切片生成后，仅展示版本，不打印正文或完整证据。
$sample = Invoke-RestMethod "$base/chunks?limit=1&offset=0" -ErrorAction Stop
$sample.items[0].metadata | Select-Object chunker_version
```

验收需实际新Chunk为`block-chunk-v7`、`chunk_count=vector_count>0`、无无效向量、management=done，终态由真实原质量决定READY/PARTIAL_READY。原12块不应丢失。若仍失败，保留`embedding.chunk_resource_rejected`的Chunk ID/计数或`embedding.chunk_rejected`的真实Token诊断，只读核查，不连续重投。不能把代码/Stub通过当作业务恢复，不批量Reindex其他文档或进入34。

## 三份原云失败原因已确认：Office转换后超过200页

用户已私有导出第一份原Batch的完整固定原因；三份此前78字符的SHA256完全相同，均为超过200页。对应`8a3496d6…`（PPTX）、`81f211b8…`及`c8e4ca42…`（DOCX）。这不是Adapter故障或原件损坏，也不是HTTP查询失败。固定完整短语现映射为本地安全类别PAGE_COUNT_LIMIT及-60006；明确是消息分类，不冒称响应包含数字码，未知长消息仍只留指纹。

不再反复GET相同Batch或要求平台补原因。原远端failed保持终态，普通Retry不能使超页原件通过；没有成功原ZIP可供本地回放。现有生产只支持PDF分片，不能把Office直接套入PDF账本。三份尚未恢复；后续需要明确授权并实现/验收保留原件Hash、转换来源和逻辑页映射的Office转换/分片流程，以及另行确认新增云外传/费用。本轮不转换、不新提交、不Force，也不改冻结200原件/4087QA。

01:20历史快照仍为116 READY、80 PARTIAL_READY、4 FAILED；新增六份已真实索引但保留原PARTIAL质量。后续082的XML及聚合门槛已越过，最新失败与单文档验证步骤以上节为准；不能把历史XML操作再执行一遍当作修复。

## 10月19日00:52快照：剩余10份分组恢复（历史）

用户三份CSV最终为116 READY、74 PARTIAL_READY、10 FAILED；190份已索引项均有Block、Chunk与向量数相等且无无效向量。新增两份Office为PARTIAL_READY，不等于全部200份完整验收。剩余为六份旧外链预检、三份远端failed、一份XML预算；六份当天只有GET状态，前轮脚本遇到`c8e4ca42…`云失败后停止了组内后续提交，并非六份应用修复后再次失败。

本轮不修改Adapter或旧记录。远端失败保留已知代码/固定映射到独立`_remote_failure`，管理cause可显示受控原因；远端终态保持不可自动重投，即使原因属于临时服务类别也不自动新建批次。未知原文不输出。历史三份原因未保存，新增CLI只读查原Job，可显式GET一次原Batch；不上传、下载结果、发布缓存或恢复业务。若远端仍为failed且原因未知，需平台按Batch/trace核对，不能认定可用旧ZIP恢复。

由用户手动测试（Codex未执行），再确认相关队列任务安全结束，按README重启API及所有CPU Worker：

```powershell
python -m pytest tests/test_mineru_remote_status.py tests/test_stage33_office_script.py tests/test_mineru_recovery.py tests/test_mineru_post_adapter.py tests/test_document_management.py -q -ra
```

恢复余六份时先保持XML预算16。显式继续选项只跨过业务FAILED且管理failed的确定终态，仍串行每份POST一次；HTTP未确认、超时、已有Job和身份冲突仍停止。不重投前轮已成功或云失败项，最多首次提交余六份（可能收费）：

```powershell
powershell -NoProfile -File scripts/recover_stage33_office.ps1 -ExecuteSavedOffice -AllowCloud -ContinueSavedOfficeOnFailure
```

三份原云失败仅做诊断，命令由用户执行；省略`--query-existing`仅查只读SQL，不访问MinerU：

```powershell
$remoteDocIds = @(
    '8a3496d6-dd9e-40bd-be60-70b4b701002a',
    '81f211b8-330f-4015-9ad7-b3d377a7392b',
    'c8e4ca42-f739-48c4-9c2d-50120f956309'
)
foreach ($remoteDocId in $remoteDocIds) {
    python scripts/mineru_remote_status.py --doc-id $remoteDocId --query-existing
}
```

XML项`082ebed3-a4e9-4b61-934b-4025b472192e`的旧实测单部件20,204,999字节、总XML38,016,609字节。当前本地`.env`仍为16 MiB单部件预算，超限是预期保护。余六份结束后、相关任务安全退出时，用户手动将已有`ROUTER_XML_PART_MAX_MB=16`改为32，总预算保持64；重启API/CPU Worker后执行以下单文档命令。配置变化会影响新缓存身份，不借此重建已成功文档。若实时状态或Job历史与预期不符，命令停止；可能首次走云，用户须确认费用。

```powershell
$xmlDocId = '082ebed3-a4e9-4b61-934b-4025b472192e'
$xmlBase = "http://127.0.0.1:8000/api/admin/documents/$xmlDocId"
$xmlState = Invoke-RestMethod "$xmlBase/status" -ErrorAction Stop
$xmlDetail = Invoke-RestMethod $xmlBase -ErrorAction Stop
if ($xmlState.doc_id -ne $xmlDocId -or $xmlDetail.doc_id -ne $xmlDocId -or
    $xmlState.kb_id -ne '158030a1-42d6-42ee-835d-02fdbff23f81' -or
    $xmlDetail.kb_id -ne $xmlState.kb_id -or $xmlState.status -ne 'FAILED' -or
    $xmlState.management.state -ne 'failed' -or $xmlState.management.cause_code -ne 'PREFLIGHT_LIMIT' -or
    $null -eq $xmlDetail.parse_jobs -or @($xmlDetail.parse_jobs).Count -ne 0) {
    throw '状态/身份/云历史变化，未重投；先检查当前详情。'
}
Invoke-RestMethod -Method Post "$xmlBase/retry" -TimeoutSec 120 -ErrorAction Stop
# 全200份重新查询；不读旧import_checkpoint判断恢复成败。
powershell -NoProfile -File scripts/recover_stage33_office.ps1
```

验收仍看真实status及Block/Chunk/向量，不能把受理、远端done或诊断通过当作READY。74份PARTIAL_READY及其真实缺页/外部源缺口仍未完整验收；本轮不修改质量或伪造完整状态。

## 九份Office保存快照恢复（前轮实现）

用户报告上一轮操作已完成，尚无新的全200份CSV，本节不猜测当前总数。只读核对这9份冻结原件及关系元数据后，确认先前“全部缺少正文附件”的分类不准确：5份DOCX/PPTX含已保存数据的图表更新源，其中1份链接图片同时有包内副本；4份XLSX包含外部工作簿更新引用，原单元格/公式仍可保留。此前统一报`EXTERNAL_ATTACHMENT_MISSING`发生在MinerU提交前，不是上一轮strong切片修复失效或MinerU解析失败。

本次仅允许有证据的保存快照：精确关系类型及拥有者、缓存索引/值、同元素图片副本和包内目标均验证。真正单独外链图片、未知关系、内部缺件及损坏仍拒绝。XLSX走Whole加Native主结果，原公式保留且不计算；不抓取外部URL，也不将缓存值当作外部数据完整证明。Guard保留原块及既有缺口，新增`OFFICE_SAVED_SNAPSHOT / EXTERNAL_SOURCE_UNVERIFIED`，最终索引状态应为PARTIAL_READY，正式完整验收仍未通过。缓存仅此分支增加`office_snapshot_version=office-saved-snapshot-v1`，其他旧Job/缓存/READY向量不重建。

由用户先运行定向回归（Codex未执行）：

```powershell
Set-Location 'D:\python project\dkf-agent'
conda activate dkf-agent
python -m pytest tests/test_office_saved_snapshot.py tests/test_stage33_office_recovery.py tests/test_parser_router.py tests/test_xlsx_primary.py tests/test_parse_cache.py -q -ra
```

通过后停止新投递，检查active/reserved/scheduled，确认相关任务安全结束再正常停止并按README重启API、全部CPU/GPU Worker；不强杀、不清队列，无新迁移/依赖或环境变量。保持目前已采用的配置，不为这9份额外调整XML预算。

```powershell
python -m celery -A app.core.celery_app:celery_app inspect active
python -m celery -A app.core.celery_app:celery_app inspect reserved
python -m celery -A app.core.celery_app:celery_app inspect scheduled
# 默认只读：登记原知识库全200份并另存实时CSV，不以旧checkpoint判断状态。
powershell -NoProfile -File scripts/recover_stage33_office.ps1
# 仅确认首次云解析费用后执行：固定九份、串行等待、每份POST一次，不执行Force。
powershell -NoProfile -File scripts/recover_stage33_office.ps1 -ExecuteSavedOffice -AllowCloud
# 完成后重新取得全200份实时报告。
powershell -NoProfile -File scripts/recover_stage33_office.ps1
```

九份固定身份为下方历史清单中的4份外部工作簿及5份OLE/图片关系文档，脚本已单独列明。此处描述前轮首次恢复前状态，当时均没有Job，可能首次提交0～9个Whole云任务，并非已有ZIP的无云恢复。脚本再次读取实时详情，已有任何Job即停止新提交，不借本修复重传旧Batch；跳过READY/PARTIAL_READY及活动任务。默认第一份实际失败后不再提交该组后续项，但仍完整生成200份报告；新显式继续开关见上节，HTTP确认失败/超时仍停止，不盲重发。

验收可靠内容入库需`PARTIAL_READY`、`chunk_count=vector_count>0`、`invalid_vector_count=0`、管理done，并确认质量保留外部源告警与原公式/来源；这不能替代完整外部数据验收或宣称200份READY。若失败，使用新CSV的stage/cause/error及对应Worker原异常继续定位，不能将“路由放行”当成恢复成功。9份真实供应商转换、GPU/索引及新增测试尚未执行，必须由用户确认。

## 23:45快照与单文档恢复（历史）

10月18日23:42/23:45两份用户实时CSV均为115 READY、72 PARTIAL_READY、13 FAILED，全部200份均有记录；187份已索引文档的Chunk数与向量数一致、无无效向量。剩余13份为9份外部依赖、2份原云失败、1份真实Token超限、1份XML预算超限。PARTIAL_READY不是全量完整验收，其中10份记录required缺页。

只读检查确认`efdf30a0-e412-4d28-8a3a-5257f2fe62a0`的第101片为12238真实Token，已使用v4切片仍失败。其现有成功解析缓存的三张HTML表包含单元格`p`内无属性的`strong`；v4行解析不接受该标签，记录`UNSUPPORTED_TABLE_ROWS`后保留整表。新v5仅增加这一样式兼容，原表/行/rowspan/实体/字符来源不变，Adapter默认行为及v1～v4冻结；不提高8192上限、不截断。尚未执行真实网格回放和Tokenizer，不能保证没有其他超长行或结构问题。

先保持现有`ROUTER_XML_PART_MAX_MB=16`，由用户执行本轮定向回归，Codex未执行：

```powershell
conda activate dkf-agent
python -m pytest tests/test_chunk_table_rows.py tests/test_chunk_splitter.py tests/test_document_management.py tests/test_stage33_office_recovery.py tests/test_mineru_table_merge.py -q -ra
```

通过后暂停新投递，检查下列队列，待相关任务安全结束再正常停止并按README重启API/全部CPU/GPU Worker；不清队列、不强杀任务，无新迁移/依赖。只重启API不能更新CPU切片版本。

```powershell
python -m celery -A app.core.celery_app:celery_app inspect active
python -m celery -A app.core.celery_app:celery_app inspect reserved
python -m celery -A app.core.celery_app:celery_app inspect scheduled
```

**只对这一份执行一次Reindex**；普通Retry沿用旧Chunk，不能应用v5。现有成功缓存参与身份/Hash校验，命中后直接重建Chunk并接续GPU/Index；缺缓存会失败，不重新请求MinerU。保留原知识库、原run和所有其他READY向量。

```powershell
$base = 'http://127.0.0.1:8000/api/admin/documents/efdf30a0-e412-4d28-8a3a-5257f2fe62a0'
Invoke-RestMethod -Method Post -Uri "$base/reindex" -TimeoutSec 120 -ErrorAction Stop
# 受理不等于完成；后续用GET查询，不重复POST。
Invoke-RestMethod -Uri "$base/status" -TimeoutSec 30 -ErrorAction Stop |
    Select-Object status,block_count,chunk_count,vector_count,invalid_vector_count,error_message,management |
    ConvertTo-Json -Depth 6
# 新切片已经生成后检查实际版本；不是block-chunk-v5时先查旧Worker，不继续重投。
$sample = Invoke-RestMethod -Uri "$base/chunks?limit=1&offset=0" -TimeoutSec 30 -ErrorAction Stop
$sample.items[0].metadata | Select-Object chunker_version,source_parse_task_id
```

验收：`READY`或如实保留原质量的`PARTIAL_READY`、`chunk_count=vector_count>0`、`invalid_vector_count=0`、管理任务done。只有READY计完整验收；PARTIAL_READY表示本地入库完成但质量缺口仍在。若仍超Token，保留该次`embedding.chunk_rejected`的chunk_id、序号和真实Token数，停止重复GPU任务。全量脚本继续将该失败项列为`TOKEN_SOURCE_REVIEW`，避免未验收前自动批量Reindex。

完成当前配置下的缓存操作后，再单独处理`082ebed3-a4e9-4b61-934b-4025b472192e`：已测单XML为20204999字节、Sheet总XML38016609字节，前者超过16MiB。由用户在`.env`将既有`ROUTER_XML_PART_MAX_MB`调至32，确认任务结束后重启API/CPU Worker；其他总XML/格式/实体/Hash校验不变。配置参与缓存身份，不能先改配置再恢复前一份缓存。

```powershell
# 修改已有配置项，不追加重复项；这是用户手动部署步骤。
notepad .env
# 保存ROUTER_XML_PART_MAX_MB=32并安全重启相关进程后核对：
Select-String -LiteralPath .env -Pattern '^ROUTER_XML_PART_MAX_MB='
# 仅确认可能首次提交MinerU及费用后执行；不是已证明的无云恢复。
$xmlBase = 'http://127.0.0.1:8000/api/admin/documents/082ebed3-a4e9-4b61-934b-4025b472192e'
Invoke-RestMethod -Method Post -Uri "$xmlBase/retry" -TimeoutSec 120 -ErrorAction Stop
Invoke-RestMethod -Uri "$xmlBase/status" -TimeoutSec 30 -ErrorAction Stop |
    Select-Object status,error_message,management | ConvertTo-Json -Depth 6
```

9份外部关系已由10月19日的逐项检查修正为可处理保存快照，详见上节；外部源内容完整性仍待核验。2份远端failed需核对原Job/Batch的平台具体原因，不能用本地切片修复代替或自动申请新批次。72份PARTIAL_READY的缺页/视觉/空结构分别核对，不清警告改READY。完成上述单文档后用默认只读命令刷新全200份报告，不读旧checkpoint判断恢复结果：

```powershell
powershell -NoProfile -File scripts/recover_stage33_office.ps1
```

## 22:27恢复结果与Embedding聚合预算修复（历史）

用户三份实时CSV分别为：22:11 READY102/PARTIAL_READY68/FAILED30；22:18 READY107/PARTIAL_READY68/FAILED24/EMBEDDING1；22:27 READY112/PARTIAL_READY70/FAILED16/EMBEDDING2，均登记200份。报告是逐文档读取快照，不保证同时刻状态；目前不宣称全部READY。

原任务`885de43e-d667-4501-82b7-40ce7482d1eb`关联`7339e772-0627-40bf-b90f-a0e6646c0356`。日志明确为`read_chunks → prepare_embedding → next_action`抛出`EMBEDDING_LIMIT`，随后被管理任务误包装成`MANAGEMENT_CHECKPOINT`。只读SQL聚合确认：

| 文档 | 当前Chunk | 文本总字符 | 原上限 | GPU任务 |
|---|---:|---:|---:|---|
| be585058-60d3-4e7a-a96b-db0bc23f6d28 | 15160 | 11273580 | 10000000 | 未创建 |
| 7339e772-0627-40bf-b90f-a0e6646c0356 | 12992 | 14965690 | 10000000 | 未创建 |

本轮仅修复文档聚合预算（1600万字符）及准备失败的错误分类/阶段，保留单批Service的1000万字符、2万Chunk、8192真实Token及512MiB向量产物限制。两份可用原Chunk普通Retry，不重新解析/切片/请求MinerU。新失败按真实阶段记录`MANAGEMENT_STAGE_FAILED/cause_code`；真正代次冲突仍拒绝。新CSV补充management_state/management_error，避免把已停止编排误当仍在运行。

用户先执行定向Mock测试，Codex未执行：

```powershell
conda activate dkf-agent
python -m pytest tests/test_embedding_processing.py tests/test_embedding_service.py tests/test_management_task.py tests/test_document_management.py tests/test_business_logging.py -q -ra
```

测试通过后，先暂停新投递并检查`celery inspect active/reserved/scheduled`，待任务安全结束再正常停止、重启API和全部CPU/GPU Worker加载修改。无需迁移或修改环境变量。仅恢复上述两份时，由用户先对第一份执行并等到终态，再处理第二份：

```powershell
$base = 'http://127.0.0.1:8000/api/admin/documents/7339e772-0627-40bf-b90f-a0e6646c0356'
Invoke-RestMethod -Method Post -Uri "$base/retry" -TimeoutSec 120 -ErrorAction Stop
Invoke-RestMethod -Uri "$base/status" -TimeoutSec 30 -ErrorAction Stop |
    Select-Object status,chunk_count,vector_count,invalid_vector_count,embedding_task_id,management |
    ConvertTo-Json -Depth 6
# 第一份完成后，将$base末尾换为be585058-60d3-4e7a-a96b-db0bc23f6d28，执行相同Retry/状态命令。
```

状态/向量数量及原解析质量决定业务验收，不以202或GPU任务已创建当完成。两份各有约1.3～1.5万Chunk，真实GPU耗时/显存及本地向量文件大小需用户验收，建议串行。使用下列只读命令重新生成全部200份实时报告：

```powershell
powershell -NoProfile -File scripts/recover_stage33_office.ps1
```

该脚本`-ExecuteLocal`新增对两份`LOCAL_RETRY`的串行接续；原已验证缓存组仍按Reindex。`efdf30a0…`已由v4重建为124个Chunk，日志仍有第101个Chunk的12238真实Token超限，现标`TOKEN_SOURCE_REVIEW`，不反复重建并消耗GPU；本轮没有修复该来源长片。3份旧缓存未发布、9份外部依赖、1份XML预算、2份原云失败及70份PARTIAL_READY仍分别保留，不因本次预算修复自动通过。

## 较早清单：30份FAILED、68份PARTIAL_READY的恢复

用户最新输出中，剩余6份`EMBEDDING_TOO_LONG`的Chunk仍为`block-chunk-v3`（已逐份只读查询确认）；原生TSV和大表策略需要CPU Worker加载v4后**Reindex**才生效。重试Embedding不会改变原Chunk。已补充`chunk.started`的`step=实际冻结版本`日志，避免只看磁盘源码误判运行版本。

| 数量 | 当前原因 | 恢复方式与边界 |
|---|---|---|
| 6+1 | 旧版切片导致超Token/源文本预算 | 已有成功缓存，加载v4后串行Reindex，不请求MinerU |
| 6+2 | DOCX排版模板/customXml误拒绝 | 兼容代码已编写，加载新Worker后Retry；可能首次提交0～8个新云文件，须用户确认费用 |
| 3 | 同Hash原成功Job只有v2缓存，当前目标无缓存/Job | 新增显式本地缓存升级CLI，核对原成功Job及原ZIP，转换后由原Reindex接续 |
| 1 | 单XML 20204999字节大于16MiB | 不取消资源保护，最后单独调整既有预算至32MiB，再由用户恢复；不能保证无后续问题/费用 |
| 9 | 当时按外部工作簿、图片/OLE关系统一拒绝 | 10月19日已确认保存快照可继续处理，保留外部源未核验的PARTIAL，见最新恢复节 |
| 2 | 原MinerU Job远端failed | 没有已证明成功结果；需要平台具体原因，不自动重投、Force或追加批次 |

68份PARTIAL_READY已存在真实向量，但不等于完整内容通过。19:57本地质量快照按优先原因分为：10份缺required页、27份含纯视觉页、12份存在空结构/表格内容，另19份需进一步质量审核；分类互斥，仅用于定位，不据此猜测图片语义或批量改READY。最新逐文档缺页、待审页、警告及错误由下述只读脚本重新查询并输出CSV。

### 1. 用户执行定向回归并确认进程安全退出

Codex仅编写，未执行以下测试、真实转换、缓存发布或任务恢复：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_office_cache_upgrade.py tests/test_stage33_office_recovery.py tests/test_chunk_table_rows.py tests/test_chunk_splitter.py tests/test_document_management.py tests/test_business_logging.py -q -ra
python -m celery -A app.core.celery_app:celery_app inspect active
python -m celery -A app.core.celery_app:celery_app inspect reserved
python -m celery -A app.core.celery_app:celery_app inspect scheduled
```

先暂停新的导入，待相关任务安全结束后正常停止API/全部CPU及GPU Worker，不强杀任务、不清队列。下面缓存升级期间只需PostgreSQL保持可用；停止相关进程可避免文件发布与其他文档维护交错。本轮无新迁移、依赖或环境变量。

### 2. 三份旧成功缓存显式升级（本地，无云请求）

`upgrade_mineru_office_cache.py`默认只核验身份；`--execute`才运行本地Adapter，`--publish`才发布目标新缓存。只支持整份DOCX/PPTX，保留原Job/Batch/缓存，不用于PDF分片、Hybrid或XLSX Native主结果。数据库事务强制READ ONLY并回滚；原件Hash、所有缓存文件Hash、原Job专属缓存回执、云参数Hash、冻结Guard/覆盖范围必须匹配。不会复制旧Normalizer标签来冒充升级。报告保留具体`identity_diagnostic.failed_check`；失败时停止，不下载或重新上传。

**在调整任何ROUTER配置之前执行**，配置/云参数变化仍拒绝；不把16→32MiB的资源许可混入旧缓存升级。

```powershell
$kbId = '158030a1-42d6-42ee-835d-02fdbff23f81'
$upgrades = @(
    @{ Source='e5254d97-ae7b-4235-98e0-0e3b12e2c08d'; Job='8790d023-7981-4616-a62b-f8056ce77786'; Target='be97464e-7ecc-4fd3-9af7-14a830c3bdf6' },
    @{ Source='cd8126f6-2d64-4277-9025-b86a9dff7479'; Job='5feb103f-0895-470f-9d21-ee10e295f56f'; Target='5bf1bd8a-8879-4472-a2c7-4ff04bcba0ef' },
    @{ Source='1f76ed22-b166-42d2-bb9f-cc6d595a941e'; Job='cd5e7a78-d808-4d03-bee4-09435185409c'; Target='d517319d-c348-49c6-925e-4abf894d1b8d' }
)
foreach ($entry in $upgrades) {
    $report = "results/cache_upgrade_$($entry.Target)_$(Get-Date -Format yyyyMMdd_HHmmssfff).json"
    python scripts/upgrade_mineru_office_cache.py `
        --source-doc-id $entry.Source --source-job-id $entry.Job `
        --target-doc-id $entry.Target --kb-id $kbId `
        --execute --publish --output $report
    if ($LASTEXITCODE -ne 0) { throw "升级停止，请查看 $report；未触发云任务。" }
}
```

预期`CACHE_PUBLISHED_NOT_RECOVERED`、`cache_published=true`、`cloud_submitted=false`、`database_modified=false`；这还不是READY。质量如实保留，转换后的PARTIAL不冒充完整。已有目标latest指针时拒绝覆盖；若上次发布已完成，先查看报告并直接用Reindex，不反复执行升级CLI。若发布中途磁盘/缓存错误，报告`publication_attempted=true, cache_published=null`，表示可能已有部分产物，不能解读成“从未写入”；先核对目标latest及报告，不清缓存或盲目重跑。

### 3. 原知识库分组恢复及全部200份状态核验

按README手动启动API和加载新代码的CPU/GPU Worker。只读报告可随时执行；默认不会重投任何任务：

```powershell
# 全部200份实时状态，生成新的逐文档CSV，不改原run/checkpoint。
powershell -NoProfile -File scripts/recover_stage33_office.ps1

# 只串行Reindex已检查过且已有成功缓存的失败项；READY/PARTIAL_READY保留。
powershell -NoProfile -File scripts/recover_stage33_office.ps1 -ExecuteLocal

# 仅在确认8份首次云解析费用后，由用户执行另一组：
powershell -NoProfile -File scripts/recover_stage33_office.ps1 -ExecuteNewParse -AllowCloud
```

脚本始终登记原知识库全部200份，每次只对一份POST并有界等待；已有活动编排不覆盖，请求确认失败/超时不盲重发。原`-ExecuteNewParse`仍只允许上述8个身份；10月19日新增九份独立`-ExecuteSavedOffice -AllowCloud`，发现已有Job停止，不能把新开关用于其他文件。缓存缺失不会误走云重试；旧Worker版本检查保留。失败原因原样保留，不标成功。

报告位于`results/stage33/recovery_status_*.csv`，记录所有200份的`status/passed/action/cause/error`及`warnings/missing_pages/visual_review_pages/diagnostic_reasons`。再次检查用默认命令，不读取旧checkpoint状态，不自动刷新run或启动QA评测。CSV含文件名与内部质量摘要，仅保留在本地受限results目录。

### 4. 本轮不能自动消除的项

- `082ebed3-a4e9-4b61-934b-4025b472192e`：先完成所有旧配置缓存操作，再由用户调整现有`ROUTER_XML_PART_MAX_MB=32`并重启相关API/CPU进程；允许32MiB不等于跳过总XML/DTD/实体检查。确认潜在首次云费用后，只对该文档手动`POST /api/admin/documents/082ebed3-a4e9-4b61-934b-4025b472192e/retry`。当前脚本不替用户更改配置或授权这笔费用。
- 外部关系9份、远端failed 2份身份见下方历史清单；九份保存快照规则已修正见最新节，远端失败仍需原Batch具体原因，不Force绕过。
- PARTIAL_READY的68份：可靠块/向量已入库，可继续查看来源和质量；正式全量验收仍须完整READY。缺页、缺表体、纯视觉内容必须按原件/原ZIP核对，不能统一清警告或把未识别图像当装饰。当前没有据日志凭空补文的修复。

## 19:57历史检查点与既有Office兼容修复

本轮只读检查19:57检查点及日志：READY101、PARTIAL_READY68、FAILED31，全部31份失败属于OCB；不是只处理了部分200份。修改代码/编写测试不代表失败文件已恢复，本轮没有执行测试、回放、API重投或GPU。

| 数量 | 已确认原因 | 本轮处理及边界 |
|---|---|---|
| 7 | 原生TSV表格被保留为整块，真实Token超过8192 | 新`block-chunk-v4`按完整TSV逻辑记录切片；使用已有解析缓存Reindex，普通Retry不能换掉旧Chunk |
| 1 | NBA工作簿缓存10823650字符超过1000万；另有8064171字符/12845行/295435单元格、含p段落的HTML表 | v4源上限1600万、有界网格及单元格p兼容；旧v1～v3、Adapter默认行为不变，单文档串行重建 |
| 6 | DOCX仅外部排版模板被误判缺正文附件 | 仅官方`attachedTemplate`外部关系例外；不下载模板，不放行内容附件；此前未创建云Job，Retry可能首次收费 |
| 2 | Workbook合法的根`customXml/item*.xml`被Hybrid误判必须在xl内 | 精确官方关系允许已有包内部件，其他路径/内容校验保留；后续可能新建Bundle/Whole云任务 |
| 1 | XLSX单XML部件20204999字节超过默认16MiB；Sheet总XML38016609字节 | 保留保护并增加实测字节日志；由用户完成前述缓存恢复后受控设置既有预算32MiB，不能直接宣称已通过 |
| 9 | 外部工作簿、OLE或图片内容关系 | 当时保守阻塞；10月19日核对保存快照后可继续PARTIAL索引，不冒充外部源完整 |
| 3 | 同Hash旧付费成功缓存为Normalizer v2，新v4缓存不命中 | 找到旧缓存及其中ZIP的文件路径；未执行Hash/转换验证，未复制身份或静默升级，费用保护保留 |
| 2 | 日志明确记录MinerU远端state=failed | 没有可证明成功的本地结果，不能用Adapter修改修复或自动申请新批次 |

用户先执行定向回归（不含真实DB/GPU/API；Codex未运行）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_stage33_office_recovery.py tests/test_chunk_table_rows.py tests/test_chunk_splitter.py tests/test_document_management.py tests/test_parser_router.py tests/test_hybrid.py tests/test_parse_cache.py tests/test_business_logging.py tests/test_stage33_pipeline.py -q -ra
```

回归通过后，先确认队列任务安全结束，再由用户重启API及CPU/GPU Worker加载新代码；不要清Redis/缓存、强杀处理中任务或迁移数据库。本轮无新迁移/依赖。只读查看队列：

```powershell
python -m celery -A app.core.celery_app:celery_app inspect active
python -m celery -A app.core.celery_app:celery_app inspect reserved
python -m celery -A app.core.celery_app:celery_app inspect scheduled
```

以下辅助函数只调用原管理API，一份结束才处理下一份；不会上传文件、创建知识库或Force。HTTP确认丢失时停止，不自动再次POST。`PARTIAL_READY`仍须核对缺口，不算正式完整READY。

```powershell
$ErrorActionPreference = 'Stop'
function Invoke-Stage33OfficeRecovery {
    # 手动串行恢复指定失败身份；遇活动编排只观察，遇失败/超时立即停止。
    param([string]$DocId, [ValidateSet('reindex','retry')][string]$Operation)
    $base = "http://127.0.0.1:8000/api/admin/documents/$DocId"
    $s = Invoke-RestMethod "$base/status" -TimeoutSec 30
    if ($s.kb_id -ne '158030a1-42d6-42ee-835d-02fdbff23f81') { throw "知识库不符：$DocId" }
    if ($s.status -in @('READY','PARTIAL_READY')) {
        Write-Host "$DocId 已为 $($s.status)，未重投"
        return
    }
    if ($s.management.state -ne 'running') {
        if ($s.status -ne 'FAILED') { throw "请先核对活动任务：$DocId / $($s.status)" }
        if ($Operation -eq 'reindex' -and -not $s.parse_cache_present) { throw "成功缓存缺失：$DocId" }
        if ($Operation -eq 'retry') {
            # 本组只允许从未创建云Job的文件，收费历史不能靠重复Retry绕过。
            $detail = Invoke-RestMethod $base -TimeoutSec 30
            if (@($detail.parse_jobs).Count -gt 0) { throw "已有Job，先核对结果/费用：$DocId" }
        }
        Invoke-RestMethod -Method Post "$base/$Operation" -TimeoutSec 120 | Out-Null
    }
    $deadline = (Get-Date).AddHours(2)
    do {
        Start-Sleep -Seconds 5
        $s = Invoke-RestMethod "$base/status" -TimeoutSec 30
        if ($s.status -in @('READY','PARTIAL_READY')) {
            if ($s.chunk_count -lt 1 -or $s.vector_count -ne $s.chunk_count -or $s.invalid_vector_count -ne 0) {
                throw "终态向量统计异常：$DocId"
            }
            $s | Select-Object doc_id,status,block_count,chunk_count,vector_count
            return
        }
        if ($s.management.state -eq 'failed') {
            throw "$DocId 失败：stage=$($s.management.stage)，cause=$($s.management.cause_code)，error=$($s.error_message)"
        }
    } while ((Get-Date) -lt $deadline)
    throw "$DocId 等待超时；后台可能仍执行，请先GET状态，不重复POST"
}

# 第一组：8份已有成功缓存，先执行第一份验证v4切片/GPU/索引，再串行处理其余。
$cached = @(
    'b37f4679-2701-4d54-8165-c80fd0840611',
    '8b72d308-ac54-4506-bf8d-734cacaf1b28',
    '897d54a0-dd38-42ad-b7e5-d0a22b35f956',
    'e4c88d4d-f816-4b55-a0ea-92a5d642ec16',
    '3649c30e-d0cd-4c36-95cc-4842599156e5',
    'ce7e99cf-0928-4609-a1bf-2baa6781bc86',
    'efdf30a0-e412-4d28-8a3a-5257f2fe62a0',
    'be585058-60d3-4e7a-a96b-db0bc23f6d28'
)
Invoke-Stage33OfficeRecovery -DocId $cached[0] -Operation reindex
# 确认第一份的真实终态及Chunk元数据chunker_version=block-chunk-v4，再运行：
$cached | Select-Object -Skip 1 | ForEach-Object { Invoke-Stage33OfficeRecovery -DocId $_ -Operation reindex }
```

第二组命令由用户在接受**新解析费用**后手动执行：这8份此前没有自己的云Job；可能0～8个新云文件任务，实际由Native/Hybrid/Whole决定，不是复用旧付费结果。不要把其他14份塞入此列表。

```powershell
$newParse = @(
    'da3d2a31-4be8-454a-82a2-e5195b2c2da8',
    '6978b773-c412-4541-96ea-07ebf3517c90',
    '68989d39-3032-40c0-8d42-dddb29ea2185',
    '3f73d303-099f-4698-8910-4748667f64d2',
    '7440559b-26e2-40dd-ae35-39881536ac11',
    'ee5e1f7a-0208-4c2d-adbc-eca7b98a4aa7',
    '7339e772-0627-40bf-b90f-a0e6646c0356',
    '83d24856-fb63-49f9-9466-8db2bf8543c8'
)
$newParse | ForEach-Object { Invoke-Stage33OfficeRecovery -DocId $_ -Operation retry }
```

单XML超限的`082ebed3-a4e9-4b61-934b-4025b472192e`应最后处理。完成上组后，确认Worker空闲；在API和CPU Worker各自启动终端设置`$env:ROUTER_XML_PART_MAX_MB='32'`并按README重启，再单独调用上面的函数`-Operation retry`。可能首次新增1个云文件任务；若仍超限按新日志处理，不关闭总XML/DTD安全限制。进程环境变化不写入`.env`；重启前恢复原值16。路由配置参与缓存身份，不在该临时配置下恢复其他旧缓存。

剩余14份必须保留在失败清单，不能声称本轮已修好：

- 外部工作簿4份：`fd0eed46-d01c-450d-901e-d6c10edcdb68`、`d5eefa49-4d03-4e66-aa33-54b7d27e5734`、`5f7494bb-d83a-41cf-b4ee-30a618b4da60`、`b353a134-04d8-4cb2-ae30-9d87301f459f`。
- 外部OLE/图片5份：`4d18710e-c8bf-4504-8982-bd5ce7e115a9`、`926d4339-96f6-4756-af61-121901efd806`、`e1f59a27-a401-4de1-809b-a998ae7c63f1`、`c8e4ca42-f739-48c4-9c2d-50120f956309`、`2d4cb398-1f04-461a-87e8-84437fcf2554`。
- 远端失败2份：`81f211b8-330f-4015-9ad7-b3d377a7392b`（Job `681c250a-43c4-4557-bdbb-c5a0043edbec`），`8a3496d6-dd9e-40bd-be60-70b4b701002a`（Job `53406701-32c5-47ff-a09c-a239c8324dad`）。需平台安全原因，不能凭本地异常推断ZIP损坏或直接重收费。

3份v2历史成功缓存已找到如下映射（仅读取元数据及文件存在性，尚未执行回放/校验）：

| 阶段33失败文档 | 原文档 | 原成功Job |
|---|---|---|
| be97464e-7ecc-4fd3-9af7-14a830c3bdf6 | e5254d97-ae7b-4235-98e0-0e3b12e2c08d | 8790d023-7981-4616-a62b-f8056ce77786 |
| 5bf1bd8a-8879-4472-a2c7-4ff04bcba0ef | cd8126f6-2d64-4277-9025-b86a9dff7479 | 5feb103f-0895-470f-9d21-ee10e295f56f |
| d517319d-c348-49c6-925e-4abf894d1b8d | 1f76ed22-b166-42d2-bb9f-cc6d595a941e | cd5e7a78-d808-4d03-bee4-09435185409c |

原缓存包含`raw/mineru_result.zip`，PDF诊断CLI不能用于这些Office。现已新增本页顶部的显式本地升级CLI及回归，尚未由Codex执行；不能复制latest指针、改Normalizer标签或Force代替身份校验。下面保留原成功Job只读查询命令：

```powershell
@('e5254d97-ae7b-4235-98e0-0e3b12e2c08d','cd8126f6-2d64-4277-9025-b86a9dff7479','1f76ed22-b166-42d2-bb9f-cc6d595a941e') |
    ForEach-Object {
        $d = Invoke-RestMethod "http://127.0.0.1:8000/api/admin/documents/$_" -TimeoutSec 30
        $d.parse_jobs | Select-Object @{n='source_doc_id';e={$d.doc_id}},job_id,state,error_code,batch_id,upload_complete
    } | Format-Table -AutoSize
```

恢复期间用GET查看真实状态，重新加载`$items`只会重读磁盘旧快照。所有200份都已登记时，可在手动恢复结束后沿用原run执行普通`--step import --execute --resume --allow-cloud`（**不加**`--retry-failed`/`--retry-failed-only`），由原导入器观察并刷新报告；该命令仍具有补传缺失文档的能力，执行前确认200个原doc_id均存在。`--step report`单独运行只汇总既有检查点，不访问数据库刷新。正式检索仍须200完整READY，不将68个PARTIAL_READY合算成功。

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
