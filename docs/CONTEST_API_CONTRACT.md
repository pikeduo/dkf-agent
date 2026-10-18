# contest-agent-v1：冻结契约与后续适配设计

## 状态与权威来源

版本`contest-agent-v1`以[DTO源码](../contracts/contest_v1.py)和[Tool Protocol](../contracts/tools.py)为准，阶段2未修改公共字段/版本或Protocol。独立竞赛服务已编写会话/授权、SQLTool及ClarificationTool适配，但测试与真实运行均待用户验收；统一Agent HTTP、RAG/Formula适配和自动跨源规划器仍未实现。Pydantic只检查结构一致性，不能证明SQL真的执行、引用真的核对、预算真的预留或访问真的有权。具体配置、身份及限制见[阶段2竞赛后端](CONTEST_BACKEND.md)。

不兼容字段/状态变化升级协议版本；原知识API不因此改变字段或行为。新增统一入口在实现前必须遵守本契约，不能静默把参考包的旧结果当成v1成功响应。

## 路由与现有能力

| 浏览器路径 | 服务与状态 |
|---|---|
| `/api/admin/knowledge-bases`及其`/{kb_id}/documents` | 原知识服务现有GET/POST；上传multipart字段`file`支持1～50份，parse_mode默认auto |
| `/api/admin/documents/{doc_id}`及`/status`、`/blocks`、`/chunks` | 原服务现有详情/状态/分页接口；retry、reindex、reparse及DELETE仍沿用原管理契约 |
| `/api/knowledge/search` | 现有POST：kb_id、question、doc_ids及top_k；success/empty与原Evidence直传 |
| `/api/knowledge/query` | 现有POST：搜索参数加allow_cloud默认false；直传原RAGAnswer |
| `/api/contest/*` | 后续代理到8001的`/api/*`；阶段2已实现8001领域路由，但Vue代理尚未配置 |
| 8001 `/api/query`、`/api/catalog`、`/api/sessions`及`/api/traces/{trace_id}` | 已编写问数领域API，复用冻结AgentRequest/AgentResponse；会话/许可/reset完整路径见阶段2说明，尚未运行验收 |
| `/api/contest/agent/query` | 拟议统一POST入口，本轮不注册；请求/响应使用下述v1，仅为未来适配设计 |

统一入口未启用时前端显示能力未接入，不发送请求后用本地数据模拟成功。EvidenceService来源反查、Formula及KnowledgeAgent目前为Python入口，不存在新的来源/公式HTTP接口。后续只增加薄适配时另行记录真实路由与鉴权。

原知识接口当前没有会话鉴权，仅限本机/可信内网；同源代理不会自动增加访问控制。竞赛服务已编写独立入口Bearer令牌及会话私钥校验，默认空令牌关闭业务API；单入口令牌授权全部注册源，尚无多租户身份系统，限本机单可信操作员。

## 请求：AgentRequest

| 字段 | 规则 |
|---|---|
| version | 固定contest-agent-v1 |
| client_request_id | 非零UUID；用于用户本次操作的重入识别，不能冒充服务端run/trace身份 |
| session_id | 公共DTO允许首轮null；阶段2领域API要求先POST sessions取得会话，UUID不是凭据 |
| question | 原始非空问题，最多4000字符；不得在补参时拼接成新的、无法核对的原始问题 |
| intent | sql/rag/formula/auto；auto保留并返回unsupported，不运行跨源自动规划 |
| sql_scope | db_id、sub_db均是服务端注册表标识，不接受DSN、文件路径、任意表名或table白名单 |
| knowledge_scope | 非零kb_id；doc_ids省略/null表示本库合格文档，非空最多100个；空/零身份/重复值拒绝 |
| formula | 显式doc/chunk/block与可选旧定义、原UserVariableInput/EvidenceVariableInput；必须在知识范围内 |
| clarification | 原clarification_id、reply_to_run_id、revision与逐题答案；选项ID或自由文本二选一 |
| cloud | 默认关闭的CloudAuthorization，详见费用边界 |

sql只带sql_scope；rag/formula只带knowledge_scope，formula还须选择原公式。auto可保留两个范围用于后续版本适配，但本阶段不处理。客户端不能传confirmed_slots、provider URL、Key或服务端授权表；额外字段拒绝。

阶段2服务端入口顺序：身份/会话所有权 → client_request_id重入检查 → 固定范围/目录快照 → 待答版本/选项核对 → 意图/Linking与必要澄清 → 云许可及调用前预算预留 → 生成/校验/执行。未解决歧义先返回，不产生模型或SQL执行。请求和会话范围不一致、问题过期、选项越界或跨会话拒绝；修正问题/条件先reset清空旧确认与许可，改变数据源必须新建会话。

认证令牌只走授权请求头，不放URL、请求模型、Trace或日志。传入的session_id、reply_to_run_id、consent_id均须与服务端记录核对；不因结构合法而信任。

## 澄清与RunContext

服务端创建request_id、session_id、run_id、trace_id并回显client_request_id；阶段2的scope_fingerprint为完整固定SourceSpec目录快照的SHA256，同一会话不热切目录。所有工具共享同一RunContext，不在各工具另建无关联Trace。补参后的新请求有新run/trace，安全快照保存reply_to_run_id关联并继续原任务范围与预算。request_id与HTTP的X-Request-ID一致，客户端同名头不作授权依据。

ClarificationPrompt含clarification_id、当前reply_to_run_id、递增revision、questions及真实schema_links。每题包含question_id、slot、prompt、options和allow_text；option_id对应的业务值保存在服务端。回复需核对所有待答问题，部分回答保留待答状态，不调用SQL；陈旧回复/重放不得重复收费或执行。

ConfirmedSlot仅由服务端生成，保留slot、value、source_request_id及可选clarification_id/revision。原问题中明确条件使用revision=0；澄清条件必须保留非零clarification_id和版本。工具必须将全部确认条件传入Schema Linking、SQL生成及已授权修复，并核对最终SQL约束；未知/无法应用条件继续澄清，不能忽略条件后报success。

## 云外传、费用与执行边界

CloudAuthorization默认allow_cloud=false、max_calls=0、max_total_tokens=0。开启须提供服务端核实的consent_id、注册provider_id、正调用/Token预算；协议上限4次/32768 Token，服务端只能进一步收紧。预算覆盖同一许可下全部意图、Linking、生成、修复、RAG/Formula调用，不按Tool或多轮重新归零。

许可需明确接收方、外传数据范围与费用；SQL调用可能外传问题、选中DDL/别名和确认条件，RAG/公式会外传原服务允许的最终证据。MinerU上传/解析许可独立，不因勾选问答LLM而自动授权新的文档收费处理。provider_id是服务端配置选择器，不允许任意地址。

每次调用前在会话锁内预留累计调用数及输入/最大输出Token额度；无法满足预算时停止，不能先请求后补授权。阶段2按UTF8字节四倍加封装余量与输出额度保守预留，不是实际Tokenizer或账单保证。失败、超时或响应丢失仍保守占用预算；用量未知不虚构统计并关闭额外调用，完整的本次候选可继续校验。许可与状态仅单进程内存、TTL内有效；启动必须workers=1。max_total_tokens不是账单金额，前端不得虚构价格/节省费用。

无传输层重试、自动供应商选择、自动Fallback或不确定请求重放。SQL最多一次**显式**语义修复，allow_sql_repair=true且剩余预算足够；SQL执行未知不修复/重放。原RAG/Formula若启用了旧Fallback，未来适配器应在调用前拒绝不符合本契约的配置，不能更改原服务全局语义后静默多收费。

阶段2SQL路径已编写只读账号/事务、全树校验、单句、限定Schema/表列/函数及实际基础表/外键核验、固定pg_catalog search_path、默认15秒最高30秒statement_timeout和最多500行输出。禁止写入/DDL/COPY/SELECT INTO、写CTE、系统目录越权、危险/未知函数和允许集之外对象。DTO不执行保护，真实PG权限与解析兼容仍待验收。额外修复只处理确定的候选SQL技术错误（包括EXPLAIN发现的分组/类型错误），不对超时/空结果/执行未知或权限问题重放。

## 响应与可解释证据

AgentResponse包含version、context、status、单个带kind的result以及trace。顶层不设可任意填充的“成功答案”字段；页面从对应真实结果渲染。终态映射如下：

| status | 结果 |
|---|---|
| answered | SQL success / 原RAG success / 原公式success；公式定义完成与计算完成仍按原result.version区分 |
| empty | SQL已成功执行且返回0行，保留真实列信息 |
| needs_clarification | ClarificationPrompt；不携带查询表格或假执行记录 |
| insufficient_evidence | 原RAG/Formula证据不足；禁止用服务异常伪装证据不足 |
| need_variables | 原公式缺参列表，不产生计算值 |
| unsupported | 未实现能力标识，或原unsupported_formula |
| failed | 固定FailureResult，或带安全诊断的SQL failed；不发布半份表格/答案 |

### SQLResult

- scope是实际执行的注册范围；schema_links逐项携带mention、selected/ambiguous/unmatched、候选表/列及来源依据。每个依据保留kind、reference_id、catalog_fingerprint，对应Schema/alias/FK/confirmed_slot/value_match真实记录。候选匹配score未知留null，分数不是概率。
- attempts最多2项且从1连续。generated_sql是生成器实际输出；executed_sql是最终送入数据库的语句。若实现使用绑定参数，parameters按参数名保留实际data_type及无损value；阶段2使用已校验SQL字面量，parameters为空，不虚构绑定记录。Schema限定、列展开及LIMIT包装会使两种SQL不同，不能用生成语句代替执行证明。每项attempt须对应Trace的sql_attempt及生成/校验/执行状态，同次同类步骤唯一。前端只以文本代码块展示并转义。
- validation为passed/rejected；execution为not_run/succeeded/failed/unknown。被拒绝SQL没有executed_sql和参数；未知执行不能展示空集、成功或自动第二次执行。未生成语句时attempts可空，使用固定错误码。
- columns按真实游标顺序给name/data_type，允许同名列；rows最多500行，returned_rows必须等于实际行数，每行列数必须匹配。数值、Decimal、大整数、日期/时间以无损字符串传输，布尔保持布尔，NULL保持null，其他类型不擅自字符串化或截断。未知/超限类型须显式拒绝结果。
- truncated只在实际读取到第501行时为true，显示前500行；没有额外取样不能猜truncated或全表总行数。结果顺序完全沿用执行结果，无ORDER BY不承诺稳定排序。
- 只有最后一次已成功执行、Schema歧义已解决、表格与计数一致才可success/empty。DTO不能验证数据库真值；后续执行器必须从同次游标/校验结果构造这些字段。

### RAG与Formula

RAGResult.result直接复用原RAGAnswer，保留完整Citation、dense/rrf/rerank三类分数及metadata/source_spans。来源快照默认snapshot_only；只有调用原EvidenceService并逐项核对相同kb/doc/chunk后才可设置verified及附EvidenceTrace。核验失败不能猜页码或把旧快照称为当前已验证。

PDF展示物理页，图片展示第1页，Office/Native文本展示已有逻辑页；页语义来自原metadata/EvidenceTrace，未知显示未知。保留source_spans的字符/Hash/Block身份，HTML表格及原文需安全转义。PARTIAL_READY只表示可靠块可检索，不提升为完整质量验收通过。原查询/分页/上传状态详见[知识API](KNOWLEDGE_QUERY_API.md)、[文档管理](DOCUMENT_MANAGEMENT.md)及[Citation](CITATION_EVIDENCE.md)。

FormulaResult直接复用FormulaSemanticResult或FormulaCalculationResult、原参数来源/缺参及manual_pending，不凭常识补值、不伪造引用分数。结构化成功不代表公式语义正确，机械计算成功不代表单位/业务期间已核验。

### Trace与错误

TraceStep仅含step_id、tool、固定operation、status、sql_attempt、elapsed_ms、llm_calls及固定error_code；全部继承响应context身份。允许步骤为意图、Schema Linking、澄清、SQL生成/校验/执行、RAG检索/合成及公式提取/绑定/计算。SQL生成/校验/执行步骤必须指明sql_attempt，其余步骤该字段为null；SQL响应逐次检查生成、校验与执行状态对应。实际未走的步骤不伪造成功，未测耗时/用量为null；succeeded与错误码矛盾、跨Tool步骤和重复step_id拒绝。每次云请求只计入实际发起步骤，不能跨层重复累加。

Trace用于可解释业务过程，不输出模型隐藏思维链。SQL正文/绑定值、原文证据仅属于会话所有者的业务响应，不能进入日志或安全Trace快照。日志复用dkf.business白名单、路由模板、资源身份/状态/数量/耗时；不记录请求头、查询串、正文、SQL、连接信息、私有路径或上游异常。阶段2注册contest-api角色及UUID trace_id，所有进程仍写相同每日文件/固定锁，不新增日志设施。

ErrorCode为固定枚举，前端固定中文映射；阶段2入口已编写输入422、未认证401、范围/许可403、过期澄清409、依赖不可用503及上游失败502的安全映射，不能返回str(exc)。needs_clarification/empty/insufficient_evidence不是HTTP内部异常。Trace/会话读取需入口令牌和会话私钥核验，不能仅凭UUID取得其他会话结果。

## Tool边界与实现门槛

| Protocol | 输入 / 输出 | 后续适配责任 |
|---|---|---|
| SQLTool.run | AgentRequest + RunContext → SQLResult或ClarificationPrompt | 复用竞赛Linking/SQL链，强制传入confirmed_slots，落实只读/范围/预算 |
| RAGTool.run | 同上 → RAGResult | 原Query/Evidence薄适配；范围/原问题/云许可透传，完整引用不裁剪 |
| FormulaTool.run | 同上 → FormulaResult | 原提取/绑定/计算服务，显式来源/缺参；不重写原LangGraph |
| ClarificationTool.propose/accept | 请求/上下文/真实候选 → 问题或确认槽位 | 会话所有权、范围、问题版本与选项校验；不执行SQL或发起云调用 |

Protocol中的run是同步接口边界，不指定线程/HTTP实现。后续适配器应核对request与context的client/session/范围一致，检查请求intent与工具类型，异常由入口映射为安全FailureResult；不能直接暴露内部服务或以客户端context作为可信输入。统一规划器、自动工具选择和跨源融合均不在本轮实现范围。

阶段1结构及知识修复验收见[竞赛整合说明](CONTEST_INTEGRATION.md#阶段1人工验收)，阶段2配置、隔离测试及真实联调步骤见[竞赛后端说明](CONTEST_BACKEND.md#用户手工验收)。SQL权限/费用/Trace访问控制和真实知识来源均需用户运行验收；契约单测通过不能替代真实服务验收。
