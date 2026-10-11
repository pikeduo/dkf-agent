# AGENTS.md

## 0. 禁止 Codex 自行进行文档测试（最高优先级）

**测试执行权属于用户，Codex 只负责代码实现、测试用例编写、人工验收步骤说明。**

- 用户说“实现阶段 X / 完成任务 / 按开发文档继续”，**不是测试授权**。
- Codex **不得自行**运行 PDF/DOCX/XLSX/PPTX/图片的解析/OCR/转换/渲染测试；不得生成/运行真实或 Mock 的文档验收样本；不得运行 `pytest`、`smoke`、完整回归、性能评测或端到端测试。
- Codex **不得自行**调用 MinerU/OCR/DeepSeek/Embedding 云 API，不得为了测试启动数据库、Redis、Celery、模型或反复轮询/重试任务。
- Codex 可以编写测试代码和断言，但**不执行**；允许对本轮改动文件进行至多一次快速 `git diff --check` / Python 语法检查，不安装依赖、不做耗时重试。
- 真实解析、样本上传、云配额消耗、日志导出、结果判断和阶段是否通过，由**用户手动执行并确认**。
- 只有用户**另行明确指定**测试命令、样本范围、联网权限、时间/费用预算时，Codex 才能代跑这一项；授权仅限一次，不转化为默认权限。
- 每轮功能完成后，Codex 应输出：修改文件、静态检查结果、用户手工测试步骤及预期结果、尚未验收事项、中文 Commit Message、下一步建议，并**立即停止**。
- 其他章节提到“自动测试”“至少验证”“完整回归”“API 接入后验证”，只表示需要提供用例/步骤，**不授权 Codex 执行**。

## 1. 项目范围与当前阶段

DKF-Agent（数知融问智能体）当前只开发 `knowledge-service`：可独立运行、测试和演示的非结构化 Knowledge Base Service。未经用户要求，不开发 NL2SQL、Schema Linking、Coordinator、Fusion 或其他成员模块。

- 最新路线：[开发计划](docs/DEVELOPMENT_PLAN_CODEX.md)（V0.9）；历史计划仅供参考，不重新实现已有阶段。

### 当前开发进度

当前阶段：**11B.2：默认 auto、取消 Review；XLSX 优先 Hybrid，提交前受控 Whole 回退及公式保护已实现，待用户验收。**

| 阶段 | 已完成任务 / 当前状态 |
|---|---|
| 0～3 | 工程初始化、Python 3.11、FastAPI、PostgreSQL/pgvector/Redis 连接、Celery 双队列 |
| 4～7 | 知识库数据模型与迁移、管理员 API、文件上传/存储/去重、异步任务及状态查询/重投 |
| 8～9 | 统一 ParsedDocument 契约；TXT、MD、DOCX、文本型 PDF Native 解析及 Block 入库 |
| 10 | MinerU Cloud；图片、扫描 PDF、表格、公式及阅读顺序已完成真实 API 人工验收 |
| 11A | 已实现十三种格式校验、CSV/XLSX Native Parser、六种 Office 显式 MinerU 支持 |
| 配套功能 | 已实现原接口多选上传、中文业务日志及按日期合并日志文件 |
| 11B.1 | 已实现预检、Native/MinerU 自动路由及完整性 Guard；不再使用人工 Review 路由 |
| 11B.2 | 已实现 XLSX Native 主解析、候选过滤/复用、结构化 Chart、单 Bundle/manifest、Anchor Merge 与事务接入；测试仅编写未执行，真实收益待验收 |
| 12 及后续 | 未开展，等待用户确认 |

- 当前迁移 head 为 `0004_mineru_jobs`；“已实现”不等于已完成真实 API 人工验收。
- 成功解析只写 `document_blocks`，停在 `CHUNKING / awaiting_chunk`，不等于可检索或 `READY`。
- 当前仅实施用户授权的 11B.2；未经用户确认，不进入 Parse Cache、Chunk、Embedding、Retrieval、RAG，不扩展 DOCX/PDF/PPT Hybrid；不新增旧 Office Native Parser、LibreOffice 或 GUI 截图链路。
- 当前自动路由规则以本节和用户最新要求为准，开发计划中历史 Review/默认关闭描述不再用于正式路由；第 0 节测试禁令保持最高优先级。

最终目标：预置多格式知识库为主、动态上传增量为辅；完成单文档及全库检索、Dense + BM25、Reranker、DeepSeek RAG、Evidence 和独立 Demo。提交至少 10 份多格式文档、10 个单文档 RAG 用例，答案忠实度目标 ≥90%，分别记录检索、重排、生成耗时并评估性能瓶颈。后续 Delete/Reindex、公式参数识别、结构化与安全计算按计划开发，不提前宣称可用。

## 2. 技术与本地环境约束

- 当前：Python 3.11、FastAPI、SQLAlchemy/Alembic、PostgreSQL + pgvector、Celery + Redis、PyMuPDF、python-docx、openpyxl、olefile、MinerU 官方精准解析 API V4（默认 `vlm`）。
- 后续按计划使用 DeepSeek、BAAI/bge-m3、BAAI/bge-reranker-v2-m3、SymPy、LangChain、LangGraph；Docker Compose 仅为未来整体部署方案。
- 新增技术或依赖须先说明原因和替代方案并等待确认；不得自行引入 Elasticsearch、知识图谱、本地大型视觉模型、新 Agent、Kubernetes、微调或无关大型依赖。

Windows 基础服务由用户手动管理，本节优先于其他文档中的旧部署说明：

| 服务 | 固定环境 |
|---|---|
| PostgreSQL | Windows 原生 17，`127.0.0.1:5432`，数据库 `dkf_agent`、用户 `dkf_user`，通过 `DATABASE_URL` 连接 |
| pgvector | VS 2022 Build Tools **x64 C++** 编译，禁止 x86；`PGROOT=C:\Program Files\PostgreSQL\17`，用户在项目库启用 `CREATE EXTENSION vector;` 并查询 `pg_extension` |
| Redis | Windows 本地后台服务，`127.0.0.1:6379`、`REDIS_URL=redis://127.0.0.1:6379/0`；已有 5.0.14.1 仅用于开发，升级须核对兼容性 |

不得自动安装、升级、编译基础服务或修改系统服务，不自动创建 PostgreSQL/Redis 的 `docker-compose.yml`，不恢复 Docker/WSL 部署；应用只连接现有服务。测试 Redis 前先 `Get-Service Redis` 确认 Running（按实际服务名）；连接失败先查服务、绑定地址/端口、6379 监听、`redis-cli ping` 的 PONG，最后核对应用/Broker/Backend URL，不直接改 Python 代码。

## 3. 开发与维护规则

### 中文说明与安全

- 新增或修改代码必须有适量中文注释；**每个函数都要中文说明**。Python 使用函数体开头的 docstring，其他语言使用函数注释；包含异步函数、方法、辅助/嵌套函数、测试、迁移及代码模板。
- 说明用途，并按复杂度补充参数、返回值、副作用、事务、异常及边界；修改实现同步更新说明，不写无意义逐行注释。
- 保留用户已有修改，不擅自扩大范围。真实密钥、密码、连接串、签名 URL、文档正文和上游错误原文不得进入日志、前端、Celery 消息或异常详情。

### 配置、文档与 Git

- 新增、修改或废弃环境变量，必须同步 `.env`、`.env.example` 及相关使用文档。`.env` 保持忽略，示例只含安全值；两份文件变量名、顺序一致，按用途分组并加中文说明，不零散追加。
- 分组顺序：应用运行 → 数据库与缓存 → 异步任务 → 模型与 LLM → OCR/外部服务 → 存储与可选功能。
- 启动、部署、API 路径/参数/响应/错误、Worker/迁移、模型下载/加载或配置变化时，**同次更新 README**；给出可执行命令、地址和必要限制，不把计划当成已实现。
- README 是用户说明书，只保留用途、环境、配置、部署、启动、接口和故障排查；不放阶段总结、内部设计、业务测试脚本、pytest 命令或验收统计。
- 阶段有实际进展时，同步更新本文件第 1 节进度清单，简要记录已完成任务、当前阶段和待验收事项，区分已实现与已验收，不追加逐次开发记录或实现细节。详细路线、契约、设计与人工验收放 `docs/`，测试代码放 `tests/`；根目录说明文档仅保留 README/AGENTS，旧计划放 `docs/archive/`。
- 新增或移动文件/目录时判断是否更新 `.gitignore`；工程文档必须可提交，素材和本地资料按需忽略，移动文档同步修复链接。源代码与配置不因文档规则迁出根目录。
- 不自动提交或 `git push`，除非用户明确要求；实际变更后提供中文 Commit Message。
- 用户可见的本地文件必须用可点击 Markdown 链接：正斜杠绝对路径以 `/D:/` 等驱动器开头，含空格的目标用尖括号包裹，不使用反斜杠或裸路径。

### 日志与测试

- 每次新增/修改业务接口、服务、异步任务，同次维护关键过程和结果日志及必要测试，覆盖成功、跳过、重试、失败；仅改文档/注释/测试且业务不变时无需新增运行日志。
- 复用 `dkf.business` 的 UTF-8 中文可读白名单摘要，记录资源标识、状态、数量与耗时；API/Worker 共用本地 `LOG_DIR`，每天仅一份 `knowledge-service-YYYY-MM-DD.log`，不输出整行 JSON，不按 PID/角色/大小拆分。
- HTTP 只记录路由模板，不记请求头、查询串或正文；恢复请求/任务上下文。落库成功日志必须在事务提交后记录；日志失败安全降级，不改变业务结果。
- 保留同目录固定锁文件、多进程整行追加与最多 1 秒锁等待；运行中不删锁，不用普通多进程 RotatingFileHandler，不保证共享盘/跨主机安全，不自动删除或合并历史日志。
- 修改前检查现有实现和测试，修改后做相应自动验证，保持阶段 1～11A 回归；明确未运行或未真实验证的部分。
- 外部 API 必须区分 Mock 与真实验收，逐项验证连接、鉴权、最小请求、返回结构、业务及异常处理；未经真实测试不得声称 API 可用。

## 4. 核心实现约束

以下为必须保留的底线；修改对应模块前，必须阅读第 5 节的专题文档。

### 上传、任务与持久化

- 按实际内容验证大小、SHA256 和格式，OOXML 校验对应主部件、旧 Office 校验 OLE 主流，不只看后缀/MIME；这些检查不等于完整解析或恶意文件扫描。
- 原上传接口的 multipart `file` 支持 1～50 份，每份独立 doc_id、原件、事务和任务；单份保留 201/202，多份 200 返回逐项报告，不新增 `/batch` 或自动云端合批。单项失败不撤销其他成功项。
- Swagger 使用一个原生 multiple 文件控件，窗口内单选/多选，不用 Add string item 冒充；保留 File 对象、输入顺序、固定 UI 版本及 root_path。选择后只初检，不读正文、不自动上传、不静默删文件，服务端最终校验。
- 原件与 Document 提交后再投递 `default_queue`。耗时入库走 Celery，晚确认、文档行锁、当前 `processing_task_id` 和稳定 ID 保证幂等；旧消息/回调不能覆盖新代次或重复写块。
- Native 临时数据库/文件错误最多重试 3 次（总共 4 次），退避 5/10/20 秒；原件缺失/空文件立即失败，记录安全原因，成功重试清旧错误。
- 解析、Guard、旧块整体替换及状态推进在同一事务，失败全回滚再独立记录原因；云端 Block、Document=CHUNKING、job=done 同事务。业务状态以 Document 为准，PARSING 不保证实时可见。
- `/process` 仅重投 UPLOADED/FAILED，不作为 Reindex；活动云任务通过已有 `/mineru` 恢复，完成状态拒绝重解析。数据库/文件/消息/云端非原子，无 Outbox、后台扫描或自动补投；提交不确定先查记录并保留原件，不盲目重传或宣称 exactly-once。
- Windows CPU threads 仅用于既有开发验证；启用 Hybrid 的 PDF Bundle 处理使用 solo 并发 1；正式 Linux CPU prefork 并发 2～4，GPU 单进程并发 1（当前不加载模型），在线 RAG 默认同步。

### Parser、证据与质量

- Native 继承 BaseParser，通过公共 `parse` 校验 ParsedDocument/ParsedBlock 及输入身份；契约独立于数据库、Celery、RAG，云 Provider 通过 Adapter 输出同一结构。
- FileType 共十三种；CSV 使用标准 csv 与严格解码，XLSX 按 Sheet/单元格原序、section=Sheet 名、保留公式不执行。Native 不宣称包含 Office 图片/图表/绘图，也不实现旧 Office/PPTX Native。
- 稳定 Block ID 由 Parser 显式提供，文档内唯一；新 `block_index` 从 1 开始且唯一，历史空序号不伪造，查询显式按序号排序。空结果不能认定成功。
- PDF 物理页码从 1 开始，bbox 为点数；图片第 1 页、像素 bbox。Native TXT/MD/CSV/DOCX/XLSX 为逻辑页 1，Cloud Office 为转换后逻辑页、bbox=NULL；未知页数、位置或置信度不伪造。
- 不按文件名、验收标记或具体字符串补 OCR、改公式名称或拆表；质量差异记录为边界，公式语义标准化留至阶段 27。
- 后续 Evidence 必须可追溯 `Answer → Evidence → Chunk → Page → Document`；Chunk 至少保留 kb_id/doc_id/chunk_id/page_start/page_end/section/text。缺证据明确返回不足，缺公式参数返回 need_variables，不猜测、不用 `eval()`，计算使用 SymPy 等安全方式。最终对外协议按开发计划，内部变更不破坏上层接口。

### MinerU Cloud

- 仅官方精准解析 API V4，不用轻量 Agent API、本地 MinerU、火山/百度 OCR；HTTP 只在 MinerUCloudProvider，申请链接 → 签名 PUT → 批次 GET。
- 固定 `trust_env=False` 直连 API/上传/下载，无代理回退、保持 TLS 校验，不绕过 VPN/TUN 或修改系统网络。对象存储不带 API Token、不设上传 Content-Type、不跟随重定向，只接受官方 HTTPS 签名域名。
- Token 仅本地 `MINERU_API_TOKEN`，SecretStr 遮盖、不自动刷新。校验 Hash、实际格式及大小：平台 200 MB/200 页，应用取更小限制，Office 未知页数由平台检查，不猜 Sheet/Slide 页数。
- 保留 Redis Lua 共享原子限流：50 文件/分钟、1000 查询/分钟、5000 文件/天，安全系数 0.9；滚动 24 小时日额度，1000 优先页仅按 UTC 日观测。按文件/查询数计量，同账户共用 Redis/命名空间，Redis 故障关闭云调用。
- submit/check 为短任务，一次检查后 countdown；禁止 while/sleep 轮询。临时错误有限退避、抖动、尊重 Retry-After，总等待有上限；永久错误立即 FAILED。
- POST 前保存意图、PUT 前保存 batch，恢复沿用检查点；POST 响应丢失禁止盲目申请。保留活动身份唯一约束、参数快照和代次检查；task_id 是本地代次，不是外部任务或整份 Celery 结果。
- 唯一 content_list.json 为结构化正文，拒绝未知结构、空正文、恶意/超量 ZIP，不以 full.md 替代。只凭可信 layout 父块 index 恢复同页 header/footer，必须唯一关联、保留正文相对顺序和原数组位置 ID；无证据/歧义/复杂布局保留整页原序，禁止全局按 y 排序、拆表或公式。

### 整份 Router v1

- 外部仅 `auto / native / mineru`；上传 Form、重投 Query 省略默认 auto，普通用户不选模式；管理员可覆盖。Hybrid 是 XLSX 云增强的内部实现，不新增外部模式。默认云外传/费用须在 README 告知。
- Worker 只读预检：可完整覆盖时 Native；复杂 DOCX/PDF、图片、旧 Office/PPT 整份 MinerU。复杂 XLSX 优先 Hybrid。MD 缺附件、XLSX 缺外部资源、云不支持、超限及坏原件明确失败；不抓 URL、不把 MD/CSV 发云、不要求普通用户选解析器。
- RouteDecision 版本固定，只含模式/格式/策略、白名单理由、结构计数、页摘要和回退次数，不含正文、地址、凭据或虚构置信度；任务结果与 job 的 `_route_decision` 保存，不参与供应商参数 Hash、不透传 API。
- auto Native 仅明确 OCRRequiredError/CompletenessError 且受支持格式可回退一次；普通解析/编码/损坏错误不回退。所有云入口远端 failed 不自动追加批次，云失败不降 Native；PUT/GET/下载有限重试仍用同批次。
- Guard 在写块前检查正文、PDF 非空页、XLSX 有效 Sheet、source、逻辑页与 Office 空 bbox，不改排序/ID、不补文字；结构检测不证明视觉/语义完全正确。
- 云交接保持行锁/代次检查、提交后投递；路由拒绝为 Document=FAILED、Celery=FAILURE，固定安全理由可追踪，无新 Block/云调用。交接成功不等于解析成功，查询 Document/Parse Job。
- 云费用按 doc_id/job_id/batch_id 去重：提交前实际 0、计划 1，确认 batch 后计 1；不直接累加重复日志，提交不确定以平台核对。

### XLSX Selective Hybrid

- auto 默认启用 XLSX Hybrid 尝试；Native table/公式保持原式，基础 Chart 确定性结构化；无操作效果、常见样式和共享公式优先合理兼容，不伪造截图。
- 同代次图片聚合一个 Bundle。仅提交前的已知能力缺口允许一次 Whole 回退；损坏、缺附件、孤立部件、超限直接失败，云提交后不追加 Whole。Whole 原件同样通过格式/大小/平台/共享预算校验。
- XLSX Whole 也冻结 Native 主结果 Hash，完成时保留整表与公式，再附云块原序；不覆盖原式、不猜 Whole 云块的原 Sheet/Anchor。主结果不可恢复或云结果失败全回滚；私有摘要不是 Parse Cache。
- manifest 与冻结配置保存在私有检查点，Sheet/Anchor 可追溯，稳定 visual/Block ID；云结果 page=1、section=原 Sheet、bbox=NULL。整 Sheet table 不拆分，增强块在对应 table 后按 Anchor 稳定排列。
- 活动恢复沿用同 Job/batch/Bundle；Bundle 缺失仅按原 Hash/映射重建，不是 Parse Cache。完整性与合并全部成功后一次事务替换 Block；失败不写半份结果。运行文件不在当前开发回合生成，真实文档处理仅由用户启动。

## 5. 专题文档与交付要求

| 修改范围 | 必须阅读 |
|---|---|
| 路线、阶段验收、最终协议 | [开发计划](docs/DEVELOPMENT_PLAN_CODEX.md) |
| Parser、页码/坐标、持久化与回归 | [Parser 契约](docs/PARSER_CONTRACT.md) |
| 云接口、限流、检查点、顺序与真实验收 | [MinerU Cloud](docs/MINERU_CLOUD.md) |
| 预检、决策、Guard、XLSX 对照 | [Parser Router](docs/PARSER_ROUTER.md) |
| XLSX 候选、Bundle、检查点、合并与人工验收 | [Selective Hybrid](docs/SELECTIVE_HYBRID.md) |
| 日志事件、脱敏、并发写入与验收 | [业务日志](docs/BUSINESS_LOGGING.md) |
| 部署与接口使用 | [README](README.md) |

完成任务后说明完成内容、修改文件、自动验证结果和未验证能力；给出具体人工测试命令/步骤，不只说“请测试”。外部 API 必须提醒真实人工验证，Mock 不能替代真实结果。实际变更附一条中文 Commit Message；每次回答都给出与当前阶段相关的“下一步建议”，按用户要求停止，不擅自推进后续阶段。
