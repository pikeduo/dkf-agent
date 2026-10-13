# Dense Retrieval：只读边界与用户验收

**阶段18单文档过滤已由用户测试并确认通过。** 阶段16正式数据验收及阶段17等此前待验收事项仍保留；本次确认不代表完整RAG或官方基准通过，不自动进入19。

## 范围与复用

指定知识库及可选单/多文档的 Python Dense Retriever，不新增 HTTP API、任务、迁移、环境变量、表或依赖，不实现 BM25、Fusion、Rerank、Query Engine、LLM 或 RAG。原上传/Parse/Chunk/Embedding/Index 不变，数据库 document_chunks 仍唯一向量真相来源。

| 入口 | 行为 |
|---|---|
| `DenseRetriever(kb_id, top_k=20, doc_ids=None)` | 构造验证并冻结范围，不加载模型或连接数据库；top_k 严格整数1～100 |
| `retrieve(str / QueryBundle)` | 返回 `list[NodeWithScore]`；Query 原文一次编码，不加指令/前缀 |
| `aretrieve(...)` | 同进程线程内独立 Session，仍使用同一模型锁；不共享线程绑定数据库会话 |
| `retrieve_results(...)` | 复用 node_to_result，返回项目内部 RetrievalResult；不是问答响应 |

## 单文档范围

```python
# 用现有 READY 文档的真实 UUID，不重新上传或生成文档向量。
retriever = DenseRetriever(kb_id, doc_ids=["目标文档UUID"], top_k=20)
nodes = retriever.retrieve(query)
```

`doc_ids` 接受列表/元组中的非零UUID对象或UUID字符串，输入最多100个；归一化、去重、稳定排序为不可变元组，修改调用方列表不改变已构造实例。单个ID限定单文档，多个ID表示该知识库内这些文档的并集。省略/None保留阶段17全库检索，显式 `[]`/空元组/单个字符串或非法ID抛 `DENSE_INPUT`，数量超限抛 `DENSE_LIMIT`，不静默放宽或截断。QueryBundle不能覆盖固定范围。

有效UUID但不存在、跨知识库、非READY或模型/代次不匹配的文档由SQL排除；全部不合格返回空集，混合范围只返回合格交集，不自动重试全库、读原件或重算。文档ID存在不等于授权，未来HTTP入口仍需另做用户权限检查；本阶段不建立RBAC。

文档IN谓词在MATERIALIZED候选CTE内、距离排序/LIMIT之前执行；已有SQL行校验再防御性拒绝越界行，不在Python里删除其他文档。Node.id_/metadata.doc_id/metadata.chunk_id/SOURCE仍为原身份，页码保留page_start/page_end与chunk_metadata.source_spans[].page，逻辑页不冒充物理页。

QueryBundle 仅接受 query_str，不接受外部 embedding/custom_embedding_strs/image_path，避免未知模型向量混用或改变本阶段编码语义。空 Query、非法控制符安全拒绝；字符上限 32768，模型实际 token 上限仍由 BGE-M3 校验，不截断。空结果不扩大知识库、不触发任务或重新索引；每次检索编码一次 Query，无新增 Query 缓存。

`DKFEmbeddingAdapter.query_model_identity()` 是 15A 最小兼容补充：调用已有进程共享服务 identity()，包含 model_name/model_fingerprint/dimension/normalized/version/max_length/precision。初次显式调用可能加载用户已部署的本地模型；构造不会加载。编码前后身份均严格校验，变化时拒绝本次结果。SQL 比较阶段 15 的完整 index_config.result.model；同为 1024 维但权重、编码版本、精度或长度不同都不混用。修改目录位置不改变模型文件指纹，修改权重/Tokenizer/Pooling 或精度等则需另行处理已有索引，不自动重新向量化。

## SQL、只读和稳定排序

复用 [pgvector SQLAlchemy cosine_distance](https://github.com/pgvector/pgvector-python#sqlalchemy)，不是 PGVectorStore 或 VectorStoreIndex。所有过滤先进入 SQL：

- Chunk/Document/KnowledgeBase 均属于指定 kb_id，关联键同时使用 doc_id/kb_id；知识库 ACTIVE、文档 READY、embedding 非空。
- 指定doc_ids时，Chunk.doc_id IN参数化ID列表与上述条件取交集，先过滤候选再计算Top-K。
- index_config.version 为现有 pgvector-index-v1，result.model 与 Query 完整身份 JSON 相等。
- index_task_id 非空，冻结 embedding_task_id/source_chunk_task_id/source_parse_task_id 与当前 Document 标识相同；Chunk.source_parse_task_id 同当前解析代次。

未知身份/历史失效代次不会偷偷补向量、恢复任务或跨库搜索。返回行还会防御性检查上述来源及距离/顺序，任意坏行/重复 ID/非法距离拒绝整个结果，不部分成功。已 READY 文档的全量有效单位向量仍由阶段 15 事务和阶段 16 人工验收保证；管理员绕过应用直接 SQL 破坏数据不在本模块完整性保证内。

首版为**精确基线**：只把符合条件的 chunk_id/embedding 放入 MATERIALIZED CTE，计算 `<=>` 后按距离升序、UUID chunk_id 升序取 Top-K，再只取这些结果的正文/证据字段。候选不经过 HNSW 的近似截断，避免过滤后不足 Top-K 或同分候选不稳定。保留阶段 15 HNSW 索引，但本版本不声称走 ANN，也不自动改索引/扩展/查询参数；大库扫描/物化可能占用 CPU/内存/临时磁盘，性能需单独人工验收。

同一数据快照及相同 Query 向量排序稳定。并发导入/维护改变候选或不同 GPU/精度的数值差异，不能承诺跨时刻结果完全相同。结果表示当前 SQL 快照，查询结束后的 Reindex/删除/状态变化不由长时间行锁阻止。

会话由工厂每次新建，默认关闭 autoflush，无已有事务或待写变更；显式 `SET TRANSACTION READ ONLY`、statement_timeout=10s、lock_timeout=2s。仅 SELECT，无 flush/commit/业务写入，会话退出回滚只读事务。不要传递带修改或预先开始事务的 Session。SQL/连接/超时错误固定为 DENSE_DATABASE，不自动重试；模型错误也不自动重新部署或降级 CPU。

返回 score 为 **1-cosine_distance**，允许零和负值，不是概率/回答置信度。Node.id_=chunk_id，SOURCE=原 Document；正文、file_name、kb_id/doc_id、页范围、section、完整 chunk_metadata/source_spans/page_semantics 与模型身份通过既有 Adapter 深复制。默认不将数据库向量放进返回 Node（include_embedding=False），不读取原文件或完整私有部署配置。

## 日志、依赖与安全

采用既有 UTF-8 中文白名单每日日志，dense.started/finished/empty/failed 仅 kb_id、top_k、数量/维度、阶段、耗时和安全错误码；指定范围时复用source_document_count，单文档另记doc_id，不输出ID数组。不记录 Query、正文、Node、向量、SQL、连接串、模型目录或异常原文。空结果是一个正常结果，不解释成语义上“没有答案”。

遵循 [LlamaIndex BaseRetriever 抽象](https://developers.llamaindex.ai/python/framework-api-reference/retrievers/)，但复写公共 retrieve/aretrieve，避免基类含 Query/Node 的 callback payload、递归检索和默认 Query Engine。显式空 CallbackManager、不改全局 Settings/LLM/Tracing；第三方全局 instrumentation 或数据库 echo/debug 若被调用方开启仍可能记录参数，必须禁用未经审查的追踪导出，不能把第三方输出当作项目脱敏日志。

仅已有 llama-index-core 0.14.x、SQLAlchemy、pgvector、BGE-M3 即可；本轮没有安装或升级依赖。core 首次导入的 NLTK 缓存风险仍见 [15A](LLAMA_INDEX_ADAPTER.md)，由用户准备，不能让 Codex 为执行测试临时联网补资源。GPU Worker 和 Query 进程不跨进程共享模型；8GB 显存验收建议先停止 GPU Worker，避免两份 BGE-M3 同卡占用。

## Mock 验收：用户执行，无真实数据要求

Codex 只编写用例，不运行。SQL 测试只编译表达式/返回内存 ORM 行；框架测试用 Query/Session Stub，不启动数据库、解析原件、加载模型、投递任务或调用云端。

```powershell
conda activate dkf-agent
python -m pytest tests/test_dense_sql.py tests/test_dense_retriever.py -q
# 可选：用户准备好原有依赖/core缓存后，检查相邻阶段回归。
python -m pytest tests/test_llama_index_adapter.py tests/test_indexing.py tests/test_business_logging.py -q
```

core 缺失会跳过框架测试，跳过不是通过。预期覆盖过滤先于距离/LIMIT、单/多文档参数绑定、None兼容、空/非法/超限范围拒绝、输入列表冻结、越界行全批拒绝、空结果不回退、同步/异步范围及完整Node/DTO证据；原有知识库/READY/模型/代次、排序、只读、无文档编码和脱敏用例保留。Stub 不证明 PostgreSQL 真执行计划、真实语义相关性、GPU 或本地模型已可用。

## PostgreSQL + BGE-M3 真实验收：阶段 16 READY 数据具备后由用户执行

可以先按 [阶段16/17 三数据集联合冒烟](PRESET_IMPORT.md#三数据集最短操作新运行)，在独立 ACTIVE 测试库用已有 OHR PDF/官方QA、Omni GT图像及 OCB DOCX/XLSX/PPTX各2份走真实上传→READY，再执行 `smoke_stage16_17.py --step retrieve`。只调用这里的 Dense，不新增 API/Query Engine；OHR/OCB分别统计文档 Hit@1/5/10、MRR@20，OHR另查可靠物理页。OCB保留完整 reference_files/expected_assertions，多文件要求依赖齐全；断言与答案证据为manual_pending，不以字符串包含判断正确。Omni GT不参与QA分母，异常/未执行不伪填零分。无需手工SQL导出，不宣告阶段16正式或官方完整质量评测通过。

以下保留独立检查方式：先按 [阶段 16](PRESET_IMPORT.md) 完成合法素材、许可/外传/云预算确认及对应 READY 全量向量验收。无需为了阶段 17 再解析/Embedding/迁移，旧模型身份不匹配不能为了测试改元数据冒充一致。

1. 用户在 pgAdmin 只读查看目标库（替换 UUID），确认已有 READY/非空向量及一致模型，勿导出向量正文：

```sql
SELECT d.doc_id,d.file_name,d.kb_id,d.status,
       d.index_config->'result'->'model' AS model_identity,
       count(c.chunk_id) AS chunk_count,count(c.embedding) AS vector_count
FROM documents d LEFT JOIN document_chunks c
ON c.doc_id=d.doc_id AND c.kb_id=d.kb_id
WHERE d.kb_id='知识库UUID'::uuid
GROUP BY d.doc_id,d.file_name,d.kb_id,d.status,d.index_config->'result'->'model';
```

预期目标 READY 行有向量，身份字段完整；阶段 16 的每文档 chunk_count=vector_count>0、1024 维/归一化和来源验证仍不可跳过。没有 READY 数据或身份缺失时停止，不把空检索当验收通过。

2. 用户确认本地 BGE-M3 已部署且与入库版本/精度/长度一致，GPU Worker 未同卡占用，core 缓存已准备。检索不需要 API/CPU/GPU Worker，只连接已运行的 PostgreSQL。用户替换 kb_id/query 后手动运行，不打印完整正文/向量：

```powershell
$denseReadCheck = @'
# 用户运行：只检索已 READY 数据，没有新任务、原文件读取或文档向量化。
from uuid import UUID
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.llama_index.dense_retriever import DenseRetriever
from app.services.rag.llama_index.node_adapter import node_to_result

configure_logging(get_settings(), role='dense-cli')
kb_id = UUID('你的知识库UUID')
doc_ids = [UUID('目标READY文档UUID')]
retriever = DenseRetriever(kb_id, doc_ids=doc_ids, top_k=20)
# 全库基线省略 doc_ids；多文档验收在同一列表添加另一个真实文档 UUID。
query = '你从已入库原文或官方QA选择的真实问题'
nodes = retriever.retrieve(query)
assert nodes, '本次真实验收需已有同模型 READY 证据，空集不视为成功'
assert len(nodes) <= 20
results = [node_to_result(node) for node in nodes]
assert all(result.kb_id == kb_id and result.doc_id in doc_ids and result.metadata.get('source_spans') for result in results)
assert all(node.node.metadata['doc_id'] in {str(value) for value in doc_ids} and
           node.node.node_id == node.node.metadata['chunk_id'] for node in nodes)
assert all(nodes[index].score >= nodes[index + 1].score for index in range(len(nodes) - 1))
print('count=',len(results),'same_kb=',True,'same_document_scope=',True,'evidence_preserved=',True)
print([(str(result.chunk_id),str(result.doc_id),result.page_start,result.page_end,result.retrieval_score) for result in results])
'@
python -c $denseReadCheck
```

预期返回 ≤20 项、kb_id 一致、距离升序/score 降序且原始 Chunk ID/页码/章节/证据可反查，不出现新文档编码、任务或 SQL 写入。第一次 Query 可能花时间加载/校验权重，模型文件缺失或显存错误必须安全失败，不自动下载或 CPU 回退。CLI 的日志角色按现有格式器显示“未知进程”，由 event/pid/kb_id 关联，不冒充 API/Worker。

3. 通过已有 `/chunks` 或 pgAdmin 只读核对每个返回 chunk_id 的正文、文件名、页码/section/source_spans，查所属 Document=READY、index_config.result.model 与本地 Query 一致。使用确定具有证据的真实问题人工判断相关性，Top-K 的存在或同文件并不证明 Retrieval/RAG 准确率；不生成回答。

4. 同数据快照重复相同 Query，预期ID/顺序稳定；分数可能有硬件数值微差。在已有同库A/B两份READY文档上：限定[A]后用B相关问题查询，可返回A或空，但不能混入B；限定[A,B]只能返回这两份；换成已核实不存在或属于其他库的文档UUID，应为空，不扩大范围。省略doc_ids仍保留全库模式。负向状态/模型篡改优先使用Stub，不污染共用库或正在处理的文档。

5. 记录检索前后目标库 Document/Chunk 数量、解析/切片/Embedding/Index task_id 和 MinerU batch，预期没有变化或额外收费；用户并发导入造成的变化须区分，不能归咎检索。日志每天同一文件，出现 dense.started 与 finished/empty/failed 的固定摘要，无 Query/正文/向量/SQL。Stage 16/14/15 的真实模型、数据库全量入库和费用待验收事项继续保留。

验收由用户执行并反馈通过，Codex未代跑上述命令、pytest、Smoke、数据库、GPU、迁移、Parser或云API，未生成测试文档或下载素材。此处不补造测试数量或日志；阶段19等待用户授权。
