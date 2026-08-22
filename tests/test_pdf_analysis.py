from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.pdf_analysis import inspect_pdf


def _make_two_page_pdf(path: Path) -> None:
    document = pymupdf.open()
    text_page = document.new_page(width=200, height=300)
    text_page.insert_text((30, 60), "COLLAR HEIGHT", fontsize=12)
    text_page.add_text_annot((35, 80), "existing reviewer note")
    text_page.set_cropbox(pymupdf.Rect(10, 20, 190, 280))

    scan_page = document.new_page(width=200, height=300)
    raster = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 300), False)
    raster.clear_with(220)
    scan_page.insert_image(scan_page.rect, pixmap=raster)
    scan_page.set_rotation(90)
    document.save(path)
    document.close()


def test_inspect_pdf_records_geometry_content_and_144_dpi_thumbnails(tmp_path: Path) -> None:
    source = tmp_path / "two-pages.pdf"
    _make_two_page_pdf(source)

    manifest = inspect_pdf(source, tmp_path / "job")

    assert manifest.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.page_count == 2
    assert manifest.has_existing_annotations is True

    first, second = manifest.pages
    assert first.crop_box == pytest.approx((10.0, 20.0, 190.0, 280.0))
    assert first.annotation_count == 1
    assert first.rotation == 0
    collar = next(span for span in first.native_spans if span.text == "COLLAR HEIGHT")
    assert collar.bbox == pytest.approx((20.0, 27.1, 116.684, 43.588), abs=0.01)
    assert first.native_text_area_ratio > 0
    assert first.needs_ocr_regions == ()

    assert second.rotation == 90
    assert second.image_coverage_ratio == pytest.approx(1.0, abs=0.01)
    assert second.is_scanned is True
    assert second.needs_ocr_regions == ((0.0, 0.0, 200.0, 300.0),)

    first_thumbnail = pymupdf.Pixmap(str(first.thumbnail_path))
    second_thumbnail = pymupdf.Pixmap(str(second.thumbnail_path))
    assert (first_thumbnail.width, first_thumbnail.height) == (360, 520)
    assert (second_thumbnail.width, second_thumbnail.height) == (600, 400)


def test_inspect_pdf_maps_corrupt_input_to_stable_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a PDF")

    with pytest.raises(TechpackError) as raised:
        inspect_pdf(source, tmp_path / "job")

    assert raised.value.code == "pdf_corrupt"


def test_inspect_pdf_rejects_password_protected_input(tmp_path: Path) -> None:
    source = tmp_path / "protected.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "secret")
    document.save(
        source,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )
    document.close()

    with pytest.raises(TechpackError) as raised:
        inspect_pdf(source, tmp_path / "job")

    assert raised.value.code == "pdf_password_required"
