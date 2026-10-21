# 阶段36：性能基线与并发观察

本轮先提供可重复的本地检索测量工具，尚无真实性能结论。阶段35的专项语义问题按用户要求暂缓，原验收状态保留。运行不会上传、重新解析、重建Chunk/向量或改变READY/PARTIAL_READY，也没有LLM/MinerU请求入口。

## 测量范围

- 复用阶段34已冻结的索引身份与官方问题，默认从`stage34_run_03`固定抽取3题。
- 同一知识库内选择10、50、200份嵌套文档范围，分别使用并发1/2；全库范围使用`doc_ids=None`。这不是三个独立物理数据库的容量对比。
- 默认每组1次预热、3题各重复2次：6组共36次测量，另有6次预热。小样本P95仅用于初步观察，不能直接承诺服务SLA。
- 每个规模共用一份Hybrid/BM25实例，线程复用本地模型与原有锁；所有检索仍经过生产搜索的范围和来源校验。HTTP契约、生产默认BM25预算与实例生命周期未改变，所以本次不是HTTP接口压测。
- 计划沿用源run显式BM25预算，不截断全库语料；默认源预算为10万Chunk、256MiB原文、3200万词项。词频、Python对象、模型和并发SQL摘要仍占额外内存，预算不等于进程RAM上限。
- 三种范围共用题单，不借参考答案选择文档；小范围可能没有答案。空候选是正常结果，报告同时显示空结果数和各阶段观测count，不据此计算问答准确率。

## 环境

使用现有`dkf-agent`环境、PostgreSQL、已部署的BGE-M3/Reranker与已有LlamaIndex/NLP资源。不需要启动API、CPU Worker、GPU Worker或Redis。

请手动确认没有导入、重建索引或同卡推理任务，再运行；其他进程会干扰CPU、GPU和内存观察。工具不会停止或重启任何服务，不下载模型。若源阶段34快照已经与当前索引不同，应先核对变更，不能手改Hash强行继续。

## 先执行Mock回归

以下用例由用户执行；内存桩覆盖计时隔离、注入范围保护、调度上限、失败/中断恢复和报告分母，不运行真实文档、数据库、模型或收费API：

```powershell
python -m pytest tests/test_query_performance.py tests/test_stage36_performance.py tests/test_knowledge_search.py tests/test_business_logging.py -q -ra
if ($LASTEXITCODE -ne 0) { throw "回归未通过，停止性能测量" }
```

预期全部通过。因缺少已有依赖而跳过的用例不算通过；此时先核对环境，不把跳过当成完整验收。

## 冻结计划并先测一组

以下整段可以粘贴到PowerShell。失败会停止本段，避免继续读取不存在的报告。`stage36_run_01`必须是新目录；已有记录保留，换新run不要删除旧记录。

```powershell
& {
    $ErrorActionPreference = "Stop"
    $perfRun = "stage36_run_01"
    python scripts/evaluate_stage36.py --step plan --run $perfRun --source-run stage34_run_03 --execute
    if ($LASTEXITCODE -ne 0) { throw "计划未通过，停止测量" }

    python scripts/evaluate_stage36.py --step evaluate --run $perfRun --execute --limit-groups 1
    if ($LASTEXITCODE -ne 0) { throw "首组失败，先查看当前run的summary.md和report.json" }

    Get-Content "results/stage36/$perfRun/summary.md" -Encoding UTF8
}
```

预期：`PLAN_READY`，然后`完成1/6批`，首组`COMPLETED`、索引`MATCHED`，6次测量全部成功。总状态`INCOMPLETE_OR_INVALID`表示还有5组未测，先检查各组状态，不能仅凭总状态判首组失败。`plan.json`包含实际范围、题单和冻结预算；不加`--execute`仅预览，不连接服务、加载模型或写文件。

## 续跑剩余组并查看报告

确认首组成功后执行：

```powershell
& {
    $ErrorActionPreference = "Stop"
    $perfRun = "stage36_run_01"
    python scripts/evaluate_stage36.py --step evaluate --run $perfRun --execute --resume --limit-groups 6
    if ($LASTEXITCODE -ne 0) { throw "续跑有失败或索引变化，保留报告并停止" }

    python scripts/evaluate_stage36.py --step report --run $perfRun --execute
    if ($LASTEXITCODE -ne 0) { throw "报告存在失败/中断项，不能视作测量完成" }

    $perfReport = Get-Content "results/stage36/$perfRun/report.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $perfReport | Select-Object status, validity, new_cloud_calls, gpu_memory, ingestion_performance
    $perfReport.groups | Select-Object documents, chunks, concurrency, status, planned, attempted, successful, failed, empty_successful |
        Format-Table -AutoSize
    Get-Content "results/stage36/$perfRun/summary.md" -Encoding UTF8
}
```

预期6/6组`COMPLETED`、总状态`MEASURED`、索引`MATCHED`、新增云调用0。`MEASURED`只说明计划测量完成，不自动判性能达标或阶段36验收通过。

`--resume`不重试已有失败或`STARTED`组；若旧组失败，后续未开始组可以测量，但旧失败继续影响总状态与退出码。索引观察一旦失败，整个run不再续写测量。修复代码、变更模型/设备/批大小或索引后，应保留旧run并新建运行；不能直接覆盖旧结果。

只需重新生成报告时执行`--step report --execute`，不需要Worker或模型，不会重跑请求。

## 报告口径与下一步

`summary.md`显示总表、分阶段P50/P95与失败摘要。`report.json`补充所有尝试/成功延迟、成功QPS、空结果数、预热明细、初始化耗时、CPU时间、RSS及失败阶段。`groups/*.json`保留逐请求固定阶段数值，不保存问题、命中文本、向量或任意异常原文。

计时有包含关系：Dense包含Query编码；fusion包含Dense/BM25；重排包含模型评分；历史合成包含LLM。不能相加这些父子阶段。预热与初始化单列，不进入测量分位数；`--warmup 0`时首个请求可能含BM25构建成本。请求耗时从工作线程开始计，不包含HTTP和提交前排队；批次QPS包含线程调度与检查点落盘。

RSS只观察本CLI批次前后，峰值为进程生命周期峰值；CPU时间是本进程消耗，不是整机利用率。没有测GPU显存/利用率、数据库服务进程资源、解析入库耗时、故障注入或完整在线RAG并发。阶段34已有成功RAG/LLM耗时单列在`historical_rag`，失败记录数量保留；它们不是本轮同配置数据，不能拼成本轮全链路耗时。

收到真实报告后，先根据阶段耗时和资源确认瓶颈，再选一个改动，用新run在相同题单、范围、并发和环境下比较；代码指纹变更必须记录。三种物理库规模、长表/低质量专项压力、故障恢复与付费RAG并发仍待另行规划及用户验收。本轮不宣称任何优化收益。
