# 整份文档 Parser Router v1（11B.1）

整份 v1 默认链路仍为 `Preflight → Decision → Native / MinerU Whole / Review → Guard`。11B.2 新增默认关闭的 XLSX 单 Bundle 增强，见 [Selective Hybrid](SELECTIVE_HYBRID.md)。阶段 1～11A 不重做，无 Parse Cache、Chunk、Embedding 或 RAG；本次测试只编写未执行，真实验收由用户完成。

## 1. 使用和兼容边界

- 原 `POST /api/admin/knowledge-bases/{kb_id}/documents`：multipart `file` 多选不变，新增可选 Form `parse_mode=auto|native|mineru`。
- 原 `POST .../documents/{doc_id}/process`：无请求体，新增可选 Query `parse_mode`；仅 UPLOADED/FAILED 可新投递。
- 省略模式：保持旧 Native 消息、结果和行为，不自动付费。显式 `native` 做能力对照，结果 `coverage=explicit_native_scope`，不能声称视觉完整。
- `auto`：结构预检；`mineru`：显式整份云解析，仍须支持格式及安全校验。无 `hybrid` 外部模式。
- 原 `/mineru/documents` 和已有文档 `/mineru` 入口不变，不用新 API 路径。
- 单份普通上传 201、多份 200 表示原件/记录提交，不是路由或解析完成；异步配置/云错误通过 Document、任务与日志查看。

## 2. 决策与原子性

`RouteDecision` 包含固定版本 router-v1、模式、FileType、策略、白名单稳定理由、结构数量/物理页摘要和 fallback_count。不包含正文、Sheet 内容、路径、签名 URL、Token 或虚构置信度。PDF page_count 是真实物理页数量；Office 转换页数未知时为 null，不用 Sheet 数伪造页数。

完整快照在初始 Celery 结果 `result.route_decision` 中；云任务复用 JSONB `parse_options._route_decision`，不改变供应商参数 Hash、不发送官方请求。Native 决策不新增审计表，任务结果默认 24 小时后可能过期，届时依安全日志追踪。

| auto 输入结构 | 策略 / 主要理由 |
|---|---|
| TXT/CSV/简单 DOCX/XLSX/PDF、无图片引用 MD | native / NATIVE_COVERAGE_SUPPORTED |
| DOCX 媒体、绘图、数学、文本框、图表、嵌入及未覆盖辅助/正文结构 | mineru_whole / DOCX_HAS_* |
| PDF 扫描/图文混合/复杂绘图/批注/多栏/公式或旋转文字风险 | mineru_whole / PDF_* |
| JPG/JPEG/PNG、DOC/XLS/PPT/PPTX | mineru_whole / VISUAL_OR_LEGACY_FORMAT |
| XLSX 媒体、绘图、图表 | 默认 review；人工开启 Hybrid 后受支持结构 hybrid_bundle，进一步检查失败仍 review |
| XLSX 嵌入、VML、外链/特殊 Sheet | review / XLSX_CLOUD_AB_REQUIRED、XLSX_* |
| MD 图片引用（本地/远程/HTML），附件未上传 | review / MD_ASSET_NOT_UPLOADED |
| 超资源上限 | review / PREFLIGHT_LIMIT |

预检禁止 XML DTD/实体和网络获取，先验证真实容器及 Hash。PDF 逐页检查，横向重叠只判断风险，不改阅读顺序。小 logo、绘图/文本量和预检规模阈值通过 `.env` 中 ROUTER_* 配置，见 README。

Guard 在最终 Block 替换前检查有效正文、非空 PDF 页面覆盖/范围、Native XLSX 有效 Sheet 数和章节、来源、逻辑页、Office 空 bbox。不改变 Block/ID/顺序、不拆表格公式、不补 OCR 字符。Native 解析/检查/整体替换/CHUNKING 同事务；失败回滚后独立记安全原因，旧结果不被半份新块覆盖。

auto Native 只有明确 OCRRequiredError/CompletenessError、且平台支持该格式时最多回退一次。空/非法 CSV、损坏文档、普通 ParserError 不回退。MinerU 失败不降 Native；路由远端明确 failed 后不自动申请第二批，由管理员决定新代次。既有显式云入口的旧远端临时错误策略保持兼容。

云交接只在当前文档行锁和代次下创建 Job，换新云代次使旧 Native 消息/失败回调失效，提交后才投递既有短任务。投递确认失败留活动 Job，可用已有 `/mineru` 恢复同一代次。数据库与 Broker 非原子，进程中断不自动补投。

review：Document=FAILED、原因前缀 ROUTER_REVIEW，初始任务 SUCCESS/result.status=review 只表示决策完成，无新块和云调用。delegated_to_mineru/cloud_dispatch_failed 同样不是解析完成。

## 3. 真实人工验收步骤

1. 由你确认 Windows PostgreSQL/Redis 正常，更新配置后重启 API 和全部 CPU Worker，别让旧 Worker 消费新模式消息。无需新迁移/系统安装。Native 不需 Token，云样本需有效 MINERU_API_TOKEN 和可用 CDN 直连。
2. 创建新的验收库，避免同库 Hash 冲突，保存最初上传响应的 task_id。
3. 在 `/docs` 原上传接口选择 `parse_mode=auto` 逐个测试；或 PowerShell：

```powershell
$kbId = '替换知识库UUID'
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents" -F "file=@docs/stage11a_multiformat_test_files/10_mixed_docx.docx" -F "parse_mode=auto"
```

4. 查询策略/理由和持久化结果。云代次 ID 不是 Celery 短任务结果 ID，保存最初响应，不以 PENDING/SUCCESS 判断整份云解析完成：

```powershell
$docId = '替换文档UUID'
$initialTaskId = '替换最初上传响应的task_id'
curl.exe "http://127.0.0.1:8000/tasks/$initialTaskId"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId/parse-jobs"
```

5. Native 必须 0 云 Job/调用；云路径等待 Document=CHUNKING、job=done、Block 实际落库。日志仍在每日同一个 knowledge-service 文件，按 doc_id 搜索 router.decided、router.fallback、router.cloud_submitted、落库/失败事件。
6. review 应 FAILED/ROUTER_REVIEW、无新块、无云 Job，缺附件 MD 不抓 URL。复杂 XLSX 在两个新知识库分别用 native/mineru 对照，记录 Cell/Formula/Visual 是否完整，不能用旧 XLS 结果代替。
7. 受控检查网络/配额/鉴权失败：安全错误、有限重试、无部分块、无 Native 降级。优先 Mock 验证故障；真实测试会消耗额度，不要反复改 Token/命名空间制造限流或重复提交。

### 样本与预期

| 样本 | 预期 / 核对重点 |
|---|---|
| TXT、CSV、纯文本 MD；普通 DOCX/XLSX | Native、0 云任务；XLSX 公式原式/Sheet 名保留 |
| stage11a 的 10_mixed_docx.docx | auto 整份 MinerU；核对正文/表格/图片 OCR/图片公式 |
| stage11a 的 20_native_multisheet.xlsx | 有图片/Chart 时 auto review；两个库显式 native/mineru 对照 |
| stage11a 的 DOC/XLS/PPT/PPTX | auto 整份 MinerU；XLS 公式原式/内嵌图像可能仍遗漏 |
| 三页 PDF：Native→扫描→Native | 整份 MinerU，三个非空物理页都有结果 |
| 同页 Native + 大截图 PDF | 整份 MinerU，不因有文本层漏图中文字 |
| 双栏/复杂绘图 PDF | 整份 MinerU，不全局 y-sort，不承诺任意布局全修复 |
| 普通文本 PDF + 小 logo | Native，阈值不把小装饰图单独送云 |
| markdown_with_image_ref.md | auto review，无远程请求；Native 对照仅保留引用文本 |

办公真实样本复用 docs/stage11a_multiformat_test_files；PDF/MD 可准备上述结构的业务样本。tests/router_samples.py 的内存夹具仅证明分支/事务，不能冒充真实供应商视觉验收。

### 状态与 Block SQL

在 dkf_agent 用实际 UUID 替换占位，只读核对：

```sql
SELECT doc_id, file_name, file_type, status, processing_task_id, error_message
FROM documents WHERE doc_id = '替换文档UUID'::uuid;

SELECT job_id, task_id, state, is_active, batch_id, upload_complete,
       retry_count, error_code, error_message,
       parse_options -> '_route_decision' AS route_decision
FROM document_parse_jobs WHERE doc_id = '替换文档UUID'::uuid
ORDER BY created_at;

SELECT block_index, block_id, page, section, block_type, source, bbox, text
FROM document_blocks WHERE doc_id = '替换文档UUID'::uuid
ORDER BY block_index NULLS LAST, block_id;

SELECT count(*) AS blocks, count(DISTINCT block_id) AS unique_blocks
FROM document_blocks WHERE doc_id = '替换文档UUID'::uuid;
```

Office 云 bbox 为 null、page 为转换逻辑页；Native XLSX page=1、section=原 Sheet。成功后重复短消息不增块，旧代次失效；不能借 `/process` 绕过完成状态。

活动 Job 投递失败/进程中断手动恢复同一任务，不重传原件：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId/mineru"
```

review/FAILED 可在支持格式范围选择路径；MD 不支持 MinerU，应补附件方案或显式 Native 对照。已完成 A/B 必须用两个库，不能绕过 CHUNKING：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId/process?parse_mode=mineru"
```

## 4. 自动验证与尚未证明的能力

tests/test_parser_router.py、tests/parser_router_smoke.py 覆盖格式矩阵、显式模式、DOCX 未覆盖结构、XLSX review、扫描/混合/多栏 PDF、小 logo 配置、缺附件 MD、单次 fallback、缺页 Guard、无降级/第二批、回滚/稳定 ID、旧默认与多选契约。数据库写入回滚的临时 schema，云 API/Broker 使用 Mock。

结构启发式不能证明任意文档语义全部完整：图片面积不等于知识重要性；文本化公式/无边框表格、特殊 Office/XML、非常规多栏可能漏检。关键小图请显式选择 MinerU 并人工核对。Guard 不判断具体 OCR 漏字、公式名称误写、table_footnote 误收正文，不以 Marker 判断完整性。

旧 XLS 的公式退化/视觉遗漏不由路由修复。复杂 XLSX Whole-MinerU 本轮尚未真实验证，auto 保守 review。真实鉴权、上传、ZIP/Adapter/落库及混合完整性必须手动验证，Mock 通过不能替代。

费用摘要：初始结果实际提交 0、云计划 1；确认 batch 后提交 1，PUT/GET/下载重试不建新源文件任务。重复日志/快照不能累加为真实配额，应按 job_id/batch_id 去重。POST 确认丢失/平台拒绝时本地无法证明实际收费/额度，以平台核对为准。开启 Hybrid 后同样最多一份 Bundle；零视觉页为零云文件，没有每图/每表调用。

当前按用户授权实现默认关闭的 11B.2；用户先完成测试与 Native/Whole/Hybrid 对照，确认收益再保留启用，不自动进入阶段 12。

## 5. 本次修改文件

- 路由核心：[初始化](../app/parsers/routing/__init__.py)、[决策契约](../app/parsers/routing/contracts.py)、[预检](../app/parsers/routing/inspector.py)、[路由规则](../app/parsers/routing/router.py)、[落库 Guard](../app/parsers/routing/guard.py)、[事务服务](../app/services/routed_processing.py)。
- 兼容接入：[管理员 API](../app/api/admin_knowledge_bases.py)、[文档投递](../app/services/document_dispatch.py)、[文档任务](../app/tasks/documents.py)、[云任务](../app/tasks/mineru.py)、[云 Job 服务](../app/services/mineru/jobs.py)。仅增加可选模式、受控交接和 Guard，不修改供应商 HTTP 路由、Token/频控设计或已有 Parser/Adapter 算法。
- 配置与日志：[Settings](../app/core/config.py)、[业务日志](../app/core/business_logging.py)、[环境模板](../.env.example)、[Git 忽略](../.gitignore)。本地 `.env` 同步七个 ROUTER 变量且继续忽略；未修改 environment.yml、新增依赖或迁移。
- 文档：[README](../README.md)、[AGENTS](../AGENTS.md)、[开发计划](DEVELOPMENT_PLAN_CODEX.md)、[MinerU](MINERU_CLOUD.md)、[Parser 契约](PARSER_CONTRACT.md)、[业务日志说明](BUSINESS_LOGGING.md)、本文件。
- 测试：[结构夹具](../tests/router_samples.py)、[路由单测](../tests/test_parser_router.py)、[事务验收](../tests/parser_router_smoke.py)、[Swagger 契约](../tests/test_swagger_docs.py)。后者只更新新增可选模式后的表单字段断言，仍检查多选 File 数组和原路径。
