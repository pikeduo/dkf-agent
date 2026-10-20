"""显式选页的灰度质量提示；仅用已有PyMuPDF/标准库，不OCR、不导出文件或访问网络。"""

import hashlib
import math
import os
from statistics import median

from app.schemas.document_analysis import VisualPageAssessment
from app.services.document_processing import source_path
from app.services.specialty_errors import SpecialtyError

MAX_SIDE = 1024
MAX_POINTS = 16000
MAX_SOURCE_BYTES = 200_000_000


def _projection_skew(points, width, height):
    """在±8度内比较行投影，仅在文本行方向信号充分时报告候选角度。"""
    if len(points) < 300:
        return None, "unknown"
    scores = []
    for half_degree in range(-16, 17):
        angle = half_degree / 2
        slope = math.tan(math.radians(angle))
        histogram = [0] * (height + width + 4)
        for x, y in points:
            index = round(y - slope * (x - width / 2)) + width // 2 + 1
            if 0 <= index < len(histogram):
                histogram[index] += 1
        scores.append((sum(value * value for value in histogram), angle))
    peak, angle = max(scores, key=lambda item: (item[0], -abs(item[1])))
    baseline = median(item[0] for item in scores)
    if baseline <= 0 or peak / baseline < 1.2 or abs(angle) == 8:
        return None, "unknown"
    return angle, "review" if abs(angle) >= 1.5 else "no_observed_issue"


def measure_gray_page(samples, width, height, stride, *, page):
    """对固定尺度单通道字节计算对比度、梯度和行方向，空白/照片式数据保留未知。"""
    if (type(width) is not int or type(height) is not int or type(stride) is not int
            or not 3 <= width <= MAX_SIDE + 2 or not 3 <= height <= MAX_SIDE + 2
            or stride < width or len(samples) != stride * height):
        raise SpecialtyError("ANALYSIS_VISUAL_LIMIT")
    total = total_squared = dark = gradient = gradient_count = 0
    for y in range(height):
        row = y * stride
        for x in range(width):
            value = samples[row + x]
            total += value
            total_squared += value * value
            dark += value < 180
            if x and y:
                gradient += (value - samples[row + x - 1]) ** 2 + (value - samples[row + x - stride]) ** 2
                gradient_count += 2
    size = width * height
    mean = total / size
    contrast = math.sqrt(max(0, total_squared / size - mean * mean))
    edge_energy = gradient / gradient_count
    ratio = dark / size
    suitable = .005 <= ratio <= .45 and contrast >= 8
    blur = "review" if suitable and edge_energy < 12 else "no_observed_issue" if suitable else "unknown"
    threshold = min(180, max(40, mean - contrast / 2))
    sampling = max(1, math.ceil(math.sqrt(max(1, dark) / MAX_POINTS)))
    points = []
    for y in range(1, height - 1, sampling):
        row = y * stride
        for x in range(1, width - 1, sampling):
            if samples[row + x] < threshold <= samples[row + x - 1]:
                points.append((x, y))
    if len(points) > MAX_POINTS:
        points = points[::math.ceil(len(points) / MAX_POINTS)]
    angle, skew = _projection_skew(points, width, height) if suitable else (None, "unknown")
    return VisualPageAssessment(page=page, width=width, height=height, contrast=round(contrast, 4),
        edge_energy=round(edge_energy, 4), dark_pixel_ratio=round(ratio, 6), blur=blur,
        skew_degrees=angle, skew=skew)


def _identity(stat):
    """比较实际文件身份及修改信息，避免普通并发替换被误当成同一原件。"""
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def inspect_visual_pages(document, pages):
    """前后核对原Hash并只渲染指定页到内存；Office原件不在此自动转换。"""
    if document.file_type not in {"pdf", "png", "jpg", "jpeg"}:
        raise SpecialtyError("ANALYSIS_VISUAL_FORMAT")
    if (not pages or len(pages) > 5 or len(set(pages)) != len(pages)
            or any(type(page) is not int or page < 1 for page in pages)):
        raise SpecialtyError("ANALYSIS_INPUT")
    try:
        import fitz

        path = source_path(document)
        if path.is_symlink() or not path.is_file():
            raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not 0 < before.st_size <= MAX_SOURCE_BYTES:
                raise SpecialtyError("ANALYSIS_VISUAL_LIMIT")
            if hashlib.file_digest(source, "sha256").hexdigest() != document.file_hash:
                raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
            if _identity(path.stat()) != _identity(before):
                raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
            results = []
            with fitz.open(str(path)) as opened:
                if opened.needs_pass or opened.page_count < 1:
                    raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
                count = opened.page_count
                if any(page > count for page in pages):
                    raise SpecialtyError("ANALYSIS_VISUAL_PAGE")
                for number in pages:
                    page = opened.load_page(number - 1)
                    rect = page.rect
                    if (not math.isfinite(rect.width) or not math.isfinite(rect.height)
                            or min(rect.width, rect.height) <= 0):
                        raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
                    # 限制显式像素观测，不将导出的Office页等同于原Office物理页。
                    if document.file_type != "pdf" and rect.width * rect.height > 20_000_000:
                        raise SpecialtyError("ANALYSIS_VISUAL_LIMIT")
                    scale = MAX_SIDE / max(rect.width, rect.height)
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY,
                        alpha=False, annots=False)
                    results.append(measure_gray_page(pixmap.samples, pixmap.width, pixmap.height,
                        pixmap.stride, page=number))
                    del pixmap
            source.seek(0)
            if (hashlib.file_digest(source, "sha256").hexdigest() != document.file_hash
                    or _identity(path.stat()) != _identity(before)
                    or _identity(os.fstat(source.fileno())) != _identity(before)):
                raise SpecialtyError("ANALYSIS_VISUAL_SOURCE")
        return count, results
    except SpecialtyError:
        raise
    except Exception:
        raise SpecialtyError("ANALYSIS_VISUAL_SOURCE") from None
