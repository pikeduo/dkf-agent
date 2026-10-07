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

### 3. 启动当前 API 服务

当前阶段已实现 FastAPI 最小服务，可在项目根目录执行：

```bash
uvicorn app.main:app --reload
```

默认访问地址为 `http://127.0.0.1:8000`。若需供局域网设备访问，可显式指定监听地址：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

PostgreSQL / pgvector、Redis、Celery 与数据库迁移将在后续开发阶段接入；在这些功能完成前，请勿执行相关启动或迁移命令。

### 4. 当前 API 接口

| 方法 | 路径 | 说明 | 预期结果 |
|---|---|---|---|
| `GET` | `/health` | FastAPI 服务健康检查 | 返回服务状态和 `knowledge-service` 标识 |
| `GET` | `/docs` | Swagger UI | 可交互查看当前 OpenAPI 文档 |
| `GET` | `/openapi.json` | OpenAPI JSON 描述 | 返回接口定义 JSON |

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
