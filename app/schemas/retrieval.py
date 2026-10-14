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


class RerankProvenance(BaseModel):
    """本地重排身份与独立分数，原检索排名保留，不把logit解释为概率。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model_name: Literal["BAAI/bge-reranker-v2-m3"]
    model_fingerprint: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    version: Literal["bge-reranker-v2-m3-logit-v1"]
    max_length: StrictInt = Field(ge=8, le=8192)
    precision: Literal["fp16", "fp32"]
    score_type: Literal["logit"]
    input_rank: StrictInt = Field(ge=1, le=100)
    retrieval_score: float = Field(allow_inf_nan=False, strict=True)
    fusion_score: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    rerank_score: float = Field(allow_inf_nan=False, strict=True)


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
    fusion_score: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    rerank_score: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    rerank: RerankProvenance | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "RetrievalResult":
        """检查原文/页范围及各阶段分数的一致性，原检索分不被重排分覆盖。"""

        if not self.text.strip() or self.page_end < self.page_start:
            raise ValueError("证据正文或页范围无效")
        fusion_score = self.fusion.fusion_score if self.fusion is not None else None
        if self.fusion_score is None:
            self.fusion_score = fusion_score
        if self.fusion_score != fusion_score:
            raise ValueError("融合分与来源不一致")
        if (fusion_score is not None and self.retrieval_score is not None
                and not math.isclose(self.retrieval_score, fusion_score, rel_tol=1e-12, abs_tol=1e-15)):
            raise ValueError("原融合检索分与来源不一致")
        if self.rerank is not None:
            if (self.retrieval_score != self.rerank.retrieval_score
                    or self.rerank_score != self.rerank.rerank_score
                    or self.fusion_score != self.rerank.fusion_score):
                raise ValueError("重排分数与来源不一致")
        elif self.rerank_score is not None:
            raise ValueError("缺少重排来源")
        return self
