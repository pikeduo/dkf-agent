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

## 三个公开基准的解析质量验收（用户执行）

本轮仅使用 **OHR-Bench、Microsoft OCB、OmniDocBench**，不引入第四个公开基准。已有 OHR 脚本、默认 7 份抽样、导入业务、数据库、Celery 和 Embedding 均不改变。新增脚本只离线准备/比较；所有下载、预览、提取、评测、测试和导入均由用户手动执行。Codex 本轮没有运行它们，也没有生成真实样本。

### 来源、用途与真实缺口

| 基准 | 官方参考与职责 | 准备目标 / 限制 |
|---|---|---|
| OHR-Bench | [官方仓库](https://github.com/opendatalab/OHR-Bench)、[官方数据](https://huggingface.co/datasets/opendatalab/OHR-Bench)：PDF + QA/证据 | 继续上节 7 份；保留 QA ID/问题/答案/证据/物理页；用于字面保留检查和后续问答评测 |
| Microsoft OCB | [官方仓库](https://github.com/microsoft/OfficeComprehensionBench)、[官方 HF](https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark)：Office 文档事实与问答断言 | 优先 File Fidelity，每类 2 份 DOCX/XLSX/PPTX；原生、可取得、许可明确才选；多文件依赖可能使总数超过 6（最多 12），缺任一文件不拆 QA |
| OmniDocBench | [官方仓库](https://github.com/opendatalab/OmniDocBench)、[官方 HF](https://huggingface.co/datasets/opendatalab/OmniDocBench)：页面区域/文字/表格/公式/顺序 GT | 默认 5 个原始页面图像（4～6）；保留对应标注。当前 HF 文件列表提供 images/ 和 OmniDocBench.json，不能据此宣称有完整原 PDF 映射 |

OHR 可用用户指定的 [HF 镜像](https://hf-mirror.com/datasets/opendatalab/OHR-Bench)作为下载传输入口；镜像不是新的官方来源，仍须留存官方 revision/原文件摘要。现有 OHR 工具不改 URL 或默认抽样行为。

核对 [OCB 官方生成器](https://github.com/microsoft/OfficeComprehensionBench/blob/main/generate_query_ndjson.py)：HF 的 `reference_files/question/expected_assertions/file_format` 分别映射为 NDJSON 的 `filepath/query/gold/original_format`；NDJSON 的 id 是源行序号，不是额外官方 UUID。工具用源文件 SHA + HF 行号或 NDJSON id 关联，原始记录原样保留；track/feature/version/domain/weights 不丢失。默认直接读取本地官方 Parquet；已有官方生成 NDJSON 时显式指定 schema，不运行官方默认联网生成器。

OCB 的 [reference_files](https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark/tree/main/reference_files) 并不保证直接提供每类两份；其 [下载转换脚本](https://github.com/microsoft/OfficeComprehensionBench/blob/main/download_and_convert_files.py) 另有外部 URL、PDF/HTML 转换及 Adobe 依赖。本工具不使用那条转换链，也不引入 Java/Tika/Adobe/LibreOffice。需要转换、登录、原件缺失、许可未知时报告缺口。外部文件仅在官方 URL manifest 明确原生同格式且 conversion_required=false、用户逐文件确认后可用。

OCB [数据卡与 NOTICES](https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark/blob/main/README.md) 的 CDLA-Permissive-2.0 不能替代外部文档各自许可。Omni/OHR 原 PDF 可能另有研究/非商业限制。**本地使用确认不等于向 MinerU Cloud 外传授权**；未取得外传权限时只做本地准备、结构检查和已有导出的离线评测，禁止正式云导入。

### 十三种格式矩阵：潜在来源不等于当前已覆盖

本轮未下载真实素材，当前新增工具没有任何已验证的实际数量；下表是来源能力，实际数字由用户运行 `benchmark_coverage.py` 汇总三个计划。

| 格式 | OHR | OCB 本工具 | Omni 本工具 | 尚需确认 |
|---|---|---|---|---|
| PDF | 官方原始 PDF/QA | 不用转换 PDF 冒充 Office | 仅另有官方原件和明确页映射时可选 | text_pdf/scan_pdf/mixed 目视确认 |
| DOCX | 无 | 原生 Office + 断言，实际可用性待查 | 无 | 原件/许可/多文件 QA 完整 |
| XLSX | 无 | 原生 Office + 断言 | 无 | Sheet/公式/图表人审 |
| PPTX | 无 | 原生 Office + 断言，实际可用性待查 | 无 | 图文/图表关系人审 |
| JPG | 无 | 不选 | 只选实际提供的对应图像 | 不保证有该后缀 |
| JPEG | 无 | 不选 | 只选实际提供的对应图像 | 不保证有该后缀 |
| PNG | 无 | 不选 | 只选实际提供的对应图像 | 不把同页图像和 PDF 算独立来源 |
| DOC | 无 | 不选 | 无 | 补合法真实旧 Office；本轮无官方 GT |
| XLS | 无 | 不选 | 无 | 同上 |
| PPT | 无 | 不选 | 无 | 同上 |
| CSV | 无 | 即使官方托管，当前抽样目标仍只选三类 Office | 无 | 补真实素材；质量未覆盖 |
| TXT | 无 | 不选 | 无 | 补真实素材；质量未覆盖 |
| MD | 无 | 不选 | 无 | 补真实素材/附件；质量未覆盖 |

仍必须满足原有至少 10 个不同 SHA、**独立来源内容**、DOC/XLS/PPT、至少三类现代格式、七个真实标签。不修改 manifest 契约/导入预检，不靠不同文件名、同一 PDF 的多个表示或 QA 标签凑数。图像页是否来自同一原文档未知时，不计为已确认的多份独立原文档。

### 本地目录、来源审核与输出

```text
docs/test/ocb_preset/
├── source/
│   ├── dataset_info.json             # 官方 HF API 元数据，包含 id/sha/cardData
│   ├── hosted_files.json             # 同 revision 的 HF reference_files tree，完整且无分页遗漏
│   ├── ocb_qna_data.parquet           # 或用户已有官方 NDJSON
│   ├── ocb_file_sources.parquet       # 可选；外部原生文件必须核对该官方 URL manifest
│   ├── source_review.json            # 用户逐原件确认许可/原生/不需转换；默认可为空数组
│   ├── reference_files/              # 官方直接托管原件，完整原始相对路径
│   └── external_files/               # 可选，已审核的官方 manifest 原生文件
└── results/                          # 或 run-02 等子目录
    ├── selection_plan.json
    ├── selection_report.json
    ├── qa_reference.jsonl            # 原始 QA/断言 + 全部依赖/SHA
    └── quality_report.json           # 用户运行后产生，不是业务内容

docs/test/omnidocbench_preset/
├── source/
│   ├── dataset_info.json
│   ├── OmniDocBench.json
│   ├── images/                       # 原始图像，不渲染/缩放/转换
│   ├── original_pdfs/                # 可选：用户确实取得的官方原始 PDF
│   └── page_sources.json             # 可选：从官方出处人工核实的原 PDF 页映射
└── results/                          # 同样三份计划/报告/qa_reference；后者是区域 GT，不是 QA

data/knowledge_base/preset/
├── manifest.json                     # 用户审核合并，工具绝不覆盖
├── ohr_bench/
├── ocb/reference_files/              # 或 ocb/external_files/
└── omnidocbench/images/              # 可选 omnidocbench/original_pdfs/
```

OCB `source_review.json` 是用户审核记录，**不是官方 schema，也不证明自动授权**。每条必须填实际 reference_file、官方 source_url、许可及以下确认，不填虚构文件名；可先用 `[]` 预览全部缺口，审核后用新 results 子目录重跑：

```json
[
  {
    "reference_file": "从官方QA原样复制的相对路径.xlsx",
    "source_url": "该文件实际官方固定revision下载地址",
    "license": "人工核对的实际许可及NOTICES依据",
    "original_native": true,
    "conversion_required": false,
    "local_use_confirmed": true
  }
]
```

这只是字段示意，不能直接运行当真样本。托管原件还会验证 HF tree 大小及 LFS SHA256（普通 Git 文件验证 blob oid）。外部文件核对官方 manifest 的 filename/url/original_format/final_format/conversion_required；只有审核记录而无官方 manifest 不接受。未知原生身份不依据后缀猜测。

Omni 保留 `page_info.image_path/page_no/width/height/page_attribute`、`layout_dets.anno_id/category_type/poly/order/text/latex/html/ignore` 和 extra.relation/merge 原值。图像按完整 image_path 匹配及核对尺寸/SHA；`page_info.page_no` 不从文件名推算，也不直接当原 PDF 物理页。业务图片一律 page=1，GT 页号单独留存。

可选 `page_sources.json` 每条包含 `image_path/pdf_file/original_pdf_identity/pdf_sha256/page_number_1based/source_url/source_revision/source_mapping_reference/local_use_confirmed/official_mapping_confirmed`。它是**用户从官方原件/标注核对的辅助记录，不冒充官方字段**；PDF 路径须在 original_pdfs/，revision 与 dataset_info 一致，SHA/真实页数检查通过。整份 PDF 只复制一次，不拆页，图像与 PDF 同来源只算一个文档来源；报告区分表示数量与已确认原文档来源数量。两种表示都导入可能分别消耗 MinerU 额度，先人工审核是否都需要，不能拿两份表示凑独立来源。无法拿到官方原件/映射时不创建该文件，报告 `original_pdf_mapping_unavailable`，不自行用 image_to_pdf.py 补原件；PDF 点坐标与图像像素也不会自动推断转换。

OCB 按 File Fidelity、缺口格式、feature、文件体积及 seed=42 确定性贪心选样，依赖整体进入结果。Omni 按 GT text/table/equation_isolated、明确多栏/中文属性补覆盖，默认 5 页、seed=42；复杂表格合并单元格及中文属性仍需人审。分类仅帮助选样，不等于解析已通过。默认每文件 ≤20 MiB；OCB 最多检查 64 个/256 MiB、选中 ≤128 MiB；Omni 同样有界，候选未读取不得伪造 SHA/尺寸。可用 --max-mb 下调到不大于本地 MAX_UPLOAD_SIZE_MB；导入仍按现有应用限制最终检查。

报告不覆盖：已存在目标/结果即停止；重新预览用 `--results-dir .../results/run-02`。来源元数据、版本、GT/SHA/计划均冻结；提取前全部复核，变更即拒绝，不手改冻结计划。仅显式提取复制真实原件；链接/Junction、越界、压缩包非法成员、重复身份/同 SHA、安全预算均拒绝。复制使用现有 OHR 的独占发布，不改该脚本；磁盘失败可能已发布少数完整文件，需用户核对，不自动删除/改名。

### Windows 手动下载、预览及提取

以下命令在项目根目录、用户确认本地研究使用许可后执行。先核对解释器；读取官方 Parquet 可选 PyArrow、图像结构检查使用 Pillow，仅缺少时用户安装，不升级全环境。已有官方 NDJSON 则不需要 PyArrow（外部 manifest Parquet 除外）：

```powershell
conda activate dkf-agent
python -c "import sys; print(sys.version); print(sys.executable)"
python -m pip show pyarrow Pillow
# 仅确实缺少时由用户手动安装；本轮 Codex 不执行，也不改 environment.yml。
python -m pip install pyarrow
python -m pip install Pillow
```

用户可定义一次安全下载辅助函数（以下函数由用户执行，不属于工具联网能力）：

```powershell
function Get-BenchmarkFile {
    # 只下载用户指定的公开来源，不覆盖既有原始文件；中断后由用户核对残留。
    param([string]$Url, [string]$Target)
    if (Test-Path -LiteralPath $Target) { throw "目标已存在，拒绝覆盖。" }
    New-Item -ItemType Directory -Path (Split-Path $Target -Parent) -Force | Out-Null
    curl.exe --fail --location --output $Target $Url
    if ($LASTEXITCODE -ne 0) { throw "下载未完成，人工核对文件后再决定下一步。" }
}

$ocbSource = ".\docs\test\ocb_preset\source"
Get-BenchmarkFile "https://huggingface.co/api/datasets/microsoft/OfficeComprehensionBenchmark" "$ocbSource\dataset_info.json"
$ocbRevision = (Get-Content "$ocbSource\dataset_info.json" -Raw -Encoding UTF8 | ConvertFrom-Json).sha
Get-BenchmarkFile "https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark/resolve/$ocbRevision/data/ocb_qna_data.parquet" "$ocbSource\ocb_qna_data.parquet"
Get-BenchmarkFile "https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark/resolve/$ocbRevision/data/ocb_file_sources.parquet" "$ocbSource\ocb_file_sources.parquet"
Get-BenchmarkFile "https://huggingface.co/api/datasets/microsoft/OfficeComprehensionBenchmark/tree/$ocbRevision/reference_files?recursive=true&limit=1000" "$ocbSource\hosted_files.json"
# 人工核对 API 响应 Link 分页头；若还有 next，按官方 API 完整取得所有页再合并，不能忽略后续页。
```

按官方 QA 查 File Fidelity 参考文件（只显示字段，不解析文档）：

```powershell
python -c "import pyarrow.parquet as pq; rows=pq.read_table('docs/test/ocb_preset/source/ocb_qna_data.parquet').to_pylist(); print([(r['reference_files'],r['track'],r['feature']) for r in rows if str(r['track']).lower().replace(' ','').replace('_','')=='filefidelity'])"
```

用户审核 QA/文件/NOTICES 后填写 source_review.json；从实际 HF tree 复制路径，**不要填示例名字**。逐份下载所需原件，保留 reference_files/ 原路径；只接受无 `..`/反斜杠/非法字符的真实 tree 路径。可对用户已审核条目手动下载（外部文件需另行按官方 manifest 核对，不自动转换）：

```powershell
$ocbHosted = Get-Content "$ocbSource\hosted_files.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$ocbReviews = Get-Content "$ocbSource\source_review.json" -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($review in $ocbReviews) {
    $matches = @($ocbHosted | Where-Object { $_.type -eq 'file' -and $_.path -eq ('reference_files/' + $review.reference_file) })
    if ($matches.Count -ne 1) { throw "原件不是唯一托管路径，请人工核对外部来源，停止批量下载。" }
    $relative = $matches[0].path
    if ($relative -match '\.\.|[:\\]' -or $relative.StartsWith('/')) { throw "路径不安全。" }
    Get-BenchmarkFile "https://huggingface.co/datasets/microsoft/OfficeComprehensionBenchmark/resolve/$ocbRevision/$relative" "$ocbSource/$relative"
}

python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42
# 如已有官方 NDJSON：保留完整官方生成记录，不自己猜字段或删除多文件依赖。
# python scripts/prepare_ocb_preset.py --confirm-official-source --schema ndjson --qa-file file_fidelity_queries.ndjson --results-dir docs/test/ocb_preset/results/run-02
Get-Content docs/test/ocb_preset/results/selection_report.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object selected_count,gaps,format_coverage
```

Omni 用户下载真实标注和图像（不要运行官方图片转 PDF 脚本）。以下 `hf download` 仅由用户运行，HF CLI 缺少时用户按其官方文档手动安装；也可逐图使用上面的 curl。默认下载整组 images 以便可靠选样，约 1.55 GB 全库量级，标注约 42 MB，实际以固定 revision 文件列表为准。OCB 全仓约 3 GB，但此处只下载 ~0.5 MB QA、~31 KB URL 表、元数据和已审核少量 Office；选中大小以报告为准。OHR 下载量见上节，另预留缓存/向量与本地报告磁盘空间。

```powershell
$omniSource = ".\docs\test\omnidocbench_preset\source"
Get-BenchmarkFile "https://huggingface.co/api/datasets/opendatalab/OmniDocBench" "$omniSource\dataset_info.json"
$omniRevision = (Get-Content "$omniSource\dataset_info.json" -Raw -Encoding UTF8 | ConvertFrom-Json).sha
Get-BenchmarkFile "https://huggingface.co/datasets/opendatalab/OmniDocBench/resolve/$omniRevision/OmniDocBench.json" "$omniSource\OmniDocBench.json"
# 用户已确认 source/images 尚不存在时才执行，避免 CLI 复用/覆盖已有用户图像。
if (Test-Path "$omniSource\images") { throw "图像目录已存在，请核对来源，不覆盖。" }
hf download opendatalab/OmniDocBench --repo-type dataset --revision $omniRevision --include "images/*" --local-dir $omniSource
python scripts/prepare_omnidocbench_preset.py --confirm-official-source --count 5 --seed 42
Get-Content docs/test/omnidocbench_preset/results/selection_report.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object selected_count,gaps,format_coverage
```

预览退出 0 仅表示报告已生成（可能有缺口），非零为安全错误；OCB/Omni 不沿用 OHR 的退出 2 语义。报告中的手动质量/原 PDF 缺口始终保留，**不能宣称抽样通过就是质量通过**。读完整 gaps/candidate_inventory，用户接受后才执行：

准备命令不是下载命令，`--confirm-official-source` 仅确认来源，不会创建缺少的输入。若未完成上述手动下载，首先会报 `BENCH_MISSING`，并指出白名单输入项（例如 dataset_info.json）；空下载文件为 `BENCH_EMPTY`，真实大小/读取预算超限才是 `BENCH_LIMIT`，目录充当文件为 `BENCH_SOURCE`，磁盘访问故障为 `BENCH_IO`。不要为缺文件问题扩大预算。OCB 的 source_review.json 须由用户手动创建，可先填写 `[]` 预览未审核缺口，不由工具替用户确认许可。

仅检查输入是否存在（不读取正文、不下载或执行抽样）：

```powershell
Test-Path -LiteralPath docs/test/ocb_preset/source/dataset_info.json -PathType Leaf
Test-Path -LiteralPath docs/test/ocb_preset/source/hosted_files.json -PathType Leaf
Test-Path -LiteralPath docs/test/ocb_preset/source/ocb_qna_data.parquet -PathType Leaf
Test-Path -LiteralPath docs/test/ocb_preset/source/source_review.json -PathType Leaf
Test-Path -LiteralPath docs/test/omnidocbench_preset/source/dataset_info.json -PathType Leaf
Test-Path -LiteralPath docs/test/omnidocbench_preset/source/OmniDocBench.json -PathType Leaf
```

目标存在也不代表完整：按固定 revision 及元数据来源核对文件大小/下载状态，不覆盖或伪造输入。OHR 的 ZipInfo 测试替身须显式包含 header_offset（真实 ZipFile 读取中央目录时赋值），不修改生产校验来迁就不完整的 Stub。修复后由用户重跑本文单测；预期不再出现 header_offset 的 AttributeError，输入缺失/空文件/超限和脱敏断言通过。

```powershell
python scripts/prepare_ocb_preset.py --extract --confirm-official-source --confirm-local-use --accept-gaps
python scripts/prepare_omnidocbench_preset.py --extract --confirm-official-source --confirm-local-use --accept-gaps
# 原 OHR 工具仍按上节提取命令执行，不替换/重写。

python scripts/benchmark_coverage.py --ohr-plan docs/test/ohr_bench_preset/results/selection_plan.json --ocb-plan docs/test/ocb_preset/results/selection_plan.json --omni-plan docs/test/omnidocbench_preset/results/selection_plan.json --output docs/test/ocb_preset/results/format_coverage.json
```

不接受缺口则不要提取，先补原件/权限、在新 results 子目录重预览。人工打开所有选中原件、核对源 QA/标注/结构、SHA 与真实标签；按照上节手动合并 manifest_candidate 的 file/tags（tags 初始为空）。补齐合法 DOC/XLS/PPT、文本素材等，保留已有用户资料。**QA、GT、source_review、报告、备份不放 preset**；.gitignore 已覆盖 docs/test 与真实 preset，无需增加例外。

### 从真实数据库导出，绝不重新解析以评测

用户在 pgAdmin 对目标预置库执行以下只读 SQL；导出单元格原始 JSON 文本（不是带 CSV 转义的文本），手动保存到任一对应 results/parsed_export.json。它不导出向量、文件路径或凭据，但包含正文，需受控访问、不提交 Git。不在本轮由 Codex执行数据库查询：

```sql
SELECT jsonb_build_object('documents', COALESCE(jsonb_agg(jsonb_build_object(
  'doc_id', d.doc_id, 'kb_id', d.kb_id, 'file_name', d.file_name,
  'file_type', d.file_type, 'file_hash', d.file_hash, 'status', d.status,
  'blocks', COALESCE((SELECT jsonb_agg(jsonb_build_object(
    'block_id', b.block_id, 'block_index', b.block_index, 'page', b.page,
    'section', b.section, 'block_type', b.block_type, 'text', b.text,
    'bbox', b.bbox, 'source', b.source) ORDER BY b.block_index)
    FROM document_blocks b WHERE b.doc_id=d.doc_id), '[]'::jsonb),
  'chunks', COALESCE((SELECT jsonb_agg(jsonb_build_object(
    'chunk_id', c.chunk_id, 'kb_id', c.kb_id, 'doc_id', c.doc_id,
    'text', c.text, 'page_start', c.page_start, 'page_end', c.page_end,
    'section', c.section, 'metadata', c.metadata)
    ORDER BY (c.metadata->>'chunk_index')::int)
    FROM document_chunks c WHERE c.doc_id=d.doc_id), '[]'::jsonb)
) ORDER BY d.file_name), '[]'::jsonb)) AS export_json
FROM documents d
WHERE d.kb_id='7f2044ca-2041-42ce-977d-40bf5c79ed40'::uuid AND d.source_type='preset';
```

以 file_hash 唯一关联官方原件，重名不猜；同 SHA 多个文档导出时先由用户限定目标 doc_id，不自动择一。Export/GT 最大输入 96 MiB，文本编辑距离 ≤10000 字符且乘积 ≤200 万，超限 not_applicable，不截断得分；大库由用户分批导出对应样本。

### 三类独立质量指标与人工判定

| 基准 | 离线确定性诊断 | 人工核查 / 不可评测边界 |
|---|---|---|
| OHR | 官方 evidence_context 为单个非空字符串时，NFC/空白归一化后检测指定物理页内字面保留 | 上下文为列表/缺失则不可自动评测；答案数值/表格/公式、原件阅读顺序、重要证据进入 Chunk 全部逐 QA 人审。存在字面内容不证明答案正确 |
| OCB | 完整多文件身份匹配、Chunk source_spans 精确偏移与 Hash | 每条 expected_assertions 生成人工待审核，不做字符 OCR 分数、子串当事实正确或 LLM 评委。核对 Word 表格/事实，Excel 原公式/Sheet，PPT 图文/图表关系；缺依赖不可评测 |
| Omni | 人工一对一匹配区域后，文本字符 NED/CER；公式去包装/空白后的精确一致；完整同页映射的顺序对一致性；同图像像素空间的轴对齐 bbox IoU；粗类别对应 | 不是官方整体成绩/CDM/COCO AP。Mask/ignore 跳过；merge/truncated/parent-child 等关系保守人工核对；缺映射/HTML/坐标变换/指标依赖返回 not_applicable，不填零 |

所有诊断保留原文档身份、官方 reference/anno_id、doc_id/Block 或 Chunk、GT 页/位置、method/status/value。`manual_pending` 等待人审，`not_applicable` 表示缺条件；有真实可比数据才 `measured`，缺失不进平均分分母。粗粒度类别与单个 IoU 不等于完整布局检测通过。Inline 公式、图表语义、章节归属等未自动评分项目由用户逐项看原件与现有 Block/Chunk，保留失败证据，不能据局部均值隐藏遗漏。

Omni 官方主代码/数据版本之间的匹配、归一化、CDM、TEDS 和阅读顺序指标会变化。当前脚本标识 `dkf-offline-diagnostic-v1-not-official-score`；NFC/空白归一化和顺序对一致性只是清楚标明的本地诊断，不冒充 [官方端到端评测](https://github.com/opendatalab/OmniDocBench#end-to-end-evaluation)。三个报告各自汇总，不生成混合官方总分。OCR/公式识别错误记录，不在 Adapter 中硬编码补文字或改名称。

Omni 用户手工在原图/GT 与项目 Block 间确认区域映射，保存 results/region_mapping.json；示例只是契约，实际填写官方 image_path、anno_id 与已有 UUID，不能用文件名/坐标自动猜：

```json
{"matches":[{"reference_id":"实际官方image_path","anno_id":123,"doc_id":"实际UUID",
             "block_ids":["实际BlockUUID"],"confirmed":true,
             "coordinate_space":"image_pixels","bbox_verified":true}]}
```

同 y、多栏、合并结构不能简单按 y 排序。顺序指标仅完整唯一 GT order 与一对一 block_index 对应、同一图片业务第 1 页时计算；缺区域则 not_applicable。GT page_no 单独保存，不与图片 page=1 混淆。PDF 没有可信点数→像素变换时 IoU 不评测；本工具不缩放猜变换、不更改任何 Block/Chunk 顺序。

可选表格 TEDS：需要 GT html 与已导出 Block 本身为有效单表 HTML；TSV/普通文字不能自动造 HTML。用户另行从**可信官方固定 commit**准备 [table_metric.py](https://github.com/opendatalab/OmniDocBench/blob/main/src/metrics/table_metric.py)，放 source/official_metrics/，核对其代码和依赖后显式提供 --teds-module；该操作会执行该本地 Python 模块，请勿提供未知代码。若缺依赖/HTML，则 not_applicable，默认不加载模块。仅缺少时由用户手动安装 `python-Levenshtein apted lxml tqdm`；不安装官方全套模型/TeX/CDM。不随工具下载该模块。报告记录模块 SHA；这里只调用官方 TEDS 算法比较人工对应的单表，不复现官方端到端匹配/总分。HTML 仅加 html/body 包装，不补造表格/合并单元格。

用户执行质量对照（输出已存在须用新文件名；脚本不修改已生成报告或解析结果）：

```powershell
python scripts/evaluate_benchmark_parsing.py --dataset ohr --references docs/test/ohr_bench_preset/results/qa_reference.jsonl --export docs/test/ohr_bench_preset/results/parsed_export.json --output docs/test/ohr_bench_preset/results/quality_report.json
python scripts/evaluate_benchmark_parsing.py --dataset ocb --references docs/test/ocb_preset/results/qa_reference.jsonl --export docs/test/ocb_preset/results/parsed_export.json --output docs/test/ocb_preset/results/quality_report.json
python scripts/evaluate_benchmark_parsing.py --dataset omnidocbench --references docs/test/omnidocbench_preset/results/qa_reference.jsonl --export docs/test/omnidocbench_preset/results/parsed_export.json --mapping docs/test/omnidocbench_preset/results/region_mapping.json --output docs/test/omnidocbench_preset/results/quality_report.json
# 仅准备好可信官方指标代码/HTML/依赖后，在上一命令追加：
# --teds-module docs/test/omnidocbench_preset/source/official_metrics/table_metric.py
```

用户另存 `manual_review.jsonl`，逐条记录 dataset/document_identity/official_reference_id/criterion/证据页或 Sheet、doc_id、Block/Chunk、检查方式、pass/fail/pending、失败证据与审核人。OCB 每条原始断言单独记录、全部依赖一起查；若能从已有结构化字段确定性核对公式/数值，也必须保存原始断言到该字段的人工映射，不能以子串替代事实验证。本轮工具不将人工记录混入自动均值或宣称已得官方 QA 分数。

### 两层验收及用户测试

第一层仍按本文原有 dry-run → 用户启动既有服务/Worker → 许可/预算确认 → 正式导入 → SQL 检查。READY 全量验证 `chunk_count=vector_count>0`、1024 维/有限归一化、pgvector、幂等和真实 MinerU 批次消费；格式/来源条件不足时不宣布阶段 16 完整通过。确认本地及第三方外传版权、费用、CPU/GPU 环境后才由用户执行：

```powershell
python scripts/import_knowledge_base.py --dry-run
# 只有满足现有预检且人工确认许可/云预算/环境后才执行；否则停止补素材。
python scripts/import_knowledge_base.py --execute --allow-cloud --timeout 1800 --poll-interval 5
```

第二层逐样本/区域/断言核对，保存文字、表格、公式、顺序、页码/来源、Chunk 证据的独立诊断与人工失败案例。OHR 的标准 QA、OCB 的问答断言仅留作本阶段解析对照及后续 Retrieval/RAG 评测；Omni 没有完整 QA。**READY ≠解析正确≠问答准确率通过。**

新增用例仅元数据/结果/接口 Stub，不生成真实 PDF/Office/图片；全部由用户执行：

```powershell
python -m pytest tests/test_public_benchmark_tools.py tests/test_prepare_ohr_bench_preset.py -q
python -m pytest tests/test_preset_import.py -q
# 用户已确认隔离数据库/原有依赖与执行范围后，才运行既有全回归：
python -m pytest tests -q
```

预期：HF/NDJSON 适配、原始字段保留、多文件依赖、确定性选择、未授权/转换原件拒绝、路径/重复/几何保护、GT 页与业务页分离、不自动补零、公式无硬编码、同页完整顺序映射、HTML/指标依赖边界、现有 Chunk Hash/偏移、十三格式缺口和不覆盖输出断言通过。用例通过不替代真实结构读取/下载/提取/解析质量/云权限验收；本轮所有运行结果尚未验证。未开展阶段 17。

工具本身无网络请求、模型、Parser、数据库或 Celery 调用，不自动安装依赖；云费用仍由已有导入任务与 MinerU 预算保护。本轮未下载真实数据、未运行准备工具、未提取样本或测试，实际匹配/五类 QA 覆盖、PDF 结构与权限仍待用户确认。

仅用户执行对应单元用例（字段/中央目录/状态 Stub，不生成真实 PDF）：

```powershell
python -m pytest tests/test_prepare_ohr_bench_preset.py tests/test_preset_import.py -q
```
