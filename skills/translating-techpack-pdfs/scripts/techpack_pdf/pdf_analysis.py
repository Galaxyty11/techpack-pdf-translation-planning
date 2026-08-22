"""Read-only PDF health inspection and page-level extraction."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .errors import TechpackError


@dataclass(frozen=True)
class NativeSpan:
    text: str
    bbox: tuple[float, float, float, float]
    font: str
    font_size: float


@dataclass(frozen=True)
class PdfPageManifest:
    page_index: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    native_spans: tuple[NativeSpan, ...]
    native_text_area_ratio: float
    image_coverage_ratio: float
    drawing_count: int
    annotation_count: int
    is_scanned: bool
    needs_ocr_regions: tuple[tuple[float, float, float, float], ...]
    thumbnail_path: Path


@dataclass(frozen=True)
class PdfManifest:
    source_path: Path
    sha256: str
    page_count: int
    has_existing_annotations: bool
    pages: tuple[PdfPageManifest, ...]


def inspect_pdf(path: Path, job_dir: Path) -> PdfManifest:
    """Inspect a PDF without changing it and render fixed 144-DPI thumbnails."""
    source_path = Path(path)
    try:
        document = pymupdf.open(source_path)
    except Exception as exc:
        raise TechpackError(
            "pdf_corrupt",
            "PDF cannot be opened",
            {"source_path": str(source_path)},
        ) from exc

    try:
        if document.needs_pass:
            raise TechpackError(
                "pdf_password_required",
                "PDF requires a password",
                {"source_path": str(source_path)},
            )
        if document.page_count <= 0:
            raise TechpackError(
                "pdf_corrupt",
                "PDF has no pages",
                {"source_path": str(source_path)},
            )

        thumbnail_dir = Path(job_dir) / "thumbnails"
        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        pages = tuple(
            _inspect_page(page, thumbnail_dir / f"page-{page.number + 1:04d}.png")
            for page in document
        )
    finally:
        document.close()

    return PdfManifest(
        source_path=source_path,
        sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        page_count=len(pages),
        has_existing_annotations=any(page.annotation_count > 0 for page in pages),
        pages=pages,
    )


def _inspect_page(page: pymupdf.Page, thumbnail_path: Path) -> PdfPageManifest:
    crop_box = page.cropbox
    media_box = page.mediabox
    if not _valid_box(crop_box) or not media_box.contains(crop_box):
        raise TechpackError(
            "pdf_corrupt",
            "PDF contains an invalid CropBox",
            {"page_index": page.number, "error_code": "invalid_crop_box"},
        )

    native_spans = _native_spans(page)
    page_area = crop_box.get_area()
    native_text_area = sum(pymupdf.Rect(span.bbox).get_area() for span in native_spans)
    image_area = sum(
        (pymupdf.Rect(image["bbox"]) & crop_box).get_area()
        for image in page.get_image_info()
    )
    native_text_area_ratio = min(native_text_area / page_area, 1.0)
    image_coverage_ratio = min(image_area / page_area, 1.0)
    is_scanned = native_text_area_ratio < 0.01 and image_coverage_ratio >= 0.5
    needs_ocr_regions = (_box_tuple(crop_box),) if is_scanned else ()

    page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(thumbnail_path)
    return PdfPageManifest(
        page_index=page.number,
        media_box=_box_tuple(media_box),
        crop_box=_box_tuple(crop_box),
        rotation=page.rotation,
        native_spans=native_spans,
        native_text_area_ratio=native_text_area_ratio,
        image_coverage_ratio=image_coverage_ratio,
        drawing_count=len(page.get_drawings()),
        annotation_count=sum(1 for _ in page.annots()),
        is_scanned=is_scanned,
        needs_ocr_regions=needs_ocr_regions,
        thumbnail_path=thumbnail_path,
    )


def _native_spans(page: pymupdf.Page) -> tuple[NativeSpan, ...]:
    spans: list[NativeSpan] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                text = str(span.get("text", ""))
                if text.strip():
                    spans.append(
                        NativeSpan(
                            text=text,
                            bbox=tuple(float(value) for value in span["bbox"]),
                            font=str(span.get("font", "")),
                            font_size=float(span.get("size", 0.0)),
                        )
                    )
    return tuple(spans)


def _valid_box(rect: pymupdf.Rect) -> bool:
    return (
        all(math.isfinite(value) for value in rect)
        and rect.width > 0
        and rect.height > 0
    )


def _box_tuple(rect: pymupdf.Rect) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in rect)
