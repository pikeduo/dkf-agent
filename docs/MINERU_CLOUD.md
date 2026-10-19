# MinerU Cloud Provider：接口边界与人工验收

本文件供开发与验收使用；部署说明和日常调用入口见 [README](../README.md)，开发路线见 [最新开发计划](DEVELOPMENT_PLAN_CODEX.md)，不把测试记录写入 README。

当前基线：阶段 10 图片、扫描、表格、公式及阅读顺序已通过真实 API 人工验收，11A 多格式代码和质量记录保留。11B.1 默认自动路由与内部 [11B.2 XLSX Hybrid](SELECTIVE_HYBRID.md) 复用现有短任务；旧显式入口不变。新增 Hybrid 测试只编写未执行，真实 API 与收益仍由用户验收。

已确认的固定英文200页超限消息现归为PAGE_COUNT_LIMIT，本地安全错误-60006；分类不冒称响应包含数字码。新Whole Office已编写LibreOffice转换PDF/180页分片，仍待人工测试与真实恢复；旧远端failed保持终态，不自动新POST，恢复须另给新片费用预算。不能以Sheet/Slide数猜页数，也不反复GET同一失败Batch当作修复。

## LibreOffice Office分页与显式恢复

适用于新Whole DOC/DOCX/XLS/XLSX/PPT/PPTX，复用原Provider、限流、短任务、分片账本和完整事务，不新增Python转换依赖或Microsoft Office COM。安装/配置见[README](../README.md#libreofficeoffice云解析前的本地分页)。Native完整覆盖时不转云；图片单页；新Hybrid Bundle最多180页，超此已知能力缺口仅在尚未提交前转Whole分页，不以Whole绕过更小的资源预算。

- API只冻结请求，CPU首次转换。原件只读副本、独立profile、固定安全配置Hash；禁宏/活动OLE/DDE/外链更新/强制重算，应用Token/DB环境不继承。`--headless`不是操作系统级网络隔离，不保证字体/公式显示/布局保真；须用受限账户处理不可信文档。启动器/soffice.bin/版本描述Hash不是全系统字体或动态库指纹。
- 共享 `.office-pdf/<配方Hash>/` 保存不可变PDF/receipt及工作目录；身份绑定原件Hash/格式、引擎Hash、安全配置Hash。冻结后用实际PDF字节/Hash/页摘要，缺失/变化停止，不自动重新渲染。应备份完整PARSED_DIR（含隐藏目录）；失败目录保留，不自动删除。超时只尝试停止本次PID树，不按名称结束其他LibreOffice。
- 新PDF计划以180页为目标，字节超限继续二分，全片实际Hash/连续覆盖/几何验证后才首POST；原200页计划及缓存键仍按冻结策略恢复。Office原件Hash不换成PDF Hash；PDF/片Hash独立保存，逐片结果按PDF校验后映射为Office逻辑页、bbox=NULL。来源指针含派生PDF Hash，XLSX Native原式保留。
- Guard使用已冻结转换PDF的真实页范围；缺required页仍PARTIAL，禁止虚构原Office物理页或坐标。`OFFICE_PDF_RENDER_NOT_VERIFIED`只说明渲染保真尚未人工评估，不是已发现缺文，也不据此伪造缺页。READY仍不代表Office排版/语义已证明无损。
- 转换PDF单页图片条目超过5000不再直接报`OFFICE_PDF_INVALID`；通用预检记录`router.preflight_geometry_limited / PDF_VISUAL_DETAIL_LIMIT`，保留实际计数、图片面积及必需覆盖页，继续检查后续页后才能发布回执。总页数/文本行数预算、真实损坏、原件/派生Hash和180页/字节/费用校验仍保留。旧有效回执只读复用，失败工作目录不当作成功缓存，也不自动删除。
- 普通Retry优先原ZIP/成功片，不能因为新增转换配方重投原Whole。仅管理员正数`office_pdf_max_new_parts`接受同Hash、已上传且明确远端失败的Whole Office；有原ZIP、未知提交、活动任务或已分片任务拒绝。新本地代次保留原Job/Batch，最多N片，实际片数超N在首POST前报OFFICE_PDF_BUDGET。该参数与`allow_new_split_parts`互斥，不是Force；其他收费历史仍阻止提交。
- 新转换Job尚无任何提交时，准备失败可普通Retry续同Job；已授权预算保留、不扩额。只在尚未提交时可显式调整预算。已有片提交后，不再用此参数创建另一个转换任务；按原账本/ZIP及旧片续交许可处理。非预算类安全错误不可用扩额解决。
- 安装/路径/转换错误分别保留`OFFICE_PDF_DEPENDENCY/PATH/IDENTITY/CHANGED/TIMEOUT/FAILED/INVALID/BUDGET`，管理cause可见安全码，原始进程输出/私有路径/上游原文不进日志。没有新迁移；不得将代码完成当作三份已恢复。

### 用户执行：测试、本地预览、单份恢复

以下全由用户执行；Codex未运行。先运行纯Mock定向回归（不安装软件也不启动真实转换器，不访问云/GPU/业务数据库）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_office_pdf.py tests/test_office_pdf_recovery.py tests/test_pdf_coverage.py tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_pdf_cloud_acceptance.py tests/test_office_saved_snapshot.py tests/test_parse_cache.py tests/test_document_management.py tests/test_business_logging.py -q -ra
```

安装并核对LibreOffice后，先只处理第一份PPTX，`--prepare`允许本地转换/生成预览片，**没有MinerU请求、业务SQL写入、GPU或恢复POST**：

```powershell
try {
    $officePlan = $null
    $officeDocId = '8a3496d6-dd9e-40bd-be60-70b4b701002a'
    $officePlanFile = "results/office_pdf_plan_${officeDocId}_$(Get-Date -Format yyyyMMdd_HHmmssfff).json"
    # 去掉--prepare为dry-run；本块仅适用于实际准备成功后的PDF预览。
    python scripts/office_pdf_plan.py --doc-id $officeDocId --prepare --output $officePlanFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $officePlanFile -PathType Leaf)) {
        throw '本地转换/分页未通过，停止读取预览，禁止提交云任务'
    }
    $officePlan = Get-Content -LiteralPath $officePlanFile -Raw -Encoding UTF8 -ErrorAction Stop |
        ConvertFrom-Json -ErrorAction Stop
    if ($officePlan.status -ne 'LOCAL_PLAN_READY' -or $officePlan.doc_id -ne $officeDocId) {
        throw '预览未通过或文档身份不匹配'
    }
    $officePlan | Select-Object status,page_count,target_pages,planned_max_new_files,cloud_submitted,database_modified
    $officePlan.parts | Format-Table ordinal,page_start,page_end,size_bytes -AutoSize
    Invoke-Item -LiteralPath $officePlan.converted_pdf_path -ErrorAction Stop
} catch {
    $officePlan = $null
    throw
}
```

请一次执行完整`try/catch`块；交互式终端中单独一行`throw`不会阻止之后手动粘贴的其他命令。失败时不写成功计划，清空`$officePlan`并停止块内后续读取/打开，避免旧变量或不存在的JSON引发连带错误。成功应为`LOCAL_PLAN_READY`、所有页连续覆盖且每片≤180页、flags为false。人工核对PDF字体/页尾/表格/图表；预览的PDF一旦有有效回执，正式Job复用同字节，不重复转换。预览会占用磁盘并保存含正文的派生PDF，报告路径只在本机，不上传公开。缺安装/格式/预检预算/超时先修对应原因；不绕过Hash/必要预检。

**只有确认PDF内容、实际片数、全部新片外传和费用后**，等待队列任务安全结束，用户手动重启API/CPU（Windows solo并发1）加载代码；GPU沿原部署，用于后续真实Embedding。下面是授权收费的单份命令，不是离线测试：

```powershell
$officeBudget = [int]$officePlan.planned_max_new_files
if ($officePlan.status -ne 'LOCAL_PLAN_READY' -or $officeBudget -lt 1 -or $officeBudget -gt 128) { throw '先完成单份本地预览' }
$officeBase = "http://127.0.0.1:8000/api/admin/documents/$officeDocId"
Invoke-RestMethod -Method Post -Uri "$officeBase/retry?office_pdf_max_new_parts=$officeBudget" -TimeoutSec 120 -ErrorAction Stop
# 受理不是完成；随后只GET，不重复POST或批量重试。
Invoke-RestMethod -Uri "$officeBase/status" -ErrorAction Stop | ConvertTo-Json -Depth 8
```

先验收单份实际Block/Chunk/向量持久化、`chunk_count=vector_count>0`、`invalid_vector_count=0`且management.done；质量为PARTIAL时只能PARTIAL_READY，不改状态凑READY。单份通过后再依次对`81f211b8-330f-4015-9ad7-b3d377a7392b`、`c8e4ca42-f739-48c4-9c2d-50120f956309`重复预览/人工核对/单份预算确认，不能直接复制第一份预算或批量重投。保留旧失败Batch及新片费用凭证。

## v4基础提取与质量分层

当前入口`normalize()`按“ZIP安全校验→可靠基础提取→可选normalize_strict增强→质量记录”处理。旧版把合并/布局证明放在任何Block生成之前，增强异常沿Parse Job事务变成整份FAILED，这是之前MERGE_EVIDENCE集中出现的架构原因，不代表云端解析失败。

- **仍致命**：ZIP路径/符号链接/CRC/解压预算、原件/分片Hash或Job/Batch身份错误、全局结构无效、整份没有来源可确认的内容。失败不删除原ZIP、不发布业务成功。
- **可降级**：TARGET_TEXT、BOUNDARY_TEXT、LINE_SPAN_COUNT、TABLE_ROWS/EXTRAS/LIMIT等增强错误。基础层优先读取同ZIP逐页`preproc_blocks`及`discarded_blocks`的原始Span/HTML/公式，按可信index或原数组顺序保留；不按bbox猜阅读顺序。无可用原始布局的页使用V1独立项；跨页合并正文无法定位则隔离，不把整段错算到第一页。V2/full.md不补正文，不执行HTML或公式。
- **三维质量**：内容COMPLETE/PARTIAL，结构BASE/ENHANCED/DEGRADED，证据VERIFIED/PARTIAL。记录固定子原因、位置和计数；缺bbox设NULL，未知页码文字仅在本地normalized_document隔离。v4.1中，同页有可靠正文且普通原图已唯一核对同ZIP成员/Hash，只记视觉语义待审，不再一律判缺文；无结构化正文的chart、原图缺失、纯图片页及真实缺页仍PARTIAL。页眉/页码/图注不能替代图片正文。`visual_assets`保留原图指针，`visual_review_pages`保留待审页；不造图片文字Block。COMPLETE不是OCR/图像语义/问答准确率。
- 原ZIP/回执独立保全，v4结果使用新缓存身份。原v3失败Job的显式Retry核对原件、分片及原Batch ZIP后，新增`_adapter_recipe`并强制`_local_replay_only`，不更改原云配方/Batch，不调用Provider/限流/HTTP。历史Retry误换代次时，仅允许同Hash且唯一可验证的原付费Job通过正常管理事务重新绑定；不是SQL改状态或新增恢复框架。
- 全部必要分片必须有本地可验证结果。无文字的中间片可记录PARTIAL，但整份仍须存在可靠块；缺ZIP停止LOCAL_RESULT_REQUIRED，不自动发云。合并沿用原物理页，缺页及来源指针带分片序号。XLSX Native整表/公式不改，缺失视觉页只记PARTIAL且不发布共享视觉缓存，Bundle页不冒充Excel页。

已获用户确认编写`0009_parse_quality`迁移，Codex未执行。用户提供的25项v4回放报告为2完整、21部分、2身份阻塞；本轮v4.1修复及新增Mock尚待用户复测，不能据此宣布业务恢复。旧v3严格规则保留为`normalize_strict()`可选增强和历史验收依据，不再作为基础内容的入库门槛。

### v4.1回放、缺页与未提交片

- 两项身份阻塞的本地分片账本全部done；旧诊断只找`current_part()`，空游标被误当成身份错误。现在按指定Batch唯一定位账本片，即使done也只读验证其原件/片/ZIP/回执；真正身份错误输出固定`identity_diagnostic.failed_checks`。`all_parts_done`只表示账本状态，报告转换范围仍是指定Batch，未恢复数据库。
- 原ZIP元数据表明`1d4ee624…`第12页仅chart原图，`fdcfaeff…`第2/59/61页仅image原图，没有可恢复文字。保留可核对的原图成员/Hash，`coverage_gaps`明确标记`VISUAL_ONLY_PAGE`，不得改成已识别文字或虚构页证据；可靠其余内容可继续PARTIAL_READY。是否需要额外OCR/视觉理解由用户另行决定，本轮不触发。
- 保持Normalizer v4，新增`content_policy_version=mineru-content-v4.1`隔离转换缓存；旧云配方/Batch不改，旧成功产物不覆盖。升级转换使用独立`result-v4.1`指针；旧已完成片按原recipe核对复用。JSON质量字段扩展，无新迁移/依赖。
- `d8087523…`原账本第一片1～200页已收费，第二片201～217页未提交。默认Retry仍只本地恢复，缺第二片结果停止；显式`allow_new_split_parts=true`才许可原账本中的新片。已收费片不重新POST；已有Batch却缺ZIP仍拒绝。许可不能用于Whole重解析/未知POST，普通新一次Retry撤销未使用额度。新片仍沿原Provider/限流/Hash/提交意图，所有片完成后才整体入库。

由用户执行（不覆盖旧报告）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_content.py tests/test_mineru_content_pipeline.py tests/test_mineru_local_replay.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_split_continuation_api.py tests/test_document_management.py tests/test_parse_cache.py tests/test_business_logging.py -q -ra
python scripts/mineru_adapter_replay.py --checkpoint results/stage33/stage33_run_01/import_checkpoint.json --kb-id 158030a1-42d6-42ee-835d-02fdbff23f81 --output "results/stage33_v41_replay_$(Get-Date -Format yyyyMMdd_HHmmss).json" --timeout 600
```

先确认0009已迁移并重启API及CPU/GPU Worker加载新代码。下面命令仅供用户在确认**新增1个文件提交、201～217页外传及费用**后执行；Codex未执行：

```powershell
$base = "http://127.0.0.1:8000/api/admin/documents/d8087523-0aa7-4f4a-8a85-8e055710f0fd"
Invoke-RestMethod -Method Post -Uri "$base/retry?allow_new_split_parts=true" -TimeoutSec 120
Invoke-RestMethod -Uri "$base/status" | ConvertTo-Json -Depth 8
```

预期第一片复用原Batch/ZIP，不再收费；第二片独立新增Batch。未确认费用则仅调用`$base/retry`，不加参数；READY/PARTIAL_READY文档不因离线报告而重投。若返回409，先读状态核对是否仍在处理，不盲目重复请求。

用户手动验证（无MinerU请求）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_content.py tests/test_mineru_content_pipeline.py tests/test_mineru_local_replay.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split_jobs.py tests/test_parse_cache.py tests/test_stage33_pipeline.py -q -ra
python scripts/mineru_adapter_replay.py --checkpoint results/stage33/stage33_run_01/import_checkpoint.json --kb-id 158030a1-42d6-42ee-835d-02fdbff23f81 --output "results/stage33_v4_replay_$(Get-Date -Format yyyyMMdd_HHmmss).json" --timeout 600
```

第二条遍历checkpoint中**全部**MERGE_EVIDENCE项，无前N份限制。逐项记录基础Block/Chunk预览、增强/降级、覆盖/缺页、隔离和失败原因；登记数以实际checkpoint为准，不预称25份全部成功。只读数据库/本地原件与ZIP，PyMuPDF仅读取页尺寸，不调用OCR/模型/云、不写SQL或缓存。无当前唯一Job、Hash不符及预算耗尽均保留记录。退出码0只表示登记项已转换（包括PARTIAL），不代表业务恢复或正式验收通过。迁移前该诊断延迟加载新字段，可先回放再升级。

确认Mock/离线报告后，备份数据库与PARSED_DIR，停止API/所有Worker，由用户执行`python -m alembic upgrade head`确认0009，再按README重启。选择一份报告通过且原ZIP回执有效的FAILED文档：

```powershell
$docId = "填写离线报告中的真实doc_id"
$base = "http://127.0.0.1:8000/api/admin/documents/$docId"
Invoke-RestMethod -Uri $base | ConvertTo-Json -Depth 8
# 人工核对原Job/Batch后执行；不加force或allow-cloud。
Invoke-RestMethod -Method Post -Uri "$base/retry" -TimeoutSec 120
Invoke-RestMethod -Uri "$base/status" | ConvertTo-Json -Depth 8
```

预期原doc_id/Job/Batch和ZIP不变，新增MinerU提交为0；原Chunk/GPU/Index完成后为READY或PARTIAL_READY，management=done/done。后者只表示可靠块向量齐全，需审核缺口，不能计入阶段33全200 READY。请求会正常写业务状态并运行本地模型，**由用户执行**；本轮不批量恢复、Force或删旧记录。

## 1. 官方契约与实现边界

- 官方协议依据：[精准解析 API 文档](https://mineru.net/apiManage/docs)。采用 V4 本地批量申请上传链接、签名 PUT、批次 GET；不使用轻量 Agent API。
- 生产 HTTP 客户端固定 `trust_env=False`，API、签名上传、结果下载共用直连策略，不发现环境 / Windows 系统代理，不自动回退代理。保持默认 HTTPS 证书校验；不读取 `SSL_CERT_FILE / SSL_CERT_DIR`，不修改系统代理或 VPN / TUN 路由。不新增环境变量。
- 结构化结果依据：官方 [ContentListV1 序列化源码](https://github.com/opendatalab/MinerU/blob/3e60291846cb7c3bf8fe7f4f16238f4fc6cce491/mineru/backend/pipeline/pipeline_middle_json_mkcontent.py)。云端实际格式仍须以真实 ZIP 验收；不能把 API V4 与开源模型版本或 ContentListV2 混为一谈。
- 默认 `vlm / ch / enable_table=true / enable_formula=true / is_ocr=true`，保存提交时参数快照。原 `/mineru/documents` 接收 1～50 个 multipart `file`，OpenAPI 为文件数组以支持 Swagger 多选；只选一份保持原 202/Parse Job，多份逐文件建立独立 Document 和 Parse Job，HTTP 200 返回逐项报告。不新增 `/batch` 路径，不是云端合批或预置知识库批量导入；Provider 的批量申请协议与按文件数计量的共享频控不变。
- Adapter内存读取唯一V1列表并校验ZIP；同ZIP逐页原始layout可作为明确标记来源的基础内容，局部未知类型不连带拒绝其他正文。不以V2/Markdown代正文，质量和缓存见上节。
- ContentListV1 的 `page_idx` 转 1 基页码、归一化 bbox 转原件尺寸，保留正文顺序和实际标题章节；只在可选 layout 证据明确时恢复页眉/页脚位置，具体保护条件见下节。表格 HTML 与公式文本原样作为证据，不执行 HTML / JavaScript / 公式。图片仅有路径而无文字时不伪造正文；无可用块时失败。
- `full.md` 可在供应商平台下载用于人工比对；当前没有 Admin 预览接口。

### 阅读顺序：仅恢复有证据的页边界项（可选严格增强）

真实扫描样本的 V1 数组把 `type=header` 放在该页正文、页底之后；数组项没有独立的阅读顺序字段。相同 ZIP 的旧版 `layout.json` 含 `pdf_info`：页眉在 `discarded_blocks` 中，父块 `index=0`，正文在 `para_blocks` 中且索引递增。原 Adapter 只读 V1 数组，因此丢失了这份可用的顺序证据；`persist_blocks` 忠实地把列表位置写成 block_index，并非 SQL 排序或页码错误。

当前修复不重新推断整页阅读顺序：

1. 本节严格增强以V1为正文输入；v4基础层另按上节读取逐页原始来源。只读取唯一、最多64 MiB的layout；不使用V2/Markdown。
2. 每个页面的所有原始项（含稍后跳过的空图片）必须与 `para_blocks + discarded_blocks` 的父块数量相同，并按类型、归一化 bbox 全量唯一关联；页内 `index` 必须为唯一的非负整数。坐标每个分量的容差为 2/1000，仅兼容取整，不是排序键。超过 512 项的页面保守不改序，限制匹配成本。
3. 只允许移动明确的 header/footer。header 必须位于页面顶部 15% 且在所有正文上方；footer 必须位于底部 15% 且在所有正文下方。候选顺序来自 layout.index，且所有正文项的相对顺序必须完全不变；普通 title 不因位置靠上就被改序。
4. 横向分离且纵向重叠的并排块、正文纵向回跳、无 bbox、索引或关联歧义均保留该页原序。检测只是保护条件，不是可靠的通用单栏/多栏分类器；绝不依靠 bbox 重建多栏顺序。
5. 只替换该页已有槽位，不跨页、不给页面重新编号。table/formula 的完整内容、page、bbox、block_type、source、confidence 不变；原始数组位置仍生成稳定 Block ID，最终列表顺序由现有事务写入 block_index。重试和重复消息的幂等机制不变。

阶段 10 已完成真实上传和落库人工验收，扫描页边界顺序与图片落库问题已确认修复。旧 CHUNKING 数据仍不会因更新 Adapter 自动改写；保留上述保护条件，不承诺任意复杂版式恢复。

### 合并正文空占位：保留原页和坐标

阶段16三份已有批次的原 ZIP 显示：上游段落合并留下 `type=text / text=""`；对应 `para_blocks` 为 `lines_deleted=true / lines=[]`，`preproc_blocks` 仍含原文字，文字已经进入前一个正文父块的 span。Academic 有一处跨页，span 明确 `cross_page=true`。这不是整份文档无正文，也不能简单跳过空项：跨页文字会被错归前页，跨栏文字会沿用不完整的目标 bbox。V2 也有空段落，切换 V2 不能代替修复。

Adapter 的受限兼容规则（代码已实现，用例待用户执行）：

- V1仍是唯一正文来源。合并目标不必紧邻占位；header/footer/page_number须验证独立discarded来源；原样image/chart/table须有唯一para/preproc类型/bbox/index，aside_text/page_footnote须有唯一layout来源，均不得已删除。仅跨越并保留这些原项，不从中提取或推断目标正文；独立Text、Title、公式及未知类型仍阻断，不使用full.md/V2补字或按文件名判断。
- 页内类型、归一化bbox必须唯一对应前后父块，index一致无重复；空来源须有删除标记。源段落允许有界（每父块最多2048）text/inline_equation Span，联合外框覆盖原父块，目标全部Span按原序核对类型、原文、坐标和跨页标记。行内公式仅在比较时要求V1美元边界，返回值仍切自V1；重复或未知Span不猜归属。坐标容差仅兼容取整，不作为排序依据。
- 逐字符核对整段V1，允许段落间空白、Markdown转义；仅两个已严格匹配的正文父块位于相邻页、Span的cross_page/原文/坐标全通过时，兼容V1省略layout末尾英文断词`-`并直接续接ASCII字母。数字、减号、非边界连字符及其他文字变化不适用。输出仍切取V1，所有切片拼接等于原字符串，不从layout补回连字符或改写OCR。
- 每个切片按真实原索引集合逐项写回（可非连续），保留数组长度/顺序及夹在其中的边界项，保留page_idx、bbox、类型、source、置信度和稳定Block ID。跨页只接受相邻后一页且目标span明确cross_page=true；不跨页排序、不按y排序，不拆table/formula。多栏来源也必须由上述证据证明，不猜栏顺序。
- 每组最多64个占位与64个间隔、V1正文最多1MiB、每页父块最多512个。页眉脚仍在边缘15%且与正文矩形无实际交叠，不以另一栏标题的y或另一阅读流index误拒；窄小page_number也允许在全部正文上下且index在外。只用于校验、不据此排序。同一来源不能重复使用，目标在V1中也必须唯一。V1和唯一边界来源同时为确切空串时，保留其原空Block/位置/ID，不视为正文或页面覆盖、不会生成空Chunk；不一致仍失败。非相邻跨页、未知Span、真空正文/表格/公式仍拒绝，不落半份Block或发布成功缓存。
- `mineru.merge_rejected` 保留对外`MERGE_EVIDENCE`，内部由`MergeEvidenceError.diagnostic`细分失败条件；日志`step`是固定子码，`stage`是项目函数及固定source/target或para/preproc结构角色，附局部1-based页、原数组1-based位置、受限计数和异常类型。不输出文字、bbox明细、路径或签名URL。恢复日志不等于事务提交。

历史v3引入严格表格恢复；当前v4将基础与增强分层，Native版本不变。新旧Whole/视觉/片缓存隔离，旧成功产物/云配方保留。普通Reindex不静默换版本或收费；原v3失败Job的显式本地Retry及诊断许可见顶部。

#### MERGE_EVIDENCE只读诊断与原Batch复用

当前调用链是`jobs.check_step → obtain_raw_result → 独立原ZIP/回执 → normalize → 文本/表格恢复 → complete_piece → 成功缓存`。Adapter失败或SQL回滚不删除独立归档；`parse_cache_present=false`只表示没有成功缓存，不代表原ZIP丢失。原件位于`PARSED_DIR/{doc_id}/mineru_raw/{job_id}/part-{序号}-{batch_id}.zip`及同名JSON，核对Batch/Data/Hash/字节数和全部成员CRC后才能重放。历史旧Worker可能未保存原ZIP，不能仅凭下载日志假定归档存在。

**云结果获取与本地适配解耦：** `check_step`取得/读取归档后，交给不接收Provider的`adapt_raw_result`做Adapter、Guard及本地发布；Worker只在真正需要HTTP时才创建云客户端。原ZIP及来源回执不依赖Normalizer版本，修Adapter不重新取得云结果，成功Parse Cache仍按Normalizer版本隔离。

普通`/retry`对同Hash/代次、已上传、Normalizer一致且归档有效的本地错误（MERGE_EVIDENCE、EMPTY_BLOCK、CONTENT_TYPE、CONTENT_LIST、BBOX、PAGE、GEOMETRY、NO_TEXT、COMPLETENESS）恢复同一Job，强制`_local_replay_only=true`。没有Provider可调用，不需要Token，不GET/PUT/POST或下载。迟到submit消息也转入本地重放；分片成功只调度下一次本地检查，保留下一片已有Batch。后续没有完整本地结果则LOCAL_RESULT_REQUIRED停止，不自动追加收费批次；所有片成功前不写半份Block。原ZIP缺失/损坏不当成云端cache miss。未知POST、远端failed、坏ZIP不自动复活；旧v2不静默升级，也不为版本变化重新POST。

历史v3逐项兼容与严格证明继续保留在增强路径；拒绝不再阻断v4逐页原始内容。新来源类别和质量必须明确，不能将缺失/未知页码假称完整。新增Mock和真实ZIP回放由用户执行，不能据代码宣称200份已READY。

两份CONTENT_TYPE失败ZIP只读字段核对均出现`ref_text`（分别29项、2项），字段为`text/page_idx/bbox/type`；现按普通text Block保留V1原文、原页/bbox及稳定位置ID，不猜参考答案/引用或丢弃条目。真正空正文、坏坐标/页码及其他未知类型仍报错。此为类型兼容修复，不代表这两份ZIP完整Adapter/Guard已通过；用户需运行离线诊断确认后续分支。

新失败的固定子码/源码阶段/局部0-based页/原数组1-based位置保存在私有`parse_options._adapter_failure`，外部错误码仍为MERGE_EVIDENCE；阶段33报告附脱敏诊断。普通Whole也可使用本节只读诊断CLI，无需分片账本，默认自动读取同Batch归档；不接受Whole的版本/覆盖升级开关。

| 内部子码示例 | 拒绝条件 |
|---|---|
| LAYOUT_PAGE / TEXT_LEVEL / ITEM_BBOX | 页声明/正文级别/坐标关联依据不可用 |
| PARENT_LIST / PARENT_LIMIT / PARENT_INDEX / PARENT_MATCH / PARENT_INDEX_MISMATCH | 前后父块缺失、超过每页512项、重复或不一致index、bbox关联不唯一 |
| LINES_MISSING / LINE_SCHEMA / LINE_SPAN_COUNT / SPAN_TYPE / SPAN_TEXT | lines结构不符、重复/超量Span、未知Span类型或空正文Span |
| GROUP_NO_TARGET / GROUP_LIMIT / PLACEHOLDER_MATCH / TARGET_TEXT / TARGET_DELETED / SOURCE_NOT_DELETED | 占位前无正文、超过64项、占位关联歧义或删除标记不成立 |
| SOURCE_SPAN_COUNT / SOURCE_SPAN_BBOX / SOURCE_BBOX_COVERAGE / TARGET_SPAN_COUNT | 重复来源、来源坐标缺失/联合覆盖不符或目标Span数量不符 |
| SOURCE_PAGE / MOVED_TEXT / MOVED_CROSS_PAGE / SOURCE_CROSS_PAGE / MOVED_BBOX | 非相邻页、移动前后文字/位置不一致或跨页标记不符 |
| TEXT_LIMIT / SOURCE_TEXT / TEXT_CHARACTER / TEXT_TRAILING | 超过1MiB、来源文本无效、逐字符不一致或有未归属尾字 |
| BOUNDARY_LAYOUT / MATCH / INDEX / POSITION / TEXT / REUSED / LIMIT、TARGET_AMBIGUOUS | 边界来源/位置/文字无法唯一核对、重复使用、超过预算或目标正文在V1中映射不唯一（BOUNDARY子码均带完整前缀） |

200页是分片限制，不是上述每页/每组预算。大型PDF更可能包含多行、多Span或复杂跨页，但**在取得真实ZIP前不能据此认定本次根因**。可用只读工具精确复现第一个失败条件：

```powershell
conda activate dkf-agent
# 先核对已有Job/当前片/Hash/版本；缺ZIP返回BLOCKED/ZIP_REQUIRED和退出码2，属预期。
python scripts/mineru_merge_diagnostic.py --job-id dceb1360-addc-4411-9d63-0e48ddc59df7 --batch-id f3234a37-7cf3-4a47-8e9e-edc408d22fe1 --output results/mineru_merge_identity_01.json
```

工具只对现有PostgreSQL开启有界`REPEATABLE READ, READ ONLY`事务并回滚，不申请行锁、不flush/commit；核对当前代次、父/片Batch和Data ID、已上传检查点、原件Hash、片Hash/实际字节、分片策略及冻结Normalizer。无ZIP不读PDF页面。输出仅允许`results/`下的新文件，自动建父目录，不覆盖旧报告、业务缓存或manifest。正常CLI角色为`mineru-diagnostic-cli`。

**本次需要用户提供：原Batch `f3234a37-7cf3-4a47-8e9e-edc408d22fe1` 中Data ID `dkf-3e27beea2aef5a4eb05231f2e77b7fd0`、原页1～200的完整原始结果ZIP**。在MinerU平台找到这个已经done的批次并下载结果，勿点重新解析/再次上传；日志记录当时大小为14,339,844字节，大小本身不是身份或完整性证明。可保存为`results/mineru_batch_f3234a37.zip`，不要解压改写内容，也不要公开签名链接。若平台仅能提供链接，先联系用户确认允许查询原Batch并下载，不能因此新建POST。

```powershell
python scripts/mineru_merge_diagnostic.py --job-id dceb1360-addc-4411-9d63-0e48ddc59df7 --batch-id f3234a37-7cf3-4a47-8e9e-edc408d22fe1 --zip results/mineru_batch_f3234a37.zip --output results/mineru_merge_zip_01.json
Get-Content -Encoding UTF8 results/mineru_merge_zip_01.json
```

预期：失败时`FAIL/MERGE_EVIDENCE`和固定子原因、局部0-based页/原页、原数组位置；缺文件/身份异常是BLOCKED；通过时只返回`ADAPTER_PASS_NOT_RECOVERED`。已有ZIP被有界读取并计算SHA256；完整性Guard使用**真实分片geometry和原冻结页摘要**，不是拿ZIP声明尺寸冒充原件验证。报告不保存Block正文/坐标。诊断既不向云发送请求，也不写数据库/成功缓存，不推进第二片。每次换新的output文件名。

**本地恢复：** 修复原因并通过只读诊断后，使用现有`/retry`重放同一Job，不另建恢复框架、不执行SQL改状态。Whole和分片诊断均可省略`--zip`自动读原归档；指定另一ZIP时必须与已有回执Hash一致。尚无归档时只报告ZIP_REQUIRED，不联网。诊断不修改Job/缓存；显式Retry才通过原事务继续处理。没有归档的历史任务需另行取得原已完成Batch结果，不得重新解析。原ZIP通常没有可验证的Batch签名，已有下载来源回执与原件身份检查仍保留。

此前诊断回归140 passed、日志相关44 passed，历史三份ZIP转换通过；只读身份报告`results/mineru_merge_dceb1360_verified_03.json`保留不改写。本次取得`data/dataset/mineru_debug/part-0001_f3234a37-7cf3-4a47-8e9e-edc408d22fe1.zip`后，确认TARGET_TEXT是把夹在目标与占位之间的header当成目标。修复后真实`restore_merged_text`恢复1处，仅原0-based索引864/866变化，865页眉和其余2290项不变；两段124+229字符无损还原原353字符，重复恢复一致。

前次完整normalize被原V1索引1820/1893/2102的空表格阻断，167项回归及报告`results/mineru_merge_noncontiguous_01.json`保留。现已补下述严格跨页表格恢复，但**本片最终Completeness仍有独立缺页问题**，不能宣称整片已可入库。

#### 跨页表格恢复（Normalizer v3）

- 只针对缺少table_body且相同位置的后处理table_body子块明确`lines_deleted=true / lines=[]`的占位；父table通常没有该标记。V1/para/preproc同页类型/bbox/index须唯一、V1位置无重复；来源必须是连续物理页，可跨已验证的页边界及原样旁注/页下注释/代码等间隔，最多200页和512个间隔，并受下述HTML预算约束。
- V1合并HTML必须与目标para的网格/单元格投影完全等价；preproc仅证明各源表行/单元格和归属。受限HTMLParser不渲染/执行，只接受表格结构、br与受检行内eq比较；未知标签/属性、嵌套表、坏HTML、缺口/重叠/越界跨度安全失败。每组HTML总量≤8MiB，单HTML≤4MiB，最多4096行/65536单元格/1024列/262144占用格。输出始终切取原V1 HTML，不重新序列化layout。
- 行数相同时沿用已通过的连续V1原字符切片。云端省略重复页首行时，允许后一页连续前缀与前页已验证前缀逐格一致，再复用对应V1原行；其余行必须按原序消耗且最终用尽全部V1行。每页最多核对64个前缀候选，多解拒绝；不拿任意缺行冒充重复表头。输出始终来自V1，每个复用行须在该页preproc中真实存在，报告保留`reused_prefix_rows`。
- 逐行核对单元格数、td/th、原序、空白标准化后的文字及rowspan；tbody/br包装不改变单元格关系。切口不能跨rowspan。列数不同仅允许一致、严格单调且不收缩的边界细化映射，不改V1的colspan。旧完整行/重复表头/两页续接路径优先；仅其拒绝后有界证明多页重复表头与相邻页续接的组合。每个续接单元格的前后非空白字符必须完整拼回V1，标签相同、rowspan=1，不能在实体或公式标签内部切割。细化列边界必须由两份完整源页共同通过各自一致的网格投影，不能仅凭一行文字相同接受。每一V1行必须用尽；复用仅限已证明的页首前缀；每条续接行最多属于相邻两页。多解/4096次候选或累计64MiB处理预算超限安全失败，不选首个解释冒充唯一。原标签、行间空白、实体和数字不重写，报告保留`continued_row_count/reused_prefix_rows`。不从layout拷正文，不处理跨三页的单行或复杂rowspan续接。
- Caption/footnote必须由preproc唯一Span及para/V1去向共同证明：仍挂表的移动结果与后页删除标记按原规则核对；已转为同页独立Text时，必须有唯一原子块、相同index/bbox、逐Span类型/原文/坐标、非跨页标记及唯一同框V1全文对应，才保留其现有Text，不重复挂回表格。未知/缺失/重复/乱序来源拒绝，不从layout补写标题或附注；不能仅凭文字相同或index相同推断。表格根bbox不一定包含footnote，不以根框猜归属。图片只核对相对ZIP身份及唯一成员，目标仍须指向首源图，保留V1的img_path，不把合并图复制给后页，也不从layout创建新图片引用。
- 恢复按原数组索引写回，保留原page/bbox/类型；新版本/同一拟用身份下Block ID稳定，图像/原ZIP不改写。真正空table即使有caption也不能冒充表体，EMPTY_BLOCK诊断给固定子原因、1-based page/item_index及结构类型；恢复日志不等于持久化成功。

当前真实ZIP验证：文本恢复1处；表格14+14→28行（86+86个单元格）、9+9→18行（45+45个）、31+8→39行（118+73个），共453个单元格逐项核对，前两组后页footnote归还原页。完整Adapter通过、2263个Block、198个有Block的页面，未解释空正文/空表格为0。

阶段33第二片的用户离线报告`stage33_part2_local_20261018_014528.json`已越过TEXT_CHARACTER，但在原页242（局部42）、V1第483项遇TABLE_EXTRAS。只读原ZIP字段核对证明：preproc内一条25字符caption已在para转为独立text，index/bbox/Span均相同；V1第484项保留同页同框完整原文。现补通用独立Text保留路径，不补字、不复制附注、不改变表体/页/ID。新增Mock覆盖caption/footnote、行内公式及缺失/改文/坐标/页/索引/重复/双重挂载，尚未由Codex执行；完整ZIP/Guard是否通过仍待用户重跑离线诊断，不能据字段调查宣称通过。

#### 合法输出与来源校验的边界

- Span身份采用父块内的出现顺序，不以内容/bbox相同判断重复；相同公式可合法出现多次，来源与目标的数量、顺序、原文、坐标和cross_page仍逐一核对。页眉也允许一行多个纯文本Span。
- 原样代码、旁注、页下注释不应截断跨页合并链。文字和表格共享受检间隔规则，保留原项/原索引；代码须有相同的前后子结构。不越过其他正文、标题或未知类型寻找目标；表格恢复不能越过另一张非空表，文字恢复跨图表仍须沿用独立身份校验。
- 用户已批准非相邻物理页的严格例外：续段之间每一页的全部para/preproc父结构完全一致，均已唯一对应原样保留的图表/代码等间隔，才允许跨过整页；中间缺页、未保留正文、删除/改写结构、倒页或错误cross_page均拒绝。真实字段调查发现正文在第10/13页，中间第11页表格和第12页图表均未变；这只是本地字段证据，不是normalize已通过。
- 英文断词在已核对的不同父块接缝适用，包括同页分栏；只接受V1已经省略的ASCII词尾连字符，数字、减号、缺字及错误跨页标记仍拒绝。
- 本次是v3中原先拒绝结构的扩展，旧完整行/重复表头路径优先，已成功结果的正文/页/ID及缓存键不变；不重写READY文档或旧缓存。多页复杂单元格续接、无法在V1找到的附注、实际缺页仍须独立核对，不能承诺全部恢复。附注数量冲突报告`observed/expected`，Whole完整性失败报告冻结的`missing_required_original_pages`及安全计数，不从原件猜补正文或解除required。
- 只读字段调查不是Adapter验收。本轮未运行pytest、真实ZIP normalize或数据库恢复；新增`tests/test_mineru_adapter_contract.py`及相邻回归由用户执行。

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_adapter_contract.py tests/test_mineru_merge.py tests/test_mineru_merge_boundaries.py tests/test_mineru_stage33_text_compat.py tests/test_mineru_table_merge.py tests/test_mineru_local_replay.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_merge_diagnostic.py -q -ra

# 只对此前离线失败的16份重放已有ZIP；新报告目录，不上传、不恢复Job。
$audit = "results/stage33_adapter_$(Get-Date -Format yyyyMMdd_HHmmssfff)"
Get-ChildItem "results/stage33_remaining_20261018_021709073/*.json" | ForEach-Object {
    $r = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($r.status -eq "FAIL") {
        python scripts/mineru_merge_diagnostic.py --job-id $r.job_id --batch-id $r.batch_id --output "$audit/$($r.doc_id).json"
    }
}
Get-ChildItem "$audit/*.json" | ForEach-Object {
    $r = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    [pscustomobject]@{doc_id=$r.doc_id; status=$r.status; error_code=$r.error_code; reason=$r.merge_diagnostic.reason; guard=$r.completeness_guard}
} | Format-Table -AutoSize
```

只有`ADAPTER_PASS_NOT_RECOVERED`且Guard通过才使用原管理Retry；先手动重启旧CPU Worker加载修复代码。无云Job的PREFLIGHT_LIMIT不属于Adapter问题，也不在这16份ZIP回放中。不要Force Reparse、清缓存或重新提交已完成批次。

#### 本轮6份恢复及其余10份回放

用户报告`results/stage33_adapter_20261018_095528300/`中6份Adapter/Guard通过、10份失败；只读通过不等于已落库。先由用户执行上述Mock回归，再重启原CPU Worker加载代码；API、数据库、Redis、GPU Worker沿用原环境。以下操作由用户执行，不重建知识库、不重传原件。只恢复同Hash/同代次的本地适配失败；READY跳过，在途跳过，身份变化/请求不明确立即停止，不自动重发。

```powershell
$docApi = "http://127.0.0.1:8000/api/admin/documents"
$kbId = "158030a1-42d6-42ee-835d-02fdbff23f81"
$reports = @(Get-ChildItem "results/stage33_adapter_20261018_095528300/*.json" | ForEach-Object {
    Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
} | Where-Object { $_.status -eq "ADAPTER_PASS_NOT_RECOVERED" -and $_.completeness_guard -eq "PASS" })
"本轮已通过报告：$($reports.Count) 份（原报告为6份）"
foreach ($r in $reports) {
    $base = "$docApi/$($r.doc_id)"
    $doc = Invoke-RestMethod -Uri $base -ErrorAction Stop
    if ($doc.kb_id -ne $kbId -or $doc.file_hash -ne $r.original_sha256) { throw "文档身份变化，停止" }
    if ($doc.status -eq "READY") { Write-Host "已READY，跳过：$($r.doc_id)"; continue }
    if ($doc.status -ne "FAILED" -or $doc.management.state -eq "running") {
        Write-Warning "在途或非失败状态，未重复投递：$($r.doc_id)"; continue
    }
    $jobs = @($doc.parse_jobs | Where-Object {
        $_.job_id -eq $r.job_id -and $_.task_id -eq $doc.task_id -and $_.batch_id -eq $r.batch_id -and
        $_.provider -eq "mineru_cloud" -and $_.state -eq "failed" -and $_.upload_complete -eq $true -and
        $_.submission_attempted -eq $true -and $_.error_code -eq "MERGE_EVIDENCE"
    })
    if ($jobs.Count -ne 1) { throw "原失败Job身份或失败阶段变化，停止；不要Force Reparse" }
    Invoke-RestMethod -Method Post -Uri "$base/retry" -TimeoutSec 120 -ErrorAction Stop |
        Select-Object doc_id, management_task_id, operation, stage, state, status
}
```

该6份的服务端Retry会校验原ZIP回执并设置`_local_replay_only`，不调用Provider；随后按原链路继续Chunk/Embedding/Index。成功以`/status`的READY、`chunk_count=vector_count>0`、无非法向量和management/done为准，不把202当作完成。报告文件是旧快照，恢复后不会自动改写它。

继续检查其余10份时，只运行离线诊断，输出新目录：

```powershell
$audit = "results/stage33_adapter_next_$(Get-Date -Format yyyyMMdd_HHmmssfff)"
Get-ChildItem "results/stage33_adapter_20261018_095528300/*.json" | ForEach-Object {
    $r = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($r.status -eq "FAIL") {
        python scripts/mineru_merge_diagnostic.py --job-id $r.job_id --batch-id $r.batch_id --output "$audit/$($r.doc_id).json"
    }
}
```

本轮字段调查确认：3份TABLE_EXTRAS均有后页caption在V1中找不到对应原文，不能从layout补造；还须核对其他附注，不能把它们宣布已修复。`d8087523-0aa7-4f4a-8a85-8e055710f0fd`的旧报告另有`unsubmitted_part_count=1`，即使首片本地通过，另一片也不是已付费结果回放；未单独授权前不得提交。Whole的COMPLETENESS保留失败，利用新缺页信号继续只读定位，不自动解除required。

#### 最新剩余11份：区分兼容缺口与V1缺文

用户执行的`results/stage33_current_20261018_105135319/`共10份诊断，另1份没有云Job；这些报告保留不覆盖。这里只读核对已存JSON/HTML结构，没有执行Adapter、pytest或业务恢复。

| 范围 | 真实原因与下一步 |
|---|---|
| `ded9c2db…` | 用户报告Adapter/Guard均PASS；仍需原管理Retry才能落库，不等于READY |
| `6565c738…`、`70261c48…` | 分别3页/6页表格，存在边界单元格续接；6页表同时省略5次重复表头。续接行逐格非空白字符能拼回原V1，现补组合恢复及网格细化证明；完整ZIP是否通过待用户执行 |
| `5cbbfce1…`、`d8087523…` | 两页各31行合并为60行；后页第二行表头与前页不同，在V1缺失。不能把不同表头当作重复行复制，仍阻塞；后者另有1片未提交，不承诺全本地恢复 |
| `127c5887…`、`52d0fe1d…`、`cd994345…` | 后页caption在V1各字段中无匹配；最后一份另有footnote缺项。原layout虽保留来源，但当前契约禁止从layout补正文，不能伪称已完整恢复 |
| `1d4ee624…`、`fdcfaeff…` | 已通过Adapter；缺页分别12及2/59/61。V1和layout保留无文字chart/image，并非装饰横线，不能解除Guard、造OCR文字或把图片路径当可检索正文 |
| `da426abd…` | 日志确认原件约3.54MB/20页，在Router预检失败、没有云Job，并非200页或200MB平台超限；旧日志未记录具体行数/对象限制，需用户只读预检进一步区分，不自动放大预算或新建云任务 |

先由用户执行Mock回归；本次组合路径只扩展旧版拒绝输入，Normalizer仍v3，旧成功正文/页/ID及缓存不改，无数据库迁移、配置项或新增依赖。对两份组合表格复用原ZIP生成新报告：

```powershell
python -m pytest tests/test_mineru_adapter_contract.py tests/test_mineru_table_merge.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_local_replay.py -q -ra
$audit = "results/stage33_chained_$(Get-Date -Format yyyyMMdd_HHmmssfff)"
foreach ($id in @("6565c738-cbf9-4122-88d4-6cdcb49c93c6", "70261c48-1389-48ae-b9ec-a13b5614cdca")) {
    $r = Get-Content "results/stage33_current_20261018_105135319/$id.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    python scripts/mineru_merge_diagnostic.py --job-id $r.job_id --batch-id $r.batch_id --output "$audit/$id.json"
}
```

本命令只回放原ZIP，不上传、不发布缓存、不恢复Job。通过后由用户重启原CPU Worker加载新代码，再沿用上述同Hash/同Job/同Batch的管理Retry；不能对仍FAIL或缺片文件盲目重试。若要利用同ZIP的layout补回V1缺失正文，需要先取得用户对正文来源契约及版本变更的明确批准，本轮不擅自实施；无文字图像的OCR补救也须另行授权，不发起新收费任务。

**历史报告03的Guard失败：** 原冻结预检要求全部200页；ZIP的第131、188页在V1/para/preproc/discarded中均无块。冻结摘要为text_chars=0、image_count=0、drawing_count=4，当时尚未核对原页，报告为`FAIL / COMPLETENESS`。该报告保留不改写；后续只读原页调查及新规则见下节，旧冻结Job默认仍失败，不能直接删除required页、伪造Block或修改SQL。

显式只读比较旧付费Job（本轮已执行，不会修改Job、发布缓存或提交第二片）：

```powershell
conda activate dkf-agent
python scripts/mineru_merge_diagnostic.py --job-id dceb1360-addc-4411-9d63-0e48ddc59df7 --batch-id f3234a37-7cf3-4a47-8e9e-edc408d22fe1 --zip data/dataset/mineru_debug/part-0001_f3234a37-7cf3-4a47-8e9e-edc408d22fe1.zip --allow-normalizer-upgrade --output results/mineru_table_merge_acceptance_04.json
```

该开关只允许v2→v3诊断；报告保留frozen_normalizer_version、拟用版本、两套不同adapter_identity和升级许可，不改原options_hash/Batch/Data ID。无开关默认CACHE_CONFIG_CHANGED。每次使用新output；本轮最终报告为`results/mineru_table_merge_acceptance_03.json`，旧报告不覆盖。后续必须人工确认131/188页及恢复方案，不能因此再POST、Force或修改SQL。

本轮Mock回归280 passed（9.89秒），日志定向44 passed（1.42秒），不含真实数据库写入/云端/Worker验收。覆盖正常多页、缺/重复行、单元格乱序/改值、rowspan切口/网格细化、删除标记/歧义/错页、caption/footnote归属、图片身份、空结构脱敏及生产旧付费Job在Provider前拒绝版本变化。手动复测：

```powershell
python -m pytest tests/test_mineru_table_merge.py tests/test_mineru_merge.py tests/test_mineru_merge_boundaries.py tests/test_mineru_merge_diagnostic.py tests/test_parse_cache.py tests/test_mineru_order.py tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_provider.py -q -ra -p no:cacheprovider
```

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_merge_boundaries.py tests/test_mineru_merge.py tests/test_mineru_order.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py -q -ra -p no:cacheprovider
```

#### PDF 页面完整性：装饰横线与冻结覆盖比较

已只读核对原257页PDF及相邻页，原Hash、首片Hash、ZIP Hash与原记录一致。物理131/188页为612×792pt、未旋转，均无文字/字体/文字绘制、图片或批注。4个不透明灰色填充路径（两个长矩形和两个端部直线封口）共同组成宽约575.775pt、高1.3515625pt的分隔线；y分别为39.1484～40.5pt、20.9219～22.2734pt，包围盒约占页面0.1605%。Poppler预览显示除此横线外为空白；相同相对路径/灰度模板在原件33页出现，包括有正常正文的189页。这不是4个图表，也未发现需要补OCR的业务内容。

本地新调查报告为`results/mineru_completeness_pages_01/investigation.json`，同目录保留130/131/132/187/188/189页PNG预览；不含正文摘录、Token或下载URL，受`/results/`忽略。本轮仅执行原页结构/视觉调查及ZIP的V1/layout读取，**未执行pytest、完整normalize或Guard诊断**，不沿用上次的280/44项结果冒充本轮通过。

新预检采用独立`pdf-coverage-v2`，只有几何/全部绘制证据严格匹配页边缘灰细横线白名单才免覆盖；未知绘图仍required，详见[Router覆盖规则](PARSER_ROUTER.md#pdf-页面覆盖-v2不改变-normalizer)。Inspector分类、Guard执行冻结要求；生产Guard及分片平移逻辑未改，Normalizer仍为v3、正文和缓存键不变。

旧Job的Normalizer v2及旧覆盖v1不能被静默覆盖。诊断CLI新增独立`--allow-coverage-upgrade`，只在Hash一致且原257页逐页原始计数完全相同、旧快照完整时，重新预检原件并构造内存副本；只允许白名单装饰页的required=true→false。页数/计数/旧快照冲突或其他变化均`DIAGNOSTIC_COVERAGE`，原件期间变更为`PDF_SPLIT_CHANGED`。新规则不是重新选择路由，仍保留原mineru_whole/参数/批次。此许可与Normalizer升级许可独立，生产Worker没有这两个开关。

用户手动执行（输出文件必须不存在；无新云调用、缓存发布、业务SQL写入或第二片投递）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_pdf_coverage.py tests/test_parser_router.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_table_merge.py tests/test_mineru_merge_boundaries.py -q -ra -p no:cacheprovider
python scripts/mineru_merge_diagnostic.py --job-id dceb1360-addc-4411-9d63-0e48ddc59df7 --batch-id f3234a37-7cf3-4a47-8e9e-edc408d22fe1 --zip data/dataset/mineru_debug/part-0001_f3234a37-7cf3-4a47-8e9e-edc408d22fe1.zip --allow-normalizer-upgrade --allow-coverage-upgrade --output results/mineru_completeness_acceptance_01.json
Get-Content -Encoding UTF8 results/mineru_completeness_acceptance_01.json
```

预期而非已执行：文本恢复1处、表格恢复3组、2263 Block；`frozen_completeness_guard=FAIL`（131/188）、`guard_evaluation_scope=proposed_coverage_only`、新`completeness_guard=PASS`、`status=ADAPTER_PASS_NOT_RECOVERED`。报告保留新旧覆盖版本、两套决策Hash、逐页变更、原冻结Normalizer/拟用版本/Adapter身份，并明确coverage比较不改Normalizer。缺少coverage开关仍按旧冻结要求FAIL/COMPLETENESS；两个开关都不加则旧Normalizer在读取ZIP前CACHE_CONFIG_CHANGED。

用户已执行该只读诊断并确认`ADAPTER_PASS_NOT_RECOVERED`。诊断不恢复FAILED Job、不标Cache READY、不继续第二片。当前不开发旧Job恢复框架，转为下述[分片到原始ZIP验收](#只验收分片到原始zip)；无数据库迁移、新环境变量或依赖。

先运行 Mock 单测（不需要数据库、Worker、GPU或云端）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_merge.py tests/test_mineru_order.py tests/test_mineru_provider.py tests/test_parse_cache.py tests/test_smoke_stage16_17.py -q
```

隔离数据库的事务/幂等回归另由用户执行，需要已有 PostgreSQL；Provider/Broker 都是 Mock，不消耗 MinerU：

```powershell
python -m pytest tests/mineru_db_smoke.py -q
```

#### 只读验证三份旧 ZIP（用户手动执行）

以下命令只转换已有 ZIP 的 JSON 并打印摘要，不读/渲染 PDF、不发云请求、不写数据库或缓存。geometry 使用 ZIP 的 layout 声明，**仅验证适配兼容，不证明它与原 PDF 的实际尺寸、OCR或阅读顺序一致**。稳定身份是本地检查专用，不用于业务回填。

```powershell
Set-Location "D:\python project\dkf-agent"
conda activate dkf-agent
$mineruZipCheck = @'
import json
from pathlib import Path
from uuid import UUID
from app.services.mineru.adapter import MinerUResultAdapter, read_content_list, read_layout_pages
from app.services.mineru.errors import MinerUError

# 使用原导出中的真实文件名；只读原 ZIP，所有错误仅输出受控错误码。
root = Path("docs/test/stage16_smoke/run-01")
snapshot = json.loads((root / "reports/20261013T045527924270Z-f57bc325-export.json").read_text(encoding="utf-8-sig"))
documents = {row["doc_id"]: row for row in snapshot["documents"]}
for path in sorted((root / "mineru_debug").glob("*/mineru_result.zip")):
    doc_id = path.parent.name
    if path.stat().st_size > 256 * 1024**2:
        print(doc_id, "FAIL ZIP_SIZE")
        continue
    archive = path.read_bytes()
    try:
        # 先验证 ZIP，再读取可选布局几何；不解压或修改原件。
        read_content_list(archive)
        pages = read_layout_pages(archive)
        geometry = {idx: tuple(page["page_size"]) for idx, page in pages.items()}
        arguments = dict(doc_id=UUID(doc_id), file_name=documents[doc_id]["file_name"],
                         file_type="pdf", geometry=geometry, identity="manual-zip-check")
        first = MinerUResultAdapter().normalize(archive, **arguments)
        second = MinerUResultAdapter().normalize(archive, **arguments)
        assert first == second and len({b.block_id for b in first.blocks}) == len(first.blocks)
        print(doc_id, "PASS", "blocks=", len(first.blocks), "pages=", sorted({b.page for b in first.blocks}))
    except MinerUError as error:
        print(doc_id, "FAIL", error.code)
    except (KeyError, TypeError, ValueError, AssertionError):
        print(doc_id, "FAIL LOCAL_INPUT_OR_IDENTITY")
'@
python -c $mineruZipCheck
```

按原 ZIP 条目统计，若全部占位通过上述核对，预期 Academic/Textbook/Finance 的 Block 数分别为135/14/72（无文字 image/chart 沿用原跳过规则）；这只是待验证预期，不是已执行结论。用户还须对照原 PDF 核对 Academic 第4页文字是否恢复到第4页、左右栏 bbox 与原件是否一致，不能仅按数量验收。

**离线转换成功不复活原 FAILED Document/Job，不发布 Parse Cache，也不触发 Chunk/Embedding/Index。** 当前没有旧 ZIP 回填或终态批次重放接口；不得修改 SQL 状态假装成功，不通过 Force Reparse 偷换成新收费批次。先反馈以上验证结果，再单独确认只使用原 batch/原文件身份的显式恢复方案。真实云接口的后续复验仍由用户授权并手动执行，本次旧 ZIP 检查不是新 API 调用验收。

### 阶段 11A：历史直传Office格式与页面语义

- [官方批量上传文档](https://mineru.net/apiManage/docs)的 file.name 支持 DOC/DOCX/PPT/PPTX/XLS/XLSX，复用原 POST→PUT→GET 流程；不发送 CSV/TXT/MD。
- 申请前与上传共用 OOXML/OLE 内容校验，继续检查大小、Hash 和安全标识。现代 Office 校验对应主部件，旧格式用 olefile 检查 CFB 与对应主流；这不是旧 Office 正文 Native 解析。
- Office 的本地页数未知返回 None；提交按文件数限流不变，优先页观察只累计已知 PDF/图片页数，未知不伪造为 1。云端最终执行 200 页限制，错误走已有安全分类，不修改限流、Token 或 Celery 整体设计。
- page_geometry 对 Office 返回 None，不尝试 PyMuPDF。Adapter 保留转换逻辑 page_idx+1，bbox 始终为 NULL，即便有供应商坐标也不伪造原件对应位置。逻辑页在 1～200 内；PDF/图片仍验证真实原件范围和尺寸。
- Office 保留 V1 原数组顺序，不套用 PDF 页边界修复；table/formula 块完整不拆分，原位置生成稳定 ID。写块、Document=CHUNKING、job=done 使用已有单事务及代次检查，重复消息不增块。

## 2. 持久化、幂等和恢复

`0004_mineru_jobs` 只新增任务表，不重建前九阶段业务表。`task_id` 是文档处理代次；官方批量接口由 batch_id / data_id 标识外部文件，不虚构外部单文件 task_id。

同一文档、Hash、供应商、模型与参数快照最多一条活动记录。文档行锁串行创建，部分唯一索引兜底。旧代次和终态消息跳过；结果写块、完成任务与推进 CHUNKING 同事务，任一步失败全部回滚，不生成 Chunk / Embedding / READY。

提交意图在 POST 前持久化；batch 和私有签名上传链接在 PUT 前持久化。上传网络失败或 Worker 重启沿用同一批次 PUT；链接成功使用后清除。若 POST 响应丢失或进程在响应持久化前退出，无法证明未创建远端批次，等待保护期限后记录 `SUBMIT_UNCERTAIN`，不自动再 POST。请先在供应商平台确认，再决定人工提交；此 API 没有已确认的幂等请求键，因此不声称绝对外部 exactly-once。

未完成仅执行一次检查，countdown 再投递。明确 429、临时服务 / 网络错误指数退避加 0～5 秒抖动，尊重 Retry-After，默认最多重试 5 次；总等待默认 7200 秒。远端明确 failed 终止本代次，不自动重建收费 batch；未知 err_msg 不透传。

远端终态的安全根因独立保存为`_remote_failure`（已知代码、Batch/Data/trace标识），路由的`ROUTED_REMOTE_FAILED`仍阻止普通Retry重投。识别既有白名单数字码或固定原因映射，未知原因只报REMOTE_FAILED；不保存或输出err_msg。管理cause及失败日志保留安全分类。历史原因缺失可由用户执行`python scripts/mineru_remote_status.py --doc-id <UUID>`只读查看当前Job；只有显式`--query-existing`才用原Provider GET一次原Batch，沿用TLS/身份/共享限流，不POST/PUT、下载结果、调用Adapter、改Job或发布缓存。该查询不是恢复或完整性验收；原批次即使done也不据此写READY。

### 远端失败原因诊断

官方精准解析[中文契约](https://mineru.net/doc/docs/index_pro/)及[英文契约](https://mineru.net/doc/docs/index_en/)中，单项失败原因位于`data.extract_result[].err_msg`；HTTP 200/code=0只说明查询成功，不能代替该项的state。本地Provider已保留对应data_id的结果字段，旧诊断只匹配六条中文消息及白名单错误码，不能从REMOTE_FAILED反推平台没有原因。

诊断CLI新增`failure_diagnostic`，不更改生产`result_failure`、Job终态或Retry权限。已知错误码优先；完整中英文固定短语可分类，不能凭消息含某个关键词猜测。模糊的“文件转换失败”只报`FILE_CONVERSION_FAILED`，不编造-60015/-60016。报告顶层`cause_code`仍是原生产分类，`message`为固定中文摘要；具体分类依据见`source/category`。

| reason_status | 含义 |
|---|---|
| KNOWN_CODE | 接口包含可识别错误码，source指定err_code或error_code |
| CLASSIFIED_MESSAGE | 接口消息命中完整固定短语；不表示已取得底层traceback |
| MESSAGE_MISSING / MESSAGE_NULL / MESSAGE_EMPTY | err_msg未返回、null或仅为空白 |
| MESSAGE_INVALID_TYPE | err_msg不是字符串，未递归输出其内容 |
| MESSAGE_UNRECOGNIZED | 接口确实返回非空消息，但本地未识别；不能说“平台没有提供原因” |

`fields`仅记录err_code/error_code/err_msg的状态，字符串长度与完整SHA256；代码值只输出已有白名单。Hash用于判断两次未知消息是否相同，不能由Hash推导原因。原文、未知字段、文档名、URL/凭据不进入报告、业务日志、数据库或异常。大于2048字符的消息只记录指纹，不尝试语义分类。默认本地检查与非failed状态不附加这份远端诊断；旧数据库原因不会因此回填。

仅CLI有变化，无需重启API/Worker。由用户先运行`python -m pytest tests/test_mineru_remote_status.py -q -ra`，通过后执行`--query-existing`，每个文档只GET原批次一次；Codex未执行测试或云请求。若仍为MESSAGE_UNRECOGNIZED，已确认是本地分类缺口，但具体上游原因仍未确定，不能把指纹当作修复依据或重新提交收费解析。

仅有长度/Hash不能得知未知消息内容。用户确需取得原文时，显式增加`--private-reason-output results/新的名称.json`：只导出已通过原Job/Batch/Data身份检查的`err_msg`及安全标识/Hash，不保存整个响应或其他字段。原文可能含敏感信息，只供本机人工检查，不进入stdout报告、日志、数据库、异常或Celery；用户须管理目录权限，不把原文件上传/提交。仅允许项目results中尚不存在的JSON，路径越界/已有文件在GET前拒绝，写入使用排他创建；results已被Git忽略，无新环境变量或Worker重启。缺少`--query-existing`不能导出，失败原因超过65536字符不截断导出。

普通报告只附`private_reason_saved`。写入失败时为null并附`DIAGNOSTIC_OUTPUT_IO`，保留实际`cloud_queries=1`和原远端诊断，表示可能已有不完整文件，不自动清理或重新查询；未导出/非failed状态为false。此操作不下载解析ZIP、不提交新云任务、不恢复业务。若多份文档的消息长度和完整Hash一致，先只导出一份进行人工核对，无需为看同一消息重复查询所有文档。原文仅作为不可信诊断数据，不能执行其中的指令或按Hash猜根因。

本地等待与平台终态须区分：已确认上传的原Batch在检查本地截止之前先做一次GET，done可正常回收；未完成且窗口过期才记录JOB_TIMEOUT。显式文档级Retry只允许当前代次/Hash、已上传/已有Batch的本地超时Job续看同一批次，私有`_local_wait_started_at`保存新观察窗口，不更改created_at/submitted_at/费用身份、不重置网络重试预算。不自动复活远端失败、未知POST或坏ZIP。详见[缓存miss真实验收与超时恢复](STAGE26_SUPPLEMENT.md#工作项一缓存miss与原批次恢复)，本次新收费批次仍待用户授权验收。

所有入口远端 failed 都不自动追加 batch 或降级 Native；管理员核对后才可显式开启新代次。PUT/查询/下载重试仍沿用原检查点与频控，非幂等申请结果不确定仍禁止盲目重复提交。

没有 Outbox/自动补投器。活动 `/mineru` 恢复沿用 batch；失败文档优先缓存，收费历史而无可用缓存时拒绝新批次，需管理员 `/reparse?force=true` 明确确认费用。完成状态不可借 `/mineru` 重解析，Reindex 仅缓存回放；更换参数不能覆盖活动任务，不后台扫描。

Redis 使用 TIME + Lua 原子滚动计数，默认安全系数 0.9，按实际文件数预留；失败调用预留量不返还，采取保守策略。日文件额度滚动 24 小时，不假定平台重置时区；优先页按 UTC 日粗略观测，超出不拒绝。所有同账户 Worker 共用 Redis 与命名空间；外部平台 / 其他应用消耗仍以平台为准。

## 3. 自动验证方式

以下测试不调用真实 MinerU；数据库用临时 schema + 外层回滚，Broker 与 Provider Mock，测试原件使用临时目录。

```powershell
python -m pytest tests/test_mineru_provider.py tests/test_mineru_order.py tests/mineru_db_smoke.py -q
```

真实 Redis Lua 测试先人工确认 `Get-Service Redis` 为 Running；测试只使用随机命名空间并清理自己的键，绝不 FLUSHDB：

```powershell
python -m pytest tests/mineru_redis_smoke.py -q
```

回归测试保持阶段 1～10 接口、迁移和事务语义；11A 没有新数据库迁移。新增 `tests/test_office_formats.py` 覆盖六种格式 Mock HTTP、Office 逻辑页与空 bbox；数据库测试覆盖 Office 上传、重复消息和 PUT 重试的批次复用。

顺序测试覆盖单页、两页及交错页不跨页、同 y 稳定顺序、表格/公式整体保留、空图片不补 OCR、无 bbox、多栏/纵向回跳、未知/歧义 layout、稳定 ID；数据库测试覆盖新顺序事务落库、重复消息不增块，以及图片查询网络重试耗尽后必须显式建立新任务。

## 4. 已验收 PDF/图片链路的真实 API 复验步骤（按需）

### 准备与启动

1. 先备份项目数据库及上传原件，停止旧 API / CPU Worker。确认 Windows PostgreSQL 17 与 Redis 服务可用；无需 WSL、Docker 或本地 MinerU 模型。
2. 在本机 `.env` 填写真实 `MINERU_API_TOKEN`，确认只有该本地文件保存密钥，`.env.example` 不填真实值。检查解析参数及账户可用额度；真实调用会外传原件并消耗平台额度。
3. 激活环境并执行迁移，然后启动 API 和 CPU Worker。若未将 python / uvicorn 命令放入 PATH，先激活 Conda：

```powershell
conda activate dkf-agent
python -m alembic upgrade head
python -m alembic current
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

另一个终端：

```powershell
conda activate dkf-agent
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=solo --concurrency=1 --hostname=cpu@%h -l info
```

预期迁移为 `0004_mineru_jobs (head)`；Worker 注册两个 MinerU 任务，并消费 default_queue。GPU Worker 不必启动。

### 四类真实样本

准备允许外传、每份少于 200 页且小于当前上传上限的样本：

1. 一张 JPG / PNG，包含清楚可辨认的文字。
2. 一份纯扫描 PDF，最好不少于两页，以检查真实物理页码。
3. 一份含表格 PDF，能人工核对表头、单元格、标题与脚注。
4. 一份含数学公式 PDF，能人工核对 LaTeX 与相邻说明。

在 `http://127.0.0.1:8000/docs` 创建一个新的验收知识库，避免与已有 Hash 冲突。本节验证显式路径，通过 `POST /api/admin/knowledge-bases/{kb_id}/mineru/documents` 上传，记录 doc_id/job_id；普通 `/documents` 省略 parse_mode 默认 auto，新自动路由验收另见 Router 文档。

如果样本已经上传但处于 UPLOADED / FAILED，可调用 `POST .../documents/{doc_id}/mineru`；已经 CHUNKING 的样本不能以此重新解析，改用独立验收知识库。

### 验证链路与数据

1. **鉴权和最小请求**：首个样本成功获得 batch_id / trace_id，未出现 Token 错误。在 MinerU 平台核对该批次 / data_id。不要截图或分享真实 Token 与签名链接。
2. **PUT 和异步检查**：GET `.../documents/{doc_id}/parse-jobs` 观察 waiting-file / pending / running / converting 等状态；可能快速完成而跳过可观察的中间态。确认 API 请求不会持续等待整份解析。重复提交同一活动文档应返回相同 job_id，不产生第二条活动记录。
3. **结果 ZIP 与结构**：平台任务完成；应用 job=done、Document=CHUNKING。若结果结构或官方资源域名改变，应用应明确失败，不静默丢块。可在供应商平台下载 ZIP 人工核对 content_list（勿提交 Git），不能仅因 full.md 看起来正确就判定程序成功。
4. **Block 入库与证据**：在 pgAdmin / psql 使用以下 SQL，把示例 UUID 替换为本次 doc_id：

```sql
SELECT doc_id, status, error_message
FROM documents WHERE doc_id = '替换为文档UUID';

SELECT job_id, provider, model_version, data_id, batch_id, trace_id,
       state, retry_count, error_code, error_message, is_active
FROM document_parse_jobs WHERE doc_id = '替换为文档UUID'
ORDER BY created_at;

SELECT block_id, block_index, page, section, block_type, text, bbox, source, confidence
FROM document_blocks WHERE doc_id = '替换为文档UUID'
ORDER BY block_index;

SELECT block_type, count(*) FROM document_blocks
WHERE doc_id = '替换为文档UUID' GROUP BY block_type;
```

5. **质量**：四类样本均有实际正文；source=mineru_cloud。核对页码、阅读顺序、bbox 与原件一致；表格样本至少出现 table，公式样本至少出现 formula，文本内容与原件对应，未知置信度允许 NULL。图片 OCR 可能返回 text 而非 image_text，不强行改造类型。扫描 PDF 不得只有第一页结果。
6. **幂等**：重复请求活动任务仍为同一 job_id；完成后再次提交应 409。记录块数与 Block ID，确认重复短消息不新增块（Mock 已覆盖）；不要为了测试在真实平台反复创建付费批次。
7. **异常**：可用一个无效 Token 在独立测试环境验证安全失败，再恢复有效 Token；确认前端、Worker 日志和 Celery 结果无 Token / 签名 URL。空文件、损坏原件应在提交前拒绝；超限PDF按下节连续分片，单页仍超限则全份停止、尚未提交任何片。429、临时失败、日额度和 Worker 重启主要由 Mock / 共享限流测试覆盖，不向平台发压测耗尽真实额度。
8. **恢复**：如果 Broker 中断导致 503，先查询已保存 doc_id / job_id，修复服务后调用显式提交入口恢复，不重新上传；若 SUBMIT_UNCERTAIN，先人工核对远端孤立批次，不直接重试。

### 结果下载与代理排查

本地 `submit_mineru_parse` 成功且返回 `scheduled_check` 只说明该短任务完成并安排了检查，不代表 ZIP 已下载或 Block 已入库。`check_mineru_result` 查询云端状态为 `done` 后才下载结果；下载失败会回滚本轮事务，本地 job 可能仍显示之前的 `waiting-file / pending`。达到重试上限后，本地 job 与 Document 会标记失败，即使云端已经成功。

按以下顺序人工检查，先不要重复上传：

1. 使用文档详情及 `GET .../documents/{doc_id}/parse-jobs` 确认本地状态、错误码、重试次数、活动标记与 batch_id；在 MinerU 平台核对同一批次是否已完成。
2. 区分三条网络链路：API 为 `mineru.net`，原件上传使用官方签名对象存储链接，结果 ZIP 使用响应中的官方 CDN 地址。API 查询、原件上传成功不能证明结果下载成功。
3. 确认更新后的 CPU Worker 已重启；当前 MinerU 客户端固定 `trust_env=False`，不读取 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY` 或 Windows 系统代理。HTTPX 原始默认值为 `True`，因此旧 Worker 仍可能自动使用代理，环境变量为空也不代表旧版本直连。参考 [HTTPX 官方代理配置说明](https://www.python-httpx.org/environment_variables/)。该设置不绕过 VPN / TUN / 全局路由；若希望网络层也直连，需要你手动关闭相关模式或设置系统级直连。不要公开代理凭据、Token 或签名下载链接。
4. 对官方结果域名分别检查 DNS、TCP 和 HTTPS；当前官方示例为 `cdn-mineru.openxlab.org.cn`。以下命令不提交云端解析任务：

```powershell
Resolve-DnsName cdn-mineru.openxlab.org.cn
Test-NetConnection cdn-mineru.openxlab.org.cn -Port 443
curl.exe --head --noproxy "*" --connect-timeout 5 --max-time 15 https://cdn-mineru.openxlab.org.cn/
```

站点根路径返回 403 / 404 也可以证明收到 HTTPS 响应，但不能证明具体签名 ZIP 有效；DNS 或 TCP 成功也不代表 TLS 与下载成功。直连仍超时时，先手动核对 VPN / TUN / 防火墙，再用其他网络（例如手机热点）对比；在 MinerU 平台尝试下载同一批次。如果只有当前网络失败，优先处理本地线路；多条独立直连网络均失败时，向 MinerU 支持提供安全的 batch_id / trace_id 核对 CDN。不要把禁用代理视为已恢复下载，也不要关闭 HTTPS 校验、改写签名 URL 或硬编码 CDN IP。

5. `NETWORK` 表示连接阶段失败；`DOWNLOAD_NETWORK` 归并了其他下载 HTTP 客户端异常，可能是读取超时、读取错误或代理异常，单凭中文错误不能确定具体异常类型。排查时只记录异常类型、耗时和域名，不输出原始带签名 URL 的异常。
6. 网络恢复后重启 CPU Worker，并先确认旧任务是否仍活动。活动任务可恢复同一批次；已失败的非活动任务没有“仅下载旧批次”的现成接口。若平台已完成，优先人工下载核对结果，决定是否允许新建付费批次；不要把重新上传或对失败任务再 POST 描述为无成本的原任务恢复。人工下载不会自动把结果导入本地数据库。

### 图片不在 CSV 中的检查与恢复

只查询 document_blocks 的内连接导出看不到零块文档，不能据此判断上传时有没有创建 Document。先在 pgAdmin / psql 执行以下只读 SQL，比较全部同名记录及它们所属的知识库，不要把旧原生入口记录和云入口记录混在一起：

```sql
SELECT d.kb_id, d.doc_id, d.file_name, d.file_type, d.file_hash,
       d.status AS document_status, d.error_message AS document_error,
       d.processing_task_id, j.task_id, j.job_id,
       j.state AS job_state, j.batch_id, j.data_id,
       j.upload_complete, j.is_active, j.retry_count,
       j.error_code, j.error_message AS job_error, j.finished_at,
       (SELECT count(*) FROM document_blocks b
        WHERE b.doc_id = d.doc_id) AS block_count
FROM documents d
LEFT JOIN document_parse_jobs j ON j.doc_id = d.doc_id
WHERE d.file_name = '01_mineru_image_ocr.png'
ORDER BY d.created_at, j.created_at;
```

历史诊断曾发现：原生入口图片无 parse job，云入口图片因 NETWORK 重试耗尽而零块，并非 Adapter 格式拒绝。阶段 10 后续真实人工验收已确认图片成功落库；历史失败记录不代表当前仍未验收。上述 SQL 继续用于排查未来零块文档，不通过删除旧失败记录掩盖错误。

API 检查（把 UUID 替换为上面 SQL 查出的云端记录）：

```powershell
$taskKbId = "替换为知识库UUID"
$taskDocId = "替换为文档UUID"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId/parse-jobs"
```

网络恢复不会自动复活终态任务。先核对旧批次和 ZIP；阶段12普通提交不再重新收费，只有明确允许新云调用后执行 Force Reparse（无需重传原件）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId/reparse?force=true&parse_mode=mineru"
```

返回202及新的本地 task_id；云 job_id 通过 parse-jobs 查询。成功应 job=done、Document=CHUNKING、block_count>0；不是仅下载旧 ZIP，可能重新收费。409先查状态，不改数据库或删旧记录。

### 回归抽检与导出

阶段10已验收，不必重做付费主链路。额外抽检可用独立知识库；已有 CHUNKING 只允许显式维护入口，Reindex不调用云，Force Reparse需确认额度。Office验收仍分别核对。

等待 job=done、Document=CHUNKING 后执行以下 SQL，将知识库 UUID 替换为本次验收库。用 LEFT JOIN 同时显示尚无块的文档，再按 block_index 验收；不要用 `ORDER BY bbox` 掩盖持久化顺序问题：

```sql
SELECT d.doc_id, d.file_name, d.status, d.error_message,
       b.block_id, b.block_index, b.page, b.block_type,
       b.section, b.text, b.bbox, b.source, b.confidence
FROM documents d
LEFT JOIN document_blocks b ON b.doc_id = d.doc_id
WHERE d.kb_id = '替换为知识库UUID'
ORDER BY d.file_name, b.block_index;

SELECT d.doc_id, d.file_name, count(b.block_id) AS block_count,
       count(DISTINCT b.block_id) AS unique_ids,
       count(DISTINCT b.block_index) AS unique_indices
FROM documents d
LEFT JOIN document_blocks b ON b.doc_id = d.doc_id
WHERE d.kb_id = '替换为知识库UUID'
GROUP BY d.doc_id, d.file_name
ORDER BY d.file_name;
```

人工确认：扫描 PDF 两页的顶部页眉均先于正文和页底；物理页码、bbox 不变；同页正文相对顺序没有改变；图片有可读文字（类型可能是 text/title，而非固定 image_text）；表格、公式没有被拆分或替换。每份已完成文档的块数、不同 ID 数、不同序号数应相等。没有公开 Block 查询 API 时以 SQL 为准，不新增接口来绕过本阶段边界。

### 四类样本当前质量边界

- 扫描 PDF 第一页的倾斜 `SCANNED-1-CHECK` 未作为正文返回；对应原始项为空 image，Adapter 跳过而不伪造 OCR。第二页有识别文字。这是模型漏识别，不由顺序修复补全。
- 公式样本名称 YoY 视觉识别为类似 `Y_0Y`，原始公式内容保持不变；上下文语义标准化留待阶段 27，不写特例替换。
- 表格后 `TABLE-AFTER-TEXT-7B21` 在真实 V1 的 `table_footnote` 中，不是 Adapter 把独立正文吞掉。Adapter 按 caption → body → footnote 合成一个 table 块；目前没有足够独立位置与语义证据稳定拆分，故保留并记录为阶段 10 边界。
- 本版不承诺修复所有标题顺序、双栏/多栏布局、左右图文、OCR、表格归属或公式语义。边界回退意味着保留供应商原序，不代表已经验证该原序正确。

## 5. 六种真实 Office 人工验收（阶段 11A）

1. 同步 environment.yml 依赖并重启 API/CPU Worker，确认 Windows Redis 服务 Running、Token 和额度有效；无需 WSL、Docker 或 LibreOffice。
2. 准备允许外传的真实 DOC、DOCX、XLS、XLSX、PPT、PPTX，每种一份；使用 Word/Excel/PowerPoint 真正保存为对应格式，不能改后缀或使用自动测试的最小容器冒充真实文件。
3. 创建独立云验收知识库，在 Swagger 逐份调用 `POST /api/admin/knowledge-bases/{kb_id}/mineru/documents`，multipart 字段 file；预期 202，记录 doc_id/job_id。DOCX/XLSX 与 Native 比较使用不同知识库，避免 Hash 去重与完成状态限制。
4. 查询文档详情和 `/documents/{doc_id}/parse-jobs`，核对 batch_id、upload_complete（可用下面 SQL）、state、错误；任务最终 job=done、Document=CHUNKING、Block 非空。中间 converting 状态可能很快结束，不要求必然观察到。
5. 执行下面 SQL，核对类型、章节、原文顺序、table/formula、逻辑页与空坐标。Office 页码只和平台转换结果比较，不与 Office 打印预览页数硬对齐。若上游返回未知结构，应安全失败，不能以 full.md 替代。

```sql
SELECT d.file_name, d.file_type, d.status, j.job_id, j.state,
       j.batch_id, j.upload_complete, j.retry_count, j.error_code, j.error_message
FROM documents d LEFT JOIN document_parse_jobs j ON j.doc_id = d.doc_id
WHERE d.kb_id = '替换为云验收知识库UUID'
ORDER BY d.file_name, j.created_at;

SELECT d.file_name, b.block_id, b.block_index, b.page, b.section,
       b.block_type, b.text, b.bbox, b.source
FROM documents d LEFT JOIN document_blocks b ON b.doc_id = d.doc_id
WHERE d.kb_id = '替换为云验收知识库UUID'
ORDER BY d.file_name, b.block_index;
```

6. 活动请求重复提交应返回同一 job_id，完成文档再提交应 409；核对 Block ID/序号不重复。不要为压测耗尽云额度；网络/PUT 重试、重复消息和事务回滚优先使用 Mock 验证。
7. CSV 调用云上传入口应 415，不创建云任务；OOXML 类型不匹配或伪造 OLE 应 415，已有原件异常通过显式提交返回安全错误。Office 本地不估计页数，云端页数超限应安全失败，无部分 Block。

## 6. 当前验证边界

阶段 10 已完成人工验收；最新开发计划记录 DOC/DOCX、PPT/PPTX 和 XLS 的真实云结果及质量边界，XLSX 云结果仍需独立对照，不能从 XLS 推断。本轮没有重新调用真实 API，11B.1 新路由仍需人工验收。Mock 不能替代真实鉴权、格式转换、页面语义和视觉质量，也不等于真实 429 或所有网络故障均已验证。

默认 auto 与 XLSX Hybrid 保留，阶段12新增整份/视觉缓存和显式 Reparse，待人工验证。上文“失败后重新提交可收费”仅是历史行为，当前必须明确 Force Reparse；不使用 Review，不进入 Chunk/Embedding/RAG，质量边界不由缓存修复。

## 超限 PDF：连续分片

Windows句柄修复及首片文本/表格恢复已保留，用户已确认首片Adapter/新覆盖Guard通过（2263 Block）。当前只补齐两片云端原始ZIP获取验收；本轮未执行真实原件分片、云API或业务数据库操作，不表示阶段32解析/评测通过。

### 范围、身份和限制

- Router依旧决定Native/Whole/Hybrid；新云PDF超过180页或200 MB时进入分片，新Whole Office先经LibreOffice转换后进入同一机制，见上节。分片保守取十进制200,000,000字节，完整Native不拆、图片单页；上传/必要预检预算不变。
- Worker先规划全部连续页区间，新计划优先180页，旧200页账本冻结；实际文件大于200MB则有界二分缩小，每片最多10次尝试。接受前核对字节/SHA256和裁剪框/旋转/视口；单页仍超限在首POST前停止。最多128片，原Router页数预算不放宽，超限不丢页。
- 原件只读，Document.doc_id/file_hash/file_name和官方QA完全不变；无子Document、无新表/迁移/环境变量。一个父Parse Job用现有JSONB的`_pdf_split_request`、`_pdf_split`保存版本/原Hash/总页数/每片原页范围、Hash、字节、状态和收费身份。
- 每片有稳定的data_id和逻辑task_id；后者不是Celery消息ID。父Job/task_id仍是当前原文档解析代次，父行batch_id/data_id/upload_complete表示**当前片**，历史片必须查账本，不把父Job行数当费用。
- 文件位于`PARSED_DIR/<doc_id>/pdf_split/<job_id>/part-0001.pdf`，并有manifest.json和同片结果指针。JSONB是收费状态真相，磁盘manifest仅为不含URL/正文的可核对副本；写副本失败记录安全事件，不据副本重建收费身份。仅允许服务自有目录，拒绝链接/越界，不覆盖不匹配的旧片。备份需包含整个PARSED_DIR及数据库。

### 短任务、缓存和原子性

每个短任务只推进当前片，复用原Provider：申请前提交意图，PUT前提交Batch，查询/下载错误沿用同Batch和原Redis共享限流。按实际每片一个文件预留额度；确认Batch的不同片计入`mineru_file_submit_count`，未知POST可能已计费，必须人工核对，不能当成确定零费用。

下载后先核对全部ZIP成员CRC、路径/展开预算和唯一非空V1结构列表，精确原字节及来源回执独立保存在`PARSED_DIR/<doc_id>/mineru_raw/<job_id>/part-NNNN-<batch_id>.zip/.json`。回执包含来源片Hash、Batch/Data ID、ZIP SHA256/字节数；同Batch仅复用完整一致的归档，冲突/损坏不覆盖、不因此再POST。该归档不是成功Parse Cache；Adapter/Guard失败及SQL回滚都不删除原ZIP，日志分开记录下载、归档、转换及落库。原有旧Job不会据磁盘回执自动恢复。

随后片结果通过Adapter/局部Guard，保存原ZIP/content_list/可选full.md/标准化块到成功缓存，再保存不可变代次指针和游标。SQL回滚可恢复同片指针，不另POST；完全相同片Hash、模型/参数/归一化/分片策略可跨原件复用。Force Reparse只绕过原件级成功入口，**仍有效的成功片不重复收费**；只有确实缺少结果且已明确允许新收费的片才提交。普通重试遇到收费历史但结果缺失安全停止。

所有片成功并核对原件Hash/分片Hash/页覆盖后，按原页排序且同页稳定保留Adapter原序，整体保留table/formula/text/bbox/source；Block ID绑定原doc_id、缓存身份和原位置，不含消息ID。映射公式：`原物理页 = page_start + 片内1-based页 - 1`；OHR原0-based证据页仅在评测侧+1，不改官方数据。不得全局按y排序或让临时局部页成为引用。

只在全片完成时发布**原件级完整缓存**，一次事务替换全部Block并推进CHUNKING；之后原管理任务接续Chunk→Embedding→Index，仍只有Index可置READY。任一片失败不发布部分Block；已有旧块保持但Document退出READY。Reindex只回放完整缓存，不重拆或发云请求。分片缓存/整份缓存都受现有容量限额保护。

活动消息丢失可用原入口或文档级Retry续同Job。显式Retry可恢复原已上传Batch的JOB_TIMEOUT/传输错误、仍有效签名链接的原Batch PUT，以及从未提交片的频控/缓存占用等待；保留成功片和网络预算，不重建收费身份。远端失败、SUBMIT_UNCERTAIN、坏ZIP/来源/缓存不自动复活，先核对平台、备份和原结果；确需新解析再由管理员确认Force费用。单片观察窗口与重试预算有限，成功推进到下一片才重置本片窗口，管理cloud窗口相应续开；不会自动复活已失败的管理任务。

### 只验收分片到原始ZIP

`scripts/mineru_pdf_split_acceptance.py`是精简的隔离云链路验收入口：复用生产`prepare_manifest/build_parts/render_range`、`MinerUCloudProvider`、原参数与Redis共享限流、原始ZIP归档器。它不连接业务数据库、不修改旧Job或Cache READY、不调用Adapter/Chunk/Embedding/Index。现有生产`jobs.check_step`仍使用同一归档器及原分片状态机；本入口只把云端ZIP获取与后续转换分开验证，不替代业务入库。

默认仅检查本地PDF页数/Hash并打印预览，不拆文件或联网。执行必须同时指定`--execute --allow-cloud`，实际分片数不能超过`--max-files`，否则首次POST之前停止。输出目录必须全新，不覆盖旧报告；不支持通过重跑自动恢复/再付费。`doc_id/job_id`仅为隔离运行的来源标识，不代表创建了数据库记录。

仅需用户已部署的Redis（共享限流）、有效`MINERU_API_TOKEN`和本地依赖；**不需要API、CPU/GPU Worker、PostgreSQL或模型**。以下原件路径是已有257页AES原件，先核对预览的页数和Hash；若另存原件，仅替换`--pdf`，不可编辑原文件：

```powershell
Set-Location "D:\python project\dkf-agent"
conda activate dkf-agent
python scripts/mineru_pdf_split_acceptance.py --pdf data/uploads/ead578f3-b18d-44c9-befb-ebf24fd9b5f0.pdf --output results/mineru_split_cloud_01 --max-files 2
```

预期`DRY_RUN`、257页、原Hash=`9c5de2227534c4d6bf3d805f635bad5d82eddf8e4183a85a3d1311b48b50291d`、最少2个云文件任务。新计划生成180页和77页，旧已冻结200+57计划不变；每片仍须实际字节/Hash/几何校验。本次是**全新2个文件、通常2个Batch**，不是复用已有首片Batch；旧首片可能已收费，本次会额外计费，未知提交/超时也不保证未收费。只有用户确认本原件外传、全部两片费用后才执行下一命令；Codex不自行发起：

```powershell
python scripts/mineru_pdf_split_acceptance.py --pdf data/uploads/ead578f3-b18d-44c9-befb-ebf24fd9b5f0.pdf --output results/mineru_split_cloud_01 --max-files 2 --execute --allow-cloud --timeout 7200 --poll-interval 15
$result = Get-Content results/mineru_split_cloud_01/report.json -Raw -Encoding UTF8 | ConvertFrom-Json
$result | Select-Object status, confirmed_submissions, adapter_status, business_database_modified
$result.parts | Format-Table ordinal,page_start,page_end,batch_id,upload_status,parse_status,zip_status,zip_sha256 -AutoSize
$result.parts | Select-Object ordinal,zip_path,zip_sha256 | Format-List
```

预期`CLOUD_ZIP_PASS`、两片`uploaded/done/validated`，新计划分别1～180/181～257页；报告给出ZIP绝对路径、SHA256、大小和结构条目数。`adapter_status=NOT_RUN`、业务库未修改、`business_parse_success=false`是本轮正确边界，不是失败；不能据此宣称Adapter/READY通过。报告每次请求前后更新，按片只POST一次，正常状态GET有界轮询；CLI允许等待，不改变Celery短任务/countdown机制。

失败返回非零退出码和`INCOMPLETE`，保留已取得的原ZIP/Batch。`JOB_TIMEOUT`只代表本地停止等待，`SUBMIT_UNCERTAIN`可能已收费；**先查报告和MinerU平台，不换目录盲目重跑、不Force Reparse、不改旧SQL状态**。下载/CRC/结构错误单独记入`zip_status/error_code`；第一片转换问题不会阻挡该隔离入口取第二片ZIP，因为本入口完全不调用Adapter。生产主链路依旧严格：Adapter失败则整份失败，不以ZIP成功冒充解析成功。

用户复测纯Mock（不生成真实PDF、不连接数据库/Redis/云端）：

本轮实际结果：分片/Provider完整流程及相邻元数据回归65 passed；新日志角色/归档脱敏定向17 passed。首次pytest受系统临时目录权限阻断，改用仓库results下全新临时目录后通过；未修改系统权限，Mock不能替代真实两片云端验收。

```powershell
python -m pytest tests/test_mineru_pdf_cloud_acceptance.py tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py -q -ra -p no:cacheprovider
```

### 用户手动验证（完整业务链路另行确认）

#### Windows保存句柄修复与离线验收

原`target.open("xb") → piece.save(stream)`会使PyMuPDF把具名文件对象转换回`.name`路径，而Python仍持有Windows目标句柄，导致`FzErrorSystem: cannot remove file ... Permission denied`。该异常继承`FzErrorBase(Exception)`，旧`RuntimeError`捕获不能覆盖，因而曾返回UNKNOWN。

当前在独立临时子目录对**未存在的字符串路径**直接`piece.save(str(generated), ...)`，关闭所有MuPDF句柄并核对页数/裁剪框/旋转后，再以`xb`独占复制原字节至目标。即使检查后目标被另一进程创建，也拒绝覆盖。不修改生成参数、分片策略版本、SHA256算法或原页映射，不为本次文件写特例。额外暂存/复制会占用少量额外本地I/O及磁盘空间，保存失败在任何云提交之前停止。

保存/发布异常安全返回`PDF_SPLIT_WRITE`，其他可识别MuPDF读取/生成错误为`PDF_SPLIT_IO`；业务事件`mineru.split_io_failed`仅记录类型和固定`stage/step`，不输出异常原文、Token、签名URL或源文件路径。旧UNKNOWN任务不自动复活，本轮不重投或修改Job。

已在Windows Conda `dkf-agent`中实际执行：新增用例8 passed（0.63秒），相邻分片Mock 51 passed（6.63秒）。新增真实PyMuPDF验证使用临时无正文257页PDF，得到200+57页，全部页几何/原件Hash保持，输出与既有相同参数的参考字节一致；同时覆盖已存在目标、发布竞态、FzErrorSystem/Generic/PermissionError及安全诊断。首轮仅有旧`.pytest_cache`写权限警告，未修改权限；不等于AES原件、真实云端或数据库并发已通过。

用户手动复测，不需要启动任何服务：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_pdf_split_windows.py -q -ra -p no:cacheprovider
python -m pytest tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py -q -ra -p no:cacheprovider
```

如需人工复验自己的真实PDF，可使用以下**纯离线**命令。原件只读；输出为系统临时目录中新建的唯一目录，不使用PARSED_DIR中的活动Job目录，不创建Job、不上传、不联系数据库，不改阶段32原件/QA：

```powershell
$pdfSource = (Resolve-Path -LiteralPath (Read-Host "输入待离线验证的PDF路径")).Path
$pdfOutput = Join-Path ([IO.Path]::GetTempPath()) ("dkf-pdf-split-" + [guid]::NewGuid().ToString("N"))
$offlineCheck = @'
import sys
from functools import partial
from pathlib import Path
import pymupdf
from app.services.mineru.pdf_split import build_parts, render_range, file_digest
from app.services.mineru.errors import MinerUError
source, output = Path(sys.argv[1]), Path(sys.argv[2])
try:
    output.mkdir(parents=True, exist_ok=False)
    before = file_digest(source)
    with pymupdf.open(source) as pdf:
        rows = build_parts(pdf.page_count, output, partial(render_range, pdf))
    assert file_digest(source) == before
    for row in rows:
        path = output / f"part-{row['ordinal']:04d}.pdf"
        assert path.stat().st_size == row['size_bytes']
        assert file_digest(path) == row['file_hash']
except MinerUError as error:
    print("OFFLINE_FAILED", error.code)
    raise SystemExit(1)
except (pymupdf.mupdf.FzErrorBase, OSError, ValueError, AssertionError) as error:
    print("OFFLINE_FAILED", type(error).__name__)
    raise SystemExit(1)
print("OFFLINE_OK", [(r['page_start'], r['page_end'], r['size_bytes']) for r in rows])
'@
python -c $offlineCheck $pdfSource $pdfOutput
if ($LASTEXITCODE -ne 0) { throw "离线验证失败；未提交云任务，不要盲目重投" }
Write-Host "离线分片输出目录：$pdfOutput"
```

257页原件在大小不触发进一步缩片时预期为`1～200`、`201～257`；任何失败都不要因此执行Force Reparse。原批次是否已提交、后续恢复与费用仍须单独核对并授权。

更广的回归命令如下，不需要启动服务、真实原件或GPU（本轮只执行了上述8+51项，未执行以下整组）：

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py tests/test_mineru_provider.py tests/test_parse_cache.py tests/test_document_management.py tests/test_business_logging.py tests/test_smoke_stage16_17.py -q -ra
```

新增用例覆盖257/217/382页、实际字节限制/双超限/单页超限、有界规划、连续无重复页、原件/片Hash、原页与QA索引、Chunk来源页、稳定ID、全片失败保护、重复消息、PUT重试、未知POST、缓存/SQL回滚恢复及Native/Office兼容。Stub事务不是PostgreSQL真实原子性或多Worker并发证明；原有`tests/mineru_db_smoke.py`等隔离DB回归及真实恢复仍由用户另行确认执行。

真实验收方案：用户先确认独立ACTIVE验收库、原件外传权、各分片预计费用、CPU/GPU与磁盘预算，重启新版API、CPU solo Worker与原GPU Worker（命令见README）。**本任务未授权Codex执行以下步骤，不自动处理阶段32原件。** 在获准的独立原件中选一份确需云解析的超限PDF，记录原Hash；实际字节拆分数只能在规划后确认。示例命令仅供用户确认后自行运行：

```powershell
$kbId = "替换为独立验收知识库UUID"
$pdfPath = Read-Host "输入已获外传许可的测试PDF绝对路径"
if ((Read-Host "确认自动分片会产生多个MinerU文件任务并接受费用，输入YES") -cne "YES") { throw "未授权" }
Get-FileHash -LiteralPath $pdfPath -Algorithm SHA256
$uploaded = curl.exe --fail-with-body -sS -X POST -F "file=@$pdfPath" -F "parse_mode=auto" "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents" | ConvertFrom-Json
$docId = $uploaded.doc_id
Invoke-RestMethod "http://127.0.0.1:8000/api/admin/documents/$docId/status"
Invoke-RestMethod "http://127.0.0.1:8000/api/admin/knowledge-bases/$kbId/documents/$docId/parse-jobs"
```

在pgAdmin执行只读SQL，替换一次文档UUID；不要导出整个parse_options（包含私有配置/签名上传检查点）：

```sql
SELECT j.job_id, j.state AS parent_state, j.error_code,
       p->>'ordinal' AS part_no, p->>'page_start' AS original_start,
       p->>'page_end' AS original_end, p->>'size_bytes' AS actual_bytes,
       p->>'file_hash' AS part_sha256, p->>'data_id' AS data_id,
       p->>'batch_id' AS batch_id, p->>'state' AS part_state,
       p->>'submission_attempted' AS attempted,
       p->>'upload_complete' AS uploaded, p->>'error_code' AS part_error
FROM document_parse_jobs j
CROSS JOIN LATERAL jsonb_array_elements(
  COALESCE(j.parse_options->'_pdf_split'->'parts', '[]'::jsonb)) p
WHERE j.doc_id = '替换为文档UUID'::uuid
ORDER BY j.created_at, (p->>'ordinal')::int;

SELECT d.doc_id, d.file_name, d.file_hash, d.status,
       d.management_config->>'operation' AS operation,
       d.management_config->>'state' AS management_state,
       (SELECT count(*) FROM document_blocks b WHERE b.doc_id=d.doc_id) AS blocks,
       (SELECT count(*) FROM document_chunks c WHERE c.doc_id=d.doc_id) AS chunks,
       (SELECT count(embedding) FROM document_chunks c WHERE c.doc_id=d.doc_id) AS vectors
FROM documents d WHERE d.doc_id = '替换为文档UUID'::uuid;

SELECT block_index, page, block_type, source
FROM document_blocks WHERE doc_id='替换为文档UUID'::uuid ORDER BY block_index;
SELECT chunk_id, page_start, page_end, chunk_metadata->>'page_semantics' AS page_semantics
FROM document_chunks WHERE doc_id='替换为文档UUID'::uuid ORDER BY page_start, chunk_id;
```

验收预期：一个doc_id；各片原页从1连续到末页无重叠、每片<=200页/200,000,000字节；257/217/382页在字节不超限时分别为200+57/17/182。首片完成、后片未完成时没有新半份Block/READY；全片成功后原页201确实对应第二片局部1页。最终READY、ingest/done、chunk_count=vector_count>0；仍需原阶段15检查1024维/单位向量及Evidence反查，并人工核对跨片表格/段落/章节质量。账本Batch数和平台费用逐一对应，不能累加重复日志；整份缓存命中可零新提交、无本代次分片账本，属于正常复用。

本地超时/Worker停机后先查询原Job/Batch，确认允许续查而非新收费，再手动`POST /api/admin/documents/{doc_id}/retry`；SUBMIT_UNCERTAIN停止并核对平台。分片丢失、Hash不符或缓存损坏应从备份恢复，不删除旧Job、篡改SQL状态、清Redis或盲目Force。生成器版本变化会安全拒绝继续，不自动更换输入。真实CRC/裁剪旋转、云接口/版面质量、多进程DB锁及费用恢复尚未由本轮验证。
