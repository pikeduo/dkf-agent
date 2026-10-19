"""检索评测的事后分组与失败定位；真值只用于分析，不参与检索或排序。"""

from scripts.stage33_retrieval import aggregate


def question_context(qa, documents):
    """按全部依赖的原状态/格式分组，保留缺页提示；Office逻辑页不映射成官方PDF页。"""
    sources = [documents.get(fid, {}) for fid in qa["source_file_ids"]]
    statuses = [source.get("execution_status") for source in sources]
    quality = ("NOT_FULLY_INDEXED" if any(s not in {"READY", "PARTIAL_READY"} for s in statuses)
               else "INCLUDES_PARTIAL" if "PARTIAL_READY" in statuses else "READY_ONLY")
    if quality == "READY_ONLY" and any((source.get("parse_quality") or {}).get("content") != "COMPLETE"
                                       for source in sources):
        quality = "READY_WITHOUT_QUALITY_METADATA"
    formats = sorted({source.get("file_type", "unknown") for source in sources})
    missing = {}
    if qa["dataset"] == "ohr":
        gold_pages = set((qa.get("evidence_page") or {}).get("system_1based") or [])
        for fid, source in zip(qa["source_file_ids"], sources, strict=True):
            if source.get("file_type") == "pdf":
                pages = set((source.get("parse_quality") or {}).get("missing_pages") or [])
                overlap = sorted(gold_pages & pages)
                if overlap:
                    missing[fid] = overlap
    return {"source_quality": quality, "source_file_types": formats,
            "file_type_group": "+".join(formats), "required_evidence_pages_missing": missing}


def diagnose_retrieval(result, strategy):
    """区分空结果、未找到目标文档、页不匹配及低排名；OCB只分析依赖文档代理指标。"""
    value = result["strategies"][strategy]
    evidence = result["truth"]["scorable"]
    basis = "official_document_and_physical_page" if evidence else "document_dependency_proxy"
    if value["status"] != "SUCCESS":
        return {"category": value["status"], "basis": basis, "error_code": value.get("error_code")}
    metric = value.get("metrics") if evidence else value.get("document_proxy_metrics")
    if not value["hits"]:
        category = "NO_HITS"
    elif metric is None:
        category = "UNSCORABLE"
    elif metric["recall_at_5"] == 1:
        category = "HIT_AT_5"
    elif metric["recall_at_10"] == 1:
        category = "HIT_AT_10"
    elif metric["mrr_at_20"] > 0:
        category = "LOW_RANK" if metric["recall_at_10"] == 0 else "PARTIAL_RECALL"
    elif evidence and (value.get("document_proxy_metrics") or {}).get("mrr_at_20", 0) > 0:
        category = "TARGET_PAGE_MISS"
    else:
        category = "TARGET_DOCUMENT_MISS"
    return {"category": category, "basis": basis, "error_code": None}


def grouped_metrics(questions, strategies):
    """按互斥格式组合和依赖质量分组；多文档题不复制到多个分母，整体分母保持不变。"""
    groups = {}
    for dimension in ("source_quality", "file_type_group"):
        labels = sorted({question["context"][dimension] for question in questions})
        groups[dimension] = {label: {strategy: aggregate(
            [question for question in questions if question["context"][dimension] == label], strategy)
            for strategy in strategies} for label in labels}
    return groups


def gate_metrics(groups, *, valid):
    """快照未验证时只保留诊断分数，统一撤销整体及分组正式指标资格。"""
    if valid:
        return
    for strategies in groups.values():
        for value in strategies.values():
            value["diagnostic_uncertified_metrics"] = value["metrics"]
            value["diagnostic_uncertified_document_proxy_metrics"] = value["document_proxy_metrics"]
            for name in ("metrics", "document_proxy_metrics", "successful_execution_metrics"):
                value[name] = {key: None for key in value[name]}
