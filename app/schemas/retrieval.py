"""项目内部检索结果 DTO，只描述证据与分数，不包含 LlamaIndex 类型或持久化操作。"""

import math
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.core.file_formats import FileType


class FusionProvenance(BaseModel):
    """等权 RRF 的可解释来源；原分数仅展示，排名从 1 开始，不表示概率。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    strategy: Literal["rrf-v1"] = "rrf-v1"
    rrf_k: StrictInt = Field(ge=1, le=1000)
    sources: list[Literal["dense", "bm25"]]
    dense_rank: StrictInt | None = Field(default=None, ge=1, le=100)
    dense_score: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False, strict=True)
    bm25_rank: StrictInt | None = Field(default=None, ge=1, le=100)
    bm25_score: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    fusion_score: float = Field(gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_ranks(self) -> "FusionProvenance":
        """来源、原排名/分数及融合分必须相互一致，拒绝缺失或伪造排序解释。"""
        sources, contributions = [], []
        for name in ("dense", "bm25"):
            rank, score = getattr(self, f"{name}_rank"), getattr(self, f"{name}_score")
            if (rank is None) != (score is None):
                raise ValueError("融合来源的排名与分数不完整")
            if rank is not None:
                sources.append(name)
                contributions.append(1.0 / (self.rrf_k + rank))
        if (not sources or self.sources != sources
                or not math.isclose(self.fusion_score, math.fsum(contributions), rel_tol=1e-12, abs_tol=1e-15)):
            raise ValueError("融合来源或排名贡献无效")
        return self


class RetrievalResult(BaseModel):
    """Chunk→Page/Section→Document 的运行时表示；无分数时保留 None，不虚构相关性。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kb_id: UUID
    doc_id: UUID
    chunk_id: UUID
    file_name: StrictStr = Field(min_length=1, max_length=255)
    file_type: FileType
    text: StrictStr
    page_start: StrictInt = Field(ge=1)
    page_end: StrictInt = Field(ge=1)
    section: StrictStr | None
    metadata: dict[str, Any]
    embedding_identity: dict[str, Any] | None = None
    retrieval_score: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    fusion: FusionProvenance | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "RetrievalResult":
        """检查原文非空和页范围；保留原字符串与逻辑/物理页语义，不修剪或猜测内容。"""

        if not self.text.strip() or self.page_end < self.page_start:
            raise ValueError("证据正文或页范围无效")
        return self
