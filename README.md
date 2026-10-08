# 数知融问智能体（DKF-Agent）

**中文名称：** 数知融问智能体  
**英文名称：** Data-Knowledge Fusion Agent  
**英文简称：** DKF-Agent  
**项目目录名：** `dkf-agent`  
**当前开发模块：** `knowledge-service`（非结构化知识问答）

本文说明开发环境配置、基础服务部署、应用启动及部署成功检查。业务测试、任务测试和自动化评测由独立测试文件维护。

## 项目简介

DKF-Agent 面向“多模态数据驱动的可解释精准问数 / 问答智能体”赛题。

当前阶段优先完成非结构化知识问答模块，建设一个可独立运行的 Knowledge Base Service：

```text
知识库文档
→ 文档解析 / OCR
→ Chunk
→ Embedding
→ pgvector
→ Dense + BM25 Retrieval
→ Reranker
→ DeepSeek RAG
→ Answer + Evidence
```

知识库以**预置文档**为主要使用方式，同时提供管理员侧的动态上传、异步解析、增量索引、删除和重新索引能力。

## 技术栈

| 模块 | 技术 |
|---|---|
| Python | Python 3.11 |
| Backend | FastAPI |
| Database | Windows 原生 PostgreSQL 17 + pgvector |
| Async Task | Celery + Redis |
| LLM | DeepSeek API |
| Embedding | BAAI/bge-m3 |
| Reranker | BAAI/bge-reranker-v2-m3 |
| Retrieval | Dense Retrieval + BM25 |
| PDF | PyMuPDF |
| DOCX | python-docx |
| OCR | 火山引擎 OCR，预留百度 OCR |
| Formula | SymPy |
| Agent | LangGraph + LangChain |
| Deploy | 当前 Windows 手动部署；Docker Compose 保留为未来整体部署方案 |

## 开发环境

当前 Windows 开发环境使用原生 PostgreSQL 17、原生编译的 pgvector 和 WSL2 Ubuntu 中的 Redis，不使用 Docker / Docker Compose 部署 PostgreSQL 或 Redis。基础服务由开发者手动安装和管理，应用通过环境变量连接现有服务。

### 1. 创建 Conda 环境

```bash
conda env create -f environment.yml
conda activate dkf-agent
```

GPU 版 PyTorch 建议根据本机 CUDA 环境单独安装，避免在环境文件中固定 CUDA 版本。

### 2. 配置环境变量

复制环境变量模板：

```powershell
# 仅在尚无 .env 时复制，已有配置应手动合并，保留本地密钥。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

至少需要配置：

```text
APP_ENV
APP_HOST
APP_PORT
DATABASE_URL
REDIS_URL
DEEPSEEK_API_KEY
OCR_PROVIDER
VOLCENGINE_ACCESS_KEY
VOLCENGINE_SECRET_KEY
```

Celery 阶段还需按“异步任务”分组配置 `CELERY_BROKER_URL` 和 `CELERY_RESULT_BACKEND`；参见下方启动与验收步骤。

在本地 `.env` 中按“数据库与缓存”分组配置连接地址（密码替换为实际本地密码；URL 中的特殊字符需进行百分号编码）：

```dotenv
DATABASE_URL=postgresql+psycopg://dkf_user:your_password@127.0.0.1:5432/dkf_agent
REDIS_URL=redis://127.0.0.1:6379/0
```

已有 `.env` 应保留本地密码与密钥，参考 `.env.example` 按分组合并新增配置。模板保留的 `POSTGRES_*` / `REDIS_PORT` 为历史部署元信息，应用只读取连接 URL。模型与 OCR 密钥在对应接入阶段配置。

### 3. Windows 原生 PostgreSQL 17

手动安装 PostgreSQL 17，启动其 Windows 服务，默认监听 `127.0.0.1:5432`。通过 pgAdmin 或 psql，以管理员账号连接后创建项目用户和数据库；已存在时无需重复创建：

```sql
CREATE USER dkf_user WITH PASSWORD 'your_password';
CREATE DATABASE dkf_agent OWNER dkf_user;
```

在 Windows PowerShell 中验证项目账号可连接（若 psql 未加入 PATH，使用 PostgreSQL 17 的 bin 目录下的 psql）：

```powershell
psql -h 127.0.0.1 -p 5432 -U dkf_user -d dkf_agent -c "SELECT 1;"
```

输入本地密码，预期返回 `1`。

### 4. pgvector 原生编译与启用

手动安装 Visual Studio 2022 Build Tools 的 C++ 桌面开发组件，以管理员身份打开 **x64 Native Tools Command Prompt for VS 2022**。必须使用 x64 工具链，不能使用 x86。

下载 [pgvector 官方源码](https://github.com/pgvector/pgvector#installation)，进入源码目录，在该开发者命令提示符中执行（以下为 cmd 语法）：

```cmd
set "PGROOT=C:\Program Files\PostgreSQL\17"
nmake /F Makefile.win
nmake /F Makefile.win install
```

安装后，以有权限创建扩展的管理员账号连接到 **dkf_agent** 数据库，执行一次：

```sql
CREATE EXTENSION vector;
```

在同一个数据库中验证：

```sql
SELECT current_database();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT '[1,2,3]'::vector;
```

预期数据库为 `dkf_agent`，扩展查询返回一行 `vector` 及版本号，类型转换返回 `[1,2,3]`。扩展按数据库启用，仅安装文件并不代表项目库已经启用。

### 5. WSL2 Ubuntu 中的 Redis

使用 WSL2 Ubuntu 部署 Redis，不使用 Windows Redis 5。在 Windows PowerShell 中查看发行版状态并进入 Ubuntu（发行版名称以实际列表为准）：

```powershell
wsl -l -v
wsl -d Ubuntu
```

在 Ubuntu 中手动安装并启动 Redis：

```bash
sudo apt update
sudo apt install redis-server
sudo systemctl start redis-server
redis-cli ping
```

若该 Ubuntu 未启用 systemd，改用 `sudo service redis-server start`。`redis-cli ping` 预期返回 `PONG`。

Windows 应用通过 `redis://127.0.0.1:6379/0` 访问 Redis，依赖 WSL 的本机端口转发，参见 [Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。WSL 停止时 Redis 也会停止，开发期间需保持 Ubuntu 为 `Running`，可保留一个 Ubuntu 终端会话。

在 Windows PowerShell 中验证：

```powershell
wsl -l -v
wsl -d Ubuntu -- redis-cli ping
Test-NetConnection 127.0.0.1 -Port 6379
conda activate dkf-agent
python -c "import redis; r=redis.Redis(host='127.0.0.1', port=6379, db=0); print(r.ping())"
```

预期 Ubuntu 为 `Running`、Redis 返回 `PONG`、`TcpTestSucceeded` 为 `True`、Python 输出 `True`。连接失败时先依次检查 WSL 状态、Redis 服务、6379 监听和 `redis-cli ping`，再判断应用问题。

### 6. 启动当前 API 服务

当前阶段已实现 FastAPI 最小服务，可在项目根目录执行：

```powershell
conda activate dkf-agent
uvicorn app.main:app --reload
```

默认访问地址为 `http://127.0.0.1:8000`。若需供局域网设备访问，可显式指定监听地址：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

当前 `/health` 仅检查服务进程，不验证 PostgreSQL 或 Redis 连接。基础服务连通性需通过前文的部署检查分别确认。

### 7. 当前 API 接口

| 方法 | 路径 | 说明 | 预期结果 |
|---|---|---|---|
| `GET` | `/health` | FastAPI 服务健康检查 | 返回服务状态和 `knowledge-service` 标识 |
| `GET` | `/docs` | Swagger UI | 可交互查看当前 OpenAPI 文档 |
| `GET` | `/openapi.json` | OpenAPI JSON 描述 | 返回接口定义 JSON |
| `POST` | `/tasks/add` | 提交异步加法任务 | 返回 HTTP 202、`task_id`、`submitted` 和队列名 |
| `GET` | `/tasks/{task_id}` | 查询 UUID 对应的任务状态 | 返回状态、成功结果或失败提示 |

健康检查示例：

```bash
curl http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "service": "knowledge-service"
}
```

### 8. Celery 配置与 Worker 启动

Celery 使用 Redis 作为 Broker 与 Result Backend，默认任务进入 `default_queue`，GPU 任务显式选择 `gpu_queue`。两个 Worker 分别只消费自己的队列。

在 `.env` 的“异步任务”分组设置以下配置，两个 URL 必须指向已运行的 WSL Redis（含认证时也应保持一致）：

```dotenv
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2
```

使用同一 Redis 服务的不同逻辑数据库分离消息与结果；若未配置这两项，应用使用 `REDIS_URL`。结果保留 24 小时。安装阶段 3 Python 依赖：

```powershell
conda activate dkf-agent
python -m pip install "celery[redis]"
```

先确认 `wsl -l -v` 中 Ubuntu 为 `Running`，再按前文验证 `PONG` 和 Windows 6379 连通。在项目根目录分别打开终端启动 Windows 开发 Worker：

```powershell
# CPU Worker：默认并发 2，也可设为 3 或 4。
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=threads --concurrency=2 --hostname=cpu@%h -l info
```

```powershell
# GPU Worker：单进程串行执行，避免后续模型被多进程重复加载。
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

当前 GPU Worker 尚未加载模型。Worker 启动完成后，日志应显示对应队列及 `ready`。

Celery 官方[不正式支持 Windows](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows)；上述 `threads` / `solo` 为本地开发验证方式。线程池不保证 Python CPU 密集任务并行加速，正式 CPU Worker 在 Linux/WSL 环境使用默认 prefork 池，规划并发 2～4。不同池的能力见[官方并发说明](https://docs.celeryq.dev/en/stable/userguide/concurrency/index.html)。GPU Worker 保持单进程串行。后续重型任务的超时和幂等须单独验证。

### 9. 部署成功检查

基础服务按前文章节确认：PostgreSQL `SELECT 1` 返回 `1`，项目库中存在 `vector` 扩展，Ubuntu 为 `Running`，Redis 返回 `PONG`，Windows 6379 端口可连接。

FastAPI 启动后，访问 `/health` 应返回 `status: ok`，访问 `http://127.0.0.1:8000/docs` 应能打开 Swagger。

两个 Celery Worker 启动并显示 `ready` 后，在已激活项目环境的终端检查 Worker 连通性与消费队列：

```powershell
python -m celery -A app.core.celery_app:celery_app inspect ping
python -m celery -A app.core.celery_app:celery_app inspect active_queues
```

预期 CPU 和 GPU Worker 均返回 `pong`，CPU Worker 仅消费 `default_queue`，GPU Worker 仅消费 `gpu_queue`。以上检查不提交业务任务，仅确认进程启动、Redis 消息通道和队列配置可用。
