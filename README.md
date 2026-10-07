# 数知融问智能体（DKF-Agent）

**中文名称：** 数知融问智能体  
**英文名称：** Data-Knowledge Fusion Agent  
**英文简称：** DKF-Agent  
**项目目录名：** `dkf-agent`  
**当前开发模块：** `knowledge-service`（非结构化知识问答）

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
| Database | PostgreSQL + pgvector |
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
| Deploy | Docker Compose |

## 开发环境

### 1. 创建 Conda 环境

```bash
conda env create -f environment.yml
conda activate dkf-agent
```

GPU 版 PyTorch 建议根据本机 CUDA 环境单独安装，避免在环境文件中固定 CUDA 版本。

### 2. 配置环境变量

复制环境变量模板：

```bash
cp .env.example .env
```

至少需要配置：

```text
DATABASE_URL
REDIS_URL
DEEPSEEK_API_KEY
OCR_PROVIDER
OCR_API_KEY
```

### 3. 启动基础服务

```bash
docker compose up -d postgres redis
```

### 4. 初始化数据库

```bash
alembic upgrade head
```

### 5. 启动 Celery Worker

CPU 任务：

```bash
celery -A app.core.celery_app worker -Q default_queue --concurrency=2 -l info
```

GPU 任务：

```bash
celery -A app.core.celery_app worker -Q gpu_queue --concurrency=1 -l info
```

### 6. 启动 API

```bash
uvicorn app.main:app --reload
```

## 主要功能

当前非结构化模块按以下顺序开发：

1. Knowledge Base 数据模型。
2. 预置知识库批量导入。
3. PDF / DOCX / TXT / Markdown 解析。
4. JPG / PNG / 扫描 PDF OCR。
5. 文档 Chunk。
6. BGE-M3 向量化并写入 pgvector。
7. Dense Retrieval + BM25。
8. bge-reranker-v2-m3 重排。
9. DeepSeek RAG 与来源引用。
10. 管理员文档上传、状态查看、Delete、Reindex。
11. 文档公式识别、参数绑定和 SymPy 计算。
12. Knowledge Agent 封装。
13. RAG / OCR / Formula 评测。

## 使用方式

### 普通知识库问答

知识库完成构建后，用户无需上传文件即可直接提问：

```text
用户问题
→ 检索知识库
→ 重排
→ RAG
→ 返回答案、文件名、页码和证据片段
```

### 管理员增量入库

```text
管理员上传文档
→ Celery 后台解析 / OCR
→ Chunk
→ Embedding
→ pgvector
→ READY
→ 新文档自动加入知识库
```

当前开发目标以非结构化知识问答独立闭环为准，后续再接入 Data Agent、Coordinator Agent 和 Fusion Agent。
