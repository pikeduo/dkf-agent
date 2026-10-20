# Hybrid Retrieval：Dense + BM25 稳定 RRF

阶段26补充：[跨语言分阶段诊断与平行表达实验](STAGE26_SUPPLEMENT.md#工作项二跨语言检索定位与受控实验)复用原检索/重排，默认只预览；生产Hybrid仍保持本文件的原Query两支等权RRF。尚未据真实通用数据决定自动扩展策略，不把单题或Mock改善宣称为BGE质量提升。

阶段20代码已实现，测试仅编写、真实验收待用户执行。阶段19的关键词空结果仍需核对真实Query；阶段16正式验收及此前待办不因本阶段开发自动通过。不进入21 Reranker、LLM、完整RAG或HTTP检索接口。

## 入口与排序

`app.services.rag.llama_index.hybrid_retriever.HybridRetriever` 继承LlamaIndex BaseRetriever，组合现有DenseRetriever和BM25Retriever，而非重做两路SQL/Embedding/索引。

- `HybridRetriever(kb_id, doc_ids=None, top_k=20, branch_top_k=20, rrf_k=60)`：惰性构造；两路范围相同，输入文档列表冻结。top_k/branch_top_k严格整数1～100，rrf_k严格整数1～1000。最终可设top_k=30，但不凑满不存在的候选。
- 内部Python构造可显式传`corpus_limits=CorpusLimits(...)`，只交给原BM25实例；阶段34沿用阶段33已冻结的大库预算。省略时生产默认不变，不截断语料、不改变SQL范围或融合分；HTTP配置不变。
- `retrieve(str / QueryBundle)`返回NodeWithScore；`aretrieve`在线程内串行执行同一入口，独立短会话，不共享线程Session；`retrieve_results`返回项目DTO。
- 仅原query_str；禁止外部向量、变换Query或图片。字符/词项预算复用两路校验，BM25词项超限在Query模型调用前拒绝。Dense仅编码一次Query，BM25不编码；不重新处理文档。
- 每路各Top20，排名从1开始。等权RRF：`fusion_score = Σ 1 / (rrf_k + branch_rank)`，不相加余弦/BM25原分数，不做分数归一化或Query Rewrite。以原chunk_id去重，融合分降序、同分UUID升序；正文相同但chunk_id不同仍独立。
- NodeWithScore.score为fusion_score；`node.metadata['fusion']`包含strategy、rrf_k、sources、dense_rank/dense_score、bm25_rank/bm25_score、fusion_score。未命中的分支为None，sources固定dense→bm25顺序。分数均不是概率或问答准确率。

这里采用框架抽象下的等价RRF，不直接使用QueryFusionRetriever：其[官方实现](https://developers.llamaindex.ai/python/framework-api-reference/retrievers/query_fusion/#llama_index.core.retrievers.QueryFusionRetriever)构造会解析默认LLM，融合使用node.hash。项目需要不初始化LLM、按业务chunk_id去重、保存各路原分数、避免原Node被改写及含正文callback，故实现受保护的组合边界。RRF使用明确的1-based公式，不声称与框架默认0-based分数逐值相等。

## 证据、安全与失败

原id_/SOURCE、正文、kb_id/doc_id/chunk_id/file_name/page_start/page_end/section、完整chunk_metadata/source_spans/embedding_identity深复制保留，不伪造物理页。仅追加运行时fusion并从框架Embedding/LLM文本排除；DTO新可选字段fusion保留来源，数据库模型/Chunk metadata不变，普通Node兼容原契约。

两路分别继承既有知识库ACTIVE/READY/代次/指定doc_ids SQL约束；Dense另核对Query模型身份。BM25-only候选是关键词来源，不声称它通过Dense模型过滤或具有相容Query向量。无新增API/表/迁移/模型/Worker/环境变量/依赖，不需改装全量llama-index；部署复用[Dense](DENSE_RETRIEVAL.md)、[BM25](BM25_RETRIEVAL.md)。检索只需已有PostgreSQL、core/NLP缓存、rank-bm25及匹配的本地BGE-M3，不需要API/Worker/Redis或云端。

首版串行调用两支，两次独立只读SQL快照；不是跨支共享REPEATABLE READ。重复chunk_id的正文/元数据/SOURCE不一致返回FUSION_CONFLICT，不挑一份、不自动重试。未在两支重复出现的并发状态变化无法通过融合层探测；输出仅代表各自查询快照，返回后仍READY不作保证。真实GPU数值及数据变化可能影响跨时刻顺序。

正常一支空集可融合另一支；均空返回[]，不扩库/重建。任一路异常整体失败（FUSION_DENSE/FUSION_BM25），不把异常当空集降级。分支超量、重复ID、越界、乱序、无效分数/证据或嵌套fusion全批拒绝FUSION_RESULT。相同Chunk两支各贡献一次；不接受单支自身重复抬分。

沿用每日中文日志fusion.started、branch_finished、finished/empty/failed；只记UUID、范围/候选数、分支阶段、耗时和固定安全码。上游已知错误保留为error_type；任意异常原文不外泄。没有全局LLM/Tracing、框架递归Query Engine；SQL echo和外部instrumentation仍需禁用。耗时记录是支路整体时间（Dense含Query编码）及融合入口总时长，不冒充纯SQL计时。

## 用户执行Mock验收

```powershell
conda activate dkf-agent
python -m pytest tests/test_hybrid_retriever.py -q
# 相邻回归，由用户选择运行；不连接真实数据库或模型。
python -m pytest tests/test_dense_sql.py tests/test_dense_retriever.py tests/test_bm25.py tests/test_bm25_retriever.py tests/test_llama_index_adapter.py tests/test_business_logging.py -q
```

覆盖RRF正确性/分数尺度独立、Top-K/同分稳定、跨支去重/同文本不同ID、来源解释/完整证据/深复制/DTO互转、正常空支、异常不降级、冲突/越界/坏分数、输入预算、原Query两路各一次、一次共享Query服务编码、范围下推及独立只读会话、同步/异步和日志脱敏。纯内存Node/Session/Query Stub，不生成测试原件；core/rank-bm25缺失跳过不是通过。Codex未执行这些测试。

## 用户执行真实只读验收

已有目标READY文档即可，不重复上传、Parse/Chunk/Embedding/Index；先确认其证据及向量完整、Query模型身份匹配，PostgreSQL已运行。停止同GPU的GPU Worker，避免8GB显存加载两份模型；无需启动新的服务、执行迁移、准备云密钥或调用MinerU。

在项目根目录PowerShell执行，输入真实关键词/官方QA，不保留示例占位文字：

```powershell
conda activate dkf-agent
$hybridReadCheck = @'
from uuid import UUID
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
from app.services.rag.llama_index.node_adapter import node_to_result

configure_logging(get_settings(), role='hybrid-cli')
kb_id = UUID(input('知识库UUID：').strip())
doc_id = UUID(input('目标READY文档UUID：').strip())
query = input('原文中真实存在的关键词或官方问题：')
retriever = HybridRetriever(kb_id, doc_ids=[doc_id], top_k=30)
nodes = retriever.retrieve(query)
assert nodes, '请先核对READY、模型身份和真实关键词；空结果不视为本次验收通过'
assert len(nodes) <= 30 and len({n.node.node_id for n in nodes}) == len(nodes)
assert all(nodes[i].score >= nodes[i + 1].score for i in range(len(nodes) - 1))
for node in nodes:
    result = node_to_result(node)
    assert result.kb_id == kb_id and result.doc_id == doc_id
    assert node.node.node_id == str(result.chunk_id) and result.metadata.get('source_spans')
    assert result.fusion.fusion_score == node.score
    print(result.doc_id, result.chunk_id, (result.page_start, result.page_end), result.fusion.model_dump())
again = retriever.retrieve(query)
assert [n.node.node_id for n in nodes] == [n.node.node_id for n in again]
print('count=', len(nodes), 'unique=', True, 'scope=', True, 'stable=', True)
'@
python -c $hybridReadCheck
```

预期不超过30条、同一chunk_id只出现一次、kb/doc隔离、原页码/证据可反查；fusion来源可为dense、bm25或两者。首次Query按既有服务加载本地模型，重复查询仍每次编码一次；稳定断言以数据/模型不变为前提。不要强求每个候选两路都命中；BM25词项不匹配时允许dense-only，需单独按阶段19核对真实词项。

另用同库两份已有READY文档限定doc_ids验证并集、用已核实跨库/不存在的doc_id验证空集，不用篡改生产数据做负向测试。核对返回Chunk原正文、表格/公式上下文和页码；检索前后Document/Chunk数、各task_id及MinerU batch不得因检索变化。每日日志仅新增检索摘要，无收费调用。Top-K存在不证明相关性/LLM回答或官方基准全量通过；阶段16正式素材覆盖、费用、质量及此前真实模型/回归验收仍待用户完成。

反馈Mock结果及真实候选来源/排序后再确认阶段20通过；本轮不自动进入21。
