# 阶段34：固定题单 RAG 质量评测

本轮提供可重复运行的评测入口，真实问答和人工评分待用户执行。阶段33已有100题/400次策略执行记录；OHR证据页指标与OCB依赖文档代理指标分开保留，不以100%召回为前置目标。阶段34不重解析、不重建向量、不修改已有200份文档。

## 范围与边界

`scripts/evaluate_stage34.py`复用原Hybrid → Top30 → BGE重排Top5 → 项目RAG合成。原问题及最终证据正文交给当前`LLM_PROVIDER`；启用备用时也允许同一内容交给官方DeepSeek。参考答案、官方断言、来源页标签不传给检索或LLM。复用已有`rag-cli`角色，日志仅含计数、状态和KB；正文仅保存在本地忽略目录`results/stage34/`。

默认从`stage33_run_03`的已选100题按身份Hash固定选择：

- OHR：10个不同文档各一题，指定单文档范围，不按检索成功挑题。
- OCB：5题，优先实际依赖多个文档的问题，再补单文档问题；`expected_assertions`须人工解释，不拼成标准答案。
- 前三个单文档问题增加全知识库范围对照，检索完整200份可靠索引。
- 前两个单文档问题增加排除官方依赖的受限范围对照。**这不证明范围内绝无答案**，预期行为默认`unknown`，须人工确认是否应拒答。

默认共20次问答，含范围对照，不能称为20道独立官方题。计划展示实际scope数量；抽样池缺少多文档题时，不冒称已覆盖。数值、单位、表格、公式、歧义及拒答通过人工标签记录实际覆盖，缺失类型待补。

保留READY/PARTIAL_READY及未知历史解析质量。显式沿用阶段33的BM25实例预算，生产默认、过滤、来源与评分不变；同时最多保留一个范围的BM25缓存，本地模型沿用进程共享服务。

## 1. 用户手动回归与准备计划

项目根目录、已有`dkf-agent`环境；Codex未运行这些测试。

```powershell
python -m pytest tests/test_stage34_evaluation.py tests/test_stage34_fallback.py tests/test_stage34_runtime.py tests/test_llm_fallback.py tests/test_llm_service.py tests/test_rag_query_engine.py -q -ra
if ($LASTEXITCODE -ne 0) { throw "回归未通过，停止问答评测" }

# run_01保留原503失败和已发生请求；修复后另建run_02，不修改旧计划。
$ragRunName = "stage34_run_02"
$ragRun = "results/stage34/$ragRunName"

# 默认只读预览：不查数据库、不加载模型、不联网、不写计划。
python scripts/evaluate_stage34.py --run $ragRunName --source-run stage33_run_03
if ($LASTEXITCODE -ne 0) { throw "计划预览失败" }

# 仅保存本地计划和空评分报告。
python scripts/evaluate_stage34.py --step plan --run $ragRunName --source-run stage33_run_03 --execute
if ($LASTEXITCODE -ne 0) { throw "计划保存失败" }

$ragPlan = Get-Content "$ragRun/plan.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$ragPlan.plan.policy | Select-Object fallback,llm_calls_per_case_max
$ragPlan.plan.cases | Select-Object case_id,dataset,scope,purpose,question | Format-Table -Wrap
$ragPlan.plan.cases | Group-Object scope | Format-Table Name,Count
```

计划只核对阶段32已有文件存在/大小及QA元数据，复验阶段33选题、索引观察和Hash绑定，不解析原件。主备开关及单题请求上界同时冻结；开关不影响题目选择。现有配置开启备用时应显示`fallback=true`、`llm_calls_per_case_max=2`。已有计划不得覆盖；相同计划可加`--resume`，改变开关、种子/数量或索引后另用新run。`run_02`若已使用，手动改为未使用的新编号。

## 2. 显式付费：先执行一题

以下步骤**由用户核对题单、提供方、外传权限和费用后手动执行**。需要现有PostgreSQL和本地BGE-M3、Reranker模型；无需CPU/GPU Worker、API、Redis或MinerU。CLI直接使用本地模型，首次加载占内存/显存，请勿并发评测。

评测沿用`.env`/进程中的现有配置，不强制关闭Fallback，也不修改配置。关闭时每题最多一次请求；开启`custom→deepseek`备用时，每题最多两次，须预留两次预算。仅网络错误、HTTP 5xx/429允许切换一次；401/403/其他普通4xx、坏JSON、非法引用、证据不足均不切换。两方接收相同原Prompt及最终证据，各用独立密钥/模型。没有自动LLM裁判。`--max-cloud-calls`为本run累计请求上限，包含中断题的保守占额；空证据可不调用LLM，失败/超时仍可能已收费。

```powershell
$ragPerCaseMax = [int]$ragPlan.plan.policy.llm_calls_per_case_max
python scripts/evaluate_stage34.py --step evaluate --run $ragRunName --execute --allow-cloud --max-cloud-calls $ragPerCaseMax --limit 1
$ragFirstExit = $LASTEXITCODE

# 先显示安全诊断，再决定是否续跑；不输出Key、Prompt或上游错误正文。
if (Test-Path "$ragRun/cases") {
    $ragFirst = Get-ChildItem "$ragRun/cases/*.json" | Select-Object -First 1 |
        Get-Content -Raw -Encoding UTF8 | ConvertFrom-Json
    $ragFirst | Select-Object status,error_code,failure_stage,actual_provider,actual_model,index_check,source_check
    $ragFirst.llm_attempts | Format-Table ordinal,provider,model,status,http_status,error_code -AutoSize
}
if ($ragFirstExit -ne 0) { throw "首题未通过，停止续跑；查看上述失败阶段及逐次请求" }
Get-Content "$ragRun/summary.md" -Encoding UTF8
Invoke-Item "$ragRun/review_packet.md"
```

主服务正常只记录一条`custom`；若本次仍返回503，应再记录一条`deepseek`，成功响应的`actual_provider=deepseek`。备用失败时保留`DEEPSEEK_*`及对应HTTP状态，不再抹成通用错误。无需为了验证切换故意制造真实故障。`COMPLETED`表示流程完成，答案可能是合理拒答；`MANUAL_REVIEW_PENDING`或`EXECUTION_INCOMPLETE`表示评分待办/剩余题未执行，不要求100%正确率。实际异常、来源失败、中断或索引变化返回非零退出码。检查首题回答、最终证据及来源核对后再续跑。

## 3. 用户确认首题后续跑

```powershell
$ragCaseCount = $ragPlan.plan.cases.Count
$ragCallBudget = $ragCaseCount * [int]$ragPlan.plan.policy.llm_calls_per_case_max
# 默认20题，备用开启则本run累计最多40次请求；首题已用次数包含在其中。
python scripts/evaluate_stage34.py --step evaluate --run $ragRunName --execute --resume --allow-cloud --max-cloud-calls $ragCallBudget --limit $ragCaseCount
```

每题先保存`STARTED`，每个主/备用请求前保存`llm_attempts`意图，包括顺序、提供方和模型；写盘失败不得发送对应请求。后续意图及最终结果保存此前安全错误码/HTTP状态/耗时。`llm_attempted`保留兼容旧记录。完成、失败或中断题都不自动重发；续跑只执行未开始题。中断题保守占完整单题上界，开启备用为两次，占额不代表已实际计费。LLM错误、来源核对失败或索引变化停止本轮。确需重测时，人工核对后使用新run、重新确认预算并保留旧记录，不删除检查点绕过保护。

每题前后通过短只读观察核对原200文档索引身份；有引用时另调用原`EvidenceService.resolve_answer`核对字符、Hash、来源和页语义。这不是长期锁库或不可变数据库快照。来源失败仍保存原答案并单独标明错误，不通过重问掩盖。运行时冻结双方模型、端点Hash、预算/超时、主备开关、Prompt及主要代码Hash，变化后不能续写原run。旧单请求计划遇到开启备用的新配置会在加载模型前报`LLM_POLICY_CHANGED`；旧run仍可离线生成报告，不改其原始结果。

### 3.1 修复后仅补跑未调用LLM的检索失败题

`stage34_run_03`的20题中，17道单文档题执行完成，3道全库对照题在BM25的首批来源摘要查询被取消（57014），这3题没有LLM请求。SQL已改为物化本批全部Chunk来源列，外层直接使用批次，避免只物化ID后又连接整张Chunk表。10秒语句/2秒锁等待、完整来源Hash及200份检索范围保持。实际超时是否消除，须以用户复测为准。

代码指纹已变化，直接续跑`stage34_run_03`会触发`RUNTIME_CHANGED`。新增`--step retry-plan --retry-source-run stage34_run_03`，在独立新run里只选择`FAILED/RAG_RETRIEVAL/retrieval`、索引核对通过、明确没有请求意图/响应/最终证据的题。保留原问题、全库范围、索引快照和主备策略，并绑定父计划及失败记录Hash；不重新抽样、不复制成功答案。付费/请求未知/中断/其他阶段失败不能通过此入口重问。旧run的17题与失败现场不变；补跑报告的分母仅为3题，不能当成原run在同一代码下完成20题。

在项目根目录的`dkf-agent`环境执行。以下是完整新增操作，不用重跑先前20题；CLI直接使用本地模型，无需CPU/GPU Worker：

```powershell
& {
    $ErrorActionPreference = "Stop"
    python -m pytest tests/test_bm25.py tests/test_bm25_retriever.py tests/test_hybrid_retriever.py tests/test_stage34_evaluation.py tests/test_stage34_runtime.py tests/test_stage34_fallback.py -q -ra
    if ($LASTEXITCODE -ne 0) { throw "回归未通过，停止补跑" }

    # run_04必须未使用；计划准备仅操作本地JSON，不连接数据库或调用模型/云端。
    python scripts/evaluate_stage34.py --step retry-plan --run stage34_run_04 --retry-source-run stage34_run_03 --execute
    if ($LASTEXITCODE -ne 0) { throw "补跑计划准备失败" }
    $ragRetryPlan = Get-Content "results/stage34/stage34_run_04/plan.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $ragRetryPlan.plan.cases | Select-Object case_id,dataset,scope | Format-Table -AutoSize
    if ($ragRetryPlan.plan.cases.Count -ne 3 -or $ragRetryPlan.plan.policy.fallback -ne $false) {
        throw "本次预期3题且关闭备用；请核对计划后再设请求预算"
    }

    # 此处开始真实问答：问题和最终证据交给官方DeepSeek，本补跑run累计最多3次请求。
    python scripts/evaluate_stage34.py --step evaluate --run stage34_run_04 --execute --allow-cloud --max-cloud-calls 3 --limit 1
    if ($LASTEXITCODE -ne 0) { throw "首道补跑失败，停止；查看bm25.failed及cases中的安全诊断" }
    Get-Content "results/stage34/stage34_run_04/summary.md" -Encoding UTF8
}
```

首题预期`完成1/3；失败0；未执行2`，日志应出现`bm25.snapshot_finished`，不再卡在首批`SOURCE_HASH_BATCH`；冷启动仍需读取完整语料并建索引，不承诺固定耗时。检查首题回答及来源后，手动继续剩余两题：

```powershell
python scripts/evaluate_stage34.py --step evaluate --run stage34_run_04 --execute --resume --allow-cloud --max-cloud-calls 3 --limit 3
Get-Content "results/stage34/stage34_run_04/summary.md" -Encoding UTF8
```

预期`完成3/3；失败0；中断0`，来源检查通过，人工指标仍待评分；合理拒答计执行完成。如果仍失败，保留新run，提供`bm25.failed`的`operation/batch_index/chunk_count/SQLSTATE/耗时`及新题安全错误，不删检查点或反复发送已付费题。旧run和补跑run独立保留；不能把新run的3/3描述成全部20题正确率100%。

## 4. 人工评分与离线报告

本地文件包含业务正文，应限制访问，不公开上传：

| 文件 | 用途 |
|---|---|
| `plan.json` | 问题、实际范围、官方依据、200文档质量/索引快照 |
| `cases/*.json` | 同次最终证据、答案、引用、来源Trace、逐方请求/实际响应方、耗时和失败阶段 |
| `review_packet.md` | 人工阅读材料，包含全部最终证据，不代替完整原件 |
| `reviews.json` | 人工评分；报告只补新题，不覆盖已有判断 |
| `summary.json/md` | 执行覆盖、人工指标、缺口与分阶段耗时 |

对照原件和`review_packet.md`，在`reviews.json`填写`reviewer`、`notes`、实际`expected_behavior`及标签。标签允许`fact/numeric/unit/table/formula/ambiguity/insufficient_evidence`。

- `answer_accuracy`：整题正确填`passed=1,total=1`；部分正确或答错填`0,1`。须有可信参考或人工核对；`expected_behavior=unknown`不能评分。合理拒答可判对，不合理拒答判错。
- `faithfulness`：分母是回答中的实质性事实数，分子为最终证据完整支持的事实数；核对数字、单位、条件和日期。拒答保持`null/null`。
- `citation_accuracy`：分母是受审“事实—引用”关系数，分子为原文支持且文件/页或章节正确的关系数。未知页语义不补造物理页；拒答保持`null/null`。

未评分项保持`{"passed": null, "total": null}`。评分绑定原结果Hash，答案变化后不能沿用旧判断。来源结构通过不自动给语义分，满足OCB文件依赖不算回答正确。

```powershell
python scripts/evaluate_stage34.py --step report --run $ragRunName --execute
Get-Content "$ragRun/summary.md" -Encoding UTF8
```

报告保留计划总数、未执行、失败、中断、拒答、已评题数及分母，JSON另按OHR/OCB和实际问答范围分组。`llm_attempts_recorded`计逐请求意图，`llm_requests_by_provider`计各方请求，`llm_response_cases_by_provider`计实际响应方，`fallback_cases`计启动备用的题数；旧记录缺提供方时保持未知，金额仍需人工核对账单。人工比率仅针对已评项，不代表全部4087题。耗时按完成题给出检索、重排、合成、来源反查和总耗时，包含冷启动，不当成稳定性能结论。90%忠实度是目标，未达标如实记录，不靠丢弃失败或空拒答提升。

下一步返回首题/首轮结果，再按实际生成、召回、来源或拒答问题改进，并补尚未覆盖的数值/表格/公式与歧义案例。多轮体验及其他专项能力留到后续。
