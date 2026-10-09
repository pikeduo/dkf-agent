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
→ Parser / OCR
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
→ Parse / OCR
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
火山引擎 OCR（预留百度 OCR Provider）
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
- Redis 运行于 WSL2 Ubuntu，不使用 Windows Redis 5。默认从 Windows 通过 `127.0.0.1:6379`、`REDIS_URL=redis://127.0.0.1:6379/0` 连接，Redis 服务由开发者通过 systemd / service 管理。

Codex 必须遵守：

1. 不自动创建 `docker-compose.yml` 来部署 PostgreSQL 或 Redis。
2. 不自动修改、安装 PostgreSQL / Redis 系统软件，不自动编译安装 pgvector，不修改系统服务。
3. 应用代码只负责通过环境变量连接现有基础服务，不承担服务安装或启动工作。
4. 测试 Redis 前，先确认 `wsl -l -v` 中 Ubuntu 状态为 `Running`。WSL 停止会导致 Redis 停止。
5. Redis 连接失败时，不直接修改 Python 代码；先依次检查 WSL 是否 Running、Redis 服务是否启动、6379 是否监听、Ubuntu 中 `redis-cli ping` 是否返回 `PONG`，再依据检查结果定位应用问题。
6. Docker Compose 保留为未来整体部署方案，当前 Windows 开发环境不使用 Docker 部署 PostgreSQL 或 Redis；除非用户明确要求，不恢复 Docker 版基础服务部署。

本节优先于开发计划或其他文档中遗留的 Docker 基础服务部署说明。开发顺序不变，但当前 PostgreSQL、pgvector 和 Redis 的部署方式以上述约束为准。

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
- `DEVELOPMENT_PLAN_CODEX.md` 保存开发路线、阶段目标和验收标准；专题契约或设计文档保存详细技术说明，测试代码维护在 `tests/` 中。
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
- 目前只检查原件并推进到 `PARSING`，返回 `awaiting_parser`。不得虚构解析结果、Block、Chunk、Embedding 或 `READY`。后续幂等必须落实到实际数据写入的事务和唯一约束，不能仅依靠当前任务入口。
- 当前没有事务 Outbox、自动扫描或后台补投器；数据库提交与消息投递并非原子操作。进程中断可能留下已有原件和记录但未投递的文档，应使用已有重新投递入口处理，不能宣称系统会自动补投。
- 文件系统与数据库也非同一事务。普通写盘或入库失败清理本次文件；提交结果不确定时保留原件，避免误删可能已提交的数据。异常和崩溃恢复不得假设文件与记录天然保持原子一致。
- 上传按实际内容计算 SHA256、检查大小和基本格式标识，不依赖客户端大小或 MIME 声明；这些检查不是完整格式解析或恶意文件扫描，文本编码留待 Parser 判断。
- CPU Worker 在 Windows 使用线程池仅作开发验证，正式 Linux 环境使用 prefork 并发 2～4；GPU Worker 单进程串行，当前不加载模型。不能把线程池等同于 CPU 密集任务的多进程加速。

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

- 所有具体 Parser 使用 `ParsedDocument / ParsedBlock`，继承 `BaseParser` 并实现 `_parse`；调用方通过公共 `parse` 入口校验结果，文档 ID、原始文件名和文件类型必须与输入一致。
- 数据契约独立于数据库、Celery 和 RAG，不写库、不调度任务、不生成 Chunk、向量或答案；详细字段、序列化和验收步骤见 [Parser 契约说明](PARSER_CONTRACT.md)，无需在 README 重复说明内部模型。
- 块 ID 必须由 Parser 显式提供且在同一文档内唯一，不在契约中随机生成；后续 Parser 应使用稳定标识，并在持久化层实现重试幂等。
- `page` 与现有数据库非空约束一致，从 1 开始；TXT、Markdown 和未分页 DOCX 使用逻辑页 1，不得当作真实排版页码。PDF 使用物理页码，证据展示必须区分页码语义。
- `bbox` 为原始页面坐标数组 `[x0, y0, x1, y1]` 或空值，PDF 使用点数、图片使用像素；未知位置或置信度不能伪造。空块集合不代表解析、OCR 或知识库入库成功。

---

## 7. 开发顺序

严格优先按以下顺序推进：

```text
01. Python / Conda 环境
02. PostgreSQL + pgvector
03. Redis + Celery
04. Knowledge Base 数据表
05. 预置知识库文档准备
06. 批量导入
07. PDF / DOCX / TXT / Markdown Parser
08. OCR Provider
09. 扫描 PDF / 图片 OCR
10. Unified Document
11. Chunk
12. BGE-M3 Embedding
13. pgvector Index
14. Dense Retrieval
15. 单文档 Retrieval
16. BM25
17. Hybrid Retrieval
18. bge-reranker-v2-m3
19. DeepSeek RAG
20. Citation / Evidence
21. Admin 文档管理
22. Delete / Reindex
23. 公式识别
24. 参数抽取与绑定
25. SymPy 计算
26. Knowledge Agent
27. RAG / OCR / Formula Eval
28. Knowledge Base Service 独立验收
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
| 7：异步入口 | `0002_document_tasks` 增加可空且唯一的任务标识；已建立提交、状态查询、重新投递及重试幂等框架。文档当前仅推进至 `PARSING / awaiting_parser`。 |
| 8：统一解析结构 | 已建立 `ParsedDocument / ParsedBlock`、Parser 公共校验入口及独立契约测试；尚无具体格式 Parser，不改变上传或任务行为，不新增配置、迁移和模型部署。 |

下一开发范围为具体格式 Parser：先 TXT / Markdown，再 DOCX 与原生 PDF。继续遵守逐阶段推进和人工验收，不提前接入 OCR、Chunk、Embedding、RAG 或其他成员模块。

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
