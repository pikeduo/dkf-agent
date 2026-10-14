# DKF-Agent 非结构化知识问答开发文档（Codex 版 V0.9 · 人工文档验收优先 + 完整性优先 Router + Selective Hybrid Visual + LlamaIndex + LangGraph）

> 项目名称：数知融问智能体（DKF-Agent）  
> 当前开发范围：`knowledge-service` 非结构化知识问答模块  
> 使用对象：Codex / 开发人员  
> 开发原则：**从零开始，严格按依赖顺序开发；一次只完成一个阶段，人工测试通过后再进入下一阶段。**
>
> 当前进度以最新代码与 AGENTS 为准：**阶段21 BGE Reranker代码已实现、待人工验收；真实模型/排序收益及阶段19/20等此前待验收事项保留。** 不自动进入22。\
> 文档解析策略：**Native Parser + MinerU 云端精准解析 API + 完整性优先 Parser Router；必要时采用受控 Selective Hybrid Visual Enhancement。**  
> RAG 编排策略：**DKF-Agent 自主管理 Parse / Chunk / Embedding / PostgreSQL + pgvector，LlamaIndex 仅作为 Retrieval / Fusion / Rerank / Response Synthesis 编排层。**  
> Agent 编排策略：**LangGraph 作为 Knowledge Agent / 后续 Coordinator Agent 的状态机与工作流框架；LangChain 仅按需提供 Prompt / Tool / Runnable / Message / Model Adapter 等标准组件，不再建立第二套 RAG、Vector Store 或 Agent Runtime。**  
> 本轮只接入项目共享Reranker与Node Postprocessor，保留原分数/证据，不重做业务链路；不触碰素材、下载/加载模型、执行迁移/测试/真实查询、启动服务或进入DeepSeek/RAG。\
> MinerU 官方 API 文档：`https://mineru.net/apiManage/docs`。  
> **最高优先级执行禁令：未经用户单独明确授权，Codex 不得安装依赖、替换 PyTorch CUDA、变更 Conda 环境、下载/运行模型、部署 GPU、执行迁移或启动 API/Worker；亦不得运行文档解析测试、生成/渲染样本、执行回归/性能测试或调用真实 API。只交付获授权的代码、文档与手动操作说明，由用户执行验收。**

---

# 0. Codex 安装、部署与测试权限边界（最高优先级，适用于所有阶段）

本节优先于全文各阶段的“安装依赖”“部署模型”“自动测试”“人工测试”“测试矩阵”“回归测试”“真实验证”“完成标准”等措辞。**这些内容规定用户应如何部署和验证，并不授权 Codex 自行安装、部署或运行测试。**

## 0.1 权责分工

| 工作项 | Codex | 用户 |
|---|---|---|
| 阅读代码、定位问题、实现本阶段功能 | 负责 | 审阅结果 |
| 编写单元测试 / Mock 测试代码与预期断言 | 可以；只编写不执行 | 决定是否运行 |
| 编写真实文档验收清单、命令、预期结果 | 负责；只输出说明 | 实际操作与记录 |
| 依赖安装、PyTorch CUDA 替换、Conda 变更、模型下载、GPU 部署及模型运行验证 | **默认禁止**；检查实现与依赖、提供手动命令及风险说明 | **亲自执行** |
| 生成或修改 PDF/DOCX/XLSX/PPTX/图片等测试文档 | **默认禁止** | 自行准备；明确委托才允许 Codex 生成 |
| 运行 Parser / OCR / MinerU / Visual Bundle 文档测试 | **默认禁止（包含本地与 Mock 文档样本）** | **亲自运行** |
| 发起真实 MinerU / OCR / DeepSeek / Embedding API 请求 | **禁止自动调用** | 确认额度后亲自执行 |
| `pytest`、全量回归、长时 smoke、RAG 评测、性能测试 | **默认不运行** | 选择性运行 |
| 静态检查（针对本轮改动的文件，有限范围） | 允许 | 可复查 |
| 解析质量判断及阶段验收 | 提供标准，标记“待人工验收” | 提交日志/CSV 后共同分析，用户批准进入下一阶段 |

**普通“实现阶段 X”“解决依赖”“完成部署说明”或“按开发文档完成阶段 X”的指令，不构成安装、部署或测试授权。** 用户上传文档、样本路径、数据库连接、已有 Token，同样不等于执行授权。

任何依赖安装、PyTorch CUDA 版本替换、Conda 环境变更、模型下载、GPU 部署和模型运行验证，默认只能由用户手动执行。对这些任务，Codex 只允许：

1. 检查已有代码与依赖声明；
2. 判断需要安装哪些依赖，不强行升级可用旧版本或安装实现无关的框架；
3. 提供可复制的 PowerShell 安装命令；
4. 说明兼容性及时间、磁盘、显存风险；
5. 提供手动验证命令与预期结果，随后停止，等待用户反馈。

## 0.2 Codex 默认允许的验证，仅限低成本静态检查

默认可执行且最多各一次：

```text
git diff --check
针对本轮实际修改的 Python 文件进行语法编译检查（如 py_compile；不导入业务模块）
检查本轮改动文件的静态内容/接口契约
```

应控制在短时间内；若环境缺失、检查超时、需要安装依赖或进一步修复，应停止并报告。**不得为“确保通过”循环尝试测试或运行耗时命令。** 不得启动服务、Celery、数据库、模型或任意会触发真实文档处理的流程。

Codex 可以新增 / 修改 `tests/` 下的测试用例，但**默认只交付测试代码与运行说明，不执行这些用例**。包括 Mock Provider 场景，只要涉及模拟文档处理、生成样本、DB/Broker 启动或耗时任务，均由用户启动执行。

## 0.3 无单独授权时禁止的操作

```text
pip / python -m pip、conda、hf download 等安装/环境/模型工具命令
模型下载、加载、推理及真实 GPU 计算验证
数据库迁移、API/Worker 或模型服务启动
pytest / pytest -q / pytest tests/ 等任意测试执行
python tests/*_smoke.py、tests/*acceptance*、批量测试脚本
调用真实 MinerU / OCR / DeepSeek / Embedding 或外部文档转换 API
上传/下载/解析/渲染真实 PDF、DOCX、XLSX、PPTX、图片等验收样本
生成大型测试文件、临时视觉 Bundle、OCR 样本、测试文档集合
启动 FastAPI/Celery/Redis/PostgreSQL 后自行发请求做端到端测试
跑性能基准、RAG 评估、反复轮询云任务、长时后台任务
为了通过测试自行重试、改进测试数据或连续修复-重测
```

以上限制适用于所有阶段，包括阶段 11A、11B.1、11B.2、12～14、15A、30、31、32、38，不得通过其他脚本间接执行，或用“解决依赖”“GPU 部署”“当前阶段自动测试要求”“完整回归”“API 必须验证”为理由绕过。

## 0.4 如果用户专门要求 Codex 代执行某一项安装、部署或测试

必须**另外取得明确的执行授权**，确认具体命令、目标环境与文件范围、联网/付费权限，以及时间、费用和磁盘/显存等资源预算。未得到这些信息时只给手动步骤，不运行；授权仅覆盖所指一次操作，不视为以后默认授权。

## 0.5 每轮开发结束时的交付与停止条件

Codex 每轮只需交付：

1. 本轮修改文件及实现内容；
2. 仅实际执行过的静态检查及结果；
3. **“未运行文档解析/真实 API/自动化验收测试，等待用户手动测试”** 的明确说明；
4. 用户可直接执行的最小测试步骤、输入文件、预期结果与失败信息收集方式（不替用户执行）；
5. 已知限制、中文 Commit Message、下一步建议。

**“代码实现完成”不等于“测试通过 / 阶段验收通过”。** 没有用户提供的人工验收结果与明确放行，Codex 不得自行进入下一阶段。若用户反馈失败，只针对反馈做一轮修复并再次交付测试方法，不得自行反复试跑。阶段进度描述仍以用户当前确认的仓库实际进度为准；本次文档版本更新不自动改变任何阶段状态。

---

# 1. 当前模块最终目标

当前只负责赛题中的**非结构化知识问答**部分，不开发 NL2SQL、Schema Linking、Coordinator Agent、Fusion Agent 等其他成员模块。

最终需要独立跑通：

```text
知识库文档
→ 上传 / 批量导入
→ Parser Router
→ Native Parser / MinerU Cloud
→ 统一 DocumentBlock
→ Chunk
→ BGE-M3 Embedding
→ PostgreSQL + pgvector
→ LlamaIndex Adapter（DocumentChunk ↔ TextNode / NodeWithScore）
→ Dense Retriever + BM25 Retriever
→ Query Fusion / Hybrid Retrieval
→ bge-reranker-v2-m3
→ DeepSeek Response Synthesis
→ Answer + Evidence + Citation
```

并完成中级任务 6：

```text
文档公式
→ 公式识别
→ 参数识别
→ 参数绑定
→ 安全计算
→ 结果 + 公式来源 + 参数来源
```

系统支持两种知识库模式：

```text
模式 A：预置知识库
系统启动前已导入 10+ 文档
→ 用户直接提问
→ 系统检索并回答

模式 B：管理员动态增量入库
上传新文档
→ Celery 异步处理
→ READY
→ 自动参与后续问答
```

---

# 2. 当前模块需要满足的赛题目标

## 2.1 基础 RAG

必须支持：

- 单文档简单检索问答。
- 从给定文档中检索相关内容并生成答案。
- 回答展示引用文档片段。
- 文档来源可追溯。
- 至少准备 10 个正式单文档 RAG 测试用例。

## 2.2 知识库文档

最终知识库样本：

- 不少于 10 份。
- 覆盖不同格式。
- 至少包含普通文本型文档和需要 OCR 的文档。

建议覆盖：

```text
TXT
Markdown
CSV
DOC / DOCX
XLS / XLSX
PPT / PPTX
PDF（文本型 / 扫描型 / 混合型）
JPG / JPEG / PNG
```

解析测试不能只按扩展名判断“能否上传”，还必须覆盖文档内部内容形态：

```text
Plain Text
Table
Formula
Chart
Reading Order
Native Text + Image / Scan 混合内容
```

OHR-Bench 可作为文档解析质量维度参考，其公开任务重点覆盖 Plain Text、Table、Formula、Chart、Reading Order：
`https://github.com/opendatalab/OHR-Bench`。

## 2.3 RAG 技术实现

需要实现并能够说明：

```text
文档解析
文档切片
LlamaIndex RAG Adapter
向量检索
关键词检索
Query Fusion
重排
Response Synthesis / 生成融合
来源标注
测试与性能评估
```

## 2.4 中级任务 6

### 6a：公式输入参数识别

需要从非结构化文档中识别：

```text
公式
变量
变量含义
变量所需数值
变量来源
```

### 6b：公式解析与计算

需要：

- 正确理解公式结构。
- 将公式转换为可安全计算的表达式。
- 使用 SymPy 计算。
- 禁止执行模型生成代码。
- 禁止使用 `eval()`。
- 参数不足时返回缺失参数，不得猜测。

---

# 3. 固定技术栈

| 模块 | 技术 |
|---|---|
| Python | Python 3.11 |
| Backend | FastAPI |
| ORM | SQLAlchemy |
| Migration | Alembic |
| Database | PostgreSQL |
| Vector DB | pgvector |
| Async Task | Celery |
| Broker / Backend | Redis |
| LLM | DeepSeek API |
| Embedding | BAAI/bge-m3 |
| Reranker | BAAI/bge-reranker-v2-m3 |
| PDF Parser | PyMuPDF |
| DOCX Parser | python-docx |
| CSV Parser | Python `csv` |
| XLSX Parser | openpyxl |
| Legacy Office 格式校验 | olefile（仅 CFB/OLE 容器与主流识别，不解析正文） |
| Complex / Legacy Office Parser | MinerU Cloud 精准解析 API（V4） |
| MinerU Model | `vlm`（默认） |
| Sparse Retrieval | BM25 |
| RAG Orchestration | LlamaIndex（Retriever / Query Fusion / Node Postprocessor / Response Synthesis） |
| Agent Workflow | LangGraph |
| LLM / Tool Integration | LangChain（按需：Prompt / Tool / Runnable / Message / Model Adapter） |
| Formula | SymPy |
| Test | pytest |
| Local Infrastructure | PostgreSQL 17 + pgvector（Windows 原生）/ Redis（Windows 本地服务） |

当前阶段不主动引入：

```text
Elasticsearch
Milvus
Qdrant
知识图谱
Kubernetes
模型微调
大型本地 VLM / MinerU 本地模型部署
复杂权限系统
```

---

# 4. 目标项目结构

开发过程中逐步形成，不要求第一步一次性全部创建：

```text
dkf-agent/
├── README.md
├── AGENTS.md
├── environment.yml
├── .env.example
├── .gitignore
├── docs/
│   ├── DEVELOPMENT_PLAN_CODEX.md
│   ├── PARSER_CONTRACT.md
│   ├── MINERU_CLOUD.md
│   └── archive/
│
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── health.py
│   │   ├── admin/
│   │   └── knowledge/
│   ├── core/
│   ├── models/
│   ├── schemas/
│   ├── parsers/
│   │   ├── text.py
│   │   ├── docx.py
│   │   ├── pdf.py
│   │   ├── csv.py        # 阶段 11A 新增
│   │   └── xlsx.py       # 阶段 11A 新增
│   ├── services/
│   │   ├── mineru/
│   │   ├── chunk/
│   │   ├── embedding/
│   │   ├── retrieval/
│   │   ├── reranker/
│   │   ├── rag/
│   │   │   └── llama_index/      # 阶段 15A 起逐步建立
│   │   │       ├── node_adapter.py
│   │   │       ├── dense_retriever.py
│   │   │       ├── bm25_retriever.py
│   │   │       ├── fusion.py
│   │   │       ├── reranker.py
│   │   │       └── query_engine.py
│   │   └── formula/
│   ├── tasks/
│   └── agents/
│       └── knowledge/             # 阶段 30 正式建立
│           ├── state.py           # LangGraph AgentState
│           ├── graph.py           # StateGraph / conditional edges
│           ├── nodes.py           # 节点实现，调用稳定 Service / Tool
│           ├── tools.py           # LangChain StructuredTool 等按需适配
│           └── prompts.py         # 受控 Prompt 模板
│
├── alembic/
├── scripts/
├── tests/
├── data/
│   ├── knowledge_base/preset/
│   └── uploads/
└── logs/
```

---

# 5. 统一文档处理状态

```text
UPLOADED
PARSING
CHUNKING
EMBEDDING
INDEXING
READY
FAILED
```

正常流程：

```text
UPLOADED
→ PARSING
→ CHUNKING
→ EMBEDDING
→ INDEXING
→ READY
```

任意阶段失败：

```text
→ FAILED
→ 保存 error_message
```

---

# 6. 阶段 0：项目初始化

## 目标
建立可长期维护的最小工程基础。

## 开发内容
- 初始化项目目录。
- 初始化 Git。
- 创建 Conda 环境。
- 放置 README、AGENTS、DEVELOPMENT_PLAN_CODEX。
- 创建 `.gitignore`、`.env.example`。
- 确定 Python 3.11。

## `.env.example`
至少预留：

```text
APP_ENV=
APP_HOST=
APP_PORT=
DATABASE_URL=
REDIS_URL=
DEEPSEEK_API_KEY=
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_API_TOKEN=
MINERU_MODEL_VERSION=vlm
```

真实密钥不得进入仓库。

## 人工测试说明（仅用户执行，Codex 不得代跑）

```text
conda activate dkf-agent
python --version
git status
```

## 完成标准
- Python 3.11 可用。
- Git 正常。
- `.env` 不被跟踪。
- 根目录文件齐全。

---

# 7. 阶段 1：FastAPI 最小项目 ✅ 已完成

## 目标
保证 Backend 能独立启动。

## 开发内容
建立：

```text
app/main.py
app/api/health.py
app/core/config.py
```

实现：

```http
GET /health
```

示例：

```json
{
  "status": "ok",
  "service": "knowledge-service"
}
```

## 暂时不要做
- 业务数据库表。
- MinerU Cloud 复杂文档解析。
- RAG。
- Agent。

## 人工测试说明（仅用户执行，Codex 不得代跑）

```bash
uvicorn app.main:app --reload
```

检查：

```text
GET /health
GET /docs
```

## 完成标准
FastAPI 正常启动，Swagger 可访问。

---

# 8. 阶段 2：PostgreSQL + pgvector + Redis ✅ 已完成

## 目标
建立后续知识库所需基础服务。

## 当前环境
本地开发不使用 Docker / Docker Compose 部署 PostgreSQL、pgvector 和 Redis。

```text
PostgreSQL 17 → Windows 原生安装 → 127.0.0.1:5432
pgvector      → 编译安装到 PostgreSQL 17，并在 dkf_agent 中启用 vector extension
Redis         → Windows 本地服务运行，应用通过 127.0.0.1:6379 访问
```

## 完成标准
- PostgreSQL 可连接并执行 `SELECT 1`。
- `vector` extension 可用。
- Redis `PING = PONG`。
- FastAPI 可通过环境变量连接 PostgreSQL 和 Redis。

---

# 9. 阶段 3：Celery 基础设施 ✅ 已完成

## 目标
建立耗时文档任务的异步执行能力。

## 开发内容
建立：

```text
app/core/celery_app.py
app/tasks/
```

Redis 用作：

```text
broker
result backend
```

队列：

```text
default_queue
gpu_queue
```

先实现测试任务，例如：

```text
add(x, y)
```

## Worker 规划

```text
CPU Worker:
default_queue
concurrency = 2~4

GPU Worker:
gpu_queue
concurrency = 1
```

## 完成标准
任务可以提交、执行、查询结果。

---

# 10. 阶段 4：Knowledge Base 数据模型 ✅ 已完成

## 目标
建立知识库核心数据库结构。

## knowledge_bases

```text
kb_id
name
description
status
created_at
updated_at
```

## documents

```text
doc_id
kb_id
file_name
file_type
file_hash
file_path
source_type
status
error_message
created_at
updated_at
```

`source_type`：

```text
preset
uploaded
```

## document_blocks

```text
block_id
doc_id
page
section
block_type
text
bbox
source
confidence
```

## document_chunks

```text
chunk_id
doc_id
kb_id
text
page_start
page_end
section
metadata
embedding
created_at
```

## 开发内容
- SQLAlchemy Model。
- Alembic Migration。
- pgvector 字段。
- 外键、索引、唯一约束。

## 完成标准
迁移可执行，并可创建默认知识库。

---

# 11. 阶段 5：Admin Knowledge Base API ✅ 已完成

## 目标
建立管理员知识库管理入口。

## 接口

```http
POST /api/admin/knowledge-bases
GET  /api/admin/knowledge-bases
GET  /api/admin/knowledge-bases/{kb_id}
GET  /api/admin/knowledge-bases/{kb_id}/documents
```

当前不做复杂 RBAC。

## 完成标准
管理员可以创建、查询知识库和文档列表。

---

# 12. 阶段 6：文档上传与文件存储 ✅ 已完成

## 目标
管理员可以上传文档，暂时不解析。

## 阶段 6 初始支持

```text
PDF
DOCX
TXT
MD
JPG
JPEG
PNG
```

该阶段已经完成。**不要回退或重写阶段 6**；阶段 11A 只对上传白名单、内容签名校验和统一 FileType 做兼容性扩展，以支持 CSV、DOC、PPT/PPTX、XLS/XLSX 等新增格式。

## 流程

```text
上传
→ 检查类型
→ 检查大小
→ SHA256
→ 去重
→ 保存文件
→ 创建 Document
→ status = UPLOADED
```

建议磁盘文件名：

```text
doc_id + extension
```

数据库保留原始文件名。

## 完成标准
文件与 Document 一一对应，重复文件可识别。

---

# 13. 阶段 7：文档异步处理入口 ✅ 已完成

## 目标
上传后自动进入 Celery。

## 核心任务

```text
process_document(doc_id)
```

当前先搭状态机和任务框架。

要求：
- 可重试。
- 幂等。
- 记录失败原因。
- 避免重复生成同一数据。

## 完成标准
上传后产生任务，可查询 Document 状态。

---

# 14. 阶段 8：统一 ParsedDocument 结构 ✅ 已完成

## 目标
让不同 Parser 最终输出统一结构。

建议：

```json
{
  "doc_id": "...",
  "file_name": "...",
  "file_type": "pdf",
  "title": "",
  "blocks": [
    {
      "block_id": "...",
      "page": 1,
      "section": "",
      "block_type": "text",
      "text": "...",
      "bbox": null,
      "source": "native_parser",
      "confidence": 1.0
    }
  ]
}
```

`block_type` 预留：

```text
title
text
table
formula
image_text
```

## 完成标准
所有 Parser 输出同一结构，不直接耦合 RAG。

---

# 15. 阶段 9：原生文本 Parser ✅ 已完成

## 目标
先完成无需 OCR 的文档解析。

## 顺序

```text
TXT
Markdown
DOCX
文本型 PDF
```

技术：

```text
TXT / MD → Python
DOCX     → python-docx
PDF      → PyMuPDF
```

要求：
- PDF 保留页码。
- DOCX 尽量保留标题与段落。
- 结果写入 `document_blocks`。

## 完成标准
四类原生文档都能生成统一 Block。

---

# 16. 阶段 10：MinerU Cloud Provider ✅ 已完成并完成人工验收

## 目标
接入 MinerU 官方**精准解析 API V4**，建立复杂文档云端解析能力。当前仅使用云端 API，不部署 MinerU 本地模型。

MinerU 主要负责：

```text
图片
扫描 PDF
混合 / 复杂 PDF
复杂版式
表格
数学公式
```

业务代码统一通过 `MinerUCloudProvider` 调用，禁止在 Controller、Celery Task 等位置散落 MinerU HTTP 请求。

## 官方接口
接口实现必须以官网文档为准：

```text
https://mineru.net/apiManage/docs
```

本项目文档已先保存到本地，因此主要流程使用：

```http
POST /api/v4/file-urls/batch
PUT  MinerU 返回的签名上传 URL
GET  /api/v4/extract-results/batch/{batch_id}
```

公网 URL 单文件场景可使用：

```http
POST /api/v4/extract/task
GET  /api/v4/extract/task/{task_id}
```

## 默认参数

```text
model_version = vlm
language = ch
enable_table = true
enable_formula = true
```

扫描 PDF / 图片需要 OCR 能力时：

```text
is_ocr = true
```

简单文本型 PDF 继续使用阶段 9 已完成的 PyMuPDF Native Parser。

## 文件限制
精准解析 API 当前核心限制：

```text
单文件最大：200 MB
单文件最多：200 页
```

本项目继续保留已验收的本地保护配置：

```text
MINERU_UPLOAD_BATCH_MAX_FILES=50
```

这里的 50 是当前应用保护值，并与账号“50 个文件 / 分钟”的提交频控保持安全一致，不把它写成永久的平台批量上限。平台支持数量发生变化时，只调整配置 / Provider 适配层。

提交前必须先做文件类型、大小和可读性校验；PDF 能提前读取页数时应先检查页数。

## 限流与限额
根据当前 MinerU API 管理后台：

```text
提交任务接口：
- 单文件解析、批量解析、本地文件批量上传、URL 批量上传共用频控
- 50 个文件 / 分钟
- 单用户最多 5000 个文件 / 天
- HTML 文件最多 100 个 / 天

获取任务结果接口：
- 单任务结果、批量任务结果共用频控
- 1000 次 / 分钟
```

平台保留根据负载动态调整限流策略的权利，因此限流参数必须配置化。

官方精准解析 API 还说明：

```text
每天前 1000 页享有最高优先级解析额度；
超过 1000 页后优先级降低，并非硬性拒绝。
```

使用 Redis 实现多 Worker 共享的计数与节流。注意：**50/分钟按文件数量计数，而不是只按 HTTP 请求次数计数。**

建议配置：

```text
MINERU_SUBMIT_FILE_LIMIT_PER_MINUTE=50
MINERU_RESULT_QUERY_LIMIT_PER_MINUTE=1000
MINERU_DAILY_FILE_LIMIT=5000
MINERU_HIGH_PRIORITY_PAGE_LIMIT=1000
MINERU_UPLOAD_BATCH_MAX_FILES=50
```

实际调度应预留安全余量，不持续顶到平台边界。

## Token 规则
只需要：

```text
MINERU_API_TOKEN
```

保存在本机 `.env`。

要求：

- 不进入 Git。
- 不写日志。
- 不返回前端。
- 不进入异常详情。
- **不实现 Token 过期时间跟踪、自动续期或自动刷新逻辑。**

## 异步任务
MinerU 精准解析 API 是异步接口。

禁止：

```python
while True:
    query_result()
    time.sleep(...)
```

正确流程：

```text
submit_mineru_parse
→ 保存 batch_id / data_id / trace_id
→ Celery 当前任务结束

延时调度 check_mineru_result
→ waiting-file / pending / running / converting
→ 未完成则 countdown 后再次检查

done
→ 获取 full_zip_url
→ 下载结果 ZIP
→ normalize
→ 保存 DocumentBlock
→ CHUNKING

failed
→ 保存错误
→ Document = FAILED
```

## Parse Job 持久化
增加 `document_parse_jobs`，至少保存：

```text
job_id
doc_id
provider
model_version
data_id
batch_id
task_id
trace_id
state
retry_count
error_code
error_message
submitted_at
finished_at
created_at
updated_at
```

`documents.status` 保持供应商无关：

```text
UPLOADED
PARSING
CHUNKING
EMBEDDING
INDEXING
READY
FAILED
```

MinerU 外部状态保存在 parse job：

```text
waiting-file
pending
running
converting
done
failed
```

## 结果标准化
解析完成后下载结果 ZIP，程序优先读取结构化结果：

```text
content_list.json
```

再通过 `MinerUResultAdapter` 转换为当前统一 `DocumentBlock`。

`full.md` 仅作为 Admin 预览、人工核对和调试输出，不应成为唯一程序输入。

映射示例：

```text
text       → block_type = text
title      → block_type = title
table      → block_type = table
formula    → block_type = formula
image text → block_type = image_text
```

统一：

```text
source = mineru_cloud
```

尽量保留物理页码、阅读顺序、bbox、表格、公式及上下文。

## 重试策略
可重试：

```text
HTTP 429
服务异常
上传 URL 临时生成失败
模型服务暂时不可用
任务队列已满
临时网络 / 解析异常
```

使用：

```text
指数退避 + 随机抖动
```

不可盲目重试：

```text
Token 错误
文件损坏
空文件
文件超过 200 MB
页数超过 200
无权限访问任务
每日任务数量达到上限
```

## 幂等
同一：

```text
doc_id
+ file_hash
+ provider
+ model_version
+ parse_options
```

不得同时存在多个有效解析任务。

Celery 重试、重复消息和 Worker 重启不得重复写入 Block。

## 人工测试说明（仅用户执行，Codex 不得代跑）
至少测试：

```text
1 张 JPG / PNG
1 份纯扫描 PDF
1 份含表格 PDF
1 份含公式 PDF
```

验证：

```text
鉴权
申请上传 URL
PUT 上传
batch_id 持久化
异步轮询
结果 ZIP 下载
content_list.json 读取
Block 入库
表格 / 公式类型
页码 / 顺序
```

还需要用 Mock 覆盖 429、临时失败和不可重试错误。

## 完成标准

```text
图片 / 扫描 PDF
→ MinerU Cloud
→ DocumentBlock
→ document_blocks
→ status = CHUNKING
```

并满足限流、幂等、错误分类和 Token 安全要求。

## 已验收基线

阶段 10 已使用真实 MinerU Cloud API 完成人工测试，至少覆盖：

```text
图片 OCR
纯扫描 PDF
表格 PDF
公式 PDF
```

真实测试确认 API 鉴权、签名上传、异步查询、结果 ZIP、`content_list.json` 标准化和 Block 入库链路可运行。

同时保留以下解析质量边界，不在阶段 10 通过硬编码特例修复：

- OCR / 公式字符可能存在供应商识别误差。
- 表格边界可能包含邻近说明文本。
- 复杂版式的 Reading Order 需要继续通过真实样本评估。
- 不为某个测试 Marker 或公式名称写特例修复。

---

# 17A. 阶段 11A：多格式文档支持扩展（当前阶段）

## 目标

在进入自动 Parser Router 前，先把常见办公文档格式的上传、统一契约、Native Parser 和显式 MinerU 能力补齐，形成清晰的格式能力矩阵。

本阶段**只扩展格式能力，不实现自动 Router**。

## 格式能力矩阵

| 格式 | 第一版处理方式 | 说明 |
|---|---|---|
| TXT | Native | 已完成 |
| Markdown / MD | Native | 已完成 |
| CSV | Native | 本阶段新增 |
| DOCX | Native；允许显式 MinerU | Native 已完成；复杂图像内容后续 Router 决定 |
| DOC | MinerU Cloud | 不开发旧 Word 二进制 Native Parser |
| XLSX | Native；允许显式 MinerU | 本阶段新增 Native；图表 / 图片后续 Router 决定 |
| XLS | MinerU Cloud | 不开发旧 Excel 二进制 Native Parser |
| PPTX | MinerU Cloud | 第一版不开发 Native PPTX Parser |
| PPT | MinerU Cloud | 不开发旧 PowerPoint 二进制 Native Parser |
| 文本型 PDF | Native | 已完成 |
| 扫描 / 复杂 PDF | MinerU Cloud | 已完成显式入口 |
| JPG / JPEG / PNG | MinerU Cloud | 已完成显式入口 |

MinerU 精准解析 API 的格式能力以官方文档为准：
`https://mineru.net/apiManage/docs`。

当前官方精准解析 API 支持 PDF、图片、Doc/Docx、Ppt/PPTx、Xls/Xlsx；CSV 不作为本项目 MinerU 主路径，使用 Native Parser。

## 统一 FileType 扩展

当前统一契约需要扩展为至少：

```text
pdf
doc
docx
ppt
pptx
xls
xlsx
csv
txt
md
jpg
jpeg
png
```

要求：

- Pydantic Schema、上传接口、Document `file_type`、Parser Registry、MinerU 校验、API 响应与测试保持一致。
- 不允许只修改扩展名白名单而遗漏统一契约。
- 新增格式不得影响阶段 1～10 已验收格式。

## 上传与文件内容校验

现代 Office OOXML 不能只看后缀：

```text
DOCX → ZIP + word/document.xml
PPTX → ZIP + ppt/presentation.xml
XLSX → ZIP + xl/workbook.xml
```

旧 Office：

```text
DOC
XLS
PPT
```

属于 OLE / Compound Binary 系列。必须至少验证实际容器类型；若引入轻量依赖判断内部流，需要同步 `environment.yml` 和 README。

不得仅通过 `filename.endswith()` 判断格式有效。

## CSV Native Parser

新增：

```text
app/parsers/csv.py
```

要求：

- 使用 Python 标准 `csv` 模块，不使用简单 `split(',')`。
- 复用现有严格文本解码策略，至少覆盖 UTF-8、UTF-8 BOM、GB18030。
- 支持 quoted field、字段内逗号、空值、中文、数字、百分比。
- 第一版可将整个 CSV 或合理分组输出为 `table` Block。
- `page = 1`，`bbox = null`，`source = native_parser`。
- 空 CSV、无法识别编码、结构异常必须安全失败。
- Block ID 稳定、重试幂等。

## XLSX Native Parser

新增：

```text
app/parsers/xlsx.py
```

使用：

```text
openpyxl
```

要求：

- 保留 Workbook 中 Sheet 顺序。
- Sheet 名保存到 `section`。
- 单元格按行列顺序读取。
- 每个有效 Sheet 至少形成稳定 `table` Block。
- 空 Sheet 可跳过；全空 Workbook 失败。
- 公式保留公式文本，不执行公式，不使用 `eval()`。
- `page = 1` 仅表示逻辑页，不声称 Excel 物理打印页。
- `bbox = null`，`source = native_parser`。
- 第一版不解析图片、图表、SmartArt、复杂绘图。
- 如果存在视觉对象，不得声称 Native Parser 已完整解析这些视觉信息。

## 扩展 MinerU Office 支持

阶段 10 的图片、扫描、表格、公式及阅读顺序已通过真实 API 人工验收；此处的 Office 扩展仍须单独人工验收，不撤销已完成基线。

显式 MinerU Cloud 路径增加：

```text
DOC
DOCX
PPT
PPTX
XLS
XLSX
```

不要将 CSV 发送到 MinerU 作为默认路径。

特别注意现有 `page_geometry` / bbox 逻辑主要针对 PDF 与图片：

```text
PDF   → 真实物理页码 + 可映射 bbox
图片  → 第 1 页 + 像素 bbox
Office → MinerU 转换后的逻辑 page；无法可靠映射原始 Office 页面坐标时 bbox = null
```

禁止为了满足契约伪造 Office 原始物理页码或坐标。

对于本地无法可靠提前计算页数的 Office 文档，不伪造页数；文件大小仍应本地校验，平台页数限制由 MinerU 正常返回安全错误。

## Parser Registry

Native Registry 增加：

```text
csv  → CsvParser
xlsx → XlsxParser
```

不要把以下格式注册为 Native Parser：

```text
doc
xls
ppt
pptx
```

它们走显式 MinerU Cloud。

## 自动化测试用例设计（Codex 只编写，用户负责执行）

**执行约束：** Codex 只负责实现测试文件、Mock/Stub 及断言；不执行 `pytest`、任何 `smoke.py`、文档解析或云服务请求。用户测试并反馈结果后再修复。


至少覆盖：

```text
CSV
- UTF-8
- UTF-8 BOM
- GB18030
- quoted comma
- empty cell
- invalid encoding

XLSX
- single sheet
- multi sheet
- empty sheet
- 中文
- 数字 / 百分比
- formula
- empty workbook

OOXML validation
- valid DOCX
- valid PPTX
- valid XLSX
- suffix / container mismatch

Legacy Office validation
- DOC
- XLS
- PPT
- invalid OLE container

MinerU Provider Mock
- DOC / DOCX
- PPT / PPTX
- XLS / XLSX
- Office result page / bbox semantics
```

阶段 1～10 的回归覆盖必须保留；是否通过由用户执行后判定，Codex 不负责自行运行全量回归。

## 人工测试说明（仅用户执行，Codex 不得代跑）

至少准备并实际上传：

```text
1 个 CSV
1 个普通 XLSX
1 个 DOC
1 个 DOCX
1 个 XLS
1 个 XLSX
1 个 PPT
1 个 PPTX
```

分别验证 Native / 显式 MinerU 路径、Block 类型、section、逻辑页语义、错误信息和幂等。

## 本阶段禁止事项

不要实现：

```text
自动 Parser Router
Parse Cache
Chunk
Embedding
Retrieval
RAG
```

不要为 DOC/XLS/PPT 引入 LibreOffice 自动转换链路；旧格式第一版统一交 MinerU。

## 完成标准

- CSV 与 XLSX Native Parser 可独立运行并输出统一 Block。
- DOC/DOCX/PPT/PPTX/XLS/XLSX 可通过显式 MinerU 路径处理。
- 上传与 FileType 契约已扩展并有格式真实性校验。
- Office 的 page / bbox 语义不被伪造。
- 阶段 1～10 回归全部通过。

---

# 17B. 阶段 11B：完整性优先 Parser Router + Selective Hybrid Visual Enhancement

## 目标

阶段 11B 继续坚持“**完整性优先**”，但在整份 Native / 整份 MinerU 两种主路径之外，预留一个受控的第三种策略：

```text
native
mineru_whole
hybrid_bundle
```

其中：

- `native`：只有能合理证明当前 Native Parser 可以完整覆盖主要信息时才使用。
- `mineru_whole`：扫描、复杂版式、强视觉文档或无法可靠局部对齐时，整份文件一次性交给 MinerU。
- `hybrid_bundle`：只在真实测试证明 Native 与 MinerU 明显互补、且可以稳定定位视觉元素时使用；Native 保留确定性结构，视觉候选集中打包后只调用 **一次 MinerU**，再按 Anchor 合并回原解析结果。

阶段 11B 不采用：

```text
Native 能抽到文字就算成功
所有文件无脑 MinerU
每张图片 / 每个图表 / 每个公式各调用一次 MinerU
```

Router 的核心问题仍然是：

```text
当前策略能否完整覆盖该文档对 RAG 有价值的主要信息？
```

而不是：

```text
能不能抽出一些文字？
```

## 为什么不默认“全部 MinerU”

真实样本已经说明，MinerU 很适合 OCR、复杂版式、图片、表格、公式与 Office 视觉内容，但也可能出现：

```text
OCR / 公式字符误差
Block 边界漂移
Reading Order 不稳定
Excel 公式原式退化为计算结果
某些内嵌图片没有被 OCR
```

同时 Native 对结构明确的内容具有确定性优势：

```text
TXT / CSV
→ 原文确定性高

XLSX Native
→ Sheet / Cell / 公式原式确定性高

简单 DOCX
→ 标题 / 正文 / 原生表格顺序可控

简单文本 PDF
→ 原生文字与物理页码稳定
```

因此：

```text
确定性结构 → Native
复杂视觉 / 复杂布局 → MinerU Whole
Native 与 MinerU 明显互补且可稳定锚定 → Selective Hybrid Visual
```

## 为什么不逐元素调用 MinerU

MinerU 提交额度按“文件数”计数。若一个 DOCX / XLSX 中包含：

```text
3 张截图
2 个图表
4 个公式图片
```

逐元素上传会把一份源文档放大为 9 个 MinerU 文件任务，显著增加：

```text
分钟提交额度消耗
每日文件额度消耗
任务管理复杂度
网络与失败重试次数
```

因此阶段 11B.2 明确禁止：

```text
visual_001.png → MinerU task 1
visual_002.png → MinerU task 2
visual_003.png → MinerU task 3
...
```

而采用：

```text
同一 source document
→ 提取真正需要视觉理解的候选
→ 去除明显装饰 / 重复候选
→ 生成一个 visual_bundle.pdf
→ 一次 MinerU 提交
→ 按 bundle manifest 拆回并合并
```

注意：Visual Bundle 只降低“文件任务数”，并不会消除页面消耗。Bundle 页数仍影响解析耗时、200 页限制和平台高优先级页额度，因此必须继续控制候选数量。

## 总体流程

```text
Upload / Existing Document
        ↓
Format Validation
        ↓
Preflight Inspector
        ↓
Route Decision
        ├─ native
        ├─ mineru_whole
        ├─ hybrid_bundle   # 仅 11B.2 启用
        └─ explicit_review
        ↓
Parser / Provider
        ↓
Completeness Guard
        ↓
DocumentBlock
        ↓
CHUNKING
```

阶段 11B.1 只实现：

```text
native
mineru_whole
explicit_review
```

阶段 11B.2 的 `hybrid_bundle` 必须在真实对照测试证明必要后再启用，不得和 11B.1 一次性开发。

## 外部 `parse_mode` 与内部 `route_strategy`

外部 API 继续保留：

```text
parse_mode = auto | native | mineru
```

不在 11B.1 直接增加 `hybrid` 用户参数，避免公开尚未稳定的策略。

内部 Router 可以产生：

```text
route_strategy = native | mineru_whole | hybrid_bundle | review
```

语义：

```text
auto
→ Preflight + Router 自动选择

native
→ 管理员显式要求 Native，用于基线对比 / 排障
→ 必须暴露 Native 已知覆盖边界

mineru
→ 管理员显式要求整份 MinerU
→ 继续遵守配额、幂等、安全和文件限制
```

如果未来 `hybrid_bundle` 稳定并需要管理员显式控制，再独立评估是否扩展 API 契约；不要提前污染公开接口。

## Route Decision 必须可解释

至少形成结构化信息：

```json
{
  "decision_version": "router-v2",
  "parse_mode": "auto",
  "file_type": "xlsx",
  "route_strategy": "review",
  "reason_codes": [
    "XLSX_HAS_EMBEDDED_IMAGE",
    "XLSX_HAS_CHART"
  ],
  "signals": {
    "sheet_count": 3,
    "formula_count": 4,
    "image_count": 1,
    "chart_count": 1
  }
}
```

要求：

- `reason_codes` 稳定、可测试。
- `signals` 只保存结构判断所需信息，不保存 Token、签名 URL 或大段原文。
- 不人为伪造 0～1 Router confidence。
- Route Decision 至少进入结构化日志 / 任务结果；是否持久化等 Admin 需求明确后再做最小 Schema。

## 第一版总体路由矩阵

| 格式 | `auto` 默认策略 | 说明 |
|---|---|---|
| TXT | Native | 确定性文本 |
| CSV | Native | 确定性表格文本 |
| Markdown / MD | Native；缺附件时不得宣称完整 | 单文件 `.md` 无法恢复未上传图片 |
| JPG / JPEG / PNG | MinerU Whole | OCR / 视觉解析 |
| DOC | MinerU Whole | 不开发旧 Word 二进制正文 Parser |
| DOCX 简单正文 / 原生表格 | Native | Preflight 未发现 Native 未覆盖结构 |
| DOCX 含图片 / 公式 / 文本框 / 图表 / 嵌入对象等 | 11B.1：MinerU Whole | 未来可在 11B.2 评估 Hybrid，但不是首选候选 |
| PPT / PPTX | MinerU Whole | 高度依赖页面视觉布局，第一版不拆元素 |
| XLS | MinerU Whole + 已知质量边界 | 兼容路径不等于完整语义解析 |
| XLSX 简单单元格 / 公式 | Native | 保留 Sheet / Cell / 公式原式 |
| XLSX 含图片 / 图表 / 绘图等 | 11B.1：Review / 已验证 Whole-MinerU；11B.2：优先候选 Hybrid Bundle | 必须由真实 XLSX MinerU 对照决定 |
| PDF 简单原生文本 | Native | 必须通过页面级 Preflight |
| PDF 扫描 / 混合 / 复杂版式 / 重要视觉内容 | MinerU Whole | 第一版和 11B.2 都不做逐页混合 |

## Markdown 特殊边界

Markdown 中：

```markdown
![销售图](images/sales.png)
```

只保存引用路径，不等于图片字节已经上传。

当前单文件上传模式下：

- 无图片引用 → Native。
- 引用了本地附件但附件未上传 → 不得宣称完整解析。
- 不因为存在图片引用就直接把 `.md` 发给 MinerU。
- 第一版不自动抓取任意远程 URL，避免 SSRF、权限和内容漂移。
- 后续如需要完整 Markdown + Assets，单独设计 ZIP + manifest / attachment relation，不塞进 Router v1。

## DOCX Preflight Inspector

DOCX 为 OOXML ZIP，可低成本检查：

```text
word/media/*
w:drawing
w:pict
m:oMath / m:oMathPara
w:txbxContent
charts / diagrams / SmartArt
embedded object / OLE object
header / footer / footnote / endnote
```

Native DOCX 当前稳定覆盖：

```text
Heading
Paragraph
Native Table
```

11B.1：

- 发现显著视觉 / 未覆盖结构 → 整份 MinerU。
- 不因为已有大量正文就忽略图片、公式或文本框。
- 不做 Native 正文 + 单图 MinerU 自动拼接。

11B.2：

- 只有 XLSX Hybrid 验证稳定后，才评估 DOCX Hybrid。
- 若评估，必须能从 OOXML 正文顺序中获得可靠 drawing / image anchor；仅凭 `word/media/image1.png` 文件名不足以回插。

## PDF Preflight Inspector

不能继续使用：

```text
有文本层 → Native
无文本层 → MinerU
```

至少按页检查：

```text
原生文本是否存在且足够
图片数量与面积占比
无文本但存在 image / drawing / annotation
明显扫描页
复杂 drawing / table / formula / 多栏风险
Native Text + Image 同页混合
```

路由：

- 全部页面均为简单原生文本且无显著视觉语义 → Native。
- 任一页明显扫描 / 混合 / 复杂视觉 → 整份 MinerU。
- 小 logo 不应单独触发 MinerU；阈值必须配置化并由测试校准。
- 不做逐页 Native + MinerU 自动拼接。
- 11B.2 的 Visual Bundle **不扩展到 PDF**；PDF 的页面、bbox、图文关系比 Office 元素更复杂，优先 Whole-MinerU。

## XLSX Preflight Inspector

Native XLSX 的确定性优势：

```text
Sheet 顺序
Cell 值
公式原式
```

Preflight 检查：

```text
xl/media/*
xl/drawings/*
xl/charts/*
xl/embeddings/*
VML / shape / picture
```

路由：

```text
无显著视觉对象
→ Native

存在视觉对象
→ complex_xlsx
```

`complex_xlsx` 必须先完成真实 `XLSX → MinerU` 对照验收，不能从 XLS 结果类推。

### XLSX 决策分支

若真实 MinerU 验证证明：

```text
Cell / Formula / Visual
均能满足 RAG 完整性要求
```

则可以：

```text
complex XLSX → MinerU Whole
```

若验证证明：

```text
Native：Sheet / Cell / Formula 原式更可靠
MinerU：图片 / 图表 /视觉信息更强
单一路径都存在明显缺失
```

则进入 11B.2 的 `hybrid_bundle` 评估。

在 Hybrid 真正验收前：

- `auto` 不得静默宣称复杂 XLSX 已完整解析。
- 可以进入 `review`，或按真实测试结论使用已验证的 Whole-MinerU。
- 管理员仍可显式 `native` / `mineru` 做 A/B 对比。

## 阶段 11B.1：Whole-document Router v1

第一版只完成：

```text
Preflight Inspector
→ Route Decision
→ Native 或 MinerU Whole
→ Completeness Guard
```

不做：

```text
逐页 Native + MinerU
逐图片 / 逐图表 MinerU
Visual Bundle
跨 Provider Block 自动合并
XLSX Native + MinerU Visual 去重
复杂 Reading Order 重建
```

11B.1 的目标是先把“什么时候应该整份 Native、什么时候应该整份 MinerU”做对并可解释。

## 阶段 11B.2：Selective Hybrid Visual Enhancement

### 启动条件

11B.2 不是默认必做项。只有同时满足以下条件才启动：

1. 真实 Ground Truth 证明单一 Provider 无法完整覆盖。
2. Native 与 MinerU 能力具有明确互补性。
3. 可以稳定定位需要视觉增强的元素在源文档中的 Anchor。
4. 可以通过单个 Visual Bundle 控制 MinerU 文件任务数。
5. 可以做到事务原子性、稳定 Block ID 和来源可追溯。

**优先候选：复杂 XLSX。**

DOCX 只能在 XLSX Hybrid 已证明可控后再评估；PDF 与 PPT/PPTX 第一版继续 Whole-MinerU。

### Hybrid 总体流程

```text
Source XLSX
   ↓
Native Primary Parse
   ├─ Sheet
   ├─ Cell
   ├─ Formula
   └─ Native Table Blocks
   ↓
Visual Candidate Extractor
   ├─ embedded image
   ├─ screenshot
   ├─ formula image
   └─ only-if-needed visual chart candidate
   ↓
Visual Filter / Dedup
   ↓
visual_bundle.pdf + bundle_manifest.json
   ↓
ONE MinerU submission
   ↓
MinerU Visual Blocks
   ↓
Anchor-based Merge
   ↓
Unified DocumentBlock
```

Native 是 Hybrid 的“主解析结果”，MinerU 是“视觉增强结果”，不得反过来用 MinerU 结果覆盖确定性公式原式或单元格真值。

### Visual Candidate 不等于所有媒体对象

不是所有图片都值得提交 MinerU。

至少过滤：

```text
极小装饰图标
重复 logo
纯色背景
完全重复内容 hash
明确无知识语义的页眉 / 装饰图片
```

保留候选：

```text
截图
含文字图片
图表截图
公式图片
业务流程图
其他可能承载独立知识的视觉对象
```

过滤规则必须配置化、可测试，不按测试 Marker 硬编码。

### XLSX Chart 的处理优先级

Excel 原生 Chart 不应一律先渲染成图片再送 MinerU。

优先级：

```text
1. 能通过 OOXML / openpyxl 确定性读取的 chart title / series / category / value reference
   → 优先结构化提取，零 MinerU 调用

2. 只有视觉表达本身承载额外语义、或结构化读取不足时
   → 才进入 Visual Candidate
```

这样可以进一步减少 MinerU 页数和额度消耗。

如果当前运行环境无法可靠渲染原生 Excel Chart，不能伪造截图；应保留结构化 Chart 信息或明确进入 review，而不是为了 Hybrid 引入不稳定的 GUI 自动化。

### Bundle Manifest

每个视觉候选必须有稳定 `visual_id`，例如：

```text
visual_id
source_doc_id
source_type
source_section
source_anchor
content_hash
media_type
width
height
bundle_page
```

例如 XLSX：

```text
visual_id      = visual-0003
source_section = 销售分析
source_anchor  = sheet=销售分析;anchor=D5:J20
source_type    = embedded_image
bundle_page    = 2
```

要求：

- Bundle Page 只用于 MinerU 返回结果映射，不是原文档物理页码。
- 不依赖 MinerU OCR 识别 `visual_id` 字符串来建立映射；映射来自本地 manifest。
- 第一版建议“一候选一 Bundle Page”，简化映射和失败诊断。
- Bundle 页数不得超过 MinerU 当前页限制；超过本地配置阈值时转 `mineru_whole` 或 `review`，不得继续生成超限任务。

### Visual Bundle 文件额度原则

对同一源文档、同一解析代次：

```text
hybrid_bundle
→ 最多一个 MinerU Visual Bundle 提交
```

禁止：

```text
每个 image 一个 MinerU task
每个 chart 一个 MinerU task
每个 formula image 一个 MinerU task
```

同一文档内相同 `content_hash` 的视觉对象：

- MinerU 只解析一次。
- 可以在多个 Anchor 复用同一标准化视觉结果。

未来阶段 12 Parse Cache 可进一步跨文档复用相同 `visual_hash`，但 11B.2 不提前实现完整缓存系统。

### Merge 原则

Hybrid Merge 必须以“原文档语义”为准，而不是以临时 Bundle 语义为准。

XLSX Visual Block 合并后：

```text
section = 原 Sheet 名
page = 1（沿用 XLSX 逻辑页约定，不把 bundle_page 当文档页）
bbox = NULL
source = mineru_cloud
```

顺序通过原始 `source_anchor` 插入 Native Block 流。

稳定 Block ID 应基于：

```text
original doc_id
+ hybrid version
+ visual_id
+ visual-local block order
```

不得使用临时 Bundle 的随机文档 ID 作为最终业务 Block ID。

### 去重原则

Hybrid 最容易产生：

```text
Native Cell 已有文本
+ 图片 / 图表 OCR 又重复同一数字和标签
```

第一版只做保守去重：

- 同一 `visual_id` 内精确 / 规范化文本重复可以去除。
- Anchor 邻域内完全相同文本可以避免重复插入。
- 不用 LLM 做不可解释的全文语义去重。
- 不因为相似度高就删除可能承载新语义的视觉内容。

### 原子性与失败处理

Hybrid 必须事务化：

```text
Native 临时结果
+ Visual Bundle MinerU 结果
+ Merge
→ 全部成功
→ 一次事务替换最终 document_blocks
```

若 Visual Bundle：

```text
鉴权失败
配额失败
网络失败
供应商失败
映射失败
Merge 失败
```

则：

- 不持久化半份 Hybrid Block。
- 不把 Native 半结果偷偷标为完整 `CHUNKING`。
- Document 进入失败 / 明确 review 状态语义，并保留安全错误。
- 不自动再追加第二次 Whole-MinerU 调用消耗额度；是否改用整份 MinerU 由显式重试 / 管理员决策。

## Fallback 规则

- `auto` 选择 Native 后，如果 Parser 抛出明确 `OCRRequiredError` / 完整性错误，可 **最多一次** 转 MinerU Whole，并记录 fallback reason。
- Preflight 已选择 MinerU Whole 后，MinerU 失败不得静默退回已知不完整 Native。
- Hybrid Bundle 失败不得逐元素重试，也不得自动升级为第二个 Whole-MinerU 文件任务。
- 禁止 Native ↔ MinerU 循环回退。
- Provider 未成功完成前不得持久化半份最终 Block。

## Completeness Guard

至少检查：

- Block 非空且有有效正文。
- 页面 / Sheet / Slide 逻辑范围基本合理。
- Preflight 的视觉风险信号没有被 Native 成功静默忽略。
- Hybrid manifest 中每个 required visual candidate 都有明确结果：`parsed / filtered / reused / failed`。
- Office MinerU `bbox = NULL` 约定不被破坏。
- 不按 Marker 字符串硬编码完整性规则。

复杂文档无法自动证明完整时，应暴露限制或进入 review，不把“API 成功”写成“语义完整”。

## Reading Order 原则

阶段 10 / 11A 已观察到扫描 PDF、PPT/PPTX 等存在 Provider Reading Order 与视觉顺序不完全一致。

因此：

- 不用简单全局 `y-sort` 覆盖供应商顺序。
- 不跨页面重排。
- 表格、公式等结构块不拆散排序。
- Reading Order Normalizer 与 Router / Hybrid Merge 解耦。
- Hybrid XLSX 依赖 Anchor 插入，不依赖 MinerU 对整个文档的 Reading Order。

## 混合文档人工验收矩阵（用户准备并执行）

以下仅用于编写预期结果和指导用户验收，**Codex 不得依据本矩阵生成测试文档、启动解析器或调用 MinerU。**

阶段 11B 至少准备：

```text
mixed.docx
- Native 标题 / 正文 / 原生表格
- 图片文字
- 图片公式
- 后续正文

mixed.xlsx
- 多 Sheet
- 单元格
- 公式原式
- 原生 Chart
- 嵌入图片 / 截图
- 重复图片 hash

mixed_page.pdf
- Page 1 原生文本
- Page 2 扫描
- Page 3 原生文本

mixed_same_page.pdf
- 同页原生文本
- 同页截图 / OCR-only 文字

two_column.pdf
complex_layout.pdf

mixed.pptx
- 文本框 / 表格 / 图表 / 图片文字

markdown_with_image_ref.md
- 本地图片引用但附件未上传
```

11B.2 额外准备：

```text
xlsx_hybrid_bundle_1.xlsx
- 5+ 视觉候选
- 2 个完全重复图片
- 原生公式
- 原生 Chart
- 图片 OCR Marker

xlsx_hybrid_bundle_over_limit.xlsx
- 视觉候选数量超过本地 Bundle 上限
- 验证不会生成超限 MinerU 任务
```

每份测试记录：

```text
expected_route
expected_reason_codes
expected_required_content
expected_missing_if_native
expected_missing_if_mineru
expected_visual_candidates
expected_bundle_submit_count
expected_page / section / slide semantics
```

## 自动化测试用例设计（Codex 只编写，用户负责执行）

**执行约束：** Codex 只负责实现测试文件、Mock/Stub 及断言；不执行 `pytest`、任何 `smoke.py`、文档解析或云服务请求。用户测试并反馈结果后再修复。


### 11B.1

至少覆盖：

```text
TXT / CSV → Native
简单 DOCX → Native
DOCX + media / formula / drawing → MinerU Whole
PPT / PPTX → MinerU Whole
DOC / XLS → MinerU Whole
简单 XLSX → Native
complex XLSX → 已验证 Whole-MinerU 或 Review
简单 PDF → Native
扫描 / 混合 PDF → MinerU Whole
Markdown 无图片引用 → Native
Markdown 缺附件 → 不得宣称完整
parse_mode=native / mineru 显式覆盖
Native → MinerU 单次安全 fallback
MinerU 失败不静默回退 Native
Route Decision reason_codes 稳定
阶段 1～11A 回归用例保持可执行；由用户运行并判断是否通过
```

### 11B.2（只有启用 Hybrid 时）

至少覆盖：

```text
一个 XLSX 有多个 visual candidates → 只产生 1 个 MinerU 文件提交
重复 visual hash → 云端只解析一次，但可回插多个 Anchor
Native 公式原式不会被 MinerU 计算结果覆盖
Bundle page → visual_id → source anchor 映射稳定
bundle_page 不写成 XLSX 文档 page
MinerU visual block 的 section 回到原 Sheet
bbox = NULL
Hybrid Block ID 重试稳定
Visual Bundle 失败 → 不写半份 blocks
Hybrid 失败 → 不逐元素补发 MinerU
超本地 visual / page 上限 → review 或已定义安全策略
装饰 / 小图 / 重复 logo 不进入 Bundle
Chart 可结构化读取时不无条件发送 MinerU
```

## 人工验收标准（仅用户执行，Codex 不得代跑）

### 11B.1

至少验证：

1. 简单 Native 文档不产生 MinerU 调用。
2. 含视觉内容的 DOCX / PDF 被识别并整份 MinerU。
3. PPT/PPTX 默认 MinerU Whole。
4. 简单 XLSX 保留公式原式与 Sheet。
5. `XLSX → MinerU` 真实结果完成后，complex XLSX 得到明确 Whole / Review / Hybrid 候选结论。
6. Route Decision 可解释。
7. 配额 / 网络失败不会导致复杂文件静默降级。
8. 最终 Block 不伪造页码 / bbox。

### 11B.2

只有真实对照证明 Hybrid 必要时再验收：

1. 同一 XLSX 中多个图片 /视觉元素只产生 **1 个 Visual Bundle MinerU 文件任务**。
2. Native Sheet / Cell / Formula 与视觉 OCR 结果均保留。
3. 视觉结果按原 Sheet / Anchor 插回，不使用 Bundle Page 作为业务页码。
4. 重复图片不重复消耗 MinerU 解析。
5. Bundle 页数、文件大小与平台限制均被本地保护。
6. 失败场景无半份最终 Block。
7. 比较 Whole-MinerU、Native、Hybrid 三种结果的完整性与 MinerU 消耗，证明 Hybrid 确有收益后才保留该功能。

## 配额与效率约束

阶段 11B 必须同时记录“解析完整性”和“云端消耗”，不能只优化其一。

建议指标：

```text
source_document_count
mineru_file_submit_count
mineru_files_per_source_document
mineru_page_count
native_route_count
mineru_whole_route_count
hybrid_bundle_route_count
hybrid_visual_candidate_count
hybrid_visual_filtered_count
hybrid_visual_dedup_count
hybrid_bundle_page_count
```

目标原则：

```text
普通 Native 文档 → 0 MinerU file
MinerU Whole 文档 → 1 source document ≈ 1 MinerU file
Hybrid 文档 → 1 source document ≈ 1 Visual Bundle MinerU file
```

除显式 Reparse / 人工重试外，不允许一个源文档因为多个视觉元素自动产生 N 个 MinerU 文件任务。

## 本阶段禁止事项

不要：

```text
把所有文件无脑交 MinerU
把 Native 成功等同于完整解析
每个图片 / 图表 / 公式单独发 MinerU
按测试 Marker 写路由规则
全局 y-sort 修 Reading Order
未经真实 XLSX 对照就启用 Hybrid
把 PDF / PPTX 一开始就做逐元素 Hybrid
为了 Hybrid 引入不稳定 GUI 自动化截图
Hybrid 失败后自动再提交 Whole-MinerU 造成不可控额度翻倍
为了 Router 提前进入 Chunk / Embedding / RAG
```

## 完成标准

### 11B.1 完成标准

- `auto` 基于完整覆盖能力做决策。
- 所有支持格式有可解释 Route Decision。
- 简单 Native 文档 0 MinerU 调用。
- 扫描 / 复杂 / 混合文档不会因 Native 能提取部分文本而静默漏主要视觉内容。
- MinerU 失败不静默降级成已知不完整 Native。
- complex XLSX 在真实云测试后明确进入 Whole-MinerU、Review 或 11B.2 候选。

### 11B.2 完成标准（仅在启用时）

- 同一源文档视觉增强默认最多一次 MinerU Visual Bundle 提交。
- Native 确定性结构不被 MinerU 覆盖或降级。
- Visual Candidate / Bundle / Manifest / Merge 全链路可解释。
- Bundle 临时页语义不会污染原文档 page / section / Citation。
- Merge 幂等、失败原子、Block ID 稳定。
- 实测证明 Hybrid 相比 Native / Whole-MinerU 在“内容完整性 vs MinerU 消耗”上确有可量化收益。

---

# 18. 阶段 12：Parse Cache

## 目标
避免 Reindex、任务重试、重复操作反复消耗 MinerU API。

## Cache Key

Whole-document 解析至少包含：

```text
file_hash
provider
model_version
language
is_ocr
enable_table
enable_formula
```

如果阶段 11B.2 最终启用 Hybrid Visual Bundle，缓存还需要区分：

```text
route_strategy
visual_extractor_version
bundle_manifest_hash
visual_content_hash
```

其中 `visual_content_hash` 用于后续复用完全相同的图片 / 截图视觉结果；不能只按临时 Bundle 文件 hash 缓存，否则不同 Bundle 中的同一视觉对象无法复用。

解析结果需要本地持久化，例如：

```text
data/parsed/{doc_id}/
├── raw/mineru_result.zip
├── content_list.json
├── full.md
└── hybrid/                 # 仅启用 Hybrid 时
    ├── bundle_manifest.json
    ├── visual_bundle.pdf
    └── normalized_visual_blocks.json
```

实际目录通过配置管理。

## Cache 行为

```text
文件未变化
+ Provider / Route Strategy 未变化
+ 模型和解析参数未变化
→ 命中 Parse Cache
→ 不调用 MinerU
→ 复用解析结果

Hybrid 场景进一步允许：

相同 visual_content_hash
+ 相同 MinerU model / options
+ 相同 visual extractor version
→ 复用已标准化视觉结果
→ 不把同一图片再次提交 MinerU
```

`Reindex` 默认：

```text
Parse Cache
→ Chunk
→ Embedding
→ Index
```

只有明确的 `Reparse / Force Reparse` 才允许重新调用 MinerU。

## 完成标准
正常 Reindex 不增加 MinerU 提交次数；强制 Reparse 才重新消耗云 API。若启用 Hybrid，相同视觉对象的缓存命中也不得增加 MinerU 文件任务数。

---

# 19. 阶段 13：Chunk

## 目标
把 DocumentBlock 转为可检索 Chunk。

## 第一版策略

输入 Block 可能包含：

```text
title
text
table
formula
image_text
```

切分优先级：

```text
标题边界优先
→ 标题与后续正文尽量保持上下文
→ 段落边界优先
→ 表格尽量整体保留
→ 公式与相邻解释尽量不切断
→ 超长内容再按长度切分
```

初始参数：

```text
chunk_size = 400~700 tokens
overlap = 50~100 tokens
```

每个 Chunk 必须保存：

```text
kb_id
doc_id
chunk_id
text
page_start
page_end
section
metadata
```

## 完成标准
任意 Chunk 可反查原文文档和页码。

---

# 20. 阶段 14：BGE-M3 Embedding

## 现有实现与手动部署

以提交 `1c1297e51e1cf2964109e0b653735288d7aa5ad9` 为基线，阶段 14 业务代码已实现；补充部署文档不意味着重新开发、重构或已经通过真实 GPU 验收。

- 正式实现使用 `sentence_transformers.SentenceTransformer`，文档/Query 共用 1024 维、L2 归一化的 Dense 服务，不使用 FlagEmbedding.BGEM3FlagModel，不因缺少 FlagEmbedding 要求安装它。
- 本地模型为 `BAAI/bge-m3`，目录默认 models/bge-m3；离线加载、不自动下载、不启用远程代码，不用第二套推理框架。
- 默认 CUDA cuda:0、batch=16、max_length=8192、fp16=true；RTX 4060 Laptop 8GB 首次部署可由用户考虑 batch=1～2。已建立的检查点冻结参数，修改环境变量不能直接覆盖原任务。
- 迁移 head 为 `0006_embedding_checkpoint`；文档任务在独立 gpu_queue 内逐批处理，Windows 使用 solo / concurrency=1。已有 Chunk 经 POST `/embed` 完成后进入 INDEXING，结果仅本地保存；不写 pgvector、不创建索引、不标记 READY。
- 环境检查、匹配现有版本的 CUDA Wheel、缺失依赖安装、完整模型下载与最小部署验证，统一见 [README：本地 BGE-M3 与 GPU Worker](../README.md#本地-bge-m3-与-gpu-worker)；详细验收继续见 [Embedding](EMBEDDING.md)。以上操作全部受第 0 节约束，仅由用户手动执行，Codex 提供命令后停止等待反馈，未经确认不进入阶段 15。

## 目标
本地向量化文档 Chunk 和 Query。

模型：

```text
BAAI/bge-m3
```

统一 Service：

```text
embed_documents()
embed_query()
```

文档 Embedding：

```text
Celery GPU Worker
Batch Processing
```

不要一 Chunk 一个 Celery Task。

GPU 默认：

```text
concurrency = 1
```

## 完成标准
Query 与 Chunk 向量维度一致，可批量生成。

---

# 21. 阶段 15：pgvector 索引入库

## 当前实现与验收边界

- 新增显式 POST `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/index`，独立 index_task_id/index_config；default_queue 单文档任务只消费阶段 14 的完整本地结果，不重新推理或调用云端。
- 校验解析/切片/GPU 输入身份、完整文件校验和、实际模型指纹/编码版本/精度、1024 维有限 L2 单位向量；固定 256 条批次更新现有 Chunk，不改正文、证据或稳定标识。
- 所有批次与 READY 同事务，失败回滚再独立记录安全原因；临时数据库错误有限重试，重复/旧消息幂等。READY 文档每个 Chunk 都有向量，不等于 Retrieval/RAG 已实现。
- `0007_pgvector_index` 新增私有入库检查点及 HNSW cosine 部分索引；迁移可能阻塞写入，需用户维护窗口。无新增依赖、环境变量、自动扩展升级或逐文档建索引。
- 代码及测试用例已编写，迁移、测试、真实 pgvector/Worker 验收均未执行。具体部署见 [README](../README.md#已有向量-pgvector-入库)，用户验收及 SQL 见 [pgvector 入库](PGVECTOR_INDEX.md)。第 0 节权限禁令继续适用，不自动进入阶段 15A。

## 目标
将向量写入 PostgreSQL。

流程：

```text
Chunk
→ Embedding
→ pgvector
→ Index
```

完整状态：

```text
UPLOADED
→ PARSING
→ CHUNKING
→ EMBEDDING
→ INDEXING
→ READY
```

## 完成标准
READY 文档所有 Chunk 均具备向量。

---

# 21A. 阶段 15A：LlamaIndex RAG Adapter

## 当前实现与待验收

- 已建立显式导入的 node_adapter/embedding_adapter、框架无关 RetrievalResult；ORM 字段必须已加载，互转不隐式读取/写库，id_=chunk_id、SOURCE 与文档/知识库一致，保留完整字符证据及逻辑/物理页语义。
- Node 默认复制已有 pgvector 向量；Query 复用项目共享 BGE-M3，构造不加载模型，框架文档编码入口直接拒绝，不创建第二套模型/缓存或向量真相表。
- 用户已明确确认新增最小 llama-index-core 0.14.x 声明；尚未安装或导入验证。无新数据库迁移、环境变量、API、Worker 或完整 Retriever；阶段 1～15 状态语义不变。
- 自动/DB 用例只编写，均未执行。core 缺失时 Adapter 用例跳过而非破坏历史收集，跳过不算验收通过；安装、NLP 缓存准备、人工转换与回归步骤见 [Adapter](LLAMA_INDEX_ADAPTER.md)。未经确认不进入后续阶段。

## 目标

在不改变 DKF-Agent 已有数据模型和索引存储的前提下，引入 LlamaIndex 作为后续 RAG 编排层的统一抽象。

本阶段只建立适配边界，不实现完整 Retrieval / BM25 / Rerank / RAG。

## 核心原则

LlamaIndex **不是**本项目的文档解析器、切片器、Embedding 存储器或数据库真相来源。

继续由 DKF-Agent 自主管理：

```text
Native Parser / MinerU
Parse Cache
DocumentBlock
Chunk
BGE-M3 Embedding
PostgreSQL + pgvector
document_chunks
```

LlamaIndex 从本阶段开始只负责：

```text
Node / NodeWithScore 统一表示
Retriever 抽象
Query Fusion
Node Postprocessor / Reranker 编排
Response Synthesis
RAG Query Engine 编排
```

## 禁止的接入方式

生产主链路不要使用下列方式重新接管已有流程：

```text
SimpleDirectoryReader → 重新读取原文件
VectorStoreIndex.from_documents() → 再次自动切片 / Embedding
IngestionPipeline → 再建一套 Parse / Chunk / Embedding Cache
PGVectorStore → 再建立一套独立业务真相表
```

允许在独立实验或测试中使用官方示例验证框架，但不得把其作为生产数据主路径。

## Node Adapter

新增统一适配层，例如：

```text
app/services/rag/llama_index/node_adapter.py
```

将已有 `DocumentChunk` 转换为 LlamaIndex `TextNode`，至少保留：

```text
id_ = chunk_id
text
metadata.kb_id
metadata.doc_id
metadata.chunk_id
metadata.file_name
metadata.page_start
metadata.page_end
metadata.section
```

要求：

- `chunk_id` 是 LlamaIndex Node 的稳定业务标识，不重新随机生成。
- LlamaIndex metadata 不得丢失 Evidence / Citation 所需字段。
- Node 转换后可以还原为项目内部检索结果，不把 LlamaIndex 类型泄漏到数据库模型。
- PostgreSQL `document_chunks` 仍是唯一持久化真相来源。

## Embedding 边界

已有 BGE-M3 `EmbeddingService` 继续是项目正式 Embedding 实现。

如果 LlamaIndex Retriever / Query Engine 需要 Embedding 接口，则增加 Adapter 调用已有服务，不另行加载第二套 BGE-M3 模型，不重复生成文档向量。

## 自动化测试用例设计（Codex 只编写，用户负责执行）

**执行约束：** Codex 只负责实现测试文件、Mock/Stub 及断言；不执行 `pytest`、任何 `smoke.py`、文档解析或云服务请求。用户测试并反馈结果后再修复。


至少覆盖：

```text
DocumentChunk → TextNode
TextNode / NodeWithScore → DKF Retrieval Result
chunk_id 稳定
metadata 完整
page / section 不丢失
同一 Chunk 重复转换结果一致
不产生第二份数据库记录
不触发文档重新 Parse / Chunk / Embedding
```

## 完成标准

- LlamaIndex 可以消费现有 `document_chunks`，不重新读取原文档。
- Chunk / Page / Section / Document 来源链不丢失。
- 不新增第二套向量真相表。
- 不改变阶段 1～15 已完成的数据处理语义。

---

# 22. 阶段 16：预置知识库批量导入

## 当前实现与待验收

- 已实现 `scripts/import_knowledge_base.py`：默认只读预检；用户显式确认云外传/费用后，复用默认库 Seed、单文件存储、auto Router 与独立 Chunk/Embedding/Index 检查点，有界推进至 READY。
- **阶段 16：代码已实现，真实数据验收待完成。** 真实数据集正在由用户独立 PowerShell 下载；真实十份以上多格式资料、覆盖清单、导入与质量验收仍待用户完成，不自动生成/下载/导入或干扰素材。标签不证明 OCR、公式、图表或 Reading Order 质量。
- 无新增依赖、环境变量、数据库迁移或 API；普通上传保持 uploaded。重复导入复用 preset 内容，FAILED/已有检查点不自动重投，收费恢复继续遵守 Parse Cache 约束。
- 用例仅编写，未执行预检/导入/回归或真实云/GPU 测试。部署命令见 README，完整人工验收与 SQL 见 [预置导入](PRESET_IMPORT.md)。本阶段只准备默认库 READY 数据；查询需后续阶段，不提前实现 Retrieval/RAG。

## 目标
建立比赛演示默认知识库。

## 阶段16/17三数据集联合冒烟（增量恢复OCB）

现有 smoke_stage16_17 已有 OHR+Omni 链路，增量纳入已下载 OCB；不重新开发业务处理或扩展下载器。默认约14份：OHR 5 PDF/约20条官方QA，Omni 3张 GT图像，OCB DOCX/XLSX/PPTX各2份及每文件最多4条完整官方QA。用户当前归档127份/547条依赖完整QA可以启动抽样，不要求补齐剩余43份；真实统计和容器/Hash重检仍以运行报告为准。

- 从本地固定版本元数据、归档回执、QA JSON及官方Parquet核对身份；缺QA导出就在新run形成副本，不下载、不覆盖旧报告或原件。优先单文件；多文件QA全依赖必须存在且全部入选，不拆题、不改原始断言。
- 同一原上传API的 auto Router→Block→Chunk→Embedding→Index→READY；无第二套模型/索引/业务表。下载完成不是版权/云权限，真实process前由用户确认所选全部文件外传及额度，不要求许可Excel/JSON合并。
- OHR与OCB分别报告 Hit@1/5/10、MRR@20及有效分母，异常/未执行不伪填零；OHR使用可靠0-based→1-based物理证据页，Office逻辑页不冒充物理页。Omni GT不是QA。
- OCB expected_assertions及答案证据为manual_pending，DOCX段落/标题/表格、XLSX Sheet/单元格/原式、PPTX图文/页面顺序需人工对照。不做字符串包含答案裁判、LLM或官方总分。
- 三数据集分别显示PASS/FAIL/BLOCKED/NOT_APPLICABLE及人工质量状态；不降低正式10份/旧Office/三现代格式/七标签/READY/幂等标准。旧run、QA/GT、preset/manifest和现有报告不改写；阶段16/17仍待人工验收，不进入18。

本轮只交付代码、Mock用例及说明，Codex不执行抽样/上传/数据库/云调用/pytest；最短用户命令见 [三数据集操作](PRESET_IMPORT.md#三数据集最短操作新运行)。

准备：

```text
data/knowledge_base/preset/
scripts/import_knowledge_base.py
```

要求：
- 10+ 文档。
- 覆盖多种后缀与内容形态，而不是同一种 PDF 重复凑数。
- 至少包含 TXT / MD / CSV / DOCX / XLSX / PPTX / PDF / 图片中的多类格式。
- 至少包含普通文本 PDF、扫描 PDF、混合文档、表格、公式、图表和 Reading Order 测试。
- 旧 Office（DOC / XLS / PPT）至少各保留 1 个兼容性样本，用于验证 MinerU 路径。

流程：

```text
扫描目录
→ Document
→ Celery
→ Parser Router
→ Native / MinerU Cloud
→ Chunk
→ Embedding
→ READY
```

## 完成标准
用户无需上传文件即可开始查询。

---

# 23. 阶段 17：Dense Retrieval（LlamaIndex Retriever）

## 当前实现与验收边界

- **阶段 17：代码已实现后待人工验收，不能宣称真实检索成功。** 用户明确允许阶段 16 待验收期间先实现本阶段，非阶段 16 验收结论，不授权自动进入 18。
- `app/services/rag/llama_index/dense_retriever.py` 继承 BaseRetriever，复用 DKFEmbeddingAdapter 与进程共享 BGE-M3；Query 编码前后核对实际模型身份，文档编码禁止项不变。
- `app/services/rag/dense.py` 用现有 SQLAlchemy/pgvector `<=>` 参数化查询 document_chunks；限定 kb_id/ACTIVE 库、READY、非空向量、实际完整模型身份和当前解析/切片/GPU 代次，未知历史向量排除。
- 首版过滤候选物化后执行精确 Top-K，默认 20、范围 1～100，余弦距离升序/稳定 chunk_id 处理同分；既有 HNSW 保留，未声称 ANN 性能或大库吞吐验收。相似度 1-distance 不代表概率。
- Node/NodeWithScore 复用 15A 转换，正文/标识/页码/章节/source_spans 保持原值；独立短会话只读事务与超时，不写库、不重新 Parse/Chunk/Embed/Index、不注册全局 Tracing。
- 仅 Python 库级入口；doc_ids筛选由下节阶段18增量补齐，没有 HTTP Retrieval/RAG API、BM25、Fusion、Rerank 或 Agent。没有新依赖、环境变量、迁移、表、模型或 Worker。
- 单元/SQL/框架/日志 Stub 用例只编写，不执行。Mock 不证明真实 PostgreSQL/CUDA/语义相关性；部署入口见 README，真实只读人工验收与风险见 [Dense Retrieval](DENSE_RETRIEVAL.md)。此前阶段与 16 的真实验收仍待用户完成。

## 目标

在 LlamaIndex Retriever 抽象下实现第一版向量检索，但底层仍查询项目自己的 PostgreSQL + pgvector。

流程：

```text
Question
→ BGE-M3 embed_query()
→ Custom DenseRetriever
→ PostgreSQL / pgvector
→ DocumentChunk
→ TextNode / NodeWithScore
→ Top-K
```

初始：

```text
top_k = 20
```

Retriever 至少返回 / 保留：

```text
chunk_id
doc_id
kb_id
file_name
page_start
page_end
section
text
retrieval_score
```

## 原则

- 第一版优先使用自定义 LlamaIndex Retriever 包装现有 SQLAlchemy / pgvector 查询。
- 不要求使用 LlamaIndex `PGVectorStore`；避免生成第二套向量表和 metadata 表。
- Query Embedding 必须调用已有 BGE-M3 服务。
- LlamaIndex Node 只是运行时表示，不是新的持久化真相来源。

## 完成标准
问题可以通过 LlamaIndex Retriever 抽象检索出正确的项目 Chunk，并完整保留 Citation metadata。

---

# 24. 阶段 18：单文档 Retrieval

## 当前实现与验收边界

- 已由用户测试并确认通过，Codex未代跑；不据此宣告阶段16/17其他待验收事项或完整RAG通过。
- `DenseRetriever(kb_id, doc_ids=[...], top_k=20)` 冻结文档范围；省略/None保留全库模式，显式空列表/非法UUID拒绝，最多100个输入ID、归一化去重。
- 文档IN谓词进入现有MATERIALIZED候选CTE，在余弦距离/Top-K前与kb_id、READY、模型及代次过滤共同执行。不存在/跨库/非READY/模型不匹配排除，不扩大范围；异常越界行全批拒绝，不事后删行。
- Node/DTO继续使用原doc_id/chunk_id、page_start/page_end及source_spans.page；不伪造单独物理页字段，不读取原件或重编码文档。同步、异步及DTO入口共用相同范围和现有BGE-M3。
- 复用既有日志，仅增加已允许的单文档UUID/范围数量摘要；没有新API/模型/依赖/迁移。验收由用户执行并确认，命令及真实只读步骤见 [Dense Retrieval](DENSE_RETRIEVAL.md#单文档范围)。本轮停止，不进入19。

## 目标
满足赛题单文档 RAG，并验证 LlamaIndex Retriever 的业务过滤能力。

检索支持：

```text
kb_id
doc_ids
```

例如：

```text
doc_ids = ["xxx"]
```

过滤应尽量下推到 PostgreSQL 查询，而不是先全库检索再在 Python 中删除其他文档。

## 完成标准
指定文档检索不会混入其他文档 Evidence；Node metadata 中仍保留原始 `doc_id / chunk_id / page`。

---

# 25. 阶段 19：BM25 Retriever

## 当前实现与验收边界

- 已实现本地BM25Plus包装及NodeWithScore/DTO返回，待用户执行单测与真实关键词验收；不宣告问答或正式基准通过。
- 固定kb_id/可选doc_ids/Top-K，SQL限定ACTIVE、READY及当前代次；无向量列读取、Query模型调用、原件读取或新业务表。
- 原TextNode派生词频索引，实例内缓存按内容/Chunk集合/版本失效；每次查当前READY，元数据只用新Node，失败不返回旧缓存。无磁盘持久化或第二套真相。
- 数字/型号/缩写完整词、NFKC与中文字符/二元片段；分数降序/同分chunk_id，零词项重叠为空，非概率且不与Dense直接加分。安全预算与质量边界见 [BM25说明](BM25_RETRIEVAL.md)。
- 本轮仅补装已声明轻量rank-bm25=0.2.2，不升级其他依赖；不运行pytest/数据库/模型/云API或准备样本。停止等待用户验收，不进入20。

## 目标

补充关键词检索，并统一到 LlamaIndex Retriever 接口。

当前仍使用本地 BM25，不引入 Elasticsearch。可以使用 LlamaIndex 的 BM25 Retriever / 对应集成，也可以在其 Retriever 抽象下包装现有 BM25 实现。

数据来源只能是当前 READY 文档的 `document_chunks` 派生 Node：

```text
DocumentChunk
→ TextNode
→ BM25 Index
→ BM25 Retriever
```

BM25 索引属于**可重建的派生索引 / 缓存**，不是第二套业务真相。

重点验证：

```text
数字
型号
专有名词
英文缩写
精确关键词
```

## 完成标准
BM25 Retriever 能返回统一 `NodeWithScore`，并保持 Chunk / Document / Page metadata。

---

# 26. 阶段 20：Hybrid Retrieval / Query Fusion

实现说明：[Hybrid Retrieval](HYBRID_RETRIEVAL.md)。已使用LlamaIndex BaseRetriever组合既有两路，各Top20，1-based等权RRF（k=60），默认最终Top20、可设30，按chunk_id去重/UUID同分排序。Node与DTO保留原证据及fusion来源；原分数不直接相加，不调用LLM/改写Query。分支异常/证据冲突整体失败，正常空支允许另一支参与；用例只编写，真实检索与回归待用户执行，不进入21。

## 目标

使用 LlamaIndex 的 Retriever 组合能力统一 Dense + BM25。

推荐第一版：

```text
Dense Retriever Top 20
          \
           → QueryFusionRetriever / RRF → 去重 → Top 20~30
          /
BM25 Retriever Top 20
```

原则：

- 第一版优先使用 Reciprocal Rank Fusion / 等价稳定融合策略。
- 不把复杂 Query Rewrite、Multi-Query Expansion 作为第一版必选项。
- 融合去重以稳定 `chunk_id` 为主，不以纯文本相等作为唯一标识。
- 保留 dense score、bm25 score、fusion score 或可解释的排序来源。

## 完成标准
得到统一候选 `NodeWithScore` 集合；相同 Chunk 不重复；能解释候选来自 Dense、BM25 或两者。

---

# 27. 阶段 21：BGE Reranker（LlamaIndex Node Postprocessor）

实现/部署及验收：[BGE Reranker](RERANKER.md)。代码已实现项目RerankerService、已声明Transformers离线加载、同步批处理、共享Postprocessor和默认Top5；原检索/融合分与原始重排logit分别保留，Node/内部DTO双向转换且原证据不变。配置已同步，不新增表/迁移/队列/依赖；用例只编写，未下载/运行模型或执行测试，排序收益必须人工对照，不进入22。

## 目标

模型：

```text
BAAI/bge-reranker-v2-m3
```

流程：

```text
Hybrid Candidates
→ LlamaIndex Node Postprocessor / 自定义 BGE Reranker Adapter
→ Top 5 Evidence
```

要求：

- 复用项目自己的 `bge-reranker-v2-m3` 服务，不因接入 LlamaIndex 再加载另一套同模型。
- 输入输出保持 `NodeWithScore` / DKF Retrieval Result 可互转。
- 保留至少 `retrieval_score / fusion_score / rerank_score`。
- Rerank 不能修改原始 `chunk_id / doc_id / page / section / text`。

在线查询默认同步执行。

## 完成标准
最终 Top 5 Evidence 排序明显优于未重排候选，且 Evidence metadata 全部可追溯。

---

# 28. 阶段 22：DeepSeek RAG / Response Synthesis

## 目标
完成核心问答闭环，并使用 LlamaIndex 组织 Retrieval → Evidence → Response Synthesis。

边界：

```text
LlamaIndex
= RAG orchestration / Query Engine / Response Synthesis

DeepSeekService
= 项目正式 LLM 边界
```

如果 LlamaIndex 需要 LLM 对象，增加适配器包装已有 `DeepSeekService`；不要在 Query Engine 内散落新的 DeepSeek HTTP 客户端。

流程：

```text
Question
→ Dense + BM25
→ Query Fusion
→ BGE Reranker
→ Evidence Nodes
→ LlamaIndex Response Synthesis
→ DeepSeek
→ Answer
```

Prompt 原则：

- 只能使用最终 Evidence。
- 证据不足时明确说明。
- 不虚构来源。
- 不虚构页码。
- 数字与关键事实必须可追溯。
- LlamaIndex 默认 Prompt 不能未经审查直接作为比赛生产 Prompt。

建议项目 API 仍返回自己的 Schema：

```json
{
  "status": "success",
  "answer": "...",
  "citations": [],
  "insufficient_evidence": false
}
```

不得把 LlamaIndex 内部 `Response / NodeWithScore` 对象直接暴露给前端。

## 完成标准
可以完成真实问题的端到端 RAG，同时保留 DKF-Agent 自己的 Evidence / Citation 契约。

---

# 29. 阶段 23：Citation / Evidence

## 目标
实现可解释来源链。

```text
Answer
→ Evidence Node
→ chunk_id
→ DocumentChunk
→ DocumentBlock / Page / Section
→ Document
```

Citation 至少：

```text
file_name
page_start
page_end
section
chunk_id
text
retrieval_score
rerank_score
```

原则：

- Citation 以 DKF 数据库和 Node metadata 为准，不依赖模型生成引用。
- LlamaIndex metadata 只承载已有可验证来源，不允许让 LLM 自行补文件名或页码。
- 对 Office 逻辑页、PDF 物理页继续沿用 Parser 契约中的页码语义。

## 完成标准
答案中的重要信息都能反查到原始 Chunk / Document / Page，框架替换不会破坏 Citation。

---

# 30. 阶段 24：Knowledge Query API

## 目标
为前端和未来 Agent 提供稳定接口，并隔离 LlamaIndex 具体实现。

建议：

```http
POST /api/knowledge/search
POST /api/knowledge/query
```

`/search`：

```text
Query
→ LlamaIndex Retriever / Fusion / Rerank
→ DKF Evidence Schema
```

`/query`：

```text
Query
→ RAG Service
→ LlamaIndex Query Engine / Response Synthesis
→ DeepSeek
→ DKF Answer + Evidence
```

请求支持：

```text
kb_id
question
doc_ids（可选）
top_k（可选）
```

API 层不得依赖或返回 LlamaIndex 内部类型；未来替换框架时不影响前端和 Coordinator Agent。

---

# 31. 阶段 25：Admin 文档管理完善

## 接口

```http
GET    /api/admin/documents/{doc_id}
GET    /api/admin/documents/{doc_id}/status
GET    /api/admin/documents/{doc_id}/blocks
GET    /api/admin/documents/{doc_id}/chunks
POST   /api/admin/documents/{doc_id}/retry
POST   /api/admin/documents/{doc_id}/reindex
POST   /api/admin/documents/{doc_id}/reparse
DELETE /api/admin/documents/{doc_id}
```

Delete 时清理：

```text
Blocks
Chunks
Embeddings
相关缓存
```

Reindex：

```text
保留原文件
→ 重建 Chunk
→ 重建 Embedding
```

Parse Cache 默认复用；只有 `reparse` 才重新调用 MinerU Cloud。

## 完成标准
管理员可完整观察文档从原文件到索引的过程。

---

# 32. 阶段 26：动态增量入库

## 目标

```text
管理员上传
→ UPLOADED
→ Celery
→ Parser Router
→ Native / MinerU Cloud
→ Chunk
→ Embedding
→ READY
→ 自动参与 Query
```

## 完成标准
新增扫描 PDF 后无需重建整个知识库。

---

# 33. 阶段 27：公式语义结构化

## 目标
在 MinerU 的公式视觉解析结果基础上完成中级任务 6 的语义层处理。

输入：

```text
formula block
LaTeX / 公式文本
相邻上下文
Evidence
```

使用 DeepSeek 输出：

```text
formula_name
raw_formula
normalized_formula
variables
variable_meanings
source
```

MinerU 负责视觉 / 版面层公式提取；DeepSeek 负责语义结构化；SymPy 负责后续安全计算。

第一版覆盖：

```text
利润率
毛利率
增长率
同比
环比
占比
加权平均
四则运算
百分比
```

## 完成标准
公式可被转换为稳定的业务表达式，变量含义和公式来源均可追溯。

---

# 34. 阶段 28：公式参数识别与绑定

## 目标
完成任务 6a。

变量记录建议：

```text
name
label
value
unit
source
```

缺参时：

```json
{
  "status": "need_variables",
  "required_variables": ["revenue", "cost"]
}
```

不得猜值。

## 完成标准
公式变量及来源可追溯。

---

# 35. 阶段 29：公式安全计算

## 目标
完成任务 6b。

流程：

```text
Raw Formula
→ 标准化
→ Allowed Symbols Check
→ SymPy
→ Result
```

禁止：

```text
eval()
exec()
```

返回至少：

```text
formula
variables
substitution
result
formula_source
variable_sources
```

## 完成标准
计算过程可解释、可复现。

---

# 36. 阶段 30：Knowledge Agent（LangGraph）

## 目标

在 Retrieval、RAG、Citation 和 Formula Service 已稳定后，使用 **LangGraph** 正式封装 Knowledge Agent。

LangGraph 只负责任务状态、条件分支、节点编排、错误传播和可观察工作流；不得重新实现 Parse / Chunk / Embedding / Retrieval。

LangChain 仅作为辅助集成层，按需使用 Prompt、Tool、Runnable、Message、Model Adapter 等组件；**不单独建立 LangChain Agent Runtime，不再维护第二套 Retriever / Vector Store / RAG Chain。**

## Tool

Knowledge Agent 只调用已经稳定的业务 Tool / Service：

```text
search_knowledge_base
query_knowledge_base
extract_formula
calculate_formula
```

Tool 必须是薄适配层：

```text
LangGraph Node
→ LangChain StructuredTool / 项目 Tool Adapter（按需）
→ Knowledge Search / Query / Formula Service
→ 项目内部统一结果
```

禁止 Tool 内再次直接实现 pgvector SQL、BM25、MinerU HTTP、DeepSeek HTTP 或公式计算逻辑。

## Agent State

建议第一版保持显式、可序列化状态，例如：

```text
question
kb_id
doc_ids
intent
evidence
answer
formula_results
insufficient_evidence
status
error_code
step_count
```

要求：

- `evidence` 继续使用项目已有 Citation / Evidence 契约，不替换成 LangGraph / LangChain 私有对象。
- `kb_id / doc_ids` 必须贯穿 Tool 调用，避免跨知识库污染。
- `step_count` / 最大步数必须配置化，禁止无限 Agent 循环。
- Tool 异常转换为结构化 Agent 状态，不把原始异常堆栈、密钥或敏感参数暴露给前端。

## 第一版 Graph

第一版保持确定、可测试，不追求开放式自主循环：

```text
START
  ↓
validate_input
  ↓
route_request
  ├─ knowledge_search / knowledge_query
  │        ↓
  │   synthesize_or_return
  │
  └─ formula_request
           ↓
      extract_formula
           ↓
      calculate_formula
           ↓
      synthesize_or_return
  ↓
finalize_response
  ↓
END
```

`route_request` 可以使用受控 LLM 结构化输出或显式规则，但必须：

- 输出固定枚举，不允许自由生成节点名。
- 有默认安全分支。
- 不因为模型一次判断失败进入无限重试。
- 对“只需要知识检索”的普通问答，不额外执行公式节点。

## LangChain 边界

允许按需使用：

```text
PromptTemplate / ChatPromptTemplate
StructuredTool / BaseTool
Runnable
Message 类型
LLM / ChatModel Adapter
结构化输出辅助
```

不允许为了“接入 LangChain”而新增：

```text
第二套 VectorStore
第二套 Retriever
第二套 Chunk / Embedding Pipeline
LangChain AgentExecutor 与 LangGraph 双 Agent Runtime
独立的 LangChain RAG Chain 覆盖 LlamaIndex 主链路
```

## 与 LlamaIndex 的职责边界

```text
LangGraph
= Agent State / Workflow / Conditional Routing / Tool Orchestration

LlamaIndex
= Knowledge RAG Retrieval / Fusion / Rerank / Response Synthesis

LangChain
= Prompt / Tool / Runnable / Message / Model Adapter 辅助层

DKF-Agent Service
= Parse / Chunk / Embedding / DB / Evidence / Formula 的业务真相来源
```

LangGraph Node 可以调用 `query_knowledge_base`，而该接口内部可以由 LlamaIndex 组织 RAG；LangGraph 不直接操作 LlamaIndex Node、pgvector 表或 Retriever 细节。

## 统一输出

```json
{
  "status": "success",
  "answer": "...",
  "entities": {},
  "evidence": [],
  "formula_results": [],
  "insufficient_evidence": false
}
```

未来 Coordinator 只依赖这一稳定协议，不依赖 LangGraph / LangChain / LlamaIndex 内部类型。

## 可观察性

至少记录：

```text
agent_run_id
selected_route
tool_name
tool_duration_ms
step_count
final_status
```

日志不得记录 API Key、MinerU Token、签名 URL 或完整敏感请求。

## 自动化测试用例设计（Codex 只编写，用户负责执行）

**执行约束：** Codex 只负责实现测试文件、Mock/Stub 及断言；不执行 `pytest`、任何 `smoke.py`、文档解析或云服务请求。用户测试并反馈结果后再修复。


至少覆盖：

```text
普通知识问答路径
指定 doc_ids 路径
公式识别 + 计算路径
证据不足路径
Tool 异常路径
非法 route 输出安全兜底
最大步数保护
Evidence / Citation 不丢失
LangGraph State 可序列化
LangChain Tool Adapter 不绕过业务 Service
```

Graph 测试用例应优先使用 Stub / Mock Service；仅由用户执行。Codex 不得为了测试 Agent 重复调用真实 MinerU 或大量外部 LLM。

## 完成标准

- Knowledge Agent 使用 LangGraph StateGraph 跑通稳定工作流。
- 普通知识问答、公式任务、证据不足与 Tool 异常都有确定路径。
- LangChain 仅作为辅助适配组件，没有产生第二套 RAG / Agent Runtime。
- Evidence / Citation / Formula 来源保持项目原始可追溯语义。
- 未来 Coordinator 只依赖 Knowledge Agent 的稳定输入输出协议。

---

# 37. 阶段 31：自动化测试用例编写（执行由用户负责）

至少建立：

```text
Parser Test
Chunk Test
Database Test
LlamaIndex Adapter Test
Retrieval Test
Query Fusion Test
Citation Test
Formula Test
LangGraph Knowledge Agent Test
LangChain Tool / Prompt Adapter Test
```

不追求一开始高覆盖率，优先设计关键链路用例；Codex 不自行运行这些用例，人工执行结果作为验收依据。

---

# 38. 阶段 32：RAG 正式评测集（用户执行评测）

至少：

```text
10 条正式单文档 RAG 测试
```

建议：

```text
30~50 条
```

覆盖：

```text
直接事实
数字问题
跨段落问题
专有名词
易混淆信息
证据不足
```

每条记录：

```text
question
expected_answer
expected_doc
expected_page
actual_answer
retrieved_chunks
```

---

# 39. 阶段 33：Retrieval 评测

指标：

```text
Recall@5
Recall@10
MRR（可选）
```

对比：

```text
Dense Only
BM25 Only
Hybrid
Hybrid + Reranker
```

目标是证明方案有效，而不是盲目堆算法。

---

# 40. 阶段 34：RAG 质量评测

指标：

```text
Answer Accuracy
Faithfulness
Citation Accuracy
```

重点：

```text
答案是否被 Evidence 支持
数字是否正确
引用页码是否正确
```

开发目标：

```text
Faithfulness >= 90%
```

---

# 41. 阶段 35：Document Parsing / MinerU / Formula 评测

Document Parsing / MinerU：

```text
Plain Text
Table
Formula
Chart
Reading Order
普通扫描 PDF
模糊扫描件
多栏 PDF
混合页 PDF
同页 Native + Image
混合 DOCX
混合 XLSX
PPTX 图文混排
中英文混合
数字 / 百分比
```

格式覆盖至少记录：

```text
TXT / MD / CSV
DOC / DOCX
XLS / XLSX
PPT / PPTX
PDF
JPG / JPEG / PNG
```

至少记录：

```text
MinerU Parse Success Rate
Page Preservation Accuracy
Block Order Accuracy
Table Recognition Accuracy
Formula Recognition Accuracy
Parser Route Decision Accuracy
Native Safe Hit Rate
MinerU Whole Route Rate
Hybrid Bundle Route Rate（若启用）
Critical False-Native Count（应为 0）
Route Fallback Count
Hybrid Visual Recall（若有 Ground Truth）
Hybrid Duplicate Block Rate
MinerU Files Per Source Document
Parse Cache Hit Rate
Visual Cache Hit Rate（若启用 Hybrid Cache）
MinerU API Retry Count
MinerU Rate Limit / 429 Count
```

Formula：

```text
Formula Detection Accuracy
Parameter Binding Accuracy
Calculation Accuracy
```

---

# 42. 阶段 36：性能记录

Query 记录：

```text
query_embedding_ms
dense_retrieval_ms
bm25_ms
fusion_ms
rerank_ms
response_synthesis_ms
llm_ms
total_ms
```

文档入库记录：

```text
router_preflight_ms
parse_ms
visual_extract_ms
visual_bundle_build_ms
hybrid_merge_ms
mineru_submit_ms
mineru_wait_ms
mineru_download_normalize_ms
chunk_ms
embedding_ms
index_ms
mineru_api_submit_count
mineru_api_query_count
mineru_file_submit_count
mineru_page_count
hybrid_visual_candidate_count
hybrid_visual_dedup_count
hybrid_bundle_page_count
parse_cache_hit
visual_cache_hit
```

至少设计两组不同知识库规模进行对比。

---

# 43. 阶段 37：独立 Demo

Demo 1：

```text
10+ READY 文档
→ 用户提问
→ Answer
→ Evidence
→ 文件名 / 页码 / 原文片段
```

Demo 2：

```text
指定单个 Document
→ 单文档 RAG
```

Demo 3：

```text
管理员上传扫描 PDF
→ MinerU Cloud 异步解析
→ READY
→ 新文档参与问答
```

Demo 4：

```text
查询公式
→ 参数识别
→ SymPy 计算
→ 展示公式和参数来源
```

Demo 5：

```text
上传 CSV / XLSX / PPTX / DOC 等多格式文档
→ 完整性优先 Router
→ Native / MinerU Whole /（若启用）Hybrid Visual Bundle
→ READY
→ 跨格式知识库统一问答与 Evidence
```

---

# 44. 阶段 38：Knowledge Service 独立验收（由用户运行）

必须可以独立执行：

```text
启动 PostgreSQL 17 / pgvector
→ 启动 Windows Redis 服务
→ FastAPI
→ Celery CPU Worker
→ Celery GPU Worker
→ 预置知识库 READY
→ Knowledge Query
→ Answer + Evidence
```

验收清单：

```text
[ ] 10+ 多格式知识库文档（含 Office / CSV / PDF / 图片）
[ ] 单文档 RAG
[ ] 全知识库 RAG
[ ] MinerU Cloud 图片 / 扫描 PDF / Office 解析
[ ] LlamaIndex Adapter（不接管 Parse / Chunk / Embedding / DB）
[ ] Dense Retrieval
[ ] BM25 Retriever
[ ] Query Fusion / Hybrid
[ ] BGE Reranker
[ ] DeepSeek Response Synthesis
[ ] Citation
[ ] Parser Router 与混合文档路由
[ ] CSV / XLSX Native Parser
[ ] 动态上传
[ ] Celery
[ ] 公式识别
[ ] 参数识别
[ ] SymPy 计算
[ ] LangGraph Knowledge Agent（状态机 / 条件路由 / Tool 编排）
[ ] LangChain 辅助集成（不建立第二套 RAG / Agent Runtime）
[ ] 10+ 正式 RAG 测试
[ ] 性能数据
[ ] Demo 可运行
```

完成后才进入团队集成。

---

# 45. 团队集成边界

当前模块最终对外暴露：

```text
Knowledge Search API
Knowledge Query API
Formula API
Knowledge Agent
```

未来：

```text
Coordinator Agent（未来）
        │
        │ LangGraph Workflow
        ↓
Knowledge Agent（阶段 30）
        │
        ├─ LangGraph State / Conditional Routing / Tool Orchestration
        │
        ├─ LangChain 辅助组件（按需）
        │   ├─ PromptTemplate
        │   ├─ StructuredTool
        │   └─ Runnable / Message / Model Adapter
        │
        ↓
Knowledge Base Service
        │
        ├─ LlamaIndex RAG Orchestration
        │   ├─ Dense Retriever
        │   ├─ BM25 Retriever
        │   ├─ Query Fusion
        │   ├─ BGE Reranker Adapter
        │   └─ DeepSeek Response Synthesis
        │
        ├─ Formula Service
        │
        └─ PostgreSQL + pgvector + document_chunks
```

职责边界固定为：

```text
LangGraph = Agent State / Workflow / Tool Orchestration 层
LlamaIndex = Knowledge RAG 编排层
LangChain = Prompt / Tool / Runnable / Message / Model Adapter 辅助层
DKF-Agent Service = 解析、切片、Embedding、持久化、Evidence、Formula 真相来源
```

当前阶段不要为了其他 Agent 提前改变底层实现。

---

# 46. Codex 执行规则

Codex 必须遵守：

1. 每次任务前阅读 `AGENTS.md`、`README.md`、`docs/DEVELOPMENT_PLAN_CODEX.md`。
2. 用户指定当前阶段后，只完成该阶段。
3. 不提前实现下一阶段。
4. 修改前先检查已有代码。
5. 关键代码保留适量中文注释。
6. API Key、密码等不得进入源码。
7. 每次完成功能说明：
   - 修改内容；
   - 修改文件；
   - 人工测试方法；
   - 尚未验证部分。
8. 不自动 `git push`。
9. 除非明确要求，不自动创建 Git Commit。
10. 代码有变化时给出中文 Commit Message。
11. 每次回答最后给出“下一步建议”。
12. 每个阶段完成后停止，等待人工测试。
13. 若用户手动测试反馈失败，则按反馈修复，不得自行重测，不进入下一阶段。
14. 外部 API 接入后必须给用户人工验证鉴权、最小调用、返回格式、业务结果、异常处理的步骤；Codex 不得自行调用付费/联网服务。
15. 未实际测试过的能力不得声称已验证成功。
16. MinerU 实现必须以官方 API 文档 `https://mineru.net/apiManage/docs` 为准，不得猜测接口、参数、状态或错误码。
17. 当前 MinerU 平台频控：提交任务共用 50 个文件/分钟；结果查询共用 1000 次/分钟；单用户 5000 个文件/天。代码必须配置化并使用 Redis 做多 Worker 共享保护。
18. MinerU 的 50 个文件/分钟按“文件数”统计，不得错误地只按 HTTP 请求次数统计。
19. 不实现 MinerU Token 到期时间跟踪、自动续期或刷新逻辑；仅保证 Token 安全读取和错误可解释。
20. Reindex 必须优先复用 Parse Cache；重新调用 MinerU 必须使用明确的 Reparse 语义。
21. 当前不部署 MinerU 本地模型，不新增 MinerU 本地推理依赖。
22. 阶段 1～10 已完成并作为冻结基线。阶段 11A 及之后若因新增格式需要修改上传白名单、统一 FileType、格式校验等前置公共代码，属于兼容性扩展，不视为“重做旧阶段”；但必须先说明原因、维护阶段 1～10 回归用例，并由用户运行确认通过。
23. 当前只实施阶段 11A；未完成并人工验收前不得进入阶段 11B Router。
24. 新增格式支持必须区分“能上传”“能解析”“能完整解析视觉内容”三个层次，不得因为提取到部分文本就宣称复杂文档完整支持。
25. DOC/XLS/PPT 旧 Office 第一版统一交 MinerU，不为其新增自研二进制 Native Parser；CSV 不默认发送 MinerU。
26. 阶段 11B 的 Router 必须遵循“完整性优先”：只有能合理证明 Native 完整覆盖主要信息时才走 Native；复杂 / 不确定内容不得因为 Native 能抽到部分文本就判成功。
27. 阶段 11B.1 第一版以整份文档单 Provider 路由为主，不提前实现逐页 / 逐元素 Native + MinerU 自动合并。
28. MinerU 失败不得静默回退到已知不完整的 Native 结果；Native 明确触发 OCR / 完整性错误时，`auto` 最多允许一次 Native → MinerU Whole 安全 fallback，并记录原因。
29. XLSX 保留 Native 的 Sheet / Cell / 公式原式优势；复杂 XLSX 是否 Whole-MinerU、Review 或进入 11B.2 Hybrid 必须由真实 XLSX MinerU 对照测试决定，不能从 XLS 结果类推。
30. 阶段 11B.2 若启用 Hybrid，禁止按图片 / 图表 / 公式逐元素创建 MinerU 文件任务；同一源文档的视觉候选必须优先聚合成单个 Visual Bundle，一次提交，再通过本地 manifest 回插。
31. Visual Bundle 只降低文件任务数，不等于不消耗页面额度；必须同时保护 bundle 页数、文件大小、MinerU 200 页限制和平台高优先级页额度。
32. Hybrid Merge 必须保留 Native 的确定性结构，不得用 MinerU 计算结果覆盖 XLSX 公式原式；临时 bundle_page 不得作为原文档业务页码。
33. Hybrid 失败不得逐元素补发 MinerU，也不得自动再提交 Whole-MinerU 造成不可控额度翻倍；失败后由显式重试 / 管理员决策处理。
34. PDF 与 PPT/PPTX 第一版继续 Whole-MinerU，不因 Hybrid 能力存在就提前做逐页 / 逐元素拼接；DOCX Hybrid 只在 XLSX Hybrid 验证稳定后再评估。
35. Markdown 图片引用在单文件上传下属于附件完整性问题，不自动抓取任意远程 URL，也不把未上传图片引用误判为完整解析。
36. LlamaIndex 计划从阶段 15A 起接入；当前阶段 11A 不安装、不实现、不提前改造 Retrieval。
37. LlamaIndex 只能作为 RAG 编排层，不得重新接管 Native/MinerU Parse、Parse Cache、Chunk、BGE-M3 文档 Embedding 或 PostgreSQL 业务真相表。
38. 生产主链路不得直接使用 `SimpleDirectoryReader`、`VectorStoreIndex.from_documents()` 或 `IngestionPipeline` 对原始知识库文件再次 Parse / Chunk / Embedding。
39. `document_chunks` 与 PostgreSQL + pgvector 保持唯一业务真相来源；LlamaIndex Node ID 使用稳定 `chunk_id`，metadata 必须保留 `kb_id / doc_id / page / section`。
40. 第一版优先使用自定义 Retriever 包装现有 SQLAlchemy / pgvector，不为了框架便利新增第二套 PGVectorStore 表。
41. DeepSeek、BGE-M3、bge-reranker-v2-m3 继续由项目 Service 管理；LlamaIndex 如需模型接口，只增加 Adapter，不重复加载或散落外部调用。
42. LangGraph 与 LlamaIndex 不互相替代：LangGraph 用于 Agent State / Workflow / Tool Orchestration，LlamaIndex 用于 Knowledge RAG。
43. LangChain 只作为辅助集成层，按需提供 Prompt / Tool / Runnable / Message / Model Adapter；不得为了框架统一再建立第二套 RAG、Vector Store、Retriever 或 Agent Runtime。
44. 阶段 30 才正式接入 LangGraph Knowledge Agent；在 Retrieval / RAG / Citation / Formula Service 稳定前不得提前实现 Agent 工作流。
45. Knowledge Agent 的 Tool 必须调用已稳定的 Knowledge Search / Query / Formula Service，不得在 Tool 内直接复制 pgvector SQL、BM25、MinerU HTTP、DeepSeek HTTP 或 SymPy 业务逻辑。
46. 第一版 LangGraph 必须使用有限状态、明确条件边和最大步数保护；禁止开放式无限循环 Agent。
47. LangGraph / LangChain / LlamaIndex 内部对象不得泄漏到对外 API；Coordinator 只依赖稳定业务协议。
48. Agent 日志只记录 route / tool / duration / status 等可观察信息，不记录 API Key、Token、签名 URL 或敏感完整请求。
49. 最高优先级遵循本文第 0 节：**Codex 默认不执行任何文档解析测试、样本生成/渲染、pytest、smoke、全量回归、性能评测或真实 MinerU/OCR/LLM 调用。**
50. 阶段里的“自动测试”“Mock 覆盖”“真实样本验收”仅要求提供可执行的测试代码、人工说明和预期结果，**不授权 Codex 实际运行**。
51. Codex 默认最多做本轮改动文件的低成本静态检查；如需运行测试、使用额外资源或产生云额度，必须经用户另行明确授权范围和预算。
52. 每轮代码实现后立刻停止并报告“待人工验收”；不得自行生成大量测试资料、循环修复-重测或自动推进下一阶段。


---


# 46.1 当前工程基线（沿用 V0.8 历史记录；V0.9 不更新阶段状态）

```text
阶段 1～9：已完成并回归通过
阶段 10：MinerU Cloud Provider 已完成真实 API 人工验收
阶段 11A：当前开发阶段
阶段 11B.1：等待 11A 和多格式真实样本验收后开始；先做 Whole-document Conservative Router
阶段 11B.2：仅在真实对照证明必要时，评估 Selective Hybrid Visual Bundle
阶段 12～15：未开始
阶段 15A：计划中的 LlamaIndex Adapter，等待 pgvector 基线完成后开始
阶段 16～29：未开始
阶段 30：计划正式接入 LangGraph Knowledge Agent；LangChain 仅按需作为辅助集成层
```

当前已确认解析基线：

```text
Native：TXT / Markdown / DOCX / 文本型 PDF
MinerU：图片 / 扫描 PDF / 表格 PDF / 公式 PDF
```

阶段 11A 扩展目标：

```text
Native 新增：CSV / XLSX
MinerU 新增显式支持：DOC / DOCX / PPT / PPTX / XLS / XLSX
```

阶段 11A 工程实现已扩展以上能力：统一十三种 FileType，上传和 MinerU 共用 OOXML/OLE 校验；CSV/XLSX 原生表格与 Office 云转换输出统一 Block。Office 本地页数保持未知、云页为逻辑页、bbox 为空。

当前真实样本验收已形成以下基线：

```text
CSV Native：UTF-8 / UTF-8 BOM / GB18030 正向样本通过；非法编码负向 fixture 需使用真正无法被 UTF-8 / GB18030 严格解码的无 BOM 字节重新验收。
XLSX Native：多 Sheet、空 Sheet、中文、数字、百分比、公式原式通过；视觉图片 / 图表不属于 Native 完整覆盖范围。
DOC / DOCX MinerU：云链路成功，正文 / 表格 / 图片 OCR 基本可用；存在公式字符、Block 边界、Reading Order 等供应商质量边界。
PPT / PPTX MinerU：云链路成功，文本 / 表格 / 图片 / 图表内容基本可用；顶部标题等 Reading Order 仍可能不稳定。
XLS MinerU：云链路成功，但复杂样本出现公式原式退化为计算结果、内嵌图片 OCR 缺失，不能视为完整语义解析。
XLSX MinerU：必须单独真实测试，不能用 XLS 结果推断。
```

当前对 Hybrid 的计划边界：

```text
11B.1：simple → Native；complex → MinerU Whole / Review
11B.2：只有真实 XLSX 对照证明两路互补时，才做 Native Primary + Visual Bundle + Anchor Merge
同一源文档多个视觉元素 → 默认最多 1 个 MinerU Bundle 文件任务，而不是 N 个元素 N 次提交
```

因此阶段 11B 已明确分两步：11B.1 先做“完整性优先”的 Whole-document Conservative Router；11B.2 仅在单一 Provider 确实无法完整覆盖、且 Native 与 MinerU 可稳定互补时，才启用 Selective Hybrid Visual Enhancement。Hybrid 以 XLSX 为首选候选，同一源文档的多个视觉元素通过单个 Visual Bundle 一次 MinerU 提交，禁止逐元素消耗文件额度。11B 尚未开始。

LlamaIndex 仅作为未来 RAG 编排层纳入路线，不改变当前阶段：完成 Parse Cache、Chunk、BGE-M3 和 pgvector 后，在阶段 15A 建立 `DocumentChunk ↔ TextNode / NodeWithScore` 适配边界，再进入 Dense / BM25 / Query Fusion / Reranker / DeepSeek RAG。

LangGraph / LangChain 同样不改变当前阶段：只有在 Knowledge Search / Query API、Citation、Formula Service 已稳定后，阶段 30 才引入 LangGraph StateGraph 封装 Knowledge Agent；LangChain 仅在该阶段及其后按需提供 Prompt、StructuredTool、Runnable、Message、Model Adapter，不承担核心 RAG 和 Agent Runtime。

已完成阶段不需要 Codex 重新实现。后续任务以**当前仓库代码 + AGENTS.md + 本开发文档**为事实来源；只有当当前阶段的新需求必须触及公共契约时，才做最小兼容修改并提供完整回归测试清单，由用户负责运行，不由 Codex 自动执行。

---

# 47. 推荐实际开发节奏

```text
用户指定阶段
→ Codex 检查当前代码
→ 给出本阶段简短实施方案
→ 修改代码
→ 只做本轮改动文件的有限静态检查（不运行 pytest / 文档解析 / 云 API）
→ 给出人工测试步骤、预期结果和反馈材料
→ 用户人工测试
→ 用户手动 Commit / Push
→ 进入下一阶段
```

每个阶段的技术完成标准仍是“能运行、能测试、能解释”，但**Codex 代码交付时只能标记待人工验收**；是否运行成功及阶段能否完成由用户测试确认，不以 Codex 自行试跑为依据。11B.2 还必须额外证明 Hybrid 在内容完整性与 MinerU 文件 / 页面消耗之间具有真实收益，否则不保留该复杂度。

LlamaIndex 相关开发同样遵循阶段闸门：未完成阶段 15 的 pgvector 真相表前，不提前进入阶段 15A；阶段 15A 只验证 Adapter，不直接跳到完整 RAG。

LangGraph / LangChain 也遵循阶段闸门：阶段 30 之前不为了“先接框架”改写现有 Service；阶段 30 只在稳定 API / Tool 之上建立有限状态 Agent 工作流，并编写普通问答、公式路径、证据不足、Tool 异常和最大步数保护的自动化用例，由用户手动运行验证。


---

# 48. 外部参考

- MinerU 精准解析 API：`https://mineru.net/apiManage/docs`
- OHR-Bench：`https://github.com/opendatalab/OHR-Bench`
- LlamaIndex Framework：`https://developers.llamaindex.ai/`
- LangGraph / LangChain：实现时以 LangChain 官方当前文档为准。

外部服务能力、限流与支持格式可能变化；实现时以官方当前文档和账号后台为准。
