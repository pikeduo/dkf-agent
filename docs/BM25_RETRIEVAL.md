# BM25 Retriever：本地关键词与人工验收

阶段33 v4补充：当前已索引语料包括READY/PARTIAL_READY的可靠块，范围和当前代次过滤不变；质量随原Node保留，派生缓存仍可重建，部分覆盖不冒充完整READY或完整评测通过。

阶段19代码已实现，单测仅编写、真实检索与性能待用户验收；阶段18用户验收结论保留，阶段16/17等其他待验收事项不自动改变。本阶段不进入Query Fusion、Reranker、LLM或完整RAG。

## 入口与数据边界

`app.services.rag.llama_index.bm25_retriever.BM25Retriever` 继承现有BaseRetriever：

- `BM25Retriever(kb_id, doc_ids=None, top_k=20)`：构造仅校验参数，不读库/建索引；Top-K为1～100，文档范围沿用阶段18的1～100个UUID列表/元组。None表示全库，显式空/非法范围拒绝。
- `retrieve(str / QueryBundle)` 返回NodeWithScore；`aretrieve` 使用同进程线程及独立会话；`retrieve_results` 返回项目证据DTO。
- QueryBundle仅接受原始query_str，不接受外部向量、变换文本或图片路径；空Query/非法控制符拒绝。无词项命中返回空集，不凑Top-K、不扩大知识库或文档范围。
- 无新HTTP API、Worker、表、迁移、模型、API Key或环境变量。只需现有PostgreSQL、core/NLP缓存及已声明rank-bm25，无GPU、BGE-M3、Redis或云调用。

来源固定为 **document_chunks → 逐行校验/ID与正文 → 本地BM25派生索引 → 命中证据NodeWithScore**。只读SQL限定Chunk/Document同kb_id、ACTIVE库、READY/PARTIAL_READY、可选doc_ids，且当前解析/切片/Embedding/Index代次一致；条件在语料LIMIT前下推。查询不读取embedding列、原件或私有模型部署配置，不绑定Query模型或产生新向量。BM25不比较当前Query的BGE模型身份，因为没有Query向量；返回证据仍保留已有入库模型身份。

只读会话禁用autoflush，显式READ ONLY、REPEATABLE READ、statement_timeout=10s、lock_timeout=2s。全库扫描和命中证据取回处于同一个查询级快照，查询结束释放会话，不跨题持有事务。越界/旧代次/未索引返回行全批拒绝，无写入/自动重试/旧缓存降级。结果表示SQL查询快照，不保证查询返回后文档仍可检索；正式HTTP入口的用户权限不由本阶段实现。

原Node.id_=chunk_id、SOURCE=doc_id、kb_id/doc_id/chunk_id/file_name、page_start/page_end/section和完整chunk_metadata/source_spans/page_semantics均由既有Adapter深复制；不伪造物理页，不把元数据前缀当正文，不随机生成ID。不返回数据库向量。

## 分词、评分与派生缓存

使用已有rank-bm25 **BM25Plus**，固定k1=1.5/b=0.75/delta=1，避免单Chunk/高频词的负IDF边界。逐块提供词项列表的迭代器，不使用其tokenizer回调（该回调会启动进程池）。分数不是概率、语义置信度或Dense余弦分数，不能直接相加。

- NFKC与英文大小写归一化只用于词项，不改Node原文。数字、百分比、BGE-M3/RX-7800-XT等内部连接符型号保持完整词；数字千分位逗号去除，不转浮点数或改公式名称。
- 中文采用字符+相邻二元片段，无jieba/外部分词模型。支持关键词基线，不宣称专业分词、语义同义词或专有名词消歧；中文长词可能部分匹配，须人工核对。欧洲小数、特殊Unicode符号和复杂单位规范化仍有边界。
- 只对实际词项重叠的Chunk计分，避免BM25Plus背景delta分把零命中Chunk当Evidence。同分按原chunk_id UUID升序稳定排序。
- 每个Retriever最多保留一个进程内词频索引。每题仅执行一次完整资格筛选，服务端游标按UUID递增产出候选ID；每次取至多256个明确ID，先按主键物化本批摘要和资格检查所需的完整Chunk列（含text/metadata，不含embedding），再复核范围并计算原文字节数、正文SHA256和完整来源SHA256。外层只读取批次CTE，不重新连接整张Chunk表，使来源JSON的资格检查和Hash都限定在本批。避免每页反复扫描/排序全库候选，不用OFFSET，也不一次物化全库ID列表。候选游标和Hash批次在同一连接、同一REPEATABLE READ事务中，逐行严格核对返回ID、顺序和数量；缺行/重复/错序即失败。摘要配方、UUID顺序和累计行数/字节预算保持；摘要覆盖全部Adapter输入、完整JSONB元数据及当前代次，由PostgreSQL 17内置函数计算，无需扩展/迁移。不能只凭数量、任务ID或命中项判断全库未变。
- 首次或摘要变化时，在同一REPEATABLE READ快照逐行完整Adapter校验，临时Node随行释放，正文/ID必须与摘要一致；完全相同且此前已验证的摘要才复用验证结果。仅元数据变化重新验证，但正文未变可保留词频。热查询不传输/解码全库正文和JSON，评分后只读Top-K完整证据，并核对ID/正文Hash与当前摘要；缺失或改变整批失败，未命中项损坏也会使摘要失效。Node不缓存，检索范围不缩减。
- 建索引只持有一块临时词项，相同词项字符串在本次构建共享，不积攒整库词项数组或使用全局永久词表。失效索引先释放再重建；并发请求已经持有的索引会在其查询结束后释放。Top-K使用有界堆，原评分与同分UUID规则保持。数据库查询/Adapter/构建失败不返回旧缓存。
- 不保存磁盘索引、不使用pickle、不新建业务表或第二份Node真相；重启后自然重建。每题仍核对全部合格语料摘要，冷启动另需完整读取/校验；首次构建和数据库摘要扫描仍有CPU/内存成本，吞吐待验收。

首版安全预算：最多10,000个Chunk（SQL多读1条检测超限）、语料正文32 MiB UTF-8、4,000,000个派生词项，Query最多32768字符/512个派生词项。超限返回BM25_LIMIT，不截断成不完整检索。可用doc_ids缩小范围；预算不是进程总内存上限，ORM/证据元数据/词频还有额外内存成本。大库增量或持久化缓存应另行设计，不在本阶段自动展开。

## 依赖及日志

已有environment.yml声明rank-bm25>=0.2.2,<0.3；本轮只在已确认的dkf-agent环境补装0.2.2，不升级其他依赖。其他机器先检查，缺失才手动安装：

```powershell
conda activate dkf-agent
python -m pip show rank-bm25 llama-index-core
# 仅rank-bm25缺失时执行，不重建环境或安装全量LlamaIndex集成。
python -m pip install "rank-bm25>=0.2.2,<0.3"
```

core首次导入仍可能检查NLTK缓存，由用户按[Adapter说明](LLAMA_INDEX_ADAPTER.md#core-首次导入资源边界)预先准备，不由检索器安装/下载。未安装依赖报BM25_DEPENDENCY，不自动联网补齐。

数据库错误采用`BM25_DATABASE_QUERY_CANCELED / BM25_DATABASE_LOCK_UNAVAILABLE / BM25_DATABASE_CONNECTION / BM25_DATABASE_RESOURCE`及原通用码。首次执行、流式拉取或关闭游标捕获SQLSTATE；关闭次生错误不覆盖首次原因。57014不单独证明语句超时，未知状态不猜测。摘要失败日志沿用`batch_index / chunk_count`标明失败批次和此前已检查的行数，`operation=SOURCE_IDS / SOURCE_HASH_BATCH`区分候选读取和Hash批次；整题失败，不返回半份摘要或旧缓存，不记录ID列表。10秒语句及2秒锁等待预算保持不变。日志`step`仅保留SQLSTATE；`index_built/index_reused`的`SOURCE_VALIDATED / SOURCE_VALIDATION_REUSED`区分本次完整校验与摘要相同的验证复用。

复用每日中文日志：bm25.started、snapshot_finished、index_built/index_reused、finished/empty/failed。`snapshot_finished.duration_ms`单列全量摘要耗时；索引事件的`duration_ms`单列锁等待、完整验证和索引构建/复用耗时，最终事件为整题总耗时；不逐批刷成功日志。只记录UUID/范围数量/Chunk数/复用/耗时/安全码，不写词项、Query、正文、Node、SQL、索引摘要或异常原文。BM25批量Node转换使用`log_success=False`，只省略逐项成功日志，错误仍记；原Adapter调用默认行为不变。SQL echo/第三方Tracing仍必须禁用。

## 用户执行单测

```powershell
conda activate dkf-agent
python -m pytest tests/test_bm25.py tests/test_bm25_retriever.py tests/test_hybrid_retriever.py -q -ra
# 验证相邻已实现行为不变：
python -m pytest tests/test_dense_sql.py tests/test_dense_retriever.py tests/test_llama_index_adapter.py tests/test_business_logging.py -q
```

覆盖数字/型号/缩写/中文片段、零重叠、单Chunk、排序/同分、范围及READY/代次SQL、Node/DTO完整证据、无Embedding、惰性/异步、内容/集合/元数据变化、来源失败不降级旧缓存、超限和日志脱敏。分批用例另覆盖单次ID枚举、全部来源列先物化且外层无原Chunk表连接、摘要字段映射/完整范围保留、空库/整批/末批边界、跨批累计预算、错序/缺失/重复行、末批来源变化、多批热查询、阶段计时及后续批次取消的SQLSTATE/安全进度。仅内存字段/词项/Session Stub，无真实素材、数据库、Parser或模型；core缺失跳过不算通过。Codex没有执行上述用例，两个服务端游标同连接的真实驱动兼容性和性能仍待用户验证。

## 最小真实验收（PowerShell，仅用户执行）

已有READY文档即可，不重复上传/解析/向量化。保持PostgreSQL运行；替换query为目标文档中真实存在的型号、数字、缩写或专有名词，不保留占位文字：

```powershell
conda activate dkf-agent
$bm25ReadCheck = @'
from uuid import UUID
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.llama_index.bm25_retriever import BM25Retriever

# 只读当前READY数据，输出标识/页码，不打印正文或向量。
configure_logging(get_settings(), role='bm25-cli')
kb_id = UUID('你的知识库UUID')
doc_id = UUID('目标READY文档UUID')
query = '替换为原文中的真实关键词'
retriever = BM25Retriever(kb_id, doc_ids=[doc_id], top_k=20)
nodes = retriever.retrieve(query)
assert nodes, '已知真实关键词未命中，需要核对原文、READY及分词边界'
assert all(n.node.metadata['kb_id'] == str(kb_id) and n.node.metadata['doc_id'] == str(doc_id)
           and n.node.node_id == n.node.metadata['chunk_id']
           and n.node.metadata['chunk_metadata'].get('source_spans') for n in nodes)
assert all(nodes[i].score >= nodes[i + 1].score for i in range(len(nodes) - 1))
again = retriever.retrieve(query)
assert [n.node.node_id for n in nodes] == [n.node.node_id for n in again]
print('count=', len(nodes), 'scope_ok=', True, 'repeat_stable=', True)
for n in nodes:
    m = n.node.metadata
    print(m['doc_id'], m['chunk_id'], m['page_start'], m['page_end'], n.score)
'@
python -c $bm25ReadCheck
```

预期1～20条真实命中、原ID/页码/证据保留，第二次查询有index_reused；没有模型权重加载、Embedding日志、新任务/费用或数据库写入。只检查结构/重复稳定性不能证明语义正确；按返回Chunk只读核对实际数字/型号/表格上下文。

依次验证数字（如原文实际数值及百分比）、型号、专有名词、英文缩写、精确关键词；不要编造官方QA。已确认不在语料中的罕见英文完整词应空，不因背景分返回其他证据；中文片段可能部分匹配，不能把它当严格全词等号。多文档/全库模式同样不跨kb_id；非READY/其他库文档由SQL排除。状态变更/缓存失效负向优先用Stub，不为测试篡改生产状态。

保存测试反馈后再确认阶段19通过；小样本关键词命中不是LLM问答准确率或官方基准总分，不自动进入阶段20。

## 阶段33全量预算补充

生产默认语料上限不变。离线正式评测可向`BM25Retriever(..., corpus_limits=CorpusLimits(...))`显式传入实例资源预算，复用同一SQL/词项/评分/Node边界；最多20万Chunk、512MiB原文、6400万词项。上限仅用于拒绝过大语料，不按前N截断或修改全局常量。正式命令/预算及未验收事项见[阶段33](STAGE33_EVALUATION.md)，无新依赖、迁移或环境变量。

本轮改动减少全库ORM/证据Node与临时词项的同时驻留，不降低检索范围、分词预算或证据完整性。进程仍需保存词频索引，阶段33还会加载两个本地模型；不能承诺固定GB上限。实际内存、耗时及PostgreSQL驱动流式兼容性待用户运行确认。正在运行的Python不会自动加载修改；用户应在当前题结束后停止旧评测进程，再按相同run/抽样参数与`--resume`启动新进程，已成功题保留。
