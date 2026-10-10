# MinerU Cloud Provider：接口边界与人工验收

本文件供开发与验收使用；部署说明和日常调用入口见 [README](../README.md)，开发路线见 [最新开发计划](DEVELOPMENT_PLAN_CODEX.md)，不把测试记录写入 README。

## 1. 官方契约与实现边界

- 官方协议依据：[精准解析 API 文档](https://mineru.net/apiManage/docs)。采用 V4 本地批量申请上传链接、签名 PUT、批次 GET；不使用轻量 Agent API。
- 生产 HTTP 客户端固定 `trust_env=False`，API、签名上传、结果下载共用直连策略，不发现环境 / Windows 系统代理，不自动回退代理。保持默认 HTTPS 证书校验；不读取 `SSL_CERT_FILE / SSL_CERT_DIR`，不修改系统代理或 VPN / TUN 路由。不新增环境变量。
- 结构化结果依据：官方 [ContentListV1 序列化源码](https://github.com/opendatalab/MinerU/blob/3e60291846cb7c3bf8fe7f4f16238f4fc6cce491/mineru/backend/pipeline/pipeline_middle_json_mkcontent.py)。云端实际格式仍须以真实 ZIP 验收；不能把 API V4 与开源模型版本或 ContentListV2 混为一谈。
- 默认 `vlm / ch / enable_table=true / enable_formula=true / is_ocr=true`，保存提交时参数快照。接口上传一份文件，Provider 支持按文件数计量的批量申请；不实现批量导入 API。
- 内存读取 ZIP 中唯一的 `content_list.json / *_content_list.json`，不持久化下载 ZIP、Markdown 或标准化结果文件，不实现 Parse Cache。未知 V2 / 类型安全失败，不能偷换成读取 full.md。
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

现有真实扫描 ZIP 在内存重新转换后，两页都成为顶部页眉 → 正文 → 页底，块数仍为 17，ID 与原始顺序转换一致；图片、表格、公式分别转换为 12、5、16 块，原文和结构没有改写。这是已有结果的 Adapter 核对，不等于新 Worker 已完成真实上传和落库验收，也没有修改历史数据。

## 2. 持久化、幂等和恢复

`0004_mineru_jobs` 只新增任务表，不重建前九阶段业务表。`task_id` 是文档处理代次；官方批量接口由 batch_id / data_id 标识外部文件，不虚构外部单文件 task_id。

同一文档、Hash、供应商、模型与参数快照最多一条活动记录。文档行锁串行创建，部分唯一索引兜底。旧代次和终态消息跳过；结果写块、完成任务与推进 CHUNKING 同事务，任一步失败全部回滚，不生成 Chunk / Embedding / READY。

提交意图在 POST 前持久化；batch 和私有签名上传链接在 PUT 前持久化。上传网络失败或 Worker 重启沿用同一批次 PUT；链接成功使用后清除。若 POST 响应丢失或进程在响应持久化前退出，无法证明未创建远端批次，等待保护期限后记录 `SUBMIT_UNCERTAIN`，不自动再 POST。请先在供应商平台确认，再决定人工提交；此 API 没有已确认的幂等请求键，因此不声称绝对外部 exactly-once。

未完成仅执行一次检查，countdown 再投递。明确 429、临时服务 / 网络错误指数退避加 0～5 秒抖动，尊重 Retry-After，默认最多重试 5 次；总等待默认 7200 秒。远端明确 failed 且原因匹配已确认临时分类时可以重建 batch；未知 err_msg 不透传也不猜测可重试。

没有 Outbox / 自动补投器。同代次的活动任务可通过显式 `POST .../documents/{doc_id}/mineru` 恢复投递，继续沿用已保存的 batch。任务已经失败且非活动时，该入口会建立新任务，可能重新申请云端批次、上传并消耗额度；它不是旧失败批次的“仅重新下载”入口。状态已完成或原生 PARSING 不允许借用它重新解析。更换参数不能覆盖活动任务。不对旧任务做后台扫描。

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

回归测试保持前九阶段接口、原生解析、迁移和事务语义。基础迁移夹具仅更新 head 与新增表清单，避免误把新增结构当旧阶段回归失败。

顺序测试覆盖单页、两页及交错页不跨页、同 y 稳定顺序、表格/公式整体保留、空图片不补 OCR、无 bbox、多栏/纵向回跳、未知/歧义 layout、稳定 ID；数据库测试覆盖新顺序事务落库、重复消息不增块，以及图片查询网络重试耗尽后必须显式建立新任务。

## 4. 真实 API 人工验收（必须由你执行）

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
python -m celery -A app.core.celery_app:celery_app worker -Q default_queue --pool=threads --concurrency=2 --hostname=cpu@%h -l info
```

预期迁移为 `0004_mineru_jobs (head)`；Worker 注册两个 MinerU 任务，并消费 default_queue。GPU Worker 不必启动。

### 四类真实样本

准备允许外传、每份少于 200 页且小于当前上传上限的样本：

1. 一张 JPG / PNG，包含清楚可辨认的文字。
2. 一份纯扫描 PDF，最好不少于两页，以检查真实物理页码。
3. 一份含表格 PDF，能人工核对表头、单元格、标题与脚注。
4. 一份含数学公式 PDF，能人工核对 LaTeX 与相邻说明。

在 `http://127.0.0.1:8000/docs` 创建一个新的验收知识库，避免与已有 Hash 冲突。每次通过 `POST /api/admin/knowledge-bases/{kb_id}/mineru/documents` 上传一份，字段为 multipart `file`；不要用旧 `/documents` 入口期待自动路由。记录返回的 doc_id / job_id。

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
7. **异常**：可用一个无效 Token 在独立测试环境验证安全失败，再恢复有效 Token；确认前端、Worker 日志和 Celery 结果无 Token / 签名 URL。空文件、损坏原件、超 200 页应在提交前拒绝，不消耗 API 额度。429、临时失败、日额度和 Worker 重启主要由 Mock / 共享限流测试覆盖，不向平台发压测来耗尽真实额度。
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

本次同 Hash 图片存在两份 Document：旧知识库的原生 Parser 明确拒绝图片且没有 parse job；云端验收库的图片 job 为 failed、upload_complete=true、batch_id 已保存、retry_count=5、error_code=NETWORK、is_active=false，块数为 0。只读查询原有云批次为 done，结果 ZIP 当前能下载，使用原件 1600×1100 像素 geometry 转换成功且有 12 个块；没有发现 file_type、geometry 或 content_list 类型导致 Adapter 拒绝。历史失败码只定位到网络连接阶段，不能据此确定最后一次失败究竟在查询 API 还是 ZIP 下载；本次没有重新申请或上传。

API 检查（把 UUID 替换为上面 SQL 查出的云端记录）：

```powershell
$taskKbId = "替换为知识库UUID"
$taskDocId = "替换为文档UUID"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId"
curl.exe "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId/parse-jobs"
```

网络恢复不会自动复活该终态任务。先在平台核对旧批次和 ZIP，若你允许新建批次并消耗额度，再对同一个 Document 显式提交（无需重复上传原件）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/admin/knowledge-bases/$taskKbId/documents/$taskDocId/mineru"
```

返回 202、新 job_id；完成后该 job=done、Document=CHUNKING 且 block_count>0。它可能重新申请、PUT 并解析，不是复用旧 ZIP；若收到 409，先查询当前状态，不直接改数据库状态或删除旧记录。

### 修复后的真实落库复验与导出

重启 API 和 CPU Worker。已 CHUNKING 的扫描件不会自动重排，现有接口也不支持完成文档 Reparse。请在新的验收知识库上传扫描 PDF（建议同库再次覆盖四类样本），或对原 FAILED 图片使用上述显式入口；真实提交会消耗云端额度。使用显式 `/mineru/documents`，不要误用原生 `/documents`。

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

## 5. 当前验证边界

已有四类真实 ZIP 已核对；扫描、表格、公式已经有真实数据库结果，图片仍为历史失败且未落库。本次顺序修复已通过已有真实 ZIP 的内存转换检查，未代替人工重启 Worker 后的新上传到落库复验。仍须人工确认图片成功入库、两页新 block_index、结构质量以及新运行代次的端到端链路。Mock 通过不等于真实 429、所有网络故障、多栏布局或 OCR 质量全部验证，不能宣称所有验收项已完成。

未实现 Parser Router、Parse Cache、Admin 解析预览、Chunk、Embedding、RAG、公式安全计算或自动 Token 续期；本阶段验收后停止，等待用户确认阶段 11。
