"""Build a self-contained review page and validate completed review documents."""

from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import ValidationError

from .errors import TechpackError
from .glossary import normalize_term
from .inputs import sha256_file
from .models import JobManifest, PipelineInfo, ReviewDocument, ReviewItem, ReviewStatus
from .selection import LockedText, LockedToken, validate_locked_tokens


_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "review-template.html"
_DATA_PLACEHOLDER = "__TECHPACK_REVIEW_DATA__"
_PNG_PREFIX = "data:image/png;base64,"
_MUTABLE_REVIEW_FIELDS = frozenset({"review_status", "reviewed_translation"})


def build_review_html(job: JobManifest, output: Any) -> str:
    """Return a self-contained review page bound to ``job`` and ``output`` data."""
    try:
        manifest = JobManifest.model_validate(job)
        raw_output = _mapping(output)
        pages = _pages(raw_output)
        items = _items(raw_output)
        pipeline = _pipeline(raw_output, items)
        page_count = _manifest_page_count(manifest, pages)
        blocking_issues = _blocking_issues(raw_output)
        payload = {
            "schema_version": "1.1",
            "job_id": manifest.job_id,
            "source": {
                "filename": manifest.source.filename,
                "sha256": manifest.source.sha256,
                "page_count": page_count,
            },
            "glossary": {
                "filename": manifest.glossary.filename,
                "sha256": manifest.glossary.sha256,
            },
            "pipeline": pipeline,
            "items": items,
            "blocking_issues": blocking_issues,
            "review_completed_at": None,
            "pages": pages,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        if template.count(_DATA_PLACEHOLDER) != 1:
            raise ValueError("review template data placeholder is invalid")
        return template.replace(_DATA_PLACEHOLDER, encoded)
    except TechpackError:
        raise
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise TechpackError(
            "review_page_invalid",
            "Offline review page data is invalid",
            {"error_code": "review_page_invalid"},
        ) from None


def load_review(
    path: str | Path,
    job: JobManifest,
    expected_output: Any,
) -> ReviewDocument:
    """Load a completed review bound to the job and trusted generated output."""
    review_path = Path(path)
    try:
        manifest = JobManifest.model_validate(job)
        if manifest.source.path is None or manifest.glossary.path is None:
            raise ValueError("job input paths are required")
        source_path = Path(manifest.source.path)
        glossary_path = Path(manifest.glossary.path)
        raw_expected_output = _mapping(expected_output)
        trusted_item_values = _items(raw_expected_output)
        trusted_items = [ReviewItem.model_validate(item) for item in trusted_item_values]
        trusted_pipeline = PipelineInfo.model_validate(
            _pipeline(raw_expected_output, trusted_item_values)
        )
        trusted_blocking_issues = _blocking_issues(raw_expected_output)
    except (TypeError, ValueError, ValidationError):
        _fail("review_job_invalid", "Review job binding is invalid")
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        _validate_review_timestamp(payload)
        review = ReviewDocument.model_validate(payload)
    except (OSError, TypeError, json.JSONDecodeError, ValidationError):
        _fail("review_schema_invalid", "Review JSON does not match schema 1.1")

    source_hash = _current_hash(source_path, "source")
    glossary_hash = _current_hash(glossary_path, "glossary")
    page_count = _pdf_page_count(source_path)

    if review.job_id != manifest.job_id:
        _fail("review_job_mismatch", "Review job does not match")
    if (
        review.source.filename != manifest.source.filename
        or review.source.filename != source_path.name
    ):
        _fail("review_source_filename_mismatch", "Review source filename does not match")
    if review.source.sha256 != manifest.source.sha256 or review.source.sha256 != source_hash:
        _fail("review_source_hash_mismatch", "Review source hash does not match")
    if review.source.page_count != manifest.source.page_count or review.source.page_count != page_count:
        _fail("review_source_page_count_mismatch", "Review source page count does not match")
    if (
        review.glossary.filename != manifest.glossary.filename
        or review.glossary.filename != glossary_path.name
    ):
        _fail("review_glossary_filename_mismatch", "Review glossary filename does not match")
    if review.glossary.sha256 != manifest.glossary.sha256 or review.glossary.sha256 != glossary_hash:
        _fail("review_glossary_hash_mismatch", "Review glossary hash does not match")
    _validate_item_binding(review.items, trusted_items)
    _validate_pipeline(review, trusted_pipeline)
    if review.blocking_issues != trusted_blocking_issues:
        _fail("review_blocking_mismatch", "Review blocking issues do not match the job")
    if trusted_blocking_issues:
        _fail("review_blocked", "Review contains unresolved blocking issues")
    if review.review_completed_at is None:
        _fail("review_incomplete", "Review completion time is missing")

    seen_ids: set[str] = set()
    for item in review.items:
        if item.item_id in seen_ids:
            _fail("review_item_duplicate", "Review contains duplicate item identifiers")
        seen_ids.add(item.item_id)
        if item.page_index >= page_count:
            _fail("review_item_page_mismatch", "Review item page is outside the source PDF")
        if item.review_status is None:
            _fail("review_status_incomplete", "Every review item needs an explicit status")
        if item.review_status is ReviewStatus.APPROVED_EDITED and not _nonblank(
            item.reviewed_translation
        ):
            _fail(
                "review_edited_translation_missing",
                "An edited approval needs a reviewed translation",
            )
        if item.review_status is ReviewStatus.APPROVED and not _nonblank(
            item.suggested_translation
        ):
            _fail("review_translation_missing", "An approval needs a validated translation")
        base_provenance = all(
            _nonblank(value)
            for value in (
                item.translation_host,
                item.translation_model,
                item.translation_prompt_version,
            )
        )
        if item.translation_execution_mode.value == "main_agent":
            role_provenance = item.translation_agent_role is None or _nonblank(
                item.translation_agent_role
            )
        else:
            role_provenance = _nonblank(item.translation_agent_role)
        if not base_provenance or not role_provenance:
            _fail(
                "review_provenance_incomplete",
                "Every review item needs complete translation provenance",
            )
        if item.review_status is not ReviewStatus.SKIPPED:
            _validate_final_translation(item)
    return review


def _mapping(value: Any) -> dict[str, Any]:
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("review output must be a mapping")
    return converted


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _blocking_issues(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = _json_value(output.get("blocking_issues", []))
    if not isinstance(value, list) or not all(isinstance(issue, dict) for issue in value):
        raise ValueError("blocking_issues must be a list of mappings")
    return value


def _pipeline(output: dict[str, Any], items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw = output.get("pipeline")
    aggregate = _aggregate_item_provenance(items)
    if raw is None:
        raw = {
            "parser": output.get("parser"),
            "translation_executor": output.get("translation_executor", "host_agent"),
            **aggregate,
        }
    info = PipelineInfo.model_validate(raw)
    if items and any(
        getattr(info, field) != value for field, value in aggregate.items()
    ):
        raise ValueError("pipeline contradicts item provenance")
    return info.model_dump(mode="json")


def _aggregate_item_provenance(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    if not items:
        return {}
    fields = {
        "host": "translation_host",
        "execution_mode": "translation_execution_mode",
        "model": "translation_model",
        "prompt_version": "translation_prompt_version",
    }
    aggregate: dict[str, str] = {}
    for pipeline_field, item_field in fields.items():
        values = {str(item.get(item_field, "")).strip() for item in items}
        if not values or "" in values:
            raise ValueError("item translation provenance is incomplete")
        aggregate[pipeline_field] = next(iter(values)) if len(values) == 1 else "mixed"
    return aggregate


def _items(output: dict[str, Any]) -> list[dict[str, Any]]:
    if "items" in output:
        raw_items = output["items"]
        if not isinstance(raw_items, list):
            raise TypeError("items must be a list")
        items = [_mapping(item) for item in raw_items]
        for item in items:
            item["review_status"] = None
            item["reviewed_translation"] = None
        return items

    candidates = output.get("candidates")
    translations = output.get("translations")
    if not isinstance(candidates, list) or not isinstance(translations, list):
        raise ValueError("items or candidates with translations are required")
    translated = {_mapping(value)["item_id"]: _mapping(value) for value in translations}
    items: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = _mapping(raw_candidate)
        if not candidate.get("should_translate", True):
            continue
        translation = translated[candidate["item_id"]]
        translator = _mapping(translation["translator"])
        bbox = candidate.get("source_bbox")
        if bbox is None:
            raise ValueError("review candidates need source coordinates")
        glossary_hits = candidate.get("glossary_hits", [])
        locked_tokens = candidate.get("locked_tokens")
        if locked_tokens is None:
            locked_text = candidate.get("locked_text", {})
            locked_tokens = [token["value"] for token in locked_text.get("tokens", [])]
        risks: list[str] = list(translation.get("warnings", []))
        confidence = candidate.get("coordinate_confidence", "low")
        model = translator.get("model")
        if confidence != "high":
            risks.append("coordinate_confidence")
        if model == "unknown":
            risks.append("unknown_model")
        items.append(
            {
                "item_id": candidate["item_id"],
                "page_index": candidate["page_index"],
                "page_type": candidate["page_type"],
                "source_text": candidate["source_text"],
                "normalized_text": candidate["normalized_text"],
                "source_bbox": bbox,
                "source_kind": candidate["source_kind"],
                "coordinate_confidence": confidence,
                "decision_reason": candidate["decision_reason"],
                "locked_tokens": locked_tokens,
                "glossary_hits": glossary_hits,
                "suggested_translation": translation.get(
                    "translated_text", translation.get("translation")
                ),
                "reviewed_translation": None,
                "review_status": None,
                "risk_level": "low" if candidate.get("auto_approvable") and not risks else "high",
                "translation_host": translator.get("host"),
                "translation_execution_mode": translator.get("execution_mode"),
                "translation_model": model,
                "translation_agent_role": translator.get("agent_role"),
                "translation_prompt_version": translator.get("prompt_version"),
                "placement_strategy": candidate.get("placement_strategy"),
                "target_rect": candidate.get("target_rect"),
                "font_size": candidate.get("font_size"),
                "leader_line": candidate.get("leader_line"),
                "warnings": risks,
            }
        )
    return items


def _pages(output: dict[str, Any]) -> list[dict[str, Any]]:
    raw_pages = output.get("pages")
    if raw_pages is None and "thumbnails" in output:
        raw_pages = [
            {"page_index": index, "thumbnail": thumbnail}
            for index, thumbnail in enumerate(output["thumbnails"])
        ]
    if not isinstance(raw_pages, list) or not raw_pages:
        raise ValueError("at least one review page is required")
    pages: list[dict[str, Any]] = []
    for position, raw_page in enumerate(raw_pages):
        page = _mapping(raw_page)
        thumbnail = page.get("thumbnail", page.get("thumbnail_data_uri", page.get("thumbnail_path")))
        width = page.get("width")
        height = page.get("height")
        crop_box = page.get("crop_box")
        if (width is None or height is None) and isinstance(crop_box, list) and len(crop_box) == 4:
            width = float(crop_box[2]) - float(crop_box[0])
            height = float(crop_box[3]) - float(crop_box[1])
        pages.append(
            {
                "page_index": int(page.get("page_index", position)),
                "width": float(width) if width is not None else None,
                "height": float(height) if height is not None else None,
                "thumbnail": _thumbnail_data_uri(thumbnail),
            }
        )
    return pages


def _thumbnail_data_uri(value: Any) -> str:
    if isinstance(value, str) and value.startswith(_PNG_PREFIX):
        encoded = value[len(_PNG_PREFIX) :]
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("thumbnail is not valid base64") from None
    elif isinstance(value, (str, Path)):
        decoded = Path(value).read_bytes()
        encoded = base64.b64encode(decoded).decode("ascii")
    else:
        raise ValueError("thumbnail must be a PNG path or data URI")
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("thumbnail must be PNG")
    return _PNG_PREFIX + encoded


def _manifest_page_count(job: JobManifest, pages: Sequence[Mapping[str, Any]]) -> int:
    page_count = job.source.page_count
    if page_count is None and job.source.path is not None:
        page_count = _pdf_page_count(Path(job.source.path))
    if page_count is None:
        page_count = len(pages)
    if page_count != len(pages):
        raise ValueError("review page count does not match job")
    expected_indexes = list(range(page_count))
    actual_indexes = [int(page["page_index"]) for page in pages]
    if actual_indexes != expected_indexes:
        raise ValueError("review pages must be complete and ordered")
    return page_count


def _current_hash(path: Path, kind: str) -> str:
    try:
        return sha256_file(path)
    except OSError:
        _fail(f"review_{kind}_unavailable", f"Current {kind} file cannot be read")


def _pdf_page_count(path: Path) -> int:
    try:
        document = pymupdf.open(path)
        try:
            if document.needs_pass or document.page_count <= 0:
                raise ValueError("source PDF is unavailable")
            return document.page_count
        finally:
            document.close()
    except (OSError, RuntimeError, ValueError):
        _fail("review_source_unavailable", "Current source PDF cannot be inspected")


def _validate_item_binding(
    reviewed_items: Sequence[ReviewItem], expected_items: Sequence[ReviewItem]
) -> None:
    reviewed_ids = [item.item_id for item in reviewed_items]
    expected_ids = [item.item_id for item in expected_items]
    if len(reviewed_ids) != len(set(reviewed_ids)):
        _fail("review_item_duplicate", "Review contains duplicate item identifiers")
    if len(expected_ids) != len(set(expected_ids)):
        _fail("review_job_invalid", "Expected review items contain duplicate identifiers")
    if set(reviewed_ids) != set(expected_ids):
        _fail("review_item_set_mismatch", "Review item set does not match the job")

    expected_by_id = {item.item_id: item for item in expected_items}
    immutable_fields = set(ReviewItem.model_fields) - _MUTABLE_REVIEW_FIELDS
    for item in reviewed_items:
        actual = item.model_dump(mode="json", include=immutable_fields)
        expected = expected_by_id[item.item_id].model_dump(
            mode="json", include=immutable_fields
        )
        if actual != expected:
            _fail("review_item_mismatch", "Review item content does not match the job")


def _validate_review_timestamp(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        return
    value = payload.get("review_completed_at")
    if value is None:
        return
    if not isinstance(value, str):
        _fail("review_timestamp_invalid", "Review completion time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("review_timestamp_invalid", "Review completion time is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("review_timestamp_invalid", "Review completion time needs a timezone")


def _validate_pipeline(review: ReviewDocument, trusted_pipeline: PipelineInfo) -> None:
    try:
        aggregate = _aggregate_item_provenance(
            [item.model_dump(mode="json") for item in review.items]
        )
    except ValueError:
        _fail(
            "review_provenance_incomplete",
            "Review item translation provenance is incomplete",
        )
    if (
        any(getattr(review.pipeline, field) != value for field, value in aggregate.items())
        or review.pipeline != trusted_pipeline
    ):
        _fail("review_pipeline_mismatch", "Review pipeline contradicts item provenance")


def _validate_final_translation(item: ReviewItem) -> None:
    final_translation = (
        item.reviewed_translation
        if item.review_status is ReviewStatus.APPROVED_EDITED
        else item.suggested_translation
    )
    if not _nonblank(final_translation):
        _fail("review_translation_missing", "Approved item has no final translation")

    exact_dnt_values: set[str] = set()
    for hit in item.glossary_hits:
        if not isinstance(hit, Mapping):
            _fail("review_glossary_invalid", "Review glossary hit is invalid")
        if bool(hit.get("do_not_translate")):
            exact_value = _project_do_not_translate_value(item.source_text, hit)
            if exact_value is None:
                _fail(
                    "review_glossary_dnt_mismatch",
                    "Final translation changed a do-not-translate term",
                )
            exact_dnt_values.add(exact_value)

    if any(
        item.source_text.count(value) <= 0
        or final_translation.count(value) != item.source_text.count(value)
        for value in exact_dnt_values
    ):
        _fail(
            "review_glossary_dnt_mismatch",
            "Final translation changed a do-not-translate term",
        )

    tokens: list[LockedToken] = []
    cursor = 0
    for value in item.locked_tokens:
        start = item.source_text.find(value, cursor)
        if start < 0:
            _fail("review_locked_token_mismatch", "Final translation changed locked tokens")
        end = start + len(value)
        tokens.append(LockedToken(value=value, start=start, end=end, kind="review"))
        cursor = end
    locked = LockedText(text=item.source_text, tokens=tuple(tokens))
    if not validate_locked_tokens(locked, final_translation):
        _fail("review_locked_token_mismatch", "Final translation changed locked tokens")

    normalized_translation = normalize_term(final_translation)
    for hit in item.glossary_hits:
        if bool(hit.get("do_not_translate")):
            continue
        target = str(hit.get("target_term") or "")
        if not target or normalize_term(target) not in normalized_translation:
            _fail(
                "review_glossary_target_missing",
                "Final translation omitted an authoritative glossary target",
            )


def _project_do_not_translate_value(
    source_text: str, hit: Mapping[str, Any]
) -> str | None:
    start = hit.get("start")
    end = hit.get("end")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    if not isinstance(end, int) or isinstance(end, bool):
        return None
    projected = _project_normalized_span(source_text, start, end)
    if projected is None:
        return None
    source_start, source_end = projected
    exact_value = source_text[source_start:source_end]
    references = {
        normalize_term(str(value))
        for value in (hit.get("matched_text"), hit.get("source_term"))
        if isinstance(value, str) and value
    }
    if not exact_value or normalize_term(exact_value) not in references:
        return None
    return exact_value


def _project_normalized_span(
    source_text: str, start: int, end: int
) -> tuple[int, int] | None:
    normalized_length = len(normalize_term(source_text))
    if start < 0 or end <= start or end > normalized_length:
        return None
    prefix_lengths = [
        len(normalize_term(source_text[:index]))
        for index in range(len(source_text) + 1)
    ]
    source_start = next(
        (index - 1 for index, length in enumerate(prefix_lengths) if length > start),
        None,
    )
    source_end = next(
        (index for index, length in enumerate(prefix_lengths) if length >= end),
        None,
    )
    while (
        source_end is not None
        and source_end < len(source_text)
        and unicodedata.combining(source_text[source_end])
    ):
        source_end += 1
    if (
        source_start is None
        or source_end is None
        or source_start < 0
        or source_end <= source_start
    ):
        return None
    return source_start, source_end


def _nonblank(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fail(code: str, message: str) -> None:
    raise TechpackError(code, message, {"error_code": code}) from None


__all__ = ["build_review_html", "load_review"]
