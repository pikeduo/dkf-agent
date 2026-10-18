"""按唯一前后layout证据拆回V1跨页合并表格；不读V2/Markdown，不发布缓存或修改任务。"""

from pathlib import PurePosixPath

from app.services.mineru.adapter import (MERGE_BOUNDARY_TYPES, PRESERVED_GAP_TYPES, MergeEvidenceError, convert_bbox,
    layout_bbox, matched_preserved_gap, merge_evidence_error, merged_text_slices,
    paragraph_spans, span_reference_text)
from app.services.mineru.table_html import TableHTML, verify_projection

# 共享文本恢复的原样结构验证，不再把旁注/页下注释/代码当作合并链的终点。
# 另一张表不是间隔，普通Text/Title和未知结构也绝不能跳过。
TABLE_GAP_TYPES = MERGE_BOUNDARY_TYPES | (PRESERVED_GAP_TYPES - {"table"})


def matched_table(item, pages, items):
    """V1/para/preproc按同页类型和bbox唯一匹配，前后父index一致且V1来源不重复。"""
    page_idx = item.get("page_idx")
    page = pages.get(page_idx) if type(page_idx) is int else None
    if not isinstance(page, dict):
        raise merge_evidence_error("TABLE_LAYOUT")
    box = convert_bbox(item.get("bbox"), (1000, 1000))
    if box is None:
        raise merge_evidence_error("TABLE_MATCH")
    matches = []
    for name in ("para_blocks", "preproc_blocks"):
        blocks = page.get(name)
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 512:
            raise merge_evidence_error("TABLE_LAYOUT")
        indices = [b.get("index") for b in blocks if isinstance(b, dict)]
        if (len(indices) != len(blocks) or any(type(i) is not int or i < 0 for i in indices)
                or len(set(indices)) != len(indices)):
            raise merge_evidence_error("TABLE_INDEX")
        found = [b for b in blocks if b.get("type") == "table"
                 and (candidate := layout_bbox(b, page.get("page_size"))) is not None
                 and all(abs(a - c) <= 2 for a, c in zip(box, candidate))]
        if len(found) != 1:
            raise merge_evidence_error("TABLE_MATCH", observed=len(found), expected=1)
        matches.append(found[0])
    if matches[0]["index"] != matches[1]["index"]:
        raise merge_evidence_error("TABLE_INDEX")
    if any(parent.get("lines_deleted", False) is not False for parent in matches):
        raise merge_evidence_error("TABLE_DELETED")
    same = 0
    for value in items:
        if value.get("type") == "table" and value.get("page_idx") == page_idx:
            candidate = convert_bbox(value.get("bbox"), (1000, 1000))
            same += candidate is not None and all(abs(a - c) <= 2 for a, c in zip(box, candidate))
    if same != 1:
        raise merge_evidence_error("TABLE_AMBIGUOUS")
    return *matches, page["page_size"]


def children(parent):
    """只接受一个表体及已知caption/footnote子块，未知附属结构不忽略。"""
    blocks = parent.get("blocks")
    if (not isinstance(blocks, list) or not 1 <= len(blocks) <= 32
            or any(not isinstance(b, dict) or b.get("type") not in
                   {"table_body", "table_caption", "table_footnote"} for b in blocks)
            or sum(b["type"] == "table_body" for b in blocks) != 1):
        raise merge_evidence_error("TABLE_LAYOUT")
    indices = [b.get("index") for b in blocks]
    if any(type(i) is not int or i < 0 for i in indices) or len(indices) != len(set(indices)):
        raise merge_evidence_error("TABLE_INDEX")
    return blocks


def body(parent, size, *, deleted=False):
    """核对table_body的删除标记/原HTML/Span/bbox；删除占位必须真正无lines。"""
    child = next(b for b in children(parent) if b["type"] == "table_body")
    child_box, parent_box = layout_bbox(child, size), layout_bbox(parent, size)
    if child["index"] != parent["index"] or child_box is None or parent_box is None:
        raise merge_evidence_error("TABLE_INDEX")
    if (child_box[0] < parent_box[0] - 2 or child_box[1] < parent_box[1] - 2
            or child_box[2] > parent_box[2] + 2 or child_box[3] > parent_box[3] + 2):
        raise merge_evidence_error("TABLE_MATCH")
    if deleted:
        if child.get("lines_deleted") is not True or child.get("lines") != []:
            raise merge_evidence_error("TABLE_DELETED")
        return None
    lines = child.get("lines")
    if (child.get("lines_deleted", False) is not False or not isinstance(lines, list) or len(lines) != 1
            or not isinstance(lines[0], dict) or not isinstance(lines[0].get("spans"), list)
            or len(lines[0]["spans"]) != 1):
        raise merge_evidence_error("TABLE_BODY")
    span = lines[0]["spans"][0]
    if (not isinstance(span, dict) or span.get("type") != "table"
            or not isinstance(span.get("html"), str) or not span["html"].strip()
            or span.get("cross_page", False) is not False or layout_bbox(span, size) is None
            or any(abs(a - c) > 2 for a, c in zip(layout_bbox(span, size), layout_bbox(child, size)))):
        raise merge_evidence_error("TABLE_BODY")
    return span


def extra_spans(parent, size, kind, *, deleted=False):
    """附属文本保留index、文字/行内公式及坐标；删除后页须有明确空lines标记。"""
    result = []
    for child in children(parent):
        if child["type"] != kind:
            continue
        if deleted:
            if child.get("lines_deleted") is not True or child.get("lines") != []:
                raise merge_evidence_error("TABLE_EXTRAS")
            continue
        if child.get("lines_deleted", False) is not False or layout_bbox(child, size) is None:
            raise merge_evidence_error("TABLE_EXTRAS")
        spans = paragraph_spans(child)
        boxes = [layout_bbox(span, size) for span in spans]
        if (any(box is None for box in boxes)
                or any(span.get("cross_page", False) is not False for span in spans)):
            raise merge_evidence_error("TABLE_EXTRAS")
        envelope = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                    max(box[2] for box in boxes), max(box[3] for box in boxes)]
        if any(abs(a - b) > 2 for a, b in zip(envelope, layout_bbox(child, size))):
            raise merge_evidence_error("TABLE_EXTRAS")
        result.append({"index": child["index"], "bbox": child["bbox"], "parts": spans})
    return result


def standalone_extra(original, kind, page, items):
    """证明表格附注已转成同页独立V1正文；不搬动、不补字、不重复写入表格。

    原caption/footnote与独立para必须index/bbox/每个Span一致，V1也须唯一同页
    同框且全文匹配。找不到独立para时仍由原表格附注检查负责，歧义或冲突拒绝。
    """
    size = page["page_size"]
    box = layout_bbox(original, size)
    parents = [b for b in page["para_blocks"]
               if b.get("type") == "text" and b.get("index") == original["index"]
               and (candidate := layout_bbox(b, size)) is not None
               and all(abs(a - b) <= 2 for a, b in zip(candidate, box))]
    if not parents:
        return False
    if len(parents) != 1 or parents[0].get("lines_deleted", False) is not False:
        raise merge_evidence_error("TABLE_EXTRAS")
    parent = parents[0]
    # 同页另一表格不能复用同一个附注来源；只接受唯一原子块身份。
    sources = [child for root in page["preproc_blocks"] if root.get("type") == "table"
               for child in (root["blocks"] if isinstance(root.get("blocks"), list) else []) if isinstance(child, dict)
               and child.get("type") == kind and child.get("index") == original["index"]
               and (candidate := layout_bbox(child, size)) is not None
               and all(abs(a - b) <= 2 for a, b in zip(candidate, box))]
    if len(sources) != 1:
        raise merge_evidence_error("TABLE_EXTRAS")
    moved = paragraph_spans(parent)
    if len(moved) != len(original["parts"]):
        raise merge_evidence_error("TABLE_EXTRAS")
    for actual, source in zip(moved, original["parts"]):
        actual_box, source_box = layout_bbox(actual, size), layout_bbox(source, size)
        if (actual.get("type") != source["type"] or actual.get("content") != source["content"]
                or actual.get("cross_page", False) is not False or actual_box is None
                or any(abs(a - b) > 2 for a, b in zip(actual_box, source_box))):
            raise merge_evidence_error("TABLE_EXTRAS")
    candidates = [value for value in items if value.get("type") == "text"
                  and value.get("page_idx") == page["page_idx"]
                  and (value_box := convert_bbox(value.get("bbox"), (1000, 1000))) is not None
                  and all(abs(a - b) <= 2 for a, b in zip(value_box, box))]
    if (len(candidates) != 1 or not isinstance(candidates[0].get("text"), str)
            or not candidates[0]["text"].strip()
            or type(candidates[0].get("text_level", 0)) is not int
            or candidates[0].get("text_level", 0) != 0):
        raise merge_evidence_error("TABLE_EXTRAS")
    merged_text_slices(candidates[0]["text"], [span_reference_text(span) for span in original["parts"]])
    return True


def restore_extras(group, evidence, *, items=None, pages=None):
    """原V1附注归还原页；已唯一转为独立Text的附注保持原样，不补字或重复挂表。"""
    changes = [{} for _ in group]
    for kind in ("table_caption", "table_footnote"):
        source = [(index, span) for index, (_, before, size) in enumerate(evidence)
                  for span in extra_spans(before, size, kind)
                  if items is None or pages is None
                  or not standalone_extra(span, kind, pages[group[index]["page_idx"]], items)]
        moved = extra_spans(evidence[0][0], evidence[0][2], kind)
        for after, _, size in evidence[1:]:
            extra_spans(after, size, kind, deleted=True)
        values = group[0].get(kind, [])
        if (not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(values) != len(source) or len(moved) != len(source)):
            raise merge_evidence_error("TABLE_EXTRAS")
        # V1为每条已删除附注保留一个空串；多个空串不是正文，也不能忽略非空附注。
        if any(not isinstance(value.get(kind, []), list)
               or any(part != "" for part in value.get(kind, [])) for value in group[1:]):
            raise merge_evidence_error("TABLE_EXTRAS")
        claimed = set()
        for value, actual in zip(values, moved):
            matches = []
            for position, (index, original) in enumerate(source):
                size = evidence[index][2]
                if ([(s["type"], s["content"]) for s in actual["parts"]]
                        == [(s["type"], s["content"]) for s in original["parts"]]
                        and (box := layout_bbox(actual, size)) is not None
                        and all(abs(a - b) <= 2 for a, b in zip(box, layout_bbox(original, size)))
                        and all((moved_box := layout_bbox(moved_span, size)) is not None
                                and (source_box := layout_bbox(source_span, size)) is not None
                                and all(abs(a - b) <= 2 for a, b in zip(moved_box, source_box))
                                for moved_span, source_span in zip(actual["parts"], original["parts"]))):
                    matches.append(position)
            if len(matches) != 1 or matches[0] in claimed or matches[0] != len(claimed):
                raise merge_evidence_error("TABLE_EXTRAS")
            claimed.add(matches[0])
            index, original = source[matches[0]]
            merged_text_slices(value, [span_reference_text(span) for span in original["parts"]])
            changes[index].setdefault(kind, []).append(value)
        for change in changes:
            change.setdefault(kind, [])
    return changes


def image_name(value):
    """仅接受相对ZIP资源身份，不读取链接/URL或用文件名猜表格归属。"""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise merge_evidence_error("TABLE_IMAGES")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise merge_evidence_error("TABLE_IMAGES")
    return path.name


def verify_images(group, originals, target, members):
    """图像必须唯一存在于原ZIP，目标图仍指向首源图；不伪造后页单独图像引用。"""
    names = [image_name(span.get("image_path")) for span in originals]
    if image_name(target.get("image_path")) != names[0]:
        raise merge_evidence_error("TABLE_IMAGES")
    for index, (value, expected) in enumerate(zip(group, names)):
        actual = image_name(value.get("img_path"))
        if actual is not None and actual != expected or index == 0 and actual != expected:
            raise merge_evidence_error("TABLE_IMAGES")
        if expected is not None and (members is None or
                sum(PurePosixPath(name).name == expected for name in members) != 1):
            raise merge_evidence_error("TABLE_IMAGES")
    if len([n for n in names if n]) != len({n for n in names if n}):
        raise merge_evidence_error("TABLE_IMAGES")


def partition_rows(merged, sources):
    """按完整网格投影分配V1行，兼容云端去除跨页重复页首行，不猜任意缺行。

    原行数相等时保留原切片行为。行数减少时，只允许后一页的连续前缀与前页
    已验证前缀一致，并复用对应V1原行；其他行逐一消耗，最终必须用尽全部V1。
    页首复用最多64行，任一来源有多个可行映射或跨rowspan切口均安全拒绝。
    """
    pieces, refinements, reused, previous = [], [], [], []
    cursor = 0
    if sum(len(source.rows) for source in sources) == len(merged.rows):
        # 已接受的连续映射保留原错误分类和字节切片，不因新策略改变成功缓存语义。
        for source in sources:
            stop = cursor + len(source.rows)
            piece = merged.slice_rows(cursor, stop)
            shape = TableHTML(piece)
            verify_projection(source, shape)
            pieces.append(piece)
            refinements.append(source.width != shape.width)
            reused.append(0)
            cursor = stop
        return pieces, refinements, reused
    for ordinal, source in enumerate(sources):
        choices = []
        limit = min(64, len(previous), len(source.rows))
        for prefix in range(limit + 1):
            stop = cursor + len(source.rows) - prefix
            if stop > len(merged.rows) or ordinal + 1 == len(sources) and stop != len(merged.rows):
                continue
            indices = previous[:prefix] + list(range(cursor, stop))
            try:
                if prefix:
                    verify_projection(TableHTML(source.slice_rows(0, prefix)),
                                      TableHTML(merged.select_rows(previous[:prefix])))
                # 无重复前缀时仍使用旧的连续原字节切片，已接受结果不变。
                piece = (merged.slice_rows(cursor, stop) if not prefix
                         else merged.select_rows(indices))
                shape = TableHTML(piece)
                verify_projection(source, shape)
            except MergeEvidenceError as error:
                if error.diagnostic["reason"] not in {"TABLE_ROWS", "TABLE_CELLS", "TABLE_GRID"}:
                    raise
                continue
            choices.append((piece, shape, prefix, stop, indices))
            if len(choices) > 1:
                raise merge_evidence_error("TABLE_ROW_MAPPING_AMBIGUOUS")
        if not choices:
            raise merge_evidence_error("TABLE_ROWS", observed=len(merged.rows),
                                       expected=sum(len(value.rows) for value in sources))
        piece, shape, prefix, cursor, previous = choices[0]
        pieces.append(piece)
        refinements.append(source.width != shape.width)
        reused.append(prefix)
    if cursor != len(merged.rows):
        raise merge_evidence_error("TABLE_ROWS", observed=cursor, expected=len(merged.rows))
    return pieces, refinements, reused


def restore_merged_tables(items, pages, *, archive_members=None):
    """恢复连续物理页的已证实合并表格；只以V1原行/附属文本构造非连续原项。

    每组最多200页/512个已验证边界；仍受HTML/网格/总字节预算约束。
    所有组先在内存完成核对，异常不返回部分结果。
    允许原样旁注等非连续间隔和已验证的重复页首行；附注可保留为独立Text。
    不处理跨分片表格，不忽略真缺失来源或以layout补写V1没有的文字。
    """
    restored, summaries, consumed = list(items), [], set()
    for offset, item in enumerate(items):
        if offset in consumed or item.get("type") != "table" or item.get("table_body"):
            continue
        if type(item.get("page_idx")) is not int or item["page_idx"] not in pages:
            continue
        try:
            page = pages[item["page_idx"]]
            box = convert_bbox(item.get("bbox"), (1000, 1000))
            candidates = page.get("para_blocks", [])
            if not isinstance(candidates, list) or len(candidates) > 512:
                raise merge_evidence_error("TABLE_LAYOUT")
            marked = [parent for parent in candidates if isinstance(parent, dict)
                and parent.get("type") == "table" and box is not None
                and (candidate_box := layout_bbox(parent, page.get("page_size"))) is not None
                and all(abs(a - c) <= 2 for a, c in zip(box, candidate_box))
                and isinstance(parent.get("blocks"), list)
                and any(isinstance(child, dict) and child.get("type") == "table_body"
                        and child.get("lines_deleted") is True for child in parent["blocks"])]
            if not marked:
                continue
            candidate = matched_table(item, pages, items)
            marker = next(b for b in children(candidate[0]) if b["type"] == "table_body")
            # 无明确删除标记的真空表格仍交给EMPTY_BLOCK，不用其他页面拼凑正文。
            if marker.get("lines_deleted") is not True:
                continue
            start, gaps = offset - 1, []
            while start >= 0 and items[start].get("type") in TABLE_GAP_TYPES:
                gaps.append(start)
                start -= 1
                if len(gaps) > 512:
                    raise merge_evidence_error("TABLE_LIMIT")
            if start < 0 or items[start].get("type") != "table" or not items[start].get("table_body"):
                raise merge_evidence_error("TABLE_BODY")
            indices, cursor = [start, offset], offset + 1
            while cursor < len(items):
                boundary = cursor
                while boundary < len(items) and items[boundary].get("type") in TABLE_GAP_TYPES and boundary - cursor < 64:
                    boundary += 1
                if (boundary >= len(items) or items[boundary].get("type") != "table"
                        or items[boundary].get("table_body")):
                    break
                indices.append(boundary)
                gaps.extend(range(cursor, boundary))
                if len(indices) > 200 or len(gaps) > 512:
                    raise merge_evidence_error("TABLE_LIMIT")
                cursor = boundary + 1
            group = [items[i] for i in indices]
            if any(type(v.get("page_idx")) is not int or v["page_idx"] != group[0]["page_idx"] + i
                   for i, v in enumerate(group)):
                raise merge_evidence_error("TABLE_PAGES")
            used = set()
            for index in gaps:
                identity = matched_preserved_gap(items[index], pages, {v["page_idx"] for v in group})
                if identity in used:
                    raise merge_evidence_error("TABLE_AMBIGUOUS")
                used.add(identity)
            evidence = [matched_table(value, pages, items) for value in group]
            originals = [body(before, size) for _, before, size in evidence]
            target = body(evidence[0][0], evidence[0][2])
            if any(abs(a - b) > 2 for a, b in zip(layout_bbox(target, evidence[0][2]),
                                                 layout_bbox(originals[0], evidence[0][2]))):
                raise merge_evidence_error("TABLE_MATCH")
            for after, before, size in evidence[1:]:
                body(after, size, deleted=True)
                a = next(b for b in children(after) if b["type"] == "table_body")
                b = next(b for b in children(before) if b["type"] == "table_body")
                if any(abs(x - y) > 2 for x, y in zip(layout_bbox(a, size), layout_bbox(b, size))):
                    raise merge_evidence_error("TABLE_MATCH")
            verify_images(group, originals, target, archive_members)
            if sum(len(span["html"]) for span in [target, *originals]) > 8 * 1024**2:
                raise merge_evidence_error("TABLE_LIMIT")
            merged = TableHTML(group[0]["table_body"])
            # 先证明V1与para整个表格的网格/单元格/公式一致，再切V1原字符行。
            # 不再要求两种官方序列化的HTML逐字相同；未知标签或文字差异仍失败。
            verify_projection(TableHTML(target["html"]), merged)
            sources = [TableHTML(span["html"]) for span in originals]
            pieces, refinements, reused = partition_rows(merged, sources)
            extras = restore_extras(group, evidence, items=items, pages=pages)
            for index, value, piece, extra in zip(indices, group, pieces, extras):
                restored[index] = value | {"table_body": piece} | extra
                consumed.add(index)
            summaries.append({"item_indices": [i + 1 for i in indices],
                "pages": [v["page_idx"] + 1 for v in group], "rows": [len(s.rows) for s in sources],
                "merged_rows": len(merged.rows), "cells": [sum(len(r["cells"]) for r in s.rows) for s in sources],
                "column_refinement": refinements,
                "reused_prefix_rows": reused,
                "caption_counts": [len(e["table_caption"]) for e in extras],
                "footnote_counts": [len(e["table_footnote"]) for e in extras],
                "image_references_verified": True})
        except MergeEvidenceError as error:
            error.locate(page_idx=item["page_idx"], item_index=offset + 1)
            raise
    return restored, summaries
