"""专项入口的固定安全错误，复用公式与来源分类，不回显上游异常。"""

from app.services.formula import ERRORS as FORMULA_ERRORS
from app.services.formula_binding import ERRORS as BINDING_ERRORS
from app.services.formula_calculation import ERRORS as CALCULATION_ERRORS
from app.services.knowledge_query import KnowledgeServiceError
from app.services.rag.evidence import ERRORS as EVIDENCE_ERRORS

ERRORS = {
    "ANALYSIS_INPUT": (422, "文档评估参数无效"),
    "ANALYSIS_UNAVAILABLE": (404, "当前知识库范围没有可评估的已索引文档"),
    "ANALYSIS_METADATA": (409, "现有Block或质量字段无效，未猜测或修复来源"),
    "ANALYSIS_LIMIT": (413, "评估观测数量超过单次预算，未发布不完整报告"),
    "ANALYSIS_SESSION": (409, "文档评估需要独立且无待写入变更的会话"),
    "ANALYSIS_DATABASE": (503, "文档评估只读查询未完成"),
    "ANALYSIS_VISUAL_SOURCE": (409, "原件不可用、Hash不符或无法安全读取，未转换或修复原件"),
    "ANALYSIS_VISUAL_FORMAT": (422, "视觉页检查仅支持原PDF或图片，不推断Office物理页"),
    "ANALYSIS_VISUAL_PAGE": (422, "所选页不在原件实际页范围内"),
    "ANALYSIS_VISUAL_LIMIT": (413, "原件或页面超过本次有界视觉检查预算"),
    "SPECIALTY_RESULT": (502, "专项结果或来源不符合契约，未返回部分结果"),
    "SPECIALTY_INTERNAL": (500, "专项服务未完成，请查看安全日志"),
}
DOMAIN_ERRORS = FORMULA_ERRORS | BINDING_ERRORS | CALCULATION_ERRORS | EVIDENCE_ERRORS


class SpecialtyError(Exception):
    """仅使用集中白名单文案；不能把异常的任意code/message透传到HTTP或日志。"""

    def __init__(self, code):
        """保留已知底层分类与安全说明，未知错误归为内部失败。"""
        code = code if isinstance(code, str) else "SPECIALTY_INTERNAL"
        if code in ERRORS:
            self.status_code, self.message = ERRORS[code]
        elif code in DOMAIN_ERRORS:
            self.message = DOMAIN_ERRORS[code]
            self.status_code = 403 if code == "FORMULA_CLOUD_PERMISSION" else (
                503 if code in {"EVIDENCE_DATABASE", "FORMULA_CALC_DEPENDENCY"} else
                413 if code.endswith("LIMIT") else
                502 if code == "FORMULA_OUTPUT" else
                422 if code.endswith(("INPUT", "UNKNOWN", "ZERO_DIVISION", "EXPRESSION", "SYMBOLS")) else 409)
        else:
            original = KnowledgeServiceError(code)
            if original.code == "KNOWLEDGE_INTERNAL":
                code = "SPECIALTY_INTERNAL"
                self.status_code, self.message = ERRORS[code]
            else:
                self.status_code, self.message = original.status_code, original.message
        self.code = code
        super().__init__(self.message)
