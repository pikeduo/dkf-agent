# 数知融问智能体（DKF-Agent）

DKF-Agent（Data-Knowledge Fusion Agent）的 `knowledge-service` 用于非结构化知识库建设与问答。本文说明安装、配置、启动和接口使用。

当前提供知识库管理、上传、原生解析与显式 MinerU Cloud 解析入口。TXT、Markdown、CSV、DOCX、XLSX 和文本型 PDF 支持原生解析；DOC/DOCX、PPT/PPTX、XLS/XLSX、图片及扫描/复杂 PDF 可显式提交 MinerU 官方精准解析 API V4（需配置 Token）。PDF/图片主链路已完成人工验收，新增 Office 云解析仍需用真实文件验证。尚无自动 Parser 路由、切片、索引或问答，上传和解析成功不代表可以检索。

开发与验收文档统一放在 `docs/`：[开发计划](docs/DEVELOPMENT_PLAN_CODEX.md)、[Parser 契约](docs/PARSER_CONTRACT.md)、[MinerU Cloud 人工验收](docs/MINERU_CLOUD.md)。本文仅保留部署和使用说明。

## 1. 运行环境

| 组件 | 本地环境 |
|---|---|
| Python | Python 3.11，Conda 环境 `dkf-agent` |
| API | FastAPI，默认 `127.0.0.1:8000` |
| 数据库 | Windows 原生 PostgreSQL 17 + pgvector，`127.0.0.1:5432` |
| 消息与结果存储 | Windows 本地 Redis 服务，`127.0.0.1:6379` |
| 异步执行 | Celery CPU Worker；GPU Worker 按需启动 |
| 复杂文档解析 | MinerU Cloud 精准解析 V4，默认 `vlm`；不下载本地 MinerU 模型 |

当前 Windows 开发环境不使用 Docker 部署 PostgreSQL 或 Redis。Docker Compose 保留为未来整体部署方案。

## 2. 安装与配置

在项目根目录创建并激活环境：

```powershell
conda env create -f environment.yml
conda activate dkf-agent
```

已有环境需要同步依赖时，使用 `conda env update -n dkf-agent -f environment.yml`。

原生解析需要环境文件中的 [python-docx](https://python-docx.readthedocs.io/en/latest/api/document.html)（1.2 及以上）、[PyMuPDF](https://pymupdf.readthedocs.io/en/latest/changes.html#changes-in-version-1-24-3)（1.24.3 及以上）和 [openpyxl](https://openpyxl.readthedocs.io/en/stable/tutorial.html)（3.1.5～3.x）。旧 Office 容器检查使用 [olefile](https://olefile.readthedocs.io/en/latest/Howto.html) 0.47；CSV 使用标准库，无需 LibreOffice。更新依赖后重启 API 和 CPU Worker；只更新 API 不会更新 Worker 中已加载的解析代码。

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
LOG_LEVEL=INFO
LOG_DIR=logs
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5

# 数据库与缓存
DATABASE_URL=postgresql+psycopg://dkf_user:your_password@127.0.0.1:5432/dkf_agent
REDIS_URL=redis://127.0.0.1:6379/0

# 异步任务
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2

# OCR / 外部服务：仅使用云解析时需要 Token
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_API_TOKEN=
MINERU_MODEL_VERSION=vlm
MINERU_LANGUAGE=ch
MINERU_ENABLE_TABLE=true
MINERU_ENABLE_FORMULA=true
MINERU_IS_OCR=true

# 存储与可选功能
UPLOAD_DIR=data/uploads
MAX_UPLOAD_SIZE_MB=50
```

- 将 `your_password` 替换为本地密码；连接 URL 中的特殊字符需进行百分号编码。真实密码、密钥及 `.env` 不得提交到仓库。
- Redis 三个 URL 应指向同一服务；逻辑数据库 0、1、2 分别用于应用连接、消息和结果。未配置 Celery URL 时使用 `REDIS_URL`。
- `UPLOAD_DIR` 的相对路径以项目根目录为基准，也可使用绝对路径。运行账号需有创建、写入和删除权限；默认目录首次有效上传时创建，并已被 Git 忽略。
- 上传限制默认为 50 MiB，必须为正整数。API 与 CPU Worker 必须使用同一数据库，并能访问同一上传目录。
- 原生解析无需 MinerU 或 DeepSeek 密钥。云解析仅需要在本机 `.env` 填写 `MINERU_API_TOKEN`，从 [MinerU API 管理页](https://mineru.net/apiManage/docs) 获取；API 与 Worker 使用同一配置。Token 错误或过期由你手动替换，程序不自动刷新。
- `POSTGRES_*`、`REDIS_PORT`、`OCR_PROVIDER`、`VOLCENGINE_*` 为历史配置，应用不读取；当前不接入火山 / 百度 OCR。
- 修改配置后重启 API 和 Worker。使用 Uvicorn 命令启动时，监听地址和端口以命令行参数为准。

### MinerU 调用保护配置

其余变量已按用途放在 `.env.example`，保留默认值即可开始小样本使用：

| 变量 | 默认值 / 含义 |
|---|---|
| `MINERU_SUBMIT_FILE_LIMIT_PER_MINUTE` | 50，按文件数量而非请求数量计量 |
| `MINERU_RESULT_QUERY_LIMIT_PER_MINUTE` | 1000，批次查询次数 |
| `MINERU_DAILY_FILE_LIMIT` | 5000，应用按滚动 24 小时保守保护 |
| `MINERU_HIGH_PRIORITY_PAGE_LIMIT` | 1000，仅估计优先页消耗，超出不拒绝 |
| `MINERU_UPLOAD_BATCH_MAX_FILES` | 50，Provider 单批最大文件数；管理接口每次上传一份 |
| `MINERU_RATE_SAFETY_RATIO` | 0.9，实际分钟保护为 45 文件 / 900 查询；批量需满足安全额度 |
| `MINERU_QUOTA_NAMESPACE` | `dkf-agent:mineru`，同一账户的所有 Worker 必须共用同一 Redis 与命名空间 |
| `MINERU_REQUEST_TIMEOUT_SECONDS` | 60，单次 HTTP 超时 |
| `MINERU_POLL_INTERVAL_SECONDS` | 15，未完成时下次检查间隔 |
| `MINERU_JOB_TIMEOUT_SECONDS` | 7200，整份任务总等待上限 |
| `MINERU_MAX_RETRIES` | 5，临时错误重试上限 |
| `MINERU_RESULT_MAX_MB` | 256，结果 ZIP 下载上限（MiB） |

这些是本应用保护值，不能代替平台账户的实际额度；其他程序使用同一 Token 的消耗不在本应用计数内。平台可能动态限流，收到 429 会退避；Redis 不可用时不绕过保护调用云端。不要通过更换命名空间绕过额度。

原件会上传到 MinerU 云端，使用前确认文档允许外传、账户额度可用和运行机器可访问官方 API / 对象存储；不需 GPU，也不需 DeepSeek Key。

MinerU 的 API、签名原件上传和结果 ZIP 下载统一使用直连，不读取 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY` 或 Windows 系统代理，不自动回退代理；无需新增 `.env` 配置。HTTPS 证书仍正常校验，但不会读取 `SSL_CERT_FILE / SSL_CERT_DIR` 指定的 CA。此策略只作用于 MinerU 客户端，不修改系统代理，也不能绕过 VPN / TUN 的系统级路由。更新代码后必须重启 CPU Worker。

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

预期当前迁移版本为 `0004_mineru_jobs (head)`，默认知识库“默认知识库”存在。初始化可重复执行，其固定 ID 为 `7f2044ca-2041-42ce-977d-40bf5c79ed40`。

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

并发可设为 2～4。启动日志应显示 `default_queue`、`app.tasks.documents.process_document`、`app.tasks.mineru.submit_mineru_parse`、`app.tasks.mineru.check_mineru_result` 和 `ready`。升级代码与迁移后必须重启 CPU Worker；仅启动 API 不会执行文档任务。

### GPU Worker（可选）

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

当前文档任务只使用 CPU Worker，GPU Worker 尚不加载模型，可暂不启动。

Celery 官方[不正式支持 Windows](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows)。上述 `threads / solo` 用于 Windows 本地开发；正式 CPU Worker 使用 Linux 环境的 prefork 池，GPU Worker 保持单进程串行。

### 运行日志

启动命令不变。API 与 Worker 自动向终端及 `LOG_DIR` 输出 UTF-8 单行 JSON 业务日志；相对目录以项目根目录为基准，默认 `logs/`。文件名为 `api-<进程号>.log`、`worker-<进程号>.log`，不同进程独立写入。Uvicorn 访问日志和 Celery 原有日志仍保留在终端，不自动收入这些业务文件。

`LOG_LEVEL` 默认 `INFO`（可选 `DEBUG / INFO / WARNING / ERROR`）；提升为 `WARNING` 会隐藏正常请求和处理摘要。每个文件达到 `LOG_MAX_BYTES`（默认 10 MiB，至少 1024 字节）后轮转，保留 `LOG_BACKUP_COUNT` 份备份（默认 5，范围 1～100）。这是每进程的大小限制，不是全目录总量限制；重启产生新的进程文件，旧文件需由维护者按需归档或清理，不自动删除历史进程日志。

日志包含接口方法、路由模板、HTTP 状态码、耗时；上传文件名、类型、实际大小及文档标识；任务投递、原生解析与 MinerU 处理步骤、Block 数量、重试及安全错误码。正常处理的 HTTP 响应包含 `X-Request-ID`，编号随新投递的 Celery 消息传递，可用请求编号、文档 ID 或云任务 ID 关联 API 与 Worker；旧消息没有请求编号时仍可按文档/任务 ID 查询。未处理异常的框架兜底 500 响应可能没有此响应头，应查看相应请求日志。

业务日志不记录请求/响应正文、文件正文、Token、密码、连接串、签名链接或上游错误原文；记录的是状态与结果摘要，解析内容仍在数据库。`parse.persisted / mineru.blocks_persisted` 表示事务落库成功，不表示 OCR、公式或阅读顺序质量全部正确。文件名可能包含业务信息，日志目录需限制访问，不能提交仓库。日志写盘失败时会提示并降级到终端，不应据此重新上传已成功的文件。

日志配置改变后重启 API 与所有消费相同队列的 Worker；若保留旧 Worker，部分新任务可能被旧进程消费而没有新增业务日志。查阅默认目录的近期业务日志：

```powershell
Get-ChildItem .\logs\*.log | Sort-Object LastWriteTime -Descending
Get-ChildItem .\logs\*.log* | Select-String -SimpleMatch '你的文档UUID'
```

详细事件语义和人工验收见 [业务日志说明](docs/BUSINESS_LOGGING.md)。

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
| POST | `/api/admin/knowledge-bases/{kb_id}/documents` | 上传一份或多份文件；multipart `file`，单份返回 201，多份返回 200 与逐项报告 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}` | 文档状态、失败原因及任务 ID |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process` | 重新投递任务，无请求体，成功返回 202 |
| POST | `/api/admin/knowledge-bases/{kb_id}/mineru/documents` | 显式上传一份或多份 PDF / 图片 / Office；multipart `file`，单份返回 202，多份返回 200 与逐项报告 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/mineru` | 显式提交已有待处理 / 失败文档，或恢复同代次云任务；无请求体，返回 202 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/parse-jobs` | 最近 20 条云端任务、安全错误及外部状态；无签名 URL 或 Token |
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

在 Swagger 中选择原上传接口，填写已有 `kb_id`，点击 `file` 的“选择文件”，可以一次多选后提交（Windows 文件选择窗口使用 Ctrl/Shift 多选）。请求类型为 `multipart/form-data`，文件字段名仍为 `file`；多份文件通过重复的 `file` 字段传递，不使用 JSON 或 Base64。

支持 PDF、DOC、DOCX、PPT、PPTX、XLS、XLSX、CSV、TXT、MD、JPG、JPEG、PNG；不接受空文件、含路径或控制字符的文件名，文件名最多 255 字符。OOXML 检查对应 ZIP 主部件，旧 Office 检查 OLE 容器和对应主流，不能只改后缀冒充。相同知识库中内容相同的文件返回 409，错误信息包含已有 `doc_id`，无需再次上传。

| 文件 | 普通 `/documents` 的 Native 能力 | 显式 `/mineru/documents` |
|---|---|---|
| TXT / MD / CSV | 文本 / 表格 | 不接受 |
| DOCX / XLSX | 正文与表格 / 工作表单元格 | 可提交，真实 Office 结果需验收 |
| DOC / XLS / PPT / PPTX | 记录明确的需要 MinerU 错误，不自动转云端 | 可提交，真实 Office 结果需验收 |
| 文本型 PDF | 原生文本，物理页码 | 可提交 |
| 扫描/复杂 PDF、JPG/JPEG/PNG | 不执行 OCR，部分内容可能无法原生解析 | 可提交 |

TXT/Markdown/CSV 支持 UTF-8、带 BOM 的 UTF-16/32 和 GB18030；其他编码需先转换为 UTF-8。CSV 使用逗号分隔，支持带引号的逗号/换行、空字段；列数不一致或引号损坏明确失败。DOCX 提取正文段落、标题及表格，不包含页眉、页脚、文本框或图片文字。XLSX 按工作表原序提取单元格，工作表名保存为章节，跳过空表；公式保留原式、不计算，普通百分比格式可读化，复杂自定义显示格式不保证还原。XLSX 不解析图片、图表、SmartArt 或复杂绘图；需要视觉内容时手动选择 MinerU。

CSV/XLSX 的安全上限为 10 万行、1024 列、100 万单元格（XLSX 累计）；XLSX 最多 1000 个工作表，Office ZIP 总解压规模最多 256 MiB。全空或超限文件解析失败。PDF 保留真实页码；TXT、Markdown、CSV、DOCX 和 XLSX 的 Native 页码为逻辑页 1，不等同于 Office 排版或打印页码，bbox 为空。

此上传入口仍只投递原生 Parser，图片、纯扫描及包含无文本扫描页的混合 PDF 会记录需要云端解析的失败原因并保留原件；不会自动回退 MinerU。文本与图片同页时仅提取已有文本，不识别图片内文字。需要复杂文档解析时使用下方显式 MinerU 入口；加密、损坏、空文本文件也会失败。

只选一份时保持原单文件响应，包含 `doc_id`、`status`、`error_message`、`task_id` 等元信息。**201 表示原件及文档记录创建成功，不代表任务执行成功。** 队列投递失败时文件和记录可能已保留，应先查询文档详情，再决定是否重新投递。选择多份时返回下方逐项报告。

### 批量上传

批量上传直接使用原 `/documents` 接口的 `file` 多选控件，**不新增 `/batch` 接口**。普通入口仍只使用 Native；需要云端解析时明确选择原 `/mineru/documents` 接口，同样支持多选，但只接受 PDF、图片与 Office，不接受 TXT/MD/CSV。

每批 1～50 个文件；每份文件独立校验、去重、保存和投递，每个新 `doc_id` 只对应一个原件及独立任务。相同文件名但不同内容允许上传；同库相同内容（包含同批重复）返回已有 `doc_id`，不再创建文件或任务。单文件大小和格式限制沿用前文，部署到内网/服务器时还应在网关限制整个 multipart 请求大小；此处的数量和单文件检查不能阻止框架事先接收较大的请求体。

PowerShell 调用示例（替换知识库 UUID 和实际文件名）：

```powershell
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/<kb_id>/documents" -F "file=@第一份.txt" -F "file=@第二份.md"
```

一次选择两份及以上文件时，正常处理后返回 **HTTP 200**，不代表全部成功。响应包含 `total / accepted_count / failed_count / items`；`items` 按上传顺序排列，从 1 开始的 `index` 可区分同名文件。每项含 `file_name / status_code / doc_id`，Native 成功附 `document`，MinerU 成功附 `parse_job`，错误附安全 `detail`。只选一份时继续返回原 201/202 和单文件对象，校验错误也沿用原 HTTP 状态；已有单文件调用无需改字段或解析方式。

- 项内 `201`：Native 文件已创建并投递；项内 `202`：本地云任务已建立并投递。均不表示解析成功。
- 项内 `409`：重复内容，使用返回的已有 `doc_id` 查询状态；其他校验和服务错误沿用单文件状态码，意外单项错误为 `500`。
- 一个文件失败不会撤销之前成功的文件，后续文件仍继续处理；这不是整批事务。
- 项内失败仍可能已保存文件或云任务。已知时返回 `doc_id`，云投递失败的 `detail` 还含 `job_id`；数据库提交确认丢失时可能无法返回 `doc_id`，必须先查文档列表，不能盲目重新上传。

缺失 `file` 返回整批 422；数量超过 50 返回整批 413；无效知识库返回整批 404；数据库预检或 MinerU 配置不可用返回整批 503。这些预检失败不创建新文件。MinerU 每份文件独立进入已有短任务与共享频控，不将这 50 份文件合并为一个云任务；真实云调用会消耗额度。

更新代码后重启 API，并刷新 `/docs` 以加载新的文件数组定义，必要时 Ctrl+F5 强制刷新；所有相关 CPU Worker 也应使用最新代码。后续按每项的 `doc_id` 查询文档详情或 `parse-jobs`，不要只看批次 HTTP 状态判断解析结果。之前新增的两个 `/batch` 路径已移除。

### 查询状态与重新投递

- 使用文档详情接口查看持久化状态及 `error_message`；使用 `task_id` 查询 Celery 执行结果，结果默认保留 24 小时。
- 原生文本解析成功后保存解析结果，文档停在 `CHUNKING`（等待切片）；任务结果为 `awaiting_chunk`，并包含 `block_count`。当前不会继续切片或索引，Celery `SUCCESS` 不等于文档 `READY`。
- 未知或已过期的任务结果可能显示 `PENDING`，不一定表示排队，文档状态以详情接口为准。
- 修复基础服务或文件问题后，仅 `UPLOADED / FAILED` 文档可通过 `/process` 重新投递；已进入 `PARSING` 或后续状态时返回 409。该接口不是重新索引接口。
- 旧版任务留下的 `PARSING` 记录不会因更新代码自动解析，也不能通过 `/process` 重新投递；升级后的首次使用请上传新文档，不要假定历史记录已处理。

常见错误：400 文件名无效或文件为空；404 资源不存在；409 名称、内容重复或状态不允许；413 文件超限；415 格式不支持或标识不匹配；422 参数校验失败；503 基础服务、配置或迁移不可用；507 文件存储不可用。错误提示位于响应的 `detail`，具体模型见 Swagger。

### 显式使用 MinerU Cloud

在 Swagger 选择 `POST /api/admin/knowledge-bases/{kb_id}/mineru/documents`，上传 PDF、JPG/JPEG/PNG 或 DOC/DOCX/PPT/PPTX/XLS/XLSX，不接受 CSV/TXT/MD。[官方接口](https://mineru.net/apiManage/docs)限制单文件 200 MB / 200 页，应用默认上传上限仍为 50 MiB，以更小者为准。Office 无可靠本地转换页数，200 页限制由云端最终校验，不按 Sheet/Slide 数猜测；优先页观测不计入未知 Office 页数，实际额度以平台为准。202 只表示本地任务已建立并投递，不代表云端已成功。

Office 云解析的 `page` 是 MinerU 转换后的逻辑 `page_idx + 1`，不是原始 Office 物理页码；无法可靠映射原件页面尺寸，所以 bbox 始终为空。PDF 和图片继续分别使用物理页码/点数坐标和像素坐标。

查询 `GET /api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/parse-jobs`：外部状态为 `waiting-file / pending / running / converting / done / failed`；响应还包含 `batch_id / data_id / trace_id / retry_count / error_code / error_message`、活动标记和时间。`task_id` 是本地处理代次，不是整份云任务的 Celery 结果 ID。云任务应以此列表和文档详情为准，不以单个短任务 SUCCESS 或 `/tasks` 的 PENDING 判断解析成功。

成功时任务为 `done`、文档为 `CHUNKING`，仍不能检索。失败时查看安全错误，原件保留。已有 `UPLOADED / FAILED` 的 PDF / 图片 / Office 可通过 `POST .../documents/{doc_id}/mineru` 提交；活动云任务可用该入口恢复投递，重复请求沿用原任务。已 `CHUNKING` 返回 409，不提供 Reparse / Reindex。比较同一 DOCX/XLSX 的 Native 与 MinerU 结果时使用两个独立知识库，不能绕过完成状态限制。

网络重试耗尽的非活动失败任务不会因网络恢复自动继续；对它显式提交会创建新批次并可能消耗额度，不是仅下载旧批次。云端解析结果保留原始文字，页眉/页脚只在顺序证据明确时恢复位置；复杂版式、缺少顺序信息或有歧义时保留供应商原序，不保证修复全部阅读顺序、OCR 或公式名称。更新代码后重启 CPU Worker；已有解析结果不会自动改写。

未配置 Token 返回 503；不支持格式返回 415；云端原件可读性校验失败返回 422（如文件已保存，detail 含 doc_id）；队列投递失败返回 503 与已保存的 doc_id / job_id。先查现有状态再恢复任务，不要重复上传。日额度、鉴权、损坏文件不盲目重试；若提示提交结果不确定，先在 MinerU 平台核对是否已创建批次，不能直接反复申请。

目前没有自动路由、结果缓存、Admin Markdown 预览或后台补投器。更新版本不会自动处理历史记录；使用新知识库 / 新文档验证，或对符合状态条件的已有记录显式提交。管理员接口尚无鉴权，包含会产生云端调用的入口，仅限本机或可信内网。

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

MinerU 上传成功但结果下载失败时，分别检查 API 域名 `mineru.net` 与结果 CDN 域名（当前官方示例为 `cdn-mineru.openxlab.org.cn`）的 HTTPS 连通性；API 可用不代表 ZIP 下载链路可用。当前 MinerU 客户端已禁用自动代理，仍超时时请手动确认 VPN / TUN / 全局路由已关闭或对这些域名设置系统级直连；必要时用其他网络（例如手机热点）对比。不要关闭证书校验或改写签名 URL。更新代码后重启 CPU Worker，再查询现有任务状态，避免重复上传或直接新建云端批次。详细检查及恢复限制见 [MinerU Cloud 验收说明](docs/MINERU_CLOUD.md#结果下载与代理排查)。

数据库和上传目录应一起备份。文件格式检查不等同于恶意文件扫描；对外部署前还需增加鉴权及网关请求体限制。
