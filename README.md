# 数知融问智能体（DKF-Agent）

面向结构化数据问数与文档知识问答的应用，采用双 FastAPI 服务和 Vue3 工作台：

- **知识服务**：知识库管理、多文件上传、异步解析、向量检索、RAG 问答与引用溯源。
- **问数服务**：自然语言转 SQL、Schema Linking、多轮澄清、只读查询与执行 Trace。
- **前端工作台**：统一展示查询结果、SQL、文档证据、处理状态及人工跨源联动。

保留原有公式提取、安全计算与 Knowledge Agent 的 Python 入口；统一自动跨源 Agent 尚未接入。当前代码的自动测试和真实服务联调仍待完整验收。

## 安装准备

| 环境 | 要求 |
|---|---|
| Python | Conda、Python 3.11 |
| 数据库 | PostgreSQL 17，项目数据库启用 pgvector；使用 Windows 原生服务 |
| 缓存与队列 | 本地 Redis，默认 `127.0.0.1:6379` |
| 前端 | Node.js 22.18+（22.x）或 24+、npm |
| 本地模型 | BAAI/bge-m3、BAAI/bge-reranker-v2-m3 的完整权重，以及匹配的推理环境 |

模型分别准备到 `models/bge-m3`、`models/bge-reranker-v2-m3`，也可在配置中指定其他目录。使用 GPU 时需安装与驱动匹配的 CUDA 版 PyTorch；应用不自动下载模型或回退到 CPU。数据库、Redis 和模型由使用者自行部署。

## 安装与配置

以下 PowerShell 命令在项目根目录执行：

```powershell
conda env create -f environment.yml
conda activate dkf-agent
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path contest_backend/.env)) { Copy-Item contest_backend/.env.example contest_backend/.env }
if (-not (Test-Path frontend/.env)) { Copy-Item frontend/.env.example frontend/.env }
New-Item -ItemType Directory -Force data/contest
if (-not (Test-Path data/contest/registry.json)) { Copy-Item contest_backend/registry.example.json data/contest/registry.json }
```

已有环境可用 `conda env update -n dkf-agent -f environment.yml` 更新。编辑各自的本地配置，保留其他参数默认值：

| 配置位置 | 必填或需要核对的内容 |
|---|---|
| 根目录 `.env` | `DATABASE_URL`、`REDIS_URL`、`CELERY_BROKER_URL`、`CELERY_RESULT_BACKEND` |
| 根目录 `.env` | `EMBEDDING_MODEL_DIR`、`RERANKER_MODEL_DIR` 及对应的 `*_DEVICE` |
| 根目录 `.env` | 文档生成问答使用 `LLM_PROVIDER=deepseek/custom` 及所选提供方的地址、密钥和模型名 |
| 根目录 `.env` | 云解析使用 `MINERU_API_TOKEN`；只进行本地解析时可留空 |
| `contest_backend/.env` | `CONTEST_API_TOKEN`、`CONTEST_DATABASE_URLS`、`CONTEST_LLM_API_BASE_URL`、`CONTEST_LLM_API_KEY`、`CONTEST_LLM_MODEL` |
| `data/contest/registry.json` | 登记真实数据源的 `db_id/sub_db`、连接标识、Schema、表列、别名、外键及业务条件；空目录不会生成演示数据 |
| `frontend/.env` | `FRONTEND_CONTEST_TARGET=http://127.0.0.1:8001`、`FRONTEND_KNOWLEDGE_TARGET=http://127.0.0.1:8000` |

知识服务的连接格式为 `postgresql+psycopg://dkf_user:密码@127.0.0.1:5432/dkf_agent`。问数服务的 `CONTEST_DATABASE_URLS` 是连接标识到 `postgresql://` DSN 的 JSON 映射，标识须与注册表的 `connection_id` 一致，必须使用专用只读账号。

API 与 Worker 应共用数据库及 `UPLOAD_DIR`、`PARSED_DIR`、`EMBEDDINGS_DIR`；两个 API 的日志目录应指向同一本地目录。修改配置后重启相关进程。

真实密钥只放本地配置，不填写到前端环境变量或提交 Git。自动解析可能向 MinerU 上传原件；云解析和模型问答都需事先确认数据外传权限及费用。问数服务使用独立模型配置和授权，不能复用知识问答许可。

## 数据库初始化

首次安装时，由 PostgreSQL 管理员创建用户及数据库；已存在则跳过：

```sql
CREATE USER dkf_user WITH PASSWORD 'your_password';
CREATE DATABASE dkf_agent OWNER dkf_user;
```

安装 pgvector 后，连接 `dkf_agent` 数据库，由有权限的账号启用扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

填写根目录 `.env` 的连接信息后执行：

```powershell
python -m alembic upgrade head
python -m app.db.seed
```

已有数据库升级前先备份并停止 API、Worker。应用启动不会自动迁移数据库或启用扩展。

## 启动服务

先启动 PostgreSQL 和 Redis，再分别打开终端。后端命令均在项目根目录、已激活的 `dkf-agent` 环境中运行。

**知识 API（8000）**

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

**CPU Worker：解析、切片与入库**

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

**GPU Worker：文档向量化**

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

**问数 API（8001，必须单进程）**

```powershell
python -m uvicorn contest_backend.main:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

**Vue3 工作台（5173）**

```powershell
Set-Location frontend
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`；知识服务 Swagger 为 `http://127.0.0.1:8000/docs`。问数前在工作台填写 `CONTEST_API_TOKEN`，会话凭据仅保存在内存，刷新后需重新设置。

开发代理将 `/api/contest/*` 转到 8001 的 `/api/*`，将 `/api/knowledge/*`、`/api/admin/*` 原路径转到 8000。两个健康检查路径为 `/health/contest`、`/health/knowledge`。

生产构建使用 `npm run build`，输出到 `frontend/dist`；部署时需配置同源反向代理。`npm run preview` 只预览静态构建，不提供开发代理。

## 常用接口

知识服务（8000）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 进程存活检查 |
| GET / POST | `/api/admin/knowledge-bases` | 查询、创建知识库 |
| GET / POST | `/api/admin/knowledge-bases/{kb_id}/documents` | 文档列表、上传；multipart 字段 `file` 支持 1～50 份 |
| GET | `/api/admin/documents/{doc_id}/status` | 处理状态、解析质量及 Block/Chunk/向量数量 |
| GET | `/api/admin/documents/{doc_id}/blocks`、`/chunks` | 分页查看原文与切片，使用 `limit/offset` |
| POST | `/api/admin/documents/{doc_id}/retry` | 修复原因后恢复失败任务 |
| POST | `/api/admin/documents/{doc_id}/reindex` | 从成功解析缓存重建切片、向量及索引 |
| POST | `/api/admin/documents/{doc_id}/reparse?force=true` | 强制重新解析，可能再次产生云费用 |
| DELETE | `/api/admin/documents/{doc_id}` | 异步删除文档及其数据 |
| POST | `/api/knowledge/search` | 检索文档证据，不调用生成模型 |
| POST | `/api/knowledge/query` | 生成带引用的文档回答，需明确 `allow_cloud=true` |

检索请求示例：

```json
{
  "kb_id": "替换为真实知识库UUID",
  "question": "需要查询的问题",
  "top_k": 30
}
```

可增加非空 `doc_ids` 限定文档；省略表示当前知识库范围。问答接口使用相同参数，并增加本次云调用许可。

问数服务（8001）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 进程存活检查 |
| GET | `/api/catalog` | 授权数据源、表列与外键目录 |
| POST | `/api/sessions` | 按选定 SQL 范围创建会话 |
| POST | `/api/sessions/{session_id}/consents` | 明确确认外传、费用及预算 |
| POST | `/api/query` | 自然语言问数、澄清回复及真实 SQL 结果 |
| POST | `/api/sessions/{session_id}/reset` | 修改问题或条件前清空旧确认与许可 |
| GET | `/api/traces/{trace_id}?session_id=...` | 查询当前会话的安全执行记录 |

除 `/health` 外，问数 API 需要 `Authorization: Bearer <入口令牌>`；会话操作另需 `X-Session-Token`。入口令牌为空时业务接口关闭，会话和预算随进程重启失效。查询最多返回 500 行，默认不允许云调用，不自动重试收费请求；SQL 修复须另外授权。修改数据源需创建新会话。

## 使用与排障

- 新上传自动经过解析、切片、向量化和入库；仅启动 API 不会完成处理，CPU/GPU Worker 均需就绪。
- 上传成功或 HTTP 202 仅表示接收请求。`READY` 表示入库完成，`PARTIAL_READY` 表示可靠内容已入库但存在缺口，两者都不代表问答质量验收通过。
- 文档失败先查看状态并修复原因，再手动 Retry；不要通过反复上传或 Force Reparse 处理普通故障。云调用超时不等于未计费。
- 数据库或 Redis 不可用时先检查服务、端口和连接配置；模型错误先检查权重目录、设备及 CUDA 环境。健康接口只证明进程响应。
- 业务日志位于 `logs/knowledge-service-YYYY-MM-DD.log`。不要将 `.env`、原件、模型、解析缓存、向量文件或日志提交到仓库。
- 知识接口当前没有用户鉴权，仅限本机或可信内网。当前跨源页面为人工确认联动，不应视为自动跨源问答。
