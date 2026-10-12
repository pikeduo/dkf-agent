"""公开基准工具的元数据/结果 Stub 用例；由用户执行，不生成或解析真实文档样本。"""

import copy
import hashlib

import pytest

from scripts import benchmark_common as common
from scripts import benchmark_coverage
from scripts import evaluate_benchmark_parsing as quality
from scripts import prepare_ocb_preset as ocb
from scripts import prepare_omnidocbench_preset as omni


def hf_row(files=None):
    """构造纯字段契约 Stub，占位断言不是官方 QA 或真实验收材料。"""
    return {"reference_files": files or ["schema.xlsx"], "question": "__schema_question__",
            "expected_assertions": ["__schema_assertion__"], "file_format": "xlsx",
            "track": "File Fidelity", "feature": "__schema_feature__", "version": "schema"}


def asset_stub(name):
    """只提供已校验文件元数据，不创建 Office/PDF/图像或执行容器检查。"""
    return {"source_identity": name, "file_type": name.split(".")[-1], "size_bytes": 100,
            "sha256": hashlib.sha256(name.encode()).hexdigest(), "source_file": "reference_files/" + name,
            "destination_file": "ocb/reference_files/" + name}


def gt_page():
    """构造页面/区域字段 Stub，不含真实图像或官方样本内容。"""
    return {"page_info": {"image_path": "schema.png", "page_no": 8, "width": 100, "height": 200,
                          "page_attribute": {"layout": "double_column", "language": "en_ch_mixed"}},
            "layout_dets": [{"anno_id": 1, "category_type": "text_block", "poly": [0, 0, 50, 0, 50, 20, 0, 20],
                             "order": 0, "text": "__stub__", "ignore": False}], "extra": {"relation": []}}


def exported_document():
    """只构造已有解析结果 DTO，不调用 Parser/数据库或重新生成切片/向量。"""
    return {"doc_id": "schema-doc", "file_type": "png", "file_hash": "a" * 64,
            "blocks": [{"block_id": "schema-block", "block_index": 1, "page": 1, "block_type": "text",
                        "text": "__stub__", "bbox": [0, 0, 50, 20], "source": "mineru_cloud"}], "chunks": []}


def omni_reference():
    """构造图像和官方区域关联 Stub，GT 页号与业务图片第 1 页有意不同。"""
    return {"reference_id": "schema.png", "document": {"sha256": "a" * 64},
            "original": gt_page(), "source_gt_page_no": 8, "project_document_page": 1}


def region_match():
    """只表示用户已确认的区域映射，不自动猜 bbox 或读取顺序。"""
    return {"reference_id": "schema.png", "anno_id": 1, "doc_id": "schema-doc", "block_ids": ["schema-block"],
            "confirmed": True, "bbox_verified": True, "coordinate_space": "image_pixels"}


def test_ocb_schema_aliases_and_original_preservation():
    """HF/NDJSON 分别适配，保留原始字段和行身份，不将生成 id 误称官方 UUID。"""
    raw = hf_row()
    hf = ocb.qa_record(raw, 7, "hf", "a" * 64)
    nd = ocb.qa_record({"id": "7", "filepath": ["schema.xlsx"], "query": raw["question"],
                        "gold": raw["expected_assertions"], "track": "file_fidelity", "original_format": "xlsx"},
                       0, "ndjson", "b" * 64)
    assert hf["original"] == raw
    assert hf["reference_id"].endswith("hf_row:7")
    assert nd["reference_id"].endswith("ndjson_id:7")
    assert hf["expected_assertions"] == nd["expected_assertions"]
    with pytest.raises(common.BenchmarkError):
        ocb.qa_record(raw, 0, "ndjson", "a" * 64)


@pytest.mark.parametrize("track", [None, "Domain Q&A", "__unknown__"])
def test_ocb_rejects_unknown_or_non_fidelity_track(track):
    """没有 File Fidelity 证据不能自行根据问题、后缀猜 track。"""
    raw = hf_row()
    raw["track"] = track
    with pytest.raises(common.BenchmarkError):
        ocb.qa_record(raw, 0, "hf", "a" * 64)


def test_ocb_bundle_selection_keeps_all_dependencies():
    """多文件断言不会被拆成虚假的单文件 QA，共享附件只占一个真实文件身份。"""
    files = ["a.docx", "b.xlsx", "c.pptx"]
    assets = {name: asset_stub(name) for name in files}
    row = ocb.qa_record(hf_row(files), 0, "hf", "a" * 64)
    selected, references = ocb.choose([row], assets, per_format=1)
    assert len(selected) == 3
    assert len(references) == 1
    assert references[0]["reference_files"] == files
    assert len(references[0]["dependencies"]) == 3


def test_ocb_sampling_determinism():
    """候选输入顺序不影响固定种子下的选择；不依赖进程 Hash 或文件名硬编码。"""
    files = ["a.docx", "b.docx", "c.xlsx", "d.xlsx", "e.pptx", "f.pptx"]
    assets = {name: asset_stub(name) for name in files}
    rows = [ocb.qa_record(hf_row([name]), index, "hf", "a" * 64) for index, name in enumerate(files)]
    assert ocb.choose(rows, assets, seed=42) == ocb.choose(list(reversed(rows)), assets, seed=42)


def test_ocb_rejects_unreviewed_or_converted_assets(tmp_path):
    """原件/许可/转换状态未知或需要转换时在读取原件前拒绝，不触发下载。"""
    review = {"reference_file": "schema.xlsx", "original_native": True, "local_use_confirmed": True,
              "conversion_required": True, "license": "__reviewed_stub__", "source_url": "https://example.invalid/"}
    with pytest.raises(common.BenchmarkError, match="BENCH_PERMISSION"):
        ocb.reviewed_asset("schema.xlsx", {"schema.xlsx": review}, {}, tmp_path, 1024)
    with pytest.raises(common.BenchmarkError, match="BENCH_PERMISSION"):
        ocb.reviewed_asset("schema.xlsx", {}, {}, tmp_path, 1024)


def test_ocb_missing_dependency_generates_gap_not_partial_qa(monkeypatch):
    """首次遇到缺少审核附件也必须记缺口，不能生成包含未验证依赖的 QA 计划。"""
    def source_stub(root, dataset, metadata_names):
        """仅提供源摘要 Stub，不读取真实官方文件。"""
        return {"metadata_sha256": {"ocb_qna_data.parquet": "a" * 64}}
    def json_stub(path, beneath=None):
        """返回空审核/托管表，模拟真实缺口而非生成假原件。"""
        return []
    def rows_stub(path):
        """提供一个多文件字段 Stub，不包含文档内容。"""
        return [hf_row(["missing.docx", "missing.xlsx"])]
    monkeypatch.setattr(common, "source_record", source_stub)
    monkeypatch.setattr(common, "json_file", json_stub)
    monkeypatch.setattr(common, "rows_file", rows_stub)
    from types import SimpleNamespace
    plan, report, references = ocb.prepare(SimpleNamespace(qa_file="ocb_qna_data.parquet", schema="hf", max_mb=20, per_format=2, seed=42))
    assert plan["documents"] == [] and references == b""
    assert report["gaps"]["missing_or_rejected_assets"]["missing.xlsx"] == "source_or_license_review_missing"


def test_omni_optional_original_pdf_never_guesses_missing_mapping(tmp_path):
    """没有官方原件/人工页映射时只报告缺口，不通过页名推算或生成 PDF。"""
    references = [omni_reference()]
    documents = []
    missing = omni.attach_original_pdfs(tmp_path, references, documents, [], "a" * 40, 1024)
    assert missing == ["schema.png"] and documents == []
    assert "original_pdf" not in references[0]


def test_ocb_hosted_duplicate_paths_rejected():
    """HF tree 中重复或大小写冲突的路径不可冒充两个不同来源。"""
    with pytest.raises(common.BenchmarkError):
        ocb.hosted_index([{"type": "file", "path": "reference_files/a.xlsx"},
                          {"type": "file", "path": "reference_files/A.xlsx"}])


@pytest.mark.parametrize("name", ["../schema.png", "C:/schema.png", "a\\schema.png"])
def test_omni_rejects_unsafe_annotation_path(name):
    """GT 文件路径也受共享防越界规则约束，不只检查业务输出路径。"""
    row = gt_page()
    row["page_info"]["image_path"] = name
    with pytest.raises(common.ohr.PreparationError):
        omni.page_record(row)


def test_omni_preserves_identity_page_and_labels():
    """保留原始 page_no，不从名称补原 PDF 来源；分类来自真实字段而不是语义推断。"""
    raw = gt_page()
    before = copy.deepcopy(raw)
    name, labels = omni.page_record(raw)
    assert name == "schema.png" and labels == {"text", "multi_column", "chinese"}
    assert raw == before and raw["page_info"]["page_no"] == 8


def test_omni_duplicate_region_and_out_of_bounds_rejected():
    """重复区域身份或超界几何不得进入可评分的 GT。"""
    row = gt_page()
    row["layout_dets"].append(copy.deepcopy(row["layout_dets"][0]))
    with pytest.raises(common.BenchmarkError):
        omni.page_record(row)
    row = gt_page()
    row["layout_dets"][0]["poly"][0] = -1
    with pytest.raises(common.BenchmarkError):
        omni.page_record(row)


def test_empty_or_missing_metrics_are_not_zero():
    """未匹配区域、缺 HTML、无 Chunk 不能补零或虚构官方总分。"""
    document = exported_document()
    checks = quality.omni_checks([omni_reference()], {document["doc_id"]: document}, {})
    assert any(item["status"] == "not_applicable" for item in checks)
    assert not any(item["status"] == "measured" for item in checks)
    assert all(item["value"] is None for item in checks)
    assert quality.summary(checks)["region_mapping"]["mean"] is None


def test_text_and_layout_are_only_measured_after_manual_match():
    """人工一对一确认后计算字符诊断，保存真实 GT 页与图片业务页的独立语义。"""
    document = exported_document()
    checks = quality.omni_checks([omni_reference()], {document["doc_id"]: document}, {"matches": [region_match()]})
    text = next(item for item in checks if item["criterion"] == "text_cer")
    box = next(item for item in checks if item["criterion"] == "axis_aligned_bbox_iou")
    assert text["value"] == 0 and box["value"] == 1
    assert box["evidence_position"]["gt_page_no"] == 8
    assert box["evidence_position"]["project_page"] == 1


def test_pdf_points_are_not_compared_as_image_pixels():
    """没有可靠转换时不能把 PDF 点数当像素计算 IoU，必须显式不可评测。"""
    document = exported_document()
    document["file_type"] = "pdf"
    checks, _ = quality.region_checks(omni_reference(), gt_page()["layout_dets"][0], document, region_match(), None)
    box = next(item for item in checks if item["criterion"] == "axis_aligned_bbox_iou")
    assert box["status"] == "not_applicable" and box["value"] is None


def test_formula_never_normalizes_test_specific_names():
    """规范化只处理外包装/空白，不把识别错的名称硬编码成预期答案。"""
    assert quality.formula_normalize("$$ a + b $$") == "a+b"
    assert quality.formula_normalize("Y_0Y") != quality.formula_normalize("YoY")


class TedsStub:
    """官方指标接口 Stub，仅检查调用边界，不运行真实指标或生成表格样本。"""

    def evaluate(self, prediction, ground_truth):
        """返回固定接口值，要求是完整 HTML 而非 TSV。"""
        assert prediction.startswith("<html><body><table>")
        assert ground_truth.startswith("<html><body><table>")
        return 0.5


def test_teds_requires_existing_structured_html_and_optional_backend():
    """没有 HTML/依赖时不评测；有确定结构时仅调用官方接口 Stub，不发明行列。"""
    row, document = omni_reference(), exported_document()
    region = row["original"]["layout_dets"][0]
    region.update(category_type="table", html="<table><tr><td>__stub__</td></tr></table>")
    checks, _ = quality.region_checks(row, region, document, region_match(), None)
    assert next(item for item in checks if item["criterion"] == "table_teds")["value"] is None
    document["blocks"][0].update(block_type="table", text=region["html"])
    checks, _ = quality.region_checks(row, region, document, region_match(), TedsStub())
    assert next(item for item in checks if item["criterion"] == "table_teds")["value"] == 0.5


def test_reading_order_requires_complete_unique_same_page_mapping():
    """不做 y 排序；完整人工对应才比较原 Block 顺序，跨图片页不接受。"""
    row, document, match = omni_reference(), exported_document(), region_match()
    region = copy.deepcopy(row["original"]["layout_dets"][0])
    region.update(anno_id=2, order=1)
    row["original"]["layout_dets"].append(region)
    block = copy.deepcopy(document["blocks"][0])
    block.update(block_id="schema-block-2", block_index=2)
    document["blocks"].append(block)
    second = dict(match, anno_id=2, block_ids=["schema-block-2"])
    checks = quality.omni_checks([row], {document["doc_id"]: document}, {"matches": [match, second]})
    assert next(item for item in checks if item["criterion"] == "reading_order_pair_consistency")["value"] == 1
    block["page"] = 2
    checks = quality.omni_checks([row], {document["doc_id"]: document}, {"matches": [match, second]})
    assert next(item for item in checks if item["criterion"] == "reading_order_pair_consistency")["value"] is None


def test_ocb_assertions_are_manual_and_require_all_files():
    """断言不是 OCR 真值，任何依赖缺失均不能自动给通过分数。"""
    row = ocb.qa_record(hf_row(["a.docx", "b.xlsx"]), 0, "hf", "a" * 64)
    row["dependencies"] = [asset_stub("a.docx"), asset_stub("b.xlsx")]
    checks = quality.ocb_checks([row], {})
    assert checks[0]["status"] == "not_applicable"
    assert checks[0]["value"] is None and len(checks[0]["document_identity"]) == 2


def test_chunk_evidence_uses_actual_stage13_hash_field():
    """严格消费现有 block_text_hash 和 Unicode 偏移，不重新 Chunk 或生成 Embedding。"""
    document = exported_document()
    text = document["blocks"][0]["text"]
    span = {"block_id": "schema-block", "block_index": 1, "page": 1, "start_char": 0, "end_char": len(text),
            "chunk_start_char": 0, "chunk_end_char": len(text), "block_text_hash": hashlib.sha256(text.encode()).hexdigest()}
    document["chunks"] = [{"chunk_id": "schema-chunk", "text": text, "page_start": 1, "page_end": 1,
                           "metadata": {"source_spans": [span]}}]
    assert quality.chunk_evidence(document, "ocb", "__identity__", "__reference__")[0]["status"] == "pass"
    span["start_char"] = 1
    assert quality.chunk_evidence(document, "ocb", "__identity__", "__reference__")[0]["status"] == "fail"


def test_coverage_reports_all_thirteen_and_missing_legacy():
    """十三种格式全部列出，旧 Office 缺口不因 PDF/图像数量多而隐藏。"""
    report = benchmark_coverage.coverage({"ohr": {"documents": [{"sha256": "a" * 64}]},
                                         "ocb": {"documents": [asset_stub("schema.xlsx")]},
                                         "omnidocbench": {"documents": []}})
    assert set(report["format_coverage"]) == set(common.FORMATS)
    assert report["format_coverage"]["doc"]["ocb"]["selected_count"] == 0
    assert report["stage16_preflight_passed"] is None


def test_no_overwrite_reports_and_no_manifest_write(tmp_path, monkeypatch):
    """只写 results 元数据，既有报告拒绝覆盖；业务清单保持原字节不被工具接管。"""
    monkeypatch.setattr(common, "ROOT", tmp_path)
    target = tmp_path / "docs/test/ocb_preset/results"
    target.mkdir(parents=True)
    (target / "selection_plan.json").write_text("__user_report__", encoding="utf-8")
    with pytest.raises(common.BenchmarkError, match="BENCH_EXISTS"):
        common.publish_reports("ocb", target, {}, {}, b"")
    assert (target / "selection_plan.json").read_text(encoding="utf-8") == "__user_report__"


def test_changed_plan_rejected_before_reading_assets(tmp_path, monkeypatch):
    """计划摘要变化时在真实文件读取前失败，不通过测试生成任何文档样本。"""
    monkeypatch.setattr(common, "ROOT", tmp_path)
    def plan_stub(path, beneath=None):
        """只返回错误冻结计划 Stub，不读写数据集。"""
        return {"version": "benchmark-selection-v1", "dataset": "ocb", "plan_sha256": "invalid"}
    monkeypatch.setattr(common, "json_file", plan_stub)
    with pytest.raises(common.BenchmarkError, match="BENCH_PLAN"):
        common.extract("ocb", tmp_path / "docs/test/ocb_preset/results/selection_plan.json")


def test_extract_changed_asset_rejected_before_any_publish(tmp_path, monkeypatch):
    """原件 SHA 变化先拒绝整批，不调用发布、更不写入部分业务素材或覆盖 manifest。"""
    source = {"metadata_sha256": {"dataset_info.json": "b" * 64}}
    item = asset_stub("schema.xlsx")
    refs = b"__metadata_reference_stub__"
    payload = {"version": "benchmark-selection-v1", "dataset": "ocb", "source": source,
               "gaps": {}, "documents": [item], "parameters": {"max_bytes": 1024},
               "references_sha256": hashlib.sha256(refs).hexdigest()}
    plan = payload | {"plan_sha256": common.ohr.digest(payload)}
    def json_stub(path, beneath=None):
        """提供冻结计划 Stub，不写真实 source 文件。"""
        return plan
    def source_stub(root, dataset, metadata_names):
        """模拟未变化的官方元数据摘要。"""
        return source
    def read_stub(path, limit=96 * common.MIB, beneath=None):
        """仅返回参考元数据 Stub，不读取真实 Office 文件。"""
        return refs
    def asset_changed(root, relative, dataset, max_bytes):
        """模拟文件变更后的安全校验结果，不执行格式解析。"""
        return dict(item, sha256="f" * 64)
    def forbidden_publish(target, data):
        """若错误路径试图发布业务文件，断言失败。"""
        raise AssertionError("不允许发布已变化原件")
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(common, "json_file", json_stub)
    monkeypatch.setattr(common, "source_record", source_stub)
    monkeypatch.setattr(common, "read_bytes", read_stub)
    monkeypatch.setattr(common, "asset", asset_changed)
    monkeypatch.setattr(common.ohr, "publish_exclusive", forbidden_publish)
    with pytest.raises(common.BenchmarkError, match="BENCH_PLAN"):
        common.extract("ocb", tmp_path / "docs/test/ocb_preset/results/selection_plan.json")


def test_input_read_distinguishes_missing_empty_directory_and_limit(tmp_path):
    """只用临时 JSON 元数据验证错误分类；缺失/空输入不能误报预算超限，也不泄露路径。"""
    missing = tmp_path / "metadata.json"
    with pytest.raises(common.BenchmarkError, match="BENCH_MISSING") as failure:
        common.read_bytes(missing)
    assert str(tmp_path) not in str(failure.value)
    assert missing.name not in str(failure.value)
    with pytest.raises(common.BenchmarkError, match="BENCH_SOURCE"):
        common.read_bytes(tmp_path)
    missing.write_bytes(b"")
    with pytest.raises(common.BenchmarkError, match="BENCH_EMPTY"):
        common.read_bytes(missing)
    missing.write_bytes(b"{}")
    with pytest.raises(common.BenchmarkError, match="BENCH_LIMIT"):
        common.read_bytes(missing, limit=1)
    assert common.read_bytes(missing, limit=2) == b"{}"


@pytest.mark.parametrize("code", ["MISSING", "EMPTY", "LIMIT"])
def test_json_input_preserves_read_error_classification(code, monkeypatch):
    """JSON 适配层保留读取错误码，不误改为格式错误，不触发真实资料读取。"""
    def failed_read(path, limit=96 * common.MIB, beneath=None):
        """只抛固定读取错误 Stub，不产生文件或原始异常详情。"""
        raise common.BenchmarkError(code)
    monkeypatch.setattr(common, "read_bytes", failed_read)
    with pytest.raises(common.BenchmarkError) as failure:
        common.json_file("__metadata_stub__")
    assert failure.value.code == code


@pytest.mark.parametrize("dataset", ["ocb", "omnidocbench"])
def test_cli_missing_official_inputs_explains_preparation_without_outputs(dataset, tmp_path, monkeypatch, capsys):
    """两个 CLI 遇缺元数据都安全退出并给白名单输入提示，不写报告或下载/解析素材。"""
    monkeypatch.setattr(common, "ROOT", tmp_path)
    prepare = ocb.prepare if dataset == "ocb" else omni.prepare
    assert common.run(dataset, prepare, ["--confirm-official-source"]) == 1
    output = capsys.readouterr().out
    assert "BENCH_MISSING" in output and "dataset_info.json" in output
    assert dataset in output and str(tmp_path) not in output
    assert "BENCH_LIMIT" not in output
    assert not (tmp_path / "docs/test" / (dataset + "_preset") / "results").exists()


def test_input_error_labels_never_echo_untrusted_paths():
    """任意未知输入标识不会进入异常，只有预定义技术文件名可用于人工排查。"""
    error = common.BenchmarkError("MISSING", input_id="__private_path_or_content__")
    assert error.input_id is None and "__private_path_or_content__" not in str(error)
