from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parents[2]
WORKTREE = ROOT / ".worktrees" / "figure-annotation-coverage"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(WORKTREE / "scripts"))

import test_apply as fixtures  # noqa: E402
import techpack_pdf.apply as apply_module  # noqa: E402
from techpack_pdf.pdf_analysis import inspect_pdf  # noqa: E402


def _rotated_source(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    page.insert_text((20, 30), "SOURCE")
    page.set_rotation(90)
    document.save(path)
    document.close()


def test_rotated_thumbnail_uses_canonical_unrotated_coordinate_space(tmp_path: Path) -> None:
    source = tmp_path / "rotated.pdf"
    _rotated_source(source)

    manifest = inspect_pdf(source, tmp_path / "job")
    thumbnail = pymupdf.Pixmap(str(manifest.pages[0].thumbnail_path))

    assert manifest.pages[0].crop_box == (0.0, 0.0, 200.0, 300.0)
    assert (thumbnail.width, thumbnail.height) == (400, 600)


def test_rotated_apply_validates_against_canonical_crop_bounds(tmp_path: Path) -> None:
    source = tmp_path / "rotated-apply.pdf"
    _rotated_source(source)
    review_path, job, expected = fixtures._review_bundle(
        tmp_path, source, include_skipped=False
    )
    expected["pages"] = [
        {"page_index": 0, "width": 200.0, "height": 300.0, "thumbnail": None}
    ]
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    for index, item in enumerate(payload["items"]):
        y0 = 200.0 + index * 30.0
        target_width = item["target_rect"][2] - item["target_rect"][0]
        target_height = item["target_rect"][3] - item["target_rect"][1]
        item["reviewed_source_bbox"] = [10.0, y0, 70.0, y0 + 12.0]
        item["reviewed_target_rect"] = [
            80.0,
            y0,
            80.0 + target_width,
            y0 + target_height,
        ]
        item["reviewed_font_size"] = 7.0
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    review = apply_module.load_review(review_path, job, expected)

    assert apply_module._validate_inputs(
        source, review, source.with_name(source.name + ".annotated.pdf")
    ) is None

    result = apply_module.apply_review(source, review_path, job, expected)

    assert result.success is True
    output = pymupdf.open(result.output_path)
    try:
        rects = [
            tuple(annotation.rect)
            for annotation in output[0].annots()
            if annotation.info.get("title") == "techpack_pdf"
        ]
        assert len(rects) == 3
        assert all(rect[1] >= 200.0 and rect[3] <= 300.0 for rect in rects)
    finally:
        output.close()
