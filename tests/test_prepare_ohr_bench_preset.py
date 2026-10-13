"""离线抽样工具单元用例，仅用户执行；纯字段/ZipInfo/字节 Stub，不生成 PDF 或验收样本。"""

import copy
import json
import stat
from zipfile import ZipInfo

import pytest

from scripts import prepare_ohr_bench_preset as tool


def schema_row(identifier="schema-id", name="finance/schema_stub", category="table", pages=0):
    """只构造字段契约 Stub；占位问题/答案不是官方 QA，也不是任何事实验收数据。"""

    return {"doc_name": name, "ID": identifier, "questions": "__schema_stub__",
            "answers": "__schema_stub__", "doc_type": name.split("/")[0],
            "evidence_source": category, "evidence_page_no": pages}


def zip_info(name, size=128, mode=stat.S_IFREG):
    """构造带 ZipFile 读取阶段字段的中央目录替身，不创建真实 ZIP/PDF 或写样本。"""

    info = ZipInfo(name)
    info.file_size = size
    info.compress_size = size
    info.CRC = 123
    # ZipInfo 构造函数不会设置此字段；真实 ZipFile 从中央目录读取时才赋值。
    info.header_offset = 0
    info.external_attr = (mode | 0o644) << 16
    return info


class ArchiveStub:
    """只提供内存目录元数据，不读取、下载或解压实际数据集。"""

    def __init__(self, infos):
        """保存元数据列表，不建立文件句柄。"""

        self.infos = infos

    def infolist(self):
        """返回中央目录 Stub，不访问文件系统。"""

        return self.infos


@pytest.mark.parametrize("name", ["../outside.pdf", "/outside.pdf", "C:/outside.pdf", "a\\b.pdf",
                                 "a//b.pdf", "a/./b.pdf", "CON.pdf", "a./b.pdf", "."])
def test_zip_path_rejection(name):
    """ZIP 路径越界、Windows 特殊名及归一化歧义必须安全拒绝。"""

    with pytest.raises(tool.PreparationError, match="OHR_PATH") as error:
        tool.safe_name(name)
    assert name not in str(error.value)


def test_archive_rejects_symlink_encryption_and_duplicates():
    """符号链接、加密项、重复名或大小写冲突不能参与抽样或复制。"""

    encrypted = zip_info("root/finance/schema_stub.pdf")
    encrypted.flag_bits = 1
    bad_archives = [ArchiveStub([zip_info("linked.pdf", mode=stat.S_IFLNK)]), ArchiveStub([encrypted]),
                    ArchiveStub([zip_info("root/a.pdf"), zip_info("root/a.pdf")]),
                    ArchiveStub([zip_info("root/A.pdf"), zip_info("root/a.pdf")])]
    for archive in bad_archives:
        with pytest.raises(tool.PreparationError, match="OHR_ZIP"):
            tool.archive_index(archive)


def test_zip_matching_uses_full_domain_identity_and_does_not_guess_prefix():
    """任意安全顶层下仍按完整领域/文档身份匹配，多个匹配或仅同 basename 均拒绝。"""

    groups, rejected = tool.qa_rows([schema_row()])
    assert rejected == {}
    limits = tool.Limits()
    pdfs, _ = tool.archive_index(ArchiveStub([zip_info("actual-root/finance/schema_stub.pdf")]))
    pool, failures = tool.candidates(groups, pdfs, limits, tool.DOMAINS)
    assert len(pool) == 1 and not failures and pool[0]["zip_member"].startswith("actual-root/")
    pdfs["second-root/finance/schema_stub.pdf"] = zip_info("second-root/finance/schema_stub.pdf")
    pool, failures = tool.candidates(groups, pdfs, limits, tool.DOMAINS)
    assert not pool and failures[0]["reason"] == "ambiguous_pdf_path"
    pool, failures = tool.candidates(groups, {"manual/schema_stub.pdf": zip_info("manual/schema_stub.pdf")}, limits, tool.DOMAINS)
    assert not pool and failures[0]["reason"] == "missing_pdf"


def test_archive_stub_preserves_explicit_header_offset():
    """中央目录替身显式提供非零偏移，生产摘要应保留该值而不是为了测试填默认值。"""
    info = zip_info("root/finance/schema_stub.pdf")
    info.header_offset = 8192
    pdfs, fingerprint = tool.archive_index(ArchiveStub([info]))
    assert pdfs[info.filename] is info
    assert fingerprint == tool.digest([{"name": info.filename, "size": info.file_size,
        "compressed": info.compress_size, "crc": info.CRC, "offset": 8192, "attr": info.external_attr}])


def test_qa_preserves_zero_based_pages_answers_and_rejects_duplicate_ids():
    """0 页有效，多页原序与答案原值保留；布尔页码、负数和重复 ID 不被强制转换。"""

    row = schema_row(pages=[0, 2])
    groups, failures = tool.qa_rows([row])
    assert not failures
    assert groups[row["doc_name"]][0]["original"] == row
    assert groups[row["doc_name"]][0]["pages"] == (0, 2)
    for page in (True, -1, "1", []):
        groups, failures = tool.qa_rows([schema_row(pages=page)])
        assert not groups and failures["invalid_evidence_pages"] == 1
    groups, failures = tool.qa_rows([row, copy.deepcopy(row)])
    assert not groups and failures["duplicate_or_invalid_id"] == 2


def metadata_pool():
    """提供七领域/五证据类别的虚拟元数据，不声明它们是实际官方文件。"""

    pool = []
    for index, domain in enumerate(sorted(tool.DOMAINS)):
        identity = f"{domain}/schema_stub_{index}"
        rows, _ = tool.qa_rows([schema_row(f"stub-{index}-{category}-{number}", identity, category, 0)
                               for category in sorted(tool.EVIDENCE) for number in range(2)])
        pool.append({"doc_name": identity, "domain": domain, "zip_member": "root/" + identity + ".pdf",
                     "size_bytes": 128, "crc32": 123, "qas": rows[identity]})
    return pool


def marker_reader(archive, member, limit):
    """仅返回成员身份的字节标记用于摘要断言，不构造 PDF 内容。"""

    return member.encode("utf-8")


def metadata_inspector(data, max_pages):
    """只返回虚拟元数据页数，不调用 PyMuPDF、Parser 或渲染器。"""

    return 3


def test_seed_selection_is_stable_under_input_permutation():
    """相同种子、QA/ZIP 身份及限制，改变输入数组顺序不改变选中原始身份和 QA。"""

    pool, limits = metadata_pool(), tool.Limits()
    selected, failures, inspection = tool.select_documents(pool, None, limits,
        inspector=metadata_inspector, reader=marker_reader)
    repeated, _, _ = tool.select_documents(list(reversed(pool)), None, limits,
        inspector=metadata_inspector, reader=marker_reader)
    assert selected == repeated and len(selected) == 7 and not failures
    assert len({row["domain"] for row in selected}) == 7
    assert inspection["inspected_files"] == 7


def test_bad_metadata_duplicate_content_and_page_limit_report_gaps():
    """重复内容与无效结构不凑文档数，超过总页数时明确报告，不拆 PDF 伪造更多样本。"""

    def duplicate_reader(archive, member, limit):
        """所有身份共享一个非 PDF 字节标记，模拟重复内容摘要。"""

        return b"__same_digest_stub__"

    selected, failures, _ = tool.select_documents(metadata_pool(), None, tool.Limits(),
        inspector=metadata_inspector, reader=duplicate_reader)
    assert len(selected) == 1 and len(failures) == 6

    def invalid_inspector(data, max_pages):
        """模拟坏 PDF 的安全错误，不读取或产生真实文件。"""

        raise tool.PreparationError("OHR_PDF")

    selected, failures, _ = tool.select_documents(metadata_pool(), None, tool.Limits(),
        inspector=invalid_inspector, reader=marker_reader)
    assert not selected and all(row["reason"] == "OHR_PDF" for row in failures)
    selected, _, _ = tool.select_documents(metadata_pool(), None, tool.Limits(max_total_pages=3),
        inspector=metadata_inspector, reader=marker_reader)
    assert len(selected) == 1


def test_report_preserves_ground_truth_without_guessing_document_tags():
    """真实字段只由输入行传递，0-based/1-based 显式区分，QA 类别不自动填写业务标签。"""

    limits = tool.Limits()
    selected, _, inspection = tool.select_documents(metadata_pool(), None, limits,
        inspector=metadata_inspector, reader=marker_reader)
    source = {"qa_sha256": "a" * 64}
    plan, report, reference = tool.build_artifacts(selected, source, limits, tool.DOMAINS,
                                                  7, [], {}, inspection)
    rows = [json.loads(line) for line in reference.splitlines()]
    assert not tool.has_gaps(plan["gaps"])
    assert all(item["manifest_candidate"]["tags"] == [] for item in plan["documents"])
    assert all(set(item["manifest_candidate"]) == {"file", "tags"} for item in plan["documents"])
    assert all(row["answers"] == "__schema_stub__" for row in rows)
    assert all(row["evidence_page_no"] == 0 and row["_ohr_reference"]["evidence_pages_1based"] == [1] for row in rows)
    assert report["estimated_cloud_upper_bound"]["actual_cloud_calls"] == 0
    assert "READY" in report["not_verified"] and "问答准确率" in report["not_verified"]


def test_cli_permissions_reject_before_any_offline_file_read(monkeypatch):
    """未确认官方来源/本地使用权限时不读数据、不复制原件，也不请求网络或运行业务。"""

    def forbidden(*args, **kwargs):
        """任何预览/提取尝试都说明许可边界被绕过。"""

        pytest.fail("未确认权限不应运行准备步骤")

    monkeypatch.setattr(tool, "preview", forbidden)
    monkeypatch.setattr(tool, "extract", forbidden)
    assert tool.main([]) == 1
    assert tool.main(["--extract", "--confirm-official-source"]) == 1


def test_exclusive_publish_refuses_existing_file_without_overwriting(tmp_path):
    """仅用户执行时创建纯 JSON 哨兵文件，不生成 ZIP/PDF；既有文件必须原样保留。"""

    target = tmp_path / "selection_plan.json"
    target.write_bytes(b'{"user_owned":true}')
    with pytest.raises(tool.PreparationError, match="OHR_EXISTS"):
        tool.publish_exclusive(target, b'{"replacement":true}')
    assert target.read_bytes() == b'{"user_owned":true}'


def test_budget_cannot_expand_to_unbounded_pdf_reads():
    """文件数、页数和读取预算必须显式受限，不为覆盖五类证据而无界扫描。"""

    for limits in (tool.Limits(count=10), tool.Limits(max_pages=201),
                   tool.Limits(max_inspect_bytes=513 * tool.MIB), tool.Limits(qas_per_type=11)):
        with pytest.raises(tool.PreparationError, match="OHR_LIMIT"):
            limits.validate()


def test_unknown_qa_categories_and_invalid_source_identity_are_reported():
    """不识别的类别/来源不能为了补齐覆盖而猜测成五类证据或其他领域。"""

    groups, rejected = tool.qa_rows([schema_row("unknown-category", category="unknown"),
        schema_row("wrong-domain", name="finance/schema_stub") | {"doc_type": "manual"}])
    assert not groups and rejected["unknown_evidence_source"] == 1
    assert rejected["ambiguous_source_identity"] == 1


def test_modified_plan_identity_and_duplicate_sources_are_rejected():
    """计划意外改动直接拒绝；即使重新生成摘要，也不能重复身份/内容或更改真实目标路径。"""

    limits = tool.Limits()
    selected, _, inspection = tool.select_documents(metadata_pool(), None, limits,
        inspector=metadata_inspector, reader=marker_reader)
    source = {"qa_sha256": "a" * 64, "official_urls": tool.SOURCES,
              "official_source_confirmed_by_user": True, "evidence_page_base": 0}
    plan, _, _ = tool.build_artifacts(selected, source, limits, tool.DOMAINS, 7, [], {}, inspection)
    assert tool.checked_plan(plan) == limits
    corrupted = copy.deepcopy(plan)
    corrupted["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(tool.PreparationError, match="OHR_PLAN"):
        tool.checked_plan(corrupted)
    duplicate = copy.deepcopy(plan)
    duplicate["documents"][1] = copy.deepcopy(duplicate["documents"][0])
    duplicate["plan_sha256"] = tool.digest({key: value for key, value in duplicate.items() if key != "plan_sha256"})
    with pytest.raises(tool.PreparationError, match="OHR_PLAN"):
        tool.checked_plan(duplicate)


def test_candidate_inventory_does_not_claim_unchecked_pdf_metadata():
    """只读中央目录候选中真实页数/SHA 必须未知，QA 最大页仅是下界。"""

    row = tool.inventory_entry(metadata_pool()[0])
    assert row["actual_pdf_page_count"] is None and row["sha256"] is None
    assert row["minimum_pages_implied_by_qa"] == 1


def test_actual_pdf_page_range_is_required_for_every_selected_qa():
    """中央目录和 QA 下界不证明真实页数，超出结构 Stub 页范围的 QA 必须拒绝。"""

    pool = metadata_pool()
    for item in pool:
        for row in item["qas"]:
            row["pages"] = (9,)
    selected, failures, inspection = tool.select_documents(pool, None, tool.Limits(),
        inspector=metadata_inspector, reader=marker_reader)
    assert not selected and inspection["inspected_files"] == 7
    assert all(row["reason"] == "no_qa_within_real_pdf_pages" for row in failures)
    _, report, reference = tool.build_artifacts(selected, {}, tool.Limits(), tool.DOMAINS,
                                               7, failures, {}, inspection)
    assert not report["verified_coverage"]["pdf_structure_crc_sha_and_qa_page_ranges"]
    assert reference == b""


def test_official_text_maps_only_internal_category():
    """官方 text 映射内部 plain_text，原始问题、答案、字段与对象内容不被修改。"""
    raw = schema_row("text-stub", category="text")
    before = copy.deepcopy(raw)
    groups, rejected = tool.qa_rows([raw])
    item = groups[raw["doc_name"]][0]
    assert not rejected and item["category"] == "plain_text"
    assert item["original"] == before and raw == before
    assert item["original"]["evidence_source"] == "text"


def test_multi_is_separate_not_guessed_as_five_categories():
    """multi 明确记录未支持，不变成未知类别、五类问题或伪造的多个分项 QA。"""
    rows = [schema_row("multi-stub", category="multi"), schema_row("unknown-stub", category="other")]
    groups, rejected = tool.qa_rows(rows)
    assert groups == {}
    assert rejected == {"multi_evidence_unsupported": 1, "unknown_evidence_source": 1}
    inventory = tool.source_qa_inventory(rows)
    assert inventory["raw_evidence_source_counts"]["multi"] == 1
    assert inventory["multi_policy"] == "reported_separately_not_mapped_not_selected"


def test_six_domains_report_zero_newspaper_without_manufacturing_qa():
    """用户选择六领域时不要求虚构 Newspaper，仍明确记录源零 QA、排除及未覆盖。"""
    requested = tool.DOMAINS - {"newspaper"}
    pool = [item for item in metadata_pool() if item["domain"] in requested]
    extra_rows, _ = tool.qa_rows([schema_row("extra-text", "finance/extra_schema", "text")])
    pool.append({"doc_name": "finance/extra_schema", "domain": "finance",
                 "zip_member": "root/finance/extra_schema.pdf", "size_bytes": 128,
                 "crc32": 123, "qas": extra_rows["finance/extra_schema"]})
    raw_rows = [row["original"] for item in pool for row in item["qas"]]
    raw_rows.append(schema_row("multi-stub", category="multi"))
    inventory = tool.source_qa_inventory(raw_rows)
    selected, failures, inspection = tool.select_documents(pool, None, tool.Limits(),
        inspector=metadata_inspector, reader=marker_reader)
    source = {"qa_sha256": "a" * 64}
    plan, report, refs = tool.build_artifacts(selected, source, tool.Limits(), requested,
        len(pool), failures, {"multi_evidence_unsupported": 1}, inspection, qa_inventory=inventory)
    assert len(plan["documents"]) == 7 and plan["gaps"]["domain_shortfall"] == 0
    assert inventory["raw_domain_counts"]["newspaper"] == 0
    assert report["domain_coverage"]["source_domains_without_qa"] == ["newspaper"]
    assert report["domain_coverage"]["excluded_by_user"] == ["newspaper"]
    assert "newspaper" in report["domain_coverage"]["not_selected"]
    assert plan["sources"] == source  # 统计不改旧来源摘要契约。
    references = [json.loads(line) for line in refs.splitlines()]
    assert all(row["doc_type"] != "newspaper" for row in references)
    text = next(row for row in references if row["evidence_source"] == "text")
    assert text["_ohr_reference"]["category"] == "plain_text"


def test_default_seven_domains_still_reports_domain_shortfall():
    """使用完整来源摘要 Stub 验证七领域目标；缺 Newspaper 仍记缺口，不改变用户范围。"""
    pool = [item for item in metadata_pool() if item["domain"] != "newspaper"]
    selected, failures, inspection = tool.select_documents(pool, None, tool.Limits(count=6),
        inspector=metadata_inspector, reader=marker_reader)
    # 七份目标仍要求尽量覆盖七领域；这里仅构造元数据，不生成或补齐源文件。
    # 选中 QA 的来源链必需此字段；占位摘要仅用于 Mock，不代表真实官方文件。
    source = {"qa_sha256": "a" * 64}
    _, report, references = tool.build_artifacts(selected, source, tool.Limits(count=7), tool.DOMAINS,
        len(pool), failures, {}, inspection)
    assert report["gaps"]["domain_shortfall"] == 1
    assert tool.has_gaps(report["gaps"])
    assert all(row["_ohr_reference"]["source_qa_sha256"] == source["qa_sha256"]
               for row in (json.loads(line) for line in references.splitlines()))


def test_cli_can_choose_six_domains_and_run03_without_touching_old_plan(monkeypatch):
    """新命令明确选择六领域和 run-03；Stub 校验只传递参数，不运行预览或读取 PDF。"""
    seen = {}

    def preview_stub(qa_path, zip_path, results, limits, domains):
        """捕获运行参数，不创建目录、报告或真实文档样本。"""
        seen.update(results=results, domains=domains)
        return {"candidates_count": 0, "selected_count": 0, "gaps": {"document_count_shortfall": 7}}

    monkeypatch.setattr(tool, "preview", preview_stub)
    from types import SimpleNamespace
    from app.core import config

    def settings_stub():
        """只提供上传大小保护，不读取用户配置或连接业务服务。"""
        return SimpleNamespace(max_upload_size_mb=50)

    monkeypatch.setattr(config, "get_settings", settings_stub)
    requested = sorted(tool.DOMAINS - {"newspaper"})
    assert tool.main(["--confirm-official-source", "--domains", *requested,
        "--results-dir", "docs/test/ohr_bench_preset/results/run-03"]) == 2
    assert seen["domains"] == set(requested) and seen["results"].name == "run-03"
