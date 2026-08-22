"""Deterministic FreeText placement and collision detection."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

import cv2
import numpy as np
import pymupdf

from .models import ReviewItem, ReviewStatus


RectTuple: TypeAlias = tuple[float, float, float, float]
PointTuple: TypeAlias = tuple[float, float]
FONT_SIZES = (7.0, 6.5, 6.0, 5.5, 5.0)
TEXT_COLOR = (0.85, 0.05, 0.05)
MIN_CLEARANCE_PT = 1.0
TOOL_CJK_FONT = "china-s"


@dataclass(frozen=True)
class ProtectedGeometry:
    kind: str
    rect: RectTuple
    object_id: str
    key_drawing: bool = False


@dataclass(frozen=True)
class Placement:
    item_id: str
    page_index: int
    text: str
    rect: RectTuple
    font_size: float
    strategy: str
    wrapped_lines: tuple[str, ...]
    same_semantic_region: bool
    leader_line: tuple[PointTuple, ...] | None
    collision_count: int
    in_bounds: bool
    source_distance: float
    movement_distance: float
    candidate_index: int


@dataclass(frozen=True)
class Collision:
    page_index: int
    item_id: str
    kind: str
    object_id: str
    intersecting_pixels: int = 0
    render_dpi: int | None = None


@dataclass(frozen=True)
class LayoutResult:
    placements: tuple[Placement, ...]
    collisions: tuple[Collision, ...]
    rounds: int
    stable: bool


def rank_placements(
    item: ReviewItem,
    crop_box: pymupdf.Rect | Sequence[float],
    *,
    protected: Sequence[ProtectedGeometry] = (),
    same_row_cells: Sequence[pymupdf.Rect | Sequence[float]] = (),
    semantic_region: pymupdf.Rect | Sequence[float] | None = None,
    candidates: Sequence[Placement] | None = None,
) -> list[Placement]:
    """Generate and rank placements using the contract's exact priority chain."""
    page_rect = _rect(crop_box)
    if candidates is None:
        generated = _generate_candidates(
            item,
            page_rect,
            protected=protected,
            same_row_cells=same_row_cells,
            semantic_region=_rect(semantic_region) if semantic_region is not None else page_rect,
        )
    else:
        generated = list(candidates)
    return sorted(generated, key=_placement_sort_key)


def wrap_text(
    text: str,
    max_width: float,
    font_size: float,
    *,
    font: pymupdf.Font | None = None,
) -> tuple[str, ...]:
    """Wrap using actual MuPDF glyph widths, including CJK glyph metrics."""
    if max_width <= 0 or font_size <= 0:
        return ()
    metric_font = font or pymupdf.Font(fontname=TOOL_CJK_FONT)
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            proposal = current + character
            if current and metric_font.text_length(proposal, fontsize=font_size) > max_width:
                lines.append(current.rstrip())
                current = character.lstrip() if character.isspace() else character
            else:
                current = proposal
        if current or not lines:
            lines.append(current.rstrip())
    return tuple(lines)


def extract_protected_geometry(page: pymupdf.Page) -> tuple[ProtectedGeometry, ...]:
    """Return glyph, image, drawing and pre-existing annotation geometry."""
    protected: list[ProtectedGeometry] = []
    raw = page.get_text("rawdict")
    glyph_index = 0
    for block in raw.get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                for character in span.get("chars", ()):
                    bbox = pymupdf.Rect(character["bbox"])
                    if _nonempty(bbox):
                        protected.append(
                            ProtectedGeometry(
                                "text", _tuple(bbox), f"glyph-{glyph_index}"
                            )
                        )
                        glyph_index += 1
    for index, image in enumerate(page.get_image_info()):
        bbox = pymupdf.Rect(image["bbox"])
        if _nonempty(bbox):
            protected.append(ProtectedGeometry("image", _tuple(bbox), f"image-{index}"))
    for index, drawing in enumerate(page.get_drawings()):
        protected.extend(_drawing_geometries(drawing, index))
    for annotation in page.annots():
        bbox = pymupdf.Rect(annotation.rect)
        if _nonempty(bbox):
            protected.append(
                ProtectedGeometry(
                    "annotation", _tuple(bbox), f"annotation-{annotation.xref}"
                )
            )
    return tuple(protected)


def infer_semantic_region(
    page: pymupdf.Page,
    source_bbox: pymupdf.Rect | Sequence[float],
) -> RectTuple:
    """Infer the smallest native table or text-block region containing the source."""
    source = pymupdf.Rect(source_bbox)
    center = pymupdf.Point(
        (source.x0 + source.x1) / 2.0, (source.y0 + source.y1) / 2.0
    )
    table_regions: list[pymupdf.Rect] = []
    try:
        for table in page.find_tables().tables:
            region = pymupdf.Rect(table.bbox)
            if region.contains(center):
                table_regions.append(region)
    except (AttributeError, RuntimeError, ValueError):
        pass
    if table_regions:
        return _tuple(min(table_regions, key=lambda rect: rect.get_area()))

    blocks = []
    for block in page.get_text("blocks"):
        region = pymupdf.Rect(block[:4])
        if region.contains(center) or _overlaps(region, source):
            blocks.append(region)
    if blocks:
        block = min(blocks, key=lambda rect: rect.get_area())
        expanded = _expand(block, 18.0) & page.rect
        return _tuple(expanded)
    return _tuple(page.rect)


def find_same_row_blank_cells(
    page: pymupdf.Page,
    source_bbox: pymupdf.Rect | Sequence[float],
) -> list[RectTuple]:
    """Find blank Placement / Notes cells in the source text's table row."""
    source = pymupdf.Rect(source_bbox)
    matches: list[RectTuple] = []
    try:
        tables = page.find_tables().tables
    except (AttributeError, RuntimeError, ValueError):
        return matches
    for table in tables:
        values = table.extract()
        if not values or not table.rows:
            continue
        headers = [str(value or "").strip().casefold() for value in values[0]]
        preferred_columns = {
            index
            for index, header in enumerate(headers)
            if any(label in header for label in ("placement", "note", "remark"))
        }
        if not preferred_columns:
            continue
        for row_index, row in enumerate(table.rows):
            row_cells = row.cells
            if not any(
                cell is not None and _overlaps(source, pymupdf.Rect(cell))
                for cell in row_cells
            ):
                continue
            row_values = values[row_index] if row_index < len(values) else []
            for column in sorted(preferred_columns):
                if column >= len(row_cells) or row_cells[column] is None:
                    continue
                value = row_values[column] if column < len(row_values) else None
                if str(value or "").strip():
                    continue
                cell = pymupdf.Rect(row_cells[column])
                inset_amount = MIN_CLEARANCE_PT + 1.0
                inset = pymupdf.Rect(
                    cell.x0 + inset_amount,
                    cell.y0 + inset_amount,
                    cell.x1 - inset_amount,
                    cell.y1 - inset_amount,
                )
                if _nonempty(inset):
                    candidate = _tuple(inset)
                    if candidate not in matches:
                        matches.append(candidate)
    return sorted(matches)


def detect_collisions(
    page: pymupdf.Page,
    placements: Sequence[Placement],
    *,
    protected: Sequence[ProtectedGeometry] | None = None,
    render_check: bool = True,
) -> list[Collision]:
    """Detect geometry, clipping, new-new, leader and rendered glyph collisions."""
    page_rect = pymupdf.Rect(page.rect)
    obstacles = list(extract_protected_geometry(page))
    if protected is not None:
        obstacles.extend(protected)
    collisions: list[Collision] = []

    for placement in placements:
        body = pymupdf.Rect(placement.rect)
        if not page_rect.contains(body) or not _nonempty(body):
            collisions.append(
                Collision(page.number, placement.item_id, "out_of_bounds", "crop_box")
            )
        font = pymupdf.Font(fontname=TOOL_CJK_FONT)
        required_height = placement.font_size * 1.35 * max(
            len(placement.wrapped_lines), 1
        ) + 3.0
        usable_width = max(body.width - 4.0, 0.0)
        if required_height > body.height + 1e-6 or any(
            font.text_length(line, fontsize=placement.font_size) > usable_width + 1e-6
            for line in placement.wrapped_lines
        ):
            collisions.append(
                Collision(page.number, placement.item_id, "text_clipped", "annotation_rect")
            )
        for obstacle in obstacles:
            if _overlaps(body, _expand(pymupdf.Rect(obstacle.rect), MIN_CLEARANCE_PT)):
                collisions.append(
                    Collision(
                        page.number,
                        placement.item_id,
                        f"protected_{obstacle.kind}",
                        obstacle.object_id,
                    )
                )
            if placement.leader_line and _polyline_hits_rect(
                placement.leader_line,
                _expand(pymupdf.Rect(obstacle.rect), MIN_CLEARANCE_PT),
            ):
                collisions.append(
                    Collision(
                        page.number,
                        placement.item_id,
                        "leader_through_content",
                        obstacle.object_id,
                    )
                )

    for index, first in enumerate(placements):
        for second in placements[index + 1 :]:
            if _overlaps(pymupdf.Rect(first.rect), pymupdf.Rect(second.rect)):
                collisions.extend(
                    (
                        Collision(
                            page.number,
                            first.item_id,
                            "new_annotation_overlap",
                            second.item_id,
                        ),
                        Collision(
                            page.number,
                            second.item_id,
                            "new_annotation_overlap",
                            first.item_id,
                        ),
                    )
                )
            elif _overlaps(
                _expand(pymupdf.Rect(first.rect), MIN_CLEARANCE_PT),
                pymupdf.Rect(second.rect),
            ):
                collisions.extend(
                    (
                        Collision(
                            page.number,
                            first.item_id,
                            "new_annotation_clearance",
                            second.item_id,
                        ),
                        Collision(
                            page.number,
                            second.item_id,
                            "new_annotation_clearance",
                            first.item_id,
                        ),
                    )
                )
            for owner, line, other in (
                (first, first.leader_line, second),
                (second, second.leader_line, first),
            ):
                if line and _polyline_hits_rect(
                    line,
                    _expand(pymupdf.Rect(other.rect), MIN_CLEARANCE_PT),
                ):
                    collisions.append(
                        Collision(
                            page.number,
                            owner.item_id,
                            "leader_through_annotation",
                            other.item_id,
                        )
                    )

    if render_check and placements:
        collisions.extend(_render_collisions(page, placements))
    return _deduplicate_collisions(collisions)


def detect_rendered_collisions(
    before_page: pymupdf.Page,
    after_page: pymupdf.Page,
    placements: Sequence[Placement],
) -> list[Collision]:
    """Validate actual annotation appearances by before/after render differencing."""
    if (
        before_page.rect.width != after_page.rect.width
        or before_page.rect.height != after_page.rect.height
    ):
        return [
            Collision(
                before_page.number,
                placement.item_id,
                "render_size_mismatch",
                "crop_box",
            )
            for placement in placements
        ]
    collision_200 = _render_diff_mask(before_page, after_page, 200)
    if int(np.count_nonzero(collision_200)) <= 4:
        return []
    collision_300 = _render_diff_mask(before_page, after_page, 300)
    if int(np.count_nonzero(collision_300)) <= 4:
        return []

    collisions: list[Collision] = []
    for placement in placements:
        expected = _placement_mask(before_page.rect, placement, 300)
        pixels = int(np.count_nonzero(collision_300 & expected))
        if pixels > 4:
            collisions.append(
                Collision(
                    before_page.number,
                    placement.item_id,
                    "render_overlap",
                    "original_render",
                    pixels,
                    300,
                )
            )
    if not collisions:
        total = int(np.count_nonzero(collision_300))
        collisions.extend(
            Collision(
                before_page.number,
                placement.item_id,
                "render_overlap",
                "original_render",
                total,
                300,
            )
            for placement in placements
        )
    return _deduplicate_collisions(collisions)


def detect_candidate_collisions(
    page: pymupdf.Page,
    placements: Sequence[Placement],
) -> list[Collision]:
    """Run geometry plus true before/after rendering for a candidate page layout."""
    collisions = detect_collisions(page, placements, render_check=False)
    if not placements:
        return collisions
    rendered = pymupdf.open()
    try:
        rendered.insert_pdf(
            page.parent, from_page=page.number, to_page=page.number, annots=True
        )
        target = rendered[0]
        for placement in placements:
            annotation = target.add_freetext_annot(
                placement.rect,
                placement.text,
                fontsize=placement.font_size,
                fontname=TOOL_CJK_FONT,
                text_color=TEXT_COLOR,
                fill_color=None,
                border_color=None,
                border_width=0,
                callout=placement.leader_line,
            )
            annotation.update()
        collisions.extend(detect_rendered_collisions(page, target, placements))
    finally:
        rendered.close()
    return _deduplicate_collisions(collisions)


def optimize_layout(
    initial: Sequence[Placement],
    *,
    collision_detector: Callable[[Sequence[Placement]], Sequence[Collision]],
    candidate_provider: Callable[[Placement, Sequence[Collision]], Sequence[Placement]],
    max_rounds: int = 10,
) -> LayoutResult:
    """Re-evaluate the whole layout after every move, with bounded stabilization."""
    if max_rounds <= 0:
        raise ValueError("max_rounds must be positive")
    current = list(initial)
    previous_signature = _layout_signature(current)
    unchanged_rounds = 0
    final_collisions = list(collision_detector(current))
    if not final_collisions:
        return LayoutResult(tuple(current), (), 0, False)

    for round_number in range(1, max_rounds + 1):
        colliding_ids = {collision.item_id for collision in final_collisions}
        for index, placement in enumerate(tuple(current)):
            if placement.item_id not in colliding_ids:
                continue
            relevant = [
                collision
                for collision in final_collisions
                if collision.item_id == placement.item_id
            ]
            choices = list(candidate_provider(placement, relevant))
            if choices:
                evaluated: list[
                    tuple[tuple[object, ...], Placement, list[Collision]]
                ] = []
                for choice in choices:
                    trial = list(current)
                    trial[index] = choice
                    trial_collisions = list(collision_detector(trial))
                    own_count = sum(
                        collision.item_id == choice.item_id
                        for collision in trial_collisions
                    )
                    evaluated.append(
                        (
                            (own_count, len(trial_collisions), _placement_sort_key(choice)),
                            choice,
                            trial_collisions,
                        )
                    )
                    if own_count == 0 and not trial_collisions:
                        break
                _score, selected, selected_collisions = min(
                    evaluated, key=lambda value: value[0]
                )
                current[index] = selected
                final_collisions = selected_collisions
                colliding_ids = {collision.item_id for collision in final_collisions}

        signature = _layout_signature(current)
        if signature == previous_signature:
            unchanged_rounds += 1
        else:
            unchanged_rounds = 0
        previous_signature = signature
        final_collisions = list(collision_detector(current))
        if not final_collisions:
            return LayoutResult(tuple(current), (), round_number, False)
        if unchanged_rounds >= 2:
            return LayoutResult(
                tuple(current), tuple(final_collisions), round_number, True
            )
    return LayoutResult(tuple(current), tuple(final_collisions), max_rounds, False)


def _generate_candidates(
    item: ReviewItem,
    page_rect: pymupdf.Rect,
    *,
    protected: Sequence[ProtectedGeometry],
    same_row_cells: Sequence[pymupdf.Rect | Sequence[float]],
    semantic_region: pymupdf.Rect,
) -> list[Placement]:
    text = _final_text(item)
    source = pymupdf.Rect(item.source_bbox)
    raw: list[tuple[str, pymupdf.Rect, float, bool, tuple[PointTuple, ...] | None]] = []
    index = 0

    if item.target_rect is not None:
        font_size = _bounded_font(item.font_size or 7.0)
        raw.append(
            ("review_target", pymupdf.Rect(item.target_rect), font_size, True, None)
        )

    base_width = min(max(source.width * 1.35, 72.0), max(72.0, semantic_region.width * 0.45))
    gap = 4.0
    for font_size in FONT_SIZES:
        for cell in same_row_cells:
            raw.append(("same_row_cell", _rect(cell), font_size, True, None))

        nominal_lines = wrap_text(text, base_width, font_size)
        height = max(font_size * 1.35 * max(len(nominal_lines), 1) + 3.0, font_size + 4.0)
        positions = (
            ("same_region_above", pymupdf.Rect(source.x0, source.y0 - gap - height, source.x0 + base_width, source.y0)),
            ("same_region_below", pymupdf.Rect(source.x0, source.y1 + gap, source.x0 + base_width, source.y1 + gap + height)),
            ("same_region_right", pymupdf.Rect(source.x1 + gap, source.y0, source.x1 + gap + base_width, source.y0 + height)),
            ("same_region_left", pymupdf.Rect(source.x0 - gap - base_width, source.y0, source.x0 - gap, source.y0 + height)),
        )
        for strategy, rect in positions:
            raw.append((strategy, rect, font_size, True, None))

        for strategy, factor in (
            ("same_region_wide_wrap", 1.5),
            ("same_region_narrow_wrap", 0.72),
        ):
            width = max(48.0, min(base_width * factor, semantic_region.width))
            lines = wrap_text(text, width, font_size)
            changed_height = max(font_size * 1.35 * max(len(lines), 1) + 3.0, font_size + 4.0)
            raw.append(
                (
                    strategy,
                    pymupdf.Rect(source.x1 + gap, source.y0, source.x1 + gap + width, source.y0 + changed_height),
                    font_size,
                    True,
                    None,
                )
            )

        step_x = max(base_width * 0.55, 28.0)
        step_y = max(height * 0.9, 14.0)
        for step in range(1, 5):
            offsets = (
                ("same_region_above", 0.0, -step * step_y),
                ("same_region_below", 0.0, step * step_y),
                ("same_region_right", step * step_x, 0.0),
                ("same_region_left", -step * step_x, 0.0),
            )
            for direction, dx, dy in offsets:
                base = dict(positions)[direction]
                shifted = pymupdf.Rect(base.x0 + dx, base.y0 + dy, base.x1 + dx, base.y1 + dy)
                raw.append((direction, shifted, font_size, True, None))
                for suffix, factor in (("wide_wrap", 1.35), ("narrow_wrap", 0.75)):
                    width = max(48.0, min(base_width * factor, semantic_region.width))
                    lines = wrap_text(text, width, font_size)
                    variant_height = max(
                        font_size * 1.35 * max(len(lines), 1) + 3.0,
                        font_size + 4.0,
                    )
                    variant = pymupdf.Rect(
                        shifted.x0,
                        shifted.y0,
                        shifted.x0 + width,
                        shifted.y0 + variant_height,
                    )
                    raw.append(
                        (
                            f"{direction}_{suffix}",
                            variant,
                            font_size,
                            True,
                            None,
                        )
                    )

    margin_width = min(92.0, max(60.0, page_rect.width * 0.25))
    margin_font = 5.0
    margin_lines = wrap_text(text, margin_width - 6.0, margin_font)
    margin_height = max(margin_font * 1.35 * max(len(margin_lines), 1) + 4.0, 14.0)
    source_y = source.y0 + source.height / 2.0
    margin_ys = {
        min(max(page_rect.y0 + 4.0, value), page_rect.y1 - margin_height - 4.0)
        for value in (
            source.y0,
            page_rect.y0 + 4.0,
            page_rect.y1 - margin_height - 4.0,
            source.y0 - margin_height - 6.0,
            source.y1 + 6.0,
        )
    }
    for margin_y in sorted(margin_ys, key=lambda value: (abs(value - source.y0), value)):
        margin = pymupdf.Rect(
            page_rect.x1 - margin_width - 4.0,
            margin_y,
            page_rect.x1 - 4.0,
            margin_y + margin_height,
        )
        source_anchor = (source.x1 + MIN_CLEARANCE_PT + 0.01, source_y)
        margin_anchor = (margin.x0, margin.y0 + margin.height / 2.0)
        elbow = (margin.x0 - 4.0, source_y)
        raw.append(
            (
                "margin_track",
                margin,
                margin_font,
                False,
                (source_anchor, elbow, margin_anchor),
            )
        )

    placements: list[Placement] = []
    for strategy, rect, font_size, intended_same_region, leader in raw:
        lines = wrap_text(text, max(rect.width - 4.0, 1.0), font_size)
        in_bounds = page_rect.contains(rect) and _nonempty(rect)
        same_region = intended_same_region and semantic_region.contains(rect)
        collision_count = sum(
            _overlaps(rect, _expand(pymupdf.Rect(obstacle.rect), MIN_CLEARANCE_PT))
            for obstacle in protected
        )
        placements.append(
            Placement(
                item_id=item.item_id,
                page_index=item.page_index,
                text=text,
                rect=_tuple(rect),
                font_size=font_size,
                strategy=strategy,
                wrapped_lines=lines,
                same_semantic_region=same_region,
                leader_line=leader,
                collision_count=int(collision_count),
                in_bounds=in_bounds,
                source_distance=_rect_distance(source, rect),
                movement_distance=_center_distance(source, rect),
                candidate_index=index,
            )
        )
        index += 1
    return placements


def _placement_sort_key(placement: Placement) -> tuple[object, ...]:
    valid = placement.collision_count == 0 and placement.in_bounds
    return (
        not valid,
        not placement.same_semantic_region,
        placement.leader_line is not None,
        round(placement.source_distance, 6),
        -placement.font_size,
        round(placement.movement_distance, 6),
        placement.candidate_index,
        placement.strategy,
        tuple(round(value, 6) for value in placement.rect),
        placement.item_id,
    )


def _render_collisions(
    page: pymupdf.Page, placements: Sequence[Placement]
) -> list[Collision]:
    base_200 = _page_array(page, 200)
    protected_200 = cv2.dilate(
        _content_mask(base_200).astype(np.uint8), np.ones((5, 5), np.uint8)
    ).astype(bool)
    glyph_masks_200 = [_placement_mask(page.rect, placement, 200) for placement in placements]
    potential: set[int] = set()
    for index, mask in enumerate(glyph_masks_200):
        if int(np.count_nonzero(mask & protected_200)) > 4:
            potential.add(index)
        for other_index, other in enumerate(glyph_masks_200):
            if other_index >= index:
                continue
            dilated = cv2.dilate(other.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
            if int(np.count_nonzero(mask & dilated)) > 4:
                potential.update((index, other_index))

    if not potential:
        return []
    base_300 = _page_array(page, 300)
    protected_300 = cv2.dilate(
        _content_mask(base_300).astype(np.uint8), np.ones((5, 5), np.uint8)
    ).astype(bool)
    glyph_masks_300 = [_placement_mask(page.rect, placement, 300) for placement in placements]
    collisions: list[Collision] = []
    for index in sorted(potential):
        pixels = int(np.count_nonzero(glyph_masks_300[index] & protected_300))
        if pixels > 4:
            collisions.append(
                Collision(
                    page.number,
                    placements[index].item_id,
                    "render_overlap",
                    "original_render",
                    pixels,
                    300,
                )
            )
    for index, first in enumerate(glyph_masks_300):
        for other_index in range(index):
            other = cv2.dilate(
                glyph_masks_300[other_index].astype(np.uint8),
                np.ones((5, 5), np.uint8),
            ).astype(bool)
            pixels = int(np.count_nonzero(first & other))
            if pixels > 4:
                collisions.extend(
                    (
                        Collision(page.number, placements[index].item_id, "render_new_overlap", placements[other_index].item_id, pixels, 300),
                        Collision(page.number, placements[other_index].item_id, "render_new_overlap", placements[index].item_id, pixels, 300),
                    )
                )
    return collisions


def _render_diff_mask(
    before_page: pymupdf.Page, after_page: pymupdf.Page, dpi: int
) -> np.ndarray:
    before_pixels = _page_array(before_page, dpi)
    after_pixels = _page_array(after_page, dpi)
    newly_red = _red_mask(after_pixels) & ~_red_mask(before_pixels)
    protected = cv2.dilate(
        _content_mask(before_pixels).astype(np.uint8),
        np.ones((5, 5), np.uint8),
    ).astype(bool)
    return newly_red & protected


def _placement_mask(
    page_rect: pymupdf.Rect, placement: Placement, dpi: int
) -> np.ndarray:
    document = pymupdf.open()
    try:
        blank = document.new_page(width=page_rect.width, height=page_rect.height)
        annotation = blank.add_freetext_annot(
            placement.rect,
            placement.text,
            fontsize=placement.font_size,
            fontname=TOOL_CJK_FONT,
            text_color=TEXT_COLOR,
            fill_color=None,
            border_color=None,
            border_width=0,
            callout=placement.leader_line,
        )
        annotation.update()
        pixels = _page_array(blank, dpi)
        return _red_mask(pixels)
    finally:
        document.close()


def _page_array(page: pymupdf.Page, dpi: int) -> np.ndarray:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB, annots=True)
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )[:, :, :3]


def _content_mask(pixels: np.ndarray) -> np.ndarray:
    return np.any(pixels < 245, axis=2)


def _red_mask(pixels: np.ndarray) -> np.ndarray:
    red = pixels[:, :, 0].astype(np.int16)
    green = pixels[:, :, 1].astype(np.int16)
    blue = pixels[:, :, 2].astype(np.int16)
    return (red - green > 45) & (red - blue > 45) & (green < 190) & (blue < 190)


def _final_text(item: ReviewItem) -> str:
    if item.review_status is ReviewStatus.APPROVED_EDITED:
        return item.reviewed_translation or ""
    return item.suggested_translation or ""


def _bounded_font(value: float) -> float:
    return min(7.0, max(5.0, round(value * 2.0) / 2.0))


def _layout_signature(placements: Sequence[Placement]) -> tuple[object, ...]:
    return tuple(
        (
            value.item_id,
            tuple(round(number, 4) for number in value.rect),
            value.font_size,
            value.strategy,
            value.leader_line,
        )
        for value in sorted(placements, key=lambda placement: placement.item_id)
    )


def _deduplicate_collisions(collisions: Iterable[Collision]) -> list[Collision]:
    unique: dict[tuple[object, ...], Collision] = {}
    for collision in collisions:
        key = (
            collision.page_index,
            collision.item_id,
            collision.kind,
            collision.object_id,
            collision.render_dpi,
        )
        unique[key] = collision
    return sorted(
        unique.values(),
        key=lambda value: (
            value.page_index,
            value.item_id,
            value.kind,
            value.object_id,
            value.render_dpi or 0,
        ),
    )


def _minimum_visible_rect(rect: pymupdf.Rect) -> pymupdf.Rect:
    result = pymupdf.Rect(rect)
    if result.width <= 0:
        result.x0 -= 0.5
        result.x1 += 0.5
    if result.height <= 0:
        result.y0 -= 0.5
        result.y1 += 0.5
    return result


def _drawing_geometries(
    drawing: dict[str, object], drawing_index: int
) -> list[ProtectedGeometry]:
    geometries: list[ProtectedGeometry] = []
    stroke_width = max(float(drawing.get("width") or 0.0), 0.1)
    half = stroke_width / 2.0
    if drawing.get("fill") is not None:
        fill_rect = pymupdf.Rect(drawing["rect"])
        if _nonempty(fill_rect):
            geometries.append(
                ProtectedGeometry(
                    "drawing", _tuple(fill_rect), f"drawing-{drawing_index}-fill", True
                )
            )
    for item_index, item in enumerate(drawing.get("items", ())):
        operator = item[0]
        rects: list[pymupdf.Rect] = []
        if operator == "l":
            rects.append(_points_rect(item[1:3], half))
        elif operator == "re":
            rectangle = pymupdf.Rect(item[1])
            rects.extend(
                (
                    pymupdf.Rect(rectangle.x0 - half, rectangle.y0 - half, rectangle.x1 + half, rectangle.y0 + half),
                    pymupdf.Rect(rectangle.x0 - half, rectangle.y1 - half, rectangle.x1 + half, rectangle.y1 + half),
                    pymupdf.Rect(rectangle.x0 - half, rectangle.y0, rectangle.x0 + half, rectangle.y1),
                    pymupdf.Rect(rectangle.x1 - half, rectangle.y0, rectangle.x1 + half, rectangle.y1),
                )
            )
        elif operator == "c":
            curve = [value for value in item[1:] if isinstance(value, pymupdf.Point)]
            if len(curve) == 4:
                sampled = [_cubic_point(*curve, step / 16.0) for step in range(17)]
                rects.extend(
                    _points_rect((start, end), half)
                    for start, end in zip(sampled, sampled[1:])
                )
        elif operator == "qu" and isinstance(item[1], pymupdf.Quad):
            quad = item[1]
            rects.extend(
                _points_rect(edge, half)
                for edge in (
                    (quad.ul, quad.ur),
                    (quad.ur, quad.lr),
                    (quad.lr, quad.ll),
                    (quad.ll, quad.ul),
                )
            )
        for part_index, rect in enumerate(rects):
            if _nonempty(rect):
                geometries.append(
                    ProtectedGeometry(
                        "drawing",
                        _tuple(rect),
                        f"drawing-{drawing_index}-{item_index}-{part_index}",
                        True,
                    )
                )
    return geometries


def _points_rect(points: Sequence[pymupdf.Point], padding: float) -> pymupdf.Rect:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return pymupdf.Rect(
        min(xs) - padding,
        min(ys) - padding,
        max(xs) + padding,
        max(ys) + padding,
    )


def _cubic_point(
    p0: pymupdf.Point,
    p1: pymupdf.Point,
    p2: pymupdf.Point,
    p3: pymupdf.Point,
    t: float,
) -> pymupdf.Point:
    inverse = 1.0 - t
    return pymupdf.Point(
        inverse**3 * p0.x
        + 3 * inverse**2 * t * p1.x
        + 3 * inverse * t**2 * p2.x
        + t**3 * p3.x,
        inverse**3 * p0.y
        + 3 * inverse**2 * t * p1.y
        + 3 * inverse * t**2 * p2.y
        + t**3 * p3.y,
    )


def _polyline_hits_rect(points: Sequence[PointTuple], rect: pymupdf.Rect) -> bool:
    if len(points) < 2:
        return False
    for start, end in zip(points, points[1:]):
        if rect.contains(pymupdf.Point(start)) or rect.contains(pymupdf.Point(end)):
            return True
        if _segment_intersects_rect(start, end, rect):
            return True
    return False


def _segment_intersects_rect(start: PointTuple, end: PointTuple, rect: pymupdf.Rect) -> bool:
    edges = (
        ((rect.x0, rect.y0), (rect.x1, rect.y0)),
        ((rect.x1, rect.y0), (rect.x1, rect.y1)),
        ((rect.x1, rect.y1), (rect.x0, rect.y1)),
        ((rect.x0, rect.y1), (rect.x0, rect.y0)),
    )
    return any(_segments_intersect(start, end, edge_start, edge_end) for edge_start, edge_end in edges)


def _segments_intersect(a: PointTuple, b: PointTuple, c: PointTuple, d: PointTuple) -> bool:
    def orientation(p: PointTuple, q: PointTuple, r: PointTuple) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p: PointTuple, q: PointTuple, r: PointTuple) -> bool:
        return (
            min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
        )

    o1, o2 = orientation(a, b, c), orientation(a, b, d)
    o3, o4 = orientation(c, d, a), orientation(c, d, b)
    if (o1 > 0 > o2 or o2 > 0 > o1) and (o3 > 0 > o4 or o4 > 0 > o3):
        return True
    return (
        (abs(o1) <= 1e-9 and on_segment(a, c, b))
        or (abs(o2) <= 1e-9 and on_segment(a, d, b))
        or (abs(o3) <= 1e-9 and on_segment(c, a, d))
        or (abs(o4) <= 1e-9 and on_segment(c, b, d))
    )


def _expand(rect: pymupdf.Rect, amount: float) -> pymupdf.Rect:
    return pymupdf.Rect(rect.x0 - amount, rect.y0 - amount, rect.x1 + amount, rect.y1 + amount)


def _overlaps(first: pymupdf.Rect, second: pymupdf.Rect) -> bool:
    intersection = first & second
    return intersection.width > 1e-6 and intersection.height > 1e-6


def _rect_distance(first: pymupdf.Rect, second: pymupdf.Rect) -> float:
    dx = max(first.x0 - second.x1, second.x0 - first.x1, 0.0)
    dy = max(first.y0 - second.y1, second.y0 - first.y1, 0.0)
    return math.hypot(dx, dy)


def _center_distance(first: pymupdf.Rect, second: pymupdf.Rect) -> float:
    return math.hypot(
        (first.x0 + first.x1 - second.x0 - second.x1) / 2.0,
        (first.y0 + first.y1 - second.y0 - second.y1) / 2.0,
    )


def _rect(value: pymupdf.Rect | Sequence[float]) -> pymupdf.Rect:
    return pymupdf.Rect(value)


def _tuple(rect: pymupdf.Rect) -> RectTuple:
    return tuple(float(value) for value in rect)


def _nonempty(rect: pymupdf.Rect) -> bool:
    return (
        all(math.isfinite(value) for value in rect)
        and rect.width > 0
        and rect.height > 0
    )


__all__ = [
    "Collision",
    "FONT_SIZES",
    "LayoutResult",
    "Placement",
    "ProtectedGeometry",
    "TEXT_COLOR",
    "detect_collisions",
    "detect_candidate_collisions",
    "detect_rendered_collisions",
    "extract_protected_geometry",
    "find_same_row_blank_cells",
    "infer_semantic_region",
    "optimize_layout",
    "rank_placements",
    "wrap_text",
]
