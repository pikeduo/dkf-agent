# 预置知识库批量导入

阶段 16 只准备已有数据链路可消费的默认知识库，不新增检索或问答接口。导入脚本复用文件存储、自动 Router、Parse Cache、Chunk、Embedding 与 pgvector 入库；不直接解析或加载模型，不创建第二套真相表。真实素材、清单、脚本运行、测试与费用验收均由用户完成。

## 素材与人工清单

把**已有、合法且允许云外传**的资料放入 `data/knowledge_base/preset/`，可按子目录组织。本轮只创建 `.gitkeep`，没有复制、下载或生成任何比赛/测试文档。该目录中的素材及清单默认忽略，不提交 Git；需团队共享时用受控资料包另行分发。

准备至少十份不同 SHA256 的文档，建议覆盖 TXT、MD、CSV、DOCX、XLSX、PPTX、普通文本 PDF、扫描 PDF、图片，以及 DOC/XLS/PPT 各一份（建议 12～15 份）。还需覆盖混合内容、表格、公式、图表、Reading Order。不要放入负向坏文件或以同一 PDF 改名凑数量。

用户在素材目录手动创建 `manifest.json`：顶层仅 `version=1` 和 `documents`；每项仅 `file`、`tags`。以下仅说明格式，**不是可直接通过数量验收的完整素材清单**：

```json
{
  "version": 1,
  "documents": [
    {"file": "文本/实际文本PDF.pdf", "tags": ["text_pdf", "reading_order"]},
    {"file": "扫描/实际扫描PDF.pdf", "tags": ["scan_pdf"]},
    {"file": "实际混合工作簿.xlsx", "tags": ["mixed", "table", "formula", "chart"]}
  ]
}
```

清单必须列出目录中的全部业务文件，路径使用相对目录的正斜杠，禁止绝对路径、`..`、链接/Junction。允许标签仅 `text_pdf / scan_pdf / mixed / table / formula / chart / reading_order`，纯文字或旧 Office 项可用空标签列表。不能在清单覆盖 Provider、解析模式或 Token。

预检要求：至少十份不同内容、DOC/XLS/PPT 各有覆盖、现代格式至少三类（图片扩展名合并算一类）、七个标签全覆盖；text_pdf 与 scan_pdf 必须是不同 PDF 文档，不接受一个文件同时声明两种。**标签由人确认，脚本不以文件名/标签伪造语义分类或宣称 OCR 完整。** MD 附件、XLSX 外部资源和坏文件仍受既有 Router 完整性约束，脚本不自动抓 URL 或另建附件转换链路。

## 用户手动运行

在项目根目录激活环境，先运行只读预检，不需要数据库、Redis、模型或 Worker：

```powershell
conda activate dkf-agent
python scripts/import_knowledge_base.py --dry-run
```

预检只分块 Hash、大小与格式容器校验，不执行正文解析，不写数据库/文件或投递任务。默认不带参数同样是 dry-run。缺素材/清单、类型伪装、超限或覆盖不足非零退出，**不是自动生成样本补齐**。

正式导入前，用户按 README 检查迁移 head `0007_pgvector_index`、默认数据库/Redis、共享存储、MinerU Token/直连/预算、本地 BGE-M3/CUDA。用户启动 CPU `default_queue` 和 GPU `gpu_queue` Worker（Windows 均 solo，GPU 并发 1）。脚本直接连接项目数据库，不依赖 FastAPI；API 仅用于查询与显式恢复。确认外传和费用后执行：

```powershell
python scripts/import_knowledge_base.py --execute --allow-cloud --timeout 1800 --poll-interval 5
# 可选：导入到一个已存在且 ACTIVE 的知识库；省略时使用固定默认库。
python scripts/import_knowledge_base.py --directory "D:/自己的预置资料目录" --kb-id "实际知识库UUID" --execute --allow-cloud
```

默认库 `7f2044ca-2041-42ce-977d-40bf5c79ed40` 复用既有幂等 Seed；仅插入默认库记录，不迁移/启用扩展或安装服务。新文件逐项创建独立 `source_type=preset` 的 Document，原件复制到 `UPLOAD_DIR/<doc_id>.<extension>`，不覆盖输入目录或其他文件。普通上传仍为 `uploaded`，HTTP 接口/响应不变；无新环境变量或依赖。

新文档仅投递一次 auto 解析；CLI 有界检查数据库，完整解析 → CHUNKING 后提交 Chunk，EMBEDDING 后提交 GPU 批处理，INDEXING 后提交 pgvector 入库。下一阶段检查点先在行锁事务中提交，再发消息；脚本不调用任务主体或 MinerU GET。已有检查点不自动再次投递，活动解析仅等待，FAILED 明确报告并保留其他成功项；云短任务的检查/重试仍由既有任务完成。

默认最多等待约 1800 秒（每次 SQL 最长 10 秒，时间预算在轮次边界检查），检查间隔 5 秒；允许 timeout 1～86400、interval 1～60。Ctrl+C/超时不会取消已提交后台任务，也不会修改 Document 为 FAILED。只有全批记录经过 READY 身份与全量向量校验才返回退出码 0；其他情况返回 1，缺执行确认/非法 CLI 参数为 2，人工中断为 130。输出仅 doc_id、状态、固定安全错误码和数量，不含正文、向量、连接串或原始路径。单项 CLI 的 ERROR 是汇总失败，不代表脚本直接覆盖数据库状态。

## 重复执行与恢复

- 同库同内容复用原 doc_id，无新记录、原件或解析消息；已有 READY 再检查全部向量，不重新 Parse/Chunk/Embed/Index。改名不覆盖原文件名；相同内容但格式/来源冲突报告 PRESET_SOURCE_CONFLICT，不把普通上传悄悄改成 preset。
- CLI 中断后重新执行同一命令：读取已有状态，尚未建立后续检查点时继续推进；已有检查点不自动补投。它不是 Outbox/守护进程，CLI 退出后只运行已投递任务，不会自动提交尚未到达的下一个阶段。
- 提交确认丢失、UPLOADED 停滞或已有检查点无人消费，先查数据库/日志/Broker，再通过已有 `/process`、活动云 `/mineru` 或 `/chunk /embed /index` 显式恢复；CLI 不自动重试 FAILED，不换解析代次，不调用 Force Reparse，不假装 exactly-once。
- 改变原件 Hash 会作为新内容导入，旧文档不删除/替换；预检后原件变化在写盘前拒绝。已确认收费的失败遵循 Parse Cache 保护，不能用重新导入或更换知识库绕过缓存/预算限制。

业务日志复用每天唯一文件，新增 preset.started/imported/reused/advanced/state/failed/timeout/finished；落库/阶段提交后才记相应成功摘要，不另建分散日志或自动导出包含正文的报告。

## 人工验收与 SQL

Codex 只编写用例，不运行；单测使用元数据/状态 Stub，DB 用例 Stub 文件 IO，无真实文档样本、Parser、模型或云 API：

```powershell
python -m pytest tests/test_preset_import.py tests/test_business_logging.py -q
python -m pytest tests/preset_import_db_smoke.py tests/document_upload_smoke.py tests/knowledge_db_smoke.py -q
python -m pytest tests -q
```

1. 先对用户真实素材运行 dry-run；应报告十份以上不同内容与多格式，且无数据库记录/消息/云费用增加。人工确认标签真值及外传权限，不以预检代替质量验收。
2. 启动既有 CPU/GPU Worker 后正式执行；返回全批 READY。核对每项独立 doc_id、原始文件名、source_type=preset；XLSX 原公式、Sheet、物理/逻辑页语义和 Reading Order 保持既有约束。
3. pgAdmin 执行只读查询（替换目标库 UUID）：

```sql
SELECT d.doc_id,d.file_name,d.file_type,d.source_type,d.status,d.error_message,
       count(c.chunk_id) AS chunk_count,count(c.embedding) AS vector_count,
       count(c.chunk_id) FILTER (WHERE c.embedding IS NOT NULL AND
           (vector_dims(c.embedding)<>1024 OR abs(vector_norm(c.embedding)-1)>0.0001)) AS invalid_count
FROM documents d LEFT JOIN document_chunks c ON c.doc_id=d.doc_id AND c.kb_id=d.kb_id
WHERE d.kb_id='7f2044ca-2041-42ce-977d-40bf5c79ed40'::uuid AND d.source_type='preset'
GROUP BY d.doc_id ORDER BY d.file_name;

SELECT file_type,status,count(*) FROM documents
WHERE kb_id='7f2044ca-2041-42ce-977d-40bf5c79ed40'::uuid AND source_type='preset'
GROUP BY file_type,status ORDER BY file_type,status;

SELECT j.doc_id,j.job_id,j.state,j.batch_id,j.upload_complete,j.error_code,j.error_message
FROM document_parse_jobs j JOIN documents d ON d.doc_id=j.doc_id
WHERE d.kb_id='7f2044ca-2041-42ce-977d-40bf5c79ed40'::uuid AND d.source_type='preset'
ORDER BY j.doc_id,j.created_at;
```

预期至少十个不同 doc_id/内容、所有目标文档 READY、每份 chunk_count=vector_count>0、invalid_count=0。还需通过已有 `/chunks` 或 Parser 契约 SQL 人工对照原件：公式原式、表格、图表、扫描内容、Reading Order、page/section/source_spans 不丢失。扫描/PDF/图片与旧 Office 的 MinerU 路径必须真实 API 验收；旧 Office/布局的既知供应商质量边界仍有效。

4. 记录 doc_id、各阶段 task_id、Block/Chunk 数、批次与平台收费记录，再重复命令。预期均复用原文档/检查点，计数与 ID 不增加，无新解析批次或 GPU 推理；只看日志条数不能证明费用不增加。
5. 中断/投递异常/坏文件测试优先在隔离 Stub 用例中运行，不破坏共用数据库/模型/服务；真实失败需查相应阶段的安全错误并显式修复。不能跳过失败文件后宣称全批通过。

当前未准备十份真实素材、未执行预检/导入/回归、未确认 GPU 或真实云费用。READY 是比赛默认数据准备结果，查询功能依赖后续 Dense/RAG 阶段；本轮不提前开发阶段 17，也不宣称“用户已能问答”。

## OHR-Bench 官方素材准备（用户执行）

本节只准备阶段 16 验收用的可追溯 PDF 和后续评测 QA，不重新实现导入，不调用 Retrieval/RAG。工具为 `scripts/prepare_ohr_bench_preset.py`，没有下载功能；以下下载、预览、提取、测试及导入命令**全部由用户手动执行，Codex 不代跑**。

### 官方字段、页码与来源边界

先核对 [官方 README](https://github.com/opendatalab/OHR-Bench#overview)、[Hugging Face 文件列表](https://huggingface.co/datasets/opendatalab/OHR-Bench/tree/main)、[官方 QA](https://github.com/opendatalab/OHR-Bench/blob/main/data/qas_v2.json)。工具读取原始 `pdfs.zip` 和 `qas_v2.json`，不使用带 OCR 扰动的数据替代原件。

QA 顶层为数组，使用 `doc_name / ID / questions / answers / doc_type / evidence_source / evidence_page_no`；同时原样保留 `answer_form / evidence_context` 等原始字段。doc_name 是领域/文档身份，不是本地任意文件名。官方 [CustomPageRetriever](https://github.com/opendatalab/OHR-Bench/blob/main/src/retrievers/custom.py) 直接用 evidence_page_no 匹配 page_idx，结合 [官方 Reader](https://github.com/opendatalab/OHR-Bench/blob/main/src/retrievers/json_reader.py) 与 README 的页索引契约，本工具使用 **0-based 物理页索引**，支持单个整数或整数列表；另保存 `evidence_pages_1based=原索引+1` 供 DKF 对照，不改写原字段，也不使用 PDF 上印刷的页码标签代替物理页。

README 没有明确给出 pdfs.zip 的顶层目录名称；本轮未下载 ZIP，不能宣称已经确认其真实内部目录。工具先读取用户本地 ZIP 中央目录，记录真实成员和顶层组件，再将 doc_name+'.pdf' 按完整领域/身份后缀唯一匹配。没有匹配、多个匹配、只有 basename 相同、领域不一致、非法路径或来源未人工确认均不猜测。真实 ZIP/QA 版本有变化时以本地检查结果为准，缺口必须报告。

来源追溯依赖用户从上述官方入口下载并确认，工具记录 QA SHA256、ZIP 中央目录摘要、选中 PDF SHA256/CRC/实际路径及原始 QA ID。这些是**本地身份校验，不是官方签名，也不能证明版权授权**；中央目录摘要不是整个 ZIP 的内容 SHA256。保存原始输入及下载版本；main 分支以后可能变化，复现实验应保留相同原始文件，建议在官方 History 确认并记录固定 revision。

### 固定目录与输出含义

```text
docs/test/ohr_bench_preset/
├── source/
│   ├── pdfs.zip
│   └── qas_v2.json
└── results/
    ├── selection_plan.json
    ├── selection_report.json
    └── qa_reference.jsonl

data/knowledge_base/preset/
├── manifest.json                         # 用户审核后手动合并
├── ohr_bench/<官方领域>/<原始文档名>.pdf    # 仅显式提取后存在
└── 其他用户提供的真实多格式素材
```

- selection_plan：冻结版本/种子/预算、官方 URL、本地原始路径与身份、全体候选元数据、选中 PDF 与 QA 对应关系、候选 manifest 条目。未检查的候选 page_count/SHA 为 null；QA 最大页只作为最少页数下界。
- selection_report：选中 PDF 的实际大小、SHA、页数、官方 QA 类别/领域覆盖、被拒原因和真实缺口。`verified_coverage` 只说明本地文件/QA 结构已核对，**不说明项目 Parser/OCR 已通过**。
- qa_reference：保留官方 ID、问题、答案、文档、原始证据页及上下文，另加 `_ohr_reference` 记录源 QA/PDF 摘要、ZIP/业务目标路径和双页码表示。只供后续评测，不能作为 RAG 已测准确率的证据。

QA、GT、报告、备份都不放进 preset，避免严格 manifest 校验把它们视为业务文件。现有 `.gitignore` 的 `docs/*` 已忽略整个 docs/test；preset 也已忽略。无需增加忽略例外，不提交真实数据、QA、GT 或报告。

### 抽样策略与预算

默认 count=7（可选 6/7/8）、seed=42；按稳定身份排序并用 SHA256(seed,ID) 打破平局，不依赖进程随机 Hash、QA 数组或 ZIP 目录顺序。优先补齐 Plain Text/Table/Formula/Chart/Reading Order、为每类保留多个官方问题及新领域，再偏好较小页数下界/文件；领域范围为 Finance、Academic、Textbook、Law、Manual、Newspaper、Administration。每份至少有一条页码有效的 QA，默认每份每类最多保留三条；缺失类别、每类不足两条、数量/领域不足均列入 gaps。贪心策略不保证预算内全局最优，也不保证实际数据一定满足目标。

默认单 PDF ≤20 MiB/50 页、选中累计 ≤200 页、检查最多 64 个候选/256 MiB；大小还取当前 MAX_UPLOAD_SIZE_MB 和平台 200 MiB 的更小值。预览先只读中央目录/QA；为核对 SHA/CRC/实际页数，**少量候选完整字节会在受限内存中读取**，不落地 PDF、不提取正文或渲染、不解压整个 ZIP。拒绝加密、被修复/损坏、空页树、异常几何、超限和相同 SHA，不拆页/复制/改名凑数量。页树检查不是全面恶意文件或所有字体/内容对象检测。

每份 QA 的证据索引必须在原 PDF 的真实页范围内；不合法 QA 不进入参考集。QA 分类仅用于选样，Plain Text 不能自动等价 text_pdf，Table/Formula/Chart/Reading Order 不能证明表格/公式/图表/多栏已正确解析。text_pdf、scan_pdf、mixed 及真实多栏布局必须由用户目视确认；无法满足时报告缺口，不填造。

工具复用已声明的 PyMuPDF，仅在结构检查时惰性导入。不需要 datasets、pandas、pyarrow、官方 RAG 框架或额外模型。若当前环境确实缺少 PyMuPDF，仅用户补齐：

```powershell
conda activate dkf-agent
python -m pip install "pymupdf>=1.24.3"
```

### 1. 手动下载官方输入

在项目根目录，先核对版权及本地研究用途；以下命令拒绝覆盖已存在的下载文件。下载中断可能留下不完整文件，需用户核对，不让工具盲目重新下载或覆盖。

```powershell
$ohrSource = ".\docs\test\ohr_bench_preset\source"
New-Item -ItemType Directory -Path $ohrSource -Force | Out-Null
if ((Test-Path "$ohrSource\pdfs.zip") -or (Test-Path "$ohrSource\qas_v2.json")) {
    throw "源文件已经存在，请先核对来源/完整性，不覆盖原文件。"
}
curl.exe --fail --location --output "$ohrSource\pdfs.zip" "https://huggingface.co/datasets/opendatalab/OHR-Bench/resolve/main/pdfs.zip?download=true"
if ($LASTEXITCODE -ne 0) { throw "PDF ZIP 下载未完成，请人工核对。" }
curl.exe --fail --location --output "$ohrSource\qas_v2.json" "https://raw.githubusercontent.com/opendatalab/OHR-Bench/main/data/qas_v2.json"
if ($LASTEXITCODE -ne 0) { throw "QA 下载未完成，请人工核对。" }

# 可选完整原始文件 Hash 留档；读取整个 ZIP 可能需要较长时间。
"ZIP_SHA256=" + (Get-FileHash "$ohrSource\pdfs.zip" -Algorithm SHA256).Hash
"QA_SHA256=" + (Get-FileHash "$ohrSource\qas_v2.json" -Algorithm SHA256).Hash
```

据当前官方文件列表，pdfs.zip 约 **1.52 GB**，GitHub QA 约 **5.81 MB**；这不是 6～8 份 PDF 的下载大小，而是完整原始压缩包。按网速预留下载时间，建议至少 2 GB 可用磁盘，加上已选 PDF/后续缓存与向量的额外空间。默认提取至多约 160 MiB，预览逐个读取候选；提取整批验证需保留多个内存缓冲，预留约数百 MiB 及 PyMuPDF 自身开销，不保证原生库峰值严格等于字节预算。目标/报告发布要求本地 NTFS 等支持硬链接，网络盘或不支持的文件系统安全报错，不改系统设置。

可选参考 [OHR-Bench_v2.parquet](https://huggingface.co/datasets/opendatalab/OHR-Bench/blob/main/OHR-Bench_v2.parquet)（当前约 160 MB）或 [官方 GT 契约](https://github.com/opendatalab/OHR-Bench#dataset-preparation)。不属于工具必需输入，本轮不下载、不增加读取依赖；用户若另行下载，留在 source/受控参考目录，不放入 preset。

### 2. 离线预览与人工审核

确认两份原始文件确实来自官方入口后，执行：

```powershell
conda activate dkf-agent
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --count 7 --seed 42 --max-mb 20 --max-pages 50 --max-total-pages 200

$ohrResults = ".\docs\test\ohr_bench_preset\results"
$ohrPlan = Get-Content "$ohrResults\selection_plan.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$ohrReport = Get-Content "$ohrResults\selection_report.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$ohrPlan.documents | Select-Object doc_name,domain,zip_member,page_count,size_bytes,sha256
$ohrReport.verified_coverage | ConvertTo-Json -Depth 8
$ohrReport.gaps | ConvertTo-Json -Depth 8
$ohrReport.rejected_candidates | Format-Table doc_name,reason
```

退出码 0 表示结构/QA/数量/领域抽样目标满足，2 表示**报告已生成但存在缺口**，1 为安全错误；三者都不是解析质量或 READY 验收结果。源未确认、唯一匹配失败、坏文件等不能用手改计划跳过。允许候选拒绝后继续寻找有效样本，拒绝原因保留在报告。预算不足时人工决定是否增加限额/缩小领域，不由工具放宽；如需重跑且保留旧报告：

```powershell
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --count 6 --seed 42 --results-dir "docs/test/ohr_bench_preset/results/run-02"
```

结果目录只允许固定 results 或其子目录，已有报告一律不覆盖。**不要编辑冻结 selection_plan/qa_reference 来伪造校验**；候选 manifest 条目可复制出来人工补标签，不改原计划。所有未知 QA 类别和拒绝数量均需核对，不能误当成覆盖已通过。

### 3. 用户确认后显式提取

确认本地研究使用权、源身份和报告后执行；本地提取没有云上传，不需要 MinerU Token：

```powershell
python scripts/prepare_ohr_bench_preset.py --extract --confirm-official-source --confirm-local-use
# 使用另一个已审核的结果计划时：
python scripts/prepare_ohr_bench_preset.py --extract --confirm-official-source --confirm-local-use --plan-file "docs/test/ohr_bench_preset/results/run-02/selection_plan.json"
```

只有用户明确接受报告的真实缺口时才加 `--accept-gaps`；它不删除缺口、不绕过 PDF/来源/页码/重复/预算保护。工具重新核对源 QA、中央目录、每个选中 PDF SHA/CRC/页树、真实 QA ID/内容摘要及证据页码，再仅复制冻结原件到 ohr_bench/官方领域/。不修改 manifest、QA 或已有报告，不覆盖任何用户文件。目标已存在（即使 SHA 一样）拒绝，不自动改名造出重复版本。

写盘竞争/故障可能已发布少数完整文件，停止后用户按计划 Hash 核对，不自动清理/补传。同一个原始 PDF 永远只算一个来源文档。提取后对照报告逐份运行 Get-FileHash，并打开 PDF 人工核对所选 QA 的证据页、表格/公式/图表及多栏顺序；不能只看 QA 类别。

### 4. 手动标签、多格式补齐及 manifest 合并

工具的 `manifest_candidate` 严格只有 file/tags，初始 tags=[]。用户依据真实 PDF 内容填写适用标签，再将已审核的条目保存为 **results/reviewed_manifest_entries.json**（JSON 数组，只含 file/tags）。不在 preset 保存这份参考文件。补齐合法真实 TXT/MD/CSV/DOCX/XLSX/PPTX/图片及 DOC/XLS/PPT；不能把 PDF 改后缀伪造 Office。特别核对不同的 text_pdf 与 scan_pdf，以及 mixed，OHR-Bench 不保证自动提供全部这类标签。

下列**用户手动合并**保留原 manifest 中的全部条目，重复路径直接停下，不覆盖旧标签；备份放 results 而不是 preset。新提供的其他真实格式文件也必须由用户加入清单。

```powershell
$ohrManifest = ".\data\knowledge_base\preset\manifest.json"
$ohrReviewed = @(Get-Content "$ohrResults\reviewed_manifest_entries.json" -Raw -Encoding UTF8 | ConvertFrom-Json)
$ohrExisting = if (Test-Path $ohrManifest) {
    Get-Content $ohrManifest -Raw -Encoding UTF8 | ConvertFrom-Json
} else { [pscustomobject]@{version=1; documents=@()} }
if ($ohrExisting.version -ne 1) { throw "清单版本不符，请先人工核对。" }
foreach ($ohrEntry in $ohrReviewed) {
    if (@($ohrExisting.documents.file) -contains $ohrEntry.file) { throw "已有同路径条目，请人工处理，不覆盖标签。" }
}
if (Test-Path $ohrManifest) {
    $ohrBackup = Join-Path $ohrResults ("manifest-backup-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    if (Test-Path $ohrBackup) { throw "备份文件已存在，不覆盖。" }
    Copy-Item -LiteralPath $ohrManifest -Destination $ohrBackup
}
$ohrMerged = [ordered]@{version=1; documents=@($ohrExisting.documents) + $ohrReviewed}
$ohrMerged | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $ohrManifest -Encoding UTF8
```

这是用户对正式清单的明确编辑，不是抽样工具的自动写入。检查顶层仍只有 version/documents，每项只有 file/tags，无 Provider、parse_mode、Token 或额外字段；确认无丢失旧 Office/工作簿/文本条目及未列入清单的业务文件，再执行只读预检：

```powershell
conda activate dkf-agent
python scripts/import_knowledge_base.py --dry-run
```

只有进一步确认每份素材版权、**允许向第三方 MinerU 外传**、账户额度与 CPU/GPU 环境后，由用户正式导入：

```powershell
python scripts/import_knowledge_base.py --execute --allow-cloud --timeout 1800 --poll-interval 5
```

### 分层验收与风险

第一层（阶段 16 数据导入）：素材覆盖预检通过；至少十份不同 SHA/独立来源的真实文档，包含旧 Office 和多类现代格式；全部目标 READY；上文 SQL 核对 chunk_count=vector_count>0、1024 维、归一化有效/无非法向量；表格、原式、页码、source_spans 可追溯；重复导入 doc_id/Chunk/向量稳定，真实 MinerU 批次/平台费用无重复提交。不能以文件数量或 READY 代替内容质量验收。

第二层（OHR-Bench 后续评测准备）：逐 PDF 对应官方 doc_name 和 ZIP 真路径/SHA；每份至少一条原始官方 QA，证据索引有效；保留五类真实标准答案并报告缺口；必要时人工对照官方 GT。**只准备评测数据，不实现评测接口、不算回答准确率；READY 绝不代表通过 OHR-Bench 问答评测。**

[官方版权声明](https://github.com/opendatalab/OHR-Bench#copyright-statement) 限研究/非商业用途。HF 元数据的 CC-BY 标注与正文限制不能互相替代，亦存在 [官方许可元数据修订讨论](https://huggingface.co/datasets/opendatalab/OHR-Bench/discussions/6)。原 PDF 可能各有独立版权；公开下载不意味着有云外传授权。无法确认时禁止执行 --allow-cloud 正式导入，仅做本地候选/结构检查与报告；可请求提供方授权或改用明确允许外传的其他真实素材，不以 Native 显式覆盖掩盖不完整解析。

工具本身无网络请求、模型、Parser、数据库或 Celery 调用，不自动安装依赖；云费用仍由已有导入任务与 MinerU 预算保护。本轮未下载真实数据、未运行准备工具、未提取样本或测试，实际匹配/五类 QA 覆盖、PDF 结构与权限仍待用户确认。

仅用户执行对应单元用例（字段/中央目录/状态 Stub，不生成真实 PDF）：

```powershell
python -m pytest tests/test_prepare_ohr_bench_preset.py tests/test_preset_import.py -q
```
