# MinerU Cloud Provider：接口边界与人工验收

本文件供开发与验收使用；部署说明和日常调用入口见 [README](../README.md)，开发路线见 [最新开发计划](DEVELOPMENT_PLAN_CODEX.md)，不把测试记录写入 README。

当前基线：阶段 10 图片、扫描、表格、公式及阅读顺序已通过真实 API 人工验收，11A 多格式代码和质量记录保留。11B.1 默认自动路由与内部 [11B.2 XLSX Hybrid](SELECTIVE_HYBRID.md) 复用现有短任务；旧显式入口不变。新增 Hybrid 测试只编写未执行，真实 API 与收益仍由用户验收。

## 1. 官方契约与实现边界

- 官方协议依据：[精准解析 API 文档](https://mineru.net/apiManage/docs)。采用 V4 本地批量申请上传链接、签名 PUT、批次 GET；不使用轻量 Agent API。
- 生产 HTTP 客户端固定 `trust_env=False`，API、签名上传、结果下载共用直连策略，不发现环境 / Windows 系统代理，不自动回退代理。保持默认 HTTPS 证书校验；不读取 `SSL_CERT_FILE / SSL_CERT_DIR`，不修改系统代理或 VPN / TUN 路由。不新增环境变量。
- 结构化结果依据：官方 [ContentListV1 序列化源码](https://github.com/opendatalab/MinerU/blob/3e60291846cb7c3bf8fe7f4f16238f4fc6cce491/mineru/backend/pipeline/pipeline_middle_json_mkcontent.py)。云端实际格式仍须以真实 ZIP 验收；不能把 API V4 与开源模型版本或 ContentListV2 混为一谈。
- 默认 `vlm / ch / enable_table=true / enable_formula=true / is_ocr=true`，保存提交时参数快照。原 `/mineru/documents` 接收 1～50 个 multipart `file`，OpenAPI 为文件数组以支持 Swagger 多选；只选一份保持原 202/Parse Job，多份逐文件建立独立 Document 和 Parse Job，HTTP 200 返回逐项报告。不新增 `/batch` 路径，不是云端合批或预置知识库批量导入；Provider 的批量申请协议与按文件数计量的共享频控不变。
- Adapter 内存读取唯一 `content_list.json / *_content_list.json`；完整校验成功后阶段12缓存保存 ZIP、结构化结果及可选 full.md。未知 V2/类型失败，不以 Markdown 代正文；详见 [Parse Cache](PARSER_CONTRACT.md#阶段-12parse-cache-契约与验收)。
- ContentListV1 的 `page_idx` 转 1 基页码、归一化 bbox 转原件尺寸，保留正文顺序和实际标题章节；只在可选 layout 证据明确时恢复页眉/页脚位置，具体保护条件见下节。表格 HTML 与公式文本原样作为证据，不执行 HTML / JavaScript / 公式。图片仅有路径而无文字时不伪造正文；无可用块时失败。
- `full.md` 可在供应商平台下载用于人工比对；当前没有 Admin 预览接口。

### 阅读顺序：仅恢复有证据的页边界项

真实扫描样本的 V1 数组把 `type=header` 放在该页正文、页底之后；数组项没有独立的阅读顺序字段。相同 ZIP 的旧版 `layout.json` 含 `pdf_info`：页眉在 `discarded_blocks` 中，父块 `index=0`，正文在 `para_blocks` 中且索引递增。原 Adapter 只读 V1 数组，因此丢失了这份可用的顺序证据；`persist_blocks` 忠实地把列表位置写成 block_index，并非 SQL 排序或页码错误。

当前修复不重新推断整页阅读顺序：

1. V1 仍是唯一正文输入。只内存读取唯一、最多 64 MiB 的可选 `layout.json`；未知新 schema、缺失、损坏或歧义时不用它改序，不用 V2 / Markdown 替代。
2. 每个页面的所有原始项（含稍后跳过的空图片）必须与 `para_blocks + discarded_blocks` 的父块数量相同，并按类型、归一化 bbox 全量唯一关联；页内 `index` 必须为唯一的非负整数。坐标每个分量的容差为 2/1000，仅兼容取整，不是排序键。超过 512 项的页面保守不改序，限制匹配成本。
3. 只允许移动明确的 header/footer。header 必须位于页面顶部 15% 且在所有正文上方；footer 必须位于底部 15% 且在所有正文下方。候选顺序来自 layout.index，且所有正文项的相对顺序必须完全不变；普通 title 不因位置靠上就被改序。
4. 横向分离且纵向重叠的并排块、正文纵向回跳、无 bbox、索引或关联歧义均保留该页原序。检测只是保护条件，不是可靠的通用单栏/多栏分类器；绝不依靠 bbox 重建多栏顺序。
5. 只替换该页已有槽位，不跨页、不给页面重新编号。table/formula 的完整内容、page、bbox、block_type、source、confidence 不变；原始数组位置仍生成稳定 Block ID，最终列表顺序由现有事务写入 block_index。重试和重复消息的幂等机制不变。

阶段 10 已完成真实上传和落库人工验收，扫描页边界顺序与图片落库问题已确认修复。旧 CHUNKING 数据仍不会因更新 Adapter 自动改写；保留上述保护条件，不承诺任意复杂版式恢复。

### 合并正文空占位：保留原页和坐标

阶段16三份已有批次的原 ZIP 显示：上游段落合并留下 `type=text / text=""`；对应 `para_blocks` 为 `lines_deleted=true / lines=[]`，`preproc_blocks` 仍含原文字，文字已经进入前一个正文父块的 span。Academic 有一处跨页，span 明确 `cross_page=true`。这不是整份文档无正文，也不能简单跳过空项：跨页文字会被错归前页，跨栏文字会沿用不完整的目标 bbox。V2 也有空段落，切换 V2 不能代替修复。

Adapter 的受限兼容规则（代码已实现，用例待用户执行）：

- V1仍是唯一正文来源。合并目标不必在数组中紧邻占位，但只允许跨越有独立layout证据的header/footer/page_number；其他Text、Title、Table、公式、图片及未知类型不能跳过，不使用full.md/V2补字或按文件名判断。
- 页内类型、归一化 bbox 必须唯一对应前后父块，index 一致且无重复；空来源必须有删除标记，源段落当前只支持一个非空纯 text span。目标的所有 span 必须按原顺序与这些源 span 的文本/坐标完全对应。坐标容差仅兼容取整，不作为排序依据。
- 逐字符核对整段 V1，允许段落间空白与 Markdown 标点转义。输出直接切取 V1 原字符串，所有切片拼接须等于原字符串；不模糊匹配、改写 OCR、重复拼入 layout 文字或丢掉额外内容。
- 每个切片按真实原索引集合逐项写回（可非连续），保留数组长度/顺序及夹在其中的边界项，保留page_idx、bbox、类型、source、置信度和稳定Block ID。跨页只接受相邻后一页且目标span明确cross_page=true；不跨页排序、不按y排序，不拆table/formula。多栏来源也必须由上述证据证明，不猜栏顺序。
- 每组最多64个占位与64个边界、V1正文最多1MiB、每页关联父块最多512个。边界须唯一匹配discarded父块、全页index无冲突、确在页面边缘且index在正文前/后，边界原文逐字可核对；同一边界父块不能重复使用，目标在V1中的位置也必须唯一。多Span/公式混排、非相邻跨页或未知结构不尝试恢复。证据冲突为MERGE_EVIDENCE；真空正文/表格/公式仍为EMPTY_BLOCK，不落半份Block、不发布成功缓存。
- `mineru.merge_rejected` 保留对外`MERGE_EVIDENCE`，内部由`MergeEvidenceError.diagnostic`细分失败条件；日志`step`是固定子码，`stage`是项目函数及固定source/target或para/preproc结构角色，附局部1-based页、原数组1-based位置、受限计数和异常类型。不输出文字、bbox明细、路径或签名URL。恢复日志不等于事务提交。

归一化缓存身份更新为 `mineru-adapter-v2`，Native 缓存版本不变。旧成功文件不删除、不改写；旧 v1 云缓存不混入新版 Whole/视觉键，Reindex 遇到版本不一致安全返回 CACHE_CONFIG_CHANGED，不自动重算或收费。已经验证的同 Job 缓存可按原检查点恢复；尚未完成的旧冻结任务在 POST/PUT/GET 前拒绝版本不一致，不用新版结果覆盖旧键。所有进程须由用户按 README 手动重启。

#### MERGE_EVIDENCE只读诊断与原Batch复用

调用链是`jobs.check_step → download_result(内存) → normalize → restore_merged_text → complete_piece → 片缓存`。因此**Adapter失败前下载的ZIP不会由这条链路落盘**，不能因`result_downloaded`日志而假定已有缓存。没有来源证据时仍拒绝，不新增多Span拼接、跨页猜测、空项跳过或y坐标排序。后续已兼容经过严格验证的非连续页边界，不改变原已接受输入的输出/Block ID，沿用Normalizer v2；不据此自动复活旧失败Job或改写缓存。

| 内部子码示例 | 拒绝条件 |
|---|---|
| LAYOUT_PAGE / TEXT_LEVEL / ITEM_BBOX | 页声明/正文级别/坐标关联依据不可用 |
| PARENT_LIST / PARENT_LIMIT / PARENT_INDEX / PARENT_MATCH / PARENT_INDEX_MISMATCH | 前后父块缺失、超过每页512项、重复或不一致index、bbox关联不唯一 |
| LINES_MISSING / LINE_SCHEMA / LINE_SPAN_COUNT / SPAN_TYPE / SPAN_TEXT | lines结构不符、单行多Span、非纯文本或空Span |
| GROUP_NO_TARGET / GROUP_LIMIT / PLACEHOLDER_MATCH / TARGET_TEXT / TARGET_DELETED / SOURCE_NOT_DELETED | 占位前无正文、超过64项、占位关联歧义或删除标记不成立 |
| SOURCE_SPAN_COUNT / SOURCE_SPAN_BBOX / SOURCE_BBOX_COVERAGE / TARGET_SPAN_COUNT | 一个源段落多行Span、来源坐标缺失/覆盖不符或目标Span数量不符 |
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

**待单独授权的显式恢复方案（目前无FAILED-MERGE ZIP回放接口，不可执行SQL改状态代替）：**

1. 备份原Job、分片和ZIP，平台人工确认Batch/Data ID及下载来源；ZIP通常没有可验证的Batch签名，不能仅凭ZIP文件名或本地Hash证明云身份。
2. 取得真实失败子原因并补通用证据兼容及回归；若Normalizer语义变化，设计明确的旧付费Job兼容审计，不能伪造旧缓存版本或为升级重新POST。
3. 新显式恢复入口须固定doc/job/代次/片号/Batch/Data ID/原Hash/片Hash/ZIP Hash/解析参数及版本；默认dry-run，事务行锁下重核对，过期代次/来源不一致拒绝。仅Adapter+局部Guard成功后复用原片缓存发布和检查点，不跳过Parse Job或伪造READY。
4. 恢复与投递下一片必须分开授权：本次第二片尚无Batch，现有`complete_piece → advance_piece`会返回`submit`；不得直接调用它并自动投递。恢复原第一片**不应再次收费**，但第二片首次提交仍可能收费，需用户另行确认。只有全部片成功才原事务整体入Block并继续Chunk/Embedding/Index。

MERGE终态不属于已有JOB_TIMEOUT/传输错误Retry白名单，当前不建议`/retry`、`/process`或Force。先保留失败状态与账本，提供ZIP，禁止删除Job/缓存或盲目新增批次。

此前诊断回归140 passed、日志相关44 passed，历史三份ZIP转换通过；只读身份报告`results/mineru_merge_dceb1360_verified_03.json`保留不改写。本次取得`data/dataset/mineru_debug/part-0001_f3234a37-7cf3-4a47-8e9e-edc408d22fe1.zip`后，确认TARGET_TEXT是把夹在目标与占位之间的header当成目标。修复后真实`restore_merged_text`恢复1处，仅原0-based索引864/866变化，865页眉和其余2290项不变；两段124+229字符无损还原原353字符，重复恢复一致。

**完整normalize仍未通过：** 原V1索引1820/1893/2102（物理页155/162/183）为table，却无table_body且caption/footnote为空，触发原有EMPTY_BLOCK。未删除这些项、未改成图片/空成功块、未拿layout或Markdown补表。此独立表格问题须另行分析可验证的结构来源，不能用合并修复成功宣称整片解析完成。新相邻回归167 passed（7.00秒）；真实验证使用既有分片的实际geometry并核对Hash，只读原ZIP/分片，未访问数据库或调用云端。结果摘要见本地`results/mineru_merge_noncontiguous_01.json`。

```powershell
conda activate dkf-agent
python -m pytest tests/test_mineru_merge_boundaries.py tests/test_mineru_merge.py tests/test_mineru_order.py tests/test_mineru_merge_diagnostic.py tests/test_mineru_pdf_split.py tests/test_mineru_pdf_split_jobs.py -q -ra -p no:cacheprovider
```

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

### 阶段 11A：Office 格式与页面语义

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

分片主链路仍待真实原件/云端验收。Windows保存句柄修复已按本轮单独授权执行8项隔离离线用例和51项相邻Mock回归；**未执行AES真实原件分片、云API或业务数据库操作**，不表示阶段32解析/评测通过。

### 范围、身份和限制

- Router依旧决定Native/Whole/Hybrid；仅最终需要MinerU的PDF超过200页或200 MB时进入分片。分片保守取十进制200,000,000字节，不放宽Provider原边界。完整Native不拆，普通云PDF与Office/图片流程不变。原上传上限默认200MiB（MAX_UPLOAD_SIZE_MB=200）；沿用项目1024²计量，和云端分片限制分别校验，不能借分片绕过上传/预检限制。
- Worker先为全部原页规划连续闭区间，优先尝试200页；实际生成文件大于200MB时有界二分缩小，每片最多10次尝试。接受前核对实际字节和SHA256，生成器保留原裁剪框/旋转/视口；任何单页仍超限均在第一个云POST之前停止。当前安全上限128片，原Router页数上限仍默认1000、可配置至2000；超限明确失败，不丢页。
- 原件只读，Document.doc_id/file_hash/file_name和官方QA完全不变；无子Document、无新表/迁移/环境变量。一个父Parse Job用现有JSONB的`_pdf_split_request`、`_pdf_split`保存版本/原Hash/总页数/每片原页范围、Hash、字节、状态和收费身份。
- 每片有稳定的data_id和逻辑task_id；后者不是Celery消息ID。父Job/task_id仍是当前原文档解析代次，父行batch_id/data_id/upload_complete表示**当前片**，历史片必须查账本，不把父Job行数当费用。
- 文件位于`PARSED_DIR/<doc_id>/pdf_split/<job_id>/part-0001.pdf`，并有manifest.json和同片结果指针。JSONB是收费状态真相，磁盘manifest仅为不含URL/正文的可核对副本；写副本失败记录安全事件，不据副本重建收费身份。仅允许服务自有目录，拒绝链接/越界，不覆盖不匹配的旧片。备份需包含整个PARSED_DIR及数据库。

### 短任务、缓存和原子性

每个短任务只推进当前片，复用原Provider：申请前提交意图，PUT前提交Batch，查询/下载错误沿用同Batch和原Redis共享限流。按实际每片一个文件预留额度；确认Batch的不同片计入`mineru_file_submit_count`，未知POST可能已计费，必须人工核对，不能当成确定零费用。

片结果先通过局部Guard，保存原ZIP/content_list/可选full.md/标准化块到内容缓存，再保存不可变代次指针和游标。SQL回滚可恢复同片指针，不另POST；完全相同片Hash、模型/参数/归一化/分片策略可跨原件复用。Force Reparse只绕过原件级成功入口，**仍有效的成功片不重复收费**；只有确实缺少结果且已明确允许新收费的片才提交。普通重试遇到收费历史但结果缺失安全停止。

所有片成功并核对原件Hash/分片Hash/页覆盖后，按原页排序且同页稳定保留Adapter原序，整体保留table/formula/text/bbox/source；Block ID绑定原doc_id、缓存身份和原位置，不含消息ID。映射公式：`原物理页 = page_start + 片内1-based页 - 1`；OHR原0-based证据页仅在评测侧+1，不改官方数据。不得全局按y排序或让临时局部页成为引用。

只在全片完成时发布**原件级完整缓存**，一次事务替换全部Block并推进CHUNKING；之后原管理任务接续Chunk→Embedding→Index，仍只有Index可置READY。任一片失败不发布部分Block；已有旧块保持但Document退出READY。Reindex只回放完整缓存，不重拆或发云请求。分片缓存/整份缓存都受现有容量限额保护。

活动消息丢失可用原入口或文档级Retry续同Job。显式Retry可恢复原已上传Batch的JOB_TIMEOUT/传输错误、仍有效签名链接的原Batch PUT，以及从未提交片的频控/缓存占用等待；保留成功片和网络预算，不重建收费身份。远端失败、SUBMIT_UNCERTAIN、坏ZIP/来源/缓存不自动复活，先核对平台、备份和原结果；确需新解析再由管理员确认Force费用。单片观察窗口与重试预算有限，成功推进到下一片才重置本片窗口，管理cloud窗口相应续开；不会自动复活已失败的管理任务。

### 用户手动验证（先Mock，真实执行另行确认）

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
