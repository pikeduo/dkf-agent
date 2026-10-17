# 业务日志说明

## 范围与安全

现有 FastAPI、上传与 Celery 解析链路采用 Python 标准库业务日志，不增加依赖、不改变 API 请求体、任务参数、状态机、重试次数、限流或落库约束。启动和 `LOG_*` 配置见 [README](../README.md#运行日志)。

上述限制指日志设施自身；11B.1 的可选路由接口和任务约定见 [Router 说明](PARSER_ROUTER.md)。路由继续复用每日合并文件，不新增分散的日志。

每行使用 UTF-8 中文可读文本，固定前缀为“本机时间（含毫秒及 UTC 偏移） | 级别 | 注册进程角色 | 中文事件说明 | event=事件码 | pid=进程号”，后接适用的白名单字段。关联编号保留 `request_id / doc_id / kb_id / task_id / job_id / batch_id` 原名，其他摘要按固定顺序使用中文标签，例如 `文件 / 接口 / HTTP状态 / 状态 / Block数 / 耗时_ms / 错误码`。未知合法事件显示“业务事件”并保留事件码；非法事件显示 unknown，不格式化任意消息参数或异常堆栈。字段标量独立引用，字符串清理控制字符、限制长度并转义竖线，不能伪造新行或分隔字段；不是整行 JSON，不在行尾附加 JSON 对象。非有限浮点数舍弃，不因此丢掉事件。

### 进程角色

唯一集中映射为`app/core/business_logging.py`的只读`ROLE_LABELS`。所有入口显式配置与用途一致的role，不改操作系统进程名称/PID，不接受请求/业务字段覆盖；未知或非字符串角色仅显示固定“未知进程”，任意外部角色文本不进入日志。新增role同次更新注册表、入口、测试和必要文档，不伪装为API/Worker。

| role | 显示名称 |
|---|---|
| api | API |
| worker | Worker |
| dense-cli | Dense CLI |
| bm25-cli | BM25 CLI |
| hybrid-cli | Hybrid CLI |
| rerank-cli | Reranker CLI |
| rag-cli | RAG CLI |
| evidence-cli | Evidence CLI |
| formula-cli | Formula CLI |
| formula-binding-cli | Formula Binding CLI |
| formula-calculation-cli | Formula Calculation CLI |
| knowledge-agent-cli | Knowledge Agent CLI |
| retrieval-acceptance | 检索验收 |
| preset-cli | Preset CLI |
| mineru-diagnostic-cli | MinerU Diagnostic CLI |
| unknown / 未注册角色 | 未知进程 |

API和Celery Worker入口保持原role；四类检索/重排CLI使用各自角色；预置导入使用preset-cli；联合冒烟的检索步骤使用retrieval-acceptance。角色变化只改固定前缀中的显示名称，白名单摘要、脱敏、真实PID、每日合并、上下文和异常降级不变。更新代码后用户重启相应进程，旧日志不改写。

所有进程按事件发生时的本机日期写入同一 `knowledge-service-YYYY-MM-DD.log`，跨日切换目标文件，不重命名/覆盖旧文件，重启不会再增加 PID 文件。时间与文件日期使用同一本机时区。HTTP 使用路由模板，不写用户原始路径、查询串、请求头或正文。响应通常含服务生成的 `X-Request-ID`，不信任客户端提供的同名头；框架兜底未处理异常的 500 响应可能没有该头。Celery 投递头携带请求编号，连续的短任务和重试可关联；旧消息和命令行直接提交的任务没有 HTTP 请求编号。进程崩溃、队列确认丢失、磁盘故障等情况下不能保证每个事件都记录，这是排障信息，不是强一致审计或恢复机制。

不输出任意请求/响应对象、异常堆栈、数据库连接串、Token、签名 URL、文档正文或上游错误原文。业务 Logger 独立于第三方 Logger，配置 DEBUG 也不会启用 httpx 请求详情。现有 Uvicorn/Celery 自带终端输出仍由各框架管理，不宣称它们经过此白名单格式器清洗；请勿自行开启带敏感信息的第三方调试日志。

文件名、资源 ID 也可能属于敏感业务元信息，需要限制日志目录访问。每日文件只追加不按大小轮转，不使用普通多进程 RotatingFileHandler。标准库 OS 锁保护整行中文文本写入：Windows 使用 [msvcrt.locking](https://docs.python.org/3.11/library/msvcrt.html#msvcrt.locking)，Unix 使用 [fcntl.flock](https://docs.python.org/3.11/library/fcntl.html#fcntl.flock)，线程内仍由 Handler 的锁串行化；锁等待最多 1 秒。固定辅助文件 `.knowledge-service.lock` 不含正文，不随 PID 或日期增加；不能在运行中删除/替换。每条记录独立打开并最终关闭锁描述符与数据文件，prefork 子进程仍重建处理器，避免继承活跃句柄。进程退出由 OS 释放锁，不依赖 PID 清理；自动测试验证正常并发完整行，但磁盘写入中断、电源故障仍可能丢失/留下不完整记录。

所有写入者必须运行新代码并使用相同的本地 `LOG_DIR`；格式切换后需要重启 API 与所有 Worker，遗留进程可能继续输出 JSON。已有 JSON 历史行不改写、不删除，切换当天同一文件可能同时包含旧 JSON 和新中文文本。旧格式的整行 JSON 读取工具需调整，人工搜索仍可使用事件码或资源 UUID。网络共享盘、多主机锁行为不在保证范围内。旧容量/备份配置废弃，当前无单日容量上限或自动历史清理。旧 PID 文件、已有日期文件均不自动合并/删除，运维应在对应进程停止后自行归档。目录/写盘/锁失败降级终端并安全提示，不改变已提交业务结果；不保证终端与文件双重故障时仍可记录。

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
| `management.requested / dispatched / advanced / rejected` | 上传ingest或显式维护检查点已提交/管理消息投递/下一阶段投递/活动任务拒绝；仅UUID、操作、阶段及固定原因，无配置、路径或正文 |
| `management.finished / deleted / failed / dispatch_failed / error_record_failed` | 管理结束（以Document为准）/自有文件及数据库删除均完成/失败与投递或记录失败；不会把部分文件清理当删除成功 |
| `task.started / finished` | Celery 本次执行及状态/耗时；云端短任务 SUCCESS 不代表整份文档完成 |
| `parse.started / validated` | 原生 Parser 开始/统一输出校验通过；仍可能写库失败 |
| `parse.persisted / skipped` | 原生事务已提交/当前消息无需处理；成功附 Block 数量、持久化状态 |
| `parse.retry / failed / error_record_failed` | 原生退避重试、终止失败、错误记录未能写库；日志不输出异常原文 |
| `mineru.dispatched / dispatch_failed / step_started / step_finished` | 云端短步骤投递及执行摘要 |
| `mineru.http_started / http_response / http_failed` | 官方 API 请求摘要；PUT/ZIP 下载使用阶段名称而非签名 URL，HTTP 成功不代表业务 envelope 成功 |
| `mineru.batch_created / upload_complete / remote_state` | 已提交批次检查点、上传完成检查点、收到供应商单文件状态；远端 done 仍可能下载或落库失败 |
| `mineru.result_downloaded / adapter_validated / blocks_persisted` | ZIP 下载完成、统一结构校验通过、Block 与任务/文档状态已事务提交 |
| `mineru.split_planned / split_piece_done / split_manifest_failed / split_checkpoint_invalid` | 超限PDF计划已提交/成功片缓存/磁盘副本失败/坏检查点安全停止；仅UUID、数量与安全码。实际费用按片Batch去重，父Job行数不是云文件数，提交不确定须平台核对 |
| `mineru.split_io_failed` | 分片本地I/O失败；仅安全错误码、异常类型和固定stage/step源码位置，不记录异常原文/文件路径。保存/发布错误为PDF_SPLIT_WRITE，读取/生成/校验错误为PDF_SPLIT_IO |
| `mineru.merge_restored / merge_rejected` | 无损恢复/安全拒绝；对外仍MERGE_EVIDENCE，内部step固定子码、stage固定项目函数/结构角色，附原数组item_index、局部1-based page、受限数量和异常类型；不输出正文/bbox明细，恢复不等于落库 |
| `mineru.table_merge_restored` | V1跨页表格/附注已按严格来源核对切回原页；仅doc_id、数量和阶段，未发布缓存/落库 |
| `mineru.empty_block` | 未解释的空正文/表格；step固定为TABLE_NO_BODY或STRUCTURE_NO_TEXT，page/item_index均为1-based，structure_type固定枚举，不记录HTML/图片路径/正文 |
| `mineru.retry / failed / error_record_failed` | 云端错误分类、退避与终止；错误无法持久化时的安全提示 |
| `router.decided / failed / fallback` | 固定策略、模式、版本、理由码、结构计数及一次完整性回退；完整结构快照在任务结果，不记录正文 |
| `router.guard_passed / guard_failed` | 落库前结构覆盖检查，不代表语义真值、OCR 全识别或事务已提交 |
| `router.cloud_submitted` | 已确认源文件 batch，云文件提交数=1；重复日志需按 job_id/batch_id 去重 |
| `router.cloud_dispatch_failed / finished` | 云交接确认失败/路由任务结束，不等于最终解析完成 |
| `hybrid.prepared` | XLSX 主结果/候选/单 Bundle 已准备，记录候选/过滤/复用/页数/Chart 数，不代表 Block 落库 |
| `hybrid.preflight_rejected` | 固定拒绝码与提交前 Whole/FAILED 选择，不输出 XML/路径/正文 |
| `xlsx.primary_validated / primary_persisted` | Whole XLSX 的 Native 公式主结果校验 / 与云块事务提交，不能以校验事件当作落库 |
| `hybrid.started` | 开始 XLSX 主解析及候选检查；失败继续使用 parse/mineru 的安全错误事件 |
| `hybrid.persisted` | 全量 Hybrid Block 已事务提交；不记录正文、Anchor/manifest、签名 URL，仍在每日同一个文件 |
| `cache.hit / miss / stored / busy` | 缓存命中/未命中/完整结果已存盘/同内容活动任务拒绝重复；stored 不代表 SQL 已提交 |
| `cache.persisted / maintenance_requested` | 缓存已事务回放 / 显式 Reindex或Force Reparse 请求；固定操作、缓存键、命中和数量摘要 |
| `chunk.dispatched / dispatch_failed / rejected` | 独立切片投递、确认失败及状态/检查点拒绝，不记录正文或配置原文 |
| `chunk.started / persisted / skipped` | 按 Block 原序开始切片、事务提交成功及旧/已完成消息跳过；数量、估算长度参数、超目标数和耗时 |
| `chunk.retry / failed / error_record_failed` | 临时数据库有限重试、安全终止及错误未能写库，不输出异常原文 |
| `embedding.model_loaded / encoded / encode_failed / encode_skipped` | 模型加载/统一编码/安全错误/空输入跳过；模型名、设备、维度、数量和耗时，不记录正文或向量 |
| `embedding.dispatched / dispatch_failed / rejected / started` | GPU 任务投递/确认失败/拒绝/开始，独立 task_id 和 doc_id 关联 |
| `embedding.batch_completed / persisted / skipped` | 批次暂存/完整文件与 SQL 状态提交/旧消息跳过；暂存不等于完整发布，INDEXING 只表示等待入库 |
| `embedding.retry / failed / error_record_failed` | 数据库有限重试/安全终止/错误无法落库；不透传模型/CUDA/路径原始异常 |
| `index.dispatched / dispatch_failed / rejected / started` | 独立 CPU 入库任务投递、确认失败、状态拒绝及开始，复用 doc_id/task_id 关联 |
| `index.batch_written / persisted / skipped` | 批次仅执行未提交 / 所有向量与 READY 事务提交 / 旧或已完成消息跳过；不记录向量/正文/文件路径 |
| `index.retry / failed / error_record_failed` | 数据库有限退避、安全终止和失败原因无法写库；原始 SQL/连接错误不输出 |
| `rag.node_converted / result_converted` | 已有 Chunk/Node/内部结果互转，仅资源 UUID 和耗时；不代表检索、推理或落库 |
| `rag.query_encoded / nodes_reused` | Query 委托既有共享服务 / 原 Node 向量复用；仅维度/数量/耗时，不输出输入或向量 |
| `rag.adapter_failed` | 固定适配边界错误码与步骤，不透传 Pydantic 输入、路径、Node 或模型异常 |
| `dense.started / finished / empty / failed` | 指定知识库/可选文档只读检索开始/成功/空集/安全失败；kb_id、top_k、指定文档数量及单文档doc_id、结果数量/维度/步骤/耗时/固定错误码，不记录ID数组、Query/Node/向量或SQL |
| `bm25.started / index_built / index_reused / finished / empty / failed` | 当前READY快照的关键词检索、派生索引构建/复用及结果摘要；仅UUID/范围数量/Chunk数/复用/耗时/安全码，不记录Query/词项/正文/索引摘要或SQL，批量Node转换不刷逐项成功日志 |
| `fusion.started / branch_finished / finished / empty / failed` | 同范围两路检索及稳定RRF摘要；stage区分dense/bm25/rrf，记录UUID/范围数量/结果数/耗时及安全分支错误码，不记录Query/Node/原分数/异常原文，不新增日志文件 |
| `rerank.model_loaded / scored / score_failed / started / finished / skipped / failed` | 项目共享模型加载、完整原文对批量评分、Top Evidence选择及错误/空候选摘要；仅模型名/设备/数量/批次/知识库/耗时/固定安全码，加载及评分不是质量验收，不记录Query/正文/分数/路径/指纹 |
| `llm.started / finished / failed` | 单次所选LLM请求/完整响应接收/安全失败；provider只允许deepseek/custom，模型名、HTTP状态、耗时/错误码；不记录URL/问题/Prompt/响应/Key，finished不是引用或忠实度验收。旧deepseek事件标签保留兼容，不重复发两套事件 |
| `llm.fallback_started / finished / failed`、`llm.call_finished / failed` | 显式双方授权下唯一custom→deepseek运输故障切换与逻辑终态；复用provider/model_name/error_code/status_code/fallback_count/duration_ms，主失败事件保留主模型，call_finished表示实际返回方。备用失败即止；超时可能已计费。无新增文件或原文日志，业务引用/公式仍独立校验 |
| `rag.started / retrieved / synthesis_started / synthesized / insufficient / finished / failed` | 同范围问答、融合候选、最终证据校验/单次合成、引用校验/不足及完成/失败；仅kb_id、数量、阶段、状态、耗时/错误码，不记录答案/正文，不追加日志文件 |
| `citation.created` | 已从原DTO复制运行时引用；不代表答案已发布、当前数据库已核对或事实语义正确，不记录引用正文/分数/metadata |
| `evidence.started / finished / skipped / failed` | 显式只读来源核对；仅UUID/引用及唯一Block数量/耗时/固定错误码。finished表示同快照字符/Hash/页语义一致，不代表回答忠实；不足答案无引用时skipped不查库 |
| `formula.started / synthesis_started / finished / insufficient / unsupported / failed` | 指定公式的来源核对、单次所选LLM结构化、结构完成/不足/不支持/安全失败；仅UUID、数量、状态、耗时/固定错误码，不记录原式、表达式、上下文、含义或模型输出，finished不表示语义/计算验收 |
| `formula_binding.started / finished / need_variables / failed` | 已有定义与参数当前来源核对、绑定/缺参/安全失败；仅UUID、数量、状态、耗时/固定错误码，不记录变量名/数值/单位/引文，不请求LLM或计算 |
| `formula_calculation.started / finished / need_variables / failed` | 当前公式/参数复核、精确计算完成/缺参/安全失败；仅UUID、数量、状态、耗时/固定错误码，不记录原式/变量名/数值/单位/步骤/结果/正文。finished仅代表机械算术，不证明语义或单位正确 |
| `agent.started / routed / step / tool_started / tool_finished / tool_failed / tool_skipped / finished` | 单次有限图及原业务Tool；agent_run_id关联子服务，selected_route/tool_name/final_status只接受固定枚举，step_count有界、tool_duration_ms有限非负。不记录State/问题/正文/答案/公式/参数，不启用远端Trace或新增日志文件；tool_finished不是最终业务验收通过 |
| `knowledge.started / finished / failed / rejected` | API/服务的search或query过程与结果摘要：仅kb/doc UUID、候选上限/范围数量、返回数量/状态/耗时/安全码。rejected仅输入/输出校验或兜底错误；不记录问题、原响应、metadata或异常原文，复用api角色及每日单文件 |
| `preset.started / imported / reused` | 用户执行 CLI 后开始批量导入 / 原件已提交并投递 auto / 同内容沿用已有 doc_id，不代表 READY |
| `preset.advanced / state` | 下一阶段检查点提交后投递 / 业务状态变化，复用原队列与每日日志 |
| `preset.failed / timeout / finished` | 单项安全错误 / 有界等待超时但不取消任务 / 全批 READY 或未完成数量，不输出清单、路径或正文 |

`parse.persisted / mineru.blocks_persisted` 只表示代码校验与事务成功，不保证 OCR 全量识别、公式语义或任意复杂布局顺序正确，不代表 READY。真实正文、页码与 bbox 仍需查询 `document_blocks` 并对照原件。

公式服务沿用同一每日日志，集中注册`formula-cli`；新增`block_id`与原doc_id/chunk_id一样只允许UUID。API/Worker格式不变，不创建公式专用文件；外传许可在来源查询之前核对，失败不自动重试。变量数量沿用item_count摘要，不记录变量名或含义。

RAG拒答诊断：`rag.insufficient`的stage为`empty_final_evidence`时chunk_count=0，未调用DeepSeek；为`model_judged_insufficient`时记录本次非空最终证据数量，表示模型返回了有效的不足JSON。`rag.synthesis_started`在最终Node/上下文预算通过后、单次Chat前记录；`supported_claims`表示引用绑定通过而非事实准确率通过。只有数量/阶段，不能从日志推断哪段原文支持答案，也不得为排障放开Prompt/模型响应正文记录。

## 自动测试

离线日志测试覆盖中文可读格式、固定字段顺序、本机时区与毫秒、白名单、敏感字段不输出、控制字符/分隔符转义、非法事件与非有限数值、历史 JSON 不改写、并发请求隔离、HTTP 422/404/未处理异常、任务头关联及上下文恢复、同日合并、跨年日期切换、重复配置不创建空文件、真实多进程/多线程完整行、进程退出释放锁、锁超时与写盘故障降级。Linux 下额外覆盖 fork，Windows 当前覆盖 spawn：

```powershell
python -m pytest tests/test_business_logging.py -q
```

数据库测试复用现有临时 schema 与事务回滚夹具，Mock MinerU/Broker，不消耗真实 API 额度、不向 public 写业务数据：

```powershell
python -m pytest tests/business_logging_smoke.py tests/batch_upload_smoke.py -q
python -m pytest tests/chunk_db_smoke.py tests/test_business_logging.py -q
python -m pytest tests/test_embedding_service.py tests/test_embedding_artifacts.py tests/embedding_db_smoke.py -q
```

同时执行现有 Parser、接口、任务、迁移及 MinerU 回归测试。自动测试不会证明真实 Windows Worker 重启、Linux prefork 或真实 MinerU 网络链路已验收。

## 人工验收

1. 保持 `LOG_LEVEL=INFO`，重启 API 和所有消费相同队列的 CPU Worker（启用 GPU Worker 时同样更新），并确认它们使用同一 `LOG_DIR`。调用 `/health` 后应生成当天唯一的 `knowledge-service-YYYY-MM-DD.log`，用 `Get-Content -Encoding UTF8` 查看新增中文摘要、API 来源、event 和 pid，不再生成新 PID 文件；重启后再次调用，文件名不变且追加记录。历史 JSON 行保留是预期行为。固定 `.knowledge-service.lock` 是预期辅助文件，不要删除。原启动命令不变；旧 Worker 仍可能消费新消息并输出旧格式，不能只新开一个 Worker 就认为已全部升级。
2. 打开 `/health` 和 `/docs`，确认接口可用；在浏览器开发者工具查看响应 `X-Request-ID`，用该编号查询日志，看到方法、路由、状态码和耗时。不要在 URL 中传真实密钥。
3. 用验收知识库上传一份新 TXT/CSV，确认出现 `upload.stored`、`document.dispatched`、`parse.started`、`parse.validated`、`parse.persisted`，按 doc_id/request_id 对照接口与 Worker。核对文件名、字节数、Block 数量；文档详情为 CHUNKING，数据库正文正确。
4. 同库再次上传相同内容，确认 409 和 `upload.duplicate`，且没有第二次成功上传/重复生成 Block。非法扩展名或空文件应有拒绝事件/HTTP 失败记录。
5. 手动调用显式 MinerU 上传，用少量真实图片或 PDF 验证批次、上传、远端状态、ZIP、Adapter 和落库事件；按 job_id/doc_id 关联。必须对照 `document_blocks` 与原件，Mock 通过不能替代真实 API 验证。
6. 查询一份已有失败任务的详情与安全错误码日志；若要验证重试，使用专门验收环境的可控临时错误，不为了测试中断共用 PostgreSQL/Redis 服务。失败日志不能出现落库成功事件。
7. 检查业务文件不含 Token、密码、签名链接或完整正文。不要把真实密钥复制到搜索命令、终端输出或提交记录中。跨日分文件和多进程竞争已自动测试；真实持续运行时关注磁盘容量并安排历史日期日志归档。已有旧 PID 日志本次不会自动删除或合并；如需整理，先停止旧写入进程并另行确认范围，不删除运行中的锁文件。实际 Linux prefork 及网络文件系统未在 Windows 自动测试中真实验证。
8. 强制刷新 Swagger，在原 `/documents` 入口点击 `Try it out`，确认 `file` 只有一个文件控件、无 `Add string item`。打开选择窗口，用 Ctrl/Shift 一次选两份新 TXT/MD，以及一份重复文件和空文件；选择后应显示数量、名称/大小初检，尚无上传或落库日志。点击 Execute 后核对逐项 201/409/400 与汇总数量，不使用 `/batch` 路径。用响应 `X-Request-ID` 关联 `upload.batch_started / batch_item / batch_finished`；每个成功项独立 doc_id/task_id，后续 Worker 各有解析日志。原显式 `/mineru/documents` 同样多选少量新图片/PDF 验证各自 parse_job 与真实落库，不能以批次 200 当作解析成功。只选一份继续走原上传事件与单文件响应。界面初检不读取或上传文件正文，最终校验与安全日志仍由现有后端负责，不把仅打开文件窗口记成上传成功。

日志设施自身不发 API；缓存、切片、Embedding 与 pgvector 入库继续使用每日同一文件。任务用独立 task_id 与 doc_id 关联，业务成功事件在 SQL 提交后写入；GPU 暂存与入库批次日志不代表整文档完成，只有 index.persisted 表示向量及 READY 已提交。日志不输出 Chunk/Query 正文、向量、模型/结果路径。真实费用按 MinerU batch 与平台核对；本地模型、入库及新增测试待用户执行，不包含检索/RAG。

15A Adapter 使用同一业务 Logger，增加白名单 chunk_id；转换成功不是事务落库事件。Query Adapter 不使用 BaseEmbedding 的含正文/向量回调 payload，不注册第三方 Tracing。框架/依赖自身诊断输出不在本项目日志脱敏保证内，禁止开启可能记录 Node/Query 的第三方调试、回调或全局 instrumentation 导出。

阶段 17 增加白名单 top_k（中文“检索上限”），复用原每日日志、不新增日志文件。Dense 公共入口不调用 BaseRetriever 含原文 callback payload/递归 Query Engine 的路径；继承框架仍可能被外部配置的全局 instrumentation 观察，不能据此宣称任意第三方 Tracing 均已脱敏。请禁用 Query/Node 追踪导出及 SQL echo/debug。dense.finished 仅表示本次只读结果构建完成，不是文档状态提交或语义准确率结论；empty 不代表知识库无内容，可能仅无同模型 READY 数据。
