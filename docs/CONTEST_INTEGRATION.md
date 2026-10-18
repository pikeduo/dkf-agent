# 竞赛整合：方案B

## 当前交付

工作基线为本地`feat/contest-integration`的`9507c2f`，参考目录为`D:\python project\dkf_agent_integration_patch`。阶段1完成必要知识库修复的选择性移植、`contest-agent-v1`结构契约及四类Tool协议；尚未运行测试或接入第二服务/前端。后续开发不得修改或合并到main，不自动暂存、提交或推送。

原FastAPI、PostgreSQL/pgvector、Redis/Celery、MinerU、LlamaIndex、RAG、Citation、Formula及有限LangGraph Knowledge Agent继续保留。知识阶段33全200文档/4087题仍待验收，竞赛阶段1完成不改变该结论。

## 9522356的选择清单

源提交共18个文件，其中包含一份1,698,354字节的补丁ZIP。逐项审阅后仅移植下列直接相关变更；原目标文件与提交父版本一致后才移植，没有merge或整提交cherry-pick。

| 文件 | 处理及原因 |
|---|---|
| `app/parsers/routing/inspector.py` | 移植DOCX官方外部排版模板例外及XML资源超限安全诊断 |
| `app/parsers/hybrid.py` | 移植XLSX包内customXml官方关系兼容，保留缺件/越界保护 |
| `app/services/routed_processing.py` | 移植Hybrid实际终止错误码优先，避免展示启用标记作为错误 |
| `app/services/chunk/splitter.py` | 移植v4 Native TSV逻辑行和有限大表切片，保留v1～v3冻结行为 |
| `app/services/chunk/processing.py` | 移植与切片版本一致的源字符预检和安全失败摘要 |
| `app/services/mineru/table_html.py` | 移植显式TableLimits及可选单元格p兼容，Adapter默认预算不变 |
| `app/core/business_logging.py` | 移植预检拒绝事件，复用原白名单/每日共享日志，不新增进程角色 |
| `tests/test_stage33_office_recovery.py` | 移植Office正反例与预检/终止原因回归，仅编写未执行 |
| `tests/test_chunk_table_rows.py`、`tests/test_document_management.py` | 移植新旧切片版本、TSV/HTML和Reindex检查点用例 |
| `README.md`及`docs/BUSINESS_LOGGING.md`、`CHUNK.md`、`PARSER_ROUTER.md`、`SELECTIVE_HYBRID.md` | 移植上述修复的通用行为说明，并修正新Reindex版本描述 |
| `AGENTS.md`、`docs/STAGE33_EVALUATION.md` | 仅局部维护规则/修复与人工步骤；不复制源提交的新统计或逐文档UUID恢复记录 |
| `dkf_agent_integration_patch.zip` | 排除，不把交付压缩包带入源码 |

缓存、测试产物、数据库数据、文档原件和模型均不移植；真实`.env`未读取正文或修改。未增加依赖或环境变量，无需环境同步。`.gitignore`只放行本次两份工程文档。

本阶段新增`contracts/__init__.py`、`contracts/contest_v1.py`、`contracts/tools.py`和`tests/test_contest_contract.py`。前者只依赖标准库、既有Pydantic及原纯Schema，不导入运行应用、注册路由或连接数据库/模型。

## 双服务边界与参考代码冲突

```mermaid
flowchart LR
    UI[Vue3 统一界面] --> P[开发代理 / 部署反向代理]
    P -->|/api/contest/* → /api/*| C[竞赛FastAPI :8001]
    P -->|/api/* 与 /health| K[原知识FastAPI :8000]
    C --> S[Schema Linking / 澄清 / 只读SQL]
    K --> R[原上传 / Celery / RAG / Citation / Formula]
```

以上为后续架构，不代表阶段1已能启动第二服务。代理必须先匹配`/api/contest/*`再匹配通用`/api/*`；Vite只提供开发代理，生产需由用户部署反向代理。不会在第二服务复制上传、Parser、向量存储、RAG或公式引擎。

| 参考代码的实际问题 | 后续整合处理 |
|---|---|
| `workflow._query`未把confirmed_slots交给pipeline | 从已认证会话恢复槽位，显式传入Linking、生成、修复及结果证据；禁止直接信任客户端槽位 |
| Schema歧义返回needs_user，却被workflow归为执行失败 | 映射needs_clarification，保存候选与问题版本，确认前不生成/执行SQL |
| 独立Text2SQL与对话Trace不共享身份 | 请求入口创建唯一request/run/trace；所有工具继承上下文，不维护进程全局last_trace |
| catalog/pipeline硬编码注册表；mapping交付路径不一致 | 使用同一服务端配置解析注册表及Schema快照；缺失即不可用，不按工作目录猜路径 |
| auto提供方、额外重试、未显式授权修复 | 采用冻结云许可/预算，默认零调用；无传输重试或自动Fallback，最多一次另行许可的SQL修复 |
| Trace JSON含原问题/输入/输出、错误含模型原文 | UI执行证据与业务日志分开；日志只含白名单身份/数量/耗时，Trace按会话鉴权，不能保存原文运行快照 |
| 前端失败后保留Chinook选项、文档/Block只取首屏、旧文档RAG | 数据目录来自真实API，失败显示不可用；支持分页、真实上传状态与原RAG/Citation；删除冒充成功的数据 |
| 参考包包含独立RAG、OCR、Embedding、公式实现 | 不接入正式知识链路；只消费原服务接口或后续薄适配器 |

SQL必须同时使用语法树校验、服务端表/Schema/函数允许集、只读数据库账号/事务、固定search_path、30秒statement_timeout和最多500行展示；只读不等于拥有所有表的读取权限。禁止客户端DSN、文件路径、任意表授权，不能仅靠SQL以SELECT开头判断安全。运行实现、授权预算和Trace真值仍在后续阶段落实，DTO不提供这些执行保证。

## 后续实施阶段

| 阶段 | 实施范围 | 验收门槛 |
|---|---|---|
| 1（本次） | 必要修复、版本化请求/响应与四Tool边界、方案及用例 | 静态检查；用户定向回归及单文档缓存Reindex，结果由用户确认 |
| 2 | 选择性引入contest_backend、配置/注册表、只读SQL与费用边界 | 用户验证越权/写入/危险函数拒绝、真实只读空集/失败/截断；零许可零云调用 |
| 3 | 意图、Schema Linking、会话澄清与Trace贯通 | 用户确认参数进入最终SQL；歧义暂停；过期版本/换范围/跨会话访问拒绝 |
| 4 | Vue3及双服务同源代理，知识库/上传/问数/文档问答/证据界面 | 所有页面依赖真实API；失败不显示成功；完整分页、页语义、证据与执行对应 |
| 5 | 薄适配、手工联调与赛题演示说明 | 用户授权范围内人工端到端验收；未实现能力明确标记，无自动跨源规划器 |

统一Agent暂时只有[契约与设计](CONTEST_API_CONTRACT.md)，不新增Coordinator运行时，不重构原Knowledge Agent；未来启用统一入口需另立实现阶段。

## 阶段1人工验收

以下命令只供用户执行，Codex未运行。先在项目根目录进入已有`dkf-agent`环境：

```powershell
conda activate dkf-agent
python -m pytest tests/test_contest_contract.py -q -ra
python -m pytest tests/test_stage33_office_recovery.py tests/test_chunk_table_rows.py tests/test_chunk_splitter.py tests/test_document_management.py tests/test_parser_router.py tests/test_hybrid.py tests/test_parse_cache.py tests/test_business_logging.py -q -ra
```

预期：契约拒绝空范围、未授权预算、客户端伪造槽位、错run澄清、无执行凭证的成功、执行未知后重放、非有限Trace耗时及敏感额外字段；原修复回归覆盖Office例外与安全反例、长表来源/版本兼容。缺依赖或跳过不等于验收通过，不能据此断言真实SQL/云服务已可用。

真实知识恢复只选一份已有成功缓存、无活动任务的文档，由用户备份并按[阶段33移植验收](STAGE33_EVALUATION.md#竞赛分支选择性移植待人工验收)执行Reindex。预期不新增MinerU批次，不改变原页码/质量，生成v4检查点并由原Worker完成GPU/Index；超长不可拆行仍可明确失败。未做全库导入、阶段33质量评测、SQL运行、云调用或前端验收。

后续仍由用户手动安装/部署基础服务、启动API/Worker、执行迁移和验收。任何付费验收须另行明确接收方、样本、预算与次数，不能从本阶段实施授权推导测试授权。
