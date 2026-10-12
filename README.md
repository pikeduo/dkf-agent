# 数知融问智能体（DKF-Agent）

`knowledge-service` 用于非结构化知识库建设与问答。当前支持知识库管理、单选/多选上传、异步解析及状态查询；默认自动选择 Native 或 MinerU Cloud，复杂 XLSX 优先局部云增强。

**解析 → CHUNKING，显式切片 → EMBEDDING，显式本地向量化 → INDEXING，显式 pgvector 入库 → READY。** READY 表示所有 Chunk 向量已入库，当前尚无检索或问答接口。管理接口暂无鉴权，仅限本机或可信内网，不应暴露公网。

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

# 模型与 LLM：BGE-M3 本地部署，不自动下载。
EMBEDDING_MODEL_DIR=models/bge-m3
EMBEDDING_DEVICE=cuda:0
EMBEDDING_BATCH_SIZE=16
EMBEDDING_MAX_LENGTH=8192
EMBEDDING_USE_FP16=true

# OCR / 外部服务：仅云解析需要 Token
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_API_TOKEN=
MINERU_MODEL_VERSION=vlm

# 存储与可选功能
UPLOAD_DIR=data/uploads
MAX_UPLOAD_SIZE_MB=50
PARSED_DIR=data/parsed
PARSE_CACHE_MAX_ENTRY_MB=768
EMBEDDINGS_DIR=data/embeddings
# 本地切片，估算 token，提交时冻结参数。
CHUNK_SIZE=600
CHUNK_OVERLAP=80
# auto 内部优先尝试 XLSX 视觉增强。
HYBRID_XLSX_ENABLED=true
HYBRID_MAX_CANDIDATES=64
HYBRID_MAX_BUNDLE_PAGES=32
HYBRID_MIN_IMAGE_SIDE=32
HYBRID_MAX_IMAGE_PIXELS=16000000
HYBRID_MAX_IMAGE_MB=16
```

- 替换数据库密码，URL 中特殊字符需百分号编码；真实密钥和 `.env` 不提交仓库。
- Redis 逻辑库 0/1/2 分别用于应用、消息、结果；三个 URL 指向同一本地服务，未配置 Celery URL 时使用 REDIS_URL。
- API/Worker 使用同一数据库，并能读写同一原件目录；UPLOAD_DIR 相对项目根目录，默认单文件上限 50 MiB。
- API/Worker 共用可读写的 `PARSED_DIR`（默认 data/parsed）；每个缓存代次上限默认 768 MiB，超限明确失败。缓存包含原文/ZIP，限制目录访问并备份整个目录，不在运行中删隐藏元数据；自定义仓库内目录需自行加入忽略规则。没有自动清理。
- Native 解析/切片无需外部密钥或 GPU；云解析从 [MinerU API 管理页](https://mineru.net/apiManage/docs) 获取 Token。文档向量化需要本地 BGE-M3、CUDA GPU 和 GPU Worker；不需要 DeepSeek Key，不下载 Reranker。
- API/GPU Worker 共用可读写的 EMBEDDINGS_DIR，默认 data/embeddings；向量文件含业务衍生数据，应限制访问并备份，不入仓库。每代次结果上限 512 MiB，无自动清理；模型和向量目录自定义到仓库内时需自行配置忽略规则。
- 切片不读取原件、不调用 MinerU 或模型；CHUNK_SIZE 允许 400～700，CHUNK_OVERLAP 允许 50～100。采用本地 token 估算而非模型精确计量；长表格/公式优先整体保留，可能超目标，metadata 会标记。
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

启动顺序：**PostgreSQL/Redis → 数据库初始化 → API/CPU Worker；向量化另准备本地模型并启动 GPU Worker**。以下命令均在项目根目录、已激活的 dkf-agent 环境中执行。

初始化/升级数据库：

```powershell
python -m alembic upgrade head
python -m app.db.seed
python -m alembic current
```

预期 head 为 `0007_pgvector_index`，默认知识库 ID 为 `7f2044ca-2041-42ce-977d-40bf5c79ed40`。seed 可重复执行；应用启动不会自动迁移、启用扩展或创建默认库。升级前备份并停止 API/Worker，迁移后重启；历史文档不会自动切片、向量化或入库。

该迁移新增入库检查点和 HNSW cosine 部分索引，要求 pgvector 支持 HNSW（0.5.0 起）。普通事务建索引可能阻塞写入，已有大量向量时需安排维护窗口及磁盘/内存预算；应用不自动升级扩展或改系统服务。部署核对见 [pgvector 入库说明](docs/PGVECTOR_INDEX.md)。

分别打开终端启动 API 和 CPU Worker：

```powershell
uvicorn app.main:app --reload
```

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

默认访问 [Swagger](http://127.0.0.1:8000/docs) 和 [健康接口](http://127.0.0.1:8000/health)。监听地址/端口以 Uvicorn 参数为准；局域网访问加 `--host 0.0.0.0 --port 8000`，`--reload` 仅用于开发。

CPU Worker 应显示 default_queue、process_document、chunk_document、index_document、submit_mineru_parse、check_mineru_result 和 ready；仅启动 API 不会执行解析、切片或入库。Windows threads/solo 仅用于开发，正式 Linux CPU 使用 prefork 并发 2～4。

自动模式默认可能生成 XLSX 的 PDF Bundle，因此 Windows 使用上面的串行 Worker；不要同时保留旧 threads Worker 消费该队列。

### 本地 BGE-M3 与 GPU Worker

仅做上传/解析时无需模型。向量化使用 [BAAI/bge-m3 官方模型](https://huggingface.co/BAAI/bge-m3) 的 Dense 1024 维输出，文档和 Query 共用 `sentence_transformers.SentenceTransformer`，不是 FlagEmbedding.BGEM3FlagModel；不启用 Sparse/ColBERT。固定 `local_files_only=True`、`trust_remote_code=False`，缺文件报错，Worker 不自动下载或降级 CPU。

以下安装、下载、模型验证、迁移和启动命令**仅由用户手动执行**，不是给 Codex 的执行授权；不需要重建整个 Conda 环境。

#### 1. 确认环境与 CUDA

在项目根目录的 PowerShell 中执行，先确认 Python 和 pip 都属于 `dkf-agent`，而非 base 或系统 Python：

```powershell
conda activate dkf-agent
python --version
python -c "import sys; print(sys.executable)"
python -m pip --version
python -m pip show torch sentence-transformers huggingface_hub transformers sentencepiece
nvidia-smi
python -c "import torch; print('torch=',torch.__version__,'torch_cuda=',torch.version.cuda,'available=',torch.cuda.is_available(),'devices=',torch.cuda.device_count())"
```

目标为 Python 3.11、RTX 4060 Laptop（8GB 显存）；已有驱动 596.21、nvidia-smi 显示 CUDA 13.2。曾检测到的 `2.14.1+cpu` / `False` 只反映当时环境，以本次输出为准。若 torch 未安装，先完成下一步再检查。

`nvidia-smi` 的 CUDA 字段表示驱动支持的 CUDA 版本，不代表 PyTorch 使用的版本或已经安装完整 Toolkit；PyTorch 构建版本看 `torch.version.cuda`。见 [NVIDIA 字段说明](https://docs.nvidia.com/deploy/nvidia-smi/index.html#cuda-version)。

#### 2. 仅在需要时替换 CUDA 版 PyTorch

优先保留现有 PyTorch 主版本，能匹配时保留完整版本号，仅把 CPU Wheel 换成 CUDA Wheel，不无条件升级整个环境。对曾使用 `2.14.1+cpu` 的上述环境，候选命令为：

```powershell
# 先核对官方可用版本；此命令不安装软件。
python -m pip index versions torch --index-url https://download.pytorch.org/whl/cu132
# 仅确认版本、Windows x64 / Python 3.11 Wheel 和驱动兼容后执行。
python -m pip install --upgrade "torch==2.14.1+cu132" --index-url https://download.pytorch.org/whl/cu132
```

[官方 cu132 索引](https://download.pytorch.org/whl/cu132/torch/) 列有 `torch-2.14.1+cu132-cp311-cp311-win_amd64.whl`，但文件存在不等于本机运行已验证。执行前核对当前索引、解释器及驱动；已有可用 CUDA 包就跳过替换。若无法匹配，先记录错误并参考 [官方安装选择器](https://pytorch.org/get-started/locally/)，不要盲目换版本。已有 torchvision/torchaudio 时也需核对它们与 torch 的兼容性；本阶段不要求为 Dense 推理额外安装它们。

预编译 Wheel 自带所需 CUDA 运行库，通常无需另装完整 CUDA Toolkit；只有自行编译 PyTorch/自定义 CUDA 扩展等场景才另行考虑，见 [PyTorch 维护者说明](https://discuss.pytorch.org/t/should-i-install-the-extra-cudatoolkit-and-cudnn/194528/2)。由用户检查 CUDA 并做一次简单 GPU 计算：

```powershell
python -c "import torch; print('torch=',torch.__version__,'torch_cuda=',torch.version.cuda,'available=',torch.cuda.is_available(),'devices=',torch.cuda.device_count())"
python -c "import torch; assert torch.cuda.is_available(), 'CUDA unavailable'; x=torch.arange(4,device='cuda:0',dtype=torch.float32); print('device=',x.device,'sum_squares=',(x*x).sum().item())"
```

cu132 方案预期 CUDA 为 13.2、available=True、设备数至少 1，GPU 计算输出 device=cuda:0、sum_squares=14.0；否则停止模型部署，先查环境/驱动，不用 CPU 假装验收成功。

#### 3. 只补齐缺少的推理依赖

`environment.yml` 已声明 sentence-transformers、transformers；huggingface_hub 用于下载，亦为推理依赖链的一部分。**只有环境检查确认缺包时才执行对应行**，已有可用版本保留，不使用全环境升级：

```powershell
python -m pip install sentence-transformers
python -m pip install huggingface_hub
# 仅在上述依赖未补齐、且确认实际缺少时执行对应行。
python -m pip install transformers
python -m pip install sentencepiece
python -m pip check
```

tokenizers、numpy、safetensors 等由依赖解析补齐；其他缺失/冲突按 `pip check` 与导入错误单独处理，不加训练/ONNX extras。预期 `No broken requirements found.`，冲突时先核对版本再继续。虽然环境声明含 FlagEmbedding，本阶段代码不调用它，缺少它不能作为要求安装它或更换框架的理由。

#### 4. 下载完整的本地模型

由用户手动联网下载，目录保持与 `EMBEDDING_MODEL_DIR=models/bge-m3` 一致：

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='BAAI/bge-m3', local_dir='models/bge-m3', allow_patterns=['*.json','pytorch_model*.bin','model*.safetensors','sentencepiece.bpe.model'], ignore_patterns=['onnx/*']); print('model_download_complete')"
```

JSON 通配符也保留子目录配置：必须包含 config.json、modules.json、Tokenizer 文件、完整权重，以及 modules.json 指定的 Pooling/Normalize 部件（如 1_Pooling/config.json；Normalize 可能仅由模块声明提供，不自造配置）。不下载 ONNX 或 Sparse/ColBERT 权重，不仅下载单个权重文件。参考 [官方文件下载说明](https://huggingface.co/docs/huggingface_hub/guides/download)。

模型为数 GB，首次下载、校验和加载会占用时间、磁盘、内存与显存，需预留下载缓存/临时文件空间。记录官方快照版本；更新模型先停相关进程并重新向量化，不运行中替换文件。根目录 `models/` 已由 `.gitignore` 的 `/models/` 规则保护，不提交模型到 GitHub。

#### 5. 本地加载验证与 8GB 显存配置

当前默认 `EMBEDDING_DEVICE=cuda:0`、`EMBEDDING_BATCH_SIZE=16`、`EMBEDDING_MAX_LENGTH=8192`、`EMBEDDING_USE_FP16=true`。RTX 4060 的 8GB 显存不保证 batch=16 与长输入可用，首次部署建议用户手动考虑将 Batch Size 设为 1～2，保持 FP16；Codex 不修改 `.env`。最大长度是上限而非保证，超长输入不会静默截断。

**Embedding 检查点建立后，参数已冻结，直接改 `.env` 并重投不会改变原任务。** 需停止相关进程、按 [Embedding 恢复说明](docs/EMBEDDING.md#检查点状态与结果) 用成功 Parse Cache 经 Reindex→Chunk→Embed 建立新检查点；不为此 Force Reparse 或重新消耗 MinerU。

先停止 GPU Worker，避免命令行进程与 Worker 同时加载两份 BGE-M3。下面只调用项目统一入口，验证本地部署是否成功，不访问数据库或云 API，不打印向量、正文或模型路径：

```powershell
$embeddingDeployCheck = @'
# 仅用户手动执行：复用项目统一编码入口，不建立另一套模型推理流程。
import math
import torch
from app.services.embedding import embed_documents, embed_query, get_embedding_service

service = get_embedding_service()
assert service.options.device == 'cuda:0', '需要 cuda:0 配置'
assert torch.cuda.is_available(), 'CUDA 不可用'
docs = embed_documents(['本地部署验证', '知识库向量服务'])
query = embed_query('本地部署验证')
dims = [len(vector) for vector in docs]
norms = [math.sqrt(sum(value * value for value in vector)) for vector in docs + [query]]
assert len(docs) == 2 and dims == [1024, 1024] and len(query) == 1024
assert all(abs(norm - 1.0) < 1e-4 for norm in norms)
allocated_mb = torch.cuda.memory_allocated(0) / (1024 * 1024)
assert allocated_mb > 0, '当前进程未检测到 CUDA 显存分配'
print('device=', service.options.device, 'gpu_allocated_mb=', round(allocated_mb, 1))
print('docs=', len(docs), 'dims=', dims, 'query_dim=', len(query), 'norms=', norms)
'@
# 通过参数传入代码，避免旧版 Windows PowerShell 管道编码改变中文文本。
python -c $embeddingDeployCheck
```

预期 device=cuda:0、当前进程显存分配大于 0、docs=2、dims=[1024,1024]、query_dim=1024、所有范数约 1。服务只从本地加载且不自动 CPU 回退，成功编码与显存分配共同确认 GPU 路径；命令退出释放模型后再启动 Worker。此验证不等于长文档、吞吐或峰值显存验收。

#### 6. 数据库与 GPU Worker

用户先按本章数据库初始化命令确认当前 head `0007_pgvector_index`（包含 0006 的 GPU 检查点；已是 head 则跳过升级），再在独立终端启动 GPU Worker，每张 GPU 只运行一个消费进程：

```powershell
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

应显示 gpu_queue、app.tasks.embeddings.embed_document 和 ready。模型仅首次编码时加载，ready/health 不证明 GPU 已验收；一份文档一个任务、内部批处理，不每 Chunk 投递一次，只启动 CPU Worker 不会执行向量化。

对已切片、状态为 EMBEDDING 的文档调用 `POST /api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/embed`，命令见 [已有 Chunk 向量化](#已有-chunk-向量化)。完成为 INDEXING / awaiting_index，结果仅保存在 EMBEDDINGS_DIR；不写 pgvector、不创建索引、不标记 READY。详细真实验收、SQL 与故障恢复继续参阅 [Embedding](docs/EMBEDDING.md)，不在此重复业务测试清单。

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
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/chunk` | 已完成解析的 Block 异步切片 / 恢复切片检查点，无请求体 |
| GET | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/chunks` | 分页查询 Chunk 正文、原 Block 字符跨度和页码 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/embed` | GPU 批量向量化已有 Chunk / 恢复同一检查点，无请求体 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/index` | CPU 批量写入已有本地向量 / 恢复入库检查点，全部成功后 READY，无请求体 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/process` | 重新投递，可选 Query parse_mode，无请求体 |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex` | 仅回放成功解析缓存，停在等待切片，不调用 MinerU |
| POST | `/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reparse?force=true` | 管理员明确重新解析，可选 parse_mode，可能重新收费 |
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
| 省略 / auto | 默认自动：可完整覆盖的简单文档 Native；复杂 DOCX/PDF、图片及旧 Office/PPT 整份 MinerU；复杂 XLSX 优先 Hybrid |
| native | 管理员仅 Native 对照；不自动产生云费用，Office 视觉内容可能遗漏，不代表完整解析 |
| mineru | 强制云解析，仅 PDF、图片及六种 Office；不接受 TXT/MD/CSV |

普通上传无需选择模式；**auto 可能外传原件/视觉内容并消耗 MinerU 额度**，部署者须先配置 Token、直连与预算并确认外传权限。复杂 XLSX 默认优先尝试 Hybrid；可结构化 Chart 零云调用，图片聚合为一个 Bundle。仅提交前发现可受整份云端处理的能力缺口时才转 Whole；损坏、缺外部附件、云端不支持或超限明确 FAILED，不要求普通用户选择解析器。云提交后失败不会追加 Whole 或降级 Native。

升级后队列中未带模式的旧消息也按 auto 处理；重启前请核对待处理文件的外传权限和预算。历史已完成结果不会自动重写。

XLSX Whole 同样保留 Native Sheet/单元格/公式原式，云结果不覆盖原式；主结果与云结果全部通过校验后才一次入库。Whole 云块保留转换逻辑页与原章节，不猜原 Sheet/Anchor，对重复文本不做语义删块。关闭 `HYBRID_XLSX_ENABLED` 只改变内部增强方式，不关闭自动云解析；管理员可显式 `native/mineru` 覆盖。结构检测不保证 OCR/复杂版式语义全部正确。

Hybrid 需有效 MinerU Token、直连和可写原件目录；临时 Bundle/manifest 位于原件目录下 `.hybrid`，应随原件备份，不在运行中删除。图片只按大小/纯色及精确 Hash 过滤、复用；自定义装饰或图表视觉语义仍需人工核对。成功块的 section 为原 Sheet、page=1、bbox 为空，增强块位于对应整 Sheet table 后按 Anchor 排序，不伪造 Excel 打印页或逐单元格交错布局。详见 [Hybrid 使用限制](docs/SELECTIVE_HYBRID.md)。

多份文件重复 file 字段，每份有独立 doc_id；示例替换知识库 UUID 和文件名：

```powershell
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/你的知识库UUID/documents" -F "file=@第一份.txt" -F "file=@第二份.md"
```

单份普通上传返回 201，显式云上传返回 202，均不代表解析成功。多份返回 200 及 total/accepted_count/failed_count/items，**需检查每项 status_code、doc_id 和 detail，不代表全批成功**；单项失败不撤销其他项。重复内容返回 409 和已有 doc_id，无需重传。队列/提交失败可能已保存原件，应先查文档列表再恢复。

Native 文本严格解码 UTF-8、BOM UTF-16/32、GB18030；CSV 支持引号字段、空值，不支持不等宽行。DOCX 提取正文/表格；XLSX 按 Sheet 提取单元格、保留公式不执行，不解析图片/图表。Native Office/文本为逻辑页 1，PDF 为物理页；云 Office 为转换后逻辑页、bbox 为空，不声称原件排版坐标。

MinerU 还受平台 200 MB/200 页限制，应用取更小限制；Office 转换页数由平台最终校验。云失败不自动降回 Native。复杂 XLSX 的 Native/云质量需在两个知识库单独对照，不能直接假定云解析完整。

### 状态查询与恢复

- 文档详情是业务状态依据；云解析另查 parse-jobs。成功为 Document=CHUNKING，云 job=done；Celery SUCCESS 或交接结果 delegated_to_mineru 不代表整份解析成功。
- 保存最初响应的 task_id，可通过 /tasks 查询路由决策及任务结果（默认保留 24 小时）。转云后的文档 task_id 是本地云代次，不是短任务结果 ID；未知/过期结果 PENDING 也不一定表示排队。
- 仅 UPLOADED/FAILED 可通过 /process 重投，默认 auto；管理员可覆盖。活动云任务用 /mineru 恢复同一任务；收费失败且无可用缓存时，普通重投不再申请批次，返回 CACHE_REPARSE_REQUIRED。相同内容已有活动云任务时为 CACHE_IN_PROGRESS，完成后再重投可复用结果。
- 已验证的解析缓存命中不再提交 MinerU；相同视觉图片跨 XLSX 复用，仅未命中的图片聚合为一份 Bundle。缓存失败不降级为不完整 Native，不静默重新收费。
- `/reindex` 将缓存恢复为 Block 并停在 CHUNKING，再调用 `/chunk`；不执行 Embedding/Index，不重新调用 MinerU。缓存缺失、损坏或参数变化明确 FAILED，旧 Block/Chunk 保留。成功替换 Block 时同事务清除旧 Chunk，避免证据悬空。升级前已完成结果不会自动回填缓存。当前无 Delete/后台自动补投。
- `/reparse` 必须明确 `force=true`，跳过整份和视觉缓存；有活动云/切片/GPU/入库检查点时拒绝维护。维护支持 UPLOADED/FAILED/CHUNKING/EMBEDDING/INDEXING/READY；换代次后旧 Chunk/向量仅供诊断，成功解析/缓存回放时整体替换。管理接口暂无鉴权，仅限可信环境。

维护已有文档（无请求体，替换 UUID）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/reindex"
# 仅在确认外传权限和新增云费用后执行：
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/reparse?force=true"
```

202 表示已投递，使用文档详情查询最终状态。缓存回放不需要有效 Token；明确需要云解析的新任务仍需要 Token/Redis/预算。目录保存版本化的标准化结果、云 ZIP/content_list、上游提供的 full.md，以及 Hybrid manifest/Bundle/视觉结果；原始文件不会自动进入 Git。

- 云缓存任务可表现为 job=done、batch_id=null、upload_complete=false，这是本地复用，不伪造远端上传状态。
- 常见错误：400 空文件/文件名，404 不存在，409 重复/状态冲突，413 超限，415 格式，422 参数/原件校验，503 服务/配置/迁移不可用，507 存储不可用；详情见响应 detail。

### 已有文档切片

文档详情为 CHUNKING 且解析已完成时，提交一次（无请求体）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/chunk"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/chunks?limit=20&offset=0"
```

POST 返回 202 及独立的 `chunk_task_id`，用 `/tasks/{chunk_task_id}` 查切片任务，文档原 `task_id` 仍对应解析代次。完成为 EMBEDDING、任务结果 awaiting_embedding；不是 READY。GET 返回 items/total/limit/offset，每项包含 chunk_id/kb_id/doc_id/text/page_start/page_end/section/metadata，metadata 中 source_spans 可反查 Block 和字符区间；页范围含重复标题上下文的来源页，Office/文本仍为逻辑页。

重复 `/chunk` 沿用同一冻结参数和任务编号，已完成时不再投递；切片失败后再次 `/chunk` 恢复该检查点，不通过 `/process` 重新解析。503 投递确认失败可能已被 Worker 消费，先查询状态。历史 Block 缺少可信 block_index 时安全失败 CHUNK_SOURCE_ORDER，不按 UUID/bbox 猜顺序。长度/完整性及人工验收详见 [Chunk](docs/CHUNK.md)。

### 已有 Chunk 向量化

文档为 EMBEDDING 且已完成切片时，提交一次（无请求体）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/embed"
```

返回 202 及独立 embedding_task_id，用 `/tasks/{embedding_task_id}` 和文档详情查询；完成为 INDEXING / awaiting_index，结果包含数量、维度和校验和，不返回向量或私有路径。文件在 EMBEDDINGS_DIR 下按 doc_id/embedding_task_id 保存 vectors.jsonl 与 manifest.json；此步不写 Chunk.embedding，需继续显式 `/index`。

重复提交恢复同一冻结配置，完成后不重投。超过真实 token 上限、缺模型、CUDA 不可用、模型运行中变化、输出维度/数值错误均明确失败，不静默截断或伪装成功。失败后修复原配置可再次 `/embed`；改变 batch/device/模型目录等参数需要先 Reindex→缓存 Block→Chunk→Embed 创建新检查点，不需要为此 Force Reparse。独立 embed_documents()/embed_query() 调用及人工验收见 [Embedding](docs/EMBEDDING.md)，当前没有 Query Embedding HTTP 接口。

### 已有向量 pgvector 入库

文档为 INDEXING、已发布完整本地向量时提交一次（无请求体），不需要 GPU Worker 或重新下载/加载模型，但需要 CPU Worker 和可读的 EMBEDDINGS_DIR：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/知识库UUID/documents/文档UUID/index"
```

返回 202 及 `index_task_id`；用 `/tasks/{index_task_id}` 与文档详情查询。文件身份/校验和、模型指纹、Chunk 代次及 1024 维归一化全部通过后，按固定批次写入；全部向量与 READY 同事务提交。HNSW cosine 索引由迁移创建并随写入维护，不每个文档重建。

入库失败整体回滚并记录安全 `INDEX_*` 错误；修复后再次 `/index` 恢复同一代次，不用 `/embed` 重算。缺失/损坏结果不自动推理或调用云端。重复完成请求不重投，READY 文档重复 `/chunk`、`/embed` 也不重建。活动入库拒绝 Reindex/Reparse，完成后才可显式维护；READY 不等于检索/RAG 已实现。完整验收和 SQL 见 [pgvector 入库](docs/PGVECTOR_INDEX.md)。

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

数据库、原件与完整 PARSED_DIR 应一起备份；对外部署需增加鉴权、网关请求体限制和文件安全保护。

## 6. 开发文档

[开发规则](AGENTS.md) · [路线与验收标准](docs/DEVELOPMENT_PLAN_CODEX.md) · [Parser 契约](docs/PARSER_CONTRACT.md) · [MinerU Cloud](docs/MINERU_CLOUD.md) · [Parser Router](docs/PARSER_ROUTER.md) · [Selective Hybrid](docs/SELECTIVE_HYBRID.md) · [Chunk](docs/CHUNK.md) · [Embedding](docs/EMBEDDING.md) · [pgvector 入库](docs/PGVECTOR_INDEX.md) · [业务日志](docs/BUSINESS_LOGGING.md)
