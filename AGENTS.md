# AGENTS.md

## 0. 依赖可自动安装，部署与测试由用户执行（最高优先级）

**Codex 可以自动安装当前开发任务所需、已声明或已获用户确认的项目依赖；部署及测试仍由用户手动执行。此规则优先于其他项目文档中的旧依赖安装禁令。**

- 用户说“实现阶段 X / 完成任务 / 按开发文档继续 / 解决依赖”，可在任务范围内自动安装必要项目依赖，**不构成部署或测试授权**；仅修改文档或提供部署说明时，不顺带执行安装。
- 安装前确认 Python 与 pip 指向项目 Conda 环境 `dkf-agent`，可使用 `python -m pip install` 或针对该环境的 `conda install`；不得安装到 base/系统环境，不无条件升级整个环境、不安装无关依赖。新增技术或未确认的依赖仍须按第 2 节先说明并等待确认；依赖声明与必要安装说明同步维护。
- **部署操作必须由用户手动执行**：包括创建/重建 Conda 环境、更换 Python、PyTorch CPU/CUDA 版本替换、CUDA/驱动与 GPU 部署、模型或推理资源下载、基础服务安装/编译/配置、系统服务变更、数据库迁移，以及 API/Worker 启动。依赖安装许可不包含上述操作，禁止通过安装脚本间接执行部署。
- 对部署任务，Codex 仅检查现有实现与依赖、提供可复制的 PowerShell 命令和手动验证预期结果、说明兼容性及时间/磁盘/显存风险，随后停止等待用户反馈；模型加载/推理验证仍由用户执行。
- Codex **不得自行**运行 PDF/DOCX/XLSX/PPTX/图片的解析/OCR/转换/渲染测试；不得生成/运行真实或 Mock 的文档验收样本；不得运行 `pytest`、`smoke`、完整回归、性能评测或端到端测试。
- Codex **不得自行**调用 MinerU/OCR/DeepSeek/Embedding 云 API，不得为了测试启动数据库、Redis、Celery、模型或反复轮询/重试任务。
- Codex 可以编写测试代码和断言，但**不执行**；允许对本轮改动文件进行至多一次快速 `git diff --check` / Python 语法检查，不借检查启动服务、加载模型或做耗时重试。
- 真实解析、样本上传、云配额消耗、日志导出、结果判断和阶段是否通过，由**用户手动执行并确认**。
- 只有用户**另行明确指定**测试命令、环境与样本范围、联网权限、时间/费用及资源预算时，Codex 才能代跑所指测试；授权仅限一次，不转化为默认权限。自动安装依赖本身不授予测试权限。
- 每轮功能完成后，Codex 应输出：修改文件、静态检查结果、用户手工测试步骤及预期结果、尚未验收事项、中文 Commit Message、下一步建议，并**立即停止**。
- 其他章节提到“自动测试”“至少验证”“完整回归”“API 接入后验证”，只表示需要提供用例/步骤，**不授权 Codex 执行**。

## 1. 项目范围与当前阶段

DKF-Agent（数知融问智能体）当前只开发 `knowledge-service`：可独立运行、测试和演示的非结构化 Knowledge Base Service。未经用户要求，不开发 NL2SQL、Schema Linking、Coordinator、Fusion 或其他成员模块。

- 最新路线：[开发计划](docs/DEVELOPMENT_PLAN_CODEX.md)（V0.9）；历史计划仅供参考，不重新实现已有阶段。

### 当前开发进度

当前阶段：**33全量导入排错中：70261c48原ZIP已实际READY。da426abd缓存102 Block/PARTIAL；18:28日志确认缓存回放无云请求、v2生成24 Chunk，但第17片8337 Token超过8192。只读原缓存证实4张大表有表题/注释，被v2的纯HTML入口漏拆；已补v3定位唯一表体、保留附文与原字符偏移及rowspan保护，v1/v2冻结策略不变。新回归仅编写未运行，需用户单文档Reindex验证GPU/索引；全200/4087未验收，不进入34，冻结数据不改。**

| 阶段 | 已完成任务 / 当前状态 |
|---|---|
| 0～3 | 工程初始化、Python 3.11、FastAPI、PostgreSQL/pgvector/Redis 连接、Celery 双队列 |
| 4～7 | 知识库数据模型与迁移、管理员 API、文件上传/存储/去重、异步任务及状态查询/重投 |
| 8～9 | 统一 ParsedDocument 契约；TXT、MD、DOCX、文本型 PDF Native 解析及 Block 入库 |
| 10 | MinerU Cloud；图片、扫描 PDF、表格、公式及阅读顺序已完成真实 API 人工验收 |
| 11A | 已实现十三种格式校验、CSV/XLSX Native Parser、六种 Office 显式 MinerU 支持 |
| 配套功能 | 多选上传/日志/PDF分片与Windows句柄/文本表格恢复保留；首片2263 Block/新Guard及用户所述分片云验收基线保留；阶段33直接复用，不扩展旧Job恢复 |
| 11B.1 | 已实现预检、Native/MinerU 自动路由及完整性 Guard；不再使用人工 Review 路由 |
| 11B.2 | 已实现 XLSX Native 主解析、候选过滤/复用、结构化 Chart、单 Bundle/manifest、Anchor Merge 与事务接入；测试仅编写未执行，真实收益待验收 |
| 12 | 已实现配置化本地缓存、完整身份键、跨文档/视觉复用、Reindex 缓存回放与显式 Reparse；测试只编写，待人工验收 |
| 13 | 已实现标题/段落切片及稳定ID/字符证据；v3长表行策略兼容同Block表题/注释，rowspan关联行保护，v1/v2冻结策略保留；新增Mock及真实长表/GPU待用户验收 |
| 14 | 已实现 BGE-M3 本地惰性加载、文档/Query 统一 1024 维编码、单文档 GPU 批处理与检查点；真实模型/CUDA 及回归待验收 |
| 15 | 已实现显式 /index、CPU 批量入库、模型/输入身份校验、全量向量与 READY 同事务、HNSW cosine 索引迁移；未执行测试/迁移，待验收 |
| 15A | 已实现只读 Node/NodeWithScore 互转、稳定业务 ID/完整证据、共享 Query Adapter 和文档重编码保护；用例仅编写，待 core 安装与用户验收 |
| 16 | 已实现预置导入与 OHR/Omni/OCB 三数据集联合冒烟（约14份、原API链路、完整QA/GT、分数据集Dense报告）；新增用例仅编写，真实导入与正式质量验收待用户完成 |
| 17 | 已实现 LlamaIndex Dense Retriever、共享 Query 编码、同模型 READY/知识库隔离、稳定精确 Top-K 与证据互转；用例未执行，待人工验收 |
| 18 | 固定单/多文档范围、参数校验、SQL Top-K前过滤与原始证据保留；用户已确认测试通过 |
| 19 | 已实现本地BM25、kb/doc范围、当前READY快照、派生缓存重建和原始证据保留；用例未运行，待验收 |
| 20 | 已实现LlamaIndex组合Retriever、等权RRF、稳定Chunk去重、分支排名/分数与证据DTO保留；用例未运行，待验收 |
| 21 | 已实现本地BGE Reranker惰性共享/离线批处理、Top5 Postprocessor、三类分数及Node/DTO往返；用例未执行，真实排序收益待验收 |
| 22 | 已实现同范围Hybrid→Top5→单次LLM合成及引用校验；已拆分官方DeepSeek/custom独立配置，旧Sub2API真实RAG用户已确认，本轮重构Mock及真实验收待用户执行 |
| 23 | 已实现稳定Citation/三类分数、答案标记校验、只读来源快照与字符/Hash/页语义校验；相关回归用户已报告148通过，真实来源与忠实度待验收 |
| 24 | 已实现POST /api/knowledge/search、/query、范围与脱敏错误；非有限数响应校验修正待用户复测，真实HTTP/云验收待完成 |
| 25 | 已补八个文档级管理接口、逐阶段恢复、缓存Reindex至READY、异步自有文件/DB级联删除及共享缓存保护；新迁移/Mock/DB/Worker待验收 |
| 26 | 缓存命中增量真实验收通过，相关隔离DB回归93通过；补只读语言策略实验，192项Mock/相邻回归及30题真实GPU测量完成；生产未集成，独立文档留出集/新云批次及真实超时恢复待验收 |
| 27 | 已补公式提取服务、原式/变量/引用严格契约、四则语法规范化与人工验收CLI；测试未运行，真实LLM/数学语义待验收 |
| 28 | 已补用户/证据参数绑定、严格十进制、双端字符来源及need_variables；句末小数修复五文件252项Mock通过，真实来源/语义验收待用户确认 |
| 29 | 已补显式SymPy有理数计算、变量白名单、每步零分母/资源上限、精确结果/舍入展示/来源及稳定计算ID；用例仅编写，无云/业务写入，待验收 |
| 30 | 已补有限StateGraph/四薄Tool/范围来源/计步及CLI；新增统一LLM显式授权一次Fallback，默认关闭、业务校验不变；Mock仅编写，待验收 |
| 31 | 既有用例状态保留；阶段32本地提取授权不等于pytest/全量回归授权 |
| 32 | OHR100 PDF/3588题、OCB100 Office/499题及739条官方断言已本地冻结；32条Domain任务/1138条断言待审不进正式QA；双向校验通过，未做问答评测 |
| 33 | 保留v4.1质量分层及70261c48真实READY；da426abd第17片8337/8192 Token，已定位v2漏拆带表题/注释的大表并补v3；仅写回归，需单文档缓存Reindex由用户验证，无新MinerU提交，全200/4087完整验收未完成 |
| 34 及后续 | 未执行，等待用户确认 |

- 当前代码迁移head为`0009_parse_quality`：可空解析质量/Block来源及PARTIAL_READY约束；不回填历史数据。用户已确认编写但未由Codex执行迁移，“已实现”不等于已验收。
- 解析后 CHUNKING，切片后 EMBEDDING，/embed 本地向量完成后 INDEXING，独立 /index 全部向量事务入库后 READY；READY只表示数据入库完成。HTTP搜索/问答复用原Hybrid/Reranker/RAG，Dense/BM25独立Python入口不变。
- 阶段30的LangGraph仅封装原Search/Query/Formula服务，不改Parser/上传/检索/原RAG Prompt或索引语义；此前业务验收不会因阶段32数据提取自动通过，后续模型/云端/测试仍须另行授权。
- 用户后续单独授权阶段32本地数据提取，固定于`data/dataset/stage32_run_01/`，仅OHR/OCB；历史阶段16三基准工具/素材保留。正式QA必须全依赖存在且Hash一致，每原件至少一题；断言不拼答案，Domain未审不纳入。3份OHR超200页，后续可由新云PDF分片机制处理，但Codex不得自行执行真实分片、上传、评测或覆盖旧run。
- 当前自动路由规则以本节和用户最新要求为准，开发计划中历史 Review/默认关闭描述不再用于正式路由；第 0 节测试禁令保持最高优先级。
- 联合冒烟使用 `docs/test/stage16_smoke/` 与独立 ACTIVE 测试库，默认 dry-run；用户显式准备/上传/Query 后才生成真实结论，不写正式 preset/manifest、不降低十份/旧Office/七标签标准；小样本通过不代表阶段16正式或官方全量质量验收通过。
- 新准备默认 OHR5份/Omni3图/OCB三类Office各2份；旧双数据集计划不改写。OCB复用本地归档与Parquet、依赖完整才选；不重下载、不要求补43份或许可Excel。原断言保留manual_pending，不猜答案/物理页；下载成功不授予云外传，用户须明确确认本轮全部样本。

最终目标：预置多格式知识库为主、动态上传增量为辅；完成单文档及全库检索、Dense + BM25、Reranker、LLM RAG、Evidence和独立Demo。提交至少10份多格式文档、10个单文档RAG用例，忠实度目标≥90%，分别记录检索/重排/生成耗时。后续动态增量、公式参数识别、结构化与安全计算按计划开发，不提前宣称可用。

## 2. 技术与本地环境约束

- 当前：Python 3.11、FastAPI、SQLAlchemy/Alembic、PostgreSQL + pgvector、Celery + Redis、PyMuPDF、python-docx、openpyxl、olefile、MinerU 官方精准解析 API V4（默认 `vlm`）。
- 已接入独立DeepSeek官方/custom兼容API、本地BAAI/bge-reranker-v2-m3、SymPy受限算术及LangGraph编排边界；LangChain仅辅助运行时，无独立AgentExecutor/RAG。HTTP复用httpx，无OpenAI SDK；Docker Compose仅为未来整体部署方案。
- BGE-M3 Dense 使用已声明的 sentence-transformers；项目依赖可按第 0 节安装，只从用户部署的本地官方权重加载，CUDA/PyTorch CPU-CUDA 版本替换及模型准备仍由用户执行。
- BGE Reranker使用已声明Transformers的单标签分类模型，统一由项目RerankerService管理；Adapter不另载模型，不要求FlagEmbedding或框架模型集成。本地Safetensors权重与部署仍由用户准备。
- LlamaIndex 仅声明已获用户确认的 llama-index-core 0.14.x；作为可选运行时 Adapter 显式导入，core 依赖可按第 0 节安装，首次导入资源缓存准备仍由用户执行，不新增默认 OpenAI 或独立向量存储集成。
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

#### 业务日志与进程角色规范

- 所有新增独立进程、Worker或CLI测试/验收入口，使用与实际用途一致、明确稳定的`configure_logging(..., role=...)`标识；CLI不得统一伪装为`api`或`worker`来掩盖未知角色。
- 唯一角色/显示名称注册表为`app/core/business_logging.py`的`ROLE_LABELS`；新增或修改role必须同次维护该表、入口、单元测试及必要开发文档，不在其他生产文件或测试解析器另建显示映射。
- 未注册或非字符串角色统一安全显示“未知进程”，不得直接打印外部角色字符串；角色只来自入口配置，业务字段不能覆盖。只调整业务日志标识，不更改操作系统进程名称/PID，API/Worker格式及每日合并行为保持兼容。
- 继续遵守既有白名单/脱敏规范：禁止记录Query、文档正文、数据库连接信息、密钥、模型私有路径或异常原文；注册正常角色不放宽安全限制。后续模块须同步检查入口角色是否已注册。

## 4. 核心实现约束

以下为必须保留的底线；修改对应模块前，必须阅读第 5 节的专题文档。

### 上传、任务与持久化

- 按实际内容验证大小、SHA256 和格式，OOXML 校验对应主部件、旧 Office 校验 OLE 主流，不只看后缀/MIME；这些检查不等于完整解析或恶意文件扫描。
- 原上传接口的 multipart `file` 支持 1～50 份，每份独立 doc_id、原件、事务和任务；单份保留 201/202，多份 200 返回逐项报告，不新增 `/batch` 或自动云端合批。单项失败不撤销其他成功项。
- Swagger 使用一个原生 multiple 文件控件，窗口内单选/多选，不用 Add string item 冒充；保留 File 对象、输入顺序、固定 UI 版本及 root_path。选择后只初检，不读正文、不自动上传、不静默删文件，服务端最终校验。
- 原件与 Document 提交后再投递 `default_queue`。耗时入库走 Celery，晚确认、文档行锁、当前 `processing_task_id` 和稳定 ID 保证幂等；旧消息/回调不能覆盖新代次或重复写块。
- Native 临时数据库/文件错误最多重试 3 次（总共 4 次），退避 5/10/20 秒；原件缺失/空文件立即失败，记录安全原因，成功重试清旧错误。
- 解析、Guard、旧块整体替换及状态推进在同一事务，失败全回滚再独立记录原因；云端 Block、Document=CHUNKING、job=done 同事务。业务状态以 Document 为准，PARSING 不保证实时可见。
- `/process`及旧嵌套维护接口保留历史逐阶段语义；新`/api/admin/documents`提供统一管理，Reindex仅缓存回放后自动Chunk/Embedding/Index至READY，Reparse仍须force=true。活动任务不能覆盖，数据库/文件/消息/云端非原子，无Outbox，不宣称exactly-once。
- 切片 `/chunk` 采用独立 chunk_task_id/冻结参数，不改变解析代次；行锁、稳定 ID、全量 Chunk 替换和 EMBEDDING 同事务。只恢复切片检查点，不用解析失败的旧块冒充新输入。成功替换 Block 同事务失效旧 Chunk；失败回滚保留旧证据。正文重叠为软目标，结构块/原生公式完整性优先，本地 token 估算不得声称模型精确 token。
- 新切片为block-chunk-v3：定位唯一HTML表体，原表题/注释仅一次content引用；原行及外壳使用精确source_spans，rowspan关联行不可拆，不改Block/原字符/页码。无法证明行边界、单个超长行组或公式保留完整并由真实Tokenizer判上限。v1/v2检查点保持旧策略；显式文档Reindex从原解析缓存创建v3，不因切片修复重新付费解析或改变原质量。
- Windows CPU threads 仅用于既有开发验证；Hybrid PDF Bundle、超限PDF分片处理均使用 solo 并发 1；正式 Linux CPU prefork 并发 2～4；GPU Worker 使用 solo、并发 1，每 GPU 一个进程，首次任务惰性加载模型；在线 RAG 默认同步。

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
- 仅实际云路由的超限PDF可连续分片，Native可完整覆盖则不拆；一个原doc_id/hash，JSONB私有分片账本及PARSED_DIR持久文件。每片按实际字节/页数/Hash校验，单页超限失败；意图/Batch先提交、沿用共享限流，成功片不重复收费。只有全片Guard通过才整体写Block/CHUNKING，原页=`片起始页+局部页-1`；Chunk/Evidence不暴露局部页。非PDF不套用；原件/QA不改，Codex不得自行执行真实分片/收费验收。详见[MinerU分片](docs/MINERU_CLOUD.md#超限-pdf连续分片)。
- 保留 Redis Lua 共享原子限流：50 文件/分钟、1000 查询/分钟、5000 文件/天，安全系数 0.9；滚动 24 小时日额度，1000 优先页仅按 UTC 日观测。按文件/查询数计量，同账户共用 Redis/命名空间，Redis 故障关闭云调用。
- submit/check 为短任务，一次检查后 countdown；禁止 while/sleep 轮询。临时错误有限退避、抖动、尊重 Retry-After，总等待有上限；永久错误立即 FAILED。
- POST 前保存意图、PUT 前保存 batch，恢复沿用检查点；POST 响应丢失禁止盲目申请。保留活动身份唯一约束、参数快照和代次检查；task_id 是本地代次，不是外部任务或整份 Celery 结果。
- v4先从可信同ZIP的逐页preproc/discarded原始Span/HTML/公式取得基础内容，缺布局时用V1独立项；每块标明真实来源类别/指针/Hash，不把layout冒称V1，不用V2/full.md补字。遵循原数组或可信index，不按bbox猜阅读顺序。局部未知类型/Span/坐标不连带拒绝可靠正文，不能补造页码。
- 云结果先校验全部ZIP成员CRC及唯一非空结构列表，在PARSED_DIR独立保留原字节/Hash/Batch回执，再运行Adapter；转换失败不删原ZIP，也不发布成功缓存或标记业务成功。隔离验收CLI仅复用分片器/Provider/共享限流取得原ZIP，不操作旧Job/业务库、不运行Adapter和后续模型；默认dry-run，真实执行须另行授权全部新分片费用。
- 原有文本/表格严格恢复保留在`normalize_strict`增强层，唯一映射、字符/Span/网格及跨页保护不删除；增强失败记录DEGRADED子原因，回到可靠基础块，不再作为整份FAILED的默认理由。仅内容完整可READY；真实缺页/未识别视觉/隔离内容标PARTIAL，经原Chunk/GPU/Index完整写入可靠块后为PARTIAL_READY。未知页码文字留本地产物，不生成假Block或严格页命中；全局无可靠内容、Hash/Batch/ZIP安全错误仍失败。
- 当前Normalizer为v4，旧成功缓存版本隔离且不覆盖。原v3失败Job显式Retry验证原件/片/ZIP/Batch后，仅新增本地转换配方并强制离线；原云参数、Batch/收费身份保留。历史本地代次误换只接受唯一同Hash原Job，歧义拒绝。旧v2仅允许诊断显式比较，不静默迁移。缺后续片原ZIP停止，不自动追加云任务。
- v4.1按`content_policy_version`隔离本地转换缓存；普通已归档原图不一律判缺文，未识别chart/纯图片页/缺原图仍PARTIAL，记录原ZIP成员/Hash与待审页，不造文字。仅管理员显式`retry?allow_new_split_parts=true`可续交冻结PDF账本中从未提交的片；旧Batch永不重投，默认Retry仍本地，活动任务不改许可。云调用仍由用户手动确认费用后执行。

### 整份 Router v1

- 外部仅 `auto / native / mineru`；上传 Form、重投 Query 省略默认 auto，普通用户不选模式；管理员可覆盖。Hybrid 是 XLSX 云增强的内部实现，不新增外部模式。默认云外传/费用须在 README 告知。
- Worker 只读预检：可完整覆盖时 Native；复杂 DOCX/PDF、图片、旧 Office/PPT 整份 MinerU。复杂 XLSX 优先 Hybrid。MD 缺附件、XLSX 缺外部资源、云不支持、超限及坏原件明确失败；不抓 URL、不把 MD/CSV 发云、不要求普通用户选解析器。
- RouteDecision 版本固定，只含模式/格式/策略、白名单理由、结构计数、页摘要和回退次数，不含正文、地址、凭据或虚构置信度；任务结果与 job 的 `_route_decision` 保存，不参与供应商参数 Hash、不透传 API。
- auto Native 仅明确 OCRRequiredError/CompletenessError 且受支持格式可回退一次；普通解析/编码/损坏错误不回退。所有云入口远端 failed 不自动追加批次，云失败不降 Native；PUT/GET/下载有限重试仍用同批次。
- Guard 在写块前检查正文、PDF 非空页、XLSX 有效 Sheet、source、逻辑页与 Office 空 bbox，不改排序/ID、不补文字；结构检测不证明视觉/语义完全正确。
- PDF覆盖仍独立使用`pdf-coverage-v2`，不因云缺页改变required；装饰证明及冻结比较规则不变。Native缺页仍严格失败；MinerU v4有可靠内容但缺required页时保留缺口并标PARTIAL，不冒充完整。诊断显式覆盖比较仅操作副本，与Normalizer许可独立，不改Job/缓存或投递云任务。详见[覆盖比较](docs/MINERU_CLOUD.md#pdf-页面完整性装饰横线与冻结覆盖比较)。
- 云交接保持行锁/代次检查、提交后投递；路由拒绝为 Document=FAILED、Celery=FAILURE，固定安全理由可追踪，无新 Block/云调用。交接成功不等于解析成功，查询 Document/Parse Job。
- 云费用按 doc_id/job_id/batch_id 去重：普通Whole/Bundle计划1，PDF分片计划数以实际物化manifest为准；每个确认Batch的文件计1，片缓存命中计0，不直接累加重复日志。提交不确定不等于未计费，须平台核对。

### XLSX Selective Hybrid

- auto 默认启用 XLSX Hybrid 尝试；Native table/公式保持原式，基础 Chart 确定性结构化；无操作效果、常见样式和共享公式优先合理兼容，不伪造截图。
- 同代次图片聚合一个 Bundle。仅提交前的已知能力缺口允许一次 Whole 回退；损坏、缺附件、孤立部件、超限直接失败，云提交后不追加 Whole。Whole 原件同样通过格式/大小/平台/共享预算校验。
- XLSX Whole 也冻结 Native 主结果 Hash，完成时保留整表与公式，再附云块原序；不覆盖原式、不猜 Whole 云块的原 Sheet/Anchor。主结果不可恢复或云结果失败全回滚；私有摘要不是 Parse Cache。
- manifest 与冻结配置保存在私有检查点，Sheet/Anchor 可追溯，稳定 visual/Block ID；云结果 page=1、section=原 Sheet、bbox=NULL。整 Sheet table 不拆分，增强块在对应 table 后按 Anchor 稳定排列。
- 活动恢复沿用同 Job/batch/Bundle；Bundle 缺失仅按原 Hash/映射重建，不是 Parse Cache。完整性与合并全部成功后一次事务替换 Block；失败不写半份结果。运行文件不在当前开发回合生成，真实文档处理仅由用户启动。

### Parse Cache

- 整份键包含原件 Hash/格式、Provider、模型、语言、OCR/表格/公式选项、Route/覆盖范围及版本；Hybrid 额外含 Extractor、manifest/视觉内容摘要。视觉键使用原图片字节 Hash，不使用临时 PDF Hash、doc_id 或 Anchor。
- 缓存只发布安全校验通过的转换产物，保留三维质量、隔离内容与校验和；缓存有效不等于文档内容完整，PARTIAL不能被回放成完整READY。跨文档重绑身份，Reindex使用冻结代次；Native XLSX原式不被云计算值覆盖。
- `PARSED_DIR` 由所有进程共享并备份整个目录（含隐藏元数据）；缓存含原文，不入仓库/日志，变更归一化或提取算法时维护版本失效。损坏/缺失不静默重新付费，旧已完成结果不自动回填。
- 相同活动内容用数据库事务锁/Job 检查点保护；普通重试遇收费历史而无可用结果停止，只有显式 Force Reparse 允许新调用。缓存发布和数据库非同一事务；已验证结果可在 SQL 回滚后同代次恢复，落库日志只在提交后记录。

### 本地 Embedding

- 固定 BAAI/bge-m3 Dense 1024 维，文档/Query 同模型、真实 Tokenizer 与 L2 归一化；不加 Query 指令，不启用 Sparse/ColBERT。只读本地权重、local_files_only=True、trust_remote_code=False，不自动下载、不自动 CPU 回退；真实 token 超限明确失败，不截断结构块。
- 每文档一个 gpu_queue 任务，内部批处理，不每 Chunk 一个 Celery Task。独立 embedding_task_id/冻结输入及配置，失败不改 Chunk；全量本地结果校验后推进 INDEXING，只有事务提交后记业务成功日志。
- EMBEDDINGS_DIR 保存受限访问的代次向量/manifest；只发布完整结果，校验身份、顺序、维度、数值和校验和，SQL 回滚后可同代次复用。损坏不静默重算；原件/Chunk 重建失效旧检查点但不自动删除历史文件。阶段 15 才写 pgvector/建索引，后续 Query 必须核对实际模型指纹，不能仅凭维度相同混用不同模型。

### pgvector 入库

- /index 只消费已完成本地向量，一个文档一个 default_queue 任务；不加载模型/Parser 或调用云端。独立 index_task_id/index_config 冻结解析、切片、GPU 代次及文件/模型摘要，重试只恢复同一代次。
- HNSW cosine部分索引由用户迁移，任务只校验。全量可靠Chunk输入/向量通过后与终态同事务提交；内容COMPLETE为READY、PARTIAL为PARTIAL_READY，任何向量错误仍全回滚。两类可靠块可检索，知识库/文档/模型/代次隔离不变，正式全量验收只计READY。
- READY 重复 /index、/chunk、/embed 不重算；活动入库拒绝维护。显式 Reindex/Reparse 与成功 Block/Chunk 重建失效旧入库检查点，失败保留原证据；未来检索必须限定 READY 及模型/编码版本，不能用有值的旧向量冒充新代次。

### LlamaIndex Adapter

- 只转换调用方已加载的 DocumentChunk/Document，未加载必需字段安全失败，不隐式 SELECT/autoflush、读取原件或落库。Node ID 固定 chunk_id，SOURCE 固定 doc_id/知识库/文件身份，完整 chunk_metadata/source_spans 深复制；内部 DTO 不包含框架类型或向量。
- 已有向量只复制，不重编码；metadata 从框架 Embedding/LLM 文本中排除。Query Adapter 仅委托既有进程共享 EmbeddingService，文档编码入口明确拒绝，Transform 只复用已有向量；不设置全局默认 LLM/Embedding/Tracing，不启用第二套框架 Cache。
- 不通过 SimpleDirectoryReader、VectorStoreIndex.from_documents、IngestionPipeline 或 PGVectorStore 接管生产流程。Adapter 互转不证明当前数据库状态/模型相容性；未来 Retriever 必须另行限定 READY/模型版本及权限，不在 15A 提前实现。

### 预置导入

- 素材及 manifest 由用户准备，不自动生成/下载；预检的格式/Hash/标签覆盖不等于内容质量验收。默认 dry-run，正式执行必须显式确认云外传/费用。
- 公开基准仅 OHR-Bench、Microsoft OCB、OmniDocBench；官方 QA/GT/断言/来源与报告留在 docs/test，不导入业务库。离线诊断不是官方总分，READY 不证明解析正确或问答准确；缺格式/原件/页映射/授权时报告真实缺口，不转换伪造或降低预检标准。
- 统一 download_benchmarks 由用户运行，固定项目根目录、官方版本及镜像 API/resolve，安全续传后独占发布完整输入；不覆盖已有原件/审核/报告，不清缓存。OCB 只自动取已审核且 QA 依赖完整的托管原生 Office，缺审核仅初始化 []；三个 prepare 工具仍保持离线，不以下载成功代替许可/质量验收。
- 复用原存储、Router 与各阶段检查点，单文件单 doc_id/source_type=preset；同内容不覆盖原来源/文件名、不自动重投失败或已有检查点、不隐式 Force Reparse。CLI 有界查数据库，不直接解析/推理/轮询 MinerU，不安装或启动服务。
- 全批 READY 须验证入库身份及全量向量；中断/超时不取消后台任务，脚本退出后无下一阶段自动补投。当前数据准备完成不代表检索/问答已实现。

### Dense Retrieval

- 复用已有 DKFEmbeddingAdapter/进程共享 BGE-M3，实际模型身份前后核对；不接收未绑定模型身份的外部 Query 向量/变换文本，不重编码文档，不启用框架存储或 Query Engine。
- 指定 kb_id/ACTIVE 库、可选 doc_ids、READY、非空向量、完整模型身份及当前解析/切片/GPU代次在候选SQL内过滤，再做Top-K。doc_ids冻结为非零UUID元组，最多100个；None保留全库语义，显式空/非法范围拒绝。不存在/跨库/不合格文档不扩大范围；越界返回行全批拒绝，不靠Python删行。独立只读短会话、有界超时，不flush/commit或共享线程Session。
- 首版物化过滤候选后精确余弦距离/稳定 chunk_id Top-K（默认 20，上限 100），保留既有 HNSW 但不声称使用 ANN；大库扫描成本待验收。1-distance 是相似度而非概率，空集不扩大范围/触发重建；Node 原 ID/正文/全部证据不变。
- 公共入口不发基类含正文的 callback payload、不递归检索，业务日志只记 UUID/Top-K/数量/耗时/安全错误；不注册全局 Tracing，第三方 instrumentation/SQL echo 仍须禁用。结果代表 SQL 查询快照，不保证查询后文档仍 READY。

### 本地 BM25

- 仅从同kb_id/ACTIVE库、可选doc_ids、READY及当前代次的document_chunks派生原TextNode；不读原件、重切片/Embedding或建立新业务表。BM25无Query向量，不要求当前BGE模型身份匹配，原Node证据及存储模型身份仍保留。
- 使用已声明rank-bm25的BM25Plus与版本化词项规则，稳定同分UUID；只返回真实词项重叠，分数不是概率，不与Dense直接相加。中文字符/二元片段存在部分匹配边界，不宣称专业分词或语义准确率。
- 实例内词频索引可重建，每次先查当前READY快照；内容/集合/版本变化重建，返回Node只用当前证据。失败不返回旧缓存，不写磁盘/pickle；超限报错，不静默截断全库语料。批量转换可省略逐项成功日志，但保留汇总及错误，默认Adapter行为不变。
- 阶段33可显式传入受检CorpusLimits放大离线全量语料预算，生产默认10000 Chunk/32MiB不变；预算不改变分词/评分/隔离，超限全批失败不截断。正式评测只读固定200份独立库，QA真值不作检索doc_ids过滤；原页证据指标和OCB依赖文档代理指标分开，未执行/不可评分不伪造分数。参见[阶段33](docs/STAGE33_EVALUATION.md)。
- 阶段33`--retry-failed-only`仅在原run/import/resume下恢复启动时FAILED且数据库同Hash/doc_id的记录，不调用上传、不重建READY；全200份/4087题及未完成状态保留。旧重试意图不清除、不盲重发，已有在途任务占并发；仍需用户明确外传/后续分片费用并手动执行，不把恢复模式冒充零费用或全量完成。

### Hybrid Retrieval / RRF

- LlamaIndex BaseRetriever组合已有Dense/BM25，同kb_id/doc_ids、每支默认20项；原Query各调用一次，只Dense调用既有Query编码。无默认LLM、Query Rewrite、多查询扩展、框架索引或新真相表。
- 等权RRF按1-based排名计算，默认k=60；以稳定chunk_id去重，同分按UUID排序，不按正文hash合并或混加原分数。Node原正文/证据不变，仅增加类型化fusion来源及原排名/分数，DTO互转保留，不混入Embedding/LLM文本。
- 跨语言优化先用独立只读实验和可靠Chunk/页真值量化；人工平行表达不得带答案。实验多查询RRF分留独立报告，不冒充既有双支来源或自动启用生产策略；Mock词项命中不证明真实Dense/Reranker提升，不为单题拆表/改证据。
- 两路各自继承READY/当前代次/范围约束，Dense仍核对模型身份；BM25-only证据不冒充同模型向量命中。独立短快照不保证跨支同一事务；重复Chunk证据冲突、越界/坏行或分支异常全批失败，正常空支可融合另一支，不自动扩大范围/降级/重试。

### BGE Reranker

- 在线默认同步，项目进程共享惰性服务及锁内批处理；离线本地Safetensors/禁止远程代码，不下载或CUDA失败回退CPU。明确CPU配置用FP32；不与Embedding服务共享不同模型权重，8GB显存双模型与长输入需人工验证。
- 只评分原Query+完整原node.text，不加metadata/指令、不重Embedding/Parse/读库。真实成对token超限、坏输入/输出/身份变化或推理失败全批拒绝，不截断证据、不静默返回原排序；空候选不加载模型。
- 默认Top5，原始有限logit降序、同分UUID稳定排序；Node当前score为rerank_score，DTO保留原retrieval_score及fusion_score。来源仅运行时rerank字段，原ID/正文/页/章节/source_spans不变且深复制；重复重排/重复Chunk/跨库候选拒绝。效果提升需同批候选、真实QA/证据人工对照，不以Stub排序或Top5存在宣称验收通过。

### LLM RAG / Response Synthesis

- `LLM_PROVIDER=deepseek/custom`为正式主选择器，默认deepseek；两个服务各自冻结本方Base URL/Key/Model/预算/超时，不混用。默认未选中Key可空；Fallback启用时须预检双方配置。旧official兼容，旧sub2api未迁移报错，不推测密钥或静默换接收方。
- LlamaIndex `DKFLLMAdapter`仅包装统一工厂服务；HTTP不在Query Engine/Agent节点散落。至多5条最终Evidence一次逻辑合成，无Refine/同方重试/流式半成品。唯一切换例外：LLM_FALLBACK_ENABLED=true、主custom/备用deepseek、本次allow_cloud=true且确认双方外传/潜在额外费用时，网络/连接读取超时、HTTP5xx/429允许一次备用，最多2POST。普通4xx（除429）、配置/权限/参数、截断/坏JSON/引用、模型不足/公式不支持均不切换；备用失败即止，不回custom。
- 自定义API必须受用户信任且兼容JSON-mode；默认禁止HTTP，临时测试仅由用户明确`CUSTOM_LLM_ALLOW_INSECURE_HTTP=true`允许，正式部署须HTTPS。不得关闭TLS验证、不读代理、不跟随跳转；官方始终只接受官方HTTPS。日志只增加固定提供方枚举/安全模型名，禁记完整URL、Key或上游原文。
- Python问答默认拒绝云调用，allow_cloud=True仅在本次外传/费用已确认后使用，Fallback开启还须确认双方。许可通过短生命周期ContextVar传递并在异常/嵌套退出时恢复，不能由工厂构造永久授权。TLS/直连/无跳转保持；主超时不等于未计费，不自动重放Agent。双方收到相同原Prompt/上下文/JSON要求，业务校验仍执行；实际返回方见llm.call_finished，不修改共享服务的last_provider或在日志输出正文/Key。
- 固定审查Prompt把问题与证据视为数据；仅最终正文与稳定Chunk ID上云，不发送其他候选、私有路径或模型身份。完整上下文超字符预算失败，不截断证据；字符数不冒充供应商token数。
- LLM返回逐事实及最终ID白名单；引用原文/来源/页码/章节/source_spans/三类分数只从原Node回填。空证据或明确不足才返回insufficient_evidence；检索/模型/HTTP/非法引用/截断失败必须报安全异常，不伪装成功或不足。引用合法仅证明来源存在，忠实度/数字事实及Prompt注入抗性仍须真实人工验收。
- 逐事实判断支持：允许忠实翻译/概括及有证据的部分回答，明确其余缺口；无关片段或局部冲突不否定独立受支持事实。没有可靠可答内容仍拒答；不得因候选非空/关键词/高分把模型true强制改成false。诊断区分empty_final_evidence与model_judged_insufficient，不保留模型拒答原文或为误拒答自动重试。
- 不走框架默认含正文的Query/LLM/Synthesis callback；不启用全局Tracing，日志只记阶段/范围/数量/状态/耗时/固定错误码。CLI使用集中注册`rag-cli`；不记录问题、Prompt、答案、证据正文、Key或上游原文。

### Citation / Evidence

- 项目Citation与来源Trace不依赖LlamaIndex类型；原Node/DTO深复制、稳定Chunk身份、完整来源及三类分数不丢失，未知值保留None，不采用LLM生成的文件名/页码。回答数字标记与本地Citation编号一致，每条claim仍受最终ID白名单约束。
- 显式EvidenceService仅在独立REPEATABLE READ只读快照核对指定kb/doc/Chunk、ACTIVE/READY、当前解析/切片检查点、整Block Hash及Unicode字符区间。缺字段、过期引用、越界或正文不一致全批失败；不读原件、不修复来源、不重Parse/Embedding、无flush/commit或自动重试。
- 沿用PDF物理页、图片像素页及Office逻辑页/空bbox；保留context/overlap与结构块原文，不去重来源片段或重排。来源核对不是答案语义验真，也不是用户鉴权；现有RAG返回引用快照，调用方显式核对当前来源，不伪称每次生成都已查库验真。
- 来源日志复用每日合并文件和集中注册evidence-cli，仅ID/数量/耗时/安全错误；新专题用例只编写，真实DB来源及回答忠实度由用户验收。

### Knowledge Query API

- /api/knowledge/search和/query只用DKF请求/响应与KnowledgeService，不导入/返回LlamaIndex类型。每次固定kb_id/doc_ids，Top-K是融合候选上限（默认30、1～100），最终沿用Top5重排；不重Parse/Embedding/Index，不扩大范围或创建新存储。
- /query的allow_cloud默认false，明确true才允许已有逻辑云合成；Fallback启用时须确认双方，最多按上述策略2POST；/search不调用LLM。异常不是空集/不足，错误只返固定安全code/message，不回显正文/上游原文，不对同方自动重试或重放Agent。
- API同步工作在线程池执行，模型由项目共享服务惰性加载；模型/API部署与验收由用户执行。现阶段无鉴权/RBAC/请求幂等或取消保证，仅本机/可信内网使用；云许可标记不是用户权限，HTTP请求重跑可能再次收费。

### 公式语义结构化

- 仅显式extract_formula处理当前ACTIVE/READY指定Chunk中的完整MinerU formula Block；复用EvidenceService的Hash/字符/代次/页语义核对，邻近上下文只取同Chunk相邻两块，缺失/片段不猜测补全。不改原式/OCR，不自动在上传/Query插入付费调用。
- 唯一LLM工厂尊重主LLM_PROVIDER，显式allow_cloud=True才允许公式/上下文外传；默认单请求，仅上述显式双方授权策略允许运输故障后一次备用。原式/名称/变量及引文校验不变，坏内容不是切换理由；来源ID/页/章节/bbox由Evidence回填，未知含义为null，不编造值或单位。
- normalized_formula只做标准库有界AST白名单校验及四则规范化，不求值、不用eval/exec/parse_expr，不调用SymPy；不支持函数/幂/不定项求和等时明确失败或unsupported。formula_id绑定原件/解析/Chunk/Block身份；来源仅为读取快照，不建立第二套真相表或公式缓存。
- success仅指结构/引用通过，semantic_validation保持manual_pending；数学等价、百分比口径与名称翻译须人工核验，不能用合法引文宣称语义验真。提取入口不绑定/计算，计算仅走阶段29显式入口；日志复用每日文件与注册formula-cli角色，禁止记录原式、规范式、变量含义、上下文或模型原文。
- 阶段28只消费已提取定义：显式用户参数标为user_input并保留input_id/原值；文档参数须同kb、显式doc/chunk/block及唯一引文，由EvidenceService重新核对READY/Hash/代次/页语义。公式快照/原式/变量/引用须与当前来源一致，同文档多Chunk代次冲突全批拒绝，不自动重提取/检索/调用LLM。
- 仅识别严格有限十进制与合法千分组，精确字符串无隐式float/舍入；零值有效、缺值返回need_variables及准确符号。未知/重复/冲突输入失败，不猜值、不填零、不默认覆盖；单位不明保留null，有文档单位须在同引文中紧邻数值，不猜表头单位或跨币种/百分比换算。
- 参数来源保持数值/单位与Block/Chunk双端字符引用及原页语义；semantic_validation/unit_validation保持manual_pending，不证明期间、量纲或参数含义正确。复用formula_binding白名单过程/数量日志和formula-binding-cli集中角色，禁止记录参数名/值/单位/正文；阶段28不求值、不写库、不自动收费。
- 阶段29只接受FormulaBindingRequest/Result，复用阶段28当前来源核对；已有结果须从raw_value/文档引文重建并整份比对，不信任篡改的value/偏移。缺参保留need_variables，不导入计算引擎或填值，不重提取公式/检索/付费。
- 白名单有界AST逐节点构造SymPy Symbol/Rational/Add/Mul及内部倒数，仅允许原四则/一元正负。禁止eval/exec、sympify/parse_expr/parse_latex/lambdify、函数/属性/索引/用户幂；每个原始除法先检查数值分母，不能靠消项/乘零绕过。各中间有理数分子/分母至多4096二进制位，不返回半份结果。
- 结果保留精确分数、最多12位半偶舍入展示、代入映射/算式/步骤、实际SymPy版本及公式/变量来源；同输入/来源/引擎版本有稳定calculation_id。仅机械算术，不推断/换算单位或百分比，语义及单位仍manual_pending。复用formula_calculation脱敏事件和formula-calculation-cli，不记录数值、算式、结果或正文，不写业务库/缓存。

### Knowledge Agent

- LangGraph只负责有限StateGraph/条件边/计步及安全错误；项目KnowledgeTools只委托原Search/Query/Formula Service，不复制SQL、HTTP、检索/模型或计算。不引入第二套Retriever/VectorStore/RAG、AgentExecutor或Coordinator。
- 显式intent决定路径，auto有公式选择才处理公式，否则Query；公式必须准确doc/chunk/block及显式参数，不按关键词猜公式/值。kb/doc_ids贯穿并复核全部返回范围；Question仅数据，不能生成节点名或权限，非法路由直接失败。复用旧成功定义可跳过提取，但计算仍核对当前来源。
- Query/首次提取须本次allow_cloud=true，搜索/复用定义计算无云调用。KNOWLEDGE_AGENT_MAX_STEPS限定2～32，默认8，含终结；预算不足Tool前拒绝，无自动循环/重试/收费重放。Tool错误清空半成品，不能伪装不足或成功；无持久化请求幂等，人工重跑可能再次收费。
- State只含JSON项目数据，不含Session/模型/框架Node/原始异常；对外仅KnowledgeAgentResponse。Citation/原metadata及Formula/EvidenceTrace沿用业务契约，不补页/单位或宣称语义已验真。默认禁用Tracing/callback，不持久化敏感State；日志仅关联agent_run_id、固定路由/Tool/终态、步数耗时，集中knowledge-agent-cli角色。

### Admin文档管理与动态增量

- 新上传（普通auto/native/mineru及显式云上传）自动建立ingest编排，保留单文件doc_id与原响应契约，按原阶段任务推进至READY；历史文档不自动接管，显式Retry/Reindex/Reparse/Delete及旧逐阶段/预置CLI语义保留。复用现有management字段，不新增业务表/全库重建。
- 独立management检查点绑定当前解析代次；Router转云时在创建Job的同一事务换绑source_parse_task_id/Job标识，管理UUID不变，不重复提交云批次。首阶段意图先提交，由原上传/Router发送；编排只接续当前文档，云失败不降级Native、不盲目补投收费任务。
- 每次短任务只推进一阶段，等待只读本地状态/countdown，有截止时间，无阻塞循环/供应商额外轮询。只有既有Index全量向量事务可置READY；READY自动进入同ACTIVE库及同模型查询，Dense读当前SQL、BM25刷新派生快照，不重Embedding旧文档或重建全库。
- 阶段投递意图先提交，完成状态以Document为准；确认丢失/崩溃缺口由显式Retry恢复，同一阶段不盲目重复补投。Retry恢复最下游检查点/同一活动云Job，不继承Force；只有明确Reparse才允许绕过缓存及收费历史。
- JOB_TIMEOUT仅代表本地观察停止；已上传原Batch先GET确认远端状态，done可回收。显式Retry仅为当前代次/同Hash/已上传原Batch续开有界本地观察窗口，保留Job/Data/Batch/创建提交时间/重试预算；不自动复活远端失败、未知提交、坏结果或申请新收费批次。
- MinerU云结果获取与本地Adapter必须解耦：原ZIP/回执独立保留，不因Adapter失败重新收费或要求云访问许可。本地Adapter/Guard错误Retry核对同代次/Hash/Batch/冻结版本后强制`_local_replay_only`，不初始化云客户端/Token/限流，不GET/PUT/POST/下载；反复修代码只重放同一原ZIP。后续分片缺本地结果则LOCAL_RESULT_REQUIRED停止，不自动转云或写部分Block/READY。新云任务另行授权，不以Force、删校验或改SQL状态解决本地兼容问题；正文/页码/坐标/ID须正确，未知结构需修通用兼容而非归罪原文件。
- Delete为202异步请求，文档先退出可检索状态并阻断旧入口；活动云/GPU/入库或被锁行拒绝。只删除UUID命名的服务自有原件、文档缓存/Hybrid/GPU目录/Job指针及无其他文档引用的整份缓存；DB级联移除Block/Chunk/向量/Job，不删用户preset、模型、日志或全局HNSW。
- 清理前验证绝对边界和符号链接/junction，全树预检后逐项重验；文件/SQL非原子，部分失败保留删除检查点供Retry，不假装删除完成。跨文档共享及无法证明独占的视觉缓存保留并报告，不清空缓存根；缓存访问/发布/清理共用固定OS锁，运行中不删锁，不保证共享盘安全。
- 详情/状态不返回私有配置/路径/密钥/签名URL；Block/Chunk原证据按稳定序号分页，向量只返统计。所有维护复用每日中文白名单日志；202、文件存在或READY不证明质量验收，无RBAC，仅可信内网。

## 5. 专题文档与交付要求

| 修改范围 | 必须阅读 |
|---|---|
| 路线、阶段验收、最终协议 | [开发计划](docs/DEVELOPMENT_PLAN_CODEX.md) |
| Parser、页码/坐标、持久化与回归 | [Parser 契约](docs/PARSER_CONTRACT.md) |
| 云接口、限流、检查点、顺序与真实验收 | [MinerU Cloud](docs/MINERU_CLOUD.md) |
| 预检、决策、Guard、XLSX 对照 | [Parser Router](docs/PARSER_ROUTER.md) |
| XLSX 候选、Bundle、检查点、合并与人工验收 | [Selective Hybrid](docs/SELECTIVE_HYBRID.md) |
| 日志事件、脱敏、并发写入与验收 | [业务日志](docs/BUSINESS_LOGGING.md) |
| 切片、字符证据、检查点与人工验收 | [Chunk](docs/CHUNK.md) |
| 本地模型、GPU 批处理、向量结果与人工验收 | [Embedding](docs/EMBEDDING.md) |
| 向量入库、HNSW、READY 与人工验收 | [pgvector 入库](docs/PGVECTOR_INDEX.md) |
| LlamaIndex Node/证据 DTO、Query 边界与人工验收 | [LlamaIndex Adapter](docs/LLAMA_INDEX_ADAPTER.md) |
| Dense SQL/模型隔离、Top-K、日志与人工验收 | [Dense Retrieval](docs/DENSE_RETRIEVAL.md) |
| BM25词项、READY快照、派生缓存及人工验收 | [BM25 Retriever](docs/BM25_RETRIEVAL.md) |
| 两路组合、RRF来源、冲突边界及人工验收 | [Hybrid Retrieval](docs/HYBRID_RETRIEVAL.md) |
| 本地重排、分数/证据、部署及质量验收 | [BGE Reranker](docs/RERANKER.md) |
| 问答编排、LLM边界、最终引用与人工验收 | [DeepSeek RAG](docs/DEEPSEEK_RAG.md) |
| 引用契约、Block字符反查、页语义与失效边界 | [Citation / Evidence](docs/CITATION_EVIDENCE.md) |
| 搜索/问答HTTP契约、云许可及安全错误 | [Knowledge Query API](docs/KNOWLEDGE_QUERY_API.md) |
| 管理接口、恢复编排、删除所有权及人工验收 | [Admin文档管理](docs/DOCUMENT_MANAGEMENT.md) |
| 阶段26云miss/费用前置/本地超时与跨语言对照 | [阶段26补充](docs/STAGE26_SUPPLEMENT.md) |
| 全200原件/4087题导入、四策略、断点与评分分母 | [阶段33评测](docs/STAGE33_EVALUATION.md) |
| 公式提取、表达式语法、语义来源及人工验收 | [公式语义结构化](docs/FORMULA_SEMANTICS.md) |
| Agent状态/路由、薄Tool、费用与有限图验收 | [Knowledge Agent](docs/KNOWLEDGE_AGENT.md) |
| 预置素材、导入与阶段推进、幂等及人工验收 | [预置导入](docs/PRESET_IMPORT.md) |
| 部署与接口使用 | [README](README.md) |

完成任务后说明完成内容、修改文件、自动验证结果和未验证能力；给出具体人工测试命令/步骤，不只说“请测试”。外部 API 必须提醒真实人工验证，Mock 不能替代真实结果。实际变更附一条中文 Commit Message；每次回答都给出与当前阶段相关的“下一步建议”，按用户要求停止，不擅自推进后续阶段。
