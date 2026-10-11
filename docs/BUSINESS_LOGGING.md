# 业务日志说明

## 范围与安全

现有 FastAPI、上传与 Celery 解析链路采用 Python 标准库业务日志，不增加依赖、不改变 API 请求体、任务参数、状态机、重试次数、限流或落库约束。启动和 `LOG_*` 配置见 [README](../README.md#运行日志)。

每行 JSON 包含 UTC 时间、级别、事件名、进程号，以及事件适用的白名单字段。HTTP 使用路由模板，不写用户原始路径、查询串、请求头或正文。响应通常含服务生成的 `X-Request-ID`，不信任客户端提供的同名头；框架兜底未处理异常的 500 响应可能没有该头。Celery 投递头携带请求编号，连续的短任务和重试可关联；旧消息和命令行直接提交的任务没有 HTTP 请求编号。进程崩溃、队列确认丢失、磁盘故障等情况下不能保证每个事件都记录，这是排障信息，不是强一致审计或恢复机制。

不输出任意请求/响应对象、异常堆栈、数据库连接串、Token、签名 URL、文档正文或上游错误原文。业务 Logger 独立于第三方 Logger，配置 DEBUG 也不会启用 httpx 请求详情。现有 Uvicorn/Celery 自带终端输出仍由各框架管理，不宣称它们经过此白名单格式器清洗；请勿自行开启带敏感信息的第三方调试日志。

文件名、资源 ID 也可能属于敏感业务元信息，需要限制日志目录访问。每个进程独立文件和备份避免多进程轮转竞争；父进程及 prefork 子进程初始化时分别配置。重启的历史文件不自动清理，运维应在确认对应进程停止后归档或删除。目录/写盘失败降级终端并安全提示，不改变已提交业务结果；不保证终端与文件双重故障时仍可记录。

## 主要事件

| 事件 | 含义 |
|---|---|
| `http.request` | 请求结束：方法、路由模板、HTTP 状态、耗时、关联 ID；不是响应正文 |
| `upload.received / rejected / duplicate` | 上传进入服务、被校验拒绝或检测到同库重复；未到服务的 multipart 错误仅有 HTTP 记录 |
| `upload.stored` | 原件和 Document 已提交，包含文件名、格式、实际字节数、doc_id/kb_id/task_id；不代表任务执行成功 |
| `upload.failed` | 磁盘、数据库或提交结果不确定等安全分类 |
| `upload.constraint_conflict / cleanup_failed` | 数据库唯一约束冲突、失败上传的文件补偿清理失败；需结合最终 HTTP 状态及数据库核对 |
| `upload.batch_rejected / batch_started` | 批次数量拒绝/预检后开始逐文件处理，包含 `file_count`；知识库或配置预检失败仅有 HTTP 请求日志 |
| `upload.batch_item / batch_finished` | 逐文件的输入序号、状态码、已知 doc_id、耗时；最终的总数、接收/失败数量和批次耗时，不记录结果正文 |
| `document.dispatched / dispatch_failed` | 原生消息投递确认/失败；确认成功不代表 Worker 已消费 |
| `task.started / finished` | Celery 本次执行及状态/耗时；云端短任务 SUCCESS 不代表整份文档完成 |
| `parse.started / validated` | 原生 Parser 开始/统一输出校验通过；仍可能写库失败 |
| `parse.persisted / skipped` | 原生事务已提交/当前消息无需处理；成功附 Block 数量、持久化状态 |
| `parse.retry / failed / error_record_failed` | 原生退避重试、终止失败、错误记录未能写库；日志不输出异常原文 |
| `mineru.dispatched / dispatch_failed / step_started / step_finished` | 云端短步骤投递及执行摘要 |
| `mineru.http_started / http_response / http_failed` | 官方 API 请求摘要；PUT/ZIP 下载使用阶段名称而非签名 URL，HTTP 成功不代表业务 envelope 成功 |
| `mineru.batch_created / upload_complete / remote_state` | 已提交批次检查点、上传完成检查点、收到供应商单文件状态；远端 done 仍可能下载或落库失败 |
| `mineru.result_downloaded / adapter_validated / blocks_persisted` | ZIP 下载完成、统一结构校验通过、Block 与任务/文档状态已事务提交 |
| `mineru.retry / failed / error_record_failed` | 云端错误分类、退避与终止；错误无法持久化时的安全提示 |

`parse.persisted / mineru.blocks_persisted` 只表示代码校验与事务成功，不保证 OCR 全量识别、公式语义或任意复杂布局顺序正确，不代表 READY。真实正文、页码与 bbox 仍需查询 `document_blocks` 并对照原件。

## 自动测试

离线日志测试覆盖 JSON 白名单、敏感字段不输出、控制字符处理、并发请求隔离、HTTP 422/404/未处理异常、任务头关联及上下文恢复、轮转、重复配置、分进程文件与写盘故障降级：

```powershell
python -m pytest tests/test_business_logging.py -q
```

数据库测试复用现有临时 schema 与事务回滚夹具，Mock MinerU/Broker，不消耗真实 API 额度、不向 public 写业务数据：

```powershell
python -m pytest tests/business_logging_smoke.py tests/batch_upload_smoke.py -q
```

同时执行现有 Parser、接口、任务、迁移及 MinerU 回归测试。自动测试不会证明真实 Windows Worker 重启、Linux prefork 或真实 MinerU 网络链路已验收。

## 人工验收

1. 保持 `LOG_LEVEL=INFO`，重启 API 和所有消费相同队列的 CPU Worker（启用 GPU Worker 时同样更新），确认 `logs/` 生成分别带当前进程号的业务文件，没有改变原启动命令。旧 Worker 仍可能消费新消息而不产生新增日志，不能只新开一个 Worker 就认为已全部升级。
2. 打开 `/health` 和 `/docs`，确认接口可用；在浏览器开发者工具查看响应 `X-Request-ID`，用该编号查询日志，看到方法、路由、状态码和耗时。不要在 URL 中传真实密钥。
3. 用验收知识库上传一份新 TXT/CSV，确认出现 `upload.stored`、`document.dispatched`、`parse.started`、`parse.validated`、`parse.persisted`，按 doc_id/request_id 对照接口与 Worker。核对文件名、字节数、Block 数量；文档详情为 CHUNKING，数据库正文正确。
4. 同库再次上传相同内容，确认 409 和 `upload.duplicate`，且没有第二次成功上传/重复生成 Block。非法扩展名或空文件应有拒绝事件/HTTP 失败记录。
5. 手动调用显式 MinerU 上传，用少量真实图片或 PDF 验证批次、上传、远端状态、ZIP、Adapter 和落库事件；按 job_id/doc_id 关联。必须对照 `document_blocks` 与原件，Mock 通过不能替代真实 API 验证。
6. 查询一份已有失败任务的详情与安全错误码日志；若要验证重试，使用专门验收环境的可控临时错误，不为了测试中断共用 PostgreSQL/Redis 服务。失败日志不能出现落库成功事件。
7. 检查业务文件不含 Token、密码、签名链接或完整正文。不要把真实密钥复制到搜索命令、终端输出或提交记录中。轮转在自动测试中已使用小容量验证；真实持续运行时检查备份和目录容量并安排历史文件归档。
8. 强制刷新 Swagger，在原 `/documents` 入口点击 `Try it out`，确认 `file` 只有一个文件控件、无 `Add string item`。打开选择窗口，用 Ctrl/Shift 一次选两份新 TXT/MD，以及一份重复文件和空文件；选择后应显示数量、名称/大小初检，尚无上传或落库日志。点击 Execute 后核对逐项 201/409/400 与汇总数量，不使用 `/batch` 路径。用响应 `X-Request-ID` 关联 `upload.batch_started / batch_item / batch_finished`；每个成功项独立 doc_id/task_id，后续 Worker 各有解析日志。原显式 `/mineru/documents` 同样多选少量新图片/PDF 验证各自 parse_job 与真实落库，不能以批次 200 当作解析成功。只选一份继续走原上传事件与单文件响应。界面初检不读取或上传文件正文，最终校验与安全日志仍由现有后端负责，不把仅打开文件窗口记成上传成功。

真实 API 请求会消耗额度；本次日志功能不自动发起真实 MinerU 调用、不安装基础服务、不开发 Router、Cache、Chunk、Embedding 或 RAG。
