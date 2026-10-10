# AGENTS.md

## 1. 项目身份

- 项目名称：数知融问智能体
- English：Data-Knowledge Fusion Agent
- 简称：DKF-Agent
- 项目目录：`dkf-agent`
- 当前负责模块：`knowledge-service`
- 当前开发范围：**非结构化知识问答**

除非用户明确要求，不优先开发 NL2SQL、Schema Linking、Coordinator、Fusion 或其他成员负责的模块。

---

## 2. 当前开发目标

当前目标是完成一个**可独立运行、可独立测试、可独立演示**的 Knowledge Base Service。

核心链路：

```text
Knowledge Ingestion
知识库文档
→ Native Parser / MinerU Cloud（复杂文档精准解析）
→ Chunk
→ BGE-M3
→ pgvector
→ READY

Knowledge Query
Query
→ Dense Retrieval + BM25
→ bge-reranker-v2-m3
→ DeepSeek RAG
→ Answer + Evidence
```

预置知识库是主要比赛模式；管理员动态上传是知识库增量入库能力。

---

## 3. 赛题目标：仅包含非结构化问答相关部分

以下要求来自赛题文档，开发时必须优先满足。

### 3.1 基础任务

必须完成：

- 搭建一个可交互的 Demo，非结构化模块至少能够独立完成知识库问答演示。
- 实现**单文档简单检索问答（RAG）**。
- 系统回答必须进行基本来源展示，例如展示 / 高亮引用的文档片段。
- 单文档 RAG 需要准备不少于 **10 个测试用例**进行客观评估。

### 3.2 RAG 技术实现要求

技术实现需要覆盖：

- 文档解析。
- 文档切片策略。
- 向量检索方案。
- 生成融合机制。
- 数据来源标注。
- 系统测试与性能评估。

知识库提交样本应：

- 不少于 **10 份文档**。
- 覆盖不同文档格式。

### 3.3 RAG 质量目标

需要评估生成答案与检索内容之间的相关性和事实准确性。

开发目标：

- RAG 答案忠实度优先达到 **90% 及以上**。
- 对检索、重排、生成分别记录耗时。
- 后续技术文档应能够说明核心链路复杂度、不同数据规模下的性能，以及瓶颈和优化策略。

### 3.4 中级任务 6：文档公式识别与计算

本模块重点攻克：

**6a：公式输入参数识别**

- 从 PDF 等非结构化文档中识别计算公式所需要的输入参数。
- 参数需要能够关联到原始文档证据。

**6b：公式结构解析与计算**

- 正确识别公式本身的结构和计算逻辑。
- 将公式标准化为可安全执行的表达式。
- 使用 SymPy 等安全计算方式得到结果。
- 结果必须能够追溯公式来源和参数来源。

参数不足时不得猜测，应返回类似：

```json
{
  "status": "need_variables",
  "required_variables": ["revenue", "cost"]
}
```

禁止使用 `eval()` 执行模型生成的公式。

---

## 4. 当前工程目标

### 4.1 Knowledge Base

需要支持：

- 预置知识库。
- 不少于 10 份多格式知识文档。
- PDF。
- DOCX。
- TXT / Markdown。
- JPG / PNG。
- 扫描 PDF。
- 文档状态管理。
- Hash 去重。
- Delete。
- Reindex。

### 4.2 Admin Module

管理员侧负责知识库建设和维护：

```text
上传 / 批量上传
→ Celery
→ Native Parse / MinerU Cloud
→ 查看解析结果
→ Chunk
→ Embedding
→ Index
→ READY
```

管理员应能够查看：

- 文档名称。
- 文档格式。
- 处理状态。
- 解析结果。
- Chunk 结果。
- Chunk 数量。
- 索引状态。
- 失败原因。

### 4.3 Knowledge Query

至少支持：

- 全知识库检索。
- 单文档检索。
- Dense Retrieval。
- BM25。
- Hybrid Retrieval。
- Reranker。
- DeepSeek RAG。
- Evidence。
- 文件名引用。
- 页码引用。
- 证据不足判断。

---

## 5. 技术约束

固定技术栈：

```text
Python 3.11
FastAPI
PostgreSQL + pgvector
Celery + Redis
DeepSeek API
BAAI/bge-m3
BAAI/bge-reranker-v2-m3
PyMuPDF
python-docx
MinerU Cloud 官方精准解析 API V4（默认 vlm）
SymPy
LangChain
LangGraph
Docker Compose
```

开发阶段不要自行引入：

- Elasticsearch。
- 知识图谱。
- 本地大型视觉语言模型。
- 新的 Agent 类型。
- Kubernetes。
- 模型微调。
- 与当前赛题目标无关的大型依赖。

如确实需要新增技术或依赖，必须先说明原因和替代方案，再等待确认。

### 5.1 Windows 本地开发环境固定约束

当前基础服务由开发者手动部署和管理：

- PostgreSQL 17 原生安装于 Windows，默认地址 `127.0.0.1:5432`；项目数据库为 `dkf_agent`，项目用户为 `dkf_user`，应用通过 `DATABASE_URL` 连接。
- pgvector 在 Windows 原生编译安装；必须使用 Visual Studio 2022 Build Tools 的 **x64 C++ 工具链**，禁止使用 x86 工具链。`PGROOT` 指向 `C:\Program Files\PostgreSQL\17`。由开发者在 `dkf_agent` 数据库中执行 `CREATE EXTENSION vector;`，通过 `pg_extension` 查询验证启用状态。
- Redis 运行于 Windows 本地，沿用已有原生安装，作为 Windows 服务后台运行；默认地址 `127.0.0.1:6379`、`REDIS_URL=redis://127.0.0.1:6379/0`，不通过 WSL 部署。当前已有 Redis 5.0.14.1 仅作为本机开发环境，不作为生产推荐；服务器或 Python 客户端升级需先核对兼容性，不自行替换安装方案。

Codex 必须遵守：

1. 不自动创建 `docker-compose.yml` 来部署 PostgreSQL 或 Redis。
2. 不自动修改、安装 PostgreSQL / Redis 系统软件，不自动编译安装 pgvector，不修改系统服务。
3. 应用代码只负责通过环境变量连接现有基础服务，不承担服务安装或启动工作。
4. 测试 Redis 前，先通过 `Get-Service Redis` 确认本地服务为 `Running`（服务名以实际安装为准）；Redis 不依赖 WSL 状态，不要求开启 Ubuntu。
5. Redis 连接失败时，不直接修改 Python 代码；先依次检查 Windows 服务状态、服务配置中的绑定地址与端口、6379 是否监听、本地 `redis-cli ping` 是否返回 `PONG`，再核对 `REDIS_URL`、Celery Broker 与 Result Backend 的 URL 是否指向同一本地服务。需要调整服务时交由用户手动处理。
6. Docker Compose 保留为未来整体部署方案，当前 Windows 开发环境不使用 Docker 部署 PostgreSQL 或 Redis；除非用户明确要求，不恢复 Docker 基础服务或 WSL Redis 部署。

本节优先于开发计划或其他文档中遗留的 Docker 基础服务或 WSL Redis 部署说明。开发顺序不变，但当前 PostgreSQL、pgvector 和 Redis 的部署方式以上述约束为准。

---

## 6. 开发规则

### 6.1 中文注释和可解释性

每次新增或修改代码时，必须保留适量中文注释，并为每个函数提供中文说明，不得遗漏。

函数说明要求：

- Python 函数在函数体开头使用中文文档字符串（docstring）；其他语言使用对应的函数注释形式。
- 适用于普通函数、异步函数、类方法、私有辅助函数、嵌套函数、测试函数和数据库迁移函数。
- 至少说明函数用途；根据实际复杂度补充关键参数、返回值、事务或其他副作用、异常与边界条件，避免只复述函数名。
- 新增或修改函数时，同步新增或更新中文说明，确保说明与实现一致。
- 生成代码的模板也必须包含函数中文说明，避免后续生成的代码遗漏注释。

重点注释：

- 为什么采用该实现。
- 关键业务流程。
- 非显而易见的算法逻辑。
- 数据结构字段意义。
- 异常处理和边界条件。

不要对显而易见的单行代码进行无意义注释。

系统设计必须保证可解释性：

```text
Answer
→ Evidence
→ Chunk
→ Page
→ Document
```

### 6.2 Git 规则

每次代码发生实际变动后：

1. 不自动执行 `git push`。
2. 除非用户明确要求，也不要自行提交 Git Commit。
3. 回答结尾必须提供一条适合本次修改的**中文 Commit Message**，供用户手动提交和推送。

格式示例：

```text
feat: 完成知识库文档异步解析与索引构建
```

或：

```text
fix: 修复扫描 PDF OCR 结果重复入库问题
```

### 6.3 每次回答必须给出下一步建议

每次完成回答后，必须增加：

```text
下一步建议：...
```

建议必须和当前开发顺序直接相关，不泛泛而谈。

### 6.4 每个任务完成后必须提醒人工测试

完成任何功能后，必须明确提醒用户进行手动测试。

例如完成 OCR API 接入后，需要提醒测试：

- API Key 是否有效。
- 是否能成功调用 OCR API。
- JPG / PNG 是否能正常识别。
- 扫描 PDF 是否能逐页处理。
- 异常或超时是否能正确 Retry。
- OCR 结果是否能正常进入后续 Chunk / RAG。

完成接口后应给出具体测试命令或测试步骤，不允许只说“请测试一下”。

### 6.5 API 接入规则

每接入一个外部 API，至少验证：

```text
连接成功
↓
鉴权成功
↓
最小请求成功
↓
返回格式正确
↓
业务功能正确
↓
异常处理正确
```

如果没有实际完成测试，不得声称“API 已验证可用”，只能说明“代码已接入，等待人工测试”。

### 6.5.1 环境变量维护规则

每次新增、修改或废弃环境变量时，必须同步维护：

- 本地运行使用的 `.env`。
- 可提交到仓库的 `.env.example`。
- 使用说明中涉及该变量的配置文档（例如 `README.md`）。

`.env.example` 不得包含真实密钥、密码或生产连接串；`.env` 必须保持在 Git 忽略规则中。

环境变量文件必须按用途分组并保持固定顺序，而不是在文件末尾零散追加变量。推荐分组顺序：

```text
应用运行配置
数据库与缓存
异步任务
模型与 LLM
OCR / 外部服务
存储与可选功能
```

每个分组使用简短中文注释说明用途；新增变量应放入对应分组，并在 `.env` 与 `.env.example` 中保持相同的变量名和排列顺序。若变量仅用于某一环境，也应在 `.env.example` 中以空值或安全示例值明确保留，避免部署遗漏。

### 6.5.2 部署与接口文档同步规则

每次变更会影响项目启动、部署、调用或模型运行方式时，必须在同一次修改中同步更新 `README.md`；未同步 README 的变更不得视为完成。

至少包括以下情形：

- 前端或后端的启动命令、启动顺序、端口、环境要求与 Docker Compose 服务。
- 新增、修改或废弃 API 的路径、方法、请求参数、响应结构、鉴权方式与错误处理。
- Celery Worker、队列、并发限制、数据库迁移、缓存或其他基础服务的运行方式。
- Embedding、Reranker、OCR、LLM 等模型或外部服务的下载、加载、部署、配置与硬件要求。

README 中应提供当前可执行的命令、访问地址或调用示例、预期结果，以及使用者需要知道的支持范围和限制；不得把计划中的命令描述为已可用能力。只更新受影响的使用说明，不因完成一个开发阶段而追加阶段总结。

### 6.5.3 文档职责与内容边界

按读者和用途维护文档，不将 README 当作开发记录：

- `README.md` 面向部署者和使用者，是项目说明书：说明项目用途、运行环境、配置、安装部署、启动顺序、接口使用、必要限制及部署故障排查。
- `AGENTS.md` 面向开发智能体：保存开发规则、模块范围、阶段边界、当前工程基线，以及需要后续开发遵守的实现约定。
- `docs/DEVELOPMENT_PLAN_CODEX.md` 保存开发路线、阶段目标和验收标准；专题契约或设计文档保存详细技术说明，测试代码维护在 `tests/` 中。
- 根目录的项目说明文档只保留 `AGENTS.md` 和 `README.md`；开发计划、契约、设计和人工验收文档统一放入 `docs/`，旧计划放入 `docs/archive/` 并标明仅供历史参考。移动文档时同步维护相对链接，不覆盖最新计划；源代码、配置和环境文件不因本规则迁出根目录。
- 新增或移动文件、目录时，判断是否需要同步 `.gitignore`。`docs/` 中的工程文档必须纳入版本控制，赛题附件、验收素材和本地资料按需忽略，不能使用整目录忽略导致开发规则或验收文档无法提交。
- 每个阶段做了什么、内部类与字段设计、事务及幂等实现、后续开发计划，不写入 README。需要持续保留的阶段信息简要更新本文件第 7.1 节，不逐次堆积修改日志。
- README 不包含业务测试脚本、pytest 命令、测试结果统计或开发验收记录；可以保留用于确认服务部署成功的检查，以及必要的 API 调用说明。
- 当前能力的简短限制仍需在 README 说明，例如“上传成功不代表可检索”，避免误导使用者；不要展开对应阶段的开发过程。
- 部署与接口发生实际变化时，仍必须遵守第 6.5.2 节同步 README；仅增加内部契约、单元测试或开发约定，且不影响使用方式时，不向 README 追加实现总结。

### 6.6 异步任务规则

耗时入库任务使用 Celery：

- Parser。
- OCR。
- Chunk。
- Embedding。
- Index。
- Reindex。
- 批量评测。

普通在线 RAG 默认同步执行。

Celery 任务要求：

- 可重试。
- 幂等。
- 不重复产生 Chunk / Embedding。
- 失败时记录错误原因。

GPU Worker 默认：

```text
concurrency = 1
```

避免 RTX 4060 8GB 环境中多个进程重复加载模型。

### 6.6.1 当前文档任务框架的实现约定

以下约定来自现有框架，扩展 Parser 或后续处理时必须保留；它们不是完整入库能力：

- 原件与 Document 提交后才投递 `process_document` 到 `default_queue`。任务使用晚确认、文档行锁和当前 `processing_task_id` 检查，重复或过期消息不能回退状态、覆盖新任务或重复生成数据。
- 临时数据库或文件访问错误最多自动重试 3 次，含首次共 4 次，退避为 5、10、20 秒；原件缺失或为空立即失败。失败记录 `error_message`，成功重试清除旧错误。数据库不可用时可能无法持久化失败原因，仍需保留安全的 Worker 日志和任务失败状态，不泄露凭据。
- 重新投递只接收 `UPLOADED / FAILED`，提交新任务标识并清除旧错误；不能绕过状态限制将其当作 Reindex。旧消息与失败回调不得覆盖新一轮任务。
- 当前原生任务解析 TXT、Markdown、DOCX、文本型 PDF，将 Block 写入 `document_blocks` 后推进到 `CHUNKING / awaiting_chunk`。此状态仅表示等待切片，不生成 Chunk、Embedding 或 `READY`；图片和需要 OCR 的 PDF 明确失败，不生成半份结果。
- 解析、该文档旧 Block 的整体替换和状态推进在同一文档行锁与数据库事务内完成；失败必须回滚全部写入，再以独立事务记录安全原因。长事务中的 `PARSING` 不保证实时可见。当前代次完成后的重复消息跳过，结果可能为 `skipped`，业务状态以 Document 为准。
- 同一原件和 Parser 版本使用稳定 Block ID，入库将块列表映射为从 1 开始的 `block_index`；保持文档内序号唯一和正数约束。历史块序号允许空值，不伪造历史阅读顺序；后续查询新解析结果应按 `block_index` 排序。
- 当前没有事务 Outbox、自动扫描或后台补投器；数据库提交与消息投递并非原子操作。进程中断可能留下已有原件和记录但未投递的文档，应使用已有重新投递入口处理，不能宣称系统会自动补投。
- 文件系统与数据库也非同一事务。普通写盘或入库失败清理本次文件；提交结果不确定时保留原件，避免误删可能已提交的数据。异常和崩溃恢复不得假设文件与记录天然保持原子一致。
- 上传按实际内容计算 SHA256、检查大小和基本格式标识，不依赖客户端大小或 MIME 声明；这些检查不是完整格式解析或恶意文件扫描，文本编码留待 Parser 判断。
- CPU Worker 在 Windows 使用线程池仅作开发验证，正式 Linux 环境使用 prefork 并发 2～4；GPU Worker 单进程串行，当前不加载模型。不能把线程池等同于 CPU 密集任务的多进程加速。

### 6.6.2 MinerU Cloud 实现约定与阶段边界

- 仅使用 [MinerU 官方精准解析 API V4](https://mineru.net/apiManage/docs)，不使用轻量 Agent API、不部署本地 MinerU 模型；复杂 PDF、扫描件、图片的 OCR、表格与公式识别统一由 MinerU 承担，不继续接入火山 / 百度 OCR。
- HTTP 只能放在 `MinerUCloudProvider`；本地原件采用申请批量上传 URL、签名 PUT、批次 GET 流程。对象存储请求不携带 API Token，不设置上传 Content-Type、不跟随重定向。只接受官方 HTTPS 签名资源域名。
- MinerU 默认 HTTP 客户端固定 `trust_env=False`：API、签名上传及结果下载统一直连，不读取环境或 Windows 系统代理，不自动回退代理。保持 HTTPS 证书校验；该设置不绕过系统 VPN / TUN 路由，不自动修改系统网络。注入客户端仅用于离线测试；不将环境代理可用视为 CDN 已直连可用。
- Token 只读取本地 `MINERU_API_TOKEN`，使用 SecretStr 遮盖表示；不进入日志、前端、Celery 消息或异常详情，不实现到期跟踪、续期或刷新。上游错误原文和签名链接不得透传。
- 提交前校验实际格式、可读性、Hash、200 MB / 200 页限制；批量申请最多 50 文件。应用上传限制仍生效，以更小的限制为准。
- Redis Lua 原子共享频控：提交按文件数计量、查询按次数计量；默认平台上限为 50 文件/分钟、1000 查询/分钟、5000 文件/天，安全系数 0.9。日额度使用保守的滚动 24 小时；1000 优先页按 UTC 日估计，只观测不硬拒绝。相同账户的所有 Worker 必须使用同一 Redis 与命名空间，Redis 故障时关闭云端调用。
- `submit_mineru_parse / check_mineru_result` 是 CPU 短任务。只查一次，未完成通过 countdown 再投递；禁止 while/sleep 等待。已确认临时错误有限指数退避加抖动，并尊重 Retry-After；永久错误立即记录 FAILED，总等待时间有上限。
- `document_parse_jobs` 保存外部六种状态、batch / data / trace 标识、参数快照、重试、错误及提交检查点；Document 保持供应商无关状态。`task_id` 为本地处理代次，不是外部单文件 task_id，也不表示整份文档的 Celery 执行结果。
- 文档行锁、活动解析身份部分唯一索引、当前代次检查和稳定 Block ID 共同保证幂等。POST 前保存申请意图、PUT 前保存 batch，上传恢复沿用同一 batch；已确认远端失败后才自动申请新 batch。POST 响应丢失时安全失败，禁止盲目重复申请；官方接口无已确认的幂等请求键，不得宣称跨数据库与云端绝对 exactly-once。
- 优先读取 ZIP 中唯一的 `content_list.json / *_content_list.json`，通过 `MinerUResultAdapter` 输出现有 ParsedDocument 契约。保留正文原序，0 基页码转 1 基，0～1000 bbox 转原件点数 / 像素；未知置信度保留空值。未知结构、空正文、恶意或超量 ZIP 明确失败，不以 full.md 冒充结构化结果。
- 阅读顺序仅允许按可选旧版 `layout.json` 的父块 `index` 恢复明确的 `header / footer`：同页类型与 bbox 必须全量唯一关联、索引唯一且非负、位置符合页边界，候选结果不能改变任何正文项的相对顺序。bbox 仅用于关联与保护条件，禁止按 y 全局排序。缺少 bbox / 可信索引、关联歧义、多栏并排、纵向回跳或超量页保留整页原序；不宣称能够可靠重建任意多栏布局。结构块保持整体，原 page / bbox / block_type / source 不变；ID 始终基于原数组位置，最终顺序写入 block_index。可选顺序元数据失败不能替代或绕过正文校验。
- 上游漏识别、公式名称误识别及普通正文被归入 table_footnote 属于解析质量边界；不得按文件名、验收标记或具体字符串补 OCR、修公式名称、强行拆表。公式语义标准化留待阶段 27。旧 CHUNKING 结果不因更新 Adapter 自动重写；终态失败任务也不自动恢复，重新提交非活动 FAILED 文档可能创建新批次并消耗额度。
- Block 替换与 Document=CHUNKING、job=done 在同一事务内提交；失败回滚所有块修改，再独立记录安全错误。full.md 仅供供应商平台人工核对，当前未新增 Admin Markdown 预览接口。
- 阶段 10 使用显式 MinerU 上传 / 已有文档提交入口，不改变阶段 9 原生接口的行为；重复活动请求仅恢复同一任务，完成文档拒绝重解析。没有 Router、Parse Cache、自动补投器或事务 Outbox；进程中断 / 队列失败通过显式入口恢复，不声称后台自动恢复。
- 详细接口边界、Mock 与真实 API 人工验收见 [MinerU Cloud 验收说明](docs/MINERU_CLOUD.md)。必须真实检查鉴权、上传、结果结构、四类样本和 Block 入库，不能以 Mock 通过代替真实 API 验证。

### 6.7 数据和证据规则

每个 Chunk 必须至少保留：

```text
kb_id
doc_id
chunk_id
page_start
page_end
section
text
```

回答不得生成无法追溯的来源。

证据不足时应明确返回不足状态，不允许让模型自行补全事实。

### 6.7.1 统一 Parser 契约

- 所有原生 Parser 使用 `ParsedDocument / ParsedBlock`，继承 `BaseParser` 并实现 `_parse`；调用方通过公共 `parse` 入口校验结果，文档 ID、原始文件名和文件类型必须与输入一致。异步云端 Provider 使用 ResultAdapter 输出同一契约，不伪装为同步本地 Parser。
- 数据契约独立于数据库、Celery 和 RAG，不写库、不调度任务、不生成 Chunk、向量或答案；详细字段、序列化和验收步骤见 [Parser 契约说明](docs/PARSER_CONTRACT.md)，无需在 README 重复说明内部模型。
- 块 ID 必须由 Parser 显式提供且在同一文档内唯一，不在契约中随机生成；当前原生 Parser 共用基于文档、版本及块位置的 UUID5 标识。后续 Parser 继续保留稳定标识、原文顺序及持久化事务幂等。
- `page` 与现有数据库非空约束一致，从 1 开始；TXT、Markdown 和未分页 DOCX 使用逻辑页 1，不得当作真实排版页码。PDF 使用物理页码，证据展示必须区分页码语义。
- `bbox` 为原始页面坐标数组 `[x0, y0, x1, y1]` 或空值，PDF 使用点数、图片使用像素；未知位置或置信度不能伪造。空块集合不代表解析、OCR 或知识库入库成功。

---

## 7. 开发顺序

以 `docs/DEVELOPMENT_PLAN_CODEX.md`（V0.3 · MinerU Cloud）为最新路线；`docs/archive/DEVELOPMENT_PLAN_CODEX_legacy.md` 仅保留历史，不作为开发依据。最新计划不覆盖第 5.1 节已验收的 Windows 本地服务约束。阶段 1～9 已完成，不重新实现。严格按以下顺序推进：

```text
01～09. 已有 FastAPI、基础服务、数据模型、管理员 API、上传、异步入口与原生 Parser
10. MinerU Cloud Provider（当前人工验收收尾；图片落库与顺序修复仍待人工确认）
11. Parser Router
12. Parse Cache
13. Chunk
14. BGE-M3 Embedding
15. pgvector 索引入库
16. 预置知识库批量导入
17. Dense Retrieval
18. 单文档 Retrieval
19. BM25
20. Hybrid Retrieval
21. Reranker
22. DeepSeek RAG
23. Citation / Evidence
24. Knowledge Query API
25. Admin 文档管理完善
26. 动态增量入库
27. 公式语义结构化
28. 公式参数识别与绑定
29. 公式安全计算
30. Knowledge Agent
31～36. 自动测试、正式评测与性能记录
37. 独立 Demo
38. Knowledge Service 独立验收
```

不得为了提前展示 Agent 而跳过底层 Knowledge Base 能力。

### 7.1 当前工程基线与阶段边界

以下阶段编号沿用用户任务及开发计划，不替换上面的总体开发优先级。仅记录后续开发需要的当前基线，不是基础服务已安装、外部 API 已验证或人工验收已通过的声明；有变化时更新对应条目，不追加逐次开发日志。

| 阶段 | 当前工程基线 |
|---|---|
| 0～3：基础设施 | Python 3.11 工程、FastAPI 健康接口、基础服务连接配置、Celery 双队列和加法任务已建立；系统软件由用户手动管理。 |
| 4：数据模型 | 已有知识库、文档、解析块和 Chunk 模型及迁移；Embedding 列为 `vector(1024)`。应用启动不自动迁移、安装扩展或创建业务数据。 |
| 5：管理员接口 | 已有知识库创建、列表、详情及文档列表；当前不实现复杂 RBAC，也未提供管理员鉴权。 |
| 6：上传与存储 | 支持七种文件类型，原始文件名入库，磁盘使用文档 UUID 和小写扩展名；按知识库内 SHA256 去重。上传不等于解析完成。 |
| 7：异步入口 | `0002_document_tasks` 增加可空且唯一的任务标识；已建立提交、状态查询、重新投递及重试幂等框架，具体处理步骤由后续 Parser 扩展。 |
| 8：统一解析结构 | 已建立 `ParsedDocument / ParsedBlock`、Parser 公共校验入口及独立契约测试；契约不耦合数据库、Celery、RAG 或模型部署。 |
| 9：原生文本解析 | TXT、Markdown、DOCX、文本型 PDF 输出统一 Block，PDF 保留物理页码，其他格式使用逻辑页 1；`0003_block_order` 增加块顺序及约束。任务事务性落库后停在 `CHUNKING / awaiting_chunk`；不执行 OCR、Chunk、Embedding 或 RAG。 |
| 10：MinerU Cloud | 已有官方 V4 主链路及 `0004_mineru_jobs`；云解析事务写块后停在 CHUNKING。扫描、表格、公式真实结果已存在，图片历史网络失败尚未落库；新增经过校验的页边界顺序恢复，已用现有真实 ZIP 在内存核对，仍需人工完成新 Worker 的上传到落库验收。 |

当前只实施阶段 10；人工验收通过并经用户确认后，下一开发范围为阶段 11 Parser Router。阶段 12 Parse Cache、Chunk、Embedding、RAG 和其他成员模块均不得提前开发。

---

## 8. Knowledge Agent 对外协议

最终向其他 Agent 暴露稳定结构化接口：

```json
{
  "status": "success",
  "answer": "...",
  "entities": {},
  "evidence": [
    {
      "doc_id": "...",
      "file_name": "...",
      "page": 1,
      "chunk_id": "...",
      "text": "...",
      "score": 0.0
    }
  ],
  "formula_results": []
}
```

Knowledge Service 的内部实现变化不应影响上层 Agent。

---

## 9. 任务完成后的回答格式要求

完成代码任务后，回复至少包含：

```text
已完成：
- ...

需要你手动测试：
1. ...
2. ...
3. ...

建议 Commit Message：
feat: ...

下一步建议：
...
```

如果当前任务只是分析或设计、不涉及代码变更，则可以省略 Commit Message，但仍需给出下一步建议。
