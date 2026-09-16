from __future__ import annotations

import pymupdf

import techpack_pdf.layout as layout_module
from techpack_pdf.layout import Placement, plan_document_layout
from techpack_pdf.models import ReviewItem


def _item() -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "item_id": "p001-i001",
            "page_index": 0,
            "page_type": "measurement",
            "source_text": "WAIST RELAXED (HALF)",
            "normalized_text": "waist relaxed (half)",
            "source_bbox": [61.0, 106.0, 117.0, 113.0],
            "source_kind": "description",
            "coordinate_confidence": "low",
            "decision_reason": "glossary_hit",
            "locked_tokens": [],
            "glossary_hits": [],
            "suggested_translation": "腰围松量",
            "reviewed_translation": None,
            "review_status": None,
            "risk_level": "high",
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


def _placement(
    strategy: str,
    rect: tuple[float, float, float, float],
    *,
    in_bounds: bool,
    movement_distance: float,
) -> Placement:
    return Placement(
        item_id="p001-i001",
        page_index=0,
        text="腰围松量",
        rect=rect,
        font_size=5.0,
        strategy=strategy,
        wrapped_lines=("腰围松量",),
        same_semantic_region=strategy == "same_region_right",
        leader_line=None,
        collision_count=0,
        in_bounds=in_bounds,
        source_distance=movement_distance,
        movement_distance=movement_distance,
        candidate_index=0,
    )


def test_document_plan_rejects_out_of_bounds_and_distant_blank_fallbacks(
    monkeypatch,
) -> None:
    out_of_bounds = _placement(
        "same_region_left", (-84.0, 106.0, 7.0, 118.0),
        in_bounds=False, movement_distance=134.0,
    )
    distant_fallback = _placement(
        "page_blank_fallback", (373.0, 440.0, 469.0, 450.0),
        in_bounds=True, movement_distance=471.0,
    )
    monkeypatch.setattr(
        layout_module,
        "rank_placements",
        lambda *_args, **_kwargs: [out_of_bounds, distant_fallback],
    )
    document = pymupdf.open()
    document.new_page(width=841.89, height=595.276)
    try:
        planned, _attempted = plan_document_layout(document, [_item()])
    finally:
        document.close()

    assert planned.placements == ()
    assert [(collision.item_id, collision.kind) for collision in planned.collisions] == [
        ("p001-i001", "candidate_exhausted")
    ]
