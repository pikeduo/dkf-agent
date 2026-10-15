# 公式语义结构化与人工验收（阶段27）

代码/Mock用例已编写，Codex未执行pytest、来源查询、真实API或模型。阶段26及此前待验收事项不自动通过，不进入阶段28/29。没有新依赖、环境变量、数据库表/迁移、HTTP接口或Worker任务；使用当前LLM配置和既有EvidenceService。

## 服务与范围

```python
from app.services.formula import extract_formula

# 由用户确认真实ID、来源允许外传及单次费用后才调用。
result = extract_formula(kb_id, chunk_id, block_id, doc_id=doc_id, allow_cloud=True)
```

默认allow_cloud=False且只接受真正的布尔True；未允许前不查来源或读取LLM配置。构造FormulaService不连接数据库或HTTP。明确执行后：

1. EvidenceService在独立只读REPEATABLE READ快照中限定ACTIVE库、READY文档、kb_id/chunk_id及可选doc_id，沿用原件Hash、解析/切片代次、Block Hash、Unicode字符和页语义全部检查。
2. 必须显式选定该Chunk中的唯一MinerU `formula` Block，来源为mineru_cloud、start_char=0且片段Hash等于整Block Hash；长公式的局部切片、普通文本或Native table内的单元格公式不当成完整输入。
3. 模型只获得所选原式及同Chunk、原block_index前后两块内的title/text/table片段。不额外读全文、跨文档检索、补OCR或读取文件；如果Chunk没有相邻说明，不自动扩大范围。其他formula不会被当成上下文补当前原式。
4. 现有create_llm_service按LLM_PROVIDER选定服务，默认DeepSeek官方；custom同样复用独立Key/模型/地址与HTTP安全边界。仅一次JSON-mode请求，无自动重试、提供方切换、Refine或收费Fallback。完整Prompt超过既有RAG_MAX_CONTEXT_CHARS时直接失败，不裁剪。
5. 本地严格校验JSON、原式一致、变量集合及原文引用；source只由已核对Evidence填入，模型不能生成文件名、页码、路径、Hash或来源UUID。不修改Block/Chunk/Embedding/Index、原RAG Prompt或MinerU Adapter。

## 输出与稳定性

FormulaSemanticResult主要字段：

| 字段 | 含义 |
|---|---|
| status | success / insufficient_evidence / unsupported_formula，后两者不携带半份定义 |
| formula_id / version | formula-semantic-v1与当前文档/原件/解析/Chunk/公式Block身份绑定的稳定UUID |
| formula_block_id / raw_formula | 原Block身份及逐字符原式，包含上游OCR误识别时也不改写 |
| formula_name | 基于证据的名称/忠实翻译；未知为null，不按文件名硬编码别名 |
| normalized_formula | 仅右侧受限业务表达式，不是计算结果或可直接eval的输入 |
| variables / variable_meanings | 确定排序的变量集合；各含meaning及其evidence，未知含义为null和空列表 |
| formula_name_evidence | 名称依据的原文引用，不采用模型虚构来源 |
| source | 完整EvidenceTrace，保留kb/doc/chunk、file_hash、原件名、两阶段任务、页语义及全部已核对source_spans |
| semantic_validation | 固定manual_pending；结构/引用合法不代表数学等价或语义质量验收通过 |

模型引用只能选择发送的evidence_id及逐字quote；本地要求引文在所选片段中恰好出现一次，生成Block与Chunk两端Unicode左闭右开偏移。重复/歧义/不存在的引文失败，不猜第一次位置。含义已知却无引文、未知却伪造引文、变量遗漏/重复/多出均失败。

稳定性是：同一来源代次有相同formula_id；相同四则AST的空白/普通小数写法规范化一致。**不声称LLM跨请求输出完全相同、代数等价证明或已有公式缓存**；重复手动调用仍会再次收费。来源是调用前只读快照，云请求期间若记录被重建，返回快照不冒充新的数据库代次；需人工核对最新来源，当前没有历史公式持久化/自动回填。

## 第一版表达式

- 支持利润率、毛利率、增长率、同比、环比、占比、明确有限项的加权平均、四则运算、百分比；这些是允许的语义目标，不是无证据自动套用的公式模板。
- 原文显式给出输入关系后，模型转换为小写ASCII snake_case符号、普通十进制常量、`+ - * /`和括号；结果只包含右侧，不带赋值。
- 百分比必须按原式语义明确乘100或除100，不能猜结果单位；利润率分母、同比/环比基期、权重项数不从常识补齐。符号最长32字符、最多32个，输入表达式最多1024字符、AST最多128节点/深度16。
- 不允许函数、属性、索引、幂、比较、循环、科学计数法或任意Python代码；数字至多12位整数/8位小数。一般求和、矩阵、分段、积分等无法展开为有证据有限四则式时应unsupported，不猜项数。
- AST只用于语法检查/括号与数字规范化，**不执行eval/exec、parse_expr、SymPy、代入、零分母求值或百分比单位换算**。变量数值/单位绑定及安全计算留给阶段28/29，不能将本阶段字符串直接用于通用求值器。

## 安全失败与边界

| 类别 | 处理 |
|---|---|
| FORMULA_CLOUD_PERMISSION / INPUT | 未许可或非法身份，来源/模型不执行 |
| EVIDENCE_* | 原有来源校验失败，保留固定分类，不修补Hash/页码/范围 |
| FORMULA_SOURCE | 不是完整唯一MinerU公式或来源格式不合格，不调用模型 |
| FORMULA_CONTEXT_LIMIT | 完整上下文超字符预算，零LLM请求 |
| FORMULA_OUTPUT / EXPRESSION | 模型JSON、原式、变量/引用或四则语法无效，不返回半份定义、不重试 |
| DEEPSEEK_* / LLM_* | 沿用所选提供方配置/鉴权/配额/限流/HTTP/网络/截断错误，无自动换服务 |

insufficient_evidence表示本次模型明确无法从当前原式/上下文可靠转换；unsupported_formula表示超第一版范围；源读取/网络/格式错误不是这两种正常语义状态。模型即便返回success，合法引文也不能证明解释忠实或表达式等价，必须人工逐项对照。raw_formula不改OCR；名称可按上下文解释，例如误识别的缩写不能靠生产代码字符串替换修复。

目前不提供自动公式发现/全文上下文扩展、原生Excel公式提取、公式持久化/去重缓存、多次推理、自动参数绑定或计算。日志复用每日文件与formula-cli角色，只含UUID/数量/状态/耗时/固定安全码，禁止公式/表达式/变量含义/正文/Prompt/回答/Key/私有路径。

## 用户手动验收

### 1. Mock与相邻回归

```powershell
conda activate dkf-agent
python -m pytest tests/test_formula.py tests/test_formula_acceptance.py tests/test_evidence.py tests/test_llm_service.py tests/test_deepseek_service.py tests/test_business_logging.py -q -ra
```

用例只构造内存来源字段、响应和MockTransport，不生成真实文档、不连数据库/GPU/云端。覆盖九类公式、原式不改、名称/含义字符反查、变量/表达式一致、规范化与稳定ID、PDF/图片/Office页语义、代次/Hash/非READY/越界、长公式片段拒绝、严格JSON/引用、上下文预算、两提供方配置复用、失败不切换/重试、日志脱敏及CLI授权门。Mock通过不证明真实公式语义正确。

### 2. 从已有READY文档选择真实公式

无需重新上传或消耗MinerU，不启动GPU、Redis或Worker。用户先确认PostgreSQL可用、已有完整MinerU formula Block；在pgAdmin执行只读SQL，替换实际库UUID：

```sql
SELECT d.kb_id, d.doc_id, c.chunk_id, b.block_id, b.page, b.section, b.text
FROM documents d
JOIN knowledge_bases k ON k.kb_id=d.kb_id AND k.status='ACTIVE'
JOIN document_chunks c ON c.doc_id=d.doc_id AND c.kb_id=d.kb_id
JOIN document_blocks b ON b.doc_id=d.doc_id
WHERE d.kb_id='实际测试库UUID' AND d.status='READY'
  AND b.block_type='formula' AND b.source='mineru_cloud'
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(c.metadata->'source_spans')='array'
           THEN c.metadata->'source_spans' ELSE '[]'::jsonb END
    ) s
    WHERE s->>'block_id'=b.block_id::text
      AND s->>'start_char'='0' AND s->>'end_char'=length(b.text)::text
  )
ORDER BY d.doc_id, b.block_index, c.chunk_id
LIMIT 20;
```

SQL只找候选，不能替代服务对Hash/全字符/代次/页语义的验证；缺少来源元数据应报告，不修改数据库伪造。正文仅供可信终端人工选取，不提交Git/日志。没有候选时先反馈现有文档/解析结果，不自动Reparse收费。

### 3. 默认预览，不查来源或联网

```powershell
python -m scripts.stage27_formula_acceptance
```

预期DRY-RUN；没有创建报告或执行其他业务。使用上一SQL真实ID填写一次：

```powershell
$kbId = '实际知识库UUID'
$docId = '实际READY文档UUID'
$chunkId = '实际Chunk UUID'
$blockId = '该Chunk中完整公式Block UUID'
python -m scripts.stage27_formula_acceptance --kb-id $kbId --doc-id $docId --chunk-id $chunkId --block-id $blockId
```

仍为DRY-RUN。`--execute`但未指定`--allow-cloud`会安全拒绝，不能因准备好Token或原件下载完成就认为获得外传许可。

### 4. 用户确认外传与费用后一次真实提取

先手工检查README中LLM_PROVIDER及所选提供方的独立配置；只有来源允许向该服务发送、接受一次费用时执行，Codex不会代跑：

```powershell
python -m scripts.stage27_formula_acceptance --kb-id $kbId --doc-id $docId --chunk-id $chunkId --block-id $blockId --execute --allow-cloud --show-content
```

默认仅输出状态、业务ID、变量数量及manual_pending；`--show-content`是明确同意在可信终端查看完整定义/来源JSON，不写报告或业务日志。命令会输出原文，仅在受信任终端使用。每次显式运行都是新调用，不为比较重复运行同一公式。

预期success时逐项核对：原LaTeX完全未变；规范式符合原运算优先级/分母/百分比口径；变量无凭空数值；名称/各已知含义的引文及两端偏移准确；PDF仍是物理页，Office仍是逻辑页/空bbox；原Document/Block/Chunk/向量和MinerU批次没有改变。未知含义保持null，不能为验收擅填。insufficient/unsupported应无半份定义并退出非零；错误只给固定安全代码。

分别选已有的九类真实公式评估，不要求为了凑覆盖制造素材。没有的类型如实记缺口；校验结构、数学等价、变量语义、来源质量四项分别人工记录。图像OCR遗漏/原式误识别、公式跨Chunk导致不完整、上下文不在当前Chunk、模型误解百分比/同环比均是需要记录的边界；本阶段不自动修复OCR、套模板、扩大上下文或请求第二次模型。

用户反馈Mock及真实公式质量后再决定下一步；本轮不开发参数识别、安全计算或Knowledge Agent。
