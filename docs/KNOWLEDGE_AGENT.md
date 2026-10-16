# Knowledge Agent：有限LangGraph工作流与人工验收

阶段30代码与Mock用例已编写，**尚未执行图、pytest、模型、真实数据库或收费API验收**。此前阶段的待办不自动通过，不推进阶段31。图API按[LangGraph官方说明](https://docs.langchain.com/oss/python/langgraph/graph-api)编写；当前开发环境未发现已安装的LangGraph目录，运行时兼容性须用户验证。

## 职责与入口

`app.agents.knowledge.KnowledgeAgent().run(request)`或`run_knowledge_agent(request)`接收`KnowledgeAgentRequest`/对应字典，返回自有`KnowledgeAgentResponse`。不新增HTTP接口，不改变`/api/knowledge/search`、`/query`或公式服务的原调用方式。

LangGraph仅负责StateGraph、有限条件边、计步和错误终结；项目`KnowledgeTools`是四类服务的薄适配，不需要额外StructuredTool/AgentExecutor，不创建第二套Retriever、VectorStore、RAG Chain或模型。Search/Query复用KnowledgeService→原LlamaIndex链路；公式复用extract_formula和calculate_formula。LangChain仅作为LangGraph所需运行时的辅助层，不运行另一套Agent。

固定路径：validate_input→route_request→Search/Query，或extract_formula→calculate_formula→synthesize_or_return→finalize_response。Search/Query分支也经过synthesize_or_return，但不再调用LLM；原Query本身只进行一次最终Evidence合成。公式返回确定过程说明和完整计算契约，不请求LLM重新解释计算结果。

## 输入与路由

沿用原Knowledge请求的`kb_id / question / doc_ids / top_k / allow_cloud`校验。默认`top_k=30`，是融合候选预算，Search/Query最终仍最多5条；原SQL中的READY、代次、模型及知识库过滤不变。

| intent | 固定行为 |
|---|---|
| auto（默认） | 提供formula选择时走公式，否则普通Query |
| knowledge_search | 原Search，不需要云许可、不执行公式或LLM |
| knowledge_query | 原Query，必须明确allow_cloud=true |
| formula_request | 必须提供公式选择，禁止仅凭问题关键词猜选Block |

`formula`仅用于auto/formula_request，字段为：

- `doc_id / chunk_id / block_id`：已有READY来源中的准确非零ID。
- `user_inputs / evidence_inputs`：完全沿用阶段28参数契约，不猜值，不从问题自动抽取数值。
- `definition`：可选的**已有成功FormulaSemanticResult**，须与所选kb/doc/chunk/block一致。没有定义时提取一次，需要allow_cloud=true；已有定义时跳过提取，直接由阶段29重新核对当前来源/绑定并计算，可保持allow_cloud=false。

显式doc_ids必须包含公式文档和全部参数文档；空数组、越界、互斥意图、未完成旧定义等拒绝。None才表示该库全范围，不代表允许跨库。图再次检查返回证据范围，不删除越界项冒充成功；旧定义/参数的Hash、字符、READY及代次校验仍由原业务服务负责。

第一版不会因“利润”“公式”等关键词额外收费或自动选公式；普通问题即使提到公式也只走Query。非法路由或与明确意图不一致时返回failed/AGENT_ROUTE，不动态执行节点名，不通过检索兜底改变用户意图。

## 状态、结果与安全

State仅为可JSON序列化的原请求、question/kb/doc/intent、selected_route、evidence、answer、formula_results、状态/安全码及计步。无数据库Session、LLM对象、LlamaIndex Node或原始异常；不把内部State当成对外协议，也不保存到数据库、磁盘Checkpointer或远端Trace。

统一结果保留`status / answer / entities / evidence / formula_results / insufficient_evidence`，另有`agent_run_id / selected_route / error_code / step_count`。entities第一版固定{}，不伪造实体识别：

- Query：原RAGAnswer中的Citation、正文、编号、页/章节、metadata.source_spans和分数完整保留，仍执行原回答引用一致性校验。
- Search：原RetrievalResult列表；无匹配为empty，不伪装证据不足或失败。
- Formula：formula_results保存原FormulaCalculationResult，evidence使用原EvidenceTrace（公式及文档参数的唯一Chunk），不捏造检索分数/引用编号；完整raw_formula、变量来源/双端偏移、精确分数、代入步骤和manual_pending状态均保留。
- 缺参为need_variables；公式不支持为unsupported_formula；真正证据不足为insufficient_evidence；Tool异常为failed。failed清空所有业务半成品，不伪装正常不足，不泄露异常原文。来源存在、精确算术或Agent success都不证明业务语义/单位/忠实度已经验真。

`.env`应用运行组增加`KNOWLEDGE_AGENT_MAX_STEPS=8`，允许2～32；每次节点执行计步，含校验、路由、Tool、返回和终结。普通完整路径需5步，公式完整路径需6步，预算不足在Tool前失败；代码测试可通过`KnowledgeAgent(max_steps=8)`覆盖。还有LangGraph recursion_limit兜底，没有开放循环、默认重试、并行收费Tool或自动重放。

固定错误：AGENT_INPUT（请求/配置）、AGENT_PERMISSION（本次云许可）、AGENT_ROUTE、AGENT_STEPS、AGENT_DEPENDENCY、AGENT_TOOL、AGENT_RESULT、AGENT_INTERNAL。底层服务已有安全错误事件沿agent_run_id关联，原文/Key/连接串不进入返回。

日志使用`knowledge-agent-cli`集中角色和原每日日志，只新增agent_run_id、固定selected_route/tool_name/final_status、有界step_count与tool_duration_ms。Agent在本次调用的Context中关联原request_id并在结束时恢复；不记录问题、原式、参数、正文、答案或完整State。调用边界按[官方tracing_context机制](https://docs.langchain.com/langsmith/trace-without-env-vars)显式enabled=False，callbacks=[]，不启用LangSmith远端追踪；用户仍须关闭自行注入的第三方全局Instrumentation/SQL echo。

没有持久化Agent历史、请求幂等键、鉴权、总超时/取消保证或exactly-once。工具调用已收费后进程崩溃/客户端超时，不代表未发生；**不得自动重发**。先核对账户与原日志；已有公式定义应复用，缺参后也不必再次提取。无旧定义可用时重跑提取可能再次收费，必须用户确认。无MinerU调用、新向量生成或业务写入。

## 用户手动验收

### 1. 依赖与Mock

```powershell
conda activate dkf-agent
python -c "import sys; print(sys.executable)"
python -m pip --version
python -m pip show langgraph langchain-core
# 仅缺少时手动安装已声明的LangGraph；不要为此升级PyTorch或下载模型。
python -m pip install langgraph
python -m pip check
python -m pytest tests/test_knowledge_agent.py tests/test_knowledge_agent_tools.py tests/test_knowledge_agent_acceptance.py tests/test_business_logging.py -q -ra
```

依赖下载需要网络与时间，先确认Python/pip均属于dkf-agent；LangGraph会带入其必要的core/运行时依赖，不需要另装全量LangChain集成/默认OpenAI。Codex未执行上述命令。缺依赖导致skip不算通过。

Mock使用真实StateGraph和内存Tool，覆盖普通Query/显式Search、doc范围、公式→计算/缺参/旧定义复用、不足/不支持、四类Tool错误不重试、非法路由、完整路径预算、跨库/跨doc/非有限返回拒绝、引用深复制、纯JSON State、上下文恢复、禁用追踪及CLI安全门。薄Tool单独断言只委托既有Service；不执行真实Parser、SQL、Embedding或HTTP。阶段27～29完整回归命令仍见[公式说明](FORMULA_SEMANTICS.md)，由用户决定执行。

### 2. 只读本地Search

先准备既有PostgreSQL/ACTIVE测试库和READY文档，以及匹配向量的BGE-M3、Reranker、LlamaIndex缓存。停止同卡GPU Worker，8GB显存须人工观察；只做查询不需要另起API/Redis/Worker，不要重新上传或生成向量。

以下命令由用户执行，创建一个新请求文件，不覆盖旧请求/报告：

```powershell
$requestPath = 'docs/test/stage30_agent/search-request-01.json'
if (Test-Path -LiteralPath $requestPath) { throw '请求已存在，请换新版本名' }
New-Item -ItemType Directory -Force -Path (Split-Path $requestPath) | Out-Null
$kbId = Read-Host '已有ACTIVE测试库UUID'
$docId = Read-Host '已有READY文档UUID'
$question = Read-Host '有真实证据的问题'
@{kb_id=$kbId; doc_ids=@($docId); question=$question; intent='knowledge_search'; allow_cloud=$false} | ConvertTo-Json -Depth 100 | Out-File -FilePath $requestPath -Encoding UTF8 -NoClobber
python -m scripts.stage30_agent_acceptance
python -m scripts.stage30_agent_acceptance --input $requestPath --execute --show-content
```

预期selected_route=knowledge_search，完整路径step_count=5；evidence全部属于指定kb/doc，页/章节/原ID/source_spans不变，无LLM/公式提取/新增解析批次。默认无execute不读输入/配置；无show-content只显示安全摘要，不导出证据正文。

### 3. 明确许可后Query/Formula

- Query：另存新JSON，将intent改为knowledge_query；确认本次问题和最终正文可以外传给当前LLM_PROVIDER并接受费用后才把allow_cloud设为true，再执行同一CLI一次。应只有query_knowledge_base Tool，原Citation合法，证据不足保持原判断。未许可应failed/AGENT_PERMISSION且不调用Tool。
- Formula：另建intent=formula_request请求，选择已有准确doc/chunk/block及阶段28显式参数。先人工确认原式、参数主体/期间/单位；首次提取须许可当前LLM并接受一次费用。预期extract_formula→calculate_formula各一次，完整成功/缺参路径6步。缺参不猜值，原始零分母或过期来源应failed/AGENT_TOOL，底层安全码可通过agent_run_id查原业务日志。
- 补参/重复计算：将已有结果中的完整`formula_results[0].formula`复制到新请求`formula.definition`，保留相同来源选择，填实际参数；allow_cloud可false。应跳过extract_formula，只计算并重新核对来源；不存在或无可用定义时不得自动假造。
- 对公式insufficient_evidence/unsupported_formula不执行calculate；对任意Tool异常不额外收费重试。手工核对完整结果/账户费用和原数据库不变，Mock不替代真实验收。

CLI退出码：0=success/empty/insufficient_evidence（后两者并非回答准确验收通过），1=failed/need_variables/unsupported_formula，2=输入文件错误。`--show-content`仅在可信终端使用，输出含原文/公式/数值，禁止提交Git或复制到业务日志。完成后反馈结果，未经确认不进入后续阶段。
