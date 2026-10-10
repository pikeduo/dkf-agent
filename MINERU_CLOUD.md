# MinerU Cloud Provider：接口边界与人工验收

本文件供开发与验收使用；部署说明和日常调用入口见 README，不把测试记录写入 README。

## 1. 官方契约与实现边界

- 官方协议依据：[精准解析 API 文档](https://mineru.net/apiManage/docs)。采用 V4 本地批量申请上传链接、签名 PUT、批次 GET；不使用轻量 Agent API。
- 结构化结果依据：官方 [ContentListV1 序列化源码](https://github.com/opendatalab/MinerU/blob/3e60291846cb7c3bf8fe7f4f16238f4fc6cce491/mineru/backend/pipeline/pipeline_middle_json_mkcontent.py)。云端实际格式仍须以真实 ZIP 验收；不能把 API V4 与开源模型版本或 ContentListV2 混为一谈。
- 默认 `vlm / ch / enable_table=true / enable_formula=true / is_ocr=true`，保存提交时参数快照。接口上传一份文件，Provider 支持按文件数计量的批量申请；不实现批量导入 API。
- 内存读取 ZIP 中唯一的 `content_list.json / *_content_list.json`，不持久化下载 ZIP、Markdown 或标准化结果文件，不实现 Parse Cache。未知 V2 / 类型安全失败，不能偷换成读取 full.md。
- ContentListV1 的 `page_idx` 转 1 基页码、归一化 bbox 转原件尺寸，保留源顺序和实际标题章节；表格 HTML 与公式文本原样作为证据，不执行 HTML / JavaScript / 公式。图片仅有路径而无文字时不伪造正文；无可用块时失败。
- `full.md` 可在供应商平台下载用于人工比对；当前没有 Admin 预览接口。

## 2. 持久化、幂等和恢复

`0004_mineru_jobs` 只新增任务表，不重建前九阶段业务表。`task_id` 是文档处理代次；官方批量接口由 batch_id / data_id 标识外部文件，不虚构外部单文件 task_id。

同一文档、Hash、供应商、模型与参数快照最多一条活动记录。文档行锁串行创建，部分唯一索引兜底。旧代次和终态消息跳过；结果写块、完成任务与推进 CHUNKING 同事务，任一步失败全部回滚，不生成 Chunk / Embedding / READY。

提交意图在 POST 前持久化；batch 和私有签名上传链接在 PUT 前持久化。上传网络失败或 Worker 重启沿用同一批次 PUT；链接成功使用后清除。若 POST 响应丢失或进程在响应持久化前退出，无法证明未创建远端批次，等待保护期限后记录 `SUBMIT_UNCERTAIN`，不自动再 POST。请先在供应商平台确认，再决定人工提交；此 API 没有已确认的幂等请求键，因此不声称绝对外部 exactly-once。

未完成仅执行一次检查，countdown 再投递。明确 429、临时服务 / 网络错误指数退避加 0～5 秒抖动，尊重 Retry-After，默认最多重试 5 次；总等待默认 7200 秒。远端明确 failed 且原因匹配已确认临时分类时可以重建 batch；未知 err_msg 不透传也不猜测可重试。

没有 Outbox / 自动补投器。已有任务通过显式 `POST .../documents/{doc_id}/mineru` 恢复；状态已完成或原生 PARSING 不允许借用它重新解析。更换参数不能覆盖活动任务。不对旧任务做后台扫描。

Redis 使用 TIME + Lua 原子滚动计数，默认安全系数 0.9，按实际文件数预留；失败调用预留量不返还，采取保守策略。日文件额度滚动 24 小时，不假定平台重置时区；优先页按 UTC 日粗略观测，超出不拒绝。所有同账户 Worker 共用 Redis 与命名空间；外部平台 / 其他应用消耗仍以平台为准。

## 3. 自动验证方式

以下测试不调用真实 MinerU；数据库用临时 schema + 外层回滚，Broker 与 Provider Mock，测试原件使用临时目录。

```powershell
python -m pytest tests/test_mineru_provider.py tests/mineru_db_smoke.py -q
```

真实 Redis Lua 测试先人工确认 `Get-Service Redis` 为 Running；测试只使用随机命名空间并清理自己的键，绝不 FLUSHDB：

```powershell
python -m pytest tests/mineru_redis_smoke.py -q
```

回归测试保持前九阶段接口、原生解析、迁移和事务语义。基础迁移夹具仅更新 head 与新增表清单，避免误把新增结构当旧阶段回归失败。

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

## 5. 当前验证边界

代码和 Mock 通过不代表真实 Token、账户额度、对象存储网络、云端当前 ZIP 契约或 OCR / 表格 / 公式质量已验证。实际 API 鉴权、申请、PUT、异步状态、ZIP 下载以及四类样本效果，必须完成上述人工验收再确认。

未实现 Parser Router、Parse Cache、Admin 解析预览、Chunk、Embedding、RAG、公式安全计算或自动 Token 续期；本阶段验收后停止，等待用户确认阶段 11。
