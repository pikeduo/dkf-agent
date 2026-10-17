"""按唯一前后layout证据拆回V1跨页合并表格；不读V2/Markdown，不发布缓存或修改任务。"""

from pathlib import PurePosixPath

from app.services.mineru.adapter import (MERGE_BOUNDARY_TYPES, MergeEvidenceError, convert_bbox,
    layout_bbox, matched_merge_boundary, merge_evidence_error, merged_text_slices, plain_layout_spans)
from app.services.mineru.table_html import TableHTML, verify_projection


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
    """附属文本须单Span且有来源坐标；被移走的后页子块须有删除标记。"""
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
        spans = plain_layout_spans(child)
        if (len(spans) != 1 or layout_bbox(spans[0], size) is None
                or spans[0].get("cross_page", False) is not False):
            raise merge_evidence_error("TABLE_EXTRAS")
        result.append(spans[0])
    return result


def restore_extras(group, evidence):
    """按唯一源Span将已合并V1 caption/footnote切回原页，不从layout添加文字。"""
    changes = [{} for _ in group]
    for kind in ("table_caption", "table_footnote"):
        source = [(index, span) for index, (_, before, size) in enumerate(evidence)
                  for span in extra_spans(before, size, kind)]
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
                if (actual["content"] == original["content"]
                        and (box := layout_bbox(actual, size)) is not None
                        and all(abs(a - b) <= 2 for a, b in zip(box, layout_bbox(original, size)))):
                    matches.append(position)
            if len(matches) != 1 or matches[0] in claimed or matches[0] != len(claimed):
                raise merge_evidence_error("TABLE_EXTRAS")
            claimed.add(matches[0])
            index, original = source[matches[0]]
            merged_text_slices(value, [original["content"]])
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


def restore_merged_tables(items, pages, *, archive_members=None):
    """恢复连续物理页的已证实合并表格；只以V1原行/附属文本构造非连续原项。

    每组最多200页/512个已验证边界；仍受HTML/网格/总字节预算约束。
    所有组先在内存完成核对，异常不返回部分结果。
    不处理跨分片的远端表格，也不把真正空表格或缺来源当成成功。
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
            while start >= 0 and items[start].get("type") in MERGE_BOUNDARY_TYPES:
                gaps.append(start)
                start -= 1
                if len(gaps) > 512:
                    raise merge_evidence_error("TABLE_LIMIT")
            if start < 0 or items[start].get("type") != "table" or not items[start].get("table_body"):
                raise merge_evidence_error("TABLE_BODY")
            indices, cursor = [start, offset], offset + 1
            while cursor < len(items):
                boundary = cursor
                while boundary < len(items) and items[boundary].get("type") in MERGE_BOUNDARY_TYPES and boundary - cursor < 64:
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
                identity = matched_merge_boundary(items[index], pages, {v["page_idx"] for v in group})
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
            if sum(len(value.rows) for value in sources) != len(merged.rows):
                raise merge_evidence_error("TABLE_ROWS")
            extras, pieces, refinements, row = restore_extras(group, evidence), [], [], 0
            for source in sources:
                piece = merged.slice_rows(row, row + len(source.rows))
                piece_shape = TableHTML(piece)
                verify_projection(source, piece_shape)
                refinements.append(source.width != piece_shape.width)
                pieces.append(piece)
                row += len(source.rows)
            for index, value, piece, extra in zip(indices, group, pieces, extras):
                restored[index] = value | {"table_body": piece} | extra
                consumed.add(index)
            summaries.append({"item_indices": [i + 1 for i in indices],
                "pages": [v["page_idx"] + 1 for v in group], "rows": [len(s.rows) for s in sources],
                "merged_rows": len(merged.rows), "cells": [sum(len(r["cells"]) for r in s.rows) for s in sources],
                "column_refinement": refinements,
                "caption_counts": [len(e["table_caption"]) for e in extras],
                "footnote_counts": [len(e["table_footnote"]) for e in extras],
                "image_references_verified": True})
        except MergeEvidenceError as error:
            error.locate(page_idx=item["page_idx"], item_index=offset + 1)
            raise
    return restored, summaries
