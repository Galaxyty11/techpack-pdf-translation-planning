from __future__ import annotations

import math
from pathlib import Path
from time import perf_counter

import pymupdf
import pytest

import techpack_pdf.layout as layout_module
from techpack_pdf.models import ReviewItem
from techpack_pdf.review_preview import plan_review_items


def _item(source_bbox: list[float], *, item_id: str = "p001-i001") -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "item_id": item_id,
            "page_index": 0,
            "page_type": "technical_drawing",
            "source_text": "BARTACK AT POCKET OPENING",
            "normalized_text": "bartack at pocket opening",
            "source_bbox": source_bbox,
            "source_kind": "body",
            "coordinate_confidence": "low",
            "decision_reason": "page_rule",
            "locked_tokens": [],
            "glossary_hits": [],
            "suggested_translation": "袋口处打枣加固",
            "reviewed_translation": None,
            "review_status": None,
            "risk_level": "medium",
            "translation_host": "codex",
            "translation_execution_mode": "main_agent",
            "translation_model": "unknown",
            "translation_agent_role": None,
            "translation_prompt_version": "1.0",
            "placement_strategy": None,
            "target_rect": None,
            "font_size": None,
            "leader_line": None,
            "warnings": [],
        }
    )


def _save_page(
    path: Path,
    *,
    blocking_rect: pymupdf.Rect | None = None,
    vector_rect: pymupdf.Rect | None = None,
    width: float = 500,
    height: float = 300,
) -> None:
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    if blocking_rect is not None:
        annotation = page.add_freetext_annot(
            blocking_rect,
            "existing content",
            fontsize=8,
            text_color=(0, 0, 0),
            fill_color=None,
            border_color=None,
        )
        annotation.update()
    if vector_rect is not None:
        page.draw_rect(vector_rect, color=(0, 0, 0), width=1)
    document.save(path)
    document.close()


def test_review_preview_keeps_a_risky_placement_near_its_source(
    tmp_path: Path,
) -> None:
    source_bbox = [180.0, 130.0, 220.0, 140.0]
    source_pdf = tmp_path / "near-source.pdf"
    _save_page(source_pdf, blocking_rect=pymupdf.Rect(80, 60, 320, 220))

    planned = plan_review_items(source_pdf, [_item(source_bbox)])[0]

    assert planned.target_rect is not None
    source_center = ((source_bbox[0] + source_bbox[2]) / 2, (source_bbox[1] + source_bbox[3]) / 2)
    target_center = (
        (planned.target_rect[0] + planned.target_rect[2]) / 2,
        (planned.target_rect[1] + planned.target_rect[3]) / 2,
    )
    assert math.dist(source_center, target_center) <= 96.0
    assert planned.placement_strategy != "page_blank_fallback"
    assert planned.risk_level == "high"
    assert "manual_placement_required" in planned.warnings


def test_wide_page_does_not_expand_the_nearby_radius(
    tmp_path: Path,
) -> None:
    source_bbox = [350.0, 260.0, 390.0, 270.0]
    source_pdf = tmp_path / "wide-near-source.pdf"
    _save_page(
        source_pdf,
        blocking_rect=pymupdf.Rect(250, 180, 440, 350),
        width=841.89,
        height=595.276,
    )

    planned = plan_review_items(source_pdf, [_item(source_bbox)])[0]

    assert planned.target_rect is not None
    source_center = (370.0, 265.0)
    target_center = (
        (planned.target_rect[0] + planned.target_rect[2]) / 2,
        (planned.target_rect[1] + planned.target_rect[3]) / 2,
    )
    assert math.dist(source_center, target_center) <= 96.0
    assert planned.risk_level == "high"
    assert "manual_placement_required" in planned.warnings


def test_review_preview_does_not_render_candidates_at_high_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_pdf = tmp_path / "blank.pdf"
    _save_page(source_pdf)

    def forbidden_render(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fast review planning must not rasterize every candidate")

    monkeypatch.setattr(layout_module, "_page_array", forbidden_render)

    planned = plan_review_items(
        source_pdf,
        [_item([180.0, 130.0, 220.0, 140.0])],
    )[0]

    assert planned.target_rect is not None
    assert planned.font_size in {7.0, 6.0, 5.0}
    assert planned.leader_line is None


def test_vector_drawing_is_a_lightweight_review_obstacle(tmp_path: Path) -> None:
    source_pdf = tmp_path / "vector-obstacle.pdf"
    _save_page(
        source_pdf,
        vector_rect=pymupdf.Rect(80, 60, 320, 220),
    )

    planned = plan_review_items(
        source_pdf,
        [_item([180.0, 130.0, 220.0, 140.0])],
    )[0]

    assert planned.risk_level == "high"
    assert "manual_placement_required" in planned.warnings


def test_every_item_in_a_new_annotation_overlap_is_marked_unsafe(
    tmp_path: Path,
) -> None:
    source_pdf = tmp_path / "dense-overlap.pdf"
    _save_page(source_pdf)
    items = [
        _item(
            [180.0, 130.0, 220.0, 140.0],
            item_id=f"p001-i{index:03d}",
        )
        for index in range(30)
    ]

    planned = plan_review_items(source_pdf, items)
    unsafe = {
        item.item_id
        for item in planned
        if "manual_placement_required" in item.warnings
    }
    overlapping: set[str] = set()
    for index, item in enumerate(planned):
        rect = pymupdf.Rect(item.target_rect)
        for other in planned[index + 1 :]:
            if rect.intersects(pymupdf.Rect(other.target_rect)):
                overlapping.update((item.item_id, other.item_id))

    assert overlapping
    assert overlapping <= unsafe


def test_dense_review_layout_avoids_quadratic_candidate_scans(
    tmp_path: Path,
) -> None:
    source_pdf = tmp_path / "dense-performance.pdf"
    _save_page(source_pdf)
    items = [
        _item(
            [180.0, 130.0, 220.0, 140.0],
            item_id=f"p001-i{index:03d}",
        )
        for index in range(100)
    ]

    started = perf_counter()
    planned = plan_review_items(source_pdf, items)
    elapsed = perf_counter() - started

    assert len(planned) == 100
    assert elapsed < 3.0
