# DKF-Agent 非结构化知识问答开发计划

> 当前范围：`knowledge-service`。每次只完成一个阶段，并在人工测试通过后进入下一阶段。

## 阶段 0：项目初始化

- 保持 Python 3.11 Conda 环境。
- 保留根目录的 `README.md`、`AGENTS.md`、`environment.yml` 与本计划。
- 使用 `.env.example` 说明配置项；真实 `.env` 始终由 Git 忽略。
- 不在本阶段创建 FastAPI 业务代码或后续知识库目录。

验收：`conda activate dkf-agent` 后 `python --version` 显示 3.11；`git status` 中不出现 `.env`。

## 阶段 1：FastAPI 最小项目

- 创建应用入口、统一配置和 `/health` 健康检查接口。
- 验收：`uvicorn app.main:app --reload` 能启动，`GET /health` 正常返回。

## 阶段 2：PostgreSQL、pgvector、Redis

- 用 Docker Compose 启动 PostgreSQL（启用 pgvector）与 Redis。
- 验收：应用能够读取配置并连接两个服务，pgvector 扩展可用。

## 阶段 3：Celery 基础设施

- Redis 作为 Broker/Result Backend，建立 CPU 默认队列、GPU 队列和最小测试任务。
- GPU Worker 后续始终采用 `--concurrency=1`。
- 验收：API 可投递任务，Worker 可执行并查询状态。

## 阶段 4—7：知识库数据与异步入口

- 建立 `knowledge_bases`、`documents`、`document_blocks`、`document_chunks` 数据模型和迁移。
- 提供管理员侧知识库创建、查询与文档列表接口。
- 支持 PDF、DOCX、TXT、Markdown、JPG、PNG 上传、SHA256 去重和 `UPLOADED` 状态。
- 建立 `UPLOADED → PARSING → OCR_PROCESSING → CHUNKING → EMBEDDING → INDEXING → READY/FAILED` 流程入口。

## 阶段 8—15：解析、OCR、Chunk 与索引

- 定义统一 `ParsedDocument` / `DocumentBlock`，保留文档、页码、章节、来源和置信度。
- 依次实现 TXT、Markdown、DOCX、文本 PDF；再通过 Provider 接口接入火山 OCR，并预留百度 OCR。
- 支持扫描/混合 PDF、OCR 缓存与可追溯 Chunk。
- 用 BGE-M3 在 GPU Worker 批量生成向量，并写入 pgvector。

每个 Chunk 必须保留：`kb_id`、`doc_id`、`chunk_id`、`page_start`、`page_end`、`section`、`text`。

## 阶段 16—22：导入、检索与 RAG

- 准备至少 10 份、多格式预置知识库文档并支持批量导入。
- 实现 Dense Retrieval、单文档检索、BM25/Hybrid Retrieval 与 bge-reranker-v2-m3。
- 接入 DeepSeek RAG，仅按 Evidence 作答；证据不足时明确返回。
- 建立 `Answer → Evidence → Chunk → Page → Document` 来源链，返回文件名、页码、片段、Chunk ID、分数和耗时。

## 阶段 23—24：管理与增量入库

- 完善文档状态、解析块、Chunk、Retry、Delete、Reindex 的管理员能力。
- 新文档经 Celery 处理到 READY 后无需重建全库即可参与检索。

## 阶段 25—27：公式识别与安全计算

- 从证据中提取公式、变量、参数含义和来源。
- 参数不足时返回 `need_variables`，不猜测数值。
- 将公式标准化后用 SymPy 安全计算；禁止 `eval()`。

## 阶段 28—30：封装、评测与独立验收

- 最后才封装 Knowledge Agent，并保持稳定输出：`status`、`answer`、`entities`、`evidence`、`formula_results`。
- 建立 RAG、OCR、Formula 评测；至少包含 10 个单文档 RAG 正式用例，优先验证忠实度、检索召回、引用准确性和耗时。
- 独立验收应覆盖 10+ 预置文档、动态上传扫描 PDF、检索问答、引用和公式计算。

## 约束

- 仅开发 `knowledge-service`，不提前开发 NL2SQL、Data Agent、Coordinator 或 Fusion。
- 耗时入库工作使用 Celery，任务必须可重试、幂等，并记录失败原因。
- 不引入 Elasticsearch、知识图谱、本地大型视觉语言模型、Kubernetes 或不相关大型依赖。
- 每一阶段完成后先进行人工测试；不自动提交或推送 Git。
