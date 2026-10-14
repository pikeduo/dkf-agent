# DeepSeek RAG：最终证据合成与人工验收

阶段22代码已实现，测试仅编写。真实DeepSeek响应、答案忠实度、数字/引用准确性、Prompt注入抗性及延迟仍待用户验收。没有新HTTP接口、数据库表/迁移、Worker、模型或依赖；阶段16正式验收与阶段21排序收益等未完成事项保留，不进入阶段23/24。

## 入口和边界

```python
from app.services.rag.query import query_knowledge_base

# 用户明确允许问题与最终证据云外传、接受费用后才传 allow_cloud=True。
result = query_knowledge_base(kb_id, question, doc_ids=[doc_id], top_k=30, allow_cloud=True)
# 返回项目 RAGAnswer；如未来API需要JSON，可使用 result.model_dump(mode="json")。
```

- 项目正式DeepSeek HTTP边界为`app.services.deepseek.DeepSeekService`。仓库此前尚无LLM服务，因此本阶段新建单一边界；没有引入OpenAI SDK或散落HTTP客户端。
- `DKFQueryEngine`继承LlamaIndex BaseQueryEngine，组合已有HybridRetriever（各路20/融合默认30）及BGERerankerAdapter（Top5），不重新Parse/Chunk/Embedding/Index。仅Query沿用共享BGE-M3，重排沿用共享Reranker，两者是不同模型。
- `DKFDeepSeekAdapter`继承CustomLLM，明确包装项目服务；`DKFResponseSynthesizer`继承BaseSynthesizer，但采用Node-aware单次Chat，显式初始化状态而不用默认LLM/PromptHelper/Tokenizer。自由text_chunks、额外source nodes、动态Prompt、Completion及流式入口拒绝；没有Refine/tree/map-reduce的隐含多次收费请求。
- 公共Query/Synthesis/Chat不走默认含正文的callback；不修改全局Settings，不注册Tracing。第三方全局instrumentation仍需用户禁用，不宣称能净化任意外部追踪。
- `doc_ids`与原检索相同：省略为指定库全范围，非空最多100个UUID；空列表/跨库/坏证据不扩大范围。READY/代次/模型过滤由原Retriever执行，最终再核对kb/doc、去重及重排前后证据一致。
- 默认同步；显式`aquery/asynthesize`只是同进程线程包装，无额外GPU进程/服务。输出项目`RAGAnswer`，不向前端暴露Response/NodeWithScore。

## 配置与费用

在本地`.env`的模型与LLM组手动填写，真实密钥不得提交或打印；本轮同步结构但保留已有密钥值：

```dotenv
DEEPSEEK_API_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_MAX_TOKENS=2048
DEEPSEEK_TIMEOUT_SECONDS=60
RAG_MAX_CONTEXT_CHARS=60000
DEEPSEEK_API_KEY=
```

复用已声明的httpx和llama-index-core，不安装全量LlamaIndex/额外LLM集成；按[README](../README.md)准备已有模型和core/NLP缓存。官方接口及模型能力以[DeepSeek文档](https://api-docs.deepseek.com/api/create-chat-completion/)和账号权限为准；默认非thinking/temperature=0/JSON/非流式。可配置官方`/v1`兼容端点，不允许任意主机拿到密钥；直连、TLS校验、不读代理、不跟随跳转。

`allow_cloud=True`是本次调用的明确外传/费用确认，不是版权证明、鉴权或通用用户权限系统。未确认时在检索/模型前拒绝；用户必须确认问题和最终正文可发给DeepSeek。已有MinerU上传许可不自动覆盖DeepSeek。正式对外用户授权/鉴权留到后续API阶段。

每次有证据的合成最多一个POST；响应超时可能已经计费，鉴权/限流/网络/供应商5xx均不自动重试。手动重跑会产生新请求，先核对账户记录；本阶段无持久化回答Cache/请求Outbox/exactly-once保证。HTTP每阶段超时默认60秒（连接10秒），不是检索+推理+云调用的总耗时保证。

上下文预算包括固定Prompt和完整问题/最终文本，按字符而非精确token计算；默认60000字符。超限报错，不偷偷丢候选/剪正文/截断表格公式；供应商真实token上限仍可能拒绝。max_tokens为输出预算，默认2048；finish_reason不是stop时整体拒绝，不把截断回答发布为成功。响应最大256KiB。

## 回答与最小引用

生产Prompt固定在`app/services/rag/synthesis.py`，明确只用最终Evidence、指令隔离、缺证据拒答、不虚构来源/页码/数字；不用框架默认Prompt。发送的正文仅含原question和最终`evidence_id=chunk_id / text`，不发送原检索其他候选、模型身份、私有路径或全部metadata。

模型输出限定为JSON的`insufficient_evidence`及逐项`claims(text, evidence_ids)`。未知/重复ID、自由answer字段、模型生成的来源字段/数字引用标记、无引用事实、非有限JSON、重复键、坏Unicode或代码围栏均安全拒绝，不修补答案。每条claim绑定最终ID白名单，再由服务生成`[1]`等引用标记。成功为`status=success`及原Citation对象列表；证据不足的实际响应：

```json
{
  "status": "insufficient_evidence",
  "answer": "当前检索到的证据不足以回答该问题。",
  "citations": [],
  "insufficient_evidence": true
}
```

实际Citation为原RetrievalResult字段加citation_id，包含kb_id/doc_id/chunk_id、file_name/file_type、page_start/page_end/section、原text、完整metadata/source_spans/页语义，以及retrieval_score/fusion_score/rerank_score及其来源；全部来自最终原Node。原Office逻辑页不能说成物理页。每个Chunk引用仅一次，按首次引用编号，最多5条，不创建数据库记录。

空候选不加载Reranker、不调用DeepSeek；模型明确不足返回`status=insufficient_evidence`、固定中文不足文本、空citations和true。负的rerank logit不作为拒答概率阈值；模型/检索/HTTP失败则抛安全异常，不伪装成功或不足。

**结构与引用白名单只能证明引用真实且绑定最终证据，不能证明claim必被原文蕴含。** LLM仍可能误解单位/数字、受注入影响或漏关键信息；无自动语义验真、公式计算或LLM答案准确率评测。本阶段只做必要引用绑定，阶段23完整Evidence展示/反查与阶段24HTTP API仍未开发。

## Mock测试（用户运行）

```powershell
conda activate dkf-agent
python -m pytest tests/test_deepseek_service.py tests/test_rag_synthesis.py tests/test_rag_query_engine.py -q
# 相邻回归；不请求真实服务，用户自行决定执行。
python -m pytest tests/test_reranker_adapter.py tests/test_hybrid_retriever.py tests/test_llama_index_adapter.py tests/test_business_logging.py -q
```

测试只有内存Node、项目服务Stub和httpx.MockTransport，未生成文档素材。覆盖唯一请求/TLS/直连/无重试、鉴权/额度/网络/响应预算/截断、JSON严格性、仅最终Evidence、原来源与三分数/深复制、引用去重/未知引用、空集/不足/服务异常区分、范围与最终证据一致、同步/异步、默认Prompt/回调旁路保护及日志脱敏。core未安装或缓存未准备导致跳过不是通过；Codex没有执行。

## 最小真实问题验收（用户手动执行）

1. 使用已有独立ACTIVE测试库及READY文档，不重传、不重新解析或消耗MinerU。确认实际匹配的本地BGE-M3、Reranker和PostgreSQL已可用；按README准备DeepSeek密钥及账号模型权限。
2. 先停止同卡GPU Worker/其他模型测试进程；8GB显存可能同时驻留两个不同模型，不保证长候选可用。OOM/真实token超限时查安全码，不静默回退CPU/删证据，不重做文档向量。
3. 对已有可靠QA选一条，只发一次；输出含答案业务内容，仅在本机可信终端查看，不粘贴到公开日志/报告。以下命令使用真实输入，不生成或伪造问题：

```powershell
conda activate dkf-agent
$ragCheck = @'
from uuid import UUID
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.query import query_knowledge_base

configure_logging(get_settings(), role='rag-cli')
kb_id = UUID(input('已有ACTIVE测试库UUID：').strip())
doc_id = UUID(input('已有READY文档UUID：').strip())
question = input('真实问题：')
if input('确认问题/证据允许DeepSeek外传并接受本次费用，输入YES：') != 'YES':
    raise SystemExit('未授权，不执行问答')
result = query_knowledge_base(kb_id, question, doc_ids=[doc_id], allow_cloud=True)
print('status=', result.status, 'insufficient=', result.insufficient_evidence,
      'citations=', len(result.citations))
print(result.answer)
for cite in result.citations:
    assert cite.kb_id == kb_id and cite.doc_id == doc_id
    assert cite.rerank_score is not None and cite.fusion_score is not None
    print('citation=', cite.citation_id, 'chunk=', cite.chunk_id, 'doc=', cite.doc_id,
          'pages=', (cite.page_start, cite.page_end), 'section=', cite.section,
          'page_semantics=', cite.metadata.get('page_semantics'))
'@
python -c $ragCheck
```

预期：要么success及1～5条真实引用，要么明确不足；错误必须有固定安全码而不是空答案。按每项`[n]`对照本地引用原Chunk/原件，核对关键数字/单位/事实、页/section及source_spans。使用同一次结果判断，不以返回success宣称质量通过。

4. 用户另行明确预算后，可手动换一个文档不包含的事实问题验证不足、使用同库两份文档验证范围；不得把未知答案也当不足真值。不同运行属于新的收费调用。负向HTTP/非法引用主要由Mock验证，不为了真实测试破坏系统/余额。
5. 日志每天仍一份，`rag-cli`显示RAG CLI；核对fusion→rerank→deepseek→rag事件及耗时/数量，无问题、Prompt、答案、证据、Token或上游原文。deepseek.finished仅表示接收完整HTTP消息，rag.synthesized才完成结构引用校验，两者都不代表忠实度通过。
6. 检索前后已有Document/Chunk/向量、任务ID和MinerU批次不应因问答增加或改变。记录真实问题ID、原证据、回答人工结论、各段耗时与DeepSeek账户费用；解析或检索真值问题与生成错误分开记录。至少覆盖普通事实、数字/表格、公式说明、无证据和对抗指令。

阶段16素材/正式导入与质量、阶段21真实重排提升及旧回归仍待各自验收。未执行任何真实问题前，不能声明阶段22端到端成功或答案忠实度≥90%；用户反馈后再决定下一步。
