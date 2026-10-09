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

文档上传阶段在“存储与可选功能”分组配置：

```dotenv
UPLOAD_DIR=data/uploads
MAX_UPLOAD_SIZE_MB=50
```

相对存储路径固定以项目根目录为基准，也可指定绝对目录；`MAX_UPLOAD_SIZE_MB` 必须为正整数，按 MiB（1024 × 1024 字节）计算。修改 `.env` 后需重启 FastAPI。运行账号需有该目录的创建、写入和删除权限，目录在首次有效上传时自动创建。默认 `data/uploads/` 已被 Git 忽略；自定义仓库内目录时需自行补充忽略规则。

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

当前 API 已提供健康检查、异步任务入口、管理员知识库管理及文档上传接口。先按第 9 节升级数据库迁移，再启动 API 和第 8 节的 CPU Worker，可在项目根目录执行：

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
| `POST` | `/api/admin/knowledge-bases` | 创建知识库 | HTTP 201，返回知识库详情 |
| `GET` | `/api/admin/knowledge-bases` | 分页查询知识库 | HTTP 200，返回 `items`、`total`、`limit`、`offset` |
| `GET` | `/api/admin/knowledge-bases/{kb_id}` | 按 UUID 查询知识库详情 | HTTP 200，返回知识库详情 |
| `GET` | `/api/admin/knowledge-bases/{kb_id}/documents` | 分页查询指定知识库的文档 | HTTP 200，返回文档分页列表 |
| `POST` | `/api/admin/knowledge-bases/{kb_id}/documents` | 单文件上传，提交后投递 CPU 任务 | HTTP 201，返回文档元信息及 `task_id`；投递失败时返回 `FAILED` 文档 |
| `GET` | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}` | 查询文档持久化状态 | HTTP 200，返回当前状态、失败原因及 `task_id` |
| `POST` | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process` | 重新投递待处理或失败文档，无请求体 | HTTP 202，返回新 `task_id` 的文档元信息；投递失败返回 503 |

管理员知识库接口依赖 PostgreSQL 和第 9 节的数据库迁移。知识库创建、列表和文档状态查询不依赖 Redis；文档上传后的任务投递及手动重新投递需要 Redis，任务执行需要消费 `default_queue` 的 CPU Worker。接口参数及响应模型可在 `/docs` 的 `admin-knowledge-bases` 分组查看。

当前管理员接口**尚未提供身份鉴权或 RBAC**，只用于本机或可信内网开发，不应直接暴露到公网。调用方不能指定知识库 ID、状态或时间。创建请求为 JSON，例如：

```json
{"name": "项目资料", "description": "项目参考文档"}
```

`name` 必填，去除首尾空白后长度为 1～255，且名称唯一；`description` 为可选字符串或 `null`，不接受其他字段。创建后的 `status` 为 `ACTIVE`。详情包含 `kb_id`、`name`、`description`、`status`、`created_at`、`updated_at`，UUID 和时间在 JSON 中均为字符串。

两个列表接口接受 `limit`（默认 20，范围 1～100）和 `offset`（默认 0，非负），按创建时间、UUID 倒序返回。文档列表项及详情包含 `doc_id`、`kb_id`、`file_name`、`file_type`、`file_hash`、`source_type`、`status`、`error_message`、`task_id`、`created_at`、`updated_at`；不包含本地文件路径、正文或向量。`task_id` 是当前一轮处理的 UUID，迁移前的历史文档可为 `null`。已有知识库没有文档时返回 `items: []`、`total: 0`。

上传接口接收 `multipart/form-data`，必填字段名为 `file`，一次提交一个文件；在 `/docs` 中输入已有 `kb_id` 后可通过文件选择框提交。不使用 JSON 或 Base64。支持 `.pdf`、`.docx`、`.txt`、`.md`、`.jpg`、`.jpeg`、`.png`，扩展名不区分大小写。原始文件名保留在数据库，磁盘使用 `<doc_id><小写扩展名>`，客户端不能指定存储路径。文件名不能包含路径或控制字符，长度最多 255；不接受空文件。

服务分块计算实际文件内容的 SHA256 并检查大小，不依赖客户端声明的大小或 MIME 类型。PDF、PNG、JPEG 检查基本格式标识，DOCX 检查 ZIP 容器的必要成员；这不等同于完整格式验证、正文解析或恶意文件扫描，TXT / MD 的编码留待解析阶段处理。同一知识库内相同内容返回 409，即使文件名不同也视为重复，`detail` 包含 `message`、已有 `doc_id` 与 `kb_id`；不同知识库可各自保存一份。

上传先保存原件并提交 `source_type=uploaded`、`status=UPLOADED` 的 Document，再投递 `process_document(doc_id)` 到 `default_queue`。正常返回 HTTP 201、`task_id`、`error_message=null`；上传响应是投递前快照，最新处理状态以文档详情接口为准。若 Redis 投递失败，已上传的文件和记录仍保留，通常返回 HTTP 201、`status=FAILED` 及安全失败原因；若 Worker 已推进状态，不会回退覆盖。**HTTP 201 只代表原件与 Document 创建成功，不代表异步处理完成。** 重复文件返回 409 且不会重复投递任务。

普通写盘或入库失败会回滚并清理本次文件；400 表示无效文件名或空文件，413 表示超限，415 表示不支持的格式或标识不匹配，507 表示存储不可用，其他数据库故障仍返回 503。缺少 `file` 或非法 UUID 返回 422，知识库不存在返回 404。

#### 文档处理入口与状态查询

当前阶段只建立状态机和任务框架：Worker 检查原件存在、非空且可读取后，将文档推进到 `PARSING`。任务成功结果为 `{"doc_id": "...", "document_status": "PARSING", "status": "awaiting_parser"}`，表示等待后续 Parser 接入；尚未解析、OCR、生成 Block / Chunk / Embedding，也不会标记 `READY`。状态机预留后续 OCR、切片、向量化、索引阶段的合法流转。

阶段 8 已定义独立的 `ParsedDocument / ParsedBlock` 数据契约与 Parser 公共校验入口，供后续格式解析器使用；目前尚未接入具体 Parser，以上任务行为保持不变。该阶段不新增依赖、环境变量、迁移或启动命令，按现有方式启动 FastAPI 和 Worker 即可。结构约定及独立验收步骤见 [Parser 契约说明](PARSER_CONTRACT.md)，README 不包含业务测试代码。

使用上传返回的 `kb_id`、`doc_id` 调用 `GET /api/admin/knowledge-bases/{kb_id}/documents/{doc_id}` 查看数据库中的最新状态与 `error_message`；使用 `task_id` 调用 `GET /tasks/{task_id}` 查询 Redis 中的 Celery 状态。后者成功结果可以是加法任务的整数或文档任务的字典。Celery `SUCCESS` 只表示当前框架步骤完成，不等于文档 `READY`；未知或已过期的任务结果可能为 `PENDING`，不能据此认定任务仍在排队，Document 状态以数据库为准。

恢复 Redis 或修复原件问题后，可对 `UPLOADED` / `FAILED` 文档调用 `POST /api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process`，无请求体。接口重置错误、分配新 `task_id`、提交后投递，成功返回 HTTP 202；投递失败返回 503 并保留文件，可再次查询持久化状态。旧任务标识不能覆盖新一轮状态。已经进入 `PARSING` 或更后续阶段的文档返回 409，不重复投递；该入口不是 Reindex。文档不存在或不属于指定知识库返回 404，非法 UUID 返回 422。

当前没有事务 Outbox 或后台补投器，数据库提交与消息投递并非原子操作：进程在两者之间退出，可能留下 `UPLOADED` 但没有消息的文档，可通过上述接口显式重新投递。仅启动 API 不会扫描或自动处理历史文档。API 与 CPU Worker 必须连接同一项目数据库，且能够访问同一上传目录；相对文件路径以各自项目根目录为基准。

文件系统与数据库不是同一事务：进程崩溃、文件清理失败或数据库提交确认丢失时仍需人工核对文件与 Document。提交结果不确定时服务会保留原件并返回 503，避免误删可能已提交记录的文件。部署时应备份数据库和上传目录；上传大小检查发生于 multipart 接收之后，对外部署还需在网关限制请求体大小，并增加鉴权，当前仍仅用于本机或可信内网。

错误响应：名称重复返回 HTTP 409；知识库不存在返回 404；非法 UUID、创建参数或分页参数返回 422；数据库连接、配置或迁移不可用返回 503。一般业务错误使用 `{"detail": "中文提示"}`，文档重复上传的 `detail` 为前述结构化对象；422 使用 FastAPI 默认的结构化校验错误。数据库失败响应不包含连接串或底层 SQL 异常。

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

当前文档任务仅使用 CPU Worker，GPU Worker 尚未加载模型。阶段 7 部署后需重启已有 CPU Worker，日志的任务列表应包含 `app.tasks.documents.process_document`，并显示 `default_queue` 及 `ready`。

文档任务最多自动重试 3 次（含首次共 4 次），临时数据库或文件访问错误按 5、10、20 秒退避；原件缺失或为空立即失败，不自动重试。失败原因写入 Document 的 `error_message`，重试成功会清除旧错误；重试耗尽将文档标记为 `FAILED`。数据库持续不可用时可能无法持久化错误，需结合 Worker 安全错误日志及 Celery 失败状态定位，不能只依赖 Document。任务使用晚确认与文档行锁，同一任务重复执行不会生成额外数据；重新投递时通过新任务标识阻止过期消息及失败回调覆盖当前状态。后续 Parser / Chunk / Embedding 接入时仍须在实际数据写入处维护唯一约束与幂等，当前框架不代替这些业务保证。

Celery 官方[不正式支持 Windows](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows)；上述 `threads` / `solo` 为本地开发验证方式。线程池不保证 Python CPU 密集任务并行加速，正式 CPU Worker 在 Linux/WSL 环境使用默认 prefork 池，规划并发 2～4。不同池的能力见[官方并发说明](https://docs.celeryq.dev/en/stable/userguide/concurrency/index.html)。GPU Worker 保持单进程串行。后续重型任务的超时和幂等须单独验证。

### 9. 初始化知识库数据库

确认本地 `.env` 的 `DATABASE_URL` 使用项目用户 `dkf_user`，并已在 `dkf_agent` 数据库手动启用 `vector`。项目用户需有建表权限。安装已声明的 Python 数据库依赖后，在项目根目录执行：

```powershell
conda activate dkf-agent
python -m pip install sqlalchemy alembic "psycopg[binary]" pgvector
python -m alembic upgrade head
python -m app.db.seed
```

初始迁移创建 `knowledge_bases`、`documents`、`document_blocks`、`document_chunks` 四张表，以及 Alembic 版本表。阶段 7 的 `0002_document_tasks` 迁移给 `documents` 增加可空且唯一的 `processing_task_id`，对外响应名为 `task_id`；历史记录保留且该字段默认为空。**已有数据库也必须执行 `python -m alembic upgrade head`，再重启 FastAPI 与 CPU Worker。** Chunk 的 Embedding 列仍为 `vector(1024)`。迁移只检查扩展是否存在，不安装或自动启用 pgvector。

默认知识库初始化命令可重复执行，固定 ID 为 `7f2044ca-2041-42ce-977d-40bf5c79ed40`，首次创建名称为“默认知识库”、状态为 `ACTIVE`。FastAPI 启动时不会自动迁移或插入数据。

部署检查：

```powershell
python -m alembic current
```

预期显示 `0002_document_tasks (head)`。通过 pgAdmin 或 psql 连接项目库，确认四张业务表存在、`documents.processing_task_id` 列存在且默认知识库记录唯一。

### 10. 部署成功检查

基础服务按前文章节确认：PostgreSQL `SELECT 1` 返回 `1`，项目库中存在 `vector` 扩展，Ubuntu 为 `Running`，Redis 返回 `PONG`，Windows 6379 端口可连接。

FastAPI 启动后，访问 `/health` 应返回 `status: ok`，访问 `http://127.0.0.1:8000/docs` 应能打开 Swagger。

两个 Celery Worker 启动并显示 `ready` 后，在已激活项目环境的终端检查 Worker 连通性与消费队列：

```powershell
python -m celery -A app.core.celery_app:celery_app inspect ping
python -m celery -A app.core.celery_app:celery_app inspect active_queues
```

预期 CPU 和 GPU Worker 均返回 `pong`，CPU Worker 仅消费 `default_queue`，GPU Worker 仅消费 `gpu_queue`。以上检查不提交业务任务，仅确认进程启动、Redis 消息通道和队列配置可用。
