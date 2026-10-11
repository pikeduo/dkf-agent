# DKF-Agent 非结构化知识问答开发文档（Codex 版 V0.6 · 完整性优先 Parser Router + LlamaIndex RAG 编排）

> 项目名称：数知融问智能体（DKF-Agent）  
> 当前开发范围：`knowledge-service` 非结构化知识问答模块  
> 使用对象：Codex / 开发人员  
> 开发原则：**从零开始，严格按依赖顺序开发；一次只完成一个阶段，人工测试通过后再进入下一阶段。**
>
> 当前进度：**阶段 1～10 已完成并完成人工验收；当前进入阶段 11A：多格式文档支持扩展。**  
> 文档解析策略：**Native Parser + MinerU 云端精准解析 API + 完整性优先 Parser Router**。  
> RAG 编排策略：**DKF-Agent 自主管理 Parse / Chunk / Embedding / PostgreSQL + pgvector，LlamaIndex 仅作为 Retrieval / Fusion / Rerank / Response Synthesis 编排层。**  
> 当前新增目标：先完成 CSV、XLSX、DOC/DOCX、XLS/XLSX、PPT/PPTX 等常见文档格式与混合内容测试；阶段 11B 采用“能证明 Native 完整覆盖才走 Native，否则走 MinerU / 显式人工决策”的保守路由；待 pgvector 基线完成后再接入 LlamaIndex Adapter，不打断当前阶段 11A。  
> MinerU 官方 API 文档：`https://mineru.net/apiManage/docs`。

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
| Formula | SymPy |
| Agent / Workflow | LangGraph / LangChain |
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

## 人工测试

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

## 人工测试

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

## 人工测试
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

## 自动测试

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

阶段 1～10 的现有回归测试必须全部继续通过。

## 人工测试

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

# 17B. 阶段 11B：完整性优先 Parser Router 与混合文档路由

## 目标

阶段 11B 不再把 Router 定义为“Native 能解析就优先 Native，失败再回退 MinerU”，也不采用“所有文件全部交给 MinerU”的简单策略。

Router 的核心判断是：

```text
这个文件能不能被当前 Native Parser 完整覆盖其主要信息？
```

只有在 **Preflight Inspector 能合理证明 Native 不会静默漏掉主要信息** 时，`auto` 才选择 Native；只要存在重要视觉内容、复杂版式、当前 Native 明确不覆盖的结构，或无法可靠判断完整性，就应选择 MinerU Cloud，或者明确要求管理员人工指定解析模式。

核心原则：

```text
能证明完整覆盖
→ Native

不能证明完整覆盖 / 存在显著视觉内容 / 判断不确定
→ MinerU Cloud 或明确人工决策
```

这里判断的是“完整性”，不是“能否抽出一些文字”。禁止把“Native 没报错”等同于“文档已完整解析”。

## 为什么不把所有文件都交给 MinerU

阶段 10 与阶段 11A 的真实样本已经说明，MinerU 很适合 OCR、复杂版式、图片、表格、公式与 Office 视觉内容，但供应商解析也可能带来模型误差和结构损失；Native 对结构明确的原生数据仍有确定性优势。

当前真实测试形成以下设计依据：

```text
Native TXT / CSV
→ 文本确定性高、无云调用、低延迟

Native XLSX
→ 能精确保留 Sheet、单元格值和公式原式

MinerU DOC / DOCX / PPT / PPTX
→ 能补充图片 OCR、复杂视觉内容
→ 但 Reading Order、公式字符、Block 边界可能存在模型波动

MinerU XLS（当前复杂样本）
→ 云端链路成功
→ 但出现公式原式退化为计算结果、内嵌图片 OCR 缺失
```

因此阶段 11B 的目标不是“尽可能少用 MinerU”，也不是“全部使用 MinerU”，而是：

```text
确定性结构 → Native
复杂视觉结构 → MinerU
两者均不能保证完整性的场景 → 明确暴露限制，再决定是否进入受控 Hybrid
```

不得为了简化实现而牺牲 RAG 的证据完整性。

## 总体流程

```text
Upload / Existing Document
        ↓
Format Validation
        ↓
Preflight Inspector
        ↓
Route Decision
        ├─ Native
        ├─ MinerU Cloud
        └─ Explicit Review / Unsupported Complete Auto Route
        ↓
Parser / Provider
        ↓
Completeness Guard
        ↓
DocumentBlock
        ↓
CHUNKING
```

第一版 Router 仍以 **整份文档选择单一主解析路径** 为主，不做逐页 / 逐元素 Native + MinerU 自动合并。

## `parse_mode`

继续保留：

```text
parse_mode = auto | native | mineru
```

语义：

```text
auto
→ 使用 Preflight Inspector 和 Router 决策

native
→ 管理员显式要求 Native，用于基线对比和排障
→ 仍必须明确暴露 Native 不支持的视觉内容，不得把部分结果声明为完整解析

mineru
→ 管理员显式要求 MinerU
→ 仍遵守文件格式、配额、幂等、安全和真实 API 限制
```

显式覆盖主要用于人工验收、A/B 对比与故障排查，不代表可以绕过格式安全校验。

## Route Decision 必须可解释

Router 不能只返回一个布尔值。至少形成结构化决策信息，例如：

```json
{
  "decision_version": "router-v1",
  "parse_mode": "auto",
  "file_type": "docx",
  "selected_provider": "mineru",
  "reason_codes": [
    "DOCX_HAS_MEDIA",
    "DOCX_HAS_OMATH"
  ],
  "signals": {
    "has_native_text": true,
    "media_count": 2,
    "has_formula": true
  }
}
```

要求：

- `reason_codes` 必须稳定、可测试，不使用自由文本替代全部逻辑。
- `signals` 只保存结构判断需要的信息，不保存大段原文、Token、签名 URL 等敏感内容。
- 不伪造“置信度”；第一版不需要给 Router 人为生成 0～1 confidence。
- Route Decision 至少应进入结构化日志 / 任务结果；若实现时确认后续 Admin 与评测需要持久化，再设计最小数据库字段或独立表，不提前堆复杂 Schema。

## 第一版总体路由矩阵

| 格式 | `auto` 默认策略 | 说明 |
|---|---|---|
| TXT | Native | 确定性文本 |
| CSV | Native | 确定性表格文本 |
| Markdown / MD | Native；发现未提供的图片附件时不得宣称完整 | Markdown 单文件本身无法让 MinerU恢复未上传的本地图片 |
| JPG / JPEG / PNG | MinerU | OCR / 视觉解析 |
| DOC | MinerU | 旧版 Word，不开发 Native 二进制正文 Parser |
| DOCX 简单正文 / 原生表格 | Native | 仅在 Preflight 未发现 Native 未覆盖结构时 |
| DOCX 含图片 / 公式 / 文本框 / 图表 / 嵌入对象等 | 整份 MinerU | 第一版不做元素级合并 |
| PPT / PPTX | MinerU | 视觉布局为主，Native 第一版不承担 |
| XLS | MinerU，但保留已知质量边界 | 兼容路径不等于保证公式和图片语义完整 |
| XLSX 简单单元格 / 公式 | Native | 优先保留 Sheet、Cell 与公式原式 |
| XLSX 含图片 / 图表 / 绘图等 | **先以真实 XLSX MinerU 对照测试决定最终策略** | 若单一 Provider 都无法完整覆盖，进入受控 Hybrid 评估，不静默宣称完整 |
| PDF 简单原生文本 | Native | 必须通过页面级 Preflight |
| PDF 扫描 / 混合 / 复杂版式 / 重要视觉内容 | 整份 MinerU | 第一版不做逐页混合 |

## Markdown 特殊边界

Markdown 与 DOCX / PDF 不同。下面的内容：

```markdown
![销售图](images/sales.png)
```

只是图片引用，不代表图片字节包含在 `.md` 文件里。

当前单文件上传模式下：

- 无图片引用的 Markdown → Native。
- Markdown 引用了本地附件，但附件没有随同上传 → 不得声称文档完整解析。
- 不因为存在 Markdown 图片引用就直接把 `.md` 发送 MinerU；MinerU 无法凭空恢复未上传附件。
- 第一版不自动抓取任意远程图片 URL，避免 SSRF、权限和内容漂移问题。
- 后续如需要完整 Markdown + Assets 支持，应设计受控 Bundle（例如 ZIP + manifest）或附件关联机制，作为独立功能，不塞进 Router v1。

## DOCX Preflight Inspector

DOCX 是 OOXML ZIP，可以在调用 Parser 前做低成本结构检查。

Native DOCX 当前擅长：

```text
Heading
Paragraph
Native Table
```

只要发现 Native 当前不覆盖、且可能承载主要语义的结构，就应将 `auto` 路由到 MinerU，例如：

```text
word/media/*
w:drawing
w:pict
m:oMath / m:oMathPara
w:txbxContent
charts / diagrams / SmartArt
embedded object / OLE object
header / footer / footnote / endnote 等当前 Native 未覆盖的重要内容
```

要求：

- 不按文件名或测试 Marker 写特例。
- 不因为文档已有大量 Native 正文就忽略图片、公式或文本框。
- 发现显著视觉 / 未覆盖结构时，第一版整份 DOCX 交 MinerU，不做“Native 正文 + MinerU 图片”自动合并。

## PDF Preflight Inspector

不能继续使用简单规则：

```text
有文本层 → Native
无文本层 → MinerU
```

因为同一 PDF 可以同时包含：

```text
原生正文
+
扫描页
+
截图文字
+
复杂表格
+
公式
+
图表
+
多栏版式
```

Preflight 至少按页检查：

```text
原生文本是否存在且足够
图片数量与面积占比
是否存在无文本但有图片 / drawing / annotation 的页面
是否存在明显扫描页
是否存在复杂 drawing / table / formula / 多栏风险信号
是否存在 Native Text + Image 的同页混合
```

路由原则：

- 每页均为简单原生文本、无显著视觉语义 → Native。
- 任一页面存在明显扫描 / 混合 / 复杂视觉内容 → 第一版整份 PDF MinerU。
- 不能因为一个很小的 logo 就无条件 MinerU；图片面积、重复性和页面语义需要形成可测试规则。
- 阈值必须配置化并通过测试集校准，不写只适用于某个测试文件的 magic number。
- 第一版不做 Page 1 Native + Page 2 MinerU + Page 3 Native 的自动拼接。

## XLSX Preflight Inspector

XLSX Native 的确定性优势必须保留：

```text
Sheet 顺序
Cell 值
公式原式
```

Preflight 可以检查 OOXML 包中是否存在：

```text
xl/media/*
xl/drawings/*
xl/charts/*
xl/embeddings/*
VML / shape / picture 等视觉对象
```

路由：

```text
无显著视觉对象
→ Native

存在图片 / 图表 / 绘图等视觉对象
→ 标记 complex_xlsx
```

`complex_xlsx` 在阶段 11B 实现前必须先完成真实 XLSX MinerU 对照验收。不能直接把 XLS 的解析结果等同于 XLSX。

### XLSX 决策分支

如果真实 XLSX MinerU 验证结果证明：

```text
Cell / Formula / Visual
均能满足 RAG 完整性要求
```

则第一版可以：

```text
complex XLSX → 整份 MinerU
```

如果验证结果显示：

```text
Native 保留公式 / Cell 更可靠
MinerU 能补图表 / 图片，但会损失公式原式或其他确定性结构
```

则不能让 `auto` 静默选择任一单一路径并宣称完整，应进入 **阶段 11B.2：受控 XLSX Hybrid Parsing** 评估。

在 Hybrid 被验证前，可以要求管理员显式选择 `native` 或 `mineru`，并在结果中暴露覆盖边界；不要用一个“看起来成功”的 `CHUNKING` 掩盖已知重要信息缺失。

## 阶段 11B.1：Whole-document Router v1

第一版优先完成：

```text
Preflight
→ 可解释 Route Decision
→ 整份 Native 或整份 MinerU
```

不做：

```text
逐页 Native + MinerU 合并
逐元素 OCR 合并
XLSX Cell + MinerU Visual 自动去重
跨 Provider Block 对齐
复杂 Reading Order 重建
```

这样先保证 Router 决策本身正确、可解释、可回归。

## 阶段 11B.2：受控 Hybrid（仅在真实测试证明必要时）

Hybrid 不是 11B 的默认目标，只在单一 Provider 明确无法完整覆盖、且两条路径具有互补价值时才进入。

优先候选是复杂 XLSX：

```text
Native
→ Cell / Sheet / Formula

MinerU
→ Image / Chart / Visual
```

只有满足以下条件才允许实施：

- 已有真实 Ground Truth 对照，证明两路确实互补。
- 可以区分来源并避免重复正文。
- Block ID、section、page 语义和 Citation 不被破坏。
- 失败时不会留下半份混合结果。
- 能明确回答“这段 Evidence 来自 Native 还是 MinerU”。

在满足前，不为了“功能看起来更强”提前做双路合并。

## Fallback 规则

Router 必须避免“悄悄降级”：

- `auto` 经过 Preflight 选择 Native 后，如果 Parser 抛出明确的 `OCRRequiredError` / 内容完整性错误，可以 **最多一次** 改走 MinerU，并记录 fallback reason。
- Preflight 已判断复杂并选择 MinerU 后，如果 MinerU 因鉴权、配额、网络、供应商失败而不可用，不得静默回退 Native 并把部分文本当完整结果；应失败并暴露原因。
- 禁止 Native ↔ MinerU 循环回退。
- Provider 未成功完成前不得持久化半份新 Block。

## Completeness Guard

Parser 成功返回结构不等于完整性通过。

至少检查：

- Block 非空且存在有效正文。
- 页面 / Sheet / Slide 逻辑范围基本合理。
- Preflight 发现的“需要视觉解析”信号不能在 Native 成功后被悄悄忽略。
- Office MinerU 的 `bbox` 仍按阶段 11A 约定保持 `NULL`，不伪造原始坐标。
- 不通过具体 Marker 字符串硬编码完整性判断；Marker 只属于测试 Ground Truth。

复杂文档无法自动证明完整时，应保留明确的已知限制，不将“接口成功”误写为“语义完整”。

## Reading Order 原则

阶段 10 / 11A 已观察到扫描 PDF、PPT/PPTX 等文件存在 Provider Reading Order 与视觉阅读顺序不完全一致的情况。

因此：

- 不用简单 `y` 坐标全局排序覆盖供应商顺序。
- 不跨页面重排。
- 表格、公式等结构块不拆散排序。
- 单栏页面如后续增加安全归一化，必须有独立测试并证明不会破坏双栏、表格、图文混排。
- Reading Order Normalizer 若需要实现，应与 Router 决策解耦，不把复杂重排逻辑塞进 Preflight。

## 混合文档测试矩阵

阶段 11B 开始前 / 实现过程中至少准备：

```text
mixed.docx
- Native 标题 / 正文
- Native 表格
- 图片文字
- 图片公式
- 后续 Native 正文

mixed.xlsx
- 单元格
- 公式原式
- 图表
- 图片 / 截图

mixed_page.pdf
- Page 1 原生文本
- Page 2 扫描
- Page 3 原生文本

mixed_same_page.pdf
- 同页原生文本
- 同页截图 / OCR-only 文字

two_column.pdf
- 明确双栏阅读顺序 Ground Truth

complex_layout.pdf
- 标题 / 多栏 / 表格 / 图片 / 页脚

mixed.pptx
- 文本框
- 表格
- 图表
- 图片文字

markdown_with_image_ref.md
- Markdown 正文
- 本地图片引用但附件未上传
- 验证系统不会把缺附件误判为完整成功
```

每份测试必须记录：

```text
expected_route
expected_reason_codes
expected_required_content
expected_missing_if_native
expected_page / section / slide semantics
```

先用真实结果决定阈值和策略，再固化 Router，不反过来为代码构造测试答案。

## 自动测试

至少覆盖：

```text
TXT / CSV → Native
简单 DOCX → Native
DOCX + media → MinerU
DOCX + formula / drawing → MinerU
PPT / PPTX → MinerU
DOC / XLS → MinerU
简单 XLSX → Native
complex XLSX → 进入已验证的复杂策略 / 明确人工决策
简单 PDF → Native
扫描 PDF → MinerU
混合页 PDF → MinerU
同页 Native + Image PDF → MinerU
Markdown 无图片引用 → Native
Markdown 缺本地图片附件 → 不得宣称完整成功
parse_mode=native 显式覆盖
parse_mode=mineru 显式覆盖
Native → MinerU 单次安全 fallback
MinerU 失败不静默回退 Native
Route Decision reason_codes 稳定
阶段 1～11A 全量回归继续通过
```

## 人工验收

至少验证：

1. 简单 Native 文档不会产生 MinerU 调用。
2. 含视觉内容的 DOCX / PDF 能被 Preflight 识别并整份路由 MinerU。
3. PPT/PPTX 默认 MinerU。
4. 简单 XLSX 保留公式原式与 Sheet 信息。
5. complex XLSX 使用真实 MinerU 结果决定 Whole-MinerU 或 Hybrid 分支，不允许凭猜测完成 Router。
6. Route Decision 能看出为什么选择某 Provider。
7. MinerU 配额 / 网络失败不会导致复杂文件静默降级成部分 Native 内容。
8. 混合文档最终 Block 可追溯，不伪造页码 / bbox。

## 本阶段禁止事项

不要：

```text
把所有文件无脑交 MinerU
把 Native 成功等同于完整解析
按单个测试 Marker 写路由规则
全局 y-sort 修复 Reading Order
未经验证就做 XLSX 双路自动合并
逐页 PDF Native/MinerU 自动拼接
为了 Router 提前进入 Parse Cache / Chunk / Embedding / RAG
```

## 完成标准

- `auto` 基于“完整覆盖能力”而不是“是否能抽到文字”做决策。
- 所有支持格式都有明确、可解释的 Route Decision。
- 简单 Native 文档不额外消耗 MinerU API。
- 扫描 / 复杂 / 混合文档不会因 Native 文本存在而静默漏掉主要视觉内容。
- `parse_mode` 显式覆盖有效且不会绕过安全校验。
- MinerU 失败时不会静默降级为已知不完整的 Native 成功。
- complex XLSX 在真实云测试后明确选择 Whole-MinerU、Hybrid 或人工决策路径。
- 混合 DOCX、XLSX、PDF、PPTX 与 Markdown 附件边界都有真实测试记录。
- Router 决策与 Reading Order 问题分层处理，不用高风险排序逻辑掩盖 Provider 输出问题。


---

# 18. 阶段 12：Parse Cache

## 目标
避免 Reindex、任务重试、重复操作反复消耗 MinerU API。

## Cache Key

至少包含：

```text
file_hash
provider
model_version
language
is_ocr
enable_table
enable_formula
```

解析结果需要本地持久化，例如：

```text
data/parsed/{doc_id}/
├── raw/mineru_result.zip
├── content_list.json
└── full.md
```

实际目录通过配置管理。

## Cache 行为

```text
文件未变化
+ Provider 未变化
+ 模型和解析参数未变化
→ 命中 Parse Cache
→ 不调用 MinerU
→ 复用解析结果
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
正常 Reindex 不增加 MinerU 提交次数；强制 Reparse 才重新消耗云 API。

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

## 自动测试

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

## 目标
建立比赛演示默认知识库。

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

# 36. 阶段 30：Knowledge Agent

## 目标
底层稳定后再封装 Agent。

Tool：

```text
search_knowledge_base
query_knowledge_base
extract_formula
calculate_formula
```

统一输出：

```json
{
  "status": "success",
  "answer": "...",
  "entities": {},
  "evidence": [],
  "formula_results": []
}
```

## 完成标准
未来 Coordinator 只依赖这一稳定协议。

---

# 37. 阶段 31：自动测试

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
```

不追求一开始高覆盖率，优先保证关键链路。

---

# 38. 阶段 32：RAG 正式评测集

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
MinerU Route Rate
Critical False-Native Count（应为 0）
Route Fallback Count
Parse Cache Hit Rate
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
mineru_submit_ms
mineru_wait_ms
mineru_download_normalize_ms
chunk_ms
embedding_ms
index_ms
mineru_api_submit_count
mineru_api_query_count
parse_cache_hit
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
→ Native 或 MinerU 自动路由
→ READY
→ 跨格式知识库统一问答与 Evidence
```

---

# 44. 阶段 38：Knowledge Service 独立验收

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
Coordinator Agent
        │
        │ LangGraph / Workflow
        ↓
Knowledge Agent
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
        └─ PostgreSQL + pgvector + document_chunks
```

职责边界固定为：

```text
LangGraph = Agent / Workflow 层
LlamaIndex = Knowledge RAG 编排层
DKF-Agent Service = 解析、切片、Embedding、持久化、Evidence 真相来源
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
13. 当前阶段测试失败时先修复，不进入下一阶段。
14. 外部 API 接入后必须提醒验证鉴权、最小调用、返回格式、业务结果、异常处理。
15. 未实际测试过的能力不得声称已验证成功。
16. MinerU 实现必须以官方 API 文档 `https://mineru.net/apiManage/docs` 为准，不得猜测接口、参数、状态或错误码。
17. 当前 MinerU 平台频控：提交任务共用 50 个文件/分钟；结果查询共用 1000 次/分钟；单用户 5000 个文件/天。代码必须配置化并使用 Redis 做多 Worker 共享保护。
18. MinerU 的 50 个文件/分钟按“文件数”统计，不得错误地只按 HTTP 请求次数统计。
19. 不实现 MinerU Token 到期时间跟踪、自动续期或刷新逻辑；仅保证 Token 安全读取和错误可解释。
20. Reindex 必须优先复用 Parse Cache；重新调用 MinerU 必须使用明确的 Reparse 语义。
21. 当前不部署 MinerU 本地模型，不新增 MinerU 本地推理依赖。
22. 阶段 1～10 已完成并作为冻结基线。阶段 11A 及之后若因新增格式需要修改上传白名单、统一 FileType、格式校验等前置公共代码，属于兼容性扩展，不视为“重做旧阶段”；但必须先说明原因并保证阶段 1～10 回归测试继续通过。
23. 当前只实施阶段 11A；未完成并人工验收前不得进入阶段 11B Router。
24. 新增格式支持必须区分“能上传”“能解析”“能完整解析视觉内容”三个层次，不得因为提取到部分文本就宣称复杂文档完整支持。
25. DOC/XLS/PPT 旧 Office 第一版统一交 MinerU，不为其新增自研二进制 Native Parser；CSV 不默认发送 MinerU。
26. 阶段 11B 的 Router 必须遵循“完整性优先”：只有能合理证明 Native 完整覆盖主要信息时才走 Native；复杂 / 不确定内容不得因为 Native 能抽到部分文本就判成功。
27. 阶段 11B 第一版以整份文档单 Provider 路由为主，不提前实现逐页 / 逐元素 Native + MinerU 自动合并。
28. MinerU 失败不得静默回退到已知不完整的 Native 结果；Native 明确触发 OCR / 完整性错误时，`auto` 最多允许一次 Native → MinerU 安全 fallback，并记录原因。
29. XLSX 保留 Native 的 Sheet / Cell / 公式原式优势；复杂 XLSX 是否 Whole-MinerU 或 Hybrid 必须由真实 XLSX MinerU 对照测试决定，不能从 XLS 结果类推。
30. Markdown 图片引用在单文件上传下属于附件完整性问题，不自动抓取任意远程 URL，也不把未上传图片引用误判为完整解析。
31. LlamaIndex 计划从阶段 15A 起接入；当前阶段 11A 不安装、不实现、不提前改造 Retrieval。
32. LlamaIndex 只能作为 RAG 编排层，不得重新接管 Native/MinerU Parse、Parse Cache、Chunk、BGE-M3 文档 Embedding 或 PostgreSQL 业务真相表。
33. 生产主链路不得直接使用 `SimpleDirectoryReader`、`VectorStoreIndex.from_documents()` 或 `IngestionPipeline` 对原始知识库文件再次 Parse / Chunk / Embedding。
34. `document_chunks` 与 PostgreSQL + pgvector 保持唯一业务真相来源；LlamaIndex Node ID 使用稳定 `chunk_id`，metadata 必须保留 `kb_id / doc_id / page / section`。
35. 第一版优先使用自定义 Retriever 包装现有 SQLAlchemy / pgvector，不为了框架便利新增第二套 PGVectorStore 表。
36. DeepSeek、BGE-M3、bge-reranker-v2-m3 继续由项目 Service 管理；LlamaIndex 如需模型接口，只增加 Adapter，不重复加载或散落外部调用。
37. LangGraph 与 LlamaIndex 不互相替代：LangGraph 用于 Agent / Workflow，LlamaIndex 用于 Knowledge RAG。


---


# 46.1 当前工程基线（V0.5）

```text
阶段 1～9：已完成并回归通过
阶段 10：MinerU Cloud Provider 已完成真实 API 人工验收
阶段 11A：当前开发阶段
阶段 11B：等待 11A 和多格式真实样本验收后开始
阶段 12～15：未开始
阶段 15A：计划中的 LlamaIndex Adapter，等待 pgvector 基线完成后开始
阶段 16+：未开始
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

因此阶段 11B 已明确采用“完整性优先的保守 Router”，而不是简单 Native-first 或 Everything-to-MinerU；11B 尚未开始。

LlamaIndex 仅作为未来 RAG 编排层纳入路线，不改变当前阶段：完成 Parse Cache、Chunk、BGE-M3 和 pgvector 后，在阶段 15A 建立 `DocumentChunk ↔ TextNode / NodeWithScore` 适配边界，再进入 Dense / BM25 / Query Fusion / Reranker / DeepSeek RAG。

已完成阶段不需要 Codex 重新实现。后续任务以**当前仓库代码 + AGENTS.md + 本开发文档**为事实来源；只有当当前阶段的新需求必须触及公共契约时，才做最小兼容修改并执行完整回归。

---

# 47. 推荐实际开发节奏

```text
用户指定阶段
→ Codex 检查当前代码
→ 给出本阶段简短实施方案
→ 修改代码
→ 执行可执行的检查 / 测试
→ 给出人工测试步骤
→ 用户人工测试
→ 用户手动 Commit / Push
→ 进入下一阶段
```

每个阶段以“能运行、能测试、能解释”为完成标准，不以“代码写完”为完成标准。

LlamaIndex 相关开发同样遵循阶段闸门：未完成阶段 15 的 pgvector 真相表前，不提前进入阶段 15A；阶段 15A 只验证 Adapter，不直接跳到完整 RAG。


---

# 48. 外部参考

- MinerU 精准解析 API：`https://mineru.net/apiManage/docs`
- OHR-Bench：`https://github.com/opendatalab/OHR-Bench`
- LlamaIndex Framework：`https://developers.llamaindex.ai/`

外部服务能力、限流与支持格式可能变化；实现时以官方当前文档和账号后台为准。
