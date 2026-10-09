# 数知融问智能体（DKF-Agent）

DKF-Agent（Data-Knowledge Fusion Agent）的 `knowledge-service` 用于非结构化知识库建设与问答。本文说明安装、配置、启动和接口使用。

当前可使用知识库创建与查询、文档上传、异步任务提交和状态查询。文档解析、OCR 和知识库问答尚不可用；上传成功不代表文档已经可以检索。

## 1. 运行环境

| 组件 | 本地环境 |
|---|---|
| Python | Python 3.11，Conda 环境 `dkf-agent` |
| API | FastAPI，默认 `127.0.0.1:8000` |
| 数据库 | Windows 原生 PostgreSQL 17 + pgvector，`127.0.0.1:5432` |
| 消息与结果存储 | Windows 本地 Redis 服务，`127.0.0.1:6379` |
| 异步执行 | Celery CPU Worker；GPU Worker 按需启动 |

当前 Windows 开发环境不使用 Docker 部署 PostgreSQL 或 Redis。Docker Compose 保留为未来整体部署方案。

## 2. 安装与配置

在项目根目录创建并激活环境：

```powershell
conda env create -f environment.yml
conda activate dkf-agent
```

已有环境需要同步依赖时，使用 `conda env update -n dkf-agent -f environment.yml`。

仅在尚无本地配置时复制模板：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，保留模板分组并填写连接信息：

```dotenv
# 应用运行配置
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000

# 数据库与缓存
DATABASE_URL=postgresql+psycopg://dkf_user:your_password@127.0.0.1:5432/dkf_agent
REDIS_URL=redis://127.0.0.1:6379/0

# 异步任务
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2

# 存储与可选功能
UPLOAD_DIR=data/uploads
MAX_UPLOAD_SIZE_MB=50
```

- 将 `your_password` 替换为本地密码；连接 URL 中的特殊字符需进行百分号编码。真实密码、密钥及 `.env` 不得提交到仓库。
- Redis 三个 URL 应指向同一服务；逻辑数据库 0、1、2 分别用于应用连接、消息和结果。未配置 Celery URL 时使用 `REDIS_URL`。
- `UPLOAD_DIR` 的相对路径以项目根目录为基准，也可使用绝对路径。运行账号需有创建、写入和删除权限；默认目录首次有效上传时创建，并已被 Git 忽略。
- 上传限制默认为 50 MiB，必须为正整数。API 与 CPU Worker 必须使用同一数据库，并能访问同一上传目录。
- 当前功能无需填写 DeepSeek 或 OCR 密钥；模板中的相关变量为后续接入保留。`POSTGRES_*`、`REDIS_PORT` 是历史元信息，应用不读取它们。
- 修改配置后重启 API 和 Worker。使用 Uvicorn 命令启动时，监听地址和端口以命令行参数为准。

## 3. 部署基础服务

### PostgreSQL 17

手动安装 PostgreSQL 17，启动 Windows 服务，默认监听 `127.0.0.1:5432`。通过 pgAdmin 或 psql，以管理员账号创建用户和数据库；已存在时无需重复创建：

```sql
CREATE USER dkf_user WITH PASSWORD 'your_password';
CREATE DATABASE dkf_agent OWNER dkf_user;
```

在 Windows PowerShell 中验证项目账号连接；若 psql 未加入 PATH，使用安装目录中的 psql：

```powershell
psql -h 127.0.0.1 -p 5432 -U dkf_user -d dkf_agent -c "SELECT 1;"
```

输入密码后应返回 `1`。

### pgvector

安装 Visual Studio 2022 Build Tools 的 C++ 桌面开发组件，以管理员身份打开 **x64 Native Tools Command Prompt for VS 2022**，不能使用 x86 工具链。

下载 [pgvector 官方源码](https://github.com/pgvector/pgvector#installation)，进入源码目录，执行以下 cmd 命令：

```cmd
set "PGROOT=C:\Program Files\PostgreSQL\17"
nmake /F Makefile.win
nmake /F Makefile.win install
```

以有权限的管理员账号连接 **dkf_agent** 数据库，启用并验证：

```sql
CREATE EXTENSION vector;
SELECT current_database();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT '[1,2,3]'::vector;
```

扩展已启用时无需重复创建。预期数据库为 `dkf_agent`，扩展查询返回一行 `vector` 和版本号，类型转换返回 `[1,2,3]`。扩展需在项目数据库单独启用。

### Redis（Windows 本地服务）

使用本机已有的 Windows Redis 安装，不通过 WSL 部署。以下命令以当前安装位置和服务名 `Redis` 为例；其他机器请替换为实际路径和服务名。已有服务无需重复安装。

服务使用的配置文件是 `redis.windows-service.conf`，本机监听配置应为：

```conf
bind 127.0.0.1
port 6379
protected-mode yes
```

如果使用 ZIP 发行包且尚未注册服务，在**管理员 PowerShell** 中执行一次；MSI 已注册服务或 `Get-Service Redis` 能找到服务时跳过。参见该 Windows 发行版的[服务安装说明](https://github.com/tporadowski/redis/blob/develop/Windows%20Service%20Documentation.md)。

```powershell
& "D:\Redis\redis-server.exe" --service-install --service-name Redis "D:\Redis\redis.windows-service.conf"
```

设置开机自动启动并启动服务（管理员 PowerShell；已有自动启动且运行中的服务可跳过设置和启动）：

```powershell
Set-Service -Name Redis -StartupType Automatic
Start-Service -Name Redis
Get-Service -Name Redis
```

Redis 作为 Windows 后台服务运行，关闭 PowerShell 窗口不会停止它，不需要保持 Ubuntu 或额外终端运行。修改服务配置后，由开发者手动重启 Redis；同一端口不要同时运行其他 Redis 实例。

在 Windows 验证部署：

```powershell
& "D:\Redis\redis-cli.exe" -h 127.0.0.1 -p 6379 ping
Test-NetConnection 127.0.0.1 -Port 6379
conda activate dkf-agent
python -c "import redis; r=redis.Redis(host='127.0.0.1', port=6379, db=0); print(r.ping())"
```

预期服务状态为 `Running`、Redis 返回 `PONG`、端口检查为 `True`、Python 输出 `True`。连接 URL 仍使用前文的 `127.0.0.1:6379`，三个 Redis URL 无需因部署方式切换而改变；启用认证时需同步配置 URL 和检查命令中的认证信息。

当前已有 Redis 5.0.14.1 属于[第三方 Windows 移植版](https://github.com/tporadowski/redis)，仅沿用作本机开发环境，不作为生产推荐。当前安装的 redis-py 5.3.1 [声明支持 Redis 5](https://github.com/redis/redis-py/blob/v5.3.1/README.md)；更新环境依赖前需重新核对兼容性。新部署或生产环境需另行评估受维护的原生方案，可参考 [Redis 的 Windows 原生部署指南](https://redis.io/tutorials/howtos/how-to-run-redis-on-windows-natively-with-memurai/)。

## 4. 初始化数据库

确认 `.env` 中连接的是项目库，`vector` 已启用，项目用户有建表权限。在项目根目录执行：

```powershell
conda activate dkf-agent
python -m alembic upgrade head
python -m app.db.seed
python -m alembic current
```

预期当前迁移版本为 `0002_document_tasks (head)`，默认知识库“默认知识库”存在。初始化可重复执行，其固定 ID 为 `7f2044ca-2041-42ce-977d-40bf5c79ed40`。

已有数据库更新应用时也需执行迁移，再重启 API 和 Worker。应用启动不会自动迁移或创建默认知识库，也不会自动安装或启用 pgvector。

## 5. 启动服务

启动顺序：**PostgreSQL / Redis → 数据库初始化 → API / CPU Worker**。在项目根目录分别打开终端，并激活 `dkf-agent` 环境。

### API

```powershell
uvicorn app.main:app --reload
```

默认地址：`http://127.0.0.1:8000`；Swagger：`http://127.0.0.1:8000/docs`。

需要局域网访问时使用 `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`。`--reload` 仅用于开发；当前接口未提供管理员鉴权，不应暴露到公网。

### CPU Worker

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=threads --concurrency=2 --hostname=cpu@%h -l info
```

并发可设为 2～4。启动日志应显示 `default_queue`、`app.tasks.documents.process_document` 和 `ready`。仅启动 API 不会执行文档任务。

### GPU Worker（可选）

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

当前文档任务只使用 CPU Worker，GPU Worker 尚不加载模型，可暂不启动。

Celery 官方[不正式支持 Windows](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows)。上述 `threads / solo` 用于 Windows 本地开发；正式 CPU Worker 使用 Linux 环境的 prefork 池，GPU Worker 保持单进程串行。

## 6. 接口使用

完整请求参数和响应结构可在 Swagger 查看。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 服务进程健康检查 |
| GET | `/docs` | Swagger UI |
| GET | `/openapi.json` | OpenAPI 接口定义 |
| POST | `/api/admin/knowledge-bases` | 创建知识库，成功返回 201 |
| GET | `/api/admin/knowledge-bases` | 知识库分页列表 |
| GET | `/api/admin/knowledge-bases/{kb_id}` | 知识库详情 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents` | 文档分页列表 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents` | 上传单个文件，成功创建返回 201 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}` | 文档状态、失败原因及任务 ID |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process` | 重新投递任务，无请求体，成功返回 202 |
| POST | `/tasks/add` | 提交加法任务；JSON 参数为整数 `x`、`y` 和可选 `queue`，返回 202 |
| GET | `/tasks/{task_id}` | 查询 Celery 任务状态和结果 |

### 创建与查询知识库

创建接口使用 JSON：

```json
{"name": "项目资料", "description": "项目参考文档"}
```

`name` 必填且唯一，去除首尾空白后为 1～255 字符；`description` 可选。成功返回 `kb_id` 等元信息，状态为 `ACTIVE`。

列表接口接受 `limit`（默认 20，范围 1～100）和 `offset`（默认 0，非负），返回 `items / total / limit / offset`。知识库创建和查询、文档列表和详情仅依赖 PostgreSQL，不依赖 Redis。

### 上传文件

在 Swagger 中选择上传接口，填写已有 `kb_id`，选择文件并提交。请求类型为 `multipart/form-data`，文件字段名为 `file`，不使用 JSON 或 Base64。

支持 PDF、DOCX、TXT、MD、JPG、JPEG、PNG；不接受空文件、含路径或控制字符的文件名，文件名最多 255 字符。相同知识库中内容相同的文件返回 409，错误信息包含已有 `doc_id`，无需再次上传。

响应包含 `doc_id`、`status`、`error_message`、`task_id` 等元信息。**201 表示原件及文档记录创建成功，不代表任务执行成功。** 队列投递失败时文件和记录可能已保留，应先查询文档详情，再决定是否重新投递。

### 查询状态与重新投递

- 使用文档详情接口查看持久化状态及 `error_message`；使用 `task_id` 查询 Celery 执行结果，结果默认保留 24 小时。
- 当前文档任务检查原件后停在 `PARSING`，结果中的 `awaiting_parser` 表示尚未接入解析。Celery `SUCCESS` 不等于文档 `READY`。
- 未知或已过期的任务结果可能显示 `PENDING`，不一定表示排队，文档状态以详情接口为准。
- 修复基础服务或文件问题后，仅 `UPLOADED / FAILED` 文档可通过 `/process` 重新投递；已进入 `PARSING` 或后续状态时返回 409。该接口不是重新索引接口。

常见错误：400 文件名无效或文件为空；404 资源不存在；409 名称、内容重复或状态不允许；413 文件超限；415 格式不支持或标识不匹配；422 参数校验失败；503 基础服务、配置或迁移不可用；507 文件存储不可用。错误提示位于响应的 `detail`，具体模型见 Swagger。

## 7. 部署成功检查与常见问题

基础服务按前文确认：PostgreSQL `SELECT 1` 返回 `1`、项目库中存在 `vector` 扩展、Windows Redis 服务为 `Running`、Redis 返回 `PONG`。

API 启动后打开 `http://127.0.0.1:8000/health`，预期返回：

```json
{"status": "ok", "service": "knowledge-service"}
```

打开 `http://127.0.0.1:8000/docs` 应能看到 Swagger。根路径 `/` 未提供页面，返回 404 属正常现象；`/health` 仅检查 API 进程，不代表数据库或 Redis 可用。

检查 Worker 连通性和队列：

```powershell
python -m celery -A app.core.celery_app:celery_app inspect ping
python -m celery -A app.core.celery_app:celery_app inspect active_queues
```

已启动的 Worker 应返回 `pong`；CPU 消费 `default_queue`，可选 GPU 消费 `gpu_queue`。这些检查不提交业务任务。

Redis 连接失败时，依次检查 Windows Redis 服务状态、服务配置中的绑定地址与端口、6379 是否监听、本地 `redis-cli ping` 是否返回 `PONG`，再核对应用与 Celery 的连接 URL。上传失败或超时后先查询文档列表，避免对已保存的文件反复上传。

数据库和上传目录应一起备份。文件格式检查不等同于恶意文件扫描；对外部署前还需增加鉴权及网关请求体限制。
