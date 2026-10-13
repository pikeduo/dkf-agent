# BM25 Retriever：本地关键词与人工验收

阶段19代码已实现，单测仅编写、真实检索与性能待用户验收；阶段18用户验收结论保留，阶段16/17等其他待验收事项不自动改变。本阶段不进入Query Fusion、Reranker、LLM或完整RAG。

## 入口与数据边界

`app.services.rag.llama_index.bm25_retriever.BM25Retriever` 继承现有BaseRetriever：

- `BM25Retriever(kb_id, doc_ids=None, top_k=20)`：构造仅校验参数，不读库/建索引；Top-K为1～100，文档范围沿用阶段18的1～100个UUID列表/元组。None表示全库，显式空/非法范围拒绝。
- `retrieve(str / QueryBundle)` 返回NodeWithScore；`aretrieve` 使用同进程线程及独立会话；`retrieve_results` 返回项目证据DTO。
- QueryBundle仅接受原始query_str，不接受外部向量、变换文本或图片路径；空Query/非法控制符拒绝。无词项命中返回空集，不凑Top-K、不扩大知识库或文档范围。
- 无新HTTP API、Worker、表、迁移、模型、API Key或环境变量。只需现有PostgreSQL、core/NLP缓存及已声明rank-bm25，无GPU、BGE-M3、Redis或云调用。

来源固定为 **document_chunks → TextNode → 本地BM25派生索引 → NodeWithScore**。只读SQL限定Chunk/Document同kb_id、ACTIVE库、READY、可选doc_ids，且当前解析/切片/Embedding/Index代次一致；条件在语料LIMIT前下推。查询不读取embedding列、原件或私有模型部署配置，不绑定Query模型或产生新向量。BM25不比较当前Query的BGE模型身份，因为没有Query向量；返回证据仍保留已有入库模型身份。

只读会话禁用autoflush，显式READ ONLY、statement_timeout=10s、lock_timeout=2s。越界/旧代次/非READY返回行全批拒绝，无写入/自动重试/旧缓存降级。结果表示SQL查询快照，不保证查询返回后文档仍READY；正式HTTP入口的用户权限不由本阶段实现。

原Node.id_=chunk_id、SOURCE=doc_id、kb_id/doc_id/chunk_id/file_name、page_start/page_end/section和完整chunk_metadata/source_spans/page_semantics均由既有Adapter深复制；不伪造物理页，不把元数据前缀当正文，不随机生成ID。不返回数据库向量。

## 分词、评分与派生缓存

使用已有rank-bm25 **BM25Plus**，固定k1=1.5/b=0.75/delta=1，避免单Chunk/高频词的负IDF边界。手动提供词项列表，不使用其tokenizer回调（该回调会启动进程池）。分数不是概率、语义置信度或Dense余弦分数，不能直接相加。

- NFKC与英文大小写归一化只用于词项，不改Node原文。数字、百分比、BGE-M3/RX-7800-XT等内部连接符型号保持完整词；数字千分位逗号去除，不转浮点数或改公式名称。
- 中文采用字符+相邻二元片段，无jieba/外部分词模型。支持关键词基线，不宣称专业分词、语义同义词或专有名词消歧；中文长词可能部分匹配，须人工核对。欧洲小数、特殊Unicode符号和复杂单位规范化仍有边界。
- 只对实际词项重叠的Chunk计分，避免BM25Plus背景delta分把零命中Chunk当Evidence。同分按原chunk_id UUID升序稳定排序。
- 每个Retriever最多保留一个进程内词频索引。每次查询先读当前READY快照、重新派生Node；相同Chunk集合/原文/算法版本及范围可复用词频。原文、Chunk集合或版本改变重建；元数据改变用本次新Node，不返回缓存Node。数据库查询/Adapter/构建失败不返回旧缓存。
- 不保存磁盘索引、不使用pickle、不新建业务表或第二份Node真相；重启后自然重建。每次仍需读取合格语料，首次构建及大库读取有CPU/内存成本，吞吐待验收。

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

复用每日中文日志：bm25.started、index_built/index_reused、finished/empty/failed。只记录UUID/范围数量/Chunk数/复用/耗时/安全码，不写词项、Query、正文、Node、SQL、索引摘要或异常原文。BM25批量Node转换使用`log_success=False`，只省略逐项成功日志，错误仍记；原Adapter调用默认行为不变。SQL echo/第三方Tracing仍必须禁用。

## 用户执行单测

```powershell
conda activate dkf-agent
python -m pytest tests/test_bm25.py tests/test_bm25_retriever.py -q
# 验证相邻已实现行为不变：
python -m pytest tests/test_dense_sql.py tests/test_dense_retriever.py tests/test_llama_index_adapter.py tests/test_business_logging.py -q
```

覆盖数字/型号/缩写/中文片段、零重叠、单Chunk、排序/同分、范围及READY/代次SQL、Node/DTO完整证据、无Embedding、惰性/异步、内容/集合/元数据变化、来源失败不降级旧缓存、超限和日志脱敏。仅内存字段/词项/Session Stub，无真实素材、数据库、Parser或模型；core缺失跳过不算通过。Codex没有执行上述用例。

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
