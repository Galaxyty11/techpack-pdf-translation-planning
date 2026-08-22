"""Safely apply a validated review as editable PDF FreeText annotations."""

from __future__ import annotations

import hashlib
import json
import os
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
    detect_collisions,
    detect_candidate_collisions,
    detect_rendered_collisions,
    extract_protected_geometry,
    find_same_row_blank_cells,
    infer_semantic_region,
    optimize_layout,
    rank_placements,
)
from .errors import TechpackError
from .models import JobManifest, ReviewDocument, ReviewItem, ReviewStatus
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


@dataclass(frozen=True)
class _ArtifactOutcome:
    unresolved: tuple[dict[str, Any], ...] = ()
    report_path: Path | None = None
    problem: dict[str, Any] | None = None


class _ArtifactWriteError(RuntimeError):
    def __init__(self, retained: Sequence[Path] = ()) -> None:
        super().__init__("artifact write failed")
        self.retained = tuple(retained)


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
            return _failure(cleanup or _problem("source_mismatch", "Source PDF changed during validation"))

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
            return _failure(cleanup or identity_problem)

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
                    return _failure(artifact.problem or cleanup)
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
            return _failure(cleanup or identity_problem)
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
                        return _failure(artifact.problem or cleanup)
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
                    return _failure(cleanup, layout_rounds=total_rounds, modified_pages=modified_pages)
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
                            return _failure(artifact.problem or cleanup)
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
                    return _failure(cleanup or identity_problem)
                continue
            if verification.problem is not None:
                cleanup = _cleanup_many((temp_path, snapshot_path), source)
                if cleanup is not None:
                    return _failure(cleanup, layout_rounds=total_rounds, modified_pages=modified_pages)
                temp_path = None
                snapshot_path = None
                return _failure(
                    verification.problem,
                    layout_rounds=total_rounds,
                    modified_pages=modified_pages,
                )
            break

        if not _source_unchanged(source, source_identity, review.source.sha256):
            cleanup = _cleanup_many((temp_path, snapshot_path), source)
            temp_path = None if cleanup is None else temp_path
            snapshot_path = None if cleanup is None else snapshot_path
            return _failure(cleanup or _problem("source_mismatch", "Source PDF changed before publication"))
        cleanup = _cleanup_problem(snapshot_path, source)
        if cleanup is not None:
            combined = _cleanup_many((snapshot_path, temp_path), source)
            return _failure(combined or cleanup)
        snapshot_path = None
        publish_problem = _publish_no_clobber(temp_path, final_path, source)
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
        if cleanup is not None:
            return _failure(cleanup)
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


def _layout_document(
    document: pymupdf.Document,
    items: Sequence[ReviewItem],
    *,
    excluded: set[tuple[object, ...]] | None = None,
    max_rounds: int = 10,
) -> tuple[LayoutResult, dict[str, list[Placement]]]:
    candidates_by_id: dict[str, list[Placement]] = {}
    initial: list[Placement] = []
    exhausted: list[Collision] = []
    by_page: dict[int, list[ReviewItem]] = defaultdict(list)
    for item in items:
        by_page[item.page_index].append(item)

    for page_index in sorted(by_page):
        page = document[page_index]
        protected = extract_protected_geometry(page)
        for item in sorted(by_page[page_index], key=lambda value: value.item_id):
            candidates = rank_placements(
                item,
                _canonical_page_rect(page),
                protected=protected,
                same_row_cells=find_same_row_blank_cells(page, item.source_bbox),
                semantic_region=infer_semantic_region(page, item.source_bbox),
            )
            if excluded:
                candidates = [
                    candidate
                    for candidate in candidates
                    if _placement_signature(candidate) not in excluded
                ]
            candidates_by_id[item.item_id] = candidates
            if candidates:
                initial.append(candidates[0])
            else:
                exhausted.append(
                    Collision(
                        page_index,
                        item.item_id,
                        "candidate_exhausted",
                        "candidate_set",
                    )
                )

    def collision_detector(placements: Sequence[Placement]) -> Sequence[Collision]:
        collisions: list[Collision] = []
        grouped: dict[int, list[Placement]] = defaultdict(list)
        for placement in placements:
            grouped[placement.page_index].append(placement)
        for page_index, page_placements in grouped.items():
            geometric = detect_collisions(
                document[page_index], page_placements, render_check=False
            )
            if geometric:
                collisions.extend(geometric)
            else:
                collisions.extend(
                    detect_candidate_collisions(
                        document[page_index], page_placements
                    )
                )
        return collisions

    result = optimize_layout(
        initial,
        collision_detector=collision_detector,
        candidate_provider=lambda placement, _collisions: candidates_by_id[
            placement.item_id
        ],
        max_rounds=max_rounds,
    )
    if exhausted:
        result = LayoutResult(
            result.placements,
            tuple(sorted((*result.collisions, *exhausted), key=lambda value: (value.page_index, value.item_id, value.kind))),
            result.rounds,
            result.stable,
        )
    return result, candidates_by_id


def _write_annotations(
    document: pymupdf.Document, placements: Sequence[Placement]
) -> tuple[_WrittenAnnotation, ...]:
    written: list[_WrittenAnnotation] = []
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
        written.append(
            _WrittenAnnotation(placement.page_index, placement.item_id, annotation.xref)
        )
    return tuple(written)


def _placement_signature(placement: Placement) -> tuple[object, ...]:
    return (
        placement.item_id,
        tuple(round(value, 4) for value in placement.rect),
        placement.font_size,
        placement.strategy,
        placement.leader_line,
    )


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


def _canonical_page_rect(page: pymupdf.Page) -> pymupdf.Rect:
    """Crop-relative, unrotated coordinates used by extraction and annotation APIs."""
    return pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)


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
        if (
            approved_ids != written_ids
            or any(count != 1 for count in approved_ids.values())
            or len({(value.page_index, value.xref) for value in written}) != len(written)
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
                or before.drawings != after.drawings
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
    created: list[Path] = []
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
            data = rendered[page_index].get_pixmap(
                dpi=300, alpha=False, colorspace=pymupdf.csRGB, annots=True
            ).tobytes("png")
            created.append(path)
            _atomic_artifact_write(path, data)
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
        created.append(report_path)
        _atomic_artifact_write(report_path, report_data)
        return _ArtifactOutcome(unresolved, report_path)
    except Exception as error:
        retained = list(_cleanup_artifacts(created, source))
        if isinstance(error, _ArtifactWriteError):
            retained.extend(
                path
                for path in error.retained
                if path.exists() and path not in retained
            )
        return _ArtifactOutcome(
            problem=_problem(
                "artifact_cleanup_failed" if retained else "artifact_write_failed",
                "Failure evidence could not be written transactionally",
                retained_paths=[str(path) for path in retained],
            )
        )
    finally:
        rendered.close()


def _atomic_artifact_write(final_path: Path, data: bytes) -> Path:
    part = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    try:
        with part.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(part, final_path)
        part.unlink()
    except Exception as error:
        retained: list[Path] = []
        for path in (part, final_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                retained.append(path)
            else:
                if path.exists():
                    retained.append(path)
        raise _ArtifactWriteError(retained) from error
    return part


def _cleanup_artifacts(paths: Sequence[Path], source: Path) -> tuple[Path, ...]:
    retained: list[Path] = []
    expected_prefix = f".{source.name}.unresolved."
    for path in dict.fromkeys(paths):
        if (
            path.parent.resolve(strict=False) != source.parent.resolve(strict=False)
            or expected_prefix not in path.name
        ):
            retained.append(path)
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            retained.append(path)
        else:
            if path.exists():
                retained.append(path)
    return tuple(retained)


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
    if path is None or _remove_exact_temp(path, source):
        return None
    return _problem(
        "temp_cleanup_failed",
        "Temporary output could not be removed",
        temp_path=str(path),
    )


def _cleanup_many(
    paths: Sequence[Path | None], source: Path
) -> dict[str, Any] | None:
    retained = [path for path in paths if path is not None and not _remove_exact_temp(path, source)]
    if not retained:
        return None
    return _problem(
        "temp_cleanup_failed",
        "Temporary outputs could not be removed",
        retained_paths=[str(path) for path in retained],
        temp_path=str(retained[0]),
    )
def _publish_no_clobber(
    temp_path: Path, final_path: Path, source: Path
) -> dict[str, Any] | None:
    try:
        os.link(temp_path, final_path)
    except FileExistsError:
        code = "output_exists"
        message = "Final output already exists"
    except OSError:
        code = "publish_failed"
        message = "Final output could not be published atomically"
    else:
        if _remove_exact_temp(temp_path, source):
            return None
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            return _problem(
                "publish_rollback_failed",
                "Published output and temporary output could not be rolled back",
                temp_path=str(temp_path),
                final_path=str(final_path),
                temp_exists=temp_path.exists(),
                final_exists=final_path.exists(),
            )
        if final_path.exists():
            return _problem(
                "publish_rollback_failed",
                "Published output and temporary output could not be rolled back",
                temp_path=str(temp_path),
                final_path=str(final_path),
                temp_exists=temp_path.exists(),
                final_exists=True,
            )
        return _problem(
            "temp_cleanup_failed",
            "Published output exists but temporary output could not be removed",
            temp_path=str(temp_path),
        )
    if not _remove_exact_temp(temp_path, source):
        return _problem(
            "temp_cleanup_failed",
            "Temporary output could not be removed",
            temp_path=str(temp_path),
        )
    return _problem(code, message)


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
    try:
        return _source_identity(path) == identity and _sha256(path) == expected_sha256
    except OSError:
        return False


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
