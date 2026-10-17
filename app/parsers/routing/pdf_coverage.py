"""PDF 覆盖白名单：只识别页边缘的单条中性灰细横线，未知绘图仍需解析。"""

from math import isfinite


def coordinates(value, length):
    """读取受限几何元组；缺字段、非有限数或未知形状均不作为装饰证据。"""
    if isinstance(value, (str, bytes)):
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if len(result) == length and all(isfinite(item) for item in result) else None


def margin_rule_only(page, drawings) -> bool:
    """严格识别整页仅含的细横分隔线，不依据绘图数量、文件名或缺失云结果放宽覆盖。

    白名单仅支持未旋转页面：所有绘制都在顶部/底部 10% 内同一个至多 2pt
    高的横带，横跨页面至少 80%；只接受不透明灰色填充长矩形及其端部直线封口。
    额外文字（包括隐藏文字）、图片、批注、曲线、阴影、未知绘制或结构都保守拒绝。
    这些阈值描述特定装饰的几何形状，不是图表重要性/语义置信度或绘图数量阈值。
    """
    try:
        if (not drawings or len(drawings) > 32 or page.rotation != 0
                or page.get_texttrace() or page.get_image_info() or list(page.annots())):
            return False
        frame = coordinates(page.rect, 4)
        bounds = [coordinates(item.get("rect"), 4) for item in drawings]
        if frame is None or any(rect is None for rect in bounds):
            return False
        width, height = frame[2] - frame[0], frame[3] - frame[1]
        if width <= 0 or height <= 0:
            return False
        left, top = min(rect[0] for rect in bounds), min(rect[1] for rect in bounds)
        right, bottom = max(rect[2] for rect in bounds), max(rect[3] for rect in bounds)
        if (not (frame[0] <= left < right <= frame[2] and frame[1] <= top < bottom <= frame[3])
                or bottom - top > 2 or right - left < width * 0.8
                or not (bottom <= frame[1] + height * 0.1 or top >= frame[3] - height * 0.1)):
            return False
        # get_drawings 不一定包含 shading/隐藏文字；同时核对完整绘制序列，不能仅看路径数量。
        paint = page.get_bboxlog()
        if len(paint) != len(drawings):
            return False
        has_rule = False
        for index, (drawing, rect) in enumerate(zip(drawings, bounds)):
            shade = coordinates(drawing.get("fill"), 3)
            paint_rect = coordinates(paint[index][1], 4)
            if (drawing.get("type") != "f" or drawing.get("fill_opacity") != 1
                    or drawing.get("even_odd") is not False or drawing.get("layer")
                    or drawing.get("color") is not None or drawing.get("seqno") != index
                    or shade is None or not 0 <= min(shade) <= max(shade) <= 1
                    or max(shade) - min(shade) > 0.00001 or paint[index][0] != "fill-path"
                    or paint_rect is None or any(abs(a - b) > 0.0001 for a, b in zip(paint_rect, rect))
                    or not (left <= rect[0] < rect[2] <= right and top <= rect[1] < rect[3] <= bottom)):
                return False
            items = drawing.get("items")
            if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 4:
                return False
            rectangle = len(items) == 1 and len(items[0]) == 3 and items[0][0] == "re"
            if rectangle:
                box = coordinates(items[0][1], 4)
                if box != rect or type(items[0][2]) is not int or items[0][2] not in (-1, 1):
                    return False
                if rect[2] - rect[0] >= right - left - 0.01:
                    has_rule = True
                    continue
            # 封口只可位于横线两端；禁止把中部细字/公式笔画当作装饰。
            if (rect[2] - rect[0] > 2
                    or not (rect[2] <= left + 2 or rect[0] >= right - 2)):
                return False
            if rectangle:
                continue
            previous = None
            for item in items:
                if len(item) != 3 or item[0] != "l":
                    return False
                start, end = coordinates(item[1], 2), coordinates(item[2], 2)
                if (start is None or end is None or previous is not None and start != previous
                        or any(not (rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3])
                               for point in (start, end))):
                    return False
                previous = end
        return has_rule
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, RuntimeError):
        # 结构证据不完整不等于装饰；让原来的 required/Guard 继续保护未知页面。
        return False
