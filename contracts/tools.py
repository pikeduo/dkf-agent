"""四类Tool的调用边界；仅声明Protocol，不实现调度、HTTP或数据库访问。"""

from typing import Protocol

from contracts.contest_v1 import (
    AgentRequest, ClarificationPrompt, ConfirmedSlot, FormulaResult, RAGResult,
    RunContext, SchemaLink, SQLResult,
)


class SQLTool(Protocol):
    """未来适配contest_backend的只读问数链路，不调用参考包的RAG或Formula实现。"""

    def run(self, request: AgentRequest, context: RunContext) -> SQLResult | ClarificationPrompt:
        """消费固定sql_scope和服务端confirmed_slots；歧义暂停，执行及收费须按契约限额。"""
        ...


class RAGTool(Protocol):
    """复用原knowledge服务的Search/Query/Evidence，不另建检索、索引或引用链路。"""

    def run(self, request: AgentRequest, context: RunContext) -> RAGResult:
        """透传原RAGAnswer；仅在本次云许可有效时调用Query，不自动启用LLM Fallback。"""
        ...


class FormulaTool(Protocol):
    """适配原公式提取、参数绑定和SymPy服务，不改有限LangGraph Knowledge Agent。"""

    def run(self, request: AgentRequest, context: RunContext) -> FormulaResult:
        """只处理显式公式来源；复用已核对定义，缺参直返，不猜值或新增计算引擎。"""
        ...


class ClarificationTool(Protocol):
    """只管理固定会话/范围下的澄清，不执行SQL或自行发起云调用。"""

    def propose(self, request: AgentRequest, context: RunContext,
                schema_links: list[SchemaLink]) -> ClarificationPrompt | None:
        """将真实歧义保存为带revision的问题；无歧义返回None，不制造已确认参数。"""
        ...

    def accept(self, request: AgentRequest, context: RunContext) -> list[ConfirmedSlot]:
        """校验会话所有权、原问题、范围、revision和选项后产生SQL必须消费的槽位。"""
        ...
