"""Adapt document layout plans into trusted, non-mutating review previews."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf

from .layout import plan_document_layout
from .models import ReviewItem


class PreviewPlanningError(ValueError):
    """Raised when every review item cannot receive a preview placement."""


def plan_review_items(
    source_pdf: str | Path, items: Sequence[ReviewItem]
) -> tuple[ReviewItem, ...]:
    """Return review items populated from a read-only whole-document layout plan."""
    document = pymupdf.open(source_pdf)
    try:
        layout, _attempted = plan_document_layout(document, items)
    finally:
        document.close()

    placements = {placement.item_id: placement for placement in layout.placements}
    unsafe = {collision.item_id for collision in layout.collisions}
    planned: list[ReviewItem] = []
    for item in items:
        placement = placements.get(item.item_id)
        if placement is None:
            raise PreviewPlanningError("review item has no planned placement")
        warnings = list(item.warnings)
        if item.item_id in unsafe:
            warnings = [
                warning
                for warning in warnings
                if warning != "manual_placement_required"
            ]
            warnings.append("manual_placement_required")
        planned.append(
            item.model_copy(
                update={
                    "placement_strategy": placement.strategy,
                    "target_rect": list(placement.rect),
                    "font_size": placement.font_size,
                    "leader_line": None,
                    "warnings": warnings,
                    "risk_level": (
                        "high" if item.item_id in unsafe else item.risk_level
                    ),
                    "reviewed_target_rect": None,
                }
            )
        )
    return tuple(planned)


__all__ = ["PreviewPlanningError", "plan_review_items"]
