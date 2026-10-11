# 阶段 11B.2：XLSX Selective Hybrid

本次只实现代码和测试用例，**不生成测试文档，不执行 pytest/Smoke/解析/渲染或真实云请求**。现有验收资料不删除。阶段是否通过由用户执行测试和真实 API 验收后确认；功能默认关闭，不声称收益已经证明。

## 范围与策略

- 仅 `auto + XLSX + HYBRID_XLSX_ENABLED=true`；省略模式、显式 Native/Whole、DOCX/PDF/PPT 不改变行为。其余复杂结构仍 review。
- 先读现有 XlsxParser，保留原始 Sheet 顺序、单元格和公式。Native 全空 Workbook 仍失败。
- OOXML 包关系定位 Sheet → drawing → image/chart；只接受有界包内关系和 one/twoCellAnchor。外链、OLE、VML、未知绘图、裁剪/旋转/效果、孤立部件、复杂 Chart 不静默忽略，进入 review。
- PNG/JPEG 解码前限制尺寸/字节；微小或所有像素完全相同的纯色图过滤。黑白扫描不等于纯色；不会自动断言任意 logo/页眉无知识语义。精确字节 Hash 相同的图片共用一页，结果回插多个 Anchor。
- 基础 bar/line/pie/doughnut/area Chart 优先提取标题、系列、类别和值引用以及本地原值。公式不计算，不使用缓存值替代原式。复杂样式、组合图、扩展对象、趋势/误差等需要视觉语义时 review，不做 GUI 截图。仅 Chart/过滤图且零视觉页时无需 Token、零云调用。
- 单源文档/单代次最多一个 Visual Bundle 文件任务；每唯一图片一页，无 OCR 标记。页数/候选数/字节/像素上限配置化；超限 review，不生成超限云任务。平台频控、Token、直连、官方 API 路径不改。

## 检查点与合并

运行中的用户文档任务在原件目录下 `.hybrid/<doc_id>/<初始路由task_id>/` 暂存 `visual_bundle.pdf`、`bundle_manifest.json`。这是业务处理输入，不是开发回合生成的测试资料，也不是 Parse Cache；没有跨文档复用、缓存命中或原件 Reparse。

`document_parse_jobs.parse_options._hybrid` 保存版本、源 Hash、路由代次、配置和 manifest。manifest 含 visual_id/source_doc_id/type/Sheet/零基 Anchor/Hash/尺寸/bundle_page，以及 Native 结果 Hash。公开 API、任务结果、日志、官方请求不输出完整 manifest；本地文件和 DB 检查点需限制访问。

申请只包含一个 PDF；POST/PUT/GET 沿用既有检查点。上传/下载/DB 临时错误有限重试，同 batch；申请不确定不盲目重发，远端明确失败不自动追加 Whole。活动 `/mineru` 恢复同一 Hybrid Job，不把它改成 XLSX 整份任务。失败后的管理员显式新代次可能重新收费。

Bundle 缺失可在真实业务恢复时根据冻结配置重建，但必须源 Hash、Native Hash、manifest 和 Bundle Hash 一致，否则安全失败；原件变化、升级导致恢复差异不绕过检查。未加入新迁移或 Outbox。

完成时在内存重新读取主结果，所有 required Bundle 页必须有有效 Block，拒绝缺页/多页/错误来源/未知 Sheet。按原 Workbook Sheet 顺序保留整 Sheet table，再将增强项按 Anchor 行、列、原位置稳定排列；**不拆 table，不承诺单元格级交错顺序**。结构化 Chart 为 native_parser；视觉结果为 mineru_cloud、page=1、section=原 Sheet、bbox=NULL。临时 Bundle 物理页不污染业务页码。

最终视觉 ID 基于源 doc_id + Hybrid 版本 + visual_id + 视觉局部原序；重复文本仅同 visual_id 同类型规范化精确去重，不进行全文语义去重。成功账本为 parsed/filtered/reused；最终失败视觉项 failed。全部 Block 替换、Document=CHUNKING、job=done、成功账本同事务，失败全部回滚再独立写安全原因，不保存 Native 半份最终结果。

## 日志与计量

复用每日中文业务日志：`hybrid.started` 是开始主解析和候选检查；`hybrid.prepared` 是准备完成，不代表落库；`hybrid.persisted` 仅在提交后记录。失败/跳过/重试沿用 parse/mineru/router 事件。只输出候选、过滤、复用、Bundle 页数、结构化 Chart 数及最终 Block 数，不输出正文/签名地址/账本。

云文件数以 job_id/batch_id 去重；重复日志不能累加配额，提交确认丢失时以平台为准。零视觉页=0文件；有视觉页最多1个 Bundle 文件。阅读顺序修复、OCR 漏识别和公式语义仍属独立质量边界。

## 用户执行的自动测试

Codex 仅编写，未运行。新增测试使用元数据/内存 XML Stub 和 Mock Provider，不生成 Office/PDF/图片验收样本。DB 测试复用已有隔离 schema，仍需已部署 PostgreSQL/vector；Broker/云调用 Mock。以下命令**仅由用户决定并执行**：

```powershell
conda activate dkf-agent
python -m pytest tests/test_hybrid.py -q
python -m pytest tests/hybrid_smoke.py -q
```

覆盖默认关闭、模式兼容、过滤/Hash 复用、单次提交/PUT 恢复、Sheet/Anchor 映射、公式原式、结构块整体保留、缺页/错身份/超限、稳定 ID、原子回滚、无第二次 Whole、日志脱敏。实际图片解码、PDF Bundle 可读性及真实 OCR 品质不是 Stub 测试已验证能力。

阶段 1～11A 与 11B.1 回归由你执行；pytest 默认文件发现不含 `*_smoke.py`，需要显式包含：

```powershell
$testFiles = @(Get-ChildItem tests -File | Where-Object { $_.Name -like 'test*.py' -or $_.Name -like '*smoke.py' } | ForEach-Object { $_.FullName })
python -m pytest @testFiles -q
node --test tests/swagger_upload.test.cjs
```

这些历史回归含原有临时夹具、Redis/Celery 检查，应先阅读对应用例并选择范围；不要把回归命令当作 Codex 的执行授权。

## 用户真实 API 人工验收

1. 你自行选择允许外传的现有混合 XLSX，不由 Codex 生成。先记录 Native/Whole 真值差异；尚未确认互补性时保持关闭。
2. 参照 README，手动将 `.env` 的 `HYBRID_XLSX_ENABLED=true`，Windows Worker 换成 solo/concurrency=1，重启 API/全部 CPU Worker。无需新迁移、依赖、系统软件或 GPU。
3. 在新知识库使用原上传接口选择 `parse_mode=auto`，记录 doc_id/最初 task_id。建议源文件有 5+视觉候选、2个完全相同图片、多 Sheet、公式和基础 Chart；同时选一份超配置上限文件。只用你已有或自己准备的文档，不要求生成器。

```powershell
$kbId = '替换知识库UUID'
$sourceXlsx = '你已有的混合文档.xlsx'
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents" -F "file=@$sourceXlsx" -F "parse_mode=auto"
$docId = '替换doc_id'
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId/parse-jobs"
```

4. 查询如下只读 SQL（替换 UUID），对照原件 Sheet/单元格/公式、图中文字、Anchor 账本与 Chart 引用。云任务完成预期 Document=CHUNKING、job=done、全量 Block；同源本代次仅1个 batch，重复图只对应一个 bundle_page。零视觉页不创建 Job。不要把任务 SUCCESS 当成正文全量识别。

```sql
SELECT doc_id, file_name, file_type, status, error_message
FROM documents WHERE doc_id = '替换文档UUID'::uuid;

SELECT job_id, state, batch_id, upload_complete, retry_count, error_code, error_message,
       parse_options -> '_route_decision' AS route_decision,
       parse_options -> '_hybrid' -> 'manifest' AS input_manifest,
       parse_options -> '_hybrid_result' AS result_manifest
FROM document_parse_jobs WHERE doc_id = '替换文档UUID'::uuid ORDER BY created_at;

SELECT block_index, block_id, page, section, block_type, source, bbox, text
FROM document_blocks WHERE doc_id = '替换文档UUID'::uuid ORDER BY block_index;

SELECT count(*) AS block_count, count(DISTINCT block_id) AS unique_ids,
       count(DISTINCT block_index) AS unique_indices
FROM document_blocks WHERE doc_id = '替换文档UUID'::uuid;
```

5. 超限/未知结构应 FAILED + ROUTER_REVIEW、无新 Block/云批次。活动恢复使用已有 `/documents/{doc_id}/mineru`，应同 Job/batch；完成状态仍禁止 Reparse。故障注入优先由你运行 Mock，不中断共用服务或故意耗尽云额度。
6. 在不同知识库比较 Native/Whole/Hybrid 的内容完整性、文件提交数、页数、耗时。真实鉴权、Bundle 上传、ZIP 页覆盖、公式不被覆盖、费用收益及 Worker 恢复必须由你确认。**本次未真实验证任何新增 Hybrid 链路，不进入阶段 12。**

## 本次文件范围

- 核心：`app/parsers/hybrid.py`、`app/services/hybrid_processing.py`。
- 接入：`app/parsers/routing/contracts.py`、`router.py`，`app/services/routed_processing.py`、`mineru/jobs.py`，`app/tasks/documents.py`、`mineru.py`。
- 配置/日志：`app/core/config.py`、`business_logging.py`、`.env.example`、`.gitignore`；本地 `.env` 已有同序六项安全默认值，核对保留，不输出真实配置。
- 测试：`tests/test_hybrid.py`、`tests/hybrid_smoke.py`（只编写未执行）。
- 文档：README、AGENTS、本文件及相关 Parser/Router/MinerU/日志说明。用户已修改的开发计划不覆盖。
