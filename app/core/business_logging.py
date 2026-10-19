"""安全的业务事件日志：白名单摘要、请求关联和跨进程每日合并，不接管第三方日志。"""

import errno
import json
import logging
import math
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep
from types import MappingProxyType
from uuid import UUID, uuid4

from app.core.config import PROJECT_ROOT, Settings

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOGGER = logging.getLogger("dkf.business")
CONTEXT: ContextVar[dict | None] = ContextVar("business_log_context", default=None)
# 独立进程角色与固定显示名称集中维护；未注册/非字符串输入不透传到日志。
ROLE_LABELS = MappingProxyType({
    "api": "API",
    "worker": "Worker",
    "dense-cli": "Dense CLI",
    "bm25-cli": "BM25 CLI",
    "hybrid-cli": "Hybrid CLI",
    "rerank-cli": "Reranker CLI",
    "rag-cli": "RAG CLI",
    "evidence-cli": "Evidence CLI",
    "formula-cli": "Formula CLI",
    "formula-binding-cli": "Formula Binding CLI",
    "formula-calculation-cli": "Formula Calculation CLI",
    "knowledge-agent-cli": "Knowledge Agent CLI",
    "retrieval-acceptance": "检索验收",
    "preset-cli": "Preset CLI",
    "mineru-diagnostic-cli": "MinerU Diagnostic CLI",
    "mineru-cache-upgrade-cli": "MinerU Cache Upgrade CLI",
    "mineru-split-cli": "MinerU Split CLI",
    "office-pdf-cli": "Office PDF CLI",
    "dataset-evaluation-cli": "Dataset Evaluation CLI",
    "unknown": "未知进程",
})
FIELDS = {
    "agent_run_id", "selected_route", "tool_name", "tool_duration_ms", "step_count", "final_status",
    "request_id",
    "task_id",
    "doc_id",
    "chunk_id",
    "block_id",
    "kb_id",
    "job_id",
    "batch_id",
    "file_name",
    "file_type",
    "size_bytes",
    "parser",
    "stage",
    "state",
    "status",
    "status_code",
    "method",
    "route",
    "duration_ms",
    "block_count",
    "chunk_count",
    "chunk_size",
    "chunk_overlap",
    "oversized_count",
    "dimension",
    "top_k",
    "model_name",
    "provider",
    "device",
    "batch_size",
    "batch_index",
    "batch_count",
    "item_count",
    "item_index",
    "page",
    "structure_type",
    "artifact_reused",
    "retry_count",
    "delay_seconds",
    "error_code",
    "error_type",
    "source_file", "source_function", "source_line",
    "queue",
    "task_name",
    "step",
    "upload_complete",
    "file_count",
    "file_index",
    "accepted_count",
    "failed_count",
    "decision_version",
    "parse_mode",
    "route_strategy",
    "reason_codes",
    "fallback_count",
    "mineru_file_submit_count",
    "page_count",
    "sheet_count",
    "image_count",
    "drawing_count",
    "line_count",
    "object_count",
    "limit_count",
    "token_count",
    "chart_count",
    "source_document_count",
    "hybrid_visual_candidate_count",
    "hybrid_visual_filtered_count",
    "hybrid_visual_dedup_count",
    "hybrid_bundle_page_count",
    "hybrid_chart_count",
    "cache_key",
    "parse_cache_hit",
    "visual_cache_hit_count",
    "operation",
}
LOCAL_IDS = {"request_id", "task_id", "doc_id", "chunk_id", "block_id", "kb_id", "job_id", "agent_run_id"}
# Agent只允许固定路由/工具/终态，不允许任意字符串借新增观测字段进入日志。
AGENT_LOG_ENUMS = {
    "selected_route": {"knowledge_search", "knowledge_query", "formula_request"},
    "tool_name": {"search_knowledge_base", "query_knowledge_base", "extract_formula", "calculate_formula"},
    "final_status": {"success", "empty", "insufficient_evidence", "need_variables", "unsupported_formula", "failed"},
}
# FastAPI 自带文档路由不总是设置 scope.route，只接受这些固定公开路径作为回退。
PUBLIC_ROUTES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
FILE_PREFIX = "knowledge-service"
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_RETRY_SECONDS = 0.01
# 中文说明便于直接阅读；保留事件码便于跨 API/Worker 搜索和定位。
EVENT_LABELS = {
    "office.pdf_started": "开始LibreOffice本地分页转换，尚未提交云任务",
    "office.pdf_completed": "Office转换PDF及来源回执已发布，尚未完成业务入库",
    "office.pdf_reused": "复用已核验的Office转换PDF，未重新运行引擎",
    "office.pdf_failed": "Office转换或来源校验失败，未提交云任务",
    "office.split_authorized": "管理员已限定Office新增PDF分片预算，原失败批次保持不变",
    "mineru.exception_frame": "MinerU内部异常调用位置（不含异常原文或局部变量）",
    "dataset_evaluation.started": "开始固定全量数据导入或检索评测",
    "dataset_evaluation.finished": "固定全量评测步骤结束，未完成项保留在报告",
    "agent.started": "开始单次Knowledge Agent工作流",
    "agent.routed": "已选择固定Knowledge Agent路由",
    "agent.step": "Knowledge Agent节点已结束",
    "agent.tool_started": "开始委托已有业务工具",
    "agent.tool_finished": "已有业务工具返回，尚需校验最终协议",
    "agent.tool_failed": "业务工具失败，未自动重试或调用其他收费工具",
    "agent.tool_skipped": "复用已有公式定义，未重复调用提取模型",
    "agent.finished": "Knowledge Agent已终结，仅返回项目协议",
    "http.request": "接口请求结束",
    "logging.file_unavailable": "日志目录不可用，已降级到终端",
    "upload.received": "收到文档上传",
    "upload.rejected": "文档上传校验未通过",
    "upload.duplicate": "检测到重复文档",
    "upload.stored": "文档原件与记录已保存",
    "upload.failed": "文档上传失败",
    "upload.constraint_conflict": "文档上传数据库约束冲突",
    "upload.cleanup_failed": "上传失败后的原件清理失败",
    "upload.batch_rejected": "批量上传数量校验未通过",
    "upload.batch_started": "开始批量上传",
    "upload.batch_item": "批量上传单文件处理结束",
    "upload.batch_finished": "批量上传处理结束",
    "document.dispatched": "文档解析任务已投递",
    "document.dispatch_failed": "文档解析任务投递失败",
    "task.started": "异步任务开始执行",
    "task.finished": "异步任务执行结束",
    "parse.started": "开始原生文档解析",
    "parse.validated": "原生解析结果结构校验通过",
    "parse.persisted": "原生解析结果已落库",
    "parse.skipped": "跳过无需处理的文档任务",
    "parse.retry": "原生解析任务等待重试",
    "parse.failed": "原生解析任务失败",
    "parse.error_record_failed": "原生解析失败原因未能写入数据库",
    "cache.upgrade_validated": "旧版Office原Job及本地缓存身份已核对，未请求云端",
    "cache.upgrade_published": "目标文档新版解析缓存已发布，业务入库尚未执行",
    "cache.upgrade_rejected": "旧版Office缓存升级停止，未请求云端",
    "mineru.dispatched": "MinerU 任务已投递",
    "mineru.dispatch_failed": "MinerU 任务投递失败",
    "mineru.step_started": "MinerU 处理步骤开始",
    "mineru.step_finished": "MinerU 处理步骤结束",
    "mineru.http_started": "开始调用 MinerU 接口",
    "mineru.http_response": "收到 MinerU HTTP 响应",
    "mineru.http_failed": "MinerU HTTP 请求失败",
    "mineru.batch_created": "MinerU 批次检查点已保存",
    "mineru.upload_complete": "MinerU 原件上传检查点已保存",
    "mineru.remote_state": "收到 MinerU 远端解析状态",
    "mineru.result_downloaded": "MinerU 结果 ZIP 下载完成",
    "mineru.raw_result_saved": "MinerU 原始 ZIP 已校验归档，尚不代表业务解析成功",
    "mineru.raw_result_reused": "复用同批次原始 ZIP，未重新下载或提交",
    "mineru.raw_result_rejected": "原始 ZIP 校验或归档失败，未覆盖既有结果或追加批次",
    "mineru.local_replay_started": "从本地原始 ZIP 开始转换，未请求 MinerU 云端",
    "mineru.adapter_failed": "原始 ZIP 已保留，Adapter 转换失败",
    "mineru.adapter_validated": "MinerU 解析结果结构校验通过",
    "mineru.adapter_content": "MinerU 基础内容已提取，结构增强与覆盖质量独立记录",
    "mineru.enhancement_degraded": "MinerU 结构增强降级，保留已提取的可靠基础内容",
    "mineru.merge_restored": "MinerU 合并正文已按证据恢复原页与位置，尚未落库",
    "mineru.merge_rejected": "MinerU 合并正文的来源证据不足，未保存不完整结果",
    "mineru.table_merge_restored": "跨页表格及附属文本已按原V1证据归还原页，尚未落库",
    "mineru.empty_block": "MinerU 结果仍有未解释的空结构，未保存不完整结果",
    "mineru.blocks_persisted": "MinerU 解析结果已落库",
    "mineru.split_planned": "超限 PDF 连续分片计划已保存，尚未提交云任务",
    "mineru.split_piece_done": "PDF 分片结果已缓存，原文档尚未整体落库",
    "mineru.split_continuation_authorized": "仅继续管理员显式授权的未提交PDF分片，可能产生新增费用",
    "mineru.split_manifest_failed": "PDF 分片磁盘副本写入失败，数据库检查点仍保留",
    "mineru.split_checkpoint_invalid": "PDF 分片检查点无效，保留原账本并停止处理",
    "mineru.split_io_failed": "PDF 分片本地文件操作失败，未提交云任务",
    "mineru.retry": "MinerU 任务等待重试",
    "mineru.failed": "MinerU 任务失败",
    "mineru.error_record_failed": "MinerU 失败原因未能写入数据库",
    "router.decided": "文档完整性路由决策已生成",
    "router.failed": "文档自动路由安全拒绝，未生成最终结果",
    "router.preflight_rejected": "PDF预检达到资源边界，未提交云任务或生成部分结果",
    "router.inspection_rejected": "Office结构部件超出预检预算，未提交云任务",
    "router.preflight_geometry_limited": "PDF绘图超出几何细查预算，保留计数与页面覆盖要求",
    "mineru.empty_boundary_preserved": "已核对并保留原空页边界，不将其计为有效正文或页面覆盖",
    "router.fallback": "解析预检要求一次提交前整份云回退",
    "router.guard_passed": "解析结构覆盖检查通过",
    "router.guard_failed": "解析覆盖检查未通过",
    "router.finished": "路由任务处理结束",
    "router.cloud_submitted": "路由整份云解析批次已确认",
    "router.cloud_dispatch_failed": "路由云任务投递确认失败",
    "hybrid.prepared": "Hybrid 候选与单个 Bundle 已准备，尚未写入最终 Block",
    "hybrid.started": "开始 XLSX 主解析与视觉候选检查",
    "hybrid.persisted": "Hybrid 主结果与增强结果已完整落库",
    "hybrid.preflight_rejected": "Hybrid 预检拒绝，按固定原因选择提交前回退或失败",
    "xlsx.primary_validated": "XLSX 原生单元格与公式主结果已校验，尚未最终落库",
    "xlsx.primary_persisted": "XLSX 原生主结果与整份云增强已一起落库",
    "cache.hit": "命中完整解析缓存，不调用云端",
    "cache.miss": "解析缓存未命中，尚未提交云任务",
    "cache.stored": "完整校验结果已持久化为解析缓存，尚未代表数据库提交",
    "cache.busy": "相同内容已有活动云任务，禁止重复收费提交",
    "cache.persisted": "解析缓存已事务回放到文档 Block",
    "cache.maintenance_requested": "收到显式缓存回放或强制重新解析请求",
    "chunk.dispatched": "本地切片任务已投递",
    "chunk.dispatch_failed": "切片投递确认失败，保留检查点",
    "chunk.rejected": "切片请求状态或检查点校验未通过",
    "chunk.started": "开始按原 Block 阅读顺序切片",
    "chunk.batch_staged": "切片批次已暂存，尚未提交完整文档事务",
    "chunk.table_split": "长表格已按完整原行分组，尚未提交切片事务",
    "chunk.table_split_skipped": "长表格行边界无法安全确认，保留原表及超目标标记",
    "chunk.persisted": "切片已事务落库，等待向量化",
    "chunk.skipped": "跳过已完成或过期切片任务",
    "chunk.retry": "切片数据库操作等待重试",
    "chunk.failed": "切片失败，未写入半份结果",
    "chunk.error_record_failed": "切片失败原因未能写入数据库",
    "embedding.model_loaded": "本地 BGE-M3 模型已加载",
    "embedding.encoded": "本地 Dense 向量编码完成，尚未代表文档任务提交",
    "embedding.encode_failed": "本地向量编码安全失败",
    "embedding.chunk_rejected": "切片超过模型真实Token上限，保留原文与来源，未发布向量结果",
    "embedding.chunk_resource_rejected": "切片超过单片字符资源预算，请重建安全切片，未发布向量结果",
    "embedding.encode_skipped": "跳过空的本地向量编码请求，未加载模型",
    "embedding.dispatched": "文档 GPU 批处理任务已投递",
    "embedding.dispatch_failed": "GPU 投递确认失败，保留检查点",
    "embedding.rejected": "文档向量化状态或检查点校验未通过",
    "embedding.started": "开始文档 Chunk 批量向量化",
    "embedding.preflight_started": "开始全量真实Token预检，尚未计算向量",
    "embedding.preflight_passed": "全部切片通过真实Token预检，开始向量编码",
    "embedding.rechunk_started": "检测到超长输入，开始从当前Block安全重切",
    "embedding.rechunk_persisted": "安全重切及新向量检查点已事务提交，等待编码",
    "embedding.rechunk_blocked": "完整公式或表格行组无法安全细分，未发布向量或半份切片",
    "embedding.batch_completed": "文档向量批次已写暂存，尚未发布完整结果",
    "embedding.persisted": "本地向量结果与文档状态已提交，等待索引入库",
    "embedding.skipped": "跳过过期或已完成向量化消息",
    "embedding.retry": "向量化数据库步骤等待重试",
    "embedding.failed": "文档向量化失败，未发布半份业务结果",
    "embedding.error_record_failed": "向量化失败原因未能写入数据库",
    "index.dispatched": "文档向量入库任务已投递",
    "index.dispatch_failed": "向量入库投递确认失败，保留检查点",
    "index.rejected": "向量入库状态或检查点校验未通过",
    "index.started": "开始校验并批量写入文档向量",
    "index.batch_written": "向量批次已执行，尚未提交事务",
    "index.persisted": "全部可靠 Chunk 向量与索引终态已事务提交",
    "router.guard_partial": "可靠内容可继续入库，但解析覆盖不足，未声明完整成功",
    "index.skipped": "跳过已完成或过期向量入库消息",
    "index.retry": "向量入库数据库步骤等待重试",
    "index.failed": "向量入库失败，本次写入已回滚",
    "index.error_record_failed": "向量入库失败原因未能写入数据库",
    "management.requested": "文档入库或维护编排检查点已提交",
    "management.dispatched": "文档管理编排任务已投递",
    "management.dispatch_failed": "文档管理投递确认失败，保留检查点",
    "management.rejected": "文档管理状态或活动任务校验未通过",
    "management.advanced": "文档管理已投递下一阶段，等待业务任务完成",
    "management.finished": "文档管理编排已结束，以文档状态为准",
    "management.deleted": "文档自有文件清理及数据库级联删除已完成",
    "management.failed": "文档管理未完成，保留检查点及安全原因",
    "embedding.input_rejected": "文档向量输入超过聚合资源预算，未继续处理或改写切片",
    "management.error_record_failed": "文档管理失败原因未能写入数据库",
    "formula.started": "开始指定来源的公式语义提取",
    "formula.synthesis_started": "公式及相邻证据已核对，准备单次语义结构化",
    "formula.finished": "公式定义结构校验完成，语义及运算口径仍待人工核验",
    "formula.insufficient": "当前公式证据不足，未补写定义或猜测变量",
    "formula.unsupported": "公式超出第一版语法范围，未执行计算",
    "formula.failed": "公式提取安全失败，未发布部分定义或自动重试",
    "formula_binding.started": "开始显式公式参数及当前来源核对",
    "formula_binding.finished": "参数绑定完成，语义及单位仍待人工核验，未执行计算",
    "formula_binding.need_variables": "公式仍缺参数，未猜值或自动填零",
    "formula_binding.failed": "公式参数绑定安全失败，未覆盖冲突值或修补来源",
    "formula_calculation.started": "开始已有公式及参数来源复核和受限计算",
    "formula_calculation.finished": "精确算术完成，业务语义与单位仍待人工核验",
    "formula_calculation.need_variables": "公式缺参，未导入计算引擎或猜值计算",
    "formula_calculation.failed": "公式计算安全失败，未输出部分结果或自动重试",
    "rag.node_converted": "已有 Chunk 已转换为运行时 Node，未重新处理或落库",
    "rag.result_converted": "运行时 Node 已还原项目证据结果",
    "rag.query_encoded": "Query 已通过项目共享服务编码，未新增文档向量",
    "rag.nodes_reused": "复用已有 Node 向量，未重新编码",
    "rag.adapter_failed": "RAG 适配边界安全拒绝或调用失败",
    "dense.started": "开始指定知识库的只读 Dense 检索",
    "dense.finished": "Dense 检索完成，已保留原 Chunk 证据",
    "dense.empty": "Dense 检索无同模型 READY 结果，未扩大范围或重编码",
    "dense.failed": "Dense 检索安全失败，未返回部分结果",
    "bm25.started": "开始当前 READY 证据的只读 BM25 检索",
    "bm25.index_built": "已构建可重建的本地 BM25 派生索引",
    "bm25.index_reused": "复用 BM25 词频索引，证据仍来自本次查询",
    "bm25.finished": "BM25 检索完成，已保留原 Chunk 证据",
    "bm25.empty": "BM25 无词项命中，未扩大范围或调用模型",
    "bm25.failed": "BM25 检索安全失败，未使用旧缓存结果",
    "fusion.started": "开始同一业务范围的 Dense 与 BM25 融合检索",
    "fusion.branch_finished": "融合检索分支已完成，尚未生成最终候选",
    "fusion.finished": "RRF 融合完成，已按 Chunk 去重并保留排序来源",
    "fusion.empty": "两路检索均为空，未扩大范围或生成回答",
    "fusion.failed": "融合检索安全失败，未静默降级为单路结果",
    "rerank.model_loaded": "项目共享本地 BGE Reranker 模型已加载",
    "rerank.scored": "本地原文对批量重排评分完成，尚未选取最终 Evidence",
    "rerank.score_failed": "本地重排评分安全失败，未返回部分分数",
    "rerank.started": "开始对既有候选同步重排，原证据保持不变",
    "rerank.finished": "Top Evidence 重排完成，原检索与融合分已保留",
    "rerank.skipped": "重排候选为空，未加载 Reranker 模型",
    "rerank.failed": "重排安全失败，未静默返回未经重排的候选",
    "llm.started": "开始单次 LLM 回答请求，本提供方不自动重试",
    "llm.finished": "LLM 完整响应已接收，尚需验证回答引用",
    "llm.failed": "本提供方请求安全失败，未返回部分回答；备用须显式授权",
    "llm.fallback_started": "主模型遇可切换故障，按双方授权配置开始唯一备用请求，可能额外收费",
    "llm.fallback_finished": "备用模型已返回完整JSON，尚需原业务引用或公式校验",
    "llm.fallback_failed": "备用模型失败，停止本次调用，不再次切换或重试",
    "llm.call_finished": "LLM逻辑调用完成，记录本次实际返回内容的提供方和模型",
    "llm.call_failed": "LLM逻辑调用失败，未发布部分内容或自动重放",
    "deepseek.started": "开始单次 LLM 回答请求，不自动重试",
    "deepseek.finished": "LLM 完整响应已接收，尚需验证回答引用",
    "deepseek.failed": "LLM 请求安全失败，未返回部分回答或追加请求",
    "rag.started": "开始同范围检索、重排与证据问答",
    "rag.retrieved": "已取得融合候选，尚未生成回答",
    "rag.synthesis_started": "最终证据已校验，开始单次回答合成",
    "rag.synthesized": "回答结构与最终证据引用已校验，忠实度仍需人工验收",
    "rag.insufficient": "最终证据不足，未猜测答案或捏造引用",
    "rag.finished": "本次问答完成，仅返回项目回答与原始引用契约",
    "rag.failed": "问答安全失败，未静默降级或发布部分回答",
    "citation.created": "运行时引用已绑定原证据，尚未核对当前数据库来源",
    "evidence.started": "开始指定范围来源的独立只读快照核对",
    "evidence.finished": "原Chunk与Block字符来源已核对，不代表事实语义验真",
    "evidence.skipped": "答案没有引用，不查询数据库或补充来源",
    "evidence.failed": "来源核对安全失败，不猜测或重建失效证据",
    "knowledge.started": "开始指定范围的Knowledge服务请求",
    "knowledge.finished": "Knowledge请求完成，仅返回DKF证据或回答契约",
    "knowledge.failed": "Knowledge请求安全失败，不降级为空证据或追加云请求",
    "knowledge.rejected": "Knowledge请求或响应校验拒绝，未回显正文",
    "preset.started": "开始导入预置知识库，默认自动路由",
    "preset.imported": "预置原件已提交并投递解析任务，尚未 READY",
    "preset.reused": "预置内容已存在，沿用原文档且不重发解析",
    "preset.advanced": "预置文档下一阶段检查点已提交并投递",
    "preset.state": "预置文档业务状态已变化",
    "preset.failed": "预置导入单项失败，未重试或覆盖原文档",
    "preset.timeout": "预置导入等待超时，后台任务未被取消",
    "preset.finished": "预置导入有界等待结束，需核对 READY 数量",
}
# 显式固定字段顺序；关联编号保留原名，其他摘要使用中文标签。
FIELD_LABELS = {
    "agent_run_id": "agent_run_id",
    "selected_route": "selected_route",
    "tool_name": "tool_name",
    "tool_duration_ms": "tool_duration_ms",
    "step_count": "step_count",
    "final_status": "final_status",
    "request_id": "request_id",
    "doc_id": "doc_id",
    "chunk_id": "chunk_id",
    "block_id": "block_id",
    "kb_id": "kb_id",
    "task_id": "task_id",
    "job_id": "job_id",
    "batch_id": "batch_id",
    "file_name": "文件",
    "file_type": "格式",
    "size_bytes": "大小_bytes",
    "method": "方法",
    "route": "接口",
    "status_code": "HTTP状态",
    "parser": "解析器",
    "stage": "阶段",
    "step": "步骤",
    "queue": "队列",
    "task_name": "任务",
    "state": "状态",
    "status": "结果",
    "upload_complete": "上传完成",
    "block_count": "Block数",
    "chunk_count": "Chunk数",
    "chunk_size": "切片目标_token估算",
    "chunk_overlap": "重叠目标_token估算",
    "oversized_count": "超目标结构切片数",
    "dimension": "向量维度",
    "top_k": "检索上限",
    "model_name": "模型",
    "provider": "提供方",
    "device": "计算设备",
    "batch_size": "批大小",
    "batch_index": "批序号",
    "batch_count": "批次数",
    "item_count": "文本数",
    "item_index": "原数组序号",
    "page": "局部页码",
    "structure_type": "结构类型",
    "artifact_reused": "复用本地向量结果",
    "file_count": "文件数",
    "file_index": "文件序号",
    "accepted_count": "接收数",
    "failed_count": "失败数",
    "retry_count": "重试次数",
    "delay_seconds": "等待秒数",
    "duration_ms": "耗时_ms",
    "error_code": "错误码",
    "error_type": "错误类型",
    "source_file": "源码文件", "source_function": "源码函数", "source_line": "源码行号",
    "decision_version": "路由版本",
    "parse_mode": "解析模式",
    "route_strategy": "路由策略",
    "reason_codes": "路由理由",
    "fallback_count": "回退次数",
    "mineru_file_submit_count": "云文件提交数",
    "page_count": "物理页数",
    "sheet_count": "Sheet数",
    "image_count": "图片数",
    "drawing_count": "绘图数",
    "line_count": "文本行数",
    "object_count": "图像及绘图数",
    "limit_count": "限制数量",
    "token_count": "真实Token数",
    "chart_count": "图表数",
    "source_document_count": "源文档数",
    "hybrid_visual_candidate_count": "视觉候选数",
    "hybrid_visual_filtered_count": "视觉过滤数",
    "hybrid_visual_dedup_count": "视觉复用数",
    "hybrid_bundle_page_count": "Bundle页数",
    "hybrid_chart_count": "结构化图表数",
    "cache_key": "缓存键",
    "parse_cache_hit": "解析缓存命中",
    "visual_cache_hit_count": "视觉缓存命中数",
    "operation": "操作",
}


def lock_file(descriptor: int, *, unlock: bool = False) -> None:
    """在固定锁文件上加锁/解锁；Windows 锁首字节，Unix 使用非阻塞独占 flock。"""

    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(
            descriptor, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB
        )


@contextmanager
def shared_file_lock(directory: Path):
    """串行化完整日志行写入；每次独立打开锁描述符，避免 prefork 继承同一锁身份。"""

    # 锁文件不能运行中删除/替换，否则新旧进程可能锁住不同文件而失去互斥。
    # Windows 允许锁住 EOF 之后的字节，无需向锁文件写入任何业务内容。
    descriptor = os.open(
        directory / f".{FILE_PREFIX}.lock", os.O_CREAT | os.O_RDWR, 0o600
    )
    acquired = False
    try:
        deadline = perf_counter() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock_file(descriptor)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if perf_counter() >= deadline:
                    raise TimeoutError("日志写入锁等待超时") from None
                sleep(LOCK_RETRY_SECONDS)
        yield
    finally:
        try:
            if acquired:
                lock_file(descriptor, unlock=True)
        finally:
            # 包括异常退出在内，描述符关闭释放 OS 锁，不遗留 PID 占用标记。
            os.close(descriptor)


def safe_fields(fields: dict) -> dict:
    """保留白名单标量；资源及Token计数仅接受有界非负整数，标识仅接受UUID。"""

    result = {}
    for key, value in fields.items():
        if key not in FIELDS or value is None:
            continue
        # 资源计数不能夹带正文、布尔值或任意对象，零计数也必须保留。
        if key in {"drawing_count", "line_count", "object_count", "limit_count", "token_count"} and (
            type(value) is not int or not 0 <= value <= 2**31 - 1
        ):
            continue
        if key == "source_line" and (type(value) is not int or not 1 <= value <= 1024**2):
            continue
        if key == "source_file" and (not isinstance(value, str)
                or not re.fullmatch(r"(?:app|scripts)/[A-Za-z0-9_/]+\.py", value)):
            continue
        if key == "source_function" and (not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}|<module>|<lambda>|<listcomp>", value)):
            continue
        if key in {"page", "item_index"} and (type(value) is not int or not 1 <= value <= 1024**2):
            continue
        if key == "structure_type" and (not isinstance(value, str) or value not in {
            "text", "title", "image_text", "header", "footer", "page_number", "aside_text",
            "page_footnote", "table", "list", "code", "equation", "formula", "unknown"}):
            continue
        if key in AGENT_LOG_ENUMS and (not isinstance(value, str) or value not in AGENT_LOG_ENUMS[key]):
            continue
        if key == "step_count" and (type(value) is not int or not 0 <= value <= 32):
            continue
        if key == "tool_duration_ms" and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            continue
        # 提供方只允许固定枚举，不能把外部地址/任意字符串伪装成角色摘要。
        if key == "provider" and (not isinstance(value, str) or value not in {"deepseek", "custom"}):
            continue
        if key in LOCAL_IDS:
            try:
                value = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
        elif isinstance(value, UUID):
            value = str(value)
        if isinstance(value, str):
            # 清除控制字符；格式器再转义字段分隔符，防止伪造新行或新字段。
            value = "".join(c for c in value if c.isprintable())[:256]
            if key == "batch_id" and not re.fullmatch(r"[\w.:-]{1,128}", value):
                continue
        elif (
            not isinstance(value, (int, float, bool))
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
            continue
        result[key] = value
    return result


def log_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    """记录白名单业务摘要；日志系统异常不得改变上传、事务或任务的业务结果。"""

    try:
        LOGGER.log(
            level,
            event,
            extra={"business_fields": safe_fields((CONTEXT.get() or {}) | fields)},
        )
    except Exception:  # noqa: BLE001
        # 可观测性不是事务的一部分；不得因为磁盘满而回退已成功的文档处理。
        return


class ReadableFormatter(logging.Formatter):
    """仅格式化业务事件，不输出异常堆栈、任意响应、连接串或文档正文。"""

    def __init__(self, role: str = "unknown") -> None:
        """只接受集中注册的稳定角色，未知/异常类型安全回退；业务字段不能伪造进程身份。"""

        super().__init__()
        self.role = role if type(role) is str and role in ROLE_LABELS else "unknown"

    def format(self, record: logging.LogRecord) -> str:
        """按集中角色显示生成中文单行摘要，保留真实PID及事件字段，不输出任意角色/正文。"""

        event = (
            record.msg
            if isinstance(record.msg, str)
            and re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", record.msg)
            else "unknown"
        )
        parts = [
            datetime.fromtimestamp(record.created, UTC)
            .astimezone()
            .isoformat(sep=" ", timespec="milliseconds"),
            logging.getLevelName(record.levelno),
            ROLE_LABELS[self.role],
            EVENT_LABELS.get(event, "业务事件"),
            f"event={event}",
            f"pid={record.process}",
        ]
        fields = safe_fields(getattr(record, "business_fields", {}))
        for key, label in FIELD_LABELS.items():
            if key in fields:
                # 只借用 JSON 的标量引用/转义，不再输出 JSON 对象；竖线不能伪造分隔符。
                value = json.dumps(fields[key], ensure_ascii=False, allow_nan=False)
                value = value.replace("|", r"\u007c")
                parts.append(f"{label}={value}")
        return " | ".join(parts)


class SafeDailyFileHandler(logging.Handler):
    """所有进程按本机日期追加同一文件，不按 PID/容量拆分，不删除历史日志。"""

    def __init__(self, directory: Path) -> None:
        """保存日志目录；不提前创建空日志，不保持跨日或跨 fork 的数据文件句柄。"""

        super().__init__()
        self.directory = directory
        self.warned = False

    def append_line(self, record: logging.LogRecord, payload: bytes) -> None:
        """在 OS 锁内完整写入 UTF-8 行；按事件产生的本机日期选文件，不跨日重命名。"""

        date = (
            datetime.fromtimestamp(record.created, UTC)
            .astimezone()
            .strftime("%Y-%m-%d")
        )
        path = self.directory / f"{FILE_PREFIX}-{date}.log"
        with shared_file_lock(self.directory), path.open("ab", buffering=0) as stream:
            pending = memoryview(payload)
            while pending:
                written = stream.write(pending)
                if not written:
                    raise OSError("日志行未完整写入")
                pending = pending[written:]

    def emit(self, record: logging.LogRecord) -> None:
        """追加单行中文摘要；锁超时、磁盘或格式错误降级到终端，不影响业务结果。"""

        try:
            self.append_line(record, (self.format(record) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """首次写盘或锁失败安全提示，不输出原始异常；终端仍记录业务事件。"""

        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write(
                    "业务日志写盘失败，请检查目录权限、磁盘空间或写入锁占用；终端日志继续输出。\n"
                )
            except OSError:
                return


def configure_logging(settings: Settings, role: str) -> None:
    """以注册角色配置业务Logger；所有进程仍合并每日文件，重复配置不增加文件数量。"""

    LOGGER.setLevel(settings.log_level)
    LOGGER.disabled = False
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = ReadableFormatter(role)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    directory = settings.log_dir
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = SafeDailyFileHandler(directory)
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    except OSError:
        log_event(
            "logging.file_unavailable", level=logging.WARNING, error_code="LOG_FILE_IO"
        )


class RequestLoggingMiddleware:
    """纯 ASGI 中间件，不读取请求或响应正文，覆盖完成、校验失败和未处理异常。"""

    def __init__(self, app) -> None:
        """保存下游应用；不改变流式传输、上传文件或数据库依赖生命周期。"""

        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        """为每次 HTTP 请求生成可信编号，返回 X-Request-ID 并记录完整响应发送耗时。"""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        token = CONTEXT.set({"request_id": request_id})
        start, status, error_type = perf_counter(), 500, None

        async def send_response(message):
            """只增加响应关联头并捕获状态码，不缓冲、解析或记录响应正文。"""

            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            # 使用匹配路由模板而非用户传入的路径或查询串，避免 URL 中的密钥进入文件。
            route = getattr(scope.get("route"), "path", None)
            if route is None:
                path = scope.get("path")
                route = path if path in PUBLIC_ROUTES else "<unmatched>"
            log_event(
                "http.request",
                level=logging.ERROR
                if error_type or status >= 500
                else logging.WARNING
                if status >= 400
                else logging.INFO,
                method=scope["method"],
                route=route,
                status_code=status,
                duration_ms=round((perf_counter() - start) * 1000, 2),
                error_type=error_type,
                **{
                    key: value
                    for key, value in scope.get("path_params", {}).items()
                    if key in LOCAL_IDS
                },
            )
            CONTEXT.reset(token)
