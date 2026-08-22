"""Safely apply a validated review as editable PDF FreeText annotations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymupdf

from .layout import (
    TEXT_COLOR,
    TOOL_CJK_FONT,
    Collision,
    LayoutResult,
    Placement,
    ProtectedGeometry,
    detect_collisions,
    detect_rendered_collisions,
    extract_protected_geometry,
    find_same_row_blank_cells,
    optimize_layout,
    rank_placements,
)
from .models import ReviewDocument, ReviewItem, ReviewStatus


TOOL_VERSION = "1.0"


@dataclass(frozen=True)
class ApplyResult:
    success: bool
    output_path: Path | None
    problems: tuple[dict[str, Any], ...] = ()
    unresolved_overlaps: tuple[dict[str, Any], ...] = ()
    layout_rounds: int = 0
    modified_pages: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output_path": str(self.output_path) if self.output_path else None,
            "problems": [_json_safe(value) for value in self.problems],
            "unresolved_overlaps": [
                _json_safe(value) for value in self.unresolved_overlaps
            ],
            "layout_rounds": self.layout_rounds,
            "modified_pages": list(self.modified_pages),
        }


@dataclass(frozen=True)
class _PageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    text: str
    content_streams: tuple[tuple[int, str], ...]
    images: tuple[tuple[Any, ...], ...]
    drawings: tuple[tuple[float, float, float, float], ...]
    annotations: tuple[tuple[Any, ...], ...]


def apply_review(
    source_pdf: str | Path,
    review: ReviewDocument,
) -> ApplyResult:
    """Apply approved review items and atomically publish ``<name>.annotated.pdf``."""
    source = Path(source_pdf)
    final_path = source.with_name(source.name + ".annotated.pdf")
    temp_path: Path | None = None
    try:
        validation_problem = _validate_inputs(source, review, final_path)
        if validation_problem is not None:
            return _failure(validation_problem)

        approved = tuple(
            item
            for item in review.items
            if item.review_status in {
                ReviewStatus.APPROVED,
                ReviewStatus.APPROVED_EDITED,
            }
        )
        if not approved:
            return _failure(
                _problem("nothing_to_apply", "Review has no approved annotations")
            )

        source_document = pymupdf.open(source)
        try:
            baseline = _snapshot_document(source_document)
            layout, attempted = _layout_document(source_document, approved)
            if layout.collisions:
                unresolved = _unresolved(layout, attempted)
                return ApplyResult(
                    success=False,
                    output_path=None,
                    problems=(
                        _problem(
                            "unresolved_overlap",
                            "One or more annotations could not be placed safely",
                        ),
                    ),
                    unresolved_overlaps=unresolved,
                    layout_rounds=layout.rounds,
                    modified_pages=tuple(
                        sorted({placement.page_index for placement in layout.placements})
                    ),
                )
            placements = layout.placements
        finally:
            source_document.close()

        temp_path = _unique_temp_path(source)
        shutil.copy2(source, temp_path)
        output_document = pymupdf.open(temp_path)
        try:
            _write_annotations(output_document, placements)
            output_document.saveIncr()
        finally:
            output_document.close()

        modified_pages = tuple(sorted({placement.page_index for placement in placements}))
        verification_problem = _verify_temp(
            source,
            temp_path,
            baseline,
            placements,
            modified_pages,
        )
        if verification_problem is not None:
            _remove_exact_temp(temp_path)
            temp_path = None
            return _failure(
                verification_problem,
                layout_rounds=layout.rounds,
                modified_pages=modified_pages,
            )

        if final_path.exists():
            _remove_exact_temp(temp_path)
            temp_path = None
            return _failure(_problem("output_exists", "Final output already exists"))
        temp_path.replace(final_path)
        temp_path = None
        return ApplyResult(
            success=True,
            output_path=final_path,
            layout_rounds=layout.rounds,
            modified_pages=modified_pages,
        )
    except Exception:
        if temp_path is not None:
            _remove_exact_temp(temp_path)
        return _failure(
            _problem("apply_failed", "PDF annotations could not be applied safely")
        )


def _validate_inputs(
    source: Path, review: ReviewDocument, final_path: Path
) -> dict[str, Any] | None:
    if not isinstance(review, ReviewDocument):
        return _problem("review_not_validated", "A validated review document is required")
    if not source.is_file():
        return _problem("source_unavailable", "Source PDF is unavailable")
    if final_path.exists():
        return _problem("output_exists", "Final output already exists")
    if review.blocking_issues or review.review_completed_at is None:
        return _problem("review_blocked", "Review is incomplete or blocked")
    if review.source.filename != source.name or review.source.sha256 != _sha256(source):
        return _problem("source_mismatch", "Source PDF no longer matches the review")
    try:
        document = pymupdf.open(source)
        try:
            if document.needs_pass or document.page_count != review.source.page_count:
                return _problem("source_mismatch", "Source PDF no longer matches the review")
            if any(item.page_index >= document.page_count for item in review.items):
                return _problem("review_page_invalid", "Review item page is invalid")
        finally:
            document.close()
    except Exception:
        return _problem("source_unavailable", "Source PDF cannot be inspected")
    return None


def _layout_document(
    document: pymupdf.Document,
    items: Sequence[ReviewItem],
) -> tuple[LayoutResult, dict[str, list[Placement]]]:
    candidates_by_id: dict[str, list[Placement]] = {}
    initial: list[Placement] = []
    by_page: dict[int, list[ReviewItem]] = defaultdict(list)
    for item in items:
        by_page[item.page_index].append(item)

    for page_index in sorted(by_page):
        page = document[page_index]
        protected = extract_protected_geometry(page)
        for item in sorted(by_page[page_index], key=lambda value: value.item_id):
            candidates = rank_placements(
                item,
                page.rect,
                protected=protected,
                same_row_cells=find_same_row_blank_cells(page, item.source_bbox),
                semantic_region=page.rect,
            )
            candidates_by_id[item.item_id] = candidates
            if candidates:
                initial.append(candidates[0])

    def collision_detector(placements: Sequence[Placement]) -> Sequence[Collision]:
        collisions: list[Collision] = []
        grouped: dict[int, list[Placement]] = defaultdict(list)
        for placement in placements:
            grouped[placement.page_index].append(placement)
        for page_index, page_placements in grouped.items():
            collisions.extend(
                detect_collisions(
                    document[page_index], page_placements, render_check=False
                )
            )
        return collisions

    result = optimize_layout(
        initial,
        collision_detector=collision_detector,
        candidate_provider=lambda placement, _collisions: candidates_by_id[
            placement.item_id
        ],
    )
    if not result.collisions:
        rendered_collisions: list[Collision] = []
        grouped: dict[int, list[Placement]] = defaultdict(list)
        for placement in result.placements:
            grouped[placement.page_index].append(placement)
        for page_index, page_placements in grouped.items():
            rendered_collisions.extend(
                detect_collisions(document[page_index], page_placements, render_check=True)
            )
        if rendered_collisions:
            result = LayoutResult(
                result.placements,
                tuple(rendered_collisions),
                result.rounds,
                result.stable,
            )
    return result, candidates_by_id


def _write_annotations(
    document: pymupdf.Document, placements: Sequence[Placement]
) -> None:
    for placement in placements:
        page = document[placement.page_index]
        annotation = page.add_freetext_annot(
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
        metadata = {
            "item_id": placement.item_id,
            "source_page": placement.page_index + 1,
            "tool_version": TOOL_VERSION,
        }
        annotation.set_info(
            title="techpack_pdf",
            subject=json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
        )
        annotation.update()


def _verify_temp(
    source: Path,
    temp: Path,
    baseline: tuple[_PageSnapshot, ...],
    placements: Sequence[Placement],
    modified_pages: Sequence[int],
) -> dict[str, Any] | None:
    original = pymupdf.open(source)
    output = pymupdf.open(temp)
    try:
        if output.needs_pass or output.page_count != original.page_count:
            return _problem("verification_failed", "Output page structure changed")
        current = _snapshot_document(output, original_annotation_xrefs=True)
        if len(current) != len(baseline):
            return _problem("verification_failed", "Output page structure changed")
        expected_new = defaultdict(int)
        for placement in placements:
            expected_new[placement.page_index] += 1
        for page_index, (before, after) in enumerate(zip(baseline, current)):
            if (
                before.media_box != after.media_box
                or before.crop_box != after.crop_box
                or before.rotation != after.rotation
                or not all(
                    line in after.text for line in before.text.splitlines() if line
                )
                or before.content_streams != after.content_streams
                or before.images != after.images
                or before.drawings != after.drawings
                or before.annotations != after.annotations
            ):
                return _problem(
                    "verification_failed",
                    "Original PDF content or annotations changed",
                    page_index=page_index,
                )
            free_text_count = sum(
                annotation.type[1] == "FreeText"
                and annotation.info.get("title") == "techpack_pdf"
                for annotation in output[page_index].annots()
            )
            if free_text_count != expected_new[page_index]:
                return _problem(
                    "verification_failed",
                    "Output annotation count is invalid",
                    page_index=page_index,
                )
        grouped: dict[int, list[Placement]] = defaultdict(list)
        for placement in placements:
            grouped[placement.page_index].append(placement)
        for page_index in modified_pages:
            pixmap = output[page_index].get_pixmap(
                dpi=300, alpha=False, colorspace=pymupdf.csRGB, annots=True
            )
            if pixmap.width <= 0 or pixmap.height <= 0:
                return _problem(
                    "verification_failed",
                    "Output page could not be rendered",
                    page_index=page_index,
                )
            final_collisions = detect_rendered_collisions(
                original[page_index], output[page_index], grouped[page_index]
            )
            if final_collisions:
                return _problem(
                    "verification_failed",
                    "Output render did not pass collision checks",
                    page_index=page_index,
                )
        return None
    finally:
        output.close()
        original.close()


def _snapshot_document(
    document: pymupdf.Document,
    *,
    original_annotation_xrefs: bool = False,
) -> tuple[_PageSnapshot, ...]:
    snapshots: list[_PageSnapshot] = []
    for page in document:
        annotations = []
        for annotation in page.annots():
            if (
                original_annotation_xrefs
                and annotation.type[1] == "FreeText"
                and annotation.info.get("title") == "techpack_pdf"
            ):
                continue
            annotations.append(
                (
                    annotation.xref,
                    annotation.type,
                    tuple(float(value) for value in annotation.rect),
                    tuple(sorted(annotation.info.items())),
                )
            )
        snapshots.append(
            _PageSnapshot(
                media_box=tuple(float(value) for value in page.mediabox),
                crop_box=tuple(float(value) for value in page.cropbox),
                rotation=page.rotation,
                text=page.get_text(),
                content_streams=tuple(
                    (
                        xref,
                        hashlib.sha256(document.xref_stream(xref)).hexdigest(),
                    )
                    for xref in page.get_contents()
                ),
                images=tuple(
                    (
                        image[0],
                        tuple(
                            tuple(float(value) for value in rect)
                            for rect in page.get_image_rects(image[0])
                        ),
                    )
                    for image in page.get_images(full=True)
                ),
                drawings=tuple(
                    tuple(float(value) for value in drawing["rect"])
                    for drawing in page.get_drawings()
                ),
                annotations=tuple(annotations),
            )
        )
    return tuple(snapshots)


def _unresolved(
    layout: LayoutResult,
    attempted: Mapping[str, Sequence[Placement]],
) -> tuple[dict[str, Any], ...]:
    issues: list[dict[str, Any]] = []
    by_item: dict[tuple[int, str], list[Collision]] = defaultdict(list)
    for collision in layout.collisions:
        by_item[(collision.page_index, collision.item_id)].append(collision)
    for (page_index, item_id), collisions in sorted(by_item.items()):
        attempts = attempted.get(item_id, ())
        issues.append(
            {
                "code": "unresolved_overlap",
                "page_index": page_index,
                "item_id": item_id,
                "collision_object": [
                    {"kind": value.kind, "object_id": value.object_id}
                    for value in collisions
                ],
                "attempted_placements": [
                    {
                        "strategy": value.strategy,
                        "rect": list(value.rect),
                        "font_size": value.font_size,
                        "leader": value.leader_line is not None,
                    }
                    for value in attempts
                ],
                "final_render_reference": f"page-{page_index + 1}@300dpi",
            }
        )
    return tuple(issues)


def _unique_temp_path(source: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{source.name}.", suffix=".tmp.pdf", dir=source.parent
    )
    os.close(descriptor)
    return Path(value)


def _remove_exact_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _problem(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "details": {key: _json_safe(value) for key, value in details.items()},
    }


def _failure(
    problem: dict[str, Any],
    *,
    layout_rounds: int = 0,
    modified_pages: Sequence[int] = (),
) -> ApplyResult:
    return ApplyResult(
        success=False,
        output_path=None,
        problems=(problem,),
        layout_rounds=layout_rounds,
        modified_pages=tuple(modified_pages),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return _json_safe(value.value)
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return str(value)


__all__ = ["ApplyResult", "TOOL_VERSION", "apply_review"]
