# Citation / Evidence：来源链与人工验收

阶段23代码已实现；用户运行Mock后发现测试桩SQL参数名兼容问题，修正待用户复测，真实数据库来源与答案忠实度仍待人工验收。不新增依赖、配置、数据库表/迁移或HTTP接口，不开发阶段24。

## 两层来源边界

1. **引用快照**：阶段22逐事实claim只接受最终Chunk白名单，本地生成`[1]`等编号。`Citation`复制原Node/项目DTO的文件名、正文、页/章节、完整`metadata.source_spans`、模型身份及`retrieval_score / fusion_score / rerank_score`，未知分数保留`None`，不把分数当概率。答案标记集合必须与Citation编号一致。
2. **当前来源核对**：显式`EvidenceService`在同一数据库只读快照中反查`Citation → DocumentChunk → source_spans → DocumentBlock → Document`。不重新检索、读原件、解析、向量化、加载模型或调用LLM。现有RAG不隐式执行这一层；仅有Citation不应声称当前数据库已经核对。

`app.schemas.evidence.Citation / EvidenceTrace`与框架无关；原`app.schemas.rag.Citation`导入路径保留。薄Adapter的`citation_from_node()`、`citation_to_node()`复用原Node Adapter，ID不重建、正文/来源/三类分数深复制，转换本身不查库。

## 反查契约

```python
from app.services.rag.evidence import EvidenceService

service = EvidenceService()  # 构造不连接数据库。
trace = service.resolve(kb_id, chunk_id, doc_id=doc_id)
# 已有Citation或RAGAnswer时显式核对旧快照，不重新运行问答。
trace = service.resolve_citation(citation)
traces = service.resolve_answer(result)
```

- 查询限定指定`kb_id / chunk_id`及可选`doc_id`、ACTIVE库、READY文档；不匹配不扩大范围。一个回答的所有引用在独立REPEATABLE READ只读事务核对，任一失败不返回部分Trace，不flush/commit或改写任何业务记录。
- 校验原件Hash、当前解析代次/冻结切片检查点；按`source_spans`读取同文档Block并核对整Block SHA256、block_index/type/source/page/bbox及两端字符区间。旧Citation的ID、文件身份、正文、页/章节和完整metadata须等于当前快照；存在模型身份时须与当前入库身份一致。
- 字符偏移是Python Unicode字符的**左闭右开**区间，不是字节或Token。`start_char / end_char`指向Block原文，`chunk_start_char / chunk_end_char`指向Chunk；片段须逐字符相等，全部片段加当前Chunker的双换行连接符须完整重建Chunk。
- `context / content / overlap`原序保留；同Block多次来源不被去重，table/formula不拆分、不计算、不改原式。Chunk页范围包含所有来源，包括标题上下文与重叠；Trace保留Chunk原section和各Block原section，不声称它们一定相同。
- `EvidenceTrace.status=verified`仅表示当前快照来源一致，含原业务ID、file_hash、解析/切片任务ID、原Chunk正文/页/章节、page_semantics及已核对的source_spans（片段、原Block章节/置信度/坐标）。不含向量、私有路径或框架类型；包含原文，应仅向获授权调用方提供，禁止写入日志。

| 文档 | page_semantics | 页/坐标含义 |
|---|---|---|
| PDF | physical_pdf | 1-based物理页；bbox为点数，未知可空 |
| JPG/JPEG/PNG | image_page | 第1页；bbox为像素，未知可空 |
| TXT/MD/CSV、Native DOCX/XLSX | logical | 逻辑页1；bbox为空 |
| Cloud Office | logical | 转换后逻辑页；不推断原Office打印页/坐标，bbox为空 |

每条SQL statement_timeout=10秒、lock_timeout=2秒；单Chunk最多1024来源片段、原Chunk及被读Block正文分别不超过1000万字符，一个回答最多5个引用。这不是整个回答的总超时保证。查询后记录可能变化，Trace只证明查询快照；没有持久化历史回答/历史Block归档、用户鉴权或自动语义验真。

## 安全失败

| 错误码 | 处理 |
|---|---|
| EVIDENCE_INPUT / CITATION | UUID、引用编号或回答标记无效；不查询补救 |
| EVIDENCE_UNAVAILABLE | 不在指定ACTIVE/READY范围、缺文档/Chunk；核对实际ID/状态 |
| EVIDENCE_METADATA | 来源片段/页语义缺失或不合法；不猜历史位置 |
| EVIDENCE_STALE | 引用/文件Hash/解析或切片检查点已变化；保留旧引用，不绑定到新记录 |
| EVIDENCE_SOURCE | 缺Block、归属/Hash/字符区间/页码不一致；报告实际来源问题，不忽略 |
| EVIDENCE_LIMIT / SESSION / DATABASE | 超只读预算、非独立会话或查询未完成；不重试/重解析/调用云端 |

日志沿用`dkf.business`每日单文件及`evidence-cli`角色，只记录UUID/数量/耗时/安全码；`citation.created`不是来源验证完成，`evidence.finished`不是答案忠实度通过。不要开启SQL echo或包含原文的第三方Tracing。

## 用户手动验收

### 1. Mock回归，不依赖真实数据库/模型/API

```powershell
conda activate dkf-agent
python -m pytest tests/test_evidence.py tests/test_citation_adapter.py tests/test_rag_synthesis.py tests/test_rag_query_engine.py tests/test_business_logging.py -q
```

新增用例覆盖：旧Citation导入/字段兼容、稳定ID、Node往返及三类分数、深复制、Unicode来源、重复context片段、表格/公式原文、页/坐标语义、SQL范围/只读投影、过期引用/缺Block/错误Hash或区间全批拒绝、同快照答案反查、无引用不查库、安全日志及会话错误。Stub仅构造内存字段，不生成或解析真实验收文档。core缺失导致skip不是通过；用户决定是否运行相邻DeepSeek/Reranker/Node Adapter回归。

测试桩按SQL表达式的实际字段、运算符与绑定值识别范围，不依赖`kb_id_1 / doc_id_1`等编译器自动参数名（联表同名列标签会影响编号）。直接Stub用例覆盖不同列标签及kb/Chunk/doc/READY过滤；负向来源用例须断言对应的精确错误码，避免Mock内部错误被安全包装后冒充来源校验通过。本次仅修正测试桩，不放宽生产Hash、字符来源、页码或只读边界；Codex未执行复测。

### 2. 已有READY文档的真实只读来源核对

用户确认PostgreSQL可用，在项目根目录运行。无需API/Redis/Worker、GPU、本地模型或云密钥；不用重新上传。填写**已有检索结果**的真实知识库、Chunk及Document ID，不能使用示例假ID。

```powershell
$evidenceCheck = @'
from app.core.business_logging import configure_logging
from app.core.config import get_settings
from app.services.rag.evidence import EvidenceService

configure_logging(get_settings(), role='evidence-cli')
kb_id = input('已有知识库UUID：').strip()
chunk_id = input('已有Chunk UUID：').strip()
doc_id = input('已有Document UUID（可留空）：').strip() or None
trace = EvidenceService().resolve(kb_id, chunk_id, doc_id=doc_id)
assert trace.text == '\n\n'.join(span.text for span in trace.source_spans)
print('status=', trace.status, 'doc_id=', trace.doc_id, 'chunk_id=', trace.chunk_id,
      'pages=', (trace.page_start, trace.page_end), 'semantics=', trace.page_semantics)
for span in trace.source_spans:
    print('block_id=', span.block_id, 'page=', span.page, 'type=', span.block_type,
          'role=', span.role, 'block_chars=', (span.start_char, span.end_char),
          'chunk_chars=', (span.chunk_start_char, span.chunk_end_char))
'@
python -c $evidenceCheck
```

预期：verified，业务ID/页语义与原结果一致，每个Block及两端字符位置可追溯；不输出正文/路径/完整metadata。分别用已有PDF、图片和Office记录核对其语义。按Block ID通过现有管理员Block查询或pgAdmin只读查看原记录，人工比较片段、章节、表格原式与原件；不要为验收修改数据库或重建文档。

### 3. 答案重要事实与引用人工对照

在**已持有RAGAnswer的同一Python进程**运行`EvidenceService().resolve_answer(result)`，无需再次调用DeepSeek。核对每个`[n]`对应原Citation的Chunk及Trace；逐项人工判断回答数字、单位、关键事实是否被引用原文支持。未知分数/位置继续保留为空，不让LLM补来源。过期引用必须失败，不用同ID新正文解释旧回答。

真实问答仍按[DeepSeek RAG](DEEPSEEK_RAG.md)由用户单独确认外传权限和收费后执行；本阶段没有发起真实请求，也不新增一次问答来“验证引用”。来源一致、Mock通过或READY均不能宣称答案忠实度/问答准确率通过。阶段16正式素材、阶段21排序收益、阶段22真实问答及误拒答效果的未验收事项继续保留；验收反馈前不进入阶段24。
