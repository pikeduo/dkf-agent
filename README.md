# 数知融问智能体（DKF-Agent）

`knowledge-service` 用于非结构化知识库建设与问答。当前可管理知识库、单选/多选上传文档、异步解析及查询状态，支持 Native、MinerU Cloud 和可选的整份自动路由。

**当前解析成功后停在 CHUNKING（等待切片），尚不能检索或问答。** 管理接口暂无鉴权，仅限本机或可信内网，不应暴露公网。

## 1. 环境与配置

本地环境：Python 3.11、Windows 原生 PostgreSQL 17 + pgvector、Windows Redis 服务、FastAPI、Celery。当前不通过 Docker 或 WSL 部署 PostgreSQL/Redis；Docker Compose 保留为未来整体部署方案。

在项目根目录安装依赖并创建本地配置：

```powershell
conda env create -f environment.yml
conda activate dkf-agent
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

已有环境更新使用 `conda env update -n dkf-agent -f environment.yml`。依赖以 [environment.yml](environment.yml) 为准；更新代码、依赖或配置后重启 API 和 Worker。

编辑 `.env`，以下为主要配置，其他保护阈值保留 [.env.example](.env.example) 默认值：

```dotenv
# 应用运行配置
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
LOG_LEVEL=INFO
LOG_DIR=logs

# 数据库与缓存
DATABASE_URL=postgresql+psycopg://dkf_user:your_password@127.0.0.1:5432/dkf_agent
REDIS_URL=redis://127.0.0.1:6379/0

# 异步任务
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2

# OCR / 外部服务：仅云解析需要 Token
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_API_TOKEN=
MINERU_MODEL_VERSION=vlm

# 存储与可选功能
UPLOAD_DIR=data/uploads
MAX_UPLOAD_SIZE_MB=50
# 可选 XLSX 视觉增强；只有人工对照确认需要后才开启。
HYBRID_XLSX_ENABLED=false
HYBRID_MAX_CANDIDATES=64
HYBRID_MAX_BUNDLE_PAGES=32
HYBRID_MIN_IMAGE_SIDE=32
HYBRID_MAX_IMAGE_PIXELS=16000000
HYBRID_MAX_IMAGE_MB=16
```

- 替换数据库密码，URL 中特殊字符需百分号编码；真实密钥和 `.env` 不提交仓库。
- Redis 逻辑库 0/1/2 分别用于应用、消息、结果；三个 URL 指向同一本地服务，未配置 Celery URL 时使用 REDIS_URL。
- API/Worker 使用同一数据库，并能读写同一原件目录；UPLOAD_DIR 相对项目根目录，默认单文件上限 50 MiB。
- Native 无需外部密钥或 GPU；云解析从 [MinerU API 管理页](https://mineru.net/apiManage/docs) 获取 Token，手动维护有效性。当前不需要 DeepSeek Key 或下载 Embedding/Reranker 模型。
- 云解析会上传原件并消耗账户额度，请先确认文档允许外传。同账户 Worker 共用限流 Redis/命名空间，不通过修改保护值绕过额度；Redis 故障时不调用云端。
- MinerU API、签名上传和 ZIP 下载固定直连，不读取环境/Windows 系统代理、不回退代理；仍校验证书，不能绕过 VPN/TUN 的系统路由。

## 2. 手动部署基础服务

已部署的服务只需检查，无需重复安装。应用不负责安装、启动系统服务或自动创建数据库。

### PostgreSQL 17 与 pgvector

安装并启动 PostgreSQL 17，默认 `127.0.0.1:5432`。以管理员账号通过 pgAdmin/psql 创建用户及数据库（已存在则跳过）：

```sql
CREATE USER dkf_user WITH PASSWORD 'your_password';
CREATE DATABASE dkf_agent OWNER dkf_user;
```

pgvector 使用 VS 2022 Build Tools 的 C++ 桌面组件。以管理员身份打开 **x64 Native Tools Command Prompt**（禁止 x86），进入 [pgvector 官方源码](https://github.com/pgvector/pgvector#installation) 目录，执行：

```cmd
set "PGROOT=C:\Program Files\PostgreSQL\17"
nmake /F Makefile.win
nmake /F Makefile.win install
```

以有权限的账号连接 **dkf_agent** 数据库启用并验证（已启用则跳过创建）：

```sql
CREATE EXTENSION vector;
SELECT current_database();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT '[1,2,3]'::vector;
```

预期数据库为 dkf_agent，扩展返回 vector 及版本号，类型转换成功。验证项目用户连接：

```powershell
psql -h 127.0.0.1 -p 5432 -U dkf_user -d dkf_agent -c "SELECT 1;"
```

应返回 1；psql 不在 PATH 时使用 PostgreSQL 安装目录中的程序。

### Redis（Windows 后台服务）

沿用本机 Redis 5.0.14.1，仅用于开发，不作为生产推荐；升级服务或 Python 客户端前需核对兼容性。不需要开启 WSL 或保持终端窗口。

以安装目录 `D:\Redis`、服务名 `Redis` 为例，请替换为实际值。服务配置 `redis.windows-service.conf` 使用：

```conf
bind 127.0.0.1
port 6379
protected-mode yes
```

ZIP 版本尚未注册服务时，在管理员 PowerShell 执行一次；MSI 已注册或 Get-Service 可找到服务则跳过：

```powershell
& "D:\Redis\redis-server.exe" --service-install --service-name Redis "D:\Redis\redis.windows-service.conf"
```

由开发者在管理员 PowerShell 设置后台自启并启动（已运行则跳过）：

```powershell
Set-Service -Name Redis -StartupType Automatic
Start-Service -Name Redis
Get-Service -Name Redis
& "D:\Redis\redis-cli.exe" -h 127.0.0.1 -p 6379 ping
Test-NetConnection 127.0.0.1 -Port 6379
```

预期 Running、PONG、TcpTestSucceeded=True。同一端口不要运行多个 Redis；启用认证时同步修改连接 URL。安装细节见该 Windows 发行版的[服务说明](https://github.com/tporadowski/redis/blob/develop/Windows%20Service%20Documentation.md)。

## 3. 初始化与启动

启动顺序：**PostgreSQL/Redis → 数据库初始化 → API/CPU Worker**。以下命令均在项目根目录、已激活的 dkf-agent 环境中执行。

初始化/升级数据库：

```powershell
python -m alembic upgrade head
python -m app.db.seed
python -m alembic current
```

预期 head 为 `0004_mineru_jobs`，默认知识库 ID 为 `7f2044ca-2041-42ce-977d-40bf5c79ed40`。seed 可重复执行；应用启动不会自动迁移、启用扩展或创建默认库。

分别打开终端启动 API 和 CPU Worker：

```powershell
uvicorn app.main:app --reload
```

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=threads --concurrency=2 --hostname=cpu@%h -l info
```

默认访问 [Swagger](http://127.0.0.1:8000/docs) 和 [健康接口](http://127.0.0.1:8000/health)。监听地址/端口以 Uvicorn 参数为准；局域网访问加 `--host 0.0.0.0 --port 8000`，`--reload` 仅用于开发。

CPU Worker 应显示 default_queue、process_document、submit_mineru_parse、check_mineru_result 和 ready；仅启动 API 不会执行解析。Windows threads/solo 仅用于开发，正式 Linux CPU 使用 prefork 并发 2～4。

启用 XLSX Hybrid 时，Windows CPU Worker 使用下列串行命令处理 PDF Bundle，替换上面的 threads Worker（不要同时保留旧 Worker 消费该队列）：

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

可选 GPU Worker 当前不加载模型、不处理文档，可暂不启动：

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

### 运行日志

API/Worker 业务日志为 UTF-8 中文可读摘要，共用 `LOG_DIR`，每天一份 `knowledge-service-YYYY-MM-DD.log`，重启继续追加；框架原始日志仍在终端。默认 INFO，记录接口、上传、解析、重试及结果数量，不记录正文或密钥；解析质量仍需对照原件。

调用接口生成日志后，查看当天文件：

```powershell
Get-Content (".\logs\knowledge-service-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd')) -Encoding UTF8 -Tail 50 -Wait
```

日志不会自动清理，请关注磁盘、限制目录访问，不删除运行中的锁文件；修改配置/格式后重启所有进程。详细事件说明见 [业务日志](docs/BUSINESS_LOGGING.md)。

## 4. 接口使用

完整参数、响应模型和错误说明以 Swagger 为准：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health`、`/docs`、`/openapi.json` | 健康、Swagger、接口定义 |
| POST / GET | `/api/admin/knowledge-bases` | 创建 / 分页列出知识库 |
| GET | `/api/admin/knowledge-bases/{kb_id}` | 知识库详情 |
| GET / POST | `/api/admin/knowledge-bases/{kb_id}/documents` | 文档列表 / 单选、多选上传 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}` | 文档状态、错误、任务标识 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process` | 重新投递，可选 Query parse_mode，无请求体 |
| POST | `/api/admin/knowledge-bases/{kb_id}/mineru/documents` | 显式云上传，支持多选 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/mineru` | 已有文档云提交 / 活动云任务恢复，无请求体 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/parse-jobs` | 云任务状态与安全错误 |
| POST | `/tasks/add` | 示例任务，JSON 参数 x、y、可选 queue |
| GET | `/tasks/{task_id}` | Celery 状态与结果 |

创建知识库使用 JSON，name 必填且唯一，description 可选：

```json
{"name": "项目资料", "description": "项目参考文档"}
```

知识库/文档列表支持 limit（默认 20，1～100）和 offset（默认 0）。

### 上传与选择解析方式

支持 PDF、DOC、DOCX、PPT、PPTX、XLS、XLSX、CSV、TXT、MD、JPG、JPEG、PNG；服务端检查真实内容和同库 SHA256 去重，不接受空文件或伪装格式。默认单文件 50 MiB、每次 1～50 份。

Swagger 点击 Try it out，填写 kb_id，打开“选择文件”窗口单选或 Ctrl/Shift 多选；选后显示名称/大小初检，确认再 Execute。只有一个文件控件，不需 Add string item；最终内容校验由服务端负责。

普通上传使用 multipart `file` 和可选 `parse_mode`：

| 模式 | 行为 |
|---|---|
| 省略 / native | 仅 Native：TXT、MD、CSV、DOCX、XLSX、文本型 PDF；不自动产生云费用，Office 视觉内容可能遗漏 |
| auto | 简单文档 Native；复杂 DOCX/PDF、图片、DOC/XLS/PPT/PPTX 整份 MinerU |
| mineru | 强制云解析，仅 PDF、图片及六种 Office；不接受 TXT/MD/CSV |

auto 默认对复杂 XLSX、缺附件的 Markdown 或预检超限要求人工复核。人工对照确认需要后，可设置 `HYBRID_XLSX_ENABLED=true` 并重启 API/Worker：受支持的 XLSX 图片进入单个云端 PDF Bundle，Native 单元格/公式保留；基础 Chart 优先本地结构化，零视觉页时不调用云端。不新增 hybrid 请求参数。未知绘图、外链、复杂 Chart 或超限仍 review；不抓取附件 URL。复核对应 Document=FAILED、错误前缀 ROUTER_REVIEW，不是成功。检测不能保证 OCR/复杂版式语义全部正确。

Hybrid 需有效 MinerU Token、直连和可写原件目录；临时 Bundle/manifest 位于原件目录下 `.hybrid`，应随原件备份，不在运行中删除。图片只按大小/纯色及精确 Hash 过滤、复用；自定义装饰或图表视觉语义仍需人工核对。成功块的 section 为原 Sheet、page=1、bbox 为空，增强块位于对应整 Sheet table 后按 Anchor 排序，不伪造 Excel 打印页或逐单元格交错布局。详见 [Hybrid 使用限制](docs/SELECTIVE_HYBRID.md)。

多份文件重复 file 字段，每份有独立 doc_id；示例替换知识库 UUID 和文件名：

```powershell
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/你的知识库UUID/documents" -F "file=@第一份.txt" -F "file=@第二份.md" -F "parse_mode=auto"
```

单份普通上传返回 201，显式云上传返回 202，均不代表解析成功。多份返回 200 及 total/accepted_count/failed_count/items，**需检查每项 status_code、doc_id 和 detail，不代表全批成功**；单项失败不撤销其他项。重复内容返回 409 和已有 doc_id，无需重传。队列/提交失败可能已保存原件，应先查文档列表再恢复。

Native 文本严格解码 UTF-8、BOM UTF-16/32、GB18030；CSV 支持引号字段、空值，不支持不等宽行。DOCX 提取正文/表格；XLSX 按 Sheet 提取单元格、保留公式不执行，不解析图片/图表。Native Office/文本为逻辑页 1，PDF 为物理页；云 Office 为转换后逻辑页、bbox 为空，不声称原件排版坐标。

MinerU 还受平台 200 MB/200 页限制，应用取更小限制；Office 转换页数由平台最终校验。云失败不自动降回 Native。复杂 XLSX 的 Native/云质量需在两个知识库单独对照，不能直接假定云解析完整。

### 状态查询与恢复

- 文档详情是业务状态依据；云解析另查 parse-jobs。成功为 Document=CHUNKING，云 job=done；Celery SUCCESS 或交接结果 delegated_to_mineru 不代表整份解析成功。
- 保存最初响应的 task_id，可通过 /tasks 查询路由决策及任务结果（默认保留 24 小时）。转云后的文档 task_id 是本地云代次，不是短任务结果 ID；未知/过期结果 PENDING 也不一定表示排队。
- 仅 UPLOADED/FAILED 可通过 /process 重投，可用 `?parse_mode=auto|native|mineru`，省略仍 Native。活动云任务用 /mineru 恢复同一任务；非活动失败文档再云提交可能新建批次并消耗额度。
- 当前不提供 Delete/Reparse/Reindex 或后台自动补投，CHUNKING 等完成状态拒绝重解析；更新代码不会改写旧结果。
- 常见错误：400 空文件/文件名，404 不存在，409 重复/状态冲突，413 超限，415 格式，422 参数/原件校验，503 服务/配置/迁移不可用，507 存储不可用；详情见响应 detail。

## 5. 部署检查与排障

- PostgreSQL SELECT 1 返回 1，项目库 vector 查询有结果，Windows Redis 服务 Running 且 ping=PONG。
- /health 返回 `{"status":"ok","service":"knowledge-service"}`，/docs 能打开；根路径 / 的 404 正常。health 只检查 API 进程，不验证数据库/Redis。
- Worker 连通与队列检查（不提交业务任务）：

```powershell
python -m celery -A app.core.celery_app:celery_app inspect ping
python -m celery -A app.core.celery_app:celery_app inspect active_queues
```

预期 pong、CPU 消费 default_queue。Redis 连接失败先检查服务、绑定/端口、6379 监听及 PONG，再查连接 URL；文档一直 UPLOADED 时确认 CPU Worker 正在运行。

Swagger 仍显示 Add string item 时，重启 API、Ctrl+F5 刷新，检查 `/static/docs/swagger_upload.js` 返回 200 及 jsDelivr 资源加载；不需要改请求字段。

MinerU API 成功不等于结果 CDN 可下载。下载失败先查 parse-jobs、平台批次与直连网络/VPN/TUN，不关闭证书校验、不改签名 URL、不盲目创建新批次。详见 [网络排查与恢复](docs/MINERU_CLOUD.md#结果下载与代理排查)。

数据库与原件目录应一起备份；对外部署需增加鉴权、网关请求体限制和文件安全保护。

## 6. 开发文档

[开发规则](AGENTS.md) · [路线与验收标准](docs/DEVELOPMENT_PLAN_CODEX.md) · [Parser 契约](docs/PARSER_CONTRACT.md) · [MinerU Cloud](docs/MINERU_CLOUD.md) · [Parser Router](docs/PARSER_ROUTER.md) · [Selective Hybrid](docs/SELECTIVE_HYBRID.md) · [业务日志](docs/BUSINESS_LOGGING.md)
