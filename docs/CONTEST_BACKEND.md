# 竞赛问数后端：阶段2实现与人工验收

## 交付状态

工作分支仅为`feat/contest-integration`。阶段2已编写独立FastAPI问数服务、Schema Linking、多轮澄清、受限PostgreSQL执行及安全Trace；Codex未运行pytest、启动服务、查询真实数据库、安装依赖或调用模型。用户定向测试反馈196通过、1失败：regclass的DataType.this为字符串，原实现访问.name导致AttributeError；现已改为只接受明确内置枚举允许集，其他节点安全返回SQL_REJECTED，并补充未知节点/系统类型拒绝与正常CAST回归，待用户复测。阶段2自动测试及真实联调仍待用户验收，阶段1人工回归与真实GPU Reindex仍待验收。阶段3已编写Vue3及同源开发代理，运行未验收；统一Agent HTTP与自动跨源规划器未注册。

只读审阅`main/f4b092e`：该提交主要增加旧Office缓存离线升级、分组恢复及冻结版本日志，并删除交付ZIP。这些对后续知识恢复有用，但不是SQL服务前置依赖；本阶段不移植、不merge、不cherry-pick，知识业务及原验收状态保留。

## 文件与参考适配

| 本地文件 | 实际职责 / 参考取舍 |
|---|---|
| `contest_backend/config.py`、`registry.py` | 合并参考config/data_access/registry及catalog多入口，采用唯一受限目录，复用已有psycopg |
| `schema_linking.py` | 适配参考table_matcher/schema_linker/fk_graph的目录、别名、词形和关系路径思路；不用networkx，不运行自动值探查或默认取第一候选 |
| `generation.py`、`llm.py` | 适配SQLGenerator/Repairer的SQL围栏及提示构造；完整传入confirmed_slots，不带参考示例数据；一次真实HTTP请求，无auto供应商/重试/Fallback |
| `sql_policy.py`、`execution.py` | 保留参考生成→校验→执行分层，改为SQLGlot全树/表列/条件校验与psycopg3只读角色、实际目录和资源核验 |
| `service.py`、`state.py` | 替换参考workflow漏传槽位的恢复流程，固定原问题/范围/问题版本；本地保守意图判定，不运行跨源调度 |
| `tracing.py`、`main.py` | 实现冻结DTO/Tool边界、独立API及安全步骤记录，不复制含输入输出的JSON Trace |
| `.env.example`、`registry.example.json` | 专用安全默认值与空目录模板，不附真实数据、映射、模型或密钥 |
| `contest_backend/tests/` | 隔离Service/Policy/HTTP/模型传输/psycopg边界用例，均未执行 |
| `app/core/business_logging.py` | 仅增contest-api角色、两类事件及UUID trace_id白名单，不改知识业务 |
| `environment.yml`、`.gitignore`及相关文档 | 声明用户已确认的sqlglot，忽略本地竞赛目录，记录部署与验收 |

`contracts/contest_v1.py`、`contracts/tools.py`的字段、版本与Protocol未修改。`SQLToolAdapter.run`、`ClarificationService.accept/propose`适配原协议；RAG Tool、Formula Tool仍为冻结边界，原LangGraph Knowledge Agent未改。

## 本机配置与数据源

仅由用户在已有`dkf-agent`环境手动操作。不要重建环境、启动迁移或将原知识库管理员账号用于SQL执行。

```powershell
Set-Location 'D:\python project\dkf-agent-main'
conda activate dkf-agent
python -m pip install 'sqlglot>=26.33,<27'
if (-not (Test-Path contest_backend/.env)) { Copy-Item contest_backend/.env.example contest_backend/.env }
New-Item -ItemType Directory -Force data/contest
if (-not (Test-Path data/contest/registry.json)) { Copy-Item contest_backend/registry.example.json data/contest/registry.json }
```

独立配置文件为`contest_backend/.env`，不读取根知识服务`.env`的同名业务项，也不修改其中已有值。两服务的日志目录需指向同一本地目录，复用原每日文件/锁；`.env`全局忽略，本地目录`data/contest/`也忽略。所有`CONTEST_`变量与顺序见专用模板：

| 配置组 | 变量及默认边界 |
|---|---|
| 入口 / 状态 | `CONTEST_API_TOKEN`空时关闭业务API；`CONTEST_SESSION_TTL_SECONDS=1800`、`CONTEST_MAX_SESSIONS=128` |
| 日志 | `CONTEST_LOG_LEVEL=INFO`、`CONTEST_LOG_DIR=logs`；不启用第三方请求调试 |
| 数据库 | `CONTEST_DATABASE_URLS={}`，JSON键为connection_id，值必须为`postgresql://`或`postgres://`专用只读DSN；不用SQLAlchemy的`+psycopg`前缀 |
| 查询资源 | `CONTEST_STATEMENT_TIMEOUT_MS=15000`（最高30000）、`CONTEST_CONNECT_TIMEOUT_SECONDS=5`（最高10）、`CONTEST_MAX_PLAN_COST=100000`、`CONTEST_MAX_RESULT_BYTES=1000000` |
| 模型 | `CONTEST_LLM_PROVIDER_ID=contest`；`CONTEST_LLM_API_BASE_URL`、`CONTEST_LLM_API_KEY`、`CONTEST_LLM_MODEL`由用户填写；基地址仅可信HTTPS，无凭据/查询串/片段，代码追加`/chat/completions` |
| 模型资源 | `CONTEST_LLM_MAX_OUTPUT_TOKENS=1024`、`CONTEST_LLM_TIMEOUT_SECONDS=30`；不继承原知识LLM的Fallback或Key |
| 目录 | `CONTEST_REGISTRY_PATH=data/contest/registry.json`；相对路径始终以项目根解析，不因启动目录改变 |

注册表由管理员根据**已存在且授权**的真实数据库填写，不自动建表、导入CSV或复制参考Chinook。以下只是结构说明，标识和列必须替换为实际目录；空模板返回空目录而不会伪造演示源。

```json
{
  "version": 1,
  "sources": [{
    "db_id": "business", "sub_db": "sales", "connection_id": "business_readonly",
    "schema_name": "contest_data",
    "tables": [
      {"name": "sales", "columns": {"id": "int4", "amount": "numeric", "customer_id": "int4", "year": "int4"}},
      {"name": "customers", "columns": {"id": "int4", "name": "text"}}
    ],
    "aliases": {"销售额": ["sales.amount"], "客户": ["customers"]},
    "foreign_keys": [{"id": "sales_customer", "from_table": "sales", "to_table": "customers", "pairs": [["customer_id", "id"]]}],
    "conditions": [{"slot": "year", "table": "sales", "column": "year", "label": "请确认统计年度", "operator": "eq", "value_type": "integer", "required": true}]
  }]
}
```

一对`db_id/sub_db`只能对应一个连接和Schema，不接受客户端DSN、路径或授权列表。列类型用PG实际内置类型名，允许bool/int2/int4/int8/numeric/float4/float8/text/varchar/bpchar/date/time/timetz/timestamp/timestamptz/uuid。别名可关联多个候选，复合外键`pairs`保留全部列对；不支持自连接。业务条件支持eq/gte/lte及text/integer/decimal/date；required条件必问，triggers只在明确命中时启用。原问题仅`slot=值`被确定性提取，其余不猜期间/单位/过滤值。别名和业务口径须由管理员整理，当前词形匹配不等于完整自然语言语义理解。

## API与DTO映射

用户手动启动（必须单进程，禁用含原始URL的默认访问日志）：

```powershell
python -m uvicorn contest_backend.main:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

直接访问8001使用本页路径；阶段3 Vite代理将浏览器`/api/contest/*`重写为这里的`/api/*`，健康路径单独匹配。代理代码已编写，浏览器同源链路仍待用户手动联调，见[前端说明](CONTEST_FRONTEND.md)。

| 路径 | 契约和行为 |
|---|---|
| `GET /health` | 仅alive；database_verified/model_verified均false，不能用它认定查询可用 |
| `GET /api/catalog` | 服务端真实目录、表列、完整外键、别名、业务条件、catalog_fingerprint；不返回DSN或私有目录路径 |
| `POST /api/sessions` | `{"scope":{"db_id":"...","sub_db":"..."}}`；201返回session_id、一次性session_token、固定范围与TTL |
| `POST /api/sessions/{session_id}/consents` | 明确accept_data_transfer/accept_charges均true、provider_id、max_calls、max_total_tokens、allow_sql_repair；返回冻结CloudAuthorization；本操作不调用模型 |
| `POST /api/query` | 冻结AgentRequest → AgentResponse；本领域入口要求先建会话且intent=sql。context包括同HTTP响应X-Request-ID的request_id及run/trace/scope身份 |
| `POST /api/sessions/{session_id}/reset` | 修正问题/条件前清空旧槽位、待答项与费用许可，保留固定源；随后重新授权。改变数据源必须新建会话 |
| `GET /api/traces/{trace_id}?session_id=...` | 会话私钥核验后仅返回安全步骤与request/run/trace/session/reply_to_run关联；无原问题/SQL/结果 |

除health外都要求`Authorization: Bearer <CONTEST_API_TOKEN>`；会话操作还要求`X-Session-Token`。这两个头不进入业务日志或Trace。入口令牌授权全部注册源，是本机单可信操作员模式，尚无多租户用户/角色体系。会话状态、许可和安全Trace仅在单进程有界内存中，重启/过期失效；不支持多Worker或跨进程恢复。

查询请求`cloud`须原样使用该会话consents返回的许可，默认零许可零请求。`question`在澄清期间保持完全相同，`client_request_id`每个明确新操作使用新UUID，同一UUID重入409且不重放。补参使用返回的clarification_id、reply_to_run_id、revision与question_id/option_id；自由文本仅允许服务端开放的条件槽位。部分回答后重新发布剩余问题及新版本，仍不执行SQL。旧revision、跨会话、换源或未reset就换问题均拒绝。Reset不清除旧重入身份，不能用reset重放旧请求。

SchemaLink/SchemaCandidate/LinkEvidence保持冻结DTO。目录的foreign_keys/aliases/conditions附reference_id，可用相同catalog_fingerprint解析引用；外键完整端点与列对来自目录，SQL生成消费同一选定路径，执行前进一步核验实际PG约束。Schema/别名不确定时返回needs_clarification，未生成SQL时不制造attempts。

SQLResult.attempts分别保留模型生成SQL及实际Schema限定、列展开和LIMIT包装后的执行SQL。当前使用安全校验后的字面量，不创建绑定参数，故parameters真实为空；不能宣称已使用参数化业务SQL。表格列/类型来自同次游标，数值无损字符串、日期ISO、bool/null保留；只在第501行存在时truncated=true。SQL失败不产生成功表格；数据库已执行但结果编码/大小无法接受时整次返回FailureResult，并在sql_execute步骤记录失败，不发布半份表格。sql_execute耗时包括目录核验、计划、取数及结果编码，本阶段未拆分独立结果处理operation。

业务失败返回固定FailureResult或SQLResult.failed并保持AgentResponse关联。输入422、入口401、范围/许可/SQL拒绝403、旧状态/重入409、依赖/执行未知503、模型/数据库/结果失败502、未预期500；PG超时对应SQL_EXECUTION_FAILED（API映射502），不回显数据库原文。非SQL/auto请求返回冻结UnsupportedResult、status=unsupported，不能触发自动规划。`/api/agent/query`仍404，尚无RAG/Formula适配HTTP。

## 执行、费用与日志边界

- SQLGlot完整解析一条PostgreSQL查询，校验全部子树；拒绝写CTE/DDL/COPY/SELECT INTO、锁、多句、系统/跨库/非授权表列、未知函数和危险类型。目录限定后展开星号；实际引用表必须等于选定集合。
- JOIN仅允许选定且完整的外键列对，必须确实连接新增右表和已连接左表；拒绝CROSS/USING/NATURAL、自连接及复杂多表子查询血缘。已确认过滤必须在直接SELECT的WHERE顶层AND中核对；不能证明条件覆盖的CTE/UNION/子查询直接拒绝，不忽略条件执行。
- 真实连接使用非超级、无建库/建角色/复制/BYPASSRLS权限的专用只读账号；禁止授权Schema创建权及已登记表的写入权限。实际表必须为非继承、非分区普通基础表，拒绝视图/外部表/自定义列类型；登记外键须在同Schema存在已验证真实约束。
- 事务只读，固定pg_catalog search_path，每语句/锁/事务空闲超时、4MB work_mem、禁并行、EXPLAIN计划成本上限，最终rollback。输出最多500行及结果字节检查。该检查发生在取数后，不是严格内存沙箱；管理员还须在专用角色配置temp_file_limit、连接数、行级权限及实际数据授权。行级隔离依赖数据库RLS/账号，不能把业务澄清值当用户权限系统。
- 模型接收原问题、选定结构、真实关系及确认条件。传输HTTPS、trust_env=false、不跟重定向，只有一次POST。许可和累计预算绑定会话当前任务；失败/超时保守占用，不释放后重试。输出完整但实际用量缺失时可使用本次候选，后续额外调用关闭。
- Token预算为UTF8字节四倍加封装余量与输出额度的保守预留，**不是实际Tokenizer计数或账单保证**。Trace.llm_calls统计实际发起的调用尝试；不显示虚构的费用或Token数。只有确定技术错误、allow_sql_repair=true且预算允许才额外修复一次；校验拒绝、超时、空结果、连接不可用或执行未知不自动修复/重放。
- Trace只存固定步骤、状态、实测耗时/调用尝试及错误码。业务日志复用dkf.business、contest-api角色、每日同文件与固定锁；每步带request_id/agent_run_id/trace_id，HTTP只记模板。SQL/问题/Prompt/确认值/结果/凭据/上游异常都不进入日志或安全Trace；SQL只在获授权单次响应中展示。禁用SQLGlot原文诊断，压低httpx/httpcore日志；部署代理和数据库日志由管理员控制，不能开启含正文的调试转储。

## 用户手工验收

以下均未由Codex运行。先安装已授权的小依赖，在项目根和dkf-agent环境执行隔离用例；这些用例不使用真实DB、云端、GPU、文档或业务数据：

```powershell
python -m pytest contest_backend/tests tests/test_contest_contract.py tests/test_business_logging.py -q -ra
```

预期包含：正常单表与空结果、外键和复合键、字段/多关系歧义暂停、多轮条件进入Prompt和最终WHERE、无许可零调用、预算与单次修复、写入/多句/系统/跨库/越权列拒绝、超时/断连/模型失败、请求身份及Trace脱敏、会话私钥/旧版本/reset隔离。Mock只验证代码边界，不能替代真实验收。SQLGlot声明版本的API/PG解析兼容也须由该步骤确认。

真实联调按顺序由用户执行并判断，**默认先不发放consent，不产生付费请求**：

1. 管理员准备专用只读源和上面的真实注册表；校验Schema/列类型、真实FK、无写权限，行级范围通过数据库账号/RLS落实。不给原知识库账号追加或撤销权限，不自动迁移。
2. 按本页启动命令运行单进程8001；访问`Invoke-RestMethod http://127.0.0.1:8001/health`，仅确认alive。带入口令牌读catalog，核对实际登记表/列/外键；不存在目录应503，空目录应sources=[]。
3. POST sessions取得私钥，在无cloud许可下发送明确单表问题；预期403 AUTHORIZATION_REQUIRED，供应商无新增请求。发送字段歧义问题应先needs_clarification，多轮中尚未解决问题时不得执行SQL/模型。
4. 只有用户另外确认接收方、数据可外传、供应商费用和次数预算后，手工POST consents（首次max_calls=1、allow_sql_repair=false）。将返回cloud原样带入query，预期真实单表/外键查询返回生成SQL、实际执行SQL、同次表格和Trace；与数据库管理工具手工只读结果对照，不能将Mock输出当对照值。
5. 真实问数确认年度等参数，逐轮只提交相应答案，核对最终WHERE及选中字段。修改条件前reset；跨源另建会话。人工确认错revision、错私钥、错范围和重复client_request_id均被拒绝。
6. 在独立验收库验证拒写、多句、越权、只读账号和超时；不在业务库运行破坏性反例。模型失败/DB不可用须安全错误，执行未知不重放。500/501行边界及数值类型用已授权只读样本核对。
7. 在本地检查每日日志和GET trace，只看固定IDs/步骤/数量/耗时，无question/SQL/Prompt/密钥/结果/原始上游错误。HTTP响应允许SQL，持久化日志不允许。费用与失败是否计费最终以供应商账单为准。

阶段2未验收：上述所有用例、真实PG权限/语法/目录、模型提供方连接与费用、真实多轮语义/性能；阶段1Office人工回归/GPU Reindex也仍待验收。阶段3虽已编写Vue3、代理及文档/RAG/页码UI，前端测试、构建与真实联调仍待用户执行，不提升阶段2验收状态。统一Agent运行时未实现。
