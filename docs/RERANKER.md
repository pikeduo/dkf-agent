# BGE Reranker：本地部署与人工验收

阶段21代码已实现，测试仅编写；真实模型兼容、显存/耗时、排序收益及此前阶段验收仍待用户完成。不开发DeepSeek、Query Engine/HTTP问答或阶段22。没有生成素材、加载模型或执行验收。

## 实现与分数边界

- `app.services.reranking.RerankerService`是项目模型管理边界；`get_reranker_service()`在当前进程按冻结配置/PID共享惰性实例，锁内同步批处理。Adapter不构造框架内置FlagEmbedding/SentenceTransformer Reranker，不加载另一份同模型。
- 使用已有Transformers的AutoTokenizer/AutoModelForSequenceClassification，沿用[BAAI官方模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)中的单标签分类路径。固定本地目录、`local_files_only=True / trust_remote_code=False / use_safetensors=True`；模型缺失/坏结构/CUDA不可用明确失败，无自动下载/CPU回退。检查加载信息，缺失/不匹配的分类头不得随机初始化后冒充官方模型。
- `score(query, passages)`顺序对应返回原始有限logit；不是向量、不做sigmoid、不当概率。输入为原Query及完整正文，不加Query指令、不混入metadata、不裁剪。真实成对token长度包含特殊token，超过配置上限返回RERANK_TOO_LONG；模型实际单标签输出和模型文件身份均校验，错误不返回半份结果。
- 只重排当前调用方已经取得的候选，不再检索/查询数据库/解析原件/重Embedding/改变状态，不能证明调用后Document仍READY或替代权限验证。输入重复Chunk、跨库、坏身份、无原分、已重排或超过100项均安全拒绝。
- `BGERerankerAdapter(top_n=5).postprocess_nodes(nodes, query_str=query)`默认同步；显式`apostprocess_nodes`仅同进程线程包装，不创建Worker。Top5不足不补候选，logit降序/同分UUID升序；负分/零分不丢弃，不设置任意“可信回答”阈值。
- Node当前score为rerank_score；Node额外`metadata['rerank']`包含原retrieval_score、fusion_score、rerank_score、input_rank、模型指纹/版本/精度/长度/score_type。原fusion字段及两路dense/bm25分数不变。Hybrid的原retrieval_score本来就是RRF分，与fusion_score相同；独立Dense/BM25输入则保留其原检索分，fusion_score=None。
- DTO通过`node_to_result()`保留三类分数及完整证据，`result_to_node()`还原NodeWithScore及当前排序分。`postprocess_results(results, query=query)`支持DTO输入/输出。未重排旧Node继续兼容，未知metadata拒绝；重复重排须使用原候选，不覆盖第一次原分。
- 原chunk_id/doc_id/kb_id/SOURCE/file_name、page_start/page_end/section/text、完整source_spans和Embedding身份深复制不变，仅追加运行时来源；数据库Chunk不修改。来源从模型输入文本中排除，模型服务只取node.text。私有模型实例不序列化、不改全局Settings/LLM或注册Tracing。

业务日志仍每天一份，rerank.started/finished/failed、model_loaded/scored/score_failed/skipped只记摘要。模型加载/评分完成不意味着相关性提高；不得开启未经审查的框架instrumentation/SQL echo或第三方正文追踪。

## 手动部署（用户执行）

先沿用[README](../README.md#本地-bge-reranker)的CUDA PyTorch部署和core/NLP缓存。没有新增依赖，不无条件升级；只缺哪个补哪个，安装PyTorch/CUDA始终由用户手动处理。本实现不调用FlagEmbedding，也不需要accelerate/device_map=auto。

```powershell
conda activate dkf-agent
python --version
python -c "import sys; print(sys.executable)"
python -m pip --version
python -m pip show torch transformers huggingface_hub safetensors llama-index-core
# 只在上面的检查确认对应轻量推理依赖缺失时执行相应行。
python -m pip install transformers
python -m pip install huggingface_hub
python -m pip install safetensors
python -m pip check
```

Python/pip必须同属dkf-agent；已可用依赖保留，不安装全量LlamaIndex。用户显式联网下载官方模型（数GB，保留额外缓存/CPU加载内存空间）：

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3', allow_patterns=['*.json','*.safetensors','sentencepiece.bpe.model'], ignore_patterns=['onnx/*']); print('reranker_download_complete')"
```

至少包含config.json、tokenizer.json、tokenizer_config.json、Tokenizer附属文件和完整model.safetensors（或其完整分片/index）；不下载ONNX、pytorch_model.bin或造Pooling配置。目录已受/models/忽略保护；用户自行记录官方下载revision，本地实际文件指纹不是官方签名或版权证明。

`.env`/`.env.example`已按模型分组新增（真实旧配置保留，用户手动确认部署值）：

```dotenv
RERANKER_MODEL_DIR=models/bge-reranker-v2-m3
RERANKER_DEVICE=cuda:0
RERANKER_BATCH_SIZE=2
RERANKER_MAX_LENGTH=8192
RERANKER_USE_FP16=true
```

RTX4060 Laptop 8GB从batch=1～2开始，停止同GPU的GPU Worker；Hybrid Query的BGE-M3与Reranker是两种不同模型，在Query进程中仍会同时驻留显存。最大长度8192是允许上限而非显存保证，长结构块不截断。OOM时安全失败，不自动降batch/精度/长度或CPU；用户可以降低批次并重启Query进程，或明确将RERANKER_DEVICE=cpu（按FP32运行，较慢）。更换模型/精度后重启，不能在运行中替换权重；Reranker配置与文档Embedding检查点独立，不需重建文档向量。

本阶段无新HTTP入口、数据库迁移或队列；在线默认同步，仅已有PostgreSQL和本地Query模型。模块/Adapter构造不读权重，首次真实评分才取共享模型；进程间不共享CUDA对象，多Query进程会各占模型显存。

## Mock用例（用户执行）

```powershell
conda activate dkf-agent
python -m pytest tests/test_reranking.py tests/test_reranker_adapter.py -q
# 相邻契约回归仍由用户选择运行。
python -m pytest tests/test_hybrid_retriever.py tests/test_llama_index_adapter.py tests/test_dense_retriever.py tests/test_bm25_retriever.py tests/test_business_logging.py -q
```

覆盖惰性/共享/PID/本地安全加载、设备/批次/真实token上限、有限logit、后续批次失败原子性、默认Top5/同分UUID、原分及Node/DTO往返、证据不变/深复制、候选异常/身份变化/重复重排拒绝、同步/异步和日志脱敏。仅内存Stub，没有解析测试文档；core未安装跳过不是通过。Mock预设好分数只证明排序算法，不能证明真实质量提升。

## 真实候选验收与排序对照（用户执行）

用户准备已有READY/同Embedding模型文档和已核对的官方QA及真实证据，PostgreSQL已运行。无需新上传、MinerU、任务或文档编码；先停止同卡Worker，确认模型下载和配置完成。以下不打印正文/模型路径：

```powershell
conda activate dkf-agent
$rerankReadCheck = @'
from uuid import UUID
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.llama_index.hybrid_retriever import HybridRetriever
from app.services.rag.llama_index.reranker_adapter import BGERerankerAdapter
from app.services.rag.llama_index.node_adapter import node_to_result

configure_logging(get_settings(), role='rerank-cli')
kb_id = UUID(input('知识库UUID：').strip())
doc_id = UUID(input('目标READY文档UUID：').strip())
query = input('真实官方问题或原文关键词：')
candidates = HybridRetriever(kb_id, doc_ids=[doc_id], top_k=30).retrieve(query)
assert candidates, '先核对READY、Query模型身份及真实查询，空候选不视为质量验收通过'
before = {hit.node.node_id: hit.model_dump() for hit in candidates}
ranked = BGERerankerAdapter(top_n=5).postprocess_nodes(candidates, query_str=query)
assert len(ranked) == min(5, len(candidates))
assert all(ranked[i].score >= ranked[i + 1].score for i in range(len(ranked) - 1))
assert before == {hit.node.node_id: hit.model_dump() for hit in candidates}
print('Hybrid Top5:', [(hit.node.node_id, hit.score) for hit in candidates[:5]])
for hit in ranked:
    result = node_to_result(hit)
    assert result.kb_id == kb_id and result.doc_id == doc_id
    source = before[hit.node.node_id]
    assert hit.node.text == source['node']['text']
    assert result.metadata == source['node']['metadata']['chunk_metadata']
    assert {key: value for key, value in hit.node.metadata.items() if key != 'rerank'} == source['node']['metadata']
    assert result.metadata.get('source_spans') and result.rerank_score == hit.score
    print('Rerank Top5:', result.chunk_id, (result.page_start, result.page_end),
          'input_rank=', result.rerank.input_rank, 'retrieval=', result.retrieval_score,
          'fusion=', result.fusion_score, 'rerank=', result.rerank_score)
'@
python -c $rerankReadCheck
```

预期原候选/证据不变，Top5有限logit降序，原检索/融合分可追溯，日志只出现本地检索/重排，不增加Document/Chunk/task_id/MinerU batch。若CUDA/文件/长度失败，先核对固定RERANK_*码，不重新解析/付费或让原候选假装重排成功。

质量验收必须在**同一次Hybrid候选集合**上对比未重排Top5与重排Top5，不能重跑不同候选后归因。至少选若干真实普通文本、表格/数字、公式及近似干扰证据问题，保存问题ID、预期chunk/doc/可靠页码、原排名、新排名和耗时；OHR 0-based官方页转换按既有工具，OCB原expected_assertions不能当字符串包含真值，不能把Omni GT当QA。

用户按同一批真值人工统计证据Hit@5/MRR或有分级标注时nDCG@5，并记录改善/退化/无变化及页码/章节准确性。单文档范围只能比较页/Chunk命中，不能用文档Hit@5=1宣称排序改善；多文档验收可省略doc_ids或选同库多文档，不能混库。无可靠证据真值的项记manual_pending/not_applicable。没有真实对照前，不能宣称“明显优于未重排”、问答准确率或阶段21完成。

模型兼容、8GB双模型峰值显存、长候选完整评分、延迟及相邻回归全部未由Codex实测；用户反馈后再确认，不进入22。
