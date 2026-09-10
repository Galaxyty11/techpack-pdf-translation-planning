from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

import techpack_pdf.review_preview as review_preview
from techpack_pdf.layout import LayoutResult
from techpack_pdf.models import ReviewItem
from techpack_pdf.review_preview import PreviewPlanningError, plan_review_items


def _review_item(
    *,
    target_rect: list[float] | None = None,
    font_size: float | None = None,
) -> ReviewItem:
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
            "suggested_translation": "领宽（HPS 到 HPS）",
            "reviewed_translation": None,
            "review_status": None,
            "risk_level": "low",
            "translation_host": "codex",
            "translation_execution_mode": "main_agent",
            "translation_model": "unknown",
            "translation_agent_role": "techpack-translator",
            "translation_prompt_version": "1.0",
            "placement_strategy": None,
            "target_rect": target_rect,
            "reviewed_target_rect": None,
            "font_size": font_size,
            "leader_line": None,
            "warnings": [],
        }
    )


def _simple_pdf(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=300, height=220)
    page.insert_text((40, 60), "NECK WIDTH - HPS TO HPS")
    document.save(path)
    document.close()
    return path


def _page_with_no_safe_space(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=200, height=120)
    page.draw_rect(page.rect, color=(0, 0, 0), fill=(0, 0, 0))
    document.save(path)
    document.close()
    return path


def test_plan_review_items_populates_displayable_layout_without_writing_pdf(tmp_path):
    source = _simple_pdf(tmp_path / "source.pdf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    planned = plan_review_items(
        source, [_review_item(target_rect=None, font_size=None)]
    )

    assert planned[0].target_rect is not None
    assert planned[0].font_size in {7.0, 6.5, 6.0, 5.5, 5.0}
    assert planned[0].placement_strategy
    assert planned[0].reviewed_target_rect is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_plan_review_items_marks_unsafe_best_attempt_for_manual_placement(tmp_path):
    source = _page_with_no_safe_space(tmp_path / "dense.pdf")

    planned = plan_review_items(source, [_review_item()])

    assert planned[0].target_rect is not None
    assert planned[0].risk_level == "high"
    assert "manual_placement_required" in planned[0].warnings


def test_plan_review_items_normalizes_only_manual_placement_warning(tmp_path):
    source = _page_with_no_safe_space(tmp_path / "dense.pdf")
    item = _review_item().model_copy(
        update={
            "warnings": [
                "coordinate_confidence",
                "manual_placement_required",
                "translator_warning",
                "manual_placement_required",
                "translator_warning",
            ]
        }
    )

    planned = plan_review_items(source, [item])

    assert planned[0].warnings == [
        "coordinate_confidence",
        "translator_warning",
        "translator_warning",
        "manual_placement_required",
    ]
    assert planned[0].warnings.count("manual_placement_required") == 1


def test_plan_review_items_rejects_an_item_without_a_planned_placement(
    tmp_path, monkeypatch
):
    source = _simple_pdf(tmp_path / "source.pdf")
    monkeypatch.setattr(
        review_preview,
        "plan_document_layout",
        lambda _document, _items: (LayoutResult((), (), 0, True), {}),
    )

    with pytest.raises(PreviewPlanningError):
        plan_review_items(source, [_review_item()])
