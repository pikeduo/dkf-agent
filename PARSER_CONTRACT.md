# 统一文档解析契约与原生 Parser

## 当前范围

`ParsedDocument`、`ParsedBlock` 与 `BaseParser` 使用已有 Pydantic，契约及具体 Parser 均不依赖 SQLAlchemy、Celery、Redis、Embedding 或 RAG。任务服务负责状态机和数据库持久化，Parser 只读取原件并返回统一结构。

已实现 TXT、Markdown、DOCX 和文本型 PDF 原生解析。Worker 校验原件、解析并保存 `document_blocks` 后停在 `CHUNKING`，返回 `awaiting_chunk` 和 `block_count`。没有 OCR、Chunk、Embedding、索引或 `READY` 能力，不增加环境变量、外部 API 或模型下载。

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
- `file_name` 保留原始文件名，非空且不超过 255 字符；`file_type` 使用小写 `pdf / docx / txt / md / jpg / jpeg / png`。这里仅约束格式标识，不表示对应解析器已经实现。
- `title`、`section` 未识别时使用空字符串，不由模型补写标题或章节。
- `blocks` 必须显式提供，按原文阅读顺序排列。契约允许空列表，但当前原生 Parser 对空文本或需要 OCR 的页面明确抛出错误，任务不能把空结果当作成功。
- `block_type` 仅允许 `title / text / table / formula / image_text`。表格和公式本阶段仍以 `text` 字段承载原始文本，不计算公式，也不做切片。
- `text` 保留换行和空白，不在数据契约中清洗原文。允许空字符串，但不因此认定块可用于检索。
- `source` 必须显式给出，非空且不超过 64 字符。原生解析约定为 `native_parser`，OCR 可使用 `volcengine_ocr`、`baidu_ocr` 等实际 Provider 标识；本阶段不实现这些 Provider。
- `confidence` 为有限数值，范围为 `[0, 1]`；默认 `null` 表示未知。原生解析器可按业务约定显式给出 `1.0`，该值不是回答的事实准确率；OCR 无置信度时不得伪造高分。
- 未知字段禁止进入顶层或块结构，防止各 Parser 私自加入 Chunk、Embedding、Answer 等不统一的字段。

### 页码与坐标

为兼容现有 `document_blocks.page` 非空约束，`page` 必须显式提供正整数，从 1 开始，拒绝 0、负数、布尔值和数字字符串。

- PDF 使用真实物理页码，不能用逻辑页替代已知物理页。
- 单张图片使用第 1 页。
- TXT / Markdown 无固定分页，使用逻辑页 1。DOCX 未经分页渲染时也仅能使用逻辑页 1，不能声称它是 Word 排版中的真实页码。后续证据展示需按文件格式区分“逻辑页”和“物理页”；如需表达未知页码，再单独设计并迁移数据库，本阶段不更改约束。
- `bbox` 为 `null` 或四个有限数值组成的数组 `[x0, y0, x1, y1]`，左上角到右下角，要求 `x1 >= x0`、`y1 >= y0`。
- 坐标是原始页面坐标，不是归一化比例：PDF 使用未旋转页面的点数坐标，后续叠加到旋转页面时需进行旋转变换；图片使用像素。TXT、Markdown 和 DOCX 无可靠位置时返回 `null`，不要伪造坐标。

### 标识、序列化与后续入库

同一 `ParsedDocument` 内 `block_id` 必须唯一，由具体 Parser 显式提供。当前原生 Parser 共用 `make_block`，使用文档 UUID 为命名空间，以 `native-v1:{page}:{index}:{block_type}` 生成 UUID5；`index` 为全文中从 1 开始的块序号。同一原件、文档 ID 和 Parser 版本的重试得到相同标识，不承诺升级 Parser 或修改原件后标识不变。

使用 `ParsedDocument.model_validate(...)` 校验字典，使用 `model_dump(mode="json")` 或 `model_dump_json()` 输出 JSON，使用 `model_validate_json(...)` 读取 JSON。验证错误为 Pydantic `ValidationError`；模型实例在 Parser 入口也会重新校验，防止构造后修改绕过检查。

顶层 `doc_id` 是所有块的所属文档；块不重复保存 `doc_id`。任务服务将其映射到 `DocumentBlock.doc_id`，把块列表顺序写入 `block_index`，并保存页码、章节、类型、正文、JSON 坐标、来源及置信度。`title` 不新增数据库列，识别出的标题通过 `title` 类型的 Block 保存。

`0003_block_order` 增加可空整数列 `block_index`、正数检查及文档内序号唯一约束。所有新解析块使用连续序号；历史块序号保持空值，不推测历史顺序。降级保留正文但丢失序号；升级不能恢复丢失的顺序。新结果查询必须显式按 `block_index` 排序，不能依赖插入顺序或 UUID。

## Parser 接入规则

具体格式 Parser 继承 `BaseParser`，只实现 `_parse(...) -> ParsedDocument`；调用方通过 `get_parser(file_type)` 选择 Parser，统一调用 `parse(doc_id=..., file_path=..., file_name=..., file_type=...)`，不绕过公共入口。

公共入口校验输出结构，并确认返回的文档 ID、原始文件名和类型与输入一致。原件读取、格式检测、解析错误由具体 Parser 实现，异常交给上层任务处理；基类不选择格式、不读取文件、不写数据库、不调用 RAG，也不接管 Celery 重试和状态机。

## 四类原生解析的范围

| 格式 | 当前行为 | 页码与限制 |
|---|---|---|
| TXT | 严格解码，按空行分段，保留段内换行与缩进 | 逻辑页 1，无标题猜测 |
| Markdown | 识别 ATX / Setext 常用标题及章节层级，保留代码围栏及其余正文语法 | 逻辑页 1，标题块去除标题标记；不是完整 CommonMark AST |
| DOCX | python-docx 按正文顺序提取段落、标题及表格，章节关联正文 | 逻辑页 1，无 Word 分页渲染；表格列用制表符、行用换行表示 |
| PDF | PyMuPDF 按页面及文本块读取原生文本，保留坐标 | 真实物理页码，从 1 开始；不猜测标题、表格或公式类型 |

TXT / Markdown 优先识别 UTF-32、UTF-16、UTF-8 的 BOM；无 BOM 时严格尝试 UTF-8，再尝试 GB18030，不使用替换字符掩盖解码失败。空白文件、非法编码和不支持的控制字符返回安全中文错误；不能可靠判断无 BOM 的所有编码，其他编码应由上传者先转换。

DOCX 支持正文标题样式、标题层级和表格，保留段落及单元格原文空白。当前不包含页眉、页脚、脚注、文本框、修订包装内的内容、图片文字或公式结构；合并单元格并非完整布局重建，纵向合并可能跨行重复文本。

PDF 跳过真正空白页但保留其后文本页的原始页码。无原生文本且有图片、绘图或批注的页面视为需要 OCR，纯扫描或含此类页面的混合 PDF 整体失败，不保存半份结果。有原生文本的页面仅提取该文本，不识别同页图片中的文字。加密文件、PyMuPDF 标记为修复过的损坏文件，以及整份文件无可用文本的情况明确失败。

四类原生块使用 `source=native_parser`、`confidence=1.0`；该值仅表示原生提取来源，不是版面还原质量或回答准确率。图片仍可上传，但当前 Parser 选择器会报告需要 OCR，不伪造 `image_text`。

## 任务持久化与失败恢复

1. 在文档行锁内校验当前 `processing_task_id` 和状态，仅处理 `UPLOADED / PARSING`。
2. 完成整份解析及契约校验后，在同一事务中整体替换该文档的 Block，并推进到 `CHUNKING`；不修改其他文档。
3. 解析、写入或提交异常退出事务后回滚本次变更，再用独立事务记录失败。旧块不会因半次写入而消失。
4. 永久格式、空文本或 OCR 需求立即标记 `FAILED`；临时文件访问 / 数据库错误按原任务重试规则处理。数据库持续不可用时可能无法记录失败原因，但保留安全日志和任务错误。
5. 过期代次返回 `stale_task`，已推进的文档返回 `skipped`，不重复解析或生成 Block。重复消息可能更新 Celery 结果为 `skipped`，以 Document 持久化状态为准。

解析过程持有事务与行锁，`PARSING` 不保证实时对外可见。更新代码不会自动补投历史文档；现有 `/process` 仍仅接受 `UPLOADED / FAILED`，不支持对已有 `PARSING / CHUNKING` 文档重新解析或 Reindex。

## 人工验收

在项目根目录同步环境、激活环境并运行契约与四类 Parser 单元测试，无需启动 PostgreSQL、Redis、FastAPI 或 Worker：

```powershell
conda activate dkf-agent
python -m pytest tests/test_parsed_document.py tests/test_text_parser.py tests/test_docx_parser.py tests/test_pdf_parser.py -q
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

1. 按 README 同步依赖，在正式业务库执行 `python -m alembic upgrade head`，确认 head 为 `0003_block_order`；重启 API 和 CPU Worker。
2. 先确认 Windows Redis 服务为 `Running`，再在 Swagger 向测试知识库分别上传中文 TXT、含标题和代码围栏的 MD、含标题/段落/表格的 DOCX、多页文本 PDF；不同文件内容需不同以避免去重。
3. 用文档详情和 `/tasks/{task_id}` 查询，预期文档为 `CHUNKING`、无失败原因，首次成功任务结果为 `awaiting_chunk` 且 `block_count > 0`。
4. 在 pgAdmin 中用实际 `doc_id` 检查有序正文、标题章节及页码：

```sql
SELECT block_index, page, section, block_type, text, bbox, source, confidence
FROM document_blocks
WHERE doc_id = '实际文档 UUID'::uuid
ORDER BY block_index;
```

5. 上传扫描 PDF 或图片，预期文档为 `FAILED`、原因明确需要 OCR、原件保留且不生成部分 Block；再检查空文本及损坏文件的安全错误。文件名、正文、物理 / 逻辑页码应与原件人工对照。

以上自动测试不等于真实 Redis、Worker 或用户样本文档已人工验证。下一阶段仅在本阶段人工验收并确认后接入 OCR Provider。
