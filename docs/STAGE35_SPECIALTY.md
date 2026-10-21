# 阶段35：公式、目录、复杂度与质量

阶段35已有代码；本轮公式格式/重试修复的授权Mock结果见下文，其余专项回归及真实效果仍待验收。不把接口存在或结构合法当成专项效果已通过。阶段34停止优化并保留报告。仅复用现有索引/Block、检索、Evidence和阶段27～29公式服务；没有新模型、依赖、数据库表/迁移、Worker任务或自动云调用。

## 入口与真实边界

所有HTTP入口均为同步POST，位于Swagger的knowledge-specialty分组。沿用现有本机/可信内网使用范围，无新鉴权；云许可不是用户权限。错误为固定`detail.code/message`，不回显输入/正文或上游异常。字段验证沿用`KNOWLEDGE_INPUT`，业务错误保留下面的实际分类。

| 路径（前缀/api/knowledge） | 输入与行为 | 资源与输出 |
|---|---|---|
| /documents/analyze | kb_id/doc_id；visual_pages默认[]，最多5个不重复正整数 | 默认仅只读DB，返回派生结构/复杂度/质量，不改原记录 |
| /formulas/discover | 与/search相同的kb_id/question/doc_ids/top_k | 原Hybrid/Top5和Evidence；使用本地模型，不调用LLM |
| /formulas/extract | 明确kb_id/doc_id/chunk_id/block_id，allow_cloud默认false | 复用原提取；逐次授权后可能1次LLM POST，显式Fallback可最多2次 |
| /formulas/bind | 原FormulaBindingRequest：formula/user_inputs/evidence_inputs | 当前来源复核，缺参返回need_variables，不收费 |
| /formulas/calculate | 同上 | 原SymPy有界四则；缺参不算，除零明确失败，不收费 |

提取仍只支持完整、唯一、Hash和字符均一致的MinerU formula Block。发现只覆盖本次检索到的最多5个Chunk，不表示遍历整份文档；长公式片段、不支持的原生单元格公式计入拒绝候选。超过100个完整候选时明确返回omitted_candidate_count。来源过期/越界失败不伪装为空候选。发现不自动选公式、不自动请求提取；补参复用成功定义，不重复提取收费。公式语义、单位/期间仍为manual_pending，计算成功仅为机械算术，详见[原公式契约](FORMULA_SEMANTICS.md)。

## 派生目录和评估规则

报告绑定kb/doc、原件SHA256、当前解析代次与观测摘要；assessment_id用于比较同一观测，不是可信签名。它不覆盖原section、Block/Chunk或Parse Cache，不影响检索分数/READY，也不是逐字符完整Evidence来源核验的替代物。

- **目录**：优先显式数字/中文章节/Markdown编号，再参考现有title类型；短编号正文为heuristic。每个候选保留原标题、Block/序号/原页、Unicode字符区间及片段SHA256。编号父章不存在时保持根节点并标记MISSING_NUMBERED_PARENT/LEVEL_GAP，不补造章节或连到错误父章；无编号标题标记层级不确定。历史Block无序号时不建立父子关系。缺失字体不会伪造字号/样式。点线/制表符目录行与正文标题只做唯一文字匹配；印刷页提示单独保存，不冒称PDF物理页。没有候选为insufficient_evidence。
- **复杂度**：已观测页数≥10/50/200分别5/15/25分；table块≥1/20分别8/20分；formula块≥1/10分别5/15分；image_text块≥1/5分别3/10分；存在多栏候选15分；全量Block字符数≥10万/100万分别8/15分。相加封顶100，低于20为low、20～49为medium、≥50为high。这是公开的工程启发式，不是概率、语义质量分或耗时预测。页数是已观测下界；一张跨页表可能有多个Block，不能把块数说成真实表格总数。
- **布局观测**：每页最多32个已有文本bbox，横向分离且纵向重叠时提示多栏候选，侧注也可能命中；不按bbox重排原文。Office无bbox、未保存字体/未提取视觉对象等明确列入unknown_dimensions，低分不能证明整份简单。
- **质量**：required减covered与已存missing合并展示；真实缺页/隔离内容为attention。结构降级、PARTIAL、上游低置信度、页码回退、空序号及前缀替代字符为review。视觉无文字仅表示需要检查，不证明业务信息缺失，更不是MinerU解析失败。历史空质量摘要为unknown；no_observed_issue仅表示已观测维度未见风险，语义和未检查项目仍列出。

默认按128块流式读取，每块只物化前512字符、整块长度、位置和轻量字段；不读整表、向量或视觉附件大JSON。最长/多行正文不会被截成假标题，后续行不在前缀内的目录项不会被补造。单报告最多50000个Block、2000个标题/目录候选，超过时报ANALYSIS_LIMIT，不发布看似完整的半份报告。每SQL沿用10秒statement/2秒lock预算，独立REPEATABLE READ只读事务结束即关闭，无flush/commit/自动重试；这些边界不是整个请求的总超时保证。

### 可选原件像素检查

visual_pages非空才读取原PDF/PNG/JPG/JPEG，使用已有PyMuPDF，前后核对原件SHA256和文件身份；只在内存处理选中页，不输出图像、OCR或重新解析。源文件上限200,000,000字节、最多5页、长边1024像素；大尺寸图片另有像素预算。Office原件明确ANALYSIS_VISUAL_FORMAT，不启动LibreOffice或把逻辑页当原件物理页。复杂PDF底层渲染耗时/解压内存不能只由输出像素上限保证，需人工观察。

contrast为灰度标准差；edge_energy为相邻像素平方差均值。暗像素比例在0.005～0.45且对比度≥8时，edge_energy<12提示模糊待审，其余只表示未观察到该风险。空白/极低对比度/不适合文本观测时为unknown。倾斜使用至多16000个边缘点，在±8度内每0.5度比较行投影；峰值/中位分数不足1.2、点不足300或峰值在边界时为unknown；可靠候选绝对角度≥1.5度为review。角度只代表投影方向提示，不是原PDF页面旋转元数据。

阈值是未校准的第一版启发式。照片、表格线、字体大小、低分辨率、复杂背景可能误报/漏报；没有做真实模糊/倾斜准确率验证。所选页测到no_observed_issue也不能推断其他页、OCR内容或图表业务信息正确。

## 用户先执行的回归

下列全专项回归尚未全部执行；本轮公式相关Mock回归范围与结果见后文。没有启动服务/模型或处理真实文档。现有Conda dkf-agent环境中执行：

```powershell
conda activate dkf-agent
python -m pytest tests/test_document_analysis.py tests/test_document_visual_quality.py tests/test_document_analysis_cli.py tests/test_knowledge_specialty.py tests/test_formula.py tests/test_formula_binding.py tests/test_formula_calculation.py tests/test_knowledge_api.py tests/test_business_logging.py -q -ra
if ($LASTEXITCODE -ne 0) { throw '专项回归未通过，停止真实验收' }
```

用例只提供内存Block/像素/来源/LLM桩及只读SQL断言，不生成PDF/Office/图片或调用真实渲染/云服务。覆盖错层级/缺父章/重复目录、字符位置/稳定身份、质量未知/视觉待审/实际缺页、原件Hash门、只读范围/游标释放、云许可及一次提取后补参计算。Mock通过不证明真实专项效果；skip不算通过。

## 无模型、无云的第一份真实报告

只需已有PostgreSQL。无需API、Redis、CPU/GPU Worker或模型。项目根目录执行；知识库ID已采用本项目既有库，不用手工填doc_id：

```powershell
$kbId = '158030a1-42d6-42ee-835d-02fdbff23f81'
$stamp = Get-Date -Format yyyyMMdd_HHmmssfff
python -m scripts.analyze_document
python -m scripts.analyze_document --kb-id $kbId --case outline --execute --output "results/stage35/outline_$stamp.json"
if ($LASTEXITCODE -ne 0) { throw '目录候选评估未完成，请按明确错误码检查' }
```

CLI按当前库里title Block数量选真实候选并输出实际doc_id，报告含标题片段，必须留在受限results目录。没有候选会ANALYSIS_UNAVAILABLE，不造测试文档。之后可将控制台doc_id固定给`--doc-id`，比较同一份原件；报告排他创建，旧文件不会覆盖。命令退出0只表示评估生成，**不是专项人工验收通过**。

再按真实记录选择典型候选，不重建200份索引：

```powershell
$stamp = Get-Date -Format yyyyMMdd_HHmmssfff
python -m scripts.analyze_document --kb-id $kbId --case complexity --execute --output "results/stage35/complexity_$stamp.json"
python -m scripts.analyze_document --kb-id $kbId --case coverage --execute --output "results/stage35/coverage_$stamp.json"
python -m scripts.analyze_document --kb-id $kbId --case visual_review --execute --output "results/stage35/visual_review_$stamp.json"
```

coverage/visual_review仅匹配当前摘要中真正有缺页/待审页的记录。没有候选不代表全库已证明完整。每条命令失败时先检查其安全码，不继续把旧文件当新结果。

从报告选择真实PDF/图片与页码，用户确认本次本地像素处理后，带`--doc-id <报告中的真实ID> --visual-pages <所选1～5页> --execute --output <新的JSON路径>`再检查。不要给Office逻辑页填此选项。此处必须使用实际选择，不能以示例页替代待审页。

## HTTP与公式演示

用户手动按README启动单API进程，确认Swagger新增5个POST入口。文档评估不需要GPU；公式discover使用原本地Embedding/Reranker，因此需已部署权重/显存，不额外启动CPU/GPU Worker。初次extract才需要已配置的LLM，允许当前提供方外传/费用；启用Fallback还须确认双方及最多2次POST。

下面先查候选，所用业务ID均来自实际返回。输出含原式，仅限可信终端：

```powershell
$api = 'http://127.0.0.1:8000/api/knowledge'
$body = @{kb_id=$kbId; question='请查找文档中的计算公式及变量说明'; top_k=30}
$json = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 100))
$found = Invoke-RestMethod -Method Post -Uri "$api/formulas/discover" -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 300
$found.candidates | Select-Object doc_id,chunk_id,block_id,page,raw_formula | Format-List
```

有明确目标文档时在body加入`doc_ids=@('真实doc_id')`，不要为了找不到公式自动扩大范围。空候选只表示本次检索范围没有完整公式，不能当作整库没有公式；不要重复收费解析。

用户在输出中确认要处理的候选序号、当前LLM提供方与原式语义后，再显式提取一次：

```powershell
if (-not $found.candidates -or $found.candidates.Count -eq 0) { throw '本次没有完整候选，停止提取' }
$number = [int](Read-Host '选择上面候选序号，从1开始')
if ($number -lt 1 -or $number -gt $found.candidates.Count) { throw '候选序号无效' }
$selected = $found.candidates[$number - 1]
if ((Read-Host '确认所选原式及相邻正文可发给当前LLM并接受费用；启用Fallback时含双方？输入YES') -ne 'YES') { throw '未授权云调用' }
$body = @{kb_id=$selected.kb_id; doc_id=$selected.doc_id; chunk_id=$selected.chunk_id; block_id=$selected.block_id; allow_cloud=$true}
$json = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 100))
$definition = Invoke-RestMethod -Method Post -Uri "$api/formulas/extract" -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 300
if ($definition.status -ne 'success') { throw '当前公式无法可靠结构化，不执行绑定/计算，也不自动重提取' }
$definition | ConvertTo-Json -Depth 100
```

接下来复用同一definition，空参数时应返回准确缺参列表：

```powershell
$binding = @{formula=$definition; user_inputs=@(); evidence_inputs=@()}
$json = [Text.Encoding]::UTF8.GetBytes(($binding | ConvertTo-Json -Depth 100))
$bound = Invoke-RestMethod -Method Post -Uri "$api/formulas/bind" -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 300
$bound | Select-Object status,required_variables | ConvertTo-Json -Depth 10
```

按真实业务场景输入变量值；这里的数值是用户假设，必须标记为user_input，不冒充文档事实。确认原式/单位/期间后进行一次本地安全计算，不重复提取：

```powershell
$binding.user_inputs = @(
    foreach ($name in $definition.variables) {
        $value = Read-Host "变量 $name 的明确十进制值（不填百分号/单位，不猜值）"
        $unit = Read-Host "变量 $name 的单位，未知留空；请自行确认统一口径"
        @{name=$name; value=$value; unit=$(if ([string]::IsNullOrWhiteSpace($unit)) {$null} else {$unit}); input_id='stage35-user-1'}
    }
)
$json = [Text.Encoding]::UTF8.GetBytes(($binding | ConvertTo-Json -Depth 100))
$calculated = Invoke-RestMethod -Method Post -Uri "$api/formulas/calculate" -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 300
$calculated | ConvertTo-Json -Depth 100
```

使用文档参数时改用原契约evidence_inputs，必须显式给同库真实doc/chunk/block、唯一quote/value_text及可选相邻unit_text；不可把人工值包装成证据。错误保留FORMULA_*/FORMULA_BINDING_*/FORMULA_CALC_*/EVIDENCE_*，如FORMULA_CALC_ZERO_DIVISION、EVIDENCE_STALE；数据库、模型、供应商故障保留原安全分类，不返回UNKNOWN或伪造need_variables。

## 典型案例与效果记录

### F01/F03提取格式修复与受控复验

已确认历史两次DeepSeek返回HTTP200后在`formula.py`的output校验失败，不是MinerU/Block/检索失败；由于没有保存模型响应，不能断定当时具体是哪种非法格式。现补充严格的小写右侧表达式要求和F01/F03完整JSON示例，原式逐字符一致性、受限AST和所有来源核对保持。

本次经用户专项授权，在`dkf-agent`环境运行公式提取/绑定/计算、专项HTTP、账本及日志相关本地回归，**307 passed**；HTTP由MockTransport拦截，账本请求为内存回调。没有调用真实LLM/MinerU、启动服务或修改现有PDF/标准答案/业务数据库。回归命令：

```powershell
python -m pytest tests/test_formula.py tests/test_formula_acceptance.py tests/test_formula_binding.py tests/test_formula_calculation.py tests/test_knowledge_specialty.py tests/test_formula_extract_attempt.py tests/test_business_logging.py::test_formula_failure_reason_is_a_finite_enum_and_does_not_expose_model_output -q -ra -p no:cacheprovider --tb=short
```

可复用账本实现在仓库`scripts/formula_extract_attempt.ps1`，本地素材包的`results/stage35_formula_fixture/stage35_formula_acceptance.ps1`已接入。后者位于忽略的results目录；旧ZIP包未改动，重新解压旧包会带回旧runner，不能据此认为仓库修复丢失。原PDF、标准答案和旧请求/结果均不覆盖。

账本默认遇到`extract_attempt_F01.json`即停止；有definition时复用结果，任何状态都不自动再提取。仅已有请求但没有保存结果时，使用`-RetryFailedExtract`并逐次输入`RETRY F01`，才增加一次同来源提取。原意图不删除，每次重试另建UUID意图及结果回执；后续默认运行仍会阻断。新意图核对kb/doc/chunk/block全部身份，旧版仅有Block的意图先核对原Block，再冻结本次完整身份；不是全局HTTP幂等。

真实复验前需人工确认：原请求已结束、此前可能已收费；API已在没有在途请求时手动重启以加载新Prompt；当前提供方允许接收相同证据并接受新增费用。Fallback开启时，一次逻辑提取仍可能最多两次POST。不要删除extract_attempt来绕过保护，不需要重新上传或重新解析。

确认后先只复验F01。以下命令**会调用真实LLM**，本轮未执行：

```powershell
& .\results\stage35_formula_fixture\stage35_formula_acceptance.ps1 -Step extract -CaseId F01 -AllowLLMCloud -RetryFailedExtract
if (-not $?) { throw 'F01未完成，停止后续提取并检查安全分类日志' }
```

按脚本选择与原意图同一Block，确认外传后再输入`RETRY F01`。成功保存定义后，人工核对`raw_formula`仍为原式、normalized_formula对应`r-c`、输入变量只对应R/C；再使用原`-Step calculate -CaseId F01`，复用定义完成缺参/参数/计算验收，不重复提取。F01通过后以同样方式单独复验F03（输入`RETRY F03`），核对右侧`s/q`及输入变量S/Q；名称、数学关系、变量含义和单位仍需人工判断。失败只查看安全码及有限原因，不输出或归档完整LLM响应。

下列是**选样与验收方法，不是已有成绩**。候选来自用户现有文件，缺某类样本如实记缺口，不制造数据或重新收费解析。

| 能力 | 选样与人工对照 | 建议记录 |
|---|---|---|
| 公式 | 已有完整公式；选四则、比例/百分比及至少一个缺参/除零边界；逐项对照原式和独立手算 | 完整公式发现数量、提取数学关系是否正确、参数引用/期间/单位是否正确、给定输入的精确结果 |
| 目录 | 有编号跳级/缺父章/无编号标题/不完整目录的真实文件，与原件逐条核对 | 标题检测precision/recall、可判断标题的层级/父节点准确率、未知/歧义数量；不要把未知计为正确 |
| 复杂度 | 文本为主、表格/公式密集、多栏/长文档各类候选 | 各因素的实际观测、公开分值、人工难度排序是否一致；不把启发式分数当准确率 |
| 质量 | 有真实覆盖缺口、视觉待审、清晰/模糊/倾斜扫描页；先由人工标注 | 缺页核对、风险提示precision/recall、误报/漏报及unknown；没有已标注样本时不得公布准确率 |

结果应保留doc_id、file_hash、source_parse_task_id、assessment_id/公式来源、样本数与可判断分母，区分通过/失败/未知。所有报告保留原始状态；PARTIAL_READY不因评估生成而升级，AI结构判断不冒充人工原件对照。目前真实案例、模糊/倾斜阈值有效性、公式数学/参数语义和专项整体效果均待用户验收；收到结果后再决定修缺陷或进入阶段36。
