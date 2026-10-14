"""框架Node与项目Citation之间的薄边界；正式引用/反查契约不依赖LlamaIndex。"""

from llama_index.core.schema import NodeWithScore, TextNode

from app.schemas.evidence import Citation
from app.services.rag.evidence import citation_to_result, make_citation
from app.services.rag.llama_index.node_adapter import node_to_result, result_to_node


def citation_from_node(node: TextNode | NodeWithScore, citation_id: int) -> Citation:
    """复用既有Node校验并绑定本地编号，不补分数、页码或查询数据库。"""
    return make_citation(node_to_result(node, log_success=False), citation_id)


def citation_to_node(citation: Citation) -> NodeWithScore:
    """去除展示编号后复用原Node还原，保留正文/来源/分数，不重编码文档。"""
    return result_to_node(citation_to_result(citation), log_success=False)
