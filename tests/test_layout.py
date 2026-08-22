from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pymupdf

from techpack_pdf.layout import (
    Collision,
    Placement,
    ProtectedGeometry,
    detect_collisions,
    detect_rendered_collisions,
    find_same_row_blank_cells,
    optimize_layout,
    rank_placements,
)
from techpack_pdf.models import ReviewItem


def _item(*, text: str = "领宽（HPS 到 HPS）") -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "item_id": "p001-i001",
            "page_index": 0,
            "page_type": "measurement",
            "source_text": "NECK WIDTH - HPS TO HPS",
            "normalized_text": "neck width - hps to hps",
            "source_bbox": [40.0, 50.0, 100.0, 62.0],
            "source_kind": "body",
            "coordinate_confidence": "high",
            "decision_reason": "field_rule",
            "locked_tokens": ["HPS", "HPS"],
            "glossary_hits": [],
            "suggested_translation": text,
            "reviewed_translation": None,
            "review_status": "approved",
            "risk_level": "low",
            "translation_host": "codex",
            "translation_execution_mode": "subagent",
            "translation_model": "unknown",
            "translation_agent_role": "techpack-translator",
            "translation_prompt_version": "1.0",
            "placement_strategy": None,
            "target_rect": None,
            "font_size": None,
            "leader_line": None,
            "warnings": [],
        }
    )


def _placement(
    strategy: str,
    *,
    rect=(110.0, 50.0, 190.0, 70.0),
    font_size=7.0,
    collision_count=0,
    in_bounds=True,
    same_region=True,
    leader=None,
    source_distance=10.0,
    movement_distance=10.0,
    candidate_index=0,
) -> Placement:
    return Placement(
        item_id="p001-i001",
        page_index=0,
        text="中文",
        rect=rect,
        font_size=font_size,
        strategy=strategy,
        wrapped_lines=("中文",),
        same_semantic_region=same_region,
        leader_line=leader,
        collision_count=collision_count,
        in_bounds=in_bounds,
        source_distance=source_distance,
        movement_distance=movement_distance,
        candidate_index=candidate_index,
    )


def test_rank_placements_generates_the_required_deterministic_candidate_sequence() -> None:
    candidates = rank_placements(
        _item(),
        (0.0, 0.0, 300.0, 220.0),
        same_row_cells=[(110.0, 45.0, 205.0, 78.0)],
        semantic_region=(20.0, 20.0, 250.0, 180.0),
    )
    generated = sorted(candidates, key=lambda value: value.candidate_index)

    at_seven = [value.strategy for value in generated if value.font_size == 7.0]
    assert at_seven[:7] == [
        "same_row_cell",
        "same_region_above",
        "same_region_below",
        "same_region_right",
        "same_region_left",
        "same_region_wide_wrap",
        "same_region_narrow_wrap",
    ]
    assert list(dict.fromkeys(value.font_size for value in generated)) == [
        7.0,
        6.5,
        6.0,
        5.5,
        5.0,
    ]
    assert generated[-1].strategy == "margin_track"
    assert generated[-1].leader_line is not None


def test_rank_placements_applies_the_exact_priority_chain_and_stable_tie_breaker() -> None:
    placements = [
        _placement("colliding", collision_count=1, candidate_index=0),
        _placement("out-of-bounds", in_bounds=False, candidate_index=1),
        _placement("other-region", same_region=False, candidate_index=2),
        _placement("leader", leader=((100.0, 60.0), (110.0, 60.0)), candidate_index=3),
        _placement("far", source_distance=30.0, candidate_index=4),
        _placement("small-font", font_size=6.5, candidate_index=5),
        _placement("more-movement", movement_distance=20.0, candidate_index=6),
        _placement("tie-b", rect=(111.0, 50.0, 191.0, 70.0), candidate_index=8),
        _placement("tie-a", candidate_index=7),
    ]

    ranked = rank_placements(_item(), (0.0, 0.0, 300.0, 220.0), candidates=placements)

    assert [value.strategy for value in ranked] == [
        "tie-a",
        "tie-b",
        "more-movement",
        "small-font",
        "far",
        "leader",
        "other-region",
        "colliding",
        "out-of-bounds",
    ]


def test_find_same_row_blank_cells_identifies_an_empty_placement_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "measurement-table.pdf"
    document = pymupdf.open()
    page = document.new_page(width=260, height=150)
    for x in (20, 120, 230):
        page.draw_line((x, 20), (x, 90), width=0.8)
    for y in (20, 50, 90):
        page.draw_line((20, y), (230, y), width=0.8)
    page.insert_text((25, 40), "DESCRIPTION", fontsize=7)
    page.insert_text((125, 40), "PLACEMENT", fontsize=7)
    page.insert_text((25, 70), "NECK WIDTH", fontsize=7)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        source_bbox = document[0].search_for("NECK WIDTH")[0]
        cells = find_same_row_blank_cells(document[0], source_bbox)
    finally:
        document.close()

    assert cells == [(121.0, 51.0, 229.0, 89.0)]


def test_detect_collisions_protects_glyphs_drawings_and_rechecks_at_300_dpi(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collision.pdf"
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    page.insert_text((40, 60), "SOURCE TEXT", fontsize=10)
    page.draw_line((20, 90), (220, 90), width=1)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        placement = _placement(
            "over-source",
            rect=(35.0, 48.0, 115.0, 68.0),
            source_distance=0.0,
            movement_distance=0.0,
        )
        collisions = detect_collisions(document[0], [placement])
    finally:
        document.close()

    assert any(value.kind == "protected_text" for value in collisions)
    render = [value for value in collisions if value.kind == "render_overlap"]
    assert render
    assert all(value.render_dpi == 300 for value in render)
    assert all(value.intersecting_pixels > 4 for value in render)


def test_detect_collisions_enforces_one_point_clearance_and_new_new_overlap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "blank.pdf"
    document = pymupdf.open()
    document.new_page(width=240, height=180)
    document.save(path)
    document.close()
    protected = [ProtectedGeometry("drawing", (50.0, 50.0, 100.0, 70.0), "line-1")]
    too_close = _placement("too-close", rect=(100.5, 50.0, 150.0, 70.0))
    exact_clearance = _placement(
        "exact-clearance",
        rect=(101.0, 80.0, 151.0, 100.0),
        candidate_index=1,
    )
    exact_clearance = replace(exact_clearance, item_id="p001-i002")
    overlapping = replace(
        exact_clearance,
        item_id="p001-i003",
        rect=(120.0, 80.0, 170.0, 100.0),
        candidate_index=2,
    )

    document = pymupdf.open(path)
    try:
        collisions = detect_collisions(
            document[0],
            [too_close, exact_clearance, overlapping],
            protected=protected,
            render_check=False,
        )
    finally:
        document.close()

    assert any(
        value.item_id == too_close.item_id and value.kind == "protected_drawing"
        for value in collisions
    )
    assert not any(
        value.item_id == exact_clearance.item_id and value.object_id == "line-1"
        for value in collisions
    )
    assert any(value.kind == "new_annotation_overlap" for value in collisions)


def test_detect_collisions_rejects_a_rect_that_clips_measured_wrapped_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "blank-clipping.pdf"
    document = pymupdf.open()
    document.new_page(width=240, height=180)
    document.save(path)
    document.close()
    clipped = replace(
        _placement("clipped", rect=(120.0, 30.0, 180.0, 36.0)),
        text="第一行第二行",
        wrapped_lines=("第一行", "第二行"),
    )

    document = pymupdf.open(path)
    try:
        collisions = detect_collisions(
            document[0], [clipped], render_check=False
        )
    finally:
        document.close()

    assert any(value.kind == "text_clipped" for value in collisions)


def test_detect_rendered_collisions_diffs_the_real_before_and_after_pages_at_300_dpi(
    tmp_path: Path,
) -> None:
    before_path = tmp_path / "before.pdf"
    after_path = tmp_path / "after.pdf"
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    page.insert_text((40, 60), "SOURCE TEXT", fontsize=10)
    document.save(before_path)
    document.save(after_path)
    document.close()
    after = pymupdf.open(after_path)
    page = after[0]
    page.add_freetext_annot(
        pymupdf.Rect(35, 48, 115, 68),
        "中文",
        fontsize=7,
        fontname="china-s",
        text_color=(0.85, 0.05, 0.05),
        fill_color=None,
        border_color=None,
        border_width=0,
    ).update()
    after.saveIncr()
    after.close()
    placement = _placement(
        "actual-over-source",
        rect=(35.0, 48.0, 115.0, 68.0),
        source_distance=0.0,
        movement_distance=0.0,
    )

    before = pymupdf.open(before_path)
    after = pymupdf.open(after_path)
    try:
        collisions = detect_rendered_collisions(before[0], after[0], [placement])
    finally:
        after.close()
        before.close()

    assert any(
        value.kind == "render_overlap"
        and value.render_dpi == 300
        and value.intersecting_pixels > 4
        for value in collisions
    )


def test_optimize_layout_stops_after_two_unchanged_rounds_and_caps_at_ten() -> None:
    initial = [_placement("a")]
    unresolved = [Collision(0, "p001-i001", "protected_text", "glyph-1")]

    stable = optimize_layout(
        initial,
        collision_detector=lambda _placements: unresolved,
        candidate_provider=lambda placement, _collisions: [placement],
    )
    assert stable.rounds == 2
    assert stable.stable is True
    assert stable.collisions == tuple(unresolved)

    def alternate(placement, _collisions):
        x0, y0, x1, y1 = placement.rect
        shift = 1.0 if x0 == 110.0 else -1.0
        return [replace(placement, rect=(x0 + shift, y0, x1 + shift, y1))]

    capped = optimize_layout(
        initial,
        collision_detector=lambda _placements: unresolved,
        candidate_provider=alternate,
    )
    assert capped.rounds == 10
    assert capped.stable is False
    assert capped.collisions == tuple(unresolved)
