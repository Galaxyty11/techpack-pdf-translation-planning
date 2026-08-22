"""Build a self-contained review page and validate completed review documents."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import ValidationError

from .errors import TechpackError
from .inputs import sha256_file
from .models import JobManifest, ReviewDocument, ReviewStatus, TranslatorInfo


_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "review-template.html"
_DATA_PLACEHOLDER = "__TECHPACK_REVIEW_DATA__"
_PNG_PREFIX = "data:image/png;base64,"
_JOB_ID = re.compile(r"^(?P<source>[0-9a-f]{12})-\d{8}T\d{6}Z$")


def build_review_html(job: JobManifest, output: Any) -> str:
    """Return a self-contained review page bound to ``job`` and ``output`` data."""
    try:
        manifest = JobManifest.model_validate(job)
        raw_output = _mapping(output)
        pipeline = _pipeline(raw_output)
        pages = _pages(raw_output)
        items = _items(raw_output)
        page_count = _manifest_page_count(manifest, pages)
        blocking_issues = _json_value(raw_output.get("blocking_issues", []))
        if not isinstance(blocking_issues, list):
            raise ValueError("blocking_issues must be a list")
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


def load_review(path: str | Path, source: str | Path, glossary: str | Path) -> ReviewDocument:
    """Load a completed review only when it still matches the current inputs."""
    review_path = Path(path)
    source_path = Path(source)
    glossary_path = Path(glossary)
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        review = ReviewDocument.model_validate(payload)
    except (OSError, TypeError, json.JSONDecodeError, ValidationError):
        _fail("review_schema_invalid", "Review JSON does not match schema 1.1")

    source_hash = _current_hash(source_path, "source")
    glossary_hash = _current_hash(glossary_path, "glossary")
    page_count = _pdf_page_count(source_path)

    if review.source.filename != source_path.name:
        _fail("review_source_filename_mismatch", "Review source filename does not match")
    if review.source.sha256 != source_hash:
        _fail("review_source_hash_mismatch", "Review source hash does not match")
    if review.source.page_count != page_count:
        _fail("review_source_page_count_mismatch", "Review source page count does not match")
    if review.glossary.filename != glossary_path.name:
        _fail("review_glossary_filename_mismatch", "Review glossary filename does not match")
    if review.glossary.sha256 != glossary_hash:
        _fail("review_glossary_hash_mismatch", "Review glossary hash does not match")
    if not _job_matches_source(review.job_id, source_hash):
        _fail("review_job_mismatch", "Review job is not bound to the current source")
    if review.blocking_issues:
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
        if not all(
            _nonblank(value)
            for value in (
                item.translation_host,
                item.translation_model,
                item.translation_agent_role,
                item.translation_prompt_version,
            )
        ):
            _fail(
                "review_provenance_incomplete",
                "Every review item needs complete translation provenance",
            )
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


def _pipeline(output: dict[str, Any]) -> dict[str, Any]:
    raw = output.get("pipeline")
    if raw is None:
        translations = output.get("translations", [])
        if translations:
            raw = _mapping(translations[0]).get("translator")
    if raw is None:
        raise ValueError("pipeline provenance is required")
    info = TranslatorInfo.model_validate(raw)
    return info.model_dump(mode="json")


def _items(output: dict[str, Any]) -> list[dict[str, Any]]:
    if "items" in output:
        raw_items = output["items"]
        if not isinstance(raw_items, list):
            raise TypeError("items must be a list")
        items = [_mapping(item) for item in raw_items]
        for item in items:
            warnings = item.get("warnings", [])
            conflict = isinstance(warnings, list) and any(
                "conflict" in str(warning).casefold() for warning in warnings
            )
            low_confidence = (
                item.get("coordinate_confidence") != "high"
                or item.get("decision_reason") == "low_confidence"
                or item.get("page_type") == "unknown"
            )
            if item.get("review_status") == "approved" and (conflict or low_confidence):
                item["review_status"] = None
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


def _job_matches_source(job_id: str, source_hash: str) -> bool:
    match = _JOB_ID.fullmatch(job_id)
    return bool(match and match.group("source") == source_hash[:12])


def _nonblank(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fail(code: str, message: str) -> None:
    raise TechpackError(code, message, {"error_code": code}) from None


__all__ = ["build_review_html", "load_review"]
