# 阶段3：Vue3竞赛工作台与人工验收

本轮仅在`feat/contest-integration`新增前端及必要文档/忽略规则，不修改两个后端及冻结Python契约，不commit/push。代码已编写，Codex未安装npm依赖、运行测试/typecheck/build、启动服务、真实调用API或模型。阶段2自动测试与真实联调、阶段1Office人工回归/GPU Reindex均继续待验收，不能视作端到端通过。

## 文件清单与参考复用

```text
frontend/
  package.json, tsconfig.json, index.html, vite.config.ts, proxy.ts, .env.example
  src/
    main.ts, App.vue, styles.css
    types/contest.ts, knowledge.ts, contract-checks.ts
    api/http.ts, guards.ts, contest.ts, knowledge.ts
    state/sql.ts, knowledge.ts, library.ts, workbench.ts
    utils/format.ts
    components/Pager.vue, ResultTable.vue, SQLDetails.vue, SchemaLinks.vue,
               TraceTimeline.vue, EvidencePanel.vue, KnowledgeScope.vue,
               KnowledgeAnswer.vue, ParseQuality.vue
    pages/HomePage.vue, ChatPage.vue, SqlPage.vue, KnowledgePage.vue,
          LibraryPage.vue, TracePage.vue, FusionPage.vue
  tests/fixtures.mjs, sql-workspace.test.mjs, api-contract.test.mjs,
        knowledge-workspace.test.mjs
```

修改`.gitignore`、`README.md`、`AGENTS.md`、`docs/CONTEST_INTEGRATION.md`、`CONTEST_API_CONTRACT.md`和`CONTEST_BACKEND.md`；新增本文。新增本地`frontend/.env`仅含与模板一致的两个回环目标，已忽略，不读取/改动根真实配置、密钥、模型或数据。没有自动生成lockfile；用户安装后请审阅`package-lock.json`并随代码提交，之后可使用`npm ci`复现。

复用参考包`App.vue`导航/工作台结构和`styles.css`配色/响应式布局，沿用ResultTable、TraceTimeline、EvidencePanel及各页面展示结构。改为TypeScript并分离API/状态/展示；旧JS/axios接口、Chinook回退、自动重试、旧Trace.steps字段、固定示例和虚假验收统计不引入。直接使用浏览器fetch，隔离测试用Node内置test，无新增测试框架。

## 页面与真实功能

| 页面 | 行为及边界 |
|---|---|
| 总览 | 能力入口与待验收状态；双health只表示进程响应，无模拟指标 |
| 智能问数与澄清 | 真目录→选范围→建会话；原问题锁定；按问题/选项ID及revision回复；部分答案继续needs_user；修改条件先reset；换源建新会话 |
| NL2SQL查询凭证 | 与问数页共用最后一次响应；分别展示generated_sql、executed_sql、参数、校验/执行/修复状态和真实表格；不单独执行SQL |
| 文档知识问答 | 知识库/文档分页和跨页多选；本地search与query分开；allow_cloud默认false、一次勾选只用于当次请求；显示引用/原文/章节/真实页语义 |
| 知识库管理 | 建库、分页目录、1～50份file上传及逐项报告、手动状态/Retry、Block/Chunk分页、向量数量和质量枚举；不将202冒充READY |
| 执行链路 | 当前会话私钥读取trace_id；显示真实状态、耗时、模型请求数和安全码；空步骤/无权/缺记录单独呈现 |
| 人工跨源联动 | 复用当前SQL成功非空结果→用户选择单元格并确认实体→选择文档范围→显式检索/问答；任一源run/单元格变化即失效旧确认及文档结果 |

`answered`显示success、`needs_clarification`显示needs_user，权限/预算/SQL拒绝显示blocked；empty、insufficient_evidence、unsupported和error分开，线上字段不改。SQL数值保持字符串；NULL、空字符串、空结果、500行截断各有展示，不只展示前50行。Schema候选保留表列、分数（可未知）及reference_id/目录指纹，外键按相同指纹的目录列对解释，不猜关联路径。

## 同源路径

| 浏览器请求 | 目标 |
|---|---|
| `GET /health/contest` | `8001 /health`，status=alive |
| `GET /health/knowledge` | `8000 /health`，status=ok |
| `GET /api/contest/catalog` | `8001 /api/catalog` |
| `POST /api/contest/sessions` | `8001 /api/sessions` |
| `POST /api/contest/sessions/{id}/consents` | `8001 /api/sessions/{id}/consents` |
| `POST /api/contest/sessions/{id}/reset` | `8001 /api/sessions/{id}/reset` |
| `POST /api/contest/query` | `8001 /api/query` |
| `GET /api/contest/traces/{trace_id}?session_id=...` | `8001 /api/traces/{trace_id}?session_id=...` |
| `POST /api/knowledge/search`、`/query` | `8000`原路径 |
| `GET/POST /api/admin/knowledge-bases` | `8000`原路径 |
| `GET/POST /api/admin/knowledge-bases/{kb_id}/documents` | `8000`原路径，上传为multipart `file`，同名多份 |
| `GET /api/admin/documents/{doc_id}/status`、`/blocks`、`/chunks` | `8000`原路径，证据接口带limit/offset |
| `POST /api/admin/documents/{doc_id}/retry?parse_mode=...&allow_new_split_parts=false` | `8000`原路径，无请求体；非force |

Vite分别匹配contest/knowledge/admin前缀和两个health，不存在通用`/api`将请求误转到8000；健康不转到不存在的`/api/health`。开发代理只接受配置的两个回环端口。`/api/contest/agent/query`、来源反查/公式HTTP没有注册，页面不发送；未来统一Agent接入位置为`state/workbench.ts`的禁用对象及冻结契约文档。

## TypeScript与后端DTO映射

| TypeScript | 权威Python |
|---|---|
| `types/contest.ts`的SQLScope、CloudAuthorization、RunContext、ConfirmedSlot、Clarification*、Schema*、SQLAttempt/Result、TraceStep | `contracts/contest_v1.py`同名结构 |
| SQLRequest、SQLResponse | AgentRequest/AgentResponse的**阶段2 SQL领域投影**；不声称实现RAG/Formula联合分支 |
| CatalogSource、Session、ConsentRequest、TraceRecord、ContestHealth | `contest_backend/main.py`实际响应、请求及安全快照 |
| KnowledgeBase、Document、Page | `app/schemas/knowledge_base.py` |
| BatchUpload、BatchUploadItem、ParseJob | `batch_upload.py`、`parse_job.py` |
| Management、DocumentStatus、Block、Chunk | `document_management.py`、`chunk.py` |
| SearchRequest/Response、QueryRequest | `knowledge_query.py`，保留doc_ids null与非空列表语义 |
| Evidence、Citation、RAGAnswer及分数来源 | `retrieval.py`、`evidence.py`、`rag.py`，不改原metadata |

运行时guard拒绝坏成功表格、失真数字、错误会话/请求/指纹、无执行凭证、坏引用和假health；非2xx领域响应仍保留真实SQL失败与Trace。来源页语义取`metadata.page_semantics`；Sheet名称用原section，不从文件名猜页。HTML原文/SQL均经Vue文本插值转义，不用v-html。质量只展示白名单枚举、数量、缺页/视觉待审页，不展开私有diagnostics或visual_assets。

## 配置与安全边界

仅新增`frontend/.env(.example)`的`FRONTEND_CONTEST_TARGET`和`FRONTEND_KNOWLEDGE_TARGET`，顺序一致、仅限本机。没有新增后端变量，不把凭据放到VITE_常量。

竞赛入口Bearer和会话私钥仅保存在内存请求头；不使用localStorage/sessionStorage/IndexedDB、URL或本地日志保存业务原文/结果。令牌变更清旧会话。目录失败清选项，无默认源。模型许可由后端签发；预算及修复另行确认，不自动续费。传输/坏响应导致执行不确定时，前端阻止继续发送，用户核对后台后显式重置。所有请求单发，无自动重试或Fallback；120秒客户端等待超时不意味着后端已取消，也不意味着未计费。

知识RAG每次按`allow_cloud`调用原服务。上传/Retry未新增后端授权契约：不勾许可时显式native，勾选才auto；原服务仍限本机可信操作员，前端勾选不能替代用户鉴权/服务端费用控制。已在途任务使用后端冻结配置，前端不能撤销其原许可。Retry固定不授权新增PDF片，不提供Force Reparse。需要这些能力时继续用原管理员流程并人工确认费用。

## 用户手动执行

以下命令均未由Codex运行。请先在项目根检查分支，不要切到main：

```powershell
Set-Location 'D:\python project\dkf-agent-main'
git branch --show-current
git status --short
Set-Location frontend
node --version
npm --version
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm install
npm run typecheck
npm test
npm run build
```

要求Node 22.18+（22.x）或24+，用于内置TypeScript类型剥离和测试器。npm依赖仅含Vue及Vite/TS构建工具，测试无需浏览器/数据库/模型。预期typecheck无错误，隔离测试全部通过、构建成功；安装/跳过或只有静态检查都不算真实服务验收。若安装失败，保留错误及Node版本，不无条件升级环境。

按README准备原知识API（8000）、CPU/GPU Worker及其已部署模型；竞赛后端准备真实只读账号、授权注册表、独立入口令牌及模型配置。已有服务无需重复启动；不新增数据库或迁移。需要启动时由用户分别在PowerShell执行：

```powershell
# 终端1：原知识API；Worker沿原README手动启动。
Set-Location 'D:\python project\dkf-agent-main'
conda activate dkf-agent
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

```powershell
# 终端2：问数API必须单进程；先完成CONTEST_配置与真实只读注册表。
Set-Location 'D:\python project\dkf-agent-main'
conda activate dkf-agent
python -m uvicorn contest_backend.main:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

```powershell
# 终端3：开发代理与工作台。
Set-Location 'D:\python project\dkf-agent-main\frontend'
npm run dev
# 仅当需单独核对两个同源存活路由时执行，不表示查询验收通过。
Invoke-RestMethod http://127.0.0.1:5173/health/contest
Invoke-RestMethod http://127.0.0.1:5173/health/knowledge
```

浏览器打开`http://127.0.0.1:5173`，按以下顺序验收；真实上传、GPU、本地模型加载及收费调用仍需用户选定样本/预算后手工操作：

1. 网络面板核对页面只发同源请求，两个health分别返回真实进程标识；停掉任一API后点击健康刷新显示不可用，目录/API失败不展示示例成功。健康成功不证明DB或模型部署完成。
2. 顶栏输入本地入口令牌，读取真实目录，选源并创建会话。无令牌不发业务请求；目录缺失/空目录分别显示不可用/空。刷新页面后令牌及会话消失，不在浏览器持久存储中残留。
3. 不授云许可先问有歧义的问题，检查needs_user和候选来源。部分答题后仍待澄清；请求携带相同question/session、正确revision/question_id/option_id，无confirmed_slots字段。歧义未解不得有SQL执行成功。
4. 另行确认提供方、数据外传及费用预算后登记许可，默认1次、修复关闭。按后端真实注册表做单表/外键查询，对照实际执行SQL、参数、结果和条件；检查原问题/已确认槽位一致。修改条件先reset，旧许可和槽位消失；换源取得新会话。不得为凑成功反复申请预算。
5. 在已授权只读验收数据中核对空结果、NULL、空字符串、大整数/Decimal、500/501行截断；表格应显示全部已返回行，生成与执行SQL分开。校验拒绝/模型失败/DB断开时不得有假表格；未知执行提示先核对费用，不能自动重发。
6. 在知识页分页选择库及文档，包括第二页，显式空文档列表不能扩大范围。先search，显示证据而非答案；未勾许可query应显示服务端拒绝。另行授权一次后query核对引用编号、原文、PDF物理页、Office逻辑页、Sheet。真实证据不足和服务异常分别显示。
7. 在管理页建库、选一份及多份已授权文档上传；未授权auto时native可能按能力明确失败。核对逐项HTTP/doc_id及状态，202/UPLOADED不能显示READY；人工刷新Block/Chunk/向量数量、PARTIAL_READY/FAILED及解析质量。对原失败/待续阶段仅显式Retry，不整批重传，不新增PDF片。真实GPU Reindex仍按阶段1原验收流程另行执行。
8. 在Trace页用当前会话trace_id读取完整记录，检查run/request关联、前次run、每步状态和真实耗时；null为未统计。空步骤、缺记录、错误凭据均不显示成功，持久化Trace不包含SQL/问题/原文/密钥。
9. 先取得非空SQL结果，在跨源页选择行列并点击人工确认，补充包含该实体的文档问题，显式检索；并列查看来源和引用。切换SQL run/单元格后旧确认及文档结果消失。页面明确人工联动，无统一Agent请求。
10. 检查浏览器控制台无业务日志/原始异常回显，DOM中的SQL/HTML原文没有作为脚本执行；勿将含令牌的网络面板HAR、原文或结果导出提交。

隔离测试覆盖状态、请求DTO、默认许可、拒绝/错误、目录缺失、SQL空/截断/无损值、Trace有/无、双服务故障、分页、上传和异步状态；不模拟DOM渲染、CSS视觉、真实代理转发、GPU、数据库权限或费用账单。浏览器步骤必须人工执行，不能以mock通过替代端到端验收。

## 待验收与后续

2026-10-19仅执行一次`git diff --check`，退出码0，已跟踪改动未发现空白错误，仅有Git的LF/CRLF换行提示。新增前端尚未暂存，不在普通git diff检查范围内；没有执行typecheck、npm测试或构建，不能声称Vue/TypeScript编译已通过。已只读核对分支仍为feat/contest-integration、frontend/.env被忽略；未暂存/提交/推送。

npm安装/lockfile、Vue模板类型、真实代理及浏览器交互、后台模型/PG/Worker、RAG忠实度/页码、实际权限/费用全部待人工验收。生产反向代理、统一Agent/自动规划、公式及来源反查HTTP、多租户鉴权均未实现。先由用户手动执行本页范围并反馈，阶段3结束停止，不自动进入下一阶段。
