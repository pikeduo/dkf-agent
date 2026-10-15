# 公式语义结构化、参数绑定与人工验收（阶段27/28）

阶段27/28代码与Mock用例已编写，本轮Codex未执行pytest、来源查询、真实API或模型。阶段26及此前待验收事项不自动通过，不进入阶段29。没有新依赖、环境变量、数据库表/迁移、HTTP接口或Worker任务；提取使用当前LLM配置，参数绑定只复用EvidenceService，不调用LLM。

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

提取服务不提供自动公式发现/全文上下文扩展、原生Excel公式提取、公式持久化/去重缓存、多次推理、自动参数绑定或计算；显式参数绑定见下节。提取日志复用每日文件与formula-cli角色，只含UUID/数量/状态/耗时/固定安全码，禁止公式/表达式/变量含义/正文/Prompt/回答/Key/私有路径。

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

阶段27的真实公式质量仍需用户反馈，不因进入阶段28自动通过；安全计算与Knowledge Agent不在本轮范围。

## 阶段28：参数识别与绑定

入口与阶段27分离，**不重新提取公式、请求LLM、检索全文、写业务库或执行计算**：

```python
from app.services.formula_binding import bind_formula_variables

# formula是已有的FormulaSemanticResult；以下为Python调用示意，不是PowerShell命令。
result = bind_formula_variables(formula, user_inputs=[
    {"name": "revenue", "value": "100", "unit": "万元", "input_id": "scenario-1"},
])
```

示意变量仅在原公式确实含revenue时有效；不得替换真实变量名来凑测试。第一版的“识别”是严格解析显式用户值/显式选中的证据数值，不做自由文本NLP猜测、自动期间选择或变量别名推断。

### 输入与来源

`FormulaBindingRequest`顶层仅有`formula`、`user_inputs`、`evidence_inputs`，后两者默认[]：

- `formula`：阶段27完整JSON对象，包含原source快照；不是只含表达式的自造对象。绑定前重新查询当前Evidence，校验原式、formula_id、AST变量集合、名称/含义引文双端偏移及完整快照；损坏/过期不自动请求模型重新生成。
- `user_inputs[]`：`name / value / unit / input_id`。value必须字符串，input_id为1～64位ASCII字母/数字/横线/下划线，由用户关联本次场景；unit可省略为null。来源明确标user_input，保留原值/单位，不冒充文档事实。
- `evidence_inputs[]`：`name / doc_id / chunk_id / block_id / quote / value_text / unit_text`。前三个来源ID必须非零UUID；全部来源限定原公式同kb，文档必须READY。quote必须在该Block已核对的Chunk片段中唯一出现，value_text在引文内唯一且不是更大数值的子串。unit_text可省略；提供时必须同引文、直接位于数值前/后（允许空白）。不根据远处表头或其他数字猜单位。

不同文档的参数只能显式选择，不能隐式扩大范围。相同Chunk同调用复用只读快照；同文档不同Chunk发现Hash/解析/切片代次不一致则失败。多个文档是独立短快照，非跨文档全局事务；完成后仍可能有并发维护，不保证调用后的永久READY。

数值允许正负号、普通小数、严格千分组，如`0`、`-20.50`、`1,234.50`；输出精确十进制字符串，不转float，不应用Decimal上下文舍入。布尔/JSON数字、NaN/Infinity、科学计数、范围、分数或`20%`均拒绝；百分数应显式提供`value="20",unit="%"`，本阶段**不除100**。单位未知保持null，不自动证明量纲兼容、汇率/倍率或期间正确。

同一变量重复提供（即使同值）、用户/文档冲突、未知变量均失败，不默认优先级或覆盖。缺参只表示没有提供该变量的值，不把坏引文、数据库故障或非法数值洗成缺参；即使上下文已有数字，没有显式选择也不自动绑定。

### 输出

- `status=success / need_variables`，`required_variables`为AST变量中尚未绑定的精确符号，稳定排序；部分绑定仍保留已有值及来源，缺值不填0。
- `variables[]`含`name / label / value / unit / source`；label取阶段27已核对的meaning，未知为null。数值0有效，常量公式可无参数；不在此阶段验证零分母或计算结果。
- 文档source含完整EvidenceTrace及quote/value/unit各自的FormulaReference，保留Block/Chunk双端Unicode字符区间、原PDF/图片/Office页语义，不由模型补页码。用户source含input_id/raw_value/raw_unit，无伪造文档引用。
- 原formula完整保留，`semantic_validation=manual_pending`、`unit_validation=manual_pending`、`calculation=not_performed`。成功仅表示来源/字面值/符号结构通过，不证明参数与具体业务、主体、日期、单位或公式数学关系匹配。调用方必须人工核验，不可直接宣称任务6b计算完成。

错误分类为`FORMULA_BINDING_INPUT / SOURCE / UNKNOWN / CONFLICT / UNIT`以及原`EVIDENCE_*`；固定安全文案，无自动重试/修补/付费兜底。日志仅记录开始/完成/缺参/失败及UUID/数量/耗时，角色formula-binding-cli→Formula Binding CLI；不输出参数名/值/单位或引文，继续每日单文件。

### 用户手动验收

1. 先由用户执行纯Mock和相邻回归；本轮Codex只写用例，不运行：

```powershell
conda activate dkf-agent
python -m pytest tests/test_formula_binding.py tests/test_formula_binding_acceptance.py tests/test_formula.py tests/test_formula_acceptance.py tests/test_evidence.py tests/test_business_logging.py -q -ra
```

覆盖缺参/部分/全量、零负小数/千分位精度、用户来源、文档数值/单位字符反查、PDF/图片/Office页语义、歧义/重复/冲突/未知符号、旧代次/改Hash/改公式、跨库拒绝、不调用LLM、不计算、日志脱敏及CLI安全门。Mock不能证明真实参数业务含义或单位正确。

2. 使用已有阶段27完整结果。已有`--show-content`输出时，只把**完整FormulaSemanticResult对象**保存在受限本地JSON，不包含日志、摘要或STRUCTURE_ONLY行；不要为参数绑定重复请求收费LLM。若没有任何可用定义，先由用户按上节确认外传/费用，提取一次；不是阶段28自动操作。源文件/输出含正文，放docs/test或其他受限目录，不提交Git。

3. 从该文件创建一次新的请求，不覆盖旧文件。以下命令在项目根执行，由用户替换/确认输入：

```powershell
$formulaPath = Read-Host '已有阶段27完整结果JSON路径（不是终端整份日志）'
$bindingDir = 'docs/test/stage28_formula'
$bindingPath = "$bindingDir/binding-request-01.json"
if (Test-Path -LiteralPath $bindingPath) { throw '请求文件已存在，请换新版本名，禁止覆盖' }
New-Item -ItemType Directory -Force -Path $bindingDir | Out-Null
$payload = @{ formula = (Get-Content -LiteralPath $formulaPath -Raw -Encoding UTF8 | ConvertFrom-Json); user_inputs = @(); evidence_inputs = @() }
$payload | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $bindingPath -Encoding UTF8 -NoClobber
code $bindingPath
python -m scripts.stage28_formula_acceptance
python -m scripts.stage28_formula_acceptance --input $bindingPath --execute
```

预览不读取输入/配置或查库。有效含变量公式在空参数请求下应need_variables、变量列表准确，退出码1表示未绑定齐，不是计算失败；非法/过期来源应安全错误，不伪装缺参。CLI只读现有PostgreSQL，不需要启动API/CPU/GPU Worker/Redis，也不请求任何云服务。

4. 在请求中按真实变量添加用户值，或从已有READY文档选择唯一引文及真实doc/chunk/block ID。用上节现有管理接口/只读查询查看来源，不重解析、不直接改业务库。缺一个值只列该符号，补齐后success；再用`--show-content`在可信终端核对完整source和数值/单位偏移：

```powershell
python -m scripts.stage28_formula_acceptance --input $bindingPath --execute --show-content
```

反向检查：重复变量/冲突输入须失败；零值不得当缺参；错误单位/不存在引文须失败；移除参数不得自动从上下文补值。零分母在本阶段仍只绑定、绝不求值。确认公式含义、参数主体/时间口径和单位后再反馈；自由文本自动识别、通用单位系统及阶段29计算尚未实现。
