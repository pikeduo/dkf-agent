# 预置知识库批量导入

阶段 16 只准备已有数据链路可消费的默认知识库，不新增检索或问答接口。导入脚本复用文件存储、自动 Router、Parse Cache、Chunk、Embedding 与 pgvector 入库；不直接解析或加载模型，不创建第二套真相表。真实素材、清单、脚本运行、测试与费用验收均由用户完成。

## 优先使用：阶段 16/17 小样本联合冒烟

已经下载 OHR-Bench 和 OmniDocBench 时，先用 `scripts/smoke_stage16_17.py` 跑少量真实样本，**无需 OCB、许可 Excel、正式 manifest 或 pgAdmin 手动导出**。这是独立测试库的上传/处理与 Dense 验收，不替代下文的正式预置导入标准；不进入阶段 18、LLM 或 RAG。

以下命令全部由用户手动执行。所有步骤省略 `--execute` 都是 dry-run：只说明操作，不写文件、不连接 API/数据库/模型。路径固定相对脚本所在项目根目录；测试文件、原始 QA/GT、快照和报告只写到 `docs/test/stage16_smoke/<run>/`，已被现有 `.gitignore` 保护，不进入正式 preset。

### 1. 离线准备真实样本

先确认本地已有 `ohr_bench_preset/source/pdfs.zip`、`qas_v2.json` 和 `omnidocbench_preset/source/OmniDocBench.json`、`dataset_info.json`、`images/`（均在 `docs/test/` 下）。复用既有 PyMuPDF/Pillow 容器检查，不安装依赖、不下载新数据，不读取正文或运行 Parser/OCR。

```powershell
conda activate dkf-agent
python scripts/smoke_stage16_17.py --dry-run
# 确认原件官方来源及本地研究使用权限后：
python scripts/smoke_stage16_17.py --step prepare --run run-01 --execute --confirm-local-use
notepad docs/test/stage16_smoke/run-01/plan.json
```

默认固定种子 42，约 **5 PDF + 3 GT 图像 + 20 官方 QA**。PDF 只读 ZIP 必要成员，检查 CRC、SHA256、真实页数、重复内容；优先五类证据和不同领域，每文件上限 20 MiB、50 页，整批 PDF 页预算 150，最多检查 64 个候选。Omni 只选已存在且完整、尺寸与 GT 相符的图像；不生成图片或把 GT 当 QA。只做真实来源候选分类，不据此填写七类正式内容标签。

OHR 原始字段完整保留在 `official_qa.jsonl`；`text` 映射 `plain_text`，`multi` 单独排除，证据页 **0-based → 1-based** 存双份对照，排除缺失/歧义 PDF 和超出真实页数的 QA。Omni 完整 GT 保存在 `omni_gt.jsonl`；项目图片页=1，不猜原 PDF 物理页。约 20 题不足、证据类别缺失或图像不足会如实报告，准备返回 2、处理拒绝继续，不补造数据。可在**新的** `--run run-02` 中显式调整 `--qa-count`、`--pdf-count`、`--image-count`、`--max-total-pages`（≤200）；QA 至少每 PDF 一题。`--image-count 0` 是用户显式不测图像，不证明图片链路通过。

可选自有现代 Office：在准备命令上追加 `--extra "D:/自己的真实资料/文件.xlsx"`，可重复至多三份，仅 DOCX/XLSX/PPTX，检查真实容器与 SHA，不生成自定义问答、不改后缀；来源单列 user，内容质量待人工核对。旧准备计划、正式 manifest、已有素材/报告均不改变。已有 run 不能重新 prepare；部分磁盘故障保留已发布文件，换新 run，不自动清理。

### 2. 启动现有服务，指定独立 ACTIVE 测试库

按 [README](../README.md#3-初始化与启动) 确认已有 PostgreSQL/Redis、迁移 `0007_pgvector_index`、BGE-M3/CUDA、LlamaIndex core/NLP 本地缓存、共享存储及 MinerU Token。**不重复部署或执行迁移**；未就绪时先由用户按原部署说明处理。

分别在三个 PowerShell 窗口激活环境并启动：

```powershell
conda activate dkf-agent
uvicorn app.main:app --reload
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -Q gpu_queue --pool=solo --concurrency=1 --hostname=gpu@%h -l info
```

在验收终端用**已有创建接口**新建空的独立库（名称须唯一）；已有独立 ACTIVE 库则直接填写 UUID，不重复创建。不要使用默认正式库或包含其他文件的业务库：

```powershell
$smokeKb = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/admin/knowledge-bases" -ContentType "application/json; charset=utf-8" -Body '{"name":"stage16-smoke-run-01","description":"隔离的小样本真实验收"}'
$smokeKbId = $smokeKb.kb_id
```

重新开终端时只需 `$smokeKbId = "已创建的实际UUID"`。API 使用 root_path/其他本机端口时，相应调整 URL 和工具的 `--api-url`；工具只接受本机 HTTP(S)，不向公网传管理请求。

### 3. 显式执行上传 → READY

**公开下载不等于取得第三方云外传权。** 人工确认全部样本（含额外 Office）允许上传 MinerU，核对账户剩余额度/费用；默认最多 8 份、PDF 页预算 150 加图片，实际路由/收费以平台为准。没有外传许可就停在离线准备，不执行本节。

```powershell
python scripts/smoke_stage16_17.py --step process --run run-01 --execute --kb-id $smokeKbId --confirm-test-kb --allow-cloud --timeout 1800 --poll-interval 5
```

提交前检查 SELECT 1、vector 扩展/HNSW/head、API、同 UUID ACTIVE 库、应用/Broker/Backend Redis PING、CPU/GPU 队列响应（GPU 并发1）、本地模型配置/Tokenizer/权重目录、CUDA 和 Token 是否配置。**不加载模型、不验证云 Token、不替用户确认版权/额度**；GPU/鉴权的实际可用性仍由真实任务证明。API/Worker 必须使用与脚本相同的 `.env`、数据库和存储，预检不能证明远端进程的所有配置一致。

逐文件使用原 `POST /documents`，固定 `parse_mode=auto`、独立 doc_id；通过已有 `/chunk /embed /index` 推进，已有任务标识只等待，不调用内部任务主体。状态变化及每 30 秒等待摘要可见。FAILED、超时、队列不消费、身份不符均非成功；不自动 `/process`、Force Reparse 或重试非幂等 POST。超时/中断不取消后台任务。

命令结束自动执行只读 SQL 验证/导出：按测试库+样本 Hash 找文档，Block/Chunk 稳定序号、当前解析代次、source_spans 精确字符和 Hash、冻结模型/编码身份、`chunk_count=vector_count>0`、1024 维/单位范数（误差≤1e-4），READY 和当前入库检查点。不会导出向量、私有配置、Token 或签名 URL；**正文/官方答案会进入本地受控报告/导出，不要公开**。同一解析代次出现多个 Job 要排查；重复 process 的已有 READY 文档必须保持任务/批次/Block/Chunk 不变。

首次 process 返回 0 只表示自动链路校验通过，报告仍 **BLOCKED：Dense 未执行**。只需验证已有 READY 或观察超时后状态，不必重新上传：

```powershell
python scripts/smoke_stage16_17.py --step verify --run run-01 --execute --kb-id $smokeKbId
```

中断后重复同一 process 命令：按同库 SHA 复用已有文档；有意图却没有持久化记录/任务标识时 `SMOKE_UNCERTAIN` 停止，先按 doc_id、events、当天业务日志核对，不能删除意图或改 run 来强行重复收费。硬中断可能留下 `run.lock`，确认该运行的旧 CLI 进程已退出后，用户才可删除这**一个**锁；不删业务日志锁或整个数据目录。FAILED 先人工修复原因，工具不自动恢复失败代次。

### 4. Dense 与报告（只读）

全部 READY 后，先在 GPU Worker 窗口 **Ctrl+C 停止空闲 Worker**，避免 RTX 4060 8GB 同时加载两份模型；保留 PostgreSQL，检索本身不依赖 API/Redis/Worker。不停止还在处理中的 GPU 任务，不由工具关闭服务。

```powershell
python scripts/smoke_stage16_17.py --step retrieve --run run-01 --execute --kb-id $smokeKbId
# 报告已自动生成；可独立重新生成，不连接服务/模型：
python scripts/smoke_stage16_17.py --step report --run run-01 --execute
$smokeReport = Get-ChildItem docs/test/stage16_smoke/run-01/reports/*-report.md | Sort-Object Name | Select-Object -Last 1
notepad $smokeReport.FullName
```

原样 `questions` 调用既有 `DenseRetriever(top_k=20)`，共享既有 BGE-M3 Query Adapter，无 LLM/答案生成/文档重新向量化。逐项核对原 chunk_id、同 KB、文件身份、正文/页码/section/source_spans 和模型身份；同一题重查一次看 ID 稳定，检索前后只读快照必须不变。模型/数据库异常停止后续题，其余标记 BLOCKED，不反复重载模型；空结果、未命中不能算通过。

报告统计 **文档 Hit@1/5/10、MRR@20**（截断排名，不是全库 MRR），以及可判定物理证据页的 Hit@1/5/10；页命中使用实际 source_spans，不能仅靠跨多页范围。无法判定为 not_applicable，列单独分母。严格小样本 PASS 要求链路全通过、全部选中官方题文档 Hit@10、可判定题证据页 Hit@10、同题稳定和查询无写入变化；这不是官方推荐准确率阈值。

自动复用 `evaluate_benchmark_parsing.py` 的纯诊断函数，不再要求 SQL 手工复制：OHR 官方页字面证据/字符来源，Omni 原 GT 与 Block/Chunk 对应。Omni 区域 CER/TEDS/公式/阅读顺序等没有人工一对一映射时为 not_applicable/manual_pending，**不为几张图自动猜区域**。OHR 未字面匹配为 review_required；表格/公式/视觉顺序和语义正确性仍待人工核对。自动结构与 Dense PASS 可以与解析质量 BLOCKED 同时存在，不能混为完整质量通过。

每次生成新的时间戳 JSON/Markdown，包含每文件失败阶段、Block/Chunk/向量检查、逐题排名/指标、任务/批次和潜在费用（实际金额未知，人工查 MinerU）。退出码：dry-run/无缺口准备/单独链路通过=0，已运行验收不通过=1，缺配置/许可/输入/不确定提交=2，中断=130；只读 report 按联合结果返回，文件仍会保存。

### 已有失败报告的只读复核

`SMOKE_RETRIEVAL_PREREQUISITE` 表示全部目标文档的 READY/证据/向量校验未通过，**Dense 未执行、Query 模型未加载**，不是 GPU 故障诊断。报告保留 BLOCKED，未执行的 Hit/MRR 为 `null`，不能当作零命中率。Markdown 单独列每文件的失败阶段/错误码；当前代次 MinerU Job 的 `EMPTY_BLOCK` 等固定代码不再归并为 `DOCUMENT_FAILED`，无错误保持 `null`，未知代码脱敏为 `UNKNOWN_ERROR_CODE`。历史失败 Job 不用于推断新代次错误。

旧快照不改写；仅重新 `report` 仍会读旧导出，不能还原当时丢失的错误码。用户用同一个运行和知识库执行只读复核，再查看新报告（无需重启 API/Worker，不会上传、解析、向量化或投递任务）：

```powershell
conda activate dkf-agent
$smokeKbId = (Get-Content docs/test/stage16_smoke/run-01/execution.json -Raw | ConvertFrom-Json).kb_id
python scripts/smoke_stage16_17.py --step verify --run run-01 --execute --kb-id $smokeKbId
$smokeReport = Get-ChildItem docs/test/stage16_smoke/run-01/reports/*-report.md | Sort-Object Name | Select-Object -Last 1
notepad $smokeReport.FullName
```

已有 FAILED 不因报告修复变为 READY，仍返回 FAIL。重新执行 `retrieve` 时，前置未通过会保存 `executed=false / executed_qa_count=0`，返回明确的前置错误，并且不调用 Retriever。PowerShell 找不到 `code` 仅说明 VS Code CLI 不可用；记事本或 `Get-Content -Encoding utf8 $smokeReport.FullName` 均可查看，不需要安装新依赖。

若日志为 `MinerU remote_state=done → result_downloaded → EMPTY_BLOCK`，说明云端与 ZIP 下载已完成，但 Adapter 遇到某个非 image/chart 的空文本项，**不等于整个 PDF 没有正文**。先按报告中的 doc_id/job_id/batch_id，在 MinerU 平台找到**原有批次**，下载原结果 ZIP，提取唯一 `content_list.json`（或 `*_content_list.json`），保留 ZIP 和原始 JSON，不编辑正文/表格/公式。人工放在独立诊断目录，例如 `docs/test/stage16_smoke/run-01/mineru_debug/<doc_id>/`，保持三份结果分开且不覆盖旧材料，再提供文件供只读诊断。此操作由用户执行，Codex 不代取签名链接或请求云端；没有取得旧结果前，不重传、Force Reparse 或直接跳过空正文/表格/公式。失败结果未通过 Adapter 时通常尚未发布 Parse Cache，因此不能假设 `data/parsed/` 已保存相应 ZIP；旧结果无法取得时如实报告该诊断缺口。

三个层次始终分开：

- **小样本真实冒烟：PASS / FAIL / BLOCKED**，以实际报告为准。
- **阶段16完整正式验收：BLOCKED / 待完成**。原导入器的 ≥10 独立内容、DOC/XLS/PPT、≥3 现代格式、七类真实标签、正式默认 preset 库 READY/向量/幂等/人工质量/费用要求完全不变；报告只列本次小样本缺口，不猜正式素材是否齐全。
- **官方基准完整质量评测：NOT_APPLICABLE**。没有执行官方全量评测或 RAG 问答，READY、GT 类别与文档命中都不等于回答准确率。

Mock 用例仅用户手动执行，不运行 Parser/真实数据库/模型或云端，不生成真实 PDF/Office/图片：

```powershell
python -m pytest tests/test_smoke_stage16_17.py -q
```

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

用户对当前真实 QA 的统计为 text=3409、table=2275、formula=1223、chart=765、reading_order=691、multi=135；Newspaper=0。代码**不硬编码这些数量**，每次从原始 QA 统计 `source_qa_inventory.raw_evidence_source_counts/raw_domain_counts`。仅将官方 text 映射为内部 plain_text；qa_reference 中原 evidence_source/ID/问题/答案/页码均不改，归一类别只进入 _ohr_reference.category。已有 plain_text 兼容保留。

multi 识别为 `multi_evidence_unsupported`，在源计数和拒绝计数单列，不映射为任一五类、不拆成虚构的多个问题，第一版不进入五类抽样参考。未知类别仍单独报告 unknown_evidence_source，不混为 multi。计数发生在结构筛选前，不代表全部行已可用于抽样或质量验收。

默认七领域目标不自动降低；缺 Newspaper 仍有真实领域缺口。用户可显式 --domains 选择实际六领域，count=7 仍要求七份独立 PDF，并非七个领域各造一份。`domain_coverage` 分别保存 requested/selected/excluded_by_user/not_selected/source_domains_without_qa，即使不请求 Newspaper 也记录其源零 QA、未选中。新统计只进入新计划/报告，旧来源摘要契约及已冻结计划保持不变。

默认单 PDF ≤20 MiB/50 页、选中累计 ≤200 页、检查最多 64 个候选/256 MiB；大小还取当前 MAX_UPLOAD_SIZE_MB 和平台 200 MiB 的更小值。预览先只读中央目录/QA；为核对 SHA/CRC/实际页数，**少量候选完整字节会在受限内存中读取**，不落地 PDF、不提取正文或渲染、不解压整个 ZIP。拒绝加密、被修复/损坏、空页树、异常几何、超限和相同 SHA，不拆页/复制/改名凑数量。页树检查不是全面恶意文件或所有字体/内容对象检测。

每份 QA 的证据索引必须在原 PDF 的真实页范围内；不合法 QA 不进入参考集。QA 分类仅用于选样，Plain Text 不能自动等价 text_pdf，Table/Formula/Chart/Reading Order 不能证明表格/公式/图表/多栏已正确解析。text_pdf、scan_pdf、mixed 及真实多栏布局必须由用户目视确认；无法满足时报告缺口，不填造。

工具复用已声明的 PyMuPDF，仅在结构检查时惰性导入。不需要 datasets、pandas、pyarrow、官方 RAG 框架或额外模型。若当前环境确实缺少 PyMuPDF，仅用户补齐：

```powershell
conda activate dkf-agent
python -m pip install "pymupdf>=1.24.3"
```

### 1. 手动下载官方输入

使用下节 [统一下载工具](#统一下载工具用户执行)，不再手工创建目录或运行 curl。用户确认本地研究许可后，在项目环境执行：

```powershell
python scripts/download_benchmarks.py --dataset ohr --scope required
python scripts/download_benchmarks.py --dataset ohr --scope required --verify-only
```

首次创建 source/，自动冻结 HF revision 和官方 GitHub QA commit；pdfs.zip 与 qas_v2.json 原字节不改、不整体解压。OHR QA 与 PDF 来自两个独立版本，下载器记录而不伪造两者一致性；后续准备仍实际检查文档/QA/页码匹配。

预计 ZIP 约 **1.52 GB**、QA 约 **5.81 MB**，实际以固定来源元数据为准。至少留 2 GB 下载空间，再加抽样原件/缓存/向量；下载器逐文件检查剩余空间且流式写盘，不另建 HF 全量缓存。只读验证需完整读取 ZIP 计算摘要，可能耗时。默认提取至多约 160 MiB；提取验证及 PyMuPDF 仍需数百 MiB 内存余量。正式发布要求本地 NTFS 等支持硬链接，失败不降级覆盖式 rename，不改系统设置。

可选 [OHR-Bench_v2.parquet](https://huggingface.co/datasets/opendatalab/OHR-Bench/blob/main/OHR-Bench_v2.parquet) 或官方 GT 不在 required 下载范围，用户另行准备时留在 source，不放 preset。

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

此次兼容修复后，用实际六领域在 run-03 中生成**新**计划；不要重新写入已有根目录/run-01/run-02 报告。以下仅由用户运行，包含少量 PDF 结构检查，不是 Codex 的执行授权：

```powershell
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --domains finance academic textbook law manual administration --results-dir docs/test/ohr_bench_preset/results/run-03
Get-Content docs/test/ohr_bench_preset/results/run-03/selection_report.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object gaps,source_qa_inventory,domain_coverage
```

退出 0 只说明请求范围的抽样结构目标满足，退出 2 为已生成报告但仍有真实缺口；两者均不代表解析或数据验收通过。run-03 已存在会拒绝覆盖，不自动清理、改名或重新提取。

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

本轮仅使用 **OHR-Bench、Microsoft OCB、OmniDocBench**，不引入第四个公开基准。已有 OHR 脚本、默认 7 份抽样、导入业务、数据库、Celery 和 Embedding 均不改变。准备/比较脚本保持离线，只有统一下载器可以联网；所有下载、预览、提取、评测、测试和导入均由用户手动执行。Codex 本轮没有运行它们，也没有生成真实样本。

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

### 统一下载工具（用户执行）

`scripts/download_benchmarks.py` 是唯一正式联网入口；三个 prepare 工具及质量对照仍离线。Codex 只编写代码、Mock 用例和说明，**没有运行下载器、请求真实数据、安装依赖或执行测试**。

#### 环境与逐条命令

全新环境先按 README 创建 dkf-agent；已有环境不重建。独立 Windows PowerShell 中执行，确认解释器/pip 一致，仅补缺少的包：

```powershell
Set-Location "D:\python project\dkf-agent"
conda activate dkf-agent
python --version
python -m pip --version
python -m pip show httpx pyarrow Pillow
# 以下三行仅缺哪个才运行哪个，不整体升级。
python -m pip install httpx
python -m pip install pyarrow
python -m pip install Pillow
python -m pip check

$env:HF_ENDPOINT = "https://hf-mirror.com"
python scripts/download_benchmarks.py --help
python scripts/download_benchmarks.py --dataset ohr --scope required
python scripts/download_benchmarks.py --dataset ocb --scope metadata
python scripts/download_benchmarks.py --dataset omni --scope metadata
python scripts/download_benchmarks.py --dataset omni --scope candidate-images --count 5
# 人工查看候选依赖和 NOTICES、编辑 source_review.json 后才执行：
python scripts/download_benchmarks.py --dataset ocb --scope reviewed
```

httpx 已在 environment.yml 声明，本轮不增加依赖声明。PyArrow 是现有 Parquet 离线工具的可选读取依赖，下载器验证/QA 预览需要它；Pillow 验证图像。不要安装 datasets/pandas、官方 RAG/评测模型或 Office 转换器。HF_ENDPOINT 仅是当前终端传输设置，脚本不读取应用 .env、不改变部署环境。

首次运行以 `Path(__file__).resolve().parents[1]` 找项目根，用 `mkdir(parents=True, exist_ok=True)` 创建所需 source/及 reference_files/、images/。不会预先建立空的正式目标，OCB 缺少审核文件时只独占初始化 `[]` 并提示“尚未审核”；已有审核/报告/素材不改。用户从其他工作目录执行时使用下载脚本绝对路径，数据仍写回同一个项目，不随当前目录漂移。

下载前无需创建任何数据目录；用户运行后布局如下（按 scope 才产生对应文件）：

```text
docs/test/
├── ohr_bench_preset/source/
│   ├── dataset_info.json
│   ├── pdfs.zip
│   └── qas_v2.json
├── ocb_preset/source/
│   ├── dataset_info.json
│   ├── hosted_files.json
│   ├── ocb_qna_data.parquet
│   ├── ocb_file_sources.parquet
│   ├── source_review.json           # 默认 []，用户审核后编辑
│   └── reference_files/<真实官方路径>
└── omnidocbench_preset/source/
    ├── dataset_info.json
    ├── OmniDocBench.json
    └── images/<真实标注路径>
```

每个 source/另有本工具私有 `.download/`：完整固定 Tree、逐文件 spec/大小/SHA 回执、GitHub QA 版本；OCB 的 `ocb_candidates.json`、Omni 的 `omni_candidates.json` 是**下载候选诊断**，不替代 results/的正式 selection_plan/report/qa_reference。中断使用目标旁的 .part；运行中有 .download.lock，正常退出删除本进程锁。异常断电遗留锁须人工确认无进程后处理，工具不删其他进程锁。所有真实输入/私有状态/报告均在已忽略的 docs/test；本轮没有创建这些运行目录。

#### 参数与下载范围

| 参数 | 含义 / 默认 |
|---|---|
| --dataset | 必选 ohr / ocb / omni |
| --scope | OHR: required；OCB: metadata / reviewed / hosted-tree / native-library；Omni: metadata / candidate-images / all-images |
| --hosted-file | 仅 OCB，canonical hosted_files.json 或独立副本 hosted_files.structured.json；hosted-tree 默认副本，其他模式默认 canonical |
| --replace-hosted-tree | 仅 OCB hosted-tree 的普通运行：校验新树后备份旧字符串表并原子替换 canonical；不允许与 dry-run/verify-only/list-missing 或 canonical hosted-file 参数组合 |
| --endpoint | 只允许 https://hf-mirror.com 或 https://huggingface.co；显式参数 > HF_ENDPOINT > 国内镜像 |
| --revision | 可选实际 40 位 HF commit SHA，不接收 main 或空字符串；缺省首次自动解析，以后固定复用 |
| --count | Omni 候选图像 4～12，默认 5；不等于最终抽样数 |
| --seed | 稳定候选种子，默认 42 |
| --retries | 暂时错误额外重试 0～5 次，默认 3 |
| --timeout | 每次读取超时 10～300 秒，默认 60；连接固定 10 秒 |
| --dry-run | 普通范围联网列元数据、不写目录/原件；native-library 离线生成完整计划/许可模板/自有报告，不下载原件 |
| --list-missing | 离线列当前 scope 缺失项；有缺口退出 2；不作完整性保证 |
| --verify-only | 离线对当前 scope 全部正式输入做大小/摘要/实际格式验证，缺失/坏文件失败 |
| --help | 只显示参数，不联网 |

OHR required 先冻结/取得 GitHub QA，再下载 HF 原 ZIP；不取扰动 OCR、Parquet 或额外模型。OCB metadata 只取 Dataset API 原始 JSON、完整 Tree 对象及两个小 Parquet。官方 `data/ocb_source_urls.parquet` 原字节保存为本地 `ocb_file_sources.parquet`，兼容现有工具；不会请求旧错误来源名。OCB 全仓约 3 GB，此模式通常只需约 0.5 MB QA、31 KB 来源表及元数据，实际大小以固定文件树为准。

OCB metadata 后输出**实际** track/后缀分布，完整 QA 依赖及拒绝行保存在 .download/ocb_candidates.json，不输出问题/答案。按用户已确认的官方分类，qa_record 显式接受 File Fidelity Excel、File Fidelity PowerPoint、File Fidelity Word，并兼容旧通用 File Fidelity/FileFidelity/file_fidelity；原始 track/QA/多文件依赖不变。分类先于 File Fidelity 字段校验，Domain 即使用不同字段也明确排除为 qa_track，而不是误报已支持分类的结构错误；未知 schema 不猜作 NDJSON。原始 Parquet 仅读取，未知原始字段也保留在 reference.original，不清洗/重写。Domain 和未知类似名字仍拒绝，不用 startswith 或文件后缀推断分类。用户当前统计为 Domain=120、Excel=277、PowerPoint=238、Word=383；程序实际统计而非硬编码数量，不将 Domain 当选样候选。

#### OCB 兼容诊断：保留正常 dataset_info，仅重新获取对象树

用户已独立确认 dataset_info 的 id/sha/cardData/严格 JSON 均正常、非有限值数为 0，最新 source_record 已通过，本轮不修改或重新下载它。source_record 仅读取该 JSON、检查仓库/版本/cardData 并对列出的输入做 Hash，**不验证 hosted_files 的对象结构，也不读取 Parquet 行**。当前明确故障是 hosted_files.json 的 113 条字符串不符合对象契约，不能继续归因于 cardData、NaN 或 Unicode；条数是用户观测，不硬编码。历史笼统错误没有堆栈不能可靠追溯；诊断通过实际失败步骤定位，不虚构原因。

| 步骤标识 | 表示 | 处理边界 |
|---|---|---|
| json_read / json_utf8 / json_syntax / json_strict | 指定 JSON 读取、UTF-8、语法或严格编码失败 | 原数据不清洗；不把 NaN/Infinity 转 null，不自动修复 Unicode |
| source_identity / source_hash / source_card | source_record 内的身份、输入摘要或 cardData 类型 | 标识白名单输入项；正常元数据不重写 |
| review_schema | 审核 JSON 结构失败 | 不覆盖或批准人工审核 |
| hosted_tree | 字符串列表、缺大小/OID、无效 LFS、重复对象 | 拒绝伪造补字段，从官方固定 SHA 取新完整树 |
| qa_read / parquet_decode / qa_rows_schema / qa_strict / qa_track | QA 输入/解码、行结构、严格编码或分类 | 与 dataset_info 区分；原始 QA/Track 不改 |

`--diagnose-inputs` 只读显示 source_record/审核表/文件树/QA 成功步骤及失败标识，不生成报告、不读取 Office/PDF 原件、不联网。source_record=OK 后报 hosted_tree 表示后续对象树失败；旧程序的笼统错误不再混为同一步。

原 hosted_files.json 为字符串时，不将路径包装成缺字段的伪对象。用户手动执行以下最短恢复流程，使用已验证 dataset_info.sha，无需复制 SHA 或重下 Parquet；仅显式 --replace-hosted-tree 授权校验后备份、替换该表：

```powershell
conda activate dkf-agent
$env:HF_ENDPOINT = "https://hf-mirror.com"
python scripts/download_benchmarks.py --dataset ocb --scope hosted-tree --replace-hosted-tree
python scripts/prepare_ocb_preset.py --confirm-official-source --diagnose-inputs
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42 --results-dir docs/test/ocb_preset/results/run-04
```

hosted-tree 只取得同 SHA 的完整 HF API Tree，保留 type/path/size/oid/lfs（官方文件确有 LFS 时）及未知原字段；新增 source/hosted_files.structured.json 和独立 .download/ocb-structured-tree.json。所有分页 Link 必须继续指向同仓库/同 SHA Tree API，官方 next Link 的 API 传输仍走已选镜像；重复页/路径、第二页失败等不发布半份树。完整仓库树保存在私有缓存，正式 hosted_files 只保留 reference_files/ 下的原对象，不取 siblings/SDK 字符串列表替代。私有缓存记录实际 page_count/每页条数/had_next/complete，不保存分页游标/签名链接；验证分页数量、条数总和、终页、版本、树 Hash 及实际对象字段后才发布副本。已有副本无效/不一致也拒绝覆盖。缺少正常 dataset_info 时停止，不自行重新获取它。

显式替换还要求副本与同 SHA 的已验证树完全一致。旧字符串表原字节按 SHA256 独占保存至 source/.download/backups/hosted_files-<完整SHA256>.json，校验备份后才在同目录写完整临时文件、再次校验并用原子 replace 切换 canonical；从不先删目标。备份冲突、输入在操作期间变化或替换失败均安全停止，旧原字节和已验证副本保留；不要在运行时并发手工编辑 source。只有旧字符串列表允许修复，其他不一致的正式对象表仍拒绝覆盖；已经一致时幂等跳过。不会改 dataset_info、旧树缓存、审核、Parquet 或抽样报告。正常下载随后复用新完整缓存，不再读旧字符串缓存。

如果暂不愿替换 canonical，可不带替换参数仅取副本，后续 metadata/reviewed/prepare 使用 --hosted-file hosted_files.structured.json。新副本是正式对象数组，不是下载候选报告。用户也可在显式替换前先离线核验副本：

```powershell
python scripts/download_benchmarks.py --dataset ocb --scope hosted-tree
python scripts/download_benchmarks.py --dataset ocb --scope hosted-tree --verify-only
```

run-04 必须是尚未使用的新目录；存在则人工选择新编号，不清理旧报告。新的对象树改变了 canonical 输入 Hash，因此基于旧表冻结的计划不应继续提取，也不得手改计划 Hash；保留旧计划作为历史记录，用新目录重新预览。使用 structured 副本冻结的有效计划仍通过其实际文件名复核。已有人工审核记录不变，用户自行审核新报告的 gaps/candidate_inventory；未取得原件或许可时缺口继续保留。

预期诊断显示 source_record=OK、hosted_tree=OK、qa_read=OK，并报告实际四类 Track 数量；Domain 不进入抽样候选。若后续 QA 严格编码失败，错误必须指向 ocb_qna_data.parquet/qa_strict，而非重下 dataset_info。空审核表允许诊断/缺口预览，不代表可以下载 reviewed 原件，也不代表三类 Office 已准备齐全。镜像固定 Tree/Link 的真实兼容性及新树内容仍由用户执行确认，本轮未联网验证。

OCB reviewed 只下载许可明确的**托管原生 DOCX/XLSX/PPTX**；source_review.source_url 填对应 **官方 huggingface.co 的固定 SHA resolve 地址**，即使传输走镜像，来源身份仍为官方。当前每个相交 File Fidelity QA 的所有依赖必须同时审核；无 QA 关联、遗漏依赖、超 64 个/256 MiB、单份超 20 MiB 均拒绝整批，不拆 QA 成单文件。外部来源返回 DOWNLOAD_EXTERNAL，用户按官方 manifest 手动取得原生文件到 external_files/后交现有准备器检查；不请求审核表中任意 URL、不自动 PDF/HTML 转换。这可能导致三类 Office 每类两份的缺口，必须如实记录，不能降低验收标准。

#### OCB 全量原生素材库下载（用户执行）

统一入口仍为 scripts/download_benchmarks.py，新增 --scope native-library；配套实现位于 scripts/ocb_native_download.py，没有另建下载 CLI。本模式只建立可重复使用的本地原始素材库，**不替代每类两份的预置抽样，不生成业务 manifest、不复制到 preset、不解析、不导入或评测问答**。Codex 本轮只读检查了目录/JSON 与两个 Parquet 的元数据列，未执行新模式、真实下载、格式校验、测试或 API。

本地检查确认：dataset_info 的 SHA 为 0104ac2f88348ecf1148781b85fdb6a189b59671，hosted_files 当前为 113 个对象，而非旧字符串列表；QA 1018 行，来源表 301 行。来源真实列是 filename/url/original_format/required_format/convert，当前 convert 是字符串 yes/no。运行时仍核对完整分页树、摘要、源 JSON 与 Parquet，不将上述观测数字硬编码到工具。用户提供的单文件 QA 初筛是子集，不代表完整文件库数量。

##### 文件身份与原生筛选

- 读取全部 QA、完整来源表和 HF 树的文件身份并集，包括 Domain、File Fidelity、多文件依赖及没有 QA 的官方素材。reference_files/name 与官方 name 的明确前缀统一后精确匹配，不按 basename/模糊文件名猜匹配；大小写冲突、越界、链接拒绝。
- 同文件的多条 QA 引用只算一份，保留源 Parquet SHA+QA 行号、原 track 和完整依赖，不输出问题/答案正文，不修改官方 Parquet。
- 默认 DOCX/XLSX/PPTX，可加 CSV/XLSM。只有 original_format=required_format=实际目标后缀且 convert 为明确 no/false/布尔 false 才是原生候选。空值、不明状态、缺来源或多个冲突 manifest 记录分别列缺口；PDF/HTML/ZIP 转换不下载、不伪装后缀。
- 同身份同时在 HF 和外部来源出现时优先固定 SHA 的 HF 原件，不重复下载外部替代版本，保留 size/OID/LFS 原对象。HF 托管也不自动证明原生或许可，仍需官方原生记录和人工审核。
- 第三方必须是官方 manifest 中的精确原始 URL、原生格式且逐文件获许可。只接受无凭据公开 HTTPS；HTTP、登录/密钥链接、未知大小、未审跳转都报缺口，不自动改协议或抓登录态。正常 URL 路径空格交给 HTTP 编码，来源身份保留原值。

##### 模式、参数与目录

| 模式/参数 | 行为 |
|---|---|
| 默认 / --dry-run | 离线生成完整计划、许可模板、初始自有状态报告；不建立 HTTP 客户端，不读原件 |
| --execute | 用户显式下载逐文件已审的原件，未审项列缺口，不代批准 |
| --allow-external | 仅 execute，额外允许已核对的官方第三方原生来源；否则只下载已审 HF |
| --resume | 仅 execute，显式继续已有执行报告/断点，范围、版本、来源和预算必须一致 |
| --verify-only | 离线复核本地文件大小/摘要/容器，更新本工具报告；不联网或修改原件 |
| --list-missing | 离线核对并输出未取得/无效/阻止项，不把仅存在等同下载完整 |
| --native-formats | 默认 docx xlsx pptx，可选 csv xlsm；不扩展业务格式契约 |
| --native-results-dir | 默认 OCB results 根，允许新子目录；相对路径按脚本项目根解释 |
| --workers | 1～3，默认/上限 3；每原件独立客户端/断点，整个 CLI 持有数据集锁 |
| --max-file-mb | 默认 200 MiB、允许 1～512；CSV 另受 32 MiB 完整性检查上限 |
| --max-total-mb | 默认 8192 MiB、最大 102400，并发原子预留本轮容量，外部响应长度取得后计入 |
| --timeout / --retries | 复用连接 10 秒、默认读 60 秒、额外重试 3 次和有界退避 |

用户首次运行自动建所需目录，不预建空正式原件：

```text
docs/test/ocb_preset/
├── source/
│   ├── dataset_info.json / hosted_files.json      # 已有固定来源，不改
│   ├── ocb_qna_data.parquet / ocb_file_sources.parquet
│   ├── source_review.json                       # 只由用户审核编辑
│   ├── reference_files/                         # HF 原件
│   ├── external_files/                          # 第三方原生原件
│   └── .download/                               # 身份/Range/ETag/SHA 回执
└── results/
    ├── native_download_plan.json
    ├── native_download_report.json
    ├── license_review_candidates.json
    └── 既有抽样计划/报告                          # 绝不修改
```

计划保存固定源摘要、完整 QA 依赖、官方身份及 HF 已知大小。外部大小默认 null，报告给出 HF 已知字节、外部未知数量、按限制得到的保守上界和执行预算，不把上界当真实总量。仅执行时读第三方 GET 响应头取 Content-Length，不落地探测正文，之后再取得原字节；缺长度或超量不发布。

plan/许可模板独占创建，同参数重复可复用；新范围/预算/来源用新 results 子目录，不覆盖旧计划。dry-run 保留已有运行报告；只有本工具拥有、同 plan_sha256 的 native_download_report 才能更新，保留最多 50 次安全状态/HTTP/Hash 历史，不覆盖其他报告。许可文件与下载报告分离，不因下载成功填批准值。

##### 可复制的 PowerShell 操作

以下全部由用户执行，先确认 Python/pip 属于 dkf-agent，复用已声明 httpx 和现有 PyArrow，本轮没有安装依赖：

```powershell
conda activate dkf-agent
$env:HF_ENDPOINT = "https://hf-mirror.com"
# 1. 完整离线计划和规模。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --dry-run
Get-Content docs/test/ocb_preset/results/native_download_plan.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object formats,qa_row_count,manifest_row_count,estimated_size
# 2. 打开审核模板与真实审核表。
code docs/test/ocb_preset/results/license_review_candidates.json docs/test/ocb_preset/source/source_review.json
# 3. 完成逐文件人工许可核对后才执行。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --execute --allow-external --workers 3
# 4. 中断/失败后恢复同一范围、来源与预算。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --execute --allow-external --workers 3 --resume
# 5. 离线查缺失/失败、校验本地原件。
python scripts/download_benchmarks.py --dataset ocb --scope native-library --list-missing
python scripts/download_benchmarks.py --dataset ocb --scope native-library --verify-only
$nativeReport = Get-Content docs/test/ocb_preset/results/native_download_report.json -Raw -Encoding UTF8 | ConvertFrom-Json
$nativeReport.summary.by_format | ConvertTo-Json -Depth 8
$nativeReport.records | Where-Object { $_.status -notin @('downloaded','verified_existing') } | Select-Object reference_file,status,reason,http_status,local_file
# 6. 仍由既有准备器抽取少量已审 Office，旧计划不改；目录存在则换新编号。
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42 --results-dir docs/test/ocb_preset/results/native-selection-01
```

许可审核步骤：

1. 核对每个 candidate 的官方身份、原始格式/目标格式/无需转换事实、真实来源与版权。本地公开 URL 或 HF 数据卡许可不替代原件许可。
2. 只把已经审核的 review_template 合并进 source_review.json 的 JSON 数组，保留旧条目，禁止重复 reference_file。填写真实 license；人工确认 original_native=true、conversion_required=false、local_use_confirmed=true；source_url 精确保留选择的官方地址。未审核项不填批准，不批量按下载状态批准。模板本身不是审核记录。
3. 跨域下载跳转需逐域核对，再为对应审核项填 allowed_redirect_hosts 的实际小写域名数组，不能通配；默认空数组只允许原域名。不使用 Cookie/Token/关闭 TLS 绕过登录限制。
4. 本地使用许可不包含 MinerU 外传授权；下载脚本不会写 source_review、QA、抽样计划、正式 manifest 或数据库。

扩展 CSV/XLSM 时先在新目录冻结一版计划；后续所有命令沿用相同参数：

```powershell
python scripts/download_benchmarks.py --dataset ocb --scope native-library --dry-run --native-formats docx xlsx pptx csv xlsm --native-results-dir docs/test/ocb_preset/results/native-all-01
# 人工审核扩展版模板后：
python scripts/download_benchmarks.py --dataset ocb --scope native-library --execute --allow-external --resume --native-formats docx xlsx pptx csv xlsm --native-results-dir docs/test/ocb_preset/results/native-all-01
python scripts/download_benchmarks.py --dataset ocb --scope native-library --verify-only --native-formats docx xlsx pptx csv xlsm --native-results-dir docs/test/ocb_preset/results/native-all-01
```

CSV 有界严格 UTF-8/GB18030 解码与 csv 语法检查，不执行公式或调用 Parser。XLSM 检查 ZIP 路径/规模、工作簿主部件与宏工作簿 Content-Type，不执行宏、不做转换；仅保存未来评测原件，现有 prepare_ocb 仍只选 DOCX/XLSX/PPTX，不偷渡到当前上传/RAG 链路。

##### 完整性、错误及验收边界

HF 校验同 revision 的 size/LFS SHA256 或 Git Blob OID，.part 完整校验后独占硬链接发布，完整原件复核后跳过、坏文件不覆盖。第三方无官方 Hash 时只记本地 SHA256、official_digest_checked=false，回执绑定官方 URL、源版本、实际大小/Hash，不能宣称官方摘要或不可变第三方版本。无来源回执的用户外部原件保留并报 existing_file_unverified。

续传要求同身份、206、精确 Content-Range，以及未变强 ETag 或官方摘要；第三方两者都无则不安全拼接，保留断点、报 resume_rejected。Range 被忽略为 200、ETag 变化、压缩异常或长度不符都拒绝追加，不截断重下。Ctrl+C 停止扩展并发窗口，只等最多三个在途请求按取消/超时结束并刷盘；系统 DNS、读请求和有限退避可能使中断不瞬时。重跑用 --resume，断电遗留锁须人工确认无人运行后处理。

错误分别记录 http_forbidden/403、http_not_found/404、login_required/401 或明确登录页、format_mismatch、file_too_large、unknown_content_length、network_failed、source_redirect_not_reviewed、license_review_pending、conversion_required、native_status_unknown、total_budget_exceeded。每项可追溯文件/来源、QA 行、实际 HTTP、大小/Hash、耗时及历史，不存错误正文、凭据或临时 CDN 签名。单项失败不撤销其他完整原件，也不伪装全量成功。

退出 0 为当前模式处理完成，1 为运行失败，2 为未取得或真实缺口，130 为中断。转换/未审项等缺口继续存在；下载计数不代表全部 QA 依赖齐备，也不代表完整 OCB、解析质量、READY 或问答准确率通过。

安全仅限官方已核对 HTTPS URL/逐域许可及 DNS 拒绝私网，不实现自定义连接级 DNS 锁定；操作系统 DNS 重绑定等风险需可信网络人工确认。容量/ZIP/CSV 检查不是全面恶意文件扫描。实际 Range/Content-Length、失效链接、登录限制、版权、耗时与所有格式容器均待真实用户验证。

用户执行新增 Mock 用例：

```powershell
python -m pytest tests/test_ocb_native_download.py tests/test_download_benchmarks.py tests/test_public_benchmark_tools.py -q
```

用例覆盖全 QA/多依赖、yes/no、官方身份去重/HF 优先、许可隔离、显式执行、HTTP 分类、DNS/跳转、Range/强 ETag、旧原件、容量/取消、离线模式/项目路径、CSV/XLSM 范围及容器 Stub；Codex 仅编写未执行。Mock 不替代真实下载或质量验收，阶段 16/17 待人工验收不变，不进入阶段 18。

Omni metadata 仅取原标注（约 42 MB）和 Dataset API，不下全图。candidate-images 复用 page_record 的真实 GT 分类/尺寸/ignore 检查，按缺少类别优先及稳定身份/seed 选择；相对路径只接受原 image_path（若无 images/ 前缀则加该前缀），必须在真实 Tree 中精确存在。默认按现有 20 MiB/图、最多 12 个候选（对应 256 MiB 检查预算）限制，不整库读取，不声称拥有原 PDF。

下载候选不是最终抽样：真实尺寸不匹配、同 SHA 或类别不足保留原件并报告 candidate_shortfall/missing_gt_categories/suggested_next_paths，不伪造成功或悄悄追下载。人工看报告后可显式增加 --count，例如：

```powershell
python scripts/download_benchmarks.py --dataset omni --scope candidate-images --count 8 --seed 42
# 只有明确接受约 1.5 GB 图像下载、磁盘和版权后才选全量：
python scripts/download_benchmarks.py --dataset omni --scope all-images
```

全量模式是 images/ 中真实 PNG/JPG/JPEG，不自动取原 PDF、渲染/缩放或运行图片转 PDF。最终 prepare 按真实字节大小继续选样，可能与下载候选顺序不同；更多候选也不保证覆盖/独立来源足够。

#### 固定版本、恢复和完整性

终端和 dataset_info.json 显示 HF SHA；首次冻结后同命令沿用本地 revision，不追 main。OHR 另外记录官方 GitHub QA commit/Git Blob OID，两者不是同一个 revision。新版本必须用另一个用户明确管理的项目副本/输入目录，不直接覆盖本项目的冻结原件。现有手动文件在同固定版本的官方大小/摘要及容器验证通过后跳过，失败则停止；来源不明的 .part 不自动接管。

```powershell
# 不写原件的在线计划；显式覆盖当前终端的镜像设置。
python scripts/download_benchmarks.py --dataset ocb --scope metadata --dry-run --endpoint https://huggingface.co
# 指定某个已知真实 SHA 时追加 --revision 实际40位SHA；与已有版本不同将停止。
python scripts/download_benchmarks.py --dataset ocb --scope metadata --list-missing
python scripts/download_benchmarks.py --dataset ohr --scope required --verify-only
python scripts/download_benchmarks.py --dataset ocb --scope metadata --verify-only
python scripts/download_benchmarks.py --dataset ocb --scope reviewed --verify-only
python scripts/download_benchmarks.py --dataset omni --scope metadata --verify-only
python scripts/download_benchmarks.py --dataset omni --scope candidate-images --count 5 --verify-only
# 如果实际选择 all-images，按同一范围验证，而非仅验证 5 张。
python scripts/download_benchmarks.py --dataset omni --scope all-images --verify-only
```

中断/临时网络失败后重跑原命令，不加“强制覆盖”。stream 每块至多 1 MiB，显示已下载/总字节和 MiB/s；未完成文件不发布。401/403/404 立即非零停止，不吞异常继续依赖下载；429/5xx/网络超时额外重试默认 3 次，退避且 Retry-After 秒数上限 60 秒。每文件写入前检查剩余大小+64 MiB 空间；不使用全量 HF 缓存副本，不清除缓存。下载失败可能已有其他完整文件，保留并下次验证跳过。

断点沿用固定仓库/path/revision/大小/官方摘要，强 ETag 可用时核对；必须 206 且 Content-Range 精确匹配未完成区间。收到 200 全文件、变化 ETag、异常压缩、大小不符时拒绝追加并保留 .part，不自动丢弃断点。完成后校验官方 LFS SHA256，非 LFS 对照 Git Blob SHA1；OHR ZIP 只核对容器中央目录安全/基础结构和完整官方字节摘要，不整体解压，单 PDF CRC/页树继续由离线准备器验证。JSON 严格结构，Parquet 真实 PyArrow 解码，Office 主部件及图像结构检查复用既有规则。

验证通过才同文件系统独占硬链接发布目标；不支持硬链接则安全报错，不改用户文件。回执含官方来源、固定版本、官方文件名/本地兼容名、大小、实际 SHA 与摘要对照情况；本地 Hash 不冒充官方签名/版权授权。临时签名仅在内存，HTTP 底层日志禁用；终端只有公开相对身份、计数、速度及固定安全错误，无凭据、本机绝对路径、上游正文。限制入口/已知 HF CDN，未知重定向安全失败，不关闭 TLS 或修改系统网络。

HF API 合同依据 [官方 Hub API](https://huggingface.co/docs/hub/api)、[官方 DatasetInfo/RepoFile/List Repo Tree 实现](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/hf_api.py) 及 [Link 分页实现](https://github.com/huggingface/huggingface_hub/blob/v0.36.0/src/huggingface_hub/utils/_pagination.py)：保留 id/sha/cardData 原始响应和 type/path/size/oid/lfs 对象，用 Link next 完整翻页，镜像返回官方 next 地址时只映射相同 API 路径回选定入口。固定来源 schema/分页可与现有准备工具对接，但**真实镜像的 Range、CDN 重定向、revision 可达性、授权和 QA/标注兼容性尚待用户验证**。GitHub 不经 HF 镜像；不可达时单独检查网络，不伪造 QA 或回落不明镜像。

退出 0 为该下载/校验清单完成，2 为缺失/候选覆盖不足，1 为安全失败，Ctrl+C 为 130；任何成功都不等于来源批准、解析质量通过或 READY。

### Windows 离线预览及提取

用户确认 source 原件来源、本地使用许可和下载完整性后执行；不会再下载输入：

```powershell
python scripts/prepare_ohr_bench_preset.py --confirm-official-source --count 7 --seed 42
python scripts/prepare_ocb_preset.py --confirm-official-source --per-format 2 --seed 42
python scripts/prepare_omnidocbench_preset.py --confirm-official-source --count 5 --seed 42
Get-Content docs/test/ocb_preset/results/selection_report.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object selected_count,gaps,format_coverage
Get-Content docs/test/omnidocbench_preset/results/selection_report.json -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object selected_count,gaps,format_coverage
```

报告存在时使用 --results-dir docs/test/对应数据集/results/run-02，不覆盖旧报告。OHR 退出 2 代表报告有真实缺口；OCB/Omni 预览退出 0 仅表示报告已生成，仍必须核对 gaps/candidate_inventory，不算质量通过。已有官方 NDJSON 可用现有 --schema ndjson --qa-file，但下载器不运行官方默认联网生成器。

prepare 命令不是下载命令，--confirm-official-source 不创建缺少输入。BENCH_MISSING 是缺必需文件，BENCH_EMPTY 是空文件，BENCH_LIMIT 才是实际资源超限，不要扩大预算来掩盖下载未完成；source_review.json 的 [] 不代表任何批准。

目标存在也不代表完整：用下载器 --verify-only 核对当前 scope 的正式原件，不能把 .part 当输入。现有 OHR ZipInfo 测试替身仍须显式包含 header_offset，不修改生产校验迁就不完整 Stub。真实 Tree/QA/标注兼容失败保留明确原因，由用户反馈后修复，不扩大预算或伪造字段。

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
python -m pytest tests/test_download_benchmarks.py -q
python -m pytest tests/test_public_benchmark_tools.py tests/test_prepare_ohr_bench_preset.py -q
python -m pytest tests/test_preset_import.py -q
# 用户已确认隔离数据库/原有依赖与执行范围后，才运行既有全回归：
python -m pytest tests -q
```

下载器 Mock 预期覆盖镜像/版本、原始 API 字节、完整对象/分页、官方文件名映射、临时错误及 Range/ETag 续传、格式拒绝、已有原件保护、许可/依赖、Omni 精确路径及日志脱敏。本轮补充：替换前的版本/摘要/分页/LFS 校验、备份原字节、原子替换失败保护、备份冲突拒绝、只读模式不写、修复后默认入口、重复修复幂等，以及审核/旧计划不变。QA 用例补充三子类原字段/源字节保留、Domain 分类优先拒绝、未知 schema 拒绝。现有工具预期继续覆盖 HF/NDJSON、原始字段、多文件依赖、确定性选择、路径/重复/几何保护、GT/业务页分离、证据与指标边界；但本轮均未执行。用例通过也不替代真实下载/格式兼容/解析质量/云权限验收。阶段 17 代码此前已实现待用户验收，本轮不修改其业务代码、不推进阶段 18。

三个 prepare 及质量对照工具本身无网络请求、模型、Parser、数据库或 Celery 调用，统一下载器只访问白名单公开来源，不自动安装依赖；云费用仍由已有导入任务与 MinerU 预算保护。本轮未下载真实数据、未运行准备工具、未提取样本或测试，实际匹配/五类 QA 覆盖、PDF 结构与权限仍待用户确认。

仅用户执行对应单元用例（字段/中央目录/状态 Stub，不生成真实 PDF）：

```powershell
python -m pytest tests/test_prepare_ohr_bench_preset.py tests/test_preset_import.py -q
```
