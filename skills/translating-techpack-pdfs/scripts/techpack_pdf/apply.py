"""Safely apply a validated review as editable PDF FreeText annotations."""

from __future__ import annotations

import hashlib
import gc
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
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
    detect_rendered_collisions,
    placement_signature as _placement_signature,
    plan_document_layout as _layout_document,
)
from .errors import TechpackError
from .models import (
    JobManifest,
    ReviewDocument,
    ReviewItem,
    ReviewStatus,
)
from .review import load_review


TOOL_VERSION = "1.0"


@dataclass(frozen=True)
class ApplyResult:
    success: bool
    output_path: Path | None
    problems: tuple[dict[str, Any], ...] = ()
    unresolved_overlaps: tuple[dict[str, Any], ...] = ()
    layout_rounds: int = 0
    modified_pages: tuple[int, ...] = ()
    failure_report_path: Path | None = None

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
            "failure_report_path": (
                str(self.failure_report_path) if self.failure_report_path else None
            ),
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


@dataclass(frozen=True)
class _VerificationOutcome:
    problem: dict[str, Any] | None
    collisions: tuple[Collision, ...] = ()


@dataclass(frozen=True)
class _WrittenAnnotation:
    page_index: int
    item_id: str
    xref: int
    placement_signature: tuple[object, ...]
    text: str
    rect: tuple[float, float, float, float]
    font_size: float
    leader_line: tuple[tuple[float, float], ...] | None


@dataclass(frozen=True)
class _ArtifactOutcome:
    unresolved: tuple[dict[str, Any], ...] = ()
    report_path: Path | None = None
    problem: dict[str, Any] | None = None


@dataclass(frozen=True)
class _OwnedPath:
    path: Path
    identity: tuple[int, int, int, int]
    sha256: str


class _ArtifactWriteError(RuntimeError):
    def __init__(
        self,
        retained: Sequence[Path] = (),
        ownership_mismatches: Sequence[Path] = (),
    ) -> None:
        super().__init__("artifact write failed")
        self.retained = tuple(retained)
        self.ownership_mismatches = tuple(ownership_mismatches)


def apply_review(
    source_pdf: str | Path,
    review_path: str | Path,
    job: JobManifest,
    expected_output: Any,
) -> ApplyResult:
    """Apply approved review items and atomically publish ``<name>.annotated.pdf``."""
    source = Path(source_pdf)
    final_path = source.with_name(source.name + ".annotated.pdf")
    temp_path: Path | None = None
    snapshot_path: Path | None = None
    try:
        try:
            manifest = JobManifest.model_validate(job)
            if manifest.source.path is None:
                return _failure(
                    _problem("source_job_mismatch", "Source path is not bound to the job")
                )
            if source.resolve(strict=False) != Path(manifest.source.path).resolve(
                strict=False
            ):
                return _failure(
                    _problem("source_job_mismatch", "Source path does not match the job")
                )
            review = load_review(review_path, manifest, expected_output)
        except (TechpackError, TypeError, ValueError, OSError):
            return _failure(
                _problem(
                    "review_validation_failed",
                    "Review could not be validated against the trusted job",
                )
            )
        validation_problem = _validate_inputs(source, review, final_path)
        if validation_problem is not None:
            return _failure(validation_problem)

        source_identity = _source_identity(source)
        snapshot_path = _unique_temp_path(source)
        shutil.copyfile(source, snapshot_path)
        if _sha256(snapshot_path) != review.source.sha256:
            cleanup = _cleanup_problem(snapshot_path, source)
            snapshot_path = None if cleanup is None else snapshot_path
            return _failure(_combine_problems(_problem("source_mismatch", "Source PDF changed during validation"), cleanup))

        approved = tuple(
            item
            for item in review.items
            if item.review_status in {
                ReviewStatus.APPROVED,
                ReviewStatus.APPROVED_EDITED,
            }
        )
        identity_problem = _approved_identity_problem(approved)
        if identity_problem is not None:
            cleanup = _cleanup_problem(snapshot_path, source)
            snapshot_path = None if cleanup is None else snapshot_path
            return _failure(_combine_problems(identity_problem, cleanup))

        source_document = pymupdf.open(snapshot_path)
        try:
            baseline = _snapshot_document(source_document)
            layout, attempted = _layout_document(source_document, approved)
            if layout.collisions:
                artifact = _write_unresolved_artifacts(
                    source, layout, attempted, document=source_document
                )
                source_document.close()
                cleanup = _cleanup_problem(snapshot_path, source)
                snapshot_path = None if cleanup is None else snapshot_path
                if artifact.problem is not None or cleanup is not None:
                    return _failure(
                        _combine_problems(artifact.problem, cleanup),
                        unresolved_overlaps=artifact.unresolved,
                        failure_report_path=artifact.report_path,
                    )
                return ApplyResult(
                    success=False,
                    output_path=None,
                    problems=(
                        _problem(
                            "candidate_exhausted" if any(value.kind == "candidate_exhausted" for value in layout.collisions) else "unresolved_overlap",
                            "One or more annotations could not be placed safely",
                        ),
                    ),
                    unresolved_overlaps=artifact.unresolved,
                    layout_rounds=layout.rounds,
                    modified_pages=tuple(
                        sorted({placement.page_index for placement in layout.placements})
                    ),
                    failure_report_path=artifact.report_path,
                )
            placements = layout.placements
            total_rounds = layout.rounds
        finally:
            if not source_document.is_closed:
                source_document.close()
        identity_problem = _placement_identity_problem(approved, placements)
        if identity_problem is not None:
            cleanup = _cleanup_problem(snapshot_path, source)
            snapshot_path = None if cleanup is None else snapshot_path
            return _failure(_combine_problems(identity_problem, cleanup))
        excluded: set[tuple[object, ...]] = set()
        while True:
            temp_path = _unique_temp_path(source)
            shutil.copyfile(snapshot_path, temp_path)
            output_document = pymupdf.open(temp_path)
            try:
                written = _write_annotations(output_document, placements)
                output_document.saveIncr()
            finally:
                output_document.close()

            modified_pages = tuple(
                sorted({placement.page_index for placement in placements})
            )
            verification = _verify_temp(
                snapshot_path,
                temp_path,
                baseline,
                placements,
                modified_pages,
                written=written,
            )
            if verification.collisions:
                total_rounds += 1
                if total_rounds >= 10:
                    unresolved_layout = LayoutResult(
                        tuple(placements), verification.collisions, 10, False
                    )
                    artifact = _write_unresolved_artifacts(
                        source, unresolved_layout, attempted, annotated_temp=temp_path
                    )
                    cleanup = _cleanup_many((temp_path, snapshot_path), source)
                    temp_path = None if cleanup is None else temp_path
                    snapshot_path = None if cleanup is None else snapshot_path
                    if artifact.problem is not None or cleanup is not None:
                        return _failure(
                            _combine_problems(artifact.problem, cleanup),
                            layout_rounds=10,
                            modified_pages=modified_pages,
                            unresolved_overlaps=artifact.unresolved,
                            failure_report_path=artifact.report_path,
                        )
                    return ApplyResult(
                        success=False,
                        output_path=None,
                        problems=(_problem("unresolved_overlap", "One or more annotations could not be placed safely"),),
                        unresolved_overlaps=artifact.unresolved,
                        layout_rounds=10,
                        modified_pages=modified_pages,
                        failure_report_path=artifact.report_path,
                    )
                cleanup = _cleanup_problem(temp_path, source)
                if cleanup is not None:
                    cleanup = _cleanup_many((temp_path, snapshot_path), source)
                    return _failure(
                        _combine_problems(
                            _render_collision_problem(verification.collisions),
                            cleanup,
                        ),
                        layout_rounds=total_rounds,
                        modified_pages=modified_pages,
                    )
                temp_path = None
                collided_ids = {
                    collision.item_id for collision in verification.collisions
                }
                excluded.update(
                    _placement_signature(placement)
                    for placement in placements
                    if placement.item_id in collided_ids
                )
                source_document = pymupdf.open(snapshot_path)
                try:
                    layout, retry_attempted = _layout_document(
                        source_document,
                        approved,
                        excluded=excluded,
                        max_rounds=10 - total_rounds,
                    )
                    attempted = _merge_attempted(attempted, retry_attempted)
                    total_rounds += layout.rounds
                    if layout.collisions:
                        unresolved_layout = LayoutResult(
                            layout.placements,
                            layout.collisions,
                            min(total_rounds, 10),
                            layout.stable,
                        )
                        artifact = _write_unresolved_artifacts(
                            source, unresolved_layout, attempted, document=source_document
                        )
                        source_document.close()
                        cleanup = _cleanup_problem(snapshot_path, source)
                        snapshot_path = None if cleanup is None else snapshot_path
                        if artifact.problem is not None or cleanup is not None:
                            return _failure(
                                _combine_problems(artifact.problem, cleanup),
                                layout_rounds=min(total_rounds, 10),
                                modified_pages=modified_pages,
                                unresolved_overlaps=artifact.unresolved,
                                failure_report_path=artifact.report_path,
                            )
                        return ApplyResult(
                            success=False,
                            output_path=None,
                            problems=(
                                _problem(
                                    "candidate_exhausted" if any(value.kind == "candidate_exhausted" for value in layout.collisions) else "unresolved_overlap",
                                    "One or more annotations could not be placed safely",
                                ),
                            ),
                            unresolved_overlaps=artifact.unresolved,
                            layout_rounds=min(total_rounds, 10),
                            modified_pages=modified_pages,
                            failure_report_path=artifact.report_path,
                        )
                    placements = layout.placements
                finally:
                    if not source_document.is_closed:
                        source_document.close()
                identity_problem = _placement_identity_problem(approved, placements)
                if identity_problem is not None:
                    cleanup = _cleanup_problem(snapshot_path, source)
                    snapshot_path = None if cleanup is None else snapshot_path
                    return _failure(_combine_problems(identity_problem, cleanup))
                continue
            if verification.problem is not None:
                cleanup = _cleanup_many((temp_path, snapshot_path), source)
                if cleanup is not None:
                    return _failure(
                        _combine_problems(verification.problem, cleanup),
                        layout_rounds=total_rounds,
                        modified_pages=modified_pages,
                    )
                temp_path = None
                snapshot_path = None
                return _failure(
                    verification.problem,
                    layout_rounds=total_rounds,
                    modified_pages=modified_pages,
                )
            break

        if not _stable_source_matches(source, source_identity, review.source.sha256):
            cleanup = _cleanup_many((temp_path, snapshot_path), source)
            temp_path = None if cleanup is None else temp_path
            snapshot_path = None if cleanup is None else snapshot_path
            return _failure(_combine_problems(_problem("source_mismatch", "Source PDF changed before publication"), cleanup))
        cleanup = _cleanup_problem(snapshot_path, source)
        if cleanup is not None:
            output_cleanup = _cleanup_problem(temp_path, source)
            return _failure(_combine_problems(cleanup, output_cleanup))
        snapshot_path = None
        publish_problem = _publish_no_clobber(
            temp_path,
            final_path,
            source,
            expected_source_identity=source_identity,
            expected_source_sha256=review.source.sha256,
        )
        if publish_problem is not None:
            if publish_problem["code"] == "temp_cleanup_failed":
                return _failure(publish_problem)
            temp_path = None
            return _failure(publish_problem)
        temp_path = None
        return ApplyResult(
            success=True,
            output_path=final_path,
            layout_rounds=total_rounds,
            modified_pages=modified_pages,
        )
    except Exception:
        cleanup = _cleanup_many((temp_path, snapshot_path), source)
        return _failure(
            _combine_problems(
                _problem("apply_failed", "PDF annotations could not be applied safely"),
                cleanup,
            )
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
    if any(item.review_status is None for item in review.items):
        return _problem("review_blocked", "Every review item must have a final status")
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


def _write_annotations(
    document: pymupdf.Document, placements: Sequence[Placement]
) -> tuple[_WrittenAnnotation, ...]:
    written: list[_WrittenAnnotation] = []
    for placement in placements:
        page = document[placement.page_index]
        annotation_options: dict[str, Any] = {
            "fontsize": placement.font_size,
            "fontname": TOOL_CJK_FONT,
            "text_color": TEXT_COLOR,
            "fill_color": None,
            "border_color": None,
            "border_width": 0,
        }
        if placement.leader_line is not None:
            annotation_options["callout"] = placement.leader_line
        annotation = page.add_freetext_annot(
            placement.rect, placement.text, **annotation_options
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
        _bind_da_to_cjk_appearance(document, annotation, placement.font_size)
        written.append(
            _WrittenAnnotation(
                placement.page_index,
                placement.item_id,
                annotation.xref,
                _placement_signature(placement),
                placement.text,
                placement.rect,
                placement.font_size,
                placement.leader_line,
            )
        )
    return tuple(written)


def _approved_identity_problem(
    approved: Sequence[ReviewItem],
) -> dict[str, Any] | None:
    counts = Counter(item.item_id for item in approved)
    if any(count != 1 for count in counts.values()):
        return _problem(
            "review_validation_failed", "Approved review item identifiers are not unique"
        )
    return None


def _placement_identity_problem(
    approved: Sequence[ReviewItem], placements: Sequence[Placement]
) -> dict[str, Any] | None:
    expected = Counter(item.item_id for item in approved)
    actual = Counter(placement.item_id for placement in placements)
    if expected != actual or any(count != 1 for count in actual.values()):
        return _problem(
            "placement_identity_mismatch",
            "Approved items do not map one-to-one to placements",
        )
    return None


def _merge_attempted(
    first: Mapping[str, Sequence[Placement]],
    second: Mapping[str, Sequence[Placement]],
) -> dict[str, list[Placement]]:
    merged: dict[str, list[Placement]] = {}
    for item_id in dict.fromkeys((*first.keys(), *second.keys())):
        seen: set[tuple[object, ...]] = set()
        values: list[Placement] = []
        for placement in (*first.get(item_id, ()), *second.get(item_id, ())):
            signature = _placement_signature(placement)
            if signature not in seen:
                seen.add(signature)
                values.append(placement)
        merged[item_id] = values
    return merged


def _render_collision_problem(
    collisions: Sequence[Collision],
) -> dict[str, Any]:
    return _problem(
        "render_collision_retry_aborted",
        "Rendered annotation collisions could not be retried safely",
        collisions=[
            {
                "page_index": value.page_index,
                "item_id": value.item_id,
                "kind": value.kind,
                "object_id": value.object_id,
                "intersecting_pixels": value.intersecting_pixels,
                "render_dpi": value.render_dpi,
            }
            for value in collisions
        ],
    )


def _verify_temp(
    source: Path,
    temp: Path,
    baseline: tuple[_PageSnapshot, ...],
    placements: Sequence[Placement],
    modified_pages: Sequence[int],
    *,
    written: Sequence[_WrittenAnnotation] | None = None,
) -> _VerificationOutcome:
    original = pymupdf.open(source)
    output = pymupdf.open(temp)
    try:
        if output.needs_pass or output.page_count != original.page_count:
            return _VerificationOutcome(
                _problem("verification_failed", "Output page structure changed")
            )
        written = tuple(written or ())
        approved_ids = Counter(placement.item_id for placement in placements)
        written_ids = Counter(value.item_id for value in written)
        placements_by_id = {placement.item_id: placement for placement in placements}
        if (
            approved_ids != written_ids
            or any(count != 1 for count in approved_ids.values())
            or len({(value.page_index, value.xref) for value in written}) != len(written)
            or any(
                value.item_id not in placements_by_id
                or value.placement_signature
                != _placement_signature(placements_by_id[value.item_id])
                for value in written
            )
        ):
            return _VerificationOutcome(
                _problem("verification_failed", "Approved item annotation mapping is invalid")
            )
        excluded = {(value.page_index, value.xref) for value in written}
        current = _snapshot_document(output, excluded_annotation_xrefs=excluded)
        if len(current) != len(baseline):
            return _VerificationOutcome(
                _problem("verification_failed", "Output page structure changed")
            )
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
                or not all(drawing in after.drawings for drawing in before.drawings)
                or before.annotations != after.annotations
            ):
                return _VerificationOutcome(
                    _problem(
                        "verification_failed",
                        "Original PDF content or annotations changed",
                        page_index=page_index,
                    )
                )
        actual_ids: Counter[str] = Counter()
        for record in written:
            if record.page_index < 0 or record.page_index >= output.page_count:
                return _VerificationOutcome(_problem("verification_failed", "New annotation page is invalid"))
            try:
                page = output[record.page_index]
                annotation = page.load_annot(record.xref)
                if annotation is None or annotation.type[1] != "FreeText":
                    raise ValueError("missing annotation")
                metadata = json.loads(annotation.info.get("subject") or "{}")
            except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
                return _VerificationOutcome(_problem("verification_failed", "New annotation metadata is invalid"))
            if metadata.get("item_id") != record.item_id:
                return _VerificationOutcome(_problem("verification_failed", "New annotation item binding is invalid"))
            expected_metadata = {
                "item_id": record.item_id,
                "source_page": record.page_index + 1,
                "tool_version": TOOL_VERSION,
            }
            if metadata != expected_metadata:
                return _VerificationOutcome(_problem("verification_failed", "New annotation metadata is incomplete"))
            if (
                annotation.info.get("content") != record.text
                or annotation.info.get("title") != "techpack_pdf"
                or not _rect_close(
                    _annotation_body_rect(output, annotation),
                    record.rect,
                    tolerance=0.05,
                )
                or not _appearance_is_valid(output, annotation, record.font_size)
                or float(annotation.border.get("width") or 0.0) > 0.01
                or bool(annotation.colors.get("fill"))
                or not _leader_is_valid(output, annotation, record.leader_line)
            ):
                return _VerificationOutcome(
                    _problem("verification_failed", "New annotation appearance or geometry is invalid")
                )
            actual_ids[str(metadata["item_id"])] += 1
        if actual_ids != approved_ids:
            return _VerificationOutcome(_problem("verification_failed", "Approved item annotation set is incomplete"))
        grouped: dict[int, list[Placement]] = defaultdict(list)
        for placement in placements:
            grouped[placement.page_index].append(placement)
        for page_index in modified_pages:
            pixmap = output[page_index].get_pixmap(
                dpi=300, alpha=False, colorspace=pymupdf.csRGB, annots=True
            )
            if pixmap.width <= 0 or pixmap.height <= 0:
                return _VerificationOutcome(
                    _problem(
                        "verification_failed",
                        "Output page could not be rendered",
                        page_index=page_index,
                    )
                )
            final_collisions = detect_rendered_collisions(
                original[page_index], output[page_index], grouped[page_index]
            )
            if final_collisions:
                if any(value.kind == "render_missing" for value in final_collisions):
                    return _VerificationOutcome(
                        _problem(
                            "verification_failed",
                            "New annotation glyph appearance is missing",
                            page_index=page_index,
                        )
                    )
                return _VerificationOutcome(
                    None, tuple(final_collisions)
                )
        return _VerificationOutcome(None)
    finally:
        output.close()
        original.close()


def _snapshot_document(
    document: pymupdf.Document,
    *,
    excluded_annotation_xrefs: set[tuple[int, int]] | None = None,
) -> tuple[_PageSnapshot, ...]:
    snapshots: list[_PageSnapshot] = []
    for page in document:
        annotations = []
        for annotation in page.annots():
            if excluded_annotation_xrefs and (page.number, annotation.xref) in excluded_annotation_xrefs:
                continue
            annotation_pixmap = annotation.get_pixmap(alpha=True)
            annotations.append(
                (
                    annotation.xref,
                    annotation.type,
                    tuple(float(value) for value in annotation.rect),
                    tuple(sorted(annotation.info.items())),
                    hashlib.sha256(
                        document.xref_object(
                            annotation.xref, compressed=False
                        ).encode("utf-8")
                    ).hexdigest(),
                    hashlib.sha256(annotation_pixmap.samples).hexdigest(),
                    tuple(sorted(annotation.colors.items())),
                    tuple(sorted(annotation.border.items())),
                    annotation.opacity,
                    annotation.flags,
                    tuple(annotation.vertices or ()),
                    _xref_dependency_hashes(
                        document, _annotation_appearance_xrefs(document, annotation)
                    ),
                )
            )
        seen_xrefs = {value[0] for value in annotations}
        try:
            widgets = tuple(page.widgets() or ())
        except (AttributeError, RuntimeError, ValueError):
            widgets = ()
        for widget in widgets:
            if widget.xref in seen_xrefs:
                continue
            seen_xrefs.add(widget.xref)
            annotations.append(
                _interactive_snapshot(
                    document, "widget", widget.xref, widget.rect
                )
            )
        try:
            links = tuple(page.get_links() or ())
        except (AttributeError, RuntimeError, ValueError):
            links = ()
        for link in links:
            xref = int(link.get("xref") or 0)
            if xref <= 0 or xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            annotations.append(
                _interactive_snapshot(document, "link", xref, link.get("from"))
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
                        image[1],
                        tuple(
                            tuple(float(value) for value in rect)
                            for rect in page.get_image_rects(image[0])
                        ),
                        _xref_dependency_hashes(
                            document,
                            tuple(
                                xref
                                for xref in (image[0], image[1])
                                if isinstance(xref, int) and xref > 0
                            ),
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


def _annotation_appearance_xrefs(
    document: pymupdf.Document, annotation: Any
) -> tuple[int, ...]:
    roots: list[int] = []
    for key in ("AP", "AP/N", "AP/R", "AP/D"):
        key_type, value = document.xref_get_key(annotation.xref, key)
        if key_type == "xref":
            roots.append(int(value.split()[0]))
        elif key_type == "dict":
            roots.extend(_indirect_references(value))
    return tuple(dict.fromkeys(roots))


def _interactive_snapshot(
    document: pymupdf.Document,
    kind: str,
    xref: int,
    rect: pymupdf.Rect | Sequence[float] | None,
) -> tuple[Any, ...]:
    object_text = document.xref_object(xref, compressed=False)
    proxy = type("_XrefProxy", (), {"xref": xref})()
    return (
        xref,
        (kind,),
        tuple(float(value) for value in pymupdf.Rect(rect or ())),
        (),
        hashlib.sha256(object_text.encode("utf-8")).hexdigest(),
        None,
        (),
        (),
        None,
        None,
        (),
        _xref_dependency_hashes(
            document, _annotation_appearance_xrefs(document, proxy)
        ),
    )


def _indirect_references(object_text: str) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(
            int(match.group(1))
            for match in re.finditer(r"(?<!\d)(\d+)\s+\d+\s+R", object_text)
        )
    )


def _xref_dependency_hashes(
    document: pymupdf.Document, roots: Sequence[int]
) -> tuple[tuple[int, str, str | None], ...]:
    pending = list(roots)
    visited: set[int] = set()
    values: list[tuple[int, str, str | None]] = []
    while pending:
        xref = pending.pop()
        if xref <= 0 or xref in visited or xref >= document.xref_length():
            continue
        visited.add(xref)
        object_text = document.xref_object(xref, compressed=False)
        try:
            stream = document.xref_stream(xref)
        except RuntimeError:
            stream = None
        values.append(
            (
                xref,
                hashlib.sha256(object_text.encode("utf-8")).hexdigest(),
                hashlib.sha256(stream).hexdigest() if stream is not None else None,
            )
        )
        pending.extend(
            xref for xref in _indirect_references(object_text) if xref not in visited
        )
    return tuple(sorted(values))


def _rect_close(
    actual: pymupdf.Rect,
    expected: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    return all(
        abs(float(left) - float(right)) <= tolerance
        for left, right in zip(tuple(actual), expected, strict=True)
    )


def _annotation_body_rect(
    document: pymupdf.Document, annotation: pymupdf.Annot
) -> pymupdf.Rect:
    rect = pymupdf.Rect(annotation.rect)
    key_type, raw = document.xref_get_key(annotation.xref, "RD")
    if key_type != "array":
        return rect
    values = [float(value) for value in re.findall(r"[-+]?\d*\.?\d+", raw)]
    if len(values) != 4:
        return pymupdf.Rect()
    left, bottom, right, top = values
    return pymupdf.Rect(
        rect.x0 + left,
        rect.y0 + top,
        rect.x1 - right,
        rect.y1 - bottom,
    )


def _appearance_is_valid(
    document: pymupdf.Document,
    annotation: pymupdf.Annot,
    expected_font_size: float,
) -> bool:
    if annotation.type[1] != "FreeText" or not 5.0 <= expected_font_size <= 24.0:
        return False
    default_appearance = document.xref_get_key(annotation.xref, "DA")[1]
    color_match = re.search(
        r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+rg",
        default_appearance,
    )
    font_match = re.search(r"/([^\s]+)\s+([-+]?\d*\.?\d+)\s+Tf", default_appearance)
    if color_match is None or font_match is None:
        return False
    color = tuple(float(color_match.group(index)) for index in (1, 2, 3))
    appearance_type, appearance_value = document.xref_get_key(annotation.xref, "AP/N")
    if appearance_type != "xref":
        return False
    try:
        appearance_xref = int(appearance_value.split()[0])
        appearance_object = document.xref_object(appearance_xref, compressed=False)
        appearance_stream = document.xref_stream(appearance_xref).decode(
            "latin-1", errors="strict"
        )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    ap_color_match = re.search(
        r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+rg",
        appearance_stream,
    )
    ap_font_matches = re.findall(
        r"/([^\s]+)\s+([-+]?\d*\.?\d+)\s+Tf", appearance_stream
    )
    if ap_color_match is None or not ap_font_matches:
        return False
    ap_color = tuple(float(ap_color_match.group(index)) for index in (1, 2, 3))
    trusted_font = next(
        (
            (resource, size)
            for resource, size in ap_font_matches
            if (xref := _appearance_font_xref(document, appearance_xref, resource))
            is not None
            and _trusted_cjk_font(document, xref)
        ),
        None,
    )
    if trusted_font is None:
        return False
    resource_name, ap_font_size = trusted_font
    return (
        all(abs(actual - expected) <= 0.005 for actual, expected in zip(color, TEXT_COLOR, strict=True))
        and bool(font_match.group(1))
        and font_match.group(1) == resource_name
        and resource_name != "Helv"
        and abs(float(font_match.group(2)) - expected_font_size) <= 0.01
        and all(
            abs(actual - expected) <= 0.01
            for actual, expected in zip(ap_color, TEXT_COLOR, strict=True)
        )
        and abs(float(ap_font_size) - expected_font_size) <= 0.01
    )


def _appearance_font_xref(
    document: pymupdf.Document, appearance_xref: int, resource_name: str
) -> int | None:
    try:
        key_type, value = document.xref_get_key(
            appearance_xref, f"Resources/Font/{resource_name}"
        )
    except (RuntimeError, ValueError):
        return None
    if key_type != "xref":
        return None
    try:
        return int(value.split()[0])
    except (IndexError, ValueError):
        return None


def _trusted_cjk_font(document: pymupdf.Document, font_xref: int) -> bool:
    try:
        font_object = document.xref_object(font_xref, compressed=False)
    except (RuntimeError, ValueError):
        return False
    if "/Subtype /Type0" not in font_object:
        return False
    descendants = re.search(
        r"/DescendantFonts\s*\[\s*(\d+)\s+\d+\s+R", font_object
    )
    encoding = re.search(r"/Encoding\s*/([^\s/<>\[\]()]+)", font_object)
    base_font = re.search(r"/BaseFont\s*/([^\s/<>\[\]()]+)", font_object)
    if descendants is None or encoding is None or base_font is None:
        return False
    try:
        descendant_object = document.xref_object(
            int(descendants.group(1)), compressed=False
        )
    except (RuntimeError, ValueError):
        return False
    if not re.search(r"/Subtype\s*/CIDFontType[02]\b", descendant_object):
        return False
    if not re.search(r"/FontDescriptor\s+\d+\s+\d+\s+R", descendant_object):
        return False
    registry = re.search(r"/Registry\s*\(Adobe\)", descendant_object)
    ordering = re.search(
        r"/Ordering\s*\((GB1|CNS1|Japan1|Korea1|Identity)\)",
        descendant_object,
    )
    if registry is None or ordering is None:
        return False
    encoding_name = encoding.group(1)
    base_name = base_font.group(1)
    if encoding_name.startswith("UniGB-") and ordering.group(1) == "GB1":
        return True
    return (
        encoding_name in {"Identity-H", "Identity-V"}
        and "Droid#20Sans#20Fallback" in base_name
    )


def _bind_da_to_cjk_appearance(
    document: pymupdf.Document,
    annotation: pymupdf.Annot,
    font_size: float,
) -> None:
    appearance_type, appearance_value = document.xref_get_key(annotation.xref, "AP/N")
    if appearance_type != "xref":
        raise ValueError("annotation appearance is unavailable")
    appearance_xref = int(appearance_value.split()[0])
    appearance_stream = document.xref_stream(appearance_xref).decode("latin-1")
    font_names = re.findall(r"/([^\s]+)\s+[-+]?\d*\.?\d+\s+Tf", appearance_stream)
    if not font_names:
        raise ValueError("annotation CJK appearance font is unavailable")
    name = next(
        (
            candidate
            for candidate in dict.fromkeys(font_names)
            if (xref := _appearance_font_xref(document, appearance_xref, candidate))
            is not None
            and _trusted_cjk_font(document, xref)
        ),
        None,
    )
    if name is None:
        raise ValueError("annotation CJK appearance font is not trusted")
    document.xref_set_key(
        annotation.xref,
        "DA",
        f"({TEXT_COLOR[0]} {TEXT_COLOR[1]} {TEXT_COLOR[2]} rg /{name} {font_size} Tf)",
    )


def _leader_is_valid(
    document: pymupdf.Document,
    annotation: pymupdf.Annot,
    expected: Sequence[Sequence[float]] | None,
) -> bool:
    if expected is None:
        return document.xref_get_key(annotation.xref, "IT")[0] == "null"
    actual = annotation.vertices
    if actual is None or len(actual) != len(expected):
        return False
    return all(
        abs(float(actual_value) - float(expected_value)) <= 0.05
        for actual_point, expected_point in zip(actual, expected, strict=True)
        for actual_value, expected_value in zip(actual_point, expected_point, strict=True)
    )


def _unresolved(
    layout: LayoutResult,
    attempted: Mapping[str, Sequence[Placement]],
    render_paths: Mapping[int, Path],
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
                "final_render_reference": str(render_paths[page_index]),
            }
        )
    return tuple(issues)


def _write_unresolved_artifacts(
    source: Path,
    layout: LayoutResult,
    attempted: Mapping[str, Sequence[Placement]],
    *,
    document: pymupdf.Document | None = None,
    annotated_temp: Path | None = None,
) -> _ArtifactOutcome:
    artifact_id = uuid.uuid4().hex
    page_indexes = sorted({collision.page_index for collision in layout.collisions})
    render_paths: dict[int, Path] = {}
    created: list[_OwnedPath] = []
    if annotated_temp is not None:
        rendered = pymupdf.open(annotated_temp)
    elif document is not None:
        rendered = pymupdf.open(stream=document.tobytes(), filetype="pdf")
        try:
            _write_annotations(rendered, layout.placements)
        except Exception:
            rendered.close()
            rendered = pymupdf.open(stream=document.tobytes(), filetype="pdf")
    else:
        return _ArtifactOutcome(
            problem=_problem("artifact_write_failed", "Failure evidence source is unavailable")
        )
    try:
        for page_index in page_indexes:
            path = source.with_name(
                f".{source.name}.unresolved.{artifact_id}.page-{page_index + 1}.png"
            )
            data = _render_failure_evidence(rendered[page_index])
            created.append(_atomic_artifact_write(path, data))
            render_paths[page_index] = path
        unresolved = _unresolved(layout, attempted, render_paths)
        report_path = source.with_name(f".{source.name}.unresolved.{artifact_id}.json")
        report_data = json.dumps(
            {
                "code": "unresolved_overlap",
                "source_filename": source.name,
                "layout_rounds": layout.rounds,
                "issues": unresolved,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        created.append(_atomic_artifact_write(report_path, report_data))
        return _ArtifactOutcome(unresolved, report_path)
    except Exception as error:
        retained_values, cleanup_mismatches = _cleanup_artifacts(created, source)
        retained = list(retained_values)
        ownership_mismatches = list(cleanup_mismatches)
        if isinstance(error, _ArtifactWriteError):
            retained.extend(
                path
                for path in error.retained
                if path.exists() and path not in retained
            )
            retained.extend(
                path
                for path in error.ownership_mismatches
                if path.exists() and path not in retained
            )
            ownership_mismatches.extend(
                path
                for path in error.ownership_mismatches
                if path not in ownership_mismatches
            )
        return _ArtifactOutcome(
            problem=_problem(
                "artifact_cleanup_failed" if retained else "artifact_write_failed",
                "Failure evidence could not be written transactionally",
                retained_paths=[str(path) for path in retained],
                ownership_mismatch_paths=(
                    [str(path) for path in ownership_mismatches]
                ),
            )
        )
    finally:
        rendered.close()


def _render_failure_evidence(page: pymupdf.Page) -> bytes:
    last_error: Exception | None = None
    for dpi in (300, 200, 144):
        try:
            return page.get_pixmap(
                dpi=dpi,
                alpha=False,
                colorspace=pymupdf.csRGB,
                annots=True,
            ).tobytes("png")
        except Exception as error:
            if not _is_memory_pressure(error):
                raise
            last_error = error
            gc.collect()
    assert last_error is not None
    raise last_error


def _is_memory_pressure(error: Exception) -> bool:
    if isinstance(error, MemoryError):
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in ("out of memory", "cannot allocate", "allocation failed", "malloc")
    )


def _atomic_artifact_write(final_path: Path, data: bytes) -> _OwnedPath:
    part = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    linked_by_this_call = False
    expected: _OwnedPath | None = None
    try:
        with part.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        expected = _capture_owned_path(part)
        if expected is None:
            raise OSError("artifact part ownership could not be established")
        os.link(part, final_path)
        linked_by_this_call = True
        if not _owned_path_matches(expected, final_path):
            raise OSError("linked artifact ownership does not match the part")
        part.unlink()
        if not _owned_path_matches(expected, final_path):
            raise OSError("linked artifact changed before finalization")
    except Exception as error:
        retained: list[Path] = []
        ownership_mismatches: list[Path] = []
        try:
            part.unlink(missing_ok=True)
        except OSError:
            retained.append(part)
        if linked_by_this_call:
            removed, mismatch = _rollback_owned_path(expected, final_path)
            if mismatch:
                ownership_mismatches.append(final_path)
            elif not removed and final_path.exists():
                retained.append(final_path)
        raise _ArtifactWriteError(retained, ownership_mismatches) from error
    return _OwnedPath(final_path, expected.identity, expected.sha256)


def _cleanup_artifacts(
    paths: Sequence[_OwnedPath], source: Path
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    retained: list[Path] = []
    ownership_mismatches: list[Path] = []
    expected_prefix = f".{source.name}.unresolved."
    for owned in dict.fromkeys(paths):
        path = owned.path
        if (
            path.parent.resolve(strict=False) != source.parent.resolve(strict=False)
            or expected_prefix not in path.name
        ):
            ownership_mismatches.append(path)
            continue
        removed, mismatch = _rollback_owned_path(owned, path)
        if mismatch:
            ownership_mismatches.append(path)
        elif not removed and path.exists():
            retained.append(path)
    return tuple(retained), tuple(ownership_mismatches)


def _unique_temp_path(source: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{source.name}.", suffix=".tmp.pdf", dir=source.parent
    )
    os.close(descriptor)
    return Path(value)


def _remove_exact_temp(path: Path, source: Path) -> bool:
    expected_prefix = f".{source.name}."
    if (
        path.parent.resolve(strict=False) != source.parent.resolve(strict=False)
        or not path.name.startswith(expected_prefix)
        or not path.name.endswith(".tmp.pdf")
    ):
        return False
    try:
        path.unlink(missing_ok=True)
        return not path.exists()
    except OSError:
        return False


def _cleanup_problem(path: Path | None, source: Path) -> dict[str, Any] | None:
    if path is None or _remove_temp_with_retry(path, source):
        return None
    return _problem(
        "temp_cleanup_failed",
        "Temporary output could not be removed",
        temp_path=str(path),
    )


def _cleanup_many(
    paths: Sequence[Path | None], source: Path
) -> dict[str, Any] | None:
    retained = [
        path
        for path in paths
        if path is not None and not _remove_temp_with_retry(path, source)
    ]
    if not retained:
        return None
    return _problem(
        "temp_cleanup_failed",
        "Temporary outputs could not be removed",
        retained_paths=[str(path) for path in retained],
        temp_path=str(retained[0]),
    )


def _remove_temp_with_retry(path: Path, source: Path) -> bool:
    for _attempt in range(3):
        if _remove_exact_temp(path, source) or not path.exists():
            return True
    return not path.exists()


def _combine_problems(
    *problems: dict[str, Any] | None,
) -> dict[str, Any] | None:
    present = tuple(problem for problem in problems if problem is not None)
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    paths: dict[str, dict[str, Any]] = {}
    for problem in present:
        for path in _paths_from_problem(problem):
            paths.setdefault(
                str(path),
                {
                    "path": str(path),
                    "kind": _resource_kind(path),
                    "exists": path.exists(),
                },
            )
    return _problem(
        "combined_failure",
        "Multiple safety operations failed",
        causes=list(present),
        retained_resources=list(paths.values()),
    )


def _paths_from_problem(problem: Mapping[str, Any]) -> tuple[Path, ...]:
    found: list[Path] = []

    def visit(key: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                visit(str(nested_key), nested_value)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(key, nested)
        elif isinstance(value, str) and "path" in key.casefold():
            candidate = Path(value)
            if candidate not in found:
                found.append(candidate)

    visit("problem", problem)
    return tuple(found)


def _resource_kind(path: Path) -> str:
    name = path.name.casefold()
    if name.endswith(".part"):
        return "artifact_part"
    if name.endswith(".png"):
        return "artifact_png"
    if name.endswith(".json"):
        return "artifact_json"
    if name.endswith(".annotated.pdf"):
        return "final_pdf"
    if name.endswith(".tmp.pdf"):
        return "temporary_pdf"
    return "path"


def _publish_no_clobber(
    temp_path: Path,
    final_path: Path,
    source: Path,
    *,
    expected_source_identity: tuple[int, int, int, int] | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any] | None:
    expected = _capture_owned_path(temp_path)
    if expected is None:
        primary = _problem(
            "publish_failed",
            "Temporary output ownership could not be established",
            temp_path=str(temp_path),
            temp_exists=temp_path.exists(),
        )
        return _combine_problems(primary, _cleanup_problem(temp_path, source))
    try:
        os.link(temp_path, final_path)
    except FileExistsError:
        primary = _problem(
            "output_exists",
            "Final output already exists",
            final_path=str(final_path),
            final_exists=final_path.exists(),
            ownership="foreign_or_unknown",
        )
    except OSError:
        primary = _problem(
            "publish_failed",
            "Final output could not be published atomically",
            final_path=str(final_path),
            final_exists=final_path.exists(),
            ownership="foreign_or_unknown",
        )
    else:
        if not _owned_path_matches(expected, final_path):
            primary = _problem(
                "publish_rollback_failed",
                "Published output does not match the owned temporary output",
                temp_path=str(temp_path),
                final_path=str(final_path),
                temp_exists=temp_path.exists(),
                final_exists=final_path.exists(),
                ownership_mismatch=True,
            )
            return _combine_problems(primary, _cleanup_problem(temp_path, source))
        cleanup = _cleanup_problem(temp_path, source)
        if cleanup is not None:
            removed, mismatch = _rollback_owned_path(expected, final_path)
            if mismatch or not removed:
                return _problem(
                    "publish_rollback_failed",
                    "Published output could not be safely rolled back",
                    temp_path=str(temp_path),
                    final_path=str(final_path),
                    temp_exists=temp_path.exists(),
                    final_exists=final_path.exists(),
                    ownership_mismatch=mismatch,
                    cleanup_problem=cleanup,
                )
            return cleanup
        if (
            expected_source_identity is not None
            and expected_source_sha256 is not None
            and not _stable_source_matches(
                source, expected_source_identity, expected_source_sha256
            )
        ):
            removed, mismatch = _rollback_owned_path(expected, final_path)
            if mismatch or not removed:
                return _problem(
                    "publish_rollback_failed",
                    "Source changed and published output could not be rolled back",
                    temp_path=str(temp_path),
                    final_path=str(final_path),
                    temp_exists=temp_path.exists(),
                    final_exists=final_path.exists(),
                    ownership_mismatch=mismatch,
                    source_problem=_problem(
                        "source_mismatch", "Source PDF changed at publication linearization point"
                    ),
                )
            return _problem(
                "source_mismatch",
                "Source PDF changed at publication linearization point",
                final_path=str(final_path),
                final_exists=False,
            )
        if not _owned_path_matches(expected, final_path):
            return _problem(
                "publish_rollback_failed",
                "Published output ownership changed before finalization",
                temp_path=str(temp_path),
                final_path=str(final_path),
                temp_exists=temp_path.exists(),
                final_exists=final_path.exists(),
                ownership_mismatch=True,
            )
        return None
    cleanup = _cleanup_problem(temp_path, source)
    return _combine_problems(primary, cleanup)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _source_unchanged(
    path: Path, identity: tuple[int, int, int, int], expected_sha256: str
) -> bool:
    return _stable_source_matches(path, identity, expected_sha256)


def _stable_source_matches(
    path: Path, identity: tuple[int, int, int, int], expected_sha256: str
) -> bool:
    try:
        before = _source_identity(path)
        if before != identity:
            return False
        digest = _sha256(path)
        after = _source_identity(path)
        return before == after == identity and digest == expected_sha256
    except OSError:
        return False


def _capture_owned_path(path: Path) -> _OwnedPath | None:
    try:
        before = _source_identity(path)
        digest = _sha256(path)
        after = _source_identity(path)
    except OSError:
        return None
    if before != after:
        return None
    return _OwnedPath(path, after, digest)


def _owned_path_matches(owned: _OwnedPath | None, path: Path) -> bool:
    return owned is not None and _stable_source_matches(
        path, owned.identity, owned.sha256
    )


def _rollback_owned_path(owned: _OwnedPath | None, path: Path) -> tuple[bool, bool]:
    """Return ``(removed, ownership_mismatch)`` without unlinking foreign paths."""
    if not _owned_path_matches(owned, path):
        return False, True
    try:
        path.unlink()
    except OSError:
        return False, False
    return not path.exists(), False


def _problem(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "details": {key: _json_safe(value) for key, value in details.items()},
    }


def _failure(
    problem: dict[str, Any] | None,
    *,
    layout_rounds: int = 0,
    modified_pages: Sequence[int] = (),
    unresolved_overlaps: Sequence[dict[str, Any]] = (),
    failure_report_path: Path | None = None,
) -> ApplyResult:
    if problem is None:
        problem = _problem("apply_failed", "PDF annotations could not be applied safely")
    return ApplyResult(
        success=False,
        output_path=None,
        problems=(problem,),
        unresolved_overlaps=tuple(unresolved_overlaps),
        layout_rounds=layout_rounds,
        modified_pages=tuple(modified_pages),
        failure_report_path=failure_report_path,
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
