from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parents[2]
WORKTREE = ROOT / ".worktrees" / "figure-annotation-coverage"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(WORKTREE / "scripts"))

import test_apply as fixtures  # noqa: E402
import techpack_pdf.apply as apply_module  # noqa: E402


def test_approved_review_requires_frozen_layout_values(tmp_path: Path) -> None:
    source = tmp_path / "unfrozen-layout.pdf"
    fixtures._make_source(source)
    review_path, job, expected_output = fixtures._review_bundle(
        tmp_path, source, include_skipped=False
    )
    review = apply_module.load_review(review_path, job, expected_output)

    problem = apply_module._validate_inputs(
        source, review, source.with_name(source.name + ".annotated.pdf")
    )

    assert problem is not None
    assert problem["code"] == "review_layout_incomplete"


def test_apply_uses_frozen_review_without_layout_or_full_page_raster(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "frozen-layout.pdf"
    fixtures._make_source(source)
    review_path, job, expected_output = fixtures._review_bundle(
        tmp_path, source, include_skipped=False
    )
    expected_output["pages"] = [
        {"page_index": 0, "width": 300.0, "height": 300.0, "thumbnail": None}
    ]
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    for item in payload["items"]:
        item["reviewed_source_bbox"] = item["source_bbox"]
        item["reviewed_target_rect"] = item["target_rect"]
        item["reviewed_font_size"] = item["font_size"]
    payload["items"][0]["reviewed_source_bbox"] = [25.0, 25.0, 80.0, 43.0]
    payload["items"][0]["reviewed_target_rect"] = [170.0, 25.0, 280.0, 53.0]
    payload["items"][0]["reviewed_font_size"] = 11.0
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def forbidden_layout(*_args, **_kwargs):
        raise AssertionError("automatic layout must not run after manual review")

    def forbidden_page_raster(*_args, **_kwargs):
        raise AssertionError("full-page raster verification must not run")

    monkeypatch.setattr(apply_module, "_layout_document", forbidden_layout, raising=False)
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", forbidden_page_raster)

    result = apply_module.apply_review(source, review_path, job, expected_output)

    assert result.success, result.problems
    assert result.layout_rounds == 0
    assert result.output_path == source.with_name(source.name + ".annotated.pdf")

    expected = {
        item["item_id"]: (
            item["reviewed_translation"] or item["suggested_translation"],
            tuple(item["reviewed_target_rect"]),
            item["reviewed_font_size"],
        )
        for item in payload["items"]
    }
    document = pymupdf.open(result.output_path)
    try:
        page = document[0]
        annotations = [
            annotation
            for annotation in page.annots()
            if annotation.type[1] == "FreeText"
            and annotation.info.get("title") == "techpack_pdf"
        ]
        assert len(annotations) == len(expected)
        for annotation in annotations:
            metadata = json.loads(annotation.info["subject"])
            text, rect, font_size = expected[metadata["item_id"]]
            assert annotation.type[1] == "FreeText"
            assert annotation.info["title"] == "techpack_pdf"
            assert annotation.info["content"] == text
            assert tuple(annotation.rect) == rect
            assert annotation.flags & (64 | 128 | 512) == 0
            object_text = document.xref_object(annotation.xref, compressed=False)
            match = re.search(r"/DA \([^)]*? ([0-9.]+) Tf\)", object_text)
            assert match is not None
            assert float(match.group(1)) == font_size
            pixmap = annotation.get_pixmap(alpha=True)
            assert pixmap.width > 0 and pixmap.height > 0
            assert any(pixmap.samples)
    finally:
        document.close()
