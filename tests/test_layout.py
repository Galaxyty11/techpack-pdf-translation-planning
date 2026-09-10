from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil

import numpy as np
import pymupdf
import pytest

import techpack_pdf.layout as layout_module

from techpack_pdf.layout import (
    Collision,
    Placement,
    ProtectedGeometry,
    detect_collisions,
    detect_candidate_collisions,
    detect_rendered_collisions,
    find_same_row_blank_cells,
    infer_semantic_region,
    extract_protected_geometry,
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


def test_rank_placements_adds_page_blank_fallback_for_low_confidence_coordinates() -> None:
    item = _item().model_copy(
        update={
            "coordinate_confidence": "low",
            "source_bbox": [40.0, 190.0, 100.0, 202.0],
        }
    )

    candidates = rank_placements(
        item,
        (0.0, 0.0, 300.0, 220.0),
        page_blank_fallback=True,
    )

    fallback = [candidate for candidate in candidates if candidate.strategy == "page_blank_fallback"]
    assert fallback
    assert all(candidate.leader_line is None for candidate in fallback)
    assert any(candidate.rect[1] < 180.0 for candidate in fallback)


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

    assert cells == [(122.0, 52.0, 228.0, 88.0)]


def test_find_same_row_blank_cells_uses_other_empty_cells_in_a_bom_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-empty-cells.pdf"
    document = pymupdf.open()
    page = document.new_page(width=320, height=150)
    for x in (20, 80, 170, 230, 300):
        page.draw_line((x, 20), (x, 90), width=0.8)
    for y in (20, 50, 90):
        page.draw_line((20, y), (300, y), width=0.8)
    for x, value in ((25, "PART"), (85, "COMPONENT"), (175, "SIZE"), (235, "USAGE")):
        page.insert_text((x, 40), value, fontsize=7)
    page.insert_text((25, 70), "VELUM", fontsize=7)
    page.insert_text((85, 70), "CSG-007 HANGTAG", fontsize=7)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        source_bbox = document[0].search_for("CSG-007 HANGTAG")[0]
        cells = find_same_row_blank_cells(document[0], source_bbox)
    finally:
        document.close()

    assert any(pymupdf.Rect(172, 52, 228, 88).contains(pymupdf.Rect(value)) for value in cells)


def test_find_same_row_blank_cells_uses_unused_space_in_the_source_cell(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-source-cell-remainder.pdf"
    document = pymupdf.open()
    page = document.new_page(width=320, height=150)
    for x in (20, 250, 300):
        page.draw_line((x, 20), (x, 90), width=0.8)
    for y in (20, 50, 90):
        page.draw_line((20, y), (300, y), width=0.8)
    page.insert_text((25, 40), "COMPONENT", fontsize=7)
    page.insert_text((255, 40), "COLOR", fontsize=7)
    page.insert_text((25, 70), "Component Group: LABEL", fontsize=7)
    page.insert_text((255, 70), "BLACK", fontsize=7)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        page = document[0]
        source_bbox = page.search_for("Component Group: LABEL")[0]
        cells = find_same_row_blank_cells(page, source_bbox)
    finally:
        document.close()

    assert any(value[0] > source_bbox.x1 and value[2] < 250 for value in cells)


def test_compound_table_grid_protects_strokes_without_consuming_cell_interiors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "compound-grid.pdf"
    document = pymupdf.open()
    page = document.new_page(width=260, height=150)
    shape = page.new_shape()
    for x in (20, 120, 230):
        shape.draw_line((x, 20), (x, 90))
    for y in (20, 50, 90):
        shape.draw_line((20, y), (230, y))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()
    page.insert_text((25, 40), "DESCRIPTION", fontsize=7)
    page.insert_text((125, 40), "PLACEMENT", fontsize=7)
    page.insert_text((25, 70), "NECK WIDTH", fontsize=7)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        page = document[0]
        source = page.search_for("NECK WIDTH")[0]
        protected = extract_protected_geometry(page)
        cells = find_same_row_blank_cells(page, source)
        ranked = rank_placements(
            _item(),
            page.rect,
            protected=protected,
            same_row_cells=cells,
            semantic_region=(20.0, 20.0, 230.0, 90.0),
        )
    finally:
        document.close()

    cell = next(value for value in ranked if value.strategy == "same_row_cell")
    assert cell.collision_count == 0


def test_bezier_drawing_protects_sampled_stroke_not_the_curve_bounding_box(
    tmp_path: Path,
) -> None:
    path = tmp_path / "curve.pdf"
    document = pymupdf.open()
    page = document.new_page(width=140, height=110)
    shape = page.new_shape()
    shape.draw_bezier((20, 80), (20, 20), (100, 20), (100, 80))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        protected = extract_protected_geometry(document[0])
    finally:
        document.close()

    blank_inside_bbox = pymupdf.Rect(50, 65, 70, 75)
    assert not any(
        value.kind == "drawing"
        and pymupdf.Rect(value.rect).intersects(blank_inside_bbox)
        for value in protected
    )


def test_rank_placements_scans_past_near_obstacles_and_offers_multiple_margin_slots() -> None:
    protected = [
        ProtectedGeometry("text", (102.0, 48.0, 180.0, 90.0), "near-right"),
        ProtectedGeometry("drawing", (35.0, 64.0, 130.0, 100.0), "near-below"),
    ]

    ranked = rank_placements(
        _item(text="短译文"),
        (0.0, 0.0, 320.0, 220.0),
        protected=protected,
        semantic_region=(20.0, 20.0, 260.0, 180.0),
    )

    assert any(
        value.strategy == "same_region_right"
        and value.collision_count == 0
        and value.rect[0] > 180.0
        for value in ranked
    )
    assert len([value for value in ranked if value.strategy == "margin_track"]) >= 3


def test_infer_semantic_region_uses_the_containing_table_not_an_unrelated_region(
    tmp_path: Path,
) -> None:
    path = tmp_path / "two-regions.pdf"
    document = pymupdf.open()
    page = document.new_page(width=360, height=180)
    for x0, x1 in ((20, 160), (200, 340)):
        page.draw_rect(pymupdf.Rect(x0, 20, x1, 140), width=0.8)
        page.draw_line((x0, 60), (x1, 60), width=0.8)
    page.insert_text((30, 45), "LEFT TABLE", fontsize=8)
    page.insert_text((30, 90), "NECK WIDTH", fontsize=8)
    page.insert_text((210, 45), "UNRELATED", fontsize=8)
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        page = document[0]
        region = infer_semantic_region(page, page.search_for("NECK WIDTH")[0])
    finally:
        document.close()

    assert pymupdf.Rect(region).x1 < 190


def test_new_annotation_clearance_rejects_half_point_and_accepts_exactly_one_point(
    tmp_path: Path,
) -> None:
    path = tmp_path / "new-clearance.pdf"
    document = pymupdf.open()
    document.new_page(width=240, height=180)
    document.save(path)
    document.close()
    first = _placement("first", rect=(20.0, 20.0, 70.0, 40.0))
    half = replace(first, item_id="p001-i002", rect=(70.5, 20.0, 120.5, 40.0))
    exact = replace(first, item_id="p001-i003", rect=(71.0, 60.0, 121.0, 80.0))
    exact_base = replace(first, item_id="p001-i004", rect=(20.0, 60.0, 70.0, 80.0))

    document = pymupdf.open(path)
    try:
        collisions = detect_collisions(
            document[0], [first, half, exact_base, exact], render_check=False
        )
    finally:
        document.close()

    assert any(
        value.kind == "new_annotation_clearance" and value.item_id == half.item_id
        for value in collisions
    )
    assert not any(
        value.kind == "new_annotation_clearance"
        and value.item_id in {exact.item_id, exact_base.item_id}
        for value in collisions
    )


def test_margin_leader_starts_outside_source_clearance_and_distant_collinear_line_is_safe(
    tmp_path: Path,
) -> None:
    margin = next(
        value
        for value in rank_placements(_item(), (0.0, 0.0, 300.0, 220.0))
        if value.strategy == "margin_track"
    )
    assert margin.leader_line is not None
    assert margin.leader_line[0][0] > _item().source_bbox[2] + 1.0

    path = tmp_path / "leader.pdf"
    document = pymupdf.open()
    document.new_page(width=260, height=160)
    document.save(path)
    document.close()
    distant = replace(
        _placement("leader", rect=(180.0, 40.0, 230.0, 65.0)),
        leader_line=((100.0, 10.0), (200.0, 10.0)),
    )
    protected = [ProtectedGeometry("drawing", (10.0, 9.5, 20.0, 10.5), "distant")]

    document = pymupdf.open(path)
    try:
        collisions = detect_collisions(
            document[0], [distant], protected=protected, render_check=False
        )
    finally:
        document.close()

    assert not any(value.kind == "leader_through_content" for value in collisions)


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


def test_detect_collisions_allows_blank_pixels_inside_a_page_image(
    tmp_path: Path,
) -> None:
    path = tmp_path / "blank-image-space.pdf"
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120), False)
    pixmap.clear_with(255)
    page.insert_image(pymupdf.Rect(20, 20, 220, 140), pixmap=pixmap)
    document.save(path)
    document.close()
    placement = _placement("image-blank", rect=(120.0, 80.0, 190.0, 100.0))

    document = pymupdf.open(path)
    try:
        collisions = detect_collisions(document[0], [placement])
    finally:
        document.close()

    assert collisions == []


def test_detect_collisions_ignores_light_table_fill_but_protects_dark_fill(
    tmp_path: Path,
) -> None:
    path = tmp_path / "table-fills.pdf"
    document = pymupdf.open()
    page = document.new_page(width=260, height=180)
    page.draw_rect(
        pymupdf.Rect(20, 20, 220, 70),
        color=None,
        fill=(0.92, 0.92, 0.92),
        width=0,
    )
    page.draw_rect(
        pymupdf.Rect(20, 90, 220, 140),
        color=None,
        fill=(0.25, 0.25, 0.25),
        width=0,
    )
    document.save(path)
    document.close()
    light = _placement("light", rect=(100.0, 35.0, 170.0, 55.0))
    dark = replace(
        _placement("dark", rect=(100.0, 105.0, 170.0, 125.0)),
        item_id="p001-i002",
    )

    document = pymupdf.open(path)
    try:
        light_collisions = detect_collisions(document[0], [light])
        dark_collisions = detect_collisions(document[0], [dark])
    finally:
        document.close()

    assert light_collisions == []
    assert any(
        value.kind in {"protected_drawing", "render_overlap"}
        for value in dark_collisions
    )


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


def test_detect_rendered_collisions_renders_each_real_page_once_per_dpi(
    tmp_path: Path, monkeypatch
) -> None:
    before_path = tmp_path / "before-once.pdf"
    after_path = tmp_path / "after-once.pdf"
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    page.insert_text((40, 60), "SOURCE TEXT", fontsize=10)
    document.save(before_path)
    document.save(after_path)
    document.close()
    after = pymupdf.open(after_path)
    after[0].add_freetext_annot(
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
    before_page = before[0]
    after_page = after[0]
    real_page_array = layout_module._page_array
    calls: list[tuple[str, int]] = []

    def count_page_renders(page, dpi):
        if page is before_page:
            calls.append(("before", dpi))
        elif page is after_page:
            calls.append(("after", dpi))
        return real_page_array(page, dpi)

    monkeypatch.setattr(layout_module, "_page_array", count_page_renders)
    try:
        collisions = detect_rendered_collisions(before_page, after_page, [placement])
    finally:
        after.close()
        before.close()

    assert any(value.render_dpi == 300 for value in collisions)
    assert calls.count(("before", 200)) == 1
    assert calls.count(("after", 200)) == 1
    assert calls.count(("before", 300)) == 1
    assert calls.count(("after", 300)) == 1


def test_candidate_collision_gate_renders_current_freetext_on_an_in_memory_copy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidate-render.pdf"
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    page.insert_text((40, 60), "SOURCE TEXT", fontsize=10)
    document.save(path)
    document.close()
    placement = _placement(
        "actual-over-source",
        rect=(35.0, 48.0, 115.0, 68.0),
        source_distance=0.0,
        movement_distance=0.0,
    )

    document = pymupdf.open(path)
    try:
        collisions = detect_candidate_collisions(document[0], [placement])
    finally:
        document.close()

    assert any(
        value.kind == "render_overlap" and value.render_dpi == 300
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
    assert stable.attempted_placements == tuple(initial)

    def alternate(placement, _collisions):
        x0, y0, x1, y1 = placement.rect
        shift = 1.0 if x0 == 110.0 else -1.0
        return [replace(placement, rect=(x0 + shift, y0, x1 + shift, y1))]

    render_unresolved = [
        Collision(
            0,
            "p001-i001",
            "render_overlap",
            "original_render",
            intersecting_pixels=9,
            render_dpi=300,
        )
    ]
    capped = optimize_layout(
        initial,
        collision_detector=lambda _placements: render_unresolved,
        candidate_provider=alternate,
    )
    assert capped.rounds == 10
    assert capped.stable is False
    assert capped.collisions == tuple(render_unresolved)
    assert capped.attempted_placements[0] == initial[0]
    assert len(
        {
            (value.item_id, value.rect, value.strategy)
            for value in capped.attempted_placements
        }
    ) == len(capped.attempted_placements)


def test_optimize_layout_stops_scoring_an_item_after_its_own_collisions_clear() -> None:
    first = _placement("first")
    second = replace(
        _placement("second", rect=(110.0, 90.0, 190.0, 110.0)),
        item_id="p001-i002",
    )
    first_clear = replace(first, rect=(10.0, 10.0, 90.0, 30.0), candidate_index=1)
    first_unused = replace(first, rect=(10.0, 35.0, 90.0, 55.0), candidate_index=2)
    second_clear = replace(second, rect=(200.0, 90.0, 280.0, 110.0), candidate_index=1)
    examined_first_rects: list[tuple[float, float, float, float]] = []

    def detector(placements):
        current_first, current_second = placements
        examined_first_rects.append(current_first.rect)
        collisions = []
        if current_first.rect == first.rect:
            collisions.append(Collision(0, first.item_id, "render_overlap", "source"))
        if current_second.rect == second.rect:
            collisions.append(Collision(0, second.item_id, "render_overlap", "source"))
        return collisions

    def candidates(placement, _collisions):
        if placement.item_id == first.item_id:
            return [first_clear, first_unused]
        return [second_clear]

    result = optimize_layout(
        (first, second),
        collision_detector=detector,
        candidate_provider=candidates,
    )

    assert result.collisions == ()
    assert first_unused.rect not in examined_first_rects


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_rectangular_crop_masks_align_and_attribute_source_text_collision(
    tmp_path: Path, rotation: int
) -> None:
    source = tmp_path / f"mask-{rotation}.pdf"
    after_path = tmp_path / f"mask-{rotation}-after.pdf"
    document = pymupdf.open()
    page = document.new_page(width=340, height=220)
    page.insert_text((60, 80), "SOURCE MARK", fontsize=12)
    page.set_cropbox(pymupdf.Rect(20, 20, 320, 200))
    page.set_rotation(rotation)
    document.save(source)
    document.close()
    shutil.copyfile(source, after_path)
    before = pymupdf.open(source)
    source_bbox = before[0].search_for("SOURCE MARK")[0]
    placement = _placement(
        "over-source",
        rect=tuple(source_bbox),
        font_size=7.0,
    )
    after = pymupdf.open(after_path)
    annotation = after[0].add_freetext_annot(
        placement.rect,
        placement.text,
        fontsize=placement.font_size,
        fontname="china-s",
        text_color=(0.85, 0.05, 0.05),
        fill_color=None,
        border_color=None,
        border_width=0,
    )
    annotation.update()
    after.saveIncr()
    after.close()
    after = pymupdf.open(after_path)
    try:
        expected_shape = (
            int(round(before[0].rect.height * 300 / 72)),
            int(round(before[0].rect.width * 300 / 72)),
        )
        mask = layout_module._placement_mask(before[0], placement, 300)
        actual_red = layout_module._red_mask(layout_module._page_array(after[0], 300))
        assert mask.shape == expected_shape
        assert actual_red.shape == expected_shape
        assert np.count_nonzero(mask & actual_red) > 4
        collisions = detect_rendered_collisions(before[0], after[0], (placement,))
        assert any(
            value.item_id == placement.item_id
            and value.kind == "render_overlap"
            and value.render_dpi == 300
            for value in collisions
        )
    finally:
        after.close()
        before.close()


@pytest.mark.parametrize(
    ("edge", "expected_in_bounds"),
    [(0.0, False), (0.5, False), (0.999, False), (1.0, True), (1.001, True)],
)
def test_review_target_requires_one_point_cropbox_clearance(
    edge: float, expected_in_bounds: bool
) -> None:
    item = _item().model_copy(
        update={"target_rect": [edge, 30.0, 80.0, 50.0], "font_size": 7.0}
    )
    candidate = next(
        value
        for value in rank_placements(item, (0.0, 0.0, 300.0, 180.0))
        if value.strategy == "review_target"
    )
    assert candidate.in_bounds is expected_in_bounds


def test_leader_segments_require_one_point_cropbox_clearance_on_rotated_rectangle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "leader-boundary.pdf"
    document = pymupdf.open()
    page = document.new_page(width=340, height=220)
    page.set_cropbox(pymupdf.Rect(20, 20, 320, 200))
    page.set_rotation(90)
    document.save(source)
    document.close()
    document = pymupdf.open(source)
    try:
        unsafe = _placement(
            "margin",
            rect=(200.0, 30.0, 285.0, 60.0),
            leader=((0.5, 40.0), (190.0, 40.0), (200.0, 45.0)),
        )
        exact = replace(
            unsafe,
            leader_line=((1.0, 40.0), (190.0, 40.0), (200.0, 45.0)),
        )
        assert any(
            value.kind == "leader_out_of_bounds"
            for value in detect_collisions(document[0], (unsafe,), render_check=False)
        )
        assert not any(
            value.kind == "leader_out_of_bounds"
            for value in detect_collisions(document[0], (exact,), render_check=False)
        )
    finally:
        document.close()


def test_optimizer_reports_only_actually_evaluated_candidates_in_order() -> None:
    initial = _placement("initial", rect=(10.0, 10.0, 50.0, 30.0))
    first = replace(initial, strategy="first", rect=(60.0, 10.0, 100.0, 30.0))
    second = replace(initial, strategy="second", rect=(110.0, 10.0, 150.0, 30.0))
    never = replace(initial, strategy="never", rect=(160.0, 10.0, 200.0, 30.0))

    def detector(placements):
        value = placements[0]
        if value.strategy == "second":
            return []
        return [Collision(0, value.item_id, "protected_text", "glyph")]

    result = optimize_layout(
        (initial,),
        collision_detector=detector,
        candidate_provider=lambda *_args: (first, second, never),
    )

    assert [value.strategy for value in result.attempted_placements] == [
        "initial",
        "first",
        "second",
    ]


def test_protected_geometry_includes_widgets_and_links(tmp_path: Path) -> None:
    path = tmp_path / "interactive-geometry.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=180)
    widget = pymupdf.Widget()
    widget.field_name = "approval"
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    widget.rect = pymupdf.Rect(20, 20, 42, 42)
    page.add_widget(widget)
    page = document.reload_page(page)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(60, 20, 125, 42),
            "uri": "https://example.invalid",
        }
    )
    document.save(path)
    document.close()

    document = pymupdf.open(path)
    try:
        page = document[0]
        widget_xref = next(page.widgets()).xref
        link_xref = page.get_links()[0]["xref"]
        protected = extract_protected_geometry(page)
    finally:
        document.close()

    assert any(
        value.kind == "widget"
        and value.object_id == f"widget-{widget_xref}"
        and pymupdf.Rect(value.rect) == pymupdf.Rect(20, 20, 42, 42)
        for value in protected
    )
    assert any(
        value.kind == "link"
        and value.object_id == f"link-{link_xref}"
        and pymupdf.Rect(value.rect) == pymupdf.Rect(60, 20, 125, 42)
        for value in protected
    )
