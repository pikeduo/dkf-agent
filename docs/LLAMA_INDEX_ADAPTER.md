# LlamaIndex Adapter：边界与用户验收

阶段 15A 仅建立运行时表示和 Query Embedding 适配。唯一持久化真相来源仍是 PostgreSQL document_chunks；不新增表、索引、迁移、API、Worker 或框架存储。阶段 1～15 的数据处理状态不变，不实现 Dense/BM25/Fusion/Reranker/Response Synthesis/Query Engine。

上述为 15A 独立范围；用户后续已授权 [阶段 17 Dense Retriever](DENSE_RETRIEVAL.md)。Adapter 新增 query_model_identity() 仅在显式检索时委托同一已有 Service.identity()，不猜权重指纹、不创建另一套模型；15A 的文档编码拒绝、Node 契约及待人工验收项不变。

## 手动准备最小依赖

用户已确认新增 `llama-index-core>=0.14,<0.15`，Python 3.11 在该系列支持范围内。只使用 [官方 Schema](https://developers.llamaindex.ai/python/framework-api-reference/schema/) 与 [BaseEmbedding](https://developers.llamaindex.ai/python/framework-api-reference/embeddings/)；不安装全量 llama-index 或默认 OpenAI、Reader、PGVectorStore 集成。

```powershell
conda activate dkf-agent
python -m pip install "llama-index-core>=0.14,<0.15"
python -m pip check
```

这些命令仅用户执行。安装会下载 core 及传递依赖（NLTK/tiktoken、工作流工具等），可能调整现有 Pydantic、SQLAlchemy、tenacity 等版本；需核对依赖冲突、安装时长与磁盘空间，不无条件升级环境。无需 BGE-M3/LLM 模型下载或新 API Key，转换不用 GPU。Query 真实调用仍按既有 BGE-M3 部署，CLI 与 GPU Worker 同卡可能同时加载两份模型，应避免。

### core 首次导入资源边界

官方 core 的 GlobalsHelper 在导入时校验 NLTK stopwords/punkt_tab 缓存，缺失时可能自行下载（见 [官方源码](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/utils.py)）。这不是本项目原件解析或模型推理，但不能声称第一次导入绝无网络/文件操作。现有 API/Worker 不导入 Adapter，故 core 未安装或缓存未准备不改变旧业务入口。

若需首次导入也不触发隐式资源下载，用户可在导入 core 前显式准备官方资源缓存（只在允许联网时手动执行）：

```powershell
python -c "from importlib.metadata import distribution; from pathlib import Path; import nltk; cache=Path(distribution('llama-index-core').locate_file('llama_index/core/_static/nltk_cache')); cache.mkdir(parents=True, exist_ok=True); assert nltk.download('stopwords',download_dir=str(cache),raise_on_error=True); assert nltk.download('punkt_tab',download_dir=str(cache),raise_on_error=True); print('nlp_resources_ready')"
```

该命令写 Conda 包目录缓存，需可写权限；离线环境由用户提前复制相同官方资源，不能让 Codex 临时联网补齐。本阶段不调用 LlamaIndex tokenizer、Splitter 或 Reader，不自动准备上述缓存，第三方版本/导入行为仍需人工核对。

## Node 与内部结果契约

```python
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result
from llama_index.core.schema import NodeWithScore

# chunk/document 必须由调用方从已有数据库显式读取全部必需字段。
node = chunk_to_node(chunk, document)
result = node_to_result(NodeWithScore(node=node, score=0.0))
```

- TextNode.id_ 为 chunk_id 字符串，不生成随机标识；SOURCE 绑定原 doc_id、kb_id、file_name/file_type。ID 或来源矛盾直接安全失败，不修复 Citation。
- metadata 含 kb_id/doc_id/chunk_id/file_name/file_type/page_start/page_end/section；chunk_metadata 深复制完整原字段，包括 source_spans、page_semantics、输入 Hash 和算法信息，不伪造页码/坐标。可用时保存原 index_config.result.model 的 embedding_identity。
- metadata 从 LlamaIndex EMBED/LLM 文本中排除，不把文件名/页码/ID 前缀混入已有编码；内部结果读取原 node.text，不使用附加 metadata 或修剪后的内容。
- 默认复制已有向量（兼容 pgvector NumPy 输出），严格校验 1024 维、有限单位向量但不重新归一化。尚未向量化时保持 None，不补向量；include_embedding=False 可明确只转换证据。Node/DTO 的 metadata 不共享 ORM 可变对象。
- 批量调用可显式传 `log_success=False` 省略逐项转换成功日志并由调用方记录汇总；错误事件仍记录，默认True不改变原15A/Dense行为或Node契约。
- 内部 RetrievalResult 不依赖 LlamaIndex、不包含向量；含稳定 UUID、文档/页码/章节/text/完整 metadata/可选模型身份与 retrieval_score。未知分数保留 None，0/负分原样，非有限值拒绝，不解释成置信度。
- 阶段20最小兼容：Node可额外携带`fusion`，严格验证RRF来源/原排名/分数后还原为DTO的可选`FusionProvenance`；普通Dense/BM25 Node无需此字段，未知字段仍拒绝，不修改chunk_metadata。`node_to_result(..., log_success=False)`仅关闭批量成功日志，错误仍记录，默认行为不变。融合分与NodeWithScore.score须一致，见[Hybrid](HYBRID_RETRIEVAL.md)。
- 必需 ORM 字段未加载返回 RAG_ADAPTER_UNLOADED，转换不隐式 SELECT/autoflush。Document 需 doc_id/kb_id/file_name/file_type/index_config；Chunk 需 ID/归属/text/页范围/section/chunk_metadata，默认另需 embedding。调用方使用普通 ORM select 并在会话内显式 refresh/load 字段；不要把 partial ORM 或过期对象交给 Adapter。
- 互转只校验内存来源一致性，不证明数据库当前 READY、权限、完整证据或 Query 模型相容性。未来 Retriever 仍必须限定 READY 和实际模型/编码版本，读取后不得任意修改来源 metadata。

## Embedding Adapter

DKFEmbeddingAdapter 继承 BaseEmbedding，但不修改全局 Settings/default LLM/embed_model，不启用框架 Embedding Cache 或 Tracing。构造只保存可选注入服务；显式 get_query_embedding/aget_query_embedding 才委托项目 get_embedding_service().embed_query，原 Query 不加前缀，不构造新的 SentenceTransformer。异步包装在同进程线程运行，仍用项目模型锁，无新 GPU Worker。

框架 get_text_embedding/get_text_embedding_batch（包括异步/私有入口）直接返回固定 RAG_DOCUMENT_EMBEDDING_FORBIDDEN；原文档 Embedding 继续通过项目 /embed→/index。Transform __call__/acall 只验证并返回已有向量的 Nodes，缺向量明确拒绝，绝不替 Node 补生成。这是防止重复文档编码的边界，不是另一个文档推理实现。

禁止生产使用 SimpleDirectoryReader、VectorStoreIndex.from_documents、IngestionPipeline 或 PGVectorStore 重新接管流程；本轮未实现任何完整 Retriever、Index 构建或 Query Engine。第三方 instrumentation 若由其他代码开启仍可能观察调用参数，不能把其当作已脱敏的业务日志，禁止未经审查的调试/Tracing 导出。

## 用户执行用例

Codex 只编写用例，不运行；core 缺失时以下 Adapter 模块跳过，跳过不是通过。安装及缓存准备后由用户执行：

```powershell
python -m pytest tests/test_llama_index_adapter.py tests/test_business_logging.py -q
python -m pytest tests/llama_index_adapter_db_smoke.py tests/knowledge_db_smoke.py -q
# 历史回归仍由用户决定范围与运行预算。
python -m pytest tests -q
python -m pytest tests/indexing_db_smoke.py tests/embedding_db_smoke.py tests/chunk_db_smoke.py tests/knowledge_admin_smoke.py tests/document_upload_smoke.py tests/document_processing_smoke.py -q
```

单测只构造未绑定 Session 的已有字段 Stub，不生成文件或原件。DB 用例复用隔离 schema/保存点，只模拟已有表记录，用户运行结束后回滚，不写 public、不启用第二套存储。覆盖双向契约、ID 稳定、None/0 分数、完整 source_spans/章节/页码、深复制、无隐式 SQL、Query 委托/惰性加载、文档重编码拒绝、错误与日志脱敏。Stub 不证明真实 CUDA/模型已验收。

## 已有数据库的真实手动转换

本轮不需要迁移，head 仍为 0007_pgvector_index。安装/缓存准备后，仅由用户对一条已有 Chunk 执行只读转换；替换 Chunk UUID，不重新上传、解析或向量化：

```powershell
$adapterReadCheck = @'
# 仅用户运行：显式读取现有行，转换不执行 Parser、任务投递或模型推理。
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from llama_index.core.schema import NodeWithScore
from app.db.session import get_engine
from app.models import Document, DocumentChunk
from app.services.rag.llama_index.node_adapter import chunk_to_node, node_to_result

with Session(get_engine()) as session:
    chunk, document = session.execute(select(DocumentChunk, Document)
        .join(Document, (Document.doc_id == DocumentChunk.doc_id) & (Document.kb_id == DocumentChunk.kb_id))
        .where(DocumentChunk.chunk_id == UUID('你的ChunkUUID'))).one()
    before = (chunk.text, chunk.page_start, chunk.page_end, chunk.section, chunk.chunk_metadata)
    node = chunk_to_node(chunk, document)
    result = node_to_result(NodeWithScore(node=node, score=0.0))
    assert node.node_id == str(chunk.chunk_id) and result.chunk_id == chunk.chunk_id
    assert (result.text, result.page_start, result.page_end, result.section, result.metadata) == before
    assert not session.dirty and not session.new
    print('identity_match=', True, 'evidence_match=', True, 'score=', result.retrieval_score,
          'vector_reused=', node.embedding is not None, 'span_count=', len(result.metadata.get('source_spans', [])))
'@
python -c $adapterReadCheck
```

预期 identity_match/evidence_match=True、score=0.0，已入库 Chunk 的 vector_reused=True，span_count 与原 metadata 一致；不打印正文、向量或本机路径。会话退出回滚只读事务，无新 Chunk/框架表或状态推进；文件缺失也不应读取原件来补齐。若出现 UNLOADED，显式加载字段后重试，不降低证据校验。

Query Adapter 的真实 GPU 运行仍需另行由用户按既有 Embedding 部署验证，避免与 GPU Worker 双模型占显存。本轮未安装 core、导入第三方运行时、准备 NLP 缓存、执行模型/迁移/pytest/Smoke 或真实文档处理；类型与第三方版本兼容性、全部历史回归均待手动确认。验收后等待用户决定下一阶段，不自行进入检索或 RAG。
