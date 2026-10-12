# 统一文档解析契约与原生 Parser

## 当前范围

`ParsedDocument`、`ParsedBlock` 与 `BaseParser` 使用已有 Pydantic，契约及具体 Parser 均不依赖 SQLAlchemy、Celery、Redis、Embedding 或 RAG。任务服务负责状态机和数据库持久化，Parser 只读取原件并返回统一结构。

已实现 TXT、Markdown、CSV、DOCX、XLSX 和文本型 PDF 原生解析。Worker 保存 `document_blocks` 后停在 `CHUNKING`，返回 `awaiting_chunk` 和 `block_count`。Parser 本身不调用 OCR/外部 API、不生成 Chunk/Embedding/READY；独立 [Chunk 任务](CHUNK.md) 读取已完成的 Block，切片成功后等待向量化。独立云入口见 [MinerU Cloud 验收说明](MINERU_CLOUD.md)；可选模式的路由在 Parser 外完成，见 [Router 验收说明](PARSER_ROUTER.md)，省略模式默认为 auto，管理员显式 Native 可做原生范围对照。

自动路由内部的 [XLSX Hybrid](SELECTIVE_HYBRID.md) 不改变 XlsxParser 或统一 Block 字段：保留完整 Sheet table，结构化 Chart 为 native_parser，云视觉增强映射回原 Sheet、逻辑 page=1、bbox=NULL。临时 Bundle 的 PDF 页码只在私有 manifest 使用，不作为原件页码。新增代码与测试待用户执行验收。

## 统一结构

```json
{
  "doc_id": "7f2044ca-2041-42ce-977d-40bf5c79ed40",
  "file_name": "示例.pdf",
  "file_type": "pdf",
  "title": "示例文档",
  "blocks": [
    {
      "block_id": "710c7509-4d2c-4c2a-9681-532013732546",
      "page": 1,
      "section": "第一节",
      "block_type": "text",
      "text": "这是原文内容。",
      "bbox": null,
      "source": "native_parser",
      "confidence": 1.0
    }
  ]
}
```

- `doc_id`、`block_id` 使用 UUID，JSON 中输出 UUID 字符串。`doc_id` 来自已有 Document，不在 Parser 中重新创建。
- `file_name` 保留原始文件名，非空且不超过 255 字符；`file_type` 为 `pdf / doc / docx / ppt / pptx / xls / xlsx / csv / txt / md / jpg / jpeg / png`，上传和 DocumentResponse 共用此定义。格式标识不表示 Native Parser 已实现；数据库现有 String(32) 无格式枚举约束，无需新迁移。
- `title`、`section` 未识别时使用空字符串，不由模型补写标题或章节。
- `blocks` 必须显式提供，按原文阅读顺序排列。契约允许空列表，但当前原生 Parser 对空文本或需要 OCR 的页面明确抛出错误，任务不能把空结果当作成功。
- `block_type` 仅允许 `title / text / table / formula / image_text`。表格和公式本阶段仍以 `text` 字段承载原始文本，不计算公式，也不做切片。
- `text` 保留换行和空白，不在数据契约中清洗原文。允许空字符串，但不因此认定块可用于检索。
- `source` 必须显式给出，非空且不超过 64 字符。原生解析约定为 `native_parser`，MinerU 云端解析使用 `mineru_cloud`；当前不接入火山 / 百度 OCR。
- `confidence` 为有限数值，范围为 `[0, 1]`；默认 `null` 表示未知。原生解析器可按业务约定显式给出 `1.0`，该值不是回答的事实准确率；OCR 无置信度时不得伪造高分。
- 未知字段禁止进入顶层或块结构，防止各 Parser 私自加入 Chunk、Embedding、Answer 等不统一的字段。

### 页码与坐标

为兼容现有 `document_blocks.page` 非空约束，`page` 必须显式提供正整数，从 1 开始，拒绝 0、负数、布尔值和数字字符串。

- PDF 使用真实物理页码，不能用逻辑页替代已知物理页。
- 单张图片使用第 1 页。
- Native TXT/MD/CSV/DOCX/XLSX 使用逻辑页 1，Sheet 名放 section，不能以工作表序号冒充打印页码。六种 Office 云结果保留 MinerU 转换后的 `page_idx + 1`，是云端逻辑页，不是原始 Office 排版页。后续 Evidence 必须区分页码语义。
- `bbox` 为 `null` 或四个有限数值组成的数组 `[x0, y0, x1, y1]`，左上角到右下角，要求 `x1 >= x0`、`y1 >= y0`。
- Native PDF 使用未旋转页面点数坐标；MinerU PDF 使用对应实际渲染视口的点数坐标，图片使用像素。Native TXT/MD/CSV/DOCX/XLSX 及全部云端 Office 无可靠原件坐标映射，bbox 为 `null`。Office 不套用 PDF geometry，即便云端返回归一化 bbox 也不伪造原件尺寸。

### 标识、序列化与后续入库

同一 `ParsedDocument` 内 `block_id` 必须唯一，由具体 Parser 显式提供。当前原生 Parser 共用 `make_block`，使用文档 UUID 为命名空间，以 `native-v1:{page}:{index}:{block_type}` 生成 UUID5；`index` 为全文中从 1 开始的块序号。同一原件、文档 ID 和 Parser 版本的重试得到相同标识，不承诺升级 Parser 或修改原件后标识不变。

使用 `ParsedDocument.model_validate(...)` 校验字典，使用 `model_dump(mode="json")` 或 `model_dump_json()` 输出 JSON，使用 `model_validate_json(...)` 读取 JSON。验证错误为 Pydantic `ValidationError`；模型实例在 Parser 入口也会重新校验，防止构造后修改绕过检查。

顶层 `doc_id` 是所有块的所属文档；块不重复保存 `doc_id`。任务服务将其映射到 `DocumentBlock.doc_id`，把块列表顺序写入 `block_index`，并保存页码、章节、类型、正文、JSON 坐标、来源及置信度。`title` 不新增数据库列，识别出的标题通过 `title` 类型的 Block 保存。

`0003_block_order` 增加可空整数列 `block_index`、正数检查及文档内序号唯一约束。所有新解析块使用连续序号；历史块序号保持空值，不推测历史顺序。降级保留正文但丢失序号；升级不能恢复丢失的顺序。新结果查询必须显式按 `block_index` 排序，不能依赖插入顺序或 UUID。

## Parser 接入规则

具体格式 Parser 继承 `BaseParser`，只实现 `_parse(...) -> ParsedDocument`；调用方通过 `get_parser(file_type)` 选择 Parser，统一调用 `parse(doc_id=..., file_path=..., file_name=..., file_type=...)`，不绕过公共入口。

公共入口校验输出结构，并确认返回的文档 ID、原始文件名和类型与输入一致。原件读取、格式检测、解析错误由具体 Parser 实现，异常交给上层任务处理；基类不选择格式、不读取文件、不写数据库、不调用 RAG，也不接管 Celery 重试和状态机。

## 六类原生解析的范围

| 格式 | 当前行为 | 页码与限制 |
|---|---|---|
| TXT | 严格解码，按空行分段，保留段内换行与缩进 | 逻辑页 1，无标题猜测 |
| Markdown | 识别 ATX / Setext 常用标题及章节层级，保留代码围栏及其余正文语法 | 逻辑页 1，标题块去除标题标记；不是完整 CommonMark AST |
| CSV | 标准 csv 模块严格读取逗号分隔、引号、空字段，整份一个 table | 逻辑页 1，bbox=null；字段序列化为带引号规则的 TSV |
| DOCX | python-docx 按正文顺序提取段落、标题及表格，章节关联正文 | 逻辑页 1，无 Word 分页渲染；表格列用制表符、行用换行表示 |
| XLSX | openpyxl 只读读取非空 Sheet，每个一个 table，section=Sheet 名 | 逻辑页 1，bbox=null；按行列原序输出 TSV，保留原式不执行 |
| PDF | PyMuPDF 按页面及文本块读取原生文本，保留坐标 | 真实物理页码，从 1 开始；不猜测标题、表格或公式类型 |

TXT / Markdown 优先识别 UTF-32、UTF-16、UTF-8 的 BOM；无 BOM 时严格尝试 UTF-8，再尝试 GB18030，不使用替换字符掩盖解码失败。空白文件、非法编码和不支持的控制字符返回安全中文错误；不能可靠判断无 BOM 的所有编码，其他编码应由上传者先转换。

DOCX 支持正文标题样式、标题层级和表格，保留段落及单元格原文空白。当前不包含页眉、页脚、脚注、文本框、修订包装内的内容、图片文字或公式结构；合并单元格并非完整布局重建，纵向合并可能跨行重复文本。

PDF 跳过真正空白页但保留其后文本页的原始页码。无原生文本且有图片、绘图或批注的页面视为需要 OCR，纯扫描或含此类页面的混合 PDF 整体失败，不保存半份结果。有原生文本的页面仅提取该文本，不识别同页图片中的文字。加密文件、PyMuPDF 标记为修复过的损坏文件，以及整份文件无可用文本的情况明确失败。

六类原生块使用 `source=native_parser`、`confidence=1.0`，不表示版面还原质量或回答准确率。DOC/XLS/PPT/PPTX 和图片在 Native Registry 明确要求显式 MinerU，不注册空解析器、不自动转云端。

### CSV/XLSX 细节与安全边界

- CSV 复用严格文本解码（包括 UTF-8、BOM、GB18030），保留中文、空字段、quoted comma/换行、数字与百分比字符串；不猜测分隔符，不执行以 `=` 开头的内容。空/控制字符/解码失败、列数不一致、引号结构错误或超过标准 csv 单字段上限时安全失败。不能可靠识别所有无 BOM 编码的乱码，上传者应核对原文。
- 编码失败负向 fixture 使用无 BOM 的 `b"\x81\x30\x81"`，先验证 UTF-8、GB18030 严格解码均失败，再验证 `read_text / CsvParser` 的安全 ParserError 与文档 FAILED、零 Block。带合法 UTF-16 BOM 的字节即使语义乱码也不能当作解码失败；第一版不做复杂语义乱码检测或针对具体字符串的拒绝。
- XLSX `read_only=True / data_only=False / keep_links=False`，不执行宏、公式或外部链接；公式文本保留，未知特殊公式明确失败。空 Sheet 跳过、全空失败；Sheet ID 位置基于原工作表顺序（含跳过的空表），持久化 block_index 仍连续。
- 保留内部空行空列，剔除末尾无值单元格范围；普通 `0% / 0.00%` 等格式输出百分比，复杂自定义格式不模拟 Excel 渲染，日期使用 ISO 格式。单元格数据不是视觉布局还原。
- Native Parser 自身不解析 XLSX 图片/图表/复杂绘图，DOCX 原有限制不变；外层 auto 检测后选择云端或 XLSX Hybrid。XLSX Whole 也保留 Native 整表与公式原式，全部结果一次提交。对照使用独立知识库，完成文档不能借云入口 Reparse。
- CSV/XLSX 最多 10 万行、1024 列、100 万单元格（XLSX 累计），XLSX 最多 1000 Sheet；OOXML 最多 10000 ZIP 项、256 MiB 总展开大小、单主部件 64 MiB。拒绝加密、重复项、不安全路径及类型混淆。这是基础保护，不是完整恶意文件扫描。
- 统一校验 DOCX/PPTX/XLSX 的 `[Content_Types].xml` 和对应 `word/document.xml / ppt/presentation.xml / xl/workbook.xml`；olefile 严格打开 CFB 并识别根级 WordDocument / Workbook或Book / PowerPoint Document 主流。容器检查不等于旧 Office 正文已成功解析，最终可读性由 MinerU 判断。

## 任务持久化与失败恢复

1. 在文档行锁内校验当前 `processing_task_id` 和状态，仅处理 `UPLOADED / PARSING`。
2. 完成整份解析及契约校验后，在同一事务中整体替换该文档的 Block，并推进到 `CHUNKING`；不修改其他文档。
3. 解析、写入或提交异常退出事务后回滚本次变更，再用独立事务记录失败。旧块不会因半次写入而消失。
4. 永久格式、空文本或 OCR 需求立即标记 `FAILED`；临时文件访问 / 数据库错误按原任务重试规则处理。数据库持续不可用时可能无法记录失败原因，但保留安全日志和任务错误。
5. 过期代次返回 `stale_task`，已推进的文档返回 `skipped`，不重复解析或生成 Block。重复消息可能更新 Celery 结果为 `skipped`，以 Document 持久化状态为准。

解析过程持有事务与行锁，`PARSING` 不保证实时对外可见。`/process` 仍仅接受 `UPLOADED / FAILED`；显式维护入口见下节，活动任务不可换代次，历史结果不自动重写。

## 阶段 12：Parse Cache 契约与验收

只保存通过统一契约/完整性 Guard/Merge 的全量结果，不开发 Chunk、Embedding 或真正 Index。Native 对照范围单独寻址，不能冒充 auto 完整覆盖。Whole XLSX 缓存包含 Native 主表/原式与云块。

整份键包含 `file_hash / file_type / provider / model_version / language / is_ocr / enable_table / enable_formula / route_strategy / coverage_scope`，以及本地归一化/主解析版本和路由配置摘要。Hybrid 额外含 `visual_extractor_version / bundle_manifest_hash / visual_content_hash`：manifest 摘要使用原 Sheet/Anchor/候选内容/策略，不含随机 UUID、结果状态或临时 Bundle 页号。视觉键仅用原图片字节 Hash、模型选项和 Extractor/Normalizer 版本，不依赖 doc_id/Anchor/PDF Hash。

`PARSED_DIR` 配置缓存根目录，`PARSE_CACHE_MAX_ENTRY_MB` 限制每代次总大小；无新依赖/迁移，默认目录已被忽略。运行时结构：

```text
data/parsed/
├── .cache/<key>/<generation>/
│   ├── normalized_document.json
│   ├── raw/mineru_result.zip          # 云结果
│   ├── content_list.json
│   ├── full.md                        # 上游存在且有界才保存
│   └── hybrid/
│       ├── bundle_manifest.json
│       ├── visual_bundle.pdf
│       └── normalized_visual_blocks.json
├── .cache/<key>/ready.json
├── .jobs/<job_id>.json                 # 同代次 SQL 回滚恢复指针
└── <doc_id>/
    ├── latest.json                    # 本文档冻结成功代次
    └── <key>/<generation>/...          # 文档归档与重绑后的标准化结果
```

每个文件有 Hash，先写不可变完整代次，再原子发布 READY/指针；不 extractall、不存签名链接。Markdown 不成为正文替代。ZIP/manifest 是首次云结果的来源归档，跨文档命中时不冒充新的云返回；标准化文档重绑新 doc_id/文件名/稳定 Block ID。Reindex 使用本文档冻结代次，不被其他文档或 Force 发布的新 READY 改换 ID。

新上传/首次无收费历史的 miss 仍正常解析；损坏缓存明确失败，不当成 miss。已有收费历史而缓存缺失/不确定，普通重投为 CACHE_REPARSE_REQUIRED，仅显式 Force Reparse 允许新调用。相同内容/缺失视觉对象由 PostgreSQL advisory 事务锁 + Job 私有摘要保护，活动 owner 为 CACHE_IN_PROGRESS，不并行重复付费；无自动续租/补投。

完整结果已缓存但 SQL 临时失败，同 Job 重试重放不可变结果，不再 GET/下载/POST；文件与 SQL 非原子，cache.stored 不表示落库成功。失败不缓存成成功；已有成功缓存不因 Force 失败被删除。缓存包含原文，Hash 不是防御有目录写权限者篡改的签名，需要限制权限并备份整个目录（含隐藏元数据）。无自动 TTL/清理、多主机共享盘保证。

Hybrid 先查精确视觉缓存，只有未命中图片进入一份 Bundle。缓存视觉丢弃旧页/bbox/Sheet，按当前 Anchor 回插；冻结命中集合和结果 Hash，新缓存不得改变本代次页映射。零缺失页不建云 Job。Native/Chart 来自当前原件，保留公式原式，缺页/失败/空 OCR 不缓存，不做相似文本复用。

Reindex 无成功缓存、原件/配置/模型/参数/版本变更或缓存损坏应 FAILED，旧块保留，不运行 Parser/云；目前只停在 CHUNKING。升级前完成记录不自动回填，旧活动 Job 按原检查点完成，不猜新缓存身份。云端 `vlm/pipeline` 同名模型升级不可自动检测，需要管理员 Reparse。

### 用户执行的测试与真实验收

Codex 仅编写、未运行。新用例使用元数据/Source/ZIP Stub，不生成 Office/PDF/图片验收资料。DB 用例复用已有隔离 schema，Broker/Provider Mock：

```powershell
conda activate dkf-agent
python -m pytest tests/test_parse_cache.py tests/test_hybrid.py tests/test_swagger_docs.py -q
python -m pytest tests/parse_cache_smoke.py tests/hybrid_smoke.py tests/mineru_db_smoke.py tests/parser_router_smoke.py tests/native_parser_db_smoke.py tests/document_processing_smoke.py -q
```

历史回归由用户按范围选择执行，不授权 Codex 测试。真实步骤：

1. 备份数据库/原件/整个 PARSED_DIR，核对权限、Token/直连/Redis/预算，重启全部 API/Worker；无新迁移，Windows solo/concurrency=1。只使用你已有且允许外传的资料。
2. 新库首次上传已有扫描 PDF/图片，等 CHUNKING/job=done，检查 ZIP/content_list/标准化结果与 cache.stored。记录平台文件任务数。
3. POST `/reindex`（无请求体），预期 CHUNKING、正文/ID/顺序不变，无新 batch/HTTP。向另一个库上传同一原件，预期缓存 hit、0 新云文件，doc_id/文件名/Block ID 分离。
4. 使用已有两份 XLSX：图片字节相同但单元格/Anchor 不同。第一份完成后第二份相同图片不入新 Bundle；全部命中0云任务，部分命中仅缺失图片组成1份 Bundle。核对原式/Sheet/page=1/NULL bbox/增强顺序。
5. `/reparse` 不带 force 应400；确认预算后仅执行一次 `?force=true`，预期新增最多1份 Whole/Bundle（Native/零视觉仍0）。活动 `/mineru` 仍同 Job/batch；活动维护应409。
6. 故障/并发/损坏优先运行 Mock，不破坏共用缓存/服务。独立验收目录中的 miss/配置变更 Reindex 应 FAILED，旧块保留、云次数不增；旧完成记录无缓存也不能悄悄收费。

只读 SQL（替换 UUID）：

```sql
SELECT doc_id, file_name, status, error_message FROM documents WHERE doc_id='文档UUID'::uuid;
SELECT job_id, state, is_active, batch_id, upload_complete, error_code, error_message,
       parse_options->>'_cache_key' AS cache_key,
       parse_options->>'_force_reparse' AS force_reparse,
       parse_options->'_hybrid_result' AS visual_ledger
FROM document_parse_jobs WHERE doc_id='文档UUID'::uuid ORDER BY created_at;
SELECT count(DISTINCT batch_id) FILTER (WHERE batch_id IS NOT NULL) AS confirmed_cloud_files
FROM document_parse_jobs WHERE doc_id='文档UUID'::uuid;
SELECT block_id, block_index, page, section, block_type, source, bbox, text
FROM document_blocks WHERE doc_id='文档UUID'::uuid ORDER BY block_index;
```

本地 batch 数不是收费证明，不确定申请以平台核对为准。新增缓存链路、真实费用收益、Windows 多进程持久化及全部回归待用户验收，Codex 不自行调用真实 API，不进入阶段13。

## 人工验收

在项目根目录同步环境、激活环境并运行契约与 Parser 单元测试，无需启动基础服务或 Worker：

```powershell
conda activate dkf-agent
python -m pytest tests/test_parsed_document.py tests/test_text_parser.py tests/test_docx_parser.py tests/test_pdf_parser.py tests/test_tabular_parsers.py tests/test_office_formats.py -q
```

检查测试全部通过，并人工确认：

1. 示例中的字段、五种块类型、未知置信度和坐标约定符合后续 Parser 需要。
2. JSON 往返保持 UUID、块顺序、中文正文与坐标；重复块 ID、非法页码、越界置信度及额外字段被拒绝。
3. TXT / Markdown / 未分页 DOCX 的“逻辑页 1”不被误认为真实 PDF 或 Word 页码。
4. Markdown 代码围栏内标题符号不误判，DOCX 标题和正文顺序正确，PDF 空白页之后页码不被压缩；扫描 / 混合 PDF、损坏或空文本文件明确失败。

数据库集成测试需要已部署的 PostgreSQL 和 vector 扩展，使用隔离 schema 并回滚，不迁移正式业务库；任务使用 eager / mock，不连接真实 Redis：

```powershell
python -m pytest tests/native_parser_db_smoke.py tests/document_processing_smoke.py tests/knowledge_db_smoke.py -q
```

真实上传验收：

1. 按 [README](../README.md) 同步依赖，在正式业务库执行 `python -m alembic upgrade head`，当前 head 为 `0005_chunk_checkpoint`；原生块顺序来自 `0003_block_order`。重启 API 和 CPU Worker。
2. 先确认 Windows Redis 服务为 `Running`，再在 Swagger 向测试知识库通过普通 `/documents` 上传 TXT、MD、DOCX、文本 PDF，以及三种编码（UTF-8/BOM/GB18030）的 CSV、多 Sheet XLSX；不同内容避免去重。CSV 包含中文、引号逗号、空字段、数字和百分比，XLSX 包含空 Sheet、中文、数字、百分比及未执行公式。
3. 用文档详情和 `/tasks/{task_id}` 查询，预期文档为 `CHUNKING`、无失败原因，首次成功任务结果为 `awaiting_chunk` 且 `block_count > 0`。
4. 在 pgAdmin 中用实际 `doc_id` 检查有序正文、标题章节及页码：

```sql
SELECT block_index, page, section, block_type, text, bbox, source, confidence
FROM document_blocks
WHERE doc_id = '实际文档 UUID'::uuid
ORDER BY block_index;
```

5. 核对 CSV table 的空字段/引号/数值；XLSX table 的 Sheet 原序、section、原式、百分比及空 Sheet 跳过；两者 page=1、bbox=NULL、source=native_parser。零字节文件在上传时返回 400，其他全空或畸形正文解析为 FAILED，不落部分块。管理员显式 native 上传旧 Office/PPTX/图片应明确要求 MinerU，原件保留；默认 auto 的自动路由验收见 Router 文档。

阶段 1～10 已按用户确认完成人工验收；新增 CSV/XLSX 的自动测试不代替真实 Worker 与用户原件测试。新增 Office 云端验收见 [MinerU Cloud 验收说明](MINERU_CLOUD.md)，不继续接入火山 / 百度 OCR。
