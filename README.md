# 数知融问智能体（DKF-Agent）

`knowledge-service` 用于非结构化知识库建设与问答。当前支持知识库管理、单选/多选上传、异步解析及状态查询；默认自动选择 Native 或 MinerU Cloud，复杂 XLSX 优先局部云增强。

**解析 → CHUNKING，显式切片 → EMBEDDING，显式本地向量化 → INDEXING，显式 pgvector 入库 → READY。** READY 表示所有 Chunk 向量已入库；已有只读 Dense/BM25/Hybrid、本地BGE重排及DeepSeek Python问答入口，尚无 HTTP 检索或问答接口。管理接口暂无鉴权，仅限本机或可信内网，不应暴露公网。

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
# 本地在线重排，独立于文档向量化参数。
RERANKER_MODEL_DIR=models/bge-reranker-v2-m3
RERANKER_DEVICE=cuda:0
RERANKER_BATCH_SIZE=2
RERANKER_MAX_LENGTH=8192
RERANKER_USE_FP16=true
# 仅问答需要；真实 Key 只填本地 .env。
DEEPSEEK_API_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_MAX_TOKENS=2048
DEEPSEEK_TIMEOUT_SECONDS=60
RAG_MAX_CONTEXT_CHARS=60000
DEEPSEEK_API_KEY=

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

### 可选 LlamaIndex Adapter 依赖

仅使用运行时 Adapter 时，由用户手动补齐最小 core 依赖；现有上传/解析/切片/入库接口不导入它，不需重建整个环境：

```powershell
conda activate dkf-agent
python -m pip install "llama-index-core>=0.14,<0.15"
python -m pip check
```

不安装全量 llama-index 或 OpenAI/PGVectorStore/Reader 集成，不需要新 API Key、模型或迁移。安装前核对 Python 3.11 及现有 Pydantic/SQLAlchemy/tenacity 依赖；pip 可能调整传递依赖，冲突时停止并记录，不无条件升级。core 包含 NLTK/tiktoken 等依赖，首次导入可能校验/下载缺失的 NLP 资源，需由用户预先准备缓存，具体见 [Adapter 部署与使用](docs/LLAMA_INDEX_ADAPTER.md)。Dense Python Retriever 复用同一依赖，尚无 HTTP 检索或问答接口。

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

### Dense Python Retriever

仅使用已入库且与本地 Query 模型身份一致的 READY 文档；无需新迁移、队列、依赖或 API Key。显式导入 `app.services.rag.llama_index.dense_retriever.DenseRetriever`，构造传入 `kb_id`、可选 `top_k`（默认20，1～100），以及 `doc_ids=["目标文档UUID"]` 限定单/多文档（最多100个）。省略 doc_ids 检索该库全部合格文档；空列表/非法UUID拒绝，文档过滤在 PostgreSQL Top-K 前执行，无匹配不扩大范围。`retrieve(query)` 返回 NodeWithScore，`retrieve_results(query)` 返回原始证据 DTO。Query 首次调用会加载已有本地 BGE-M3，构造不会加载；尚无 HTTP 检索或问答接口。

用户先准备已有 core/NLP 缓存、PostgreSQL 和同版本 BGE-M3；检索时不需要 API/Worker，避免与 GPU Worker 同时各加载一份模型。无匹配返回空集，不扩大库或重建数据；首版精确排序有扫描成本，分数为余弦相似度而非概率。完整使用与手动验收见 [Dense Retrieval](docs/DENSE_RETRIEVAL.md)。

### BM25 Python Retriever

已有READY文档可使用 `app.services.rag.llama_index.bm25_retriever.BM25Retriever(kb_id, doc_ids=None, top_k=20)`，`retrieve(query)`返回统一NodeWithScore，`retrieve_results(query)`返回原证据DTO。文档范围与Dense相同，无词项命中返回空集；只读PostgreSQL、不加载BGE-M3/GPU、不需要Worker或新API Key，尚无HTTP检索/问答接口。

使用已声明rank-bm25和core；新环境缺包时手动 `python -m pip install "rank-bm25>=0.2.2,<0.3"`，core/NLP缓存沿用原准备方式。索引仅为实例内可重建词频缓存，每次读取当前READY语料，不产生第二套业务表或磁盘索引。首次构建有CPU/内存成本，超限失败而非部分检索；分词/使用命令和人工验收见 [BM25 Retriever](docs/BM25_RETRIEVAL.md)。

### Hybrid Python Retriever

使用 `app.services.rag.llama_index.hybrid_retriever.HybridRetriever(kb_id, doc_ids=None, top_k=20, branch_top_k=20, rrf_k=60)` 组合已有Dense与BM25；`retrieve(query)`返回按原chunk_id去重的NodeWithScore，`retrieve_results(query)`返回原证据DTO及fusion来源。每路默认20项，最终默认20项（可设30），同分按UUID稳定排序；`node.metadata['fusion']`保留dense/bm25原分数、排名及融合分，不代表概率或问答准确率。

沿用已有core、rank-bm25、PostgreSQL及同模型本地BGE-M3，不需升级为全量llama-index、新密钥、迁移或Worker；Query只编码一次，不重编码文档。无Query Rewrite/LLM/多查询扩展；正常空分支可由另一支返回结果，异常不静默单路降级。两路为独立只读快照，同Chunk证据冲突明确失败，不自动重试。部署条件、错误边界和人工验收见 [Hybrid Retrieval](docs/HYBRID_RETRIEVAL.md)。

### 本地 BGE Reranker

重排使用[官方BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)，项目`app.services.reranking.get_reranker_service()`统一管理，`BGERerankerAdapter`只委托同一进程共享服务。复用已有Torch、Transformers和core，不需要FlagEmbedding或全量LlamaIndex集成、新密钥、迁移、Worker。缺少依赖时用户先检查`python -m pip show torch transformers huggingface_hub llama-index-core`，只补缺包；CUDA安装仍沿用上面的手动步骤。

用户手动下载完整本地Safetensors权重、Tokenizer和配置（数GB，预留缓存及内存）：

```powershell
conda activate dkf-agent
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3', allow_patterns=['*.json','*.safetensors','sentencepiece.bpe.model'], ignore_patterns=['onnx/*']); print('reranker_download_complete')"
```

`/models/`已受Git忽略保护。只读本地模型、禁止远程代码及自动CPU回退；模型缺失明确错误，构造Adapter不加载。默认cuda:0、batch=2、max_length=8192、FP16；长度是Query+候选含特殊token的总上限，超限失败，不截断正文。RTX4060 8GB先从batch=1～2尝试，停止同卡GPU Worker；在线Query的BGE-M3和Reranker是两种不同模型，会同时占显存，不保证长输入可用。不足时由用户明确配置`RERANKER_DEVICE=cpu`使用FP32（较慢），不擅自降低正文长度或升级GPU环境。

显式导入`app.services.rag.llama_index.reranker_adapter.BGERerankerAdapter`，用`BGERerankerAdapter(top_n=5).postprocess_nodes(candidates, query_str=query)`同步重排Hybrid候选。NodeWithScore.score为原始rerank logit，不是概率；Node/DTO分别保留原retrieval_score、fusion_score、rerank_score，原ID/正文/页码/章节不变。默认getter进程内共享，不跨进程共享模型；修改模型配置后重启使用它的Query进程。用例、部署核验及同批候选排序对照见[重排验收说明](docs/RERANKER.md)，尚未证明排序质量提升。

### DeepSeek Python 问答

`app.services.rag.query.query_knowledge_base(kb_id, question, doc_ids=None, top_k=30, allow_cloud=False)` 复用现有Hybrid和BGE Reranker，最多5条最终证据交由LlamaIndex合成，返回项目`RAGAnswer`（answer、citations、insufficient_evidence）。引用来源/页码由原Chunk回填，不采用模型生成的来源；无证据明确返回不足。尚无HTTP问答接口，不新增迁移或Worker。

用户在本地`.env`配置`DEEPSEEK_API_KEY`及上面的模型/预算后重启问答进程；复用已有httpx、core，不需OpenAI SDK或全量LlamaIndex。默认`deepseek-flash`、关闭thinking、JSON非流式输出，参数依据[DeepSeek官方接口](https://api-docs.deepseek.com/api/create-chat-completion/)，执行前核对账号模型权限。仅允许官方HTTPS端点（也允许`https://api.deepseek.com/v1`），直连、不读环境代理、不跟随重定向。

**显式`allow_cloud=True`表示确认本次问题与最终证据正文可以外传且接受DeepSeek费用**；仅下载原件或MinerU许可不等于此许可。失败不自动重试、不静默降级回答；输出截断/引用未知或上下文超限均失败。`RAG_MAX_CONTEXT_CHARS`为完整Prompt字符预算，不是精确token数；保留整份最终证据，不自动裁剪或Refine多次调用。需要现有PostgreSQL/READY数据、匹配的本地BGE-M3和Reranker；先停止同卡GPU Worker，注意8GB双模型显存。Redis/API/Worker无需为只读问答另行启动。

最小使用、Mock/真实问题验收及错误说明见[DeepSeek RAG](docs/DEEPSEEK_RAG.md)。结构合法和引用存在不证明答案忠实或问答准确率已通过，必须对照真实证据人工核验。

若最终证据只支持问题的一部分，允许仅回答有依据部分并明确缺口，不因无关候选或局部缺失整体拒答；`insufficient_evidence=true`表示没有可靠可答内容，`false`不保证全部子问题已回答。代码不会根据非空候选或高分强制改写模型拒答，也不会为此自动追加收费请求。

### 预置知识库导入

已有 OHR-Bench、OmniDocBench 和 OCB 原件时，可按 [三数据集联合冒烟](docs/PRESET_IMPORT.md#三数据集最短操作新运行) 在独立测试库抽取约14份（5 PDF、3图像、三类Office各2份），完成上传→READY→Dense及分数据集报告。直接使用已下载 OCB，不要求补齐缺失43份、许可Excel或手工SQL导出；默认dry-run，真实处理前须用户确认全部所选文件云外传及费用。OCB断言人工核验、Omni GT不当QA，不能代替正式多格式验收或宣称问答准确率。

将已有且允许云外传的 10+ 多格式资料放入 `data/knowledge_base/preset/`，按 [素材清单说明](docs/PRESET_IMPORT.md#素材与人工清单) 手动准备 `manifest.json`；包括普通/扫描 PDF、混合内容、表格、公式、图表、Reading Order，以及 DOC/XLS/PPT 各一份。目录只带占位文件，不自动生成或下载素材。

在项目根目录执行（默认只预检，无需启动服务）：

```powershell
python scripts/import_knowledge_base.py --dry-run
```

正式导入前由用户确认迁移、本地模型、共享存储和云预算，并启动 CPU/GPU Worker；随后手动执行：

```powershell
python scripts/import_knowledge_base.py --execute --allow-cloud --timeout 1800
```

默认使用固定默认知识库，逐文件保存 `source_type=preset` 并自动提交解析→切片→向量化→入库任务；脚本需保持运行以推进下一阶段。同内容复用已有 doc_id，不自动重投 FAILED 或已有检查点；超时/中断不取消后台任务，可核对状态后重跑。全批 READY 才返回成功，READY 仍不代表已提供查询/RAG。选项、幂等限制及恢复见 [预置导入说明](docs/PRESET_IMPORT.md)。

### 公开基准数据集下载（用户手动执行）

仅使用 [OHR-Bench](https://huggingface.co/datasets/opendatalab/OHR-Bench)（原 PDF/QA）、[Microsoft OCB](https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark)（Office/问答断言）、[OmniDocBench](https://huggingface.co/datasets/opendatalab/OmniDocBench)（页面图像/区域 GT）。统一入口是 `scripts/download_benchmarks.py`，不再用混合 curl/PowerShell 下载流程。

打开独立 PowerShell。已有项目环境无需重建；全新 Conda 环境先按第 1 节创建。确认 Python/pip 同属 dkf-agent，**只执行检查确认缺少的安装行**，不升级整个环境：

```powershell
Set-Location "D:\python project\dkf-agent"
conda activate dkf-agent
python --version
python -m pip --version
python -m pip show httpx pyarrow Pillow
# 缺哪个才安装哪个；httpx 已在 environment.yml 中声明。
python -m pip install httpx
python -m pip install pyarrow
python -m pip install Pillow
python -m pip check
```

PyArrow 用于验证官方 Parquet/预览 OCB QA；Pillow 用于图片完整性检查。下载器复用已声明的 httpx，不要求安装 datasets/pandas/官方评测框架。以下均由用户执行，Codex 不自行下载或测试：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
python scripts/download_benchmarks.py --help
python scripts/download_benchmarks.py --dataset ohr --scope required
python scripts/download_benchmarks.py --dataset ocb --scope metadata
python scripts/download_benchmarks.py --dataset omni --scope metadata
# 先取得真实标注，再下载少量候选图片，不等于最终抽样已通过。
python scripts/download_benchmarks.py --dataset omni --scope candidate-images --count 5
# 先查看 .download/ocb_candidates.json，人工填写 source_review.json；空审核表会拒绝此命令。
python scripts/download_benchmarks.py --dataset ocb --scope reviewed
```

首次运行自动创建各数据集的 `docs/test/*_preset/source/` 及所需子目录；不用手工 mkdir。输入路径以脚本所在项目为准，不依赖 PowerShell 当前目录。若从其他目录启动，用脚本绝对路径，如 `python "D:\python project\dkf-agent\scripts\download_benchmarks.py" --dataset ocb --scope metadata`。不存在的 OCB 审核文件只初始化为 `[]`，不批准、不覆盖已有审核。

HF API 和 resolve 下载都使用选定镜像，GitHub QA 仍来自官方 GitHub；不会把镜像当新的官方来源。需要切官方入口时在同一命令追加 `--endpoint https://huggingface.co`（优先于环境变量）。首次自动冻结 HF 40 位 SHA，之后复用本地版本；终端和 dataset_info.json 均可查看 revision，无需手工取得。指定历史版本用 `--revision 实际40位SHA`，与既有版本不一致会停止，不混下载。

终端持续显示文件、已下载/总字节、速度、有限重试和摘要。OHR ZIP 约 1.52 GB，OCB 默认只取元数据/小 Parquet，不下载约 3 GB 全仓；Omni 标注约 42 MB，默认不下载约 1.5 GB 图像。实际大小以固定文件树为准，另留磁盘余量。全部图像仅显式使用 `--dataset omni --scope all-images`。

下载中断后**重跑完全相同的命令**；已完成原件校验后跳过，本工具 .part/检查点继续恢复。查看缺失项、验证对应范围所有文件：

```powershell
python scripts/download_benchmarks.py --dataset ocb --scope metadata --list-missing
python scripts/download_benchmarks.py --dataset ohr --scope required --verify-only
python scripts/download_benchmarks.py --dataset ocb --scope metadata --verify-only
python scripts/download_benchmarks.py --dataset ocb --scope reviewed --verify-only
python scripts/download_benchmarks.py --dataset omni --scope metadata --verify-only
python scripts/download_benchmarks.py --dataset omni --scope candidate-images --count 5 --verify-only
```

这两个检查模式不联网；普通范围的 `--dry-run` 仅联网列版本/元数据计划，不下载原件、不建目录；下方 native-library 则离线生成自有计划/报告。404/鉴权拒绝立即停止当前文件；超时/429/5xx 有限重试；磁盘不足先腾出空间。坏正式文件、未知来源 .part 或不匹配 Range 不覆盖、不盲目重启下载；先人工核对。镜像失败可显式切官方后重跑，仍沿用相同版本与摘要。不会清空 source 或 HF 缓存。

已有 OCB 的 dataset_info.json/source_record 校验正常，但 hosted_files.json 是字符串列表时，**不要重下 dataset_info 或 Parquet**。以下显式修复命令读取本地固定 SHA，完整取得并校验官方分页对象树，再备份旧表、原子替换 hosted_files.json；不删旧表原字节、不改审核表或抽样结果：

```powershell
python scripts/download_benchmarks.py --dataset ocb --scope hosted-tree --replace-hosted-tree
python scripts/prepare_ocb_preset.py --confirm-official-source --diagnose-inputs
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42 --results-dir docs/test/ocb_preset/results/run-04
```

修复前保留已验证的 source/hosted_files.structured.json；旧字符串表完整备份至 source/.download/backups/hosted_files-<原字节SHA256>.json。无替换参数时只生成副本；副本无效、分页缺失、版本不一致或备份冲突均停止，绝不先删除正式文件。重复修复会跳过已一致的对象树。修复后下载/prepare 默认入口直接可用；--verify-only 等只读模式不能与替换参数组合。

run-04 必须是尚未使用的结果目录；若已存在，手动换新编号，不改旧计划。诊断显示 source_record、审核表、文件树、QA 读取各步骤；OCB 支持官方 File Fidelity Excel、File Fidelity Word、File Fidelity PowerPoint，保留原 track/QA/Parquet，排除 Domain。空审核或原件未齐时预览仍如实报告缺口，不代表素材验收通过。详细校验、备份及旧计划边界见 [OCB 兼容诊断](docs/PRESET_IMPORT.md#ocb-兼容诊断保留正常-dataset_info仅重新获取对象树)。

下载后运行既有**离线**准备工具，再审核报告、显式提取、补合法多格式资料并合并 manifest：

```powershell
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --count 7 --seed 42
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42
python scripts/prepare_omnidocbench_preset.py --confirm-official-source --count 5 --seed 42
```

OHR 官方 `text` 仅在内部映射为 plain_text，原始 QA 字段不变；`multi` 单独报告，不猜分项类别。用户已确认当前版本 Newspaper QA 为 0，可明确选择其他六领域，在新目录生成计划，不覆盖旧报告：

```powershell
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --domains finance academic textbook law manual administration --results-dir docs/test/ohr_bench_preset/results/run-03
```

报告仍记录 Newspaper 源 QA 数和未覆盖状态；没有自动补题、改官方数据或取消其他抽样/质量要求。

Omni 候选不足/覆盖缺口返回 2，查看 .download/omni_candidates.json 后手动增加 `--count`（最多 12）重跑；最终离线选择仍可能不同，不能伪造覆盖。OCB 原 reviewed 模式只下载已审核、依赖完整的托管 Office；全量素材库另用下节 native-library，均不自动转换。详细目录、全部参数、许可审核和缺口处理见 [公开基准准备与验收](docs/PRESET_IMPORT.md#统一下载工具用户执行)。

QA/GT、下载检查点和报告仅留 docs/test，不能进入 preset 或 Git。**下载成功不代表版权/云外传授权、解析质量合格、READY 或问答准确率通过**；三个 prepare 工具依旧离线，不自动导入。

运行准备工具前，先手动准备说明中的 source 输入。`BENCH_MISSING` 为缺输入、`BENCH_EMPTY` 为空文件，只有实际超限才返回 `BENCH_LIMIT`；不要据此直接调大预算或跳过来源校验。

#### OCB 全量原生素材库（用户执行）

复用已有 OCB 元数据和固定 revision，读取全部 QA（包括 Domain/多文件依赖），按官方文件身份去重，优先 HF 托管原件。默认 DOCX/XLSX/PPTX，不限每类两份；只有原始/目标格式一致且明确 convert=no/false 才为原生候选。无需重下载已经正常的 dataset_info 或 Parquet。

```powershell
conda activate dkf-agent
$env:HF_ENDPOINT = "https://hf-mirror.com"
# 1. 离线生成完整候选/规模报告；首次自动创建目录，不下载原件。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --dry-run
# 2. 人工查看计划/许可模板；只把实际审核过的 review_template 合并到 source_review。
code docs/test/ocb_preset/results/native_download_plan.json docs/test/ocb_preset/results/license_review_candidates.json docs/test/ocb_preset/source/source_review.json
# 3. 核对原始格式、来源和本地使用许可后，逐项确认再执行；不代表云外传授权。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --execute --allow-external --workers 3
# 4. 中断/失败后恢复同一范围、预算与来源；完整文件验证后跳过。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --execute --allow-external --workers 3 --resume
# 5. 离线查缺失/失败，并单独复核已经取得的文件。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --list-missing
python scripts/download_benchmarks.py --dataset ocb --scope native-library --verify-only
# 6. 仍由既有准备器选择少量已审核 Office；目标已存在请换新编号，不覆盖旧计划。
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42 --results-dir docs/test/ocb_preset/results/native-selection-01
```

source_review 保持 JSON 数组。审核通过需正确的 reference_file/source_url、original_native=true、conversion_required=false、local_use_confirmed=true 及真实 license；没有审核的项保留未批准，禁止一键按“已下载”批准。第三方跨域跳转需逐域核对 allowed_redirect_hosts，默认只允许原域名；公开 HTTP、登录/密钥链接、内网及未审跳转均拒绝，不关闭 TLS、不抓登录态。

报告仅写 OCB results 中的 native_download_plan.json、native_download_report.json、license_review_candidates.json，不改旧抽样结果。默认单文件 200 MiB、本轮容量 8192 MiB，可用 --max-file-mb/--max-total-mb 显式设置；第三方大小未知时报告已知 HF 字节、未知数量和上界，执行时再核对响应长度。不覆盖已有完整原件；错误原件/身份不明断点保留供人审。

原件只进入 source/reference_files 或 source/external_files，不复制到 preset。可选 CSV/XLSM 另生成一版计划：追加 `--native-formats docx xlsx pptx csv xlsm --native-results-dir docs/test/ocb_preset/results/native-all-01`，后续所有命令沿用同参数。CSV 完整性检查额外限 32 MiB；XLSM 只存原件、检查宏工作簿容器，不运行宏，也不扩展当前业务上传/解析契约。新参数与范围不一致时拒绝覆盖计划；详细审核、缺口及退出码见 [OCB 全量素材说明](docs/PRESET_IMPORT.md#ocb-全量原生素材库下载用户执行)。

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

联合冒烟的 `SMOKE_RETRIEVAL_PREREQUISITE` 表示 READY/证据/向量前置校验未通过，Dense 尚未执行，不是 GPU 故障结论；先按 [只读失败复核](docs/PRESET_IMPORT.md#已有失败报告的只读复核) 查真实文档/Job 错误码，不重新上传。`code` 命令不可用时可用 Windows `notepad` 查看报告，无需安装新依赖。

MinerU 远端 done、ZIP 已下载但出现 EMPTY_BLOCK 时，先保留旧结果并按 [合并来源排查](docs/MINERU_CLOUD.md#合并正文空占位保留原页和坐标) 核对。新版归一化缓存为 mineru-adapter-v2，用户需按原命令重启 API/CPU Worker；旧冻结任务或 Reindex 版本不一致会安全拒绝，不自动付费重解析。不要删除旧缓存、修改文档状态或盲目 Force Reparse。

数据库、原件与完整 PARSED_DIR 应一起备份；对外部署需增加鉴权、网关请求体限制和文件安全保护。

## 6. 开发文档

[开发规则](AGENTS.md) · [路线与验收标准](docs/DEVELOPMENT_PLAN_CODEX.md) · [Parser 契约](docs/PARSER_CONTRACT.md) · [MinerU Cloud](docs/MINERU_CLOUD.md) · [Parser Router](docs/PARSER_ROUTER.md) · [Selective Hybrid](docs/SELECTIVE_HYBRID.md) · [Chunk](docs/CHUNK.md) · [Embedding](docs/EMBEDDING.md) · [pgvector 入库](docs/PGVECTOR_INDEX.md) · [LlamaIndex Adapter](docs/LLAMA_INDEX_ADAPTER.md) · [Dense Retrieval](docs/DENSE_RETRIEVAL.md) · [预置导入](docs/PRESET_IMPORT.md) · [业务日志](docs/BUSINESS_LOGGING.md)
