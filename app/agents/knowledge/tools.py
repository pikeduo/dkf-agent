"""项目Tool薄适配层：只委托既有服务，不复制SQL、HTTP、检索或公式计算。"""

from app.schemas.formula_binding import FormulaBindingRequest
from app.schemas.knowledge_agent import KnowledgeAgentRequest
from app.schemas.knowledge_query import KnowledgeQueryRequest, KnowledgeSearchRequest


class KnowledgeTools:
    """不使用AgentExecutor/第二套RAG；业务服务惰性导入且继续管理模型、授权及来源。"""

    def search_knowledge_base(self, request: KnowledgeAgentRequest):
        """固定kb/doc/Top-K，复用KnowledgeService的检索及响应校验，不调用LLM。"""
        from app.services.knowledge_query import KnowledgeService
        return KnowledgeService().search(KnowledgeSearchRequest(kb_id=request.kb_id, question=request.question,
            doc_ids=request.doc_ids, top_k=request.top_k))

    def query_knowledge_base(self, request: KnowledgeAgentRequest):
        """透传本次云许可，由现有服务组织最终Evidence与一次LLM合成。"""
        from app.services.knowledge_query import KnowledgeService
        return KnowledgeService().query(KnowledgeQueryRequest(kb_id=request.kb_id, question=request.question,
            doc_ids=request.doc_ids, top_k=request.top_k, allow_cloud=request.allow_cloud))

    def extract_formula(self, request: KnowledgeAgentRequest):
        """只提取显式所选来源，不搜索、猜选公式、调用MinerU或自行创建LLM客户端。"""
        from app.services.formula import extract_formula
        selected = request.formula
        return extract_formula(request.kb_id, selected.chunk_id, selected.block_id,
                               doc_id=selected.doc_id, allow_cloud=request.allow_cloud)

    def calculate_formula(self, request: KnowledgeAgentRequest, definition):
        """复用阶段29输入与当前来源复核，不猜值、换算单位或重写SymPy逻辑。"""
        from app.services.formula_calculation import calculate_formula
        return calculate_formula(FormulaBindingRequest(formula=definition, user_inputs=request.formula.user_inputs,
                                                       evidence_inputs=request.formula.evidence_inputs))
