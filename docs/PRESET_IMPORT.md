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
