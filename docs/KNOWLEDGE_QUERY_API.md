# Knowledge Query API：契约与人工验收

阶段24代码已实现，用户运行联合Mock后发现非有限数响应校验问题，已修正但待用户复测；真实数据库/模型/HTTP/云响应待验收。不新增依赖、配置、数据库/迁移、Worker或模型，不进入阶段25；阶段23相关回归用户已报告148通过，真实来源及此前质量验收继续保留。

## 公开契约

| 接口 | 链路 | 返回 |
|---|---|---|
| POST /api/knowledge/search | 同范围Hybrid（各路默认20）→RRF Top-K→本地BGE Top5 | KnowledgeSearchResponse：status=success/empty，evidence列表 |
| POST /api/knowledge/query | 原RAG Service→LlamaIndex合成→工厂选定DeepSeekService/CustomLLMService | 原RAGAnswer：status/answer/citations/insufficient_evidence |

API只依赖项目Schema和KnowledgeService，不导入/返回LlamaIndex类型。搜索服务仅显式调用时导入原Hybrid/Node/Reranker Adapter，Query委托原`query_knowledge_base()`；框架换型仅影响服务内部。成功返回原业务ID/文件名/正文/页/章节、完整metadata/source_spans及原三类分数，不返回向量或模型对象，不把分数当概率。

请求均为JSON：kb_id非零UUID、question为非空原文本（最多32768字符，拒绝非法控制符/Unicode）、doc_ids可省略/null或1～100个非零UUID（去重排序；空数组拒绝）、top_k可省略，默认30/严格整数1～100。**top_k是融合候选上限，最终最多min(5,候选数)，不是要求返回top_k条Evidence。** 不支持额外Query rewrite/解析模式/模型对象/外部Query向量。

`/query`另有严格布尔`allow_cloud`，默认false，只有true才允许问答。许可必须包括`LLM_PROVIDER`选中的DeepSeek官方或自定义兼容服务、问题/最终正文外传及本次费用；不是鉴权或版权证明。未允许时在检索/模型/框架导入前返回403；`/search`不需要此许可，不构造LLM客户端或请求云服务。空搜索200/empty；真正证据不足200/insufficient_evidence；服务错误绝不伪装这两种正常结果。

原Retriever继续在SQL限定kb/可选doc/ACTIVE/READY/有效代次，Dense核对模型身份；接口再次检查输出范围，不删掉越界行冒充成功。合法UUID但不存在/跨库/不合格的文档不会扩大范围，可能没有匹配；空结果不证明库/文档存在，本阶段不新增存在性查询，用户可通过已有管理员详情确认。接口不重Parse/Chunk/文档Embedding/Index、不落库/迁移或创建第二套真相表。

错误固定为`detail.code/message`，不含input/ctx、问题/正文、连接串/Key、私有路径、SQL或上游原文。仅本领域APIRoute清理输入/响应验证错误，不改变其他API的错误协议。

响应在Pydantic JSON转换前递归检查原Python字段中的NaN/正负Infinity（含嵌套metadata及模型身份），任一非法值整批502/KNOWLEDGE_RESULT；不能利用JSON转换将其静默变为null后发布成功。合法None、有限数和字面文本“NaN/Infinity”保持原样，不猜测或修改原证据；新增Mock覆盖搜索/问答两条路径，由用户复测。

| HTTP | 典型安全错误 |
|---|---|
| 422 | KNOWLEDGE_INPUT / RAG_INPUT |
| 403 | RAG_CLOUD_PERMISSION |
| 409 | RAG_EVIDENCE（冲突/无效来源） |
| 413 | RAG_CONTEXT_LIMIT（完整上下文超预算） |
| 429 | DEEPSEEK_RATE_LIMIT / LLM_RATE_LIMIT（不自动重试或切换提供方） |
| 502 | KNOWLEDGE_RESULT / RAG_OUTPUT / RAG_SYNTHESIS / 上游无效或截断响应 |
| 503 | 检索/重排、部署/鉴权/配额/网络不可用；核对返回固定错误码 |
| 500 | KNOWLEDGE_INTERNAL（未预期错误，不透传原文） |

默认同步执行在FastAPI线程池，Query/重排共享原项目模型服务。构造/访问Swagger不加载模型，首次真实请求可能慢；core首次资源检查须用户先准备。建议一个API进程，多个进程会各自载入双模型。无鉴权/RBAC、HTTP级限流、请求ID幂等或整个链路总超时/取消保证，**仅限本机/可信内网，不暴露公网**。客户端超时/断连不保证服务停止或未收费；没有自动重试，但人工重发可能再次计费。日志复用api角色及每日文件，knowledge/http/fusion/rerank/rag事件不记录正文；禁止第三方Tracing/SQL echo。

## 1. 用户手动Mock回归

```powershell
conda activate dkf-agent
python -m pytest tests/test_knowledge_api.py tests/test_knowledge_search.py tests/test_evidence.py tests/test_citation_adapter.py tests/test_rag_synthesis.py tests/test_rag_query_engine.py tests/test_business_logging.py -q -ra
```

API测试仅ASGI内存应用及服务Stub，无TCP服务/数据库/模型/云请求；搜索测试复用内存Node/评分Stub，不生成素材。覆盖DKF/OpenAPI契约、输入/范围/默认值、显式云许可、空集/不足/失败区别、一次调用、三类分数/来源保留、越界/重复/坏响应拒绝、错误/日志脱敏；搜索用例验证原Hybrid/重排及Top5、空集跳过、原证据不变。core/NLP未准备导致skip不是通过。Codex未执行。

## 2. 用户启动已有服务

先确认已有PostgreSQL、ACTIVE测试知识库和READY文档、与向量匹配的本地BGE-M3、Reranker及core/NLP缓存。不要为验收重新上传/解析、重复向量化或消耗MinerU。停止同卡GPU Worker；API首次搜索会惰性加载双模型，8GB显存/长上下文仍需人工观察。不需要为查询另起CPU/GPU Worker或Redis。

独立PowerShell终端在项目根目录执行：

```powershell
conda activate dkf-agent
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问`http://127.0.0.1:8000/docs`，确认knowledge分组包含两个POST路由及DKF响应模型。现有配置变动先重启进程，不自动安装/下载模型或部署基础服务。

## 3. 用户真实本地搜索

```powershell
conda activate dkf-agent
$kbId = Read-Host '已有ACTIVE测试知识库UUID'
$docId = Read-Host '已有READY文档UUID'
$question = Read-Host '有真实原文支持的问题'
$body = @{ kb_id=$kbId; question=$question; doc_ids=@($docId); top_k=30 }
$search = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/knowledge/search' -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 10))) -TimeoutSec 300
$search | ConvertTo-Json -Depth 20
```

在可信终端检查最多5条Evidence、排序/三类分数、原Doc/Chunk/Page/Section/source_spans；全部属于指定kb/doc。该命令会显示证据正文供人工核验，不复制到业务日志或公开报告。分别对已有PDF/Office确认物理/逻辑页语义；关键词/分数不代替事实核对。省略或null doc_ids才做全库查询；空列表/非法UUID/top_k=0/true须422，不能扩大范围。查询前后Document/Chunk/向量、任务ID和MinerU批次不应改变。

## 4. 用户明确许可后真实问答（可能收费）

在同一终端保留上一步body，先按README配置所选LLM提供方的独立Key/模型/预算并重启API。官方保留DEEPSEEK_*错误码，自定义使用LLM_*；两者都不在失败后切换提供方。**以下命令只有用户确认后才发送一次请求，Codex不会执行。**

```powershell
if ((Read-Host '确认本次问题与最终正文允许发给当前所选LLM提供方并接受一次费用？输入YES') -ne 'YES') { throw '未授权，不发送问答' }
$body.allow_cloud = $true
$answer = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/knowledge/query' -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 10))) -TimeoutSec 300
$answer | ConvertTo-Json -Depth 30
```

预期：有证据时success、重要事实绑定合法`[n]`及原Citation；真正不足时固定不足结果。省略/false allow_cloud应403且不触发模型/DeepSeek；模型/网络/未知引用/上下文失败返回非200固定错误，不得判为不足。人工核对数字/单位/事实及账户费用；HTTP成功、引用合法或小样本通过不证明问答准确率。搜索与问答是两个独立查询快照，不把前一次搜索结果当成本次回答的最终证据；引用以answer.citations为准。

阶段23`EvidenceService.resolve_answer()`仍是显式当前来源反查；接口保留引用快照，不自动查询Block验真。需要反查已有HTTP回答时，调用方在可信Python进程用`RAGAnswer.model_validate()`还原，再显式反查，**无需再次调用/query**。来源校验和语义忠实度是两个层次，框架对象不出API。完成后由用户反馈测试/真实质量结果，保留此前未验收事项，不自动推进阶段25。
