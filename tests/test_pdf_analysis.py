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
    text_page.draw_rect(pymupdf.Rect(40, 100, 80, 120))
    text_page.add_text_annot((35, 80), "existing reviewer note")
    text_page.set_cropbox(pymupdf.Rect(10, 20, 190, 280))

    scan_page = document.new_page(width=200, height=300)
    raster = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 300), False)
    raster.clear_with(220)
    scan_page.insert_image(scan_page.rect, pixmap=raster)
    scan_page.set_rotation(90)
    document.save(path)
    document.close()


def _write_zero_page_pdf(path: Path) -> None:
    header = b"%PDF-1.4\n"
    objects = (
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n",
    )
    payload = bytearray(header)
    offsets: list[int] = []
    for item in objects:
        offsets.append(len(payload))
        payload.extend(item)
    xref_offset = len(payload)
    payload.extend(b"xref\n0 3\n0000000000 65535 f \n")
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(payload)


def _solid_pixmap(width: int, height: int) -> pymupdf.Pixmap:
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, width, height),
        False,
    )
    pixmap.clear_with(220)
    return pixmap


def test_inspect_pdf_records_geometry_content_and_144_dpi_thumbnails(tmp_path: Path) -> None:
    source = tmp_path / "two-pages.pdf"
    _make_two_page_pdf(source)

    manifest = inspect_pdf(source, tmp_path / "job")

    assert manifest.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.page_count == 2
    assert manifest.has_existing_annotations is True

    first, second = manifest.pages
    assert first.crop_box == pytest.approx((0.0, 0.0, 180.0, 260.0))
    assert first.pdf_crop_box == pytest.approx((10.0, 20.0, 190.0, 280.0))
    assert first.annotation_count == 1
    assert len(first.annotation_bboxes) == 1
    assert first.annotation_bboxes[0] == pytest.approx((25.0, 60.0, 41.0, 76.0))
    assert (30.0, 80.0, 70.0, 100.0) in first.drawing_bboxes
    assert first.rotation == 0
    collar = next(span for span in first.native_spans if span.text == "COLLAR HEIGHT")
    assert collar.bbox == pytest.approx((20.0, 27.1, 116.684, 43.588), abs=0.01)
    assert first.native_text_area_ratio > 0
    assert first.needs_ocr_regions == ()

    assert second.rotation == 90
    assert second.crop_box == pytest.approx((0.0, 0.0, 200.0, 300.0))
    assert len(second.image_bboxes) == 1
    assert second.image_bboxes[0] == pytest.approx((0.0, 0.0, 200.0, 300.0))
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


def test_inspect_pdf_rejects_zero_page_pdf(tmp_path: Path) -> None:
    source = tmp_path / "zero-pages.pdf"
    _write_zero_page_pdf(source)

    with pytest.raises(TechpackError) as raised:
        inspect_pdf(source, tmp_path / "job")

    assert raised.value.code == "pdf_corrupt"


def test_inspect_pdf_rejects_crop_box_outside_media_box(tmp_path: Path) -> None:
    source = tmp_path / "invalid-crop.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    document.xref_set_key(page.xref, "CropBox", "[0 0 400 300]")
    document.save(source)
    document.close()

    with pytest.raises(TechpackError) as raised:
        inspect_pdf(source, tmp_path / "job")

    assert raised.value.code == "pdf_corrupt"
    assert raised.value.details["error_code"] == "invalid_crop_box"


def test_cropped_scanned_page_uses_crop_relative_page_coordinates(tmp_path: Path) -> None:
    source = tmp_path / "cropped-scan.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    page.insert_image(
        pymupdf.Rect(50, 50, 150, 170),
        pixmap=_solid_pixmap(100, 120),
    )
    page.set_cropbox(pymupdf.Rect(50, 50, 150, 250))
    document.save(source)
    document.close()

    inspected = inspect_pdf(source, tmp_path / "job").pages[0]

    assert inspected.pdf_crop_box == (50.0, 50.0, 150.0, 250.0)
    assert inspected.crop_box == (0.0, 0.0, 100.0, 200.0)
    assert inspected.image_bboxes == ((0.0, 0.0, 100.0, 120.0),)
    assert inspected.image_coverage_ratio == pytest.approx(0.6)
    assert inspected.needs_ocr_regions == ((0.0, 0.0, 100.0, 120.0),)


def test_overlapping_images_use_union_area_for_scan_classification(tmp_path: Path) -> None:
    source = tmp_path / "overlapping-images.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    image_rect = pymupdf.Rect(0, 0, 60, 100)
    pixmap = _solid_pixmap(60, 100)
    page.insert_image(image_rect, pixmap=pixmap)
    page.insert_image(image_rect, pixmap=pixmap)
    document.save(source)
    document.close()

    inspected = inspect_pdf(source, tmp_path / "job").pages[0]

    assert inspected.image_bboxes == (
        (0.0, 0.0, 60.0, 100.0),
        (0.0, 0.0, 60.0, 100.0),
    )
    assert inspected.image_coverage_ratio == pytest.approx(0.3)
    assert inspected.is_scanned is False
    assert inspected.needs_ocr_regions == ()
