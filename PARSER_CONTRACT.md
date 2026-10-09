# 阶段 8：统一文档解析契约

## 当前范围

本阶段仅建立 `ParsedDocument`、`ParsedBlock` 与 `BaseParser`。模型使用已有 Pydantic，不依赖 SQLAlchemy、Celery、Redis、Embedding 或 RAG；不增加环境变量、数据库迁移、外部 API 或模型下载。

目前尚无具体格式 Parser。上传与阶段 7 Worker 行为不变：检查原件后停留在 `PARSING`，任务返回 `awaiting_parser`，不会生成 Block，也不会进入 `READY`。本阶段不是完整文档解析能力。

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
- `blocks` 必须显式提供，按原文阅读顺序排列；允许空列表，例如等待 OCR 的扫描页。空结果不代表解析成功或知识库就绪，下游需判断是否有可用内容。
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
- 坐标是原始页面坐标，不是归一化比例：PDF 使用页面点数，图片使用像素；具体 Parser 负责对齐页面旋转和坐标原点，并检查范围。没有可靠位置时返回 `null`，不要伪造坐标。

### 标识、序列化与后续入库

同一 `ParsedDocument` 内 `block_id` 必须唯一，由具体 Parser 显式提供。建议后续使用文档 ID 和稳定的块位置生成确定性标识；契约本身不随机生成 ID，也不替代后续入库的事务、唯一约束或幂等逻辑。

使用 `ParsedDocument.model_validate(...)` 校验字典，使用 `model_dump(mode="json")` 或 `model_dump_json()` 输出 JSON，使用 `model_validate_json(...)` 读取 JSON。验证错误为 Pydantic `ValidationError`；模型实例在 Parser 入口也会重新校验，防止构造后修改绕过检查。

顶层 `doc_id` 是所有块的所属文档；块不重复保存 `doc_id`。后续存储层负责把顶层 ID 映射到 `DocumentBlock.doc_id`，并把 `bbox` 保存为 JSON 数组；本阶段不实现该存储层，`title` 也不新增数据库列。

## Parser 接入规则

后续具体格式 Parser 继承 `BaseParser`，只实现 `_parse(...) -> ParsedDocument`；调用方统一调用 `parse(doc_id=..., file_path=..., file_name=..., file_type=...)`，不绕过公共入口。

公共入口校验输出结构，并确认返回的文档 ID、原始文件名和类型与输入一致。原件读取、格式检测、解析错误由具体 Parser 实现，异常交给上层任务处理；基类不选择格式、不读取文件、不写数据库、不调用 RAG，也不接管 Celery 重试和状态机。

## 人工验收

在项目根目录激活环境，运行独立契约测试，无需启动 PostgreSQL、Redis、FastAPI 或 Worker：

```powershell
conda activate dkf-agent
python -m pytest tests/test_parsed_document.py -q
```

检查测试全部通过，并人工确认：

1. 示例中的字段、五种块类型、未知置信度和坐标约定符合后续 Parser 需要。
2. JSON 往返保持 UUID、块顺序、中文正文与坐标；重复块 ID、非法页码、越界置信度及额外字段被拒绝。
3. TXT / Markdown / 未分页 DOCX 的“逻辑页 1”不被误认为真实 PDF 或 Word 页码。
4. 现有 `/health` 和 `/docs` 部署检查仍可执行；阶段 7 的 `PARSING / awaiting_parser` 是当前预期，不应期待本阶段上传后得到解析正文。

下一阶段先接入 TXT / Markdown Parser，再扩展 DOCX 与原生 PDF；每个 Parser 都应通过相同数据契约和实际样本文档测试。
