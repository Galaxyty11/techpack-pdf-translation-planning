"""Bound, host-visual coverage checkpoint for text inside technical figures."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Annotation(_Strict):
    text: StrictStr = Field(min_length=1, max_length=10000)
    bbox: list[StrictFloat] = Field(min_length=4, max_length=4)

    @field_validator("text")
    @classmethod
    def nonblank(cls, text):
        if not text.strip() or text != text.strip():
            raise ValueError("Annotation text must be trimmed and nonblank")
        return text

    @field_validator("bbox")
    @classmethod
    def rectangle(cls, box):
        if not all(math.isfinite(v) for v in box) or box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("Annotation rectangle must be finite and nonempty")
        return box


class PageReview(_Strict):
    page_index: StrictInt = Field(ge=0)
    business_area: Literal[
        "cad_technical_details",
        "bom_color_groups",
        "measurement_table",
        "customer_comments",
        "print_artwork",
        "packaging_information",
        "supplemental",
    ]
    checked: StrictBool
    evidence: StrictStr = Field(min_length=1)
    unresolved: list[StrictStr]
    annotations: list[Annotation]
    protected_item_ids: list[StrictStr]
    visible_color_groups: list[StrictStr]
    required_color_groups: list[StrictStr]
    checked_color_groups: list[StrictStr]

    @field_validator("evidence")
    @classmethod
    def meaningful_evidence(cls, value):
        if not value.strip():
            raise ValueError("Visual evidence required")
        return value


class VisualResponse(_Strict):
    schema_version: Literal["1.1"]
    job_id: StrictStr
    source_sha256: StrictStr
    glossary_sha256: StrictStr
    request_sha256: StrictStr
    production_stage: Literal["development", "bulk", "ambiguous"]
    stage_evidence: list[StrictStr]
    pages: list[PageReview]

    @field_validator("stage_evidence")
    @classmethod
    def meaningful_stage_evidence(cls, value):
        if len(value) != len(set(value)) or any(
            not item.strip() or item != item.strip() for item in value
        ):
            raise ValueError("Stage evidence must be unique, trimmed, and nonblank")
        return value


_BUSINESS_AREAS = {
    "technical_drawing": "cad_technical_details",
    "construction_detail": "cad_technical_details",
    "bom": "bom_color_groups",
    "measurement": "measurement_table",
    "sample_review": "customer_comments",
    "style_sample": "customer_comments",
    "print_artwork": "print_artwork",
    "label_pack": "packaging_information",
}

_DEVELOPMENT_STAGE = re.compile(
    r"(?:\bstage\s*[:\-]?\s*development\b"
    r"|\bdevelopment\s+(?:stage|phase)\b"
    r"|\bsample\s+development\b|开发阶段)",
    re.IGNORECASE,
)
_BULK_STAGE = re.compile(
    r"(?:\bstage\s*[:\-]?\s*bulk\b"
    r"|\bbulk(?:[-\s]+production)?\s+(?:stage|phase)\b"
    r"|\bbulk[-\s]+production\b|大货阶段|量产阶段)",
    re.IGNORECASE,
)


def _page_type_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _stage_binding(pages) -> tuple[str, list[str]]:
    development: list[str] = []
    bulk: list[str] = []
    for page in pages:
        for node in page.nodes:
            text = node.text.strip()
            if _DEVELOPMENT_STAGE.search(text) and text not in development:
                development.append(text)
            if _BULK_STAGE.search(text) and text not in bulk:
                bulk.append(text)
    has_development = bool(development)
    has_bulk = bool(bulk)
    if has_development and not has_bulk:
        return "development", development
    if has_bulk and not has_development:
        return "bulk", bulk
    return "ambiguous", development + bulk


def _candidate_by_item_id(analysis):
    return {candidate.item_id: candidate for candidate in analysis.candidates}


def coverage_request(analysis):
    payload = {key: getattr(analysis, key) for key in ("schema_version", "job_id", "source_sha256", "glossary_sha256")}
    required_stage, stage_evidence = _stage_binding(analysis.pages)
    payload["required_stage"] = required_stage
    payload["stage_evidence"] = stage_evidence
    candidates = _candidate_by_item_id(analysis) if hasattr(analysis, "candidates") else {}
    pages = []
    for page in analysis.pages:
        page_type = _page_type_value(page.page_type)
        annotations = []
        for position, node in enumerate(page.nodes, start=1):
            item_id = f"p{page.page_index + 1:03d}-i{position:03d}"
            candidate = candidates.get(item_id)
            should_translate = (
                candidate.should_translate
                if candidate is not None
                else bool(getattr(node, "should_translate", False))
            )
            reason = (
                candidate.decision_reason
                if candidate is not None
                else getattr(node, "decision_reason", "manual_candidate")
            )
            if hasattr(reason, "value"):
                reason = reason.value
            annotations.append(dict(
                item_id=item_id,
                text=node.text,
                bbox=node.bbox,
                should_translate=should_translate,
                decision_reason=str(reason),
            ))
        pages.append(dict(
            page_index=page.page_index,
            page_type=page_type,
            required_business_area=_BUSINESS_AREAS.get(page_type, "supplemental"),
            thumbnail=page.thumbnail,
            width=page.width,
            height=page.height,
            existing_annotations=annotations,
        ))
    payload["pages"] = pages
    payload["request_sha256"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload


def validate_coverage(request, payload):
    response = VisualResponse.model_validate(payload)
    for key in ("schema_version", "job_id", "source_sha256", "glossary_sha256", "request_sha256"):
        if getattr(response, key) != request[key]:
            raise ValueError("Visual response belongs to a different request")
    expected = {p["page_index"]: p for p in request["pages"]}
    indexes = [p.page_index for p in response.pages]
    if len(indexes) != len(set(indexes)) or set(indexes) != set(expected):
        raise ValueError("Visual coverage must include every requested page exactly once")
    if response.production_stage != request["required_stage"]:
        raise ValueError("Production stage does not match the bound request")
    if response.stage_evidence != request["stage_evidence"]:
        raise ValueError("Stage evidence does not match the bound request")
    for page in response.pages:
        bounds = expected[page.page_index]
        if page.business_area != bounds["required_business_area"]:
            raise ValueError("Business area does not match the bound page")
        if (
            len(page.protected_item_ids) != len(set(page.protected_item_ids))
            or any(
                not item_id.strip() or item_id != item_id.strip()
                for item_id in page.protected_item_ids
            )
        ):
            raise ValueError("Protected item IDs must be unique, trimmed, and nonblank")
        existing = {
            item["item_id"]: item
            for item in bounds["existing_annotations"]
        }
        if page.protected_item_ids:
            if bounds["page_type"] != "print_artwork" or any(
                item_id not in existing
                or not existing[item_id]["should_translate"]
                for item_id in page.protected_item_ids
            ):
                raise ValueError(
                    "Only selected existing print-artwork items may be protected"
                )
        group_lists = (
            page.visible_color_groups,
            page.required_color_groups,
            page.checked_color_groups,
        )
        if any(
            not value.strip() or value != value.strip() or len(values) != len(set(values))
            for values in group_lists
            for value in values
        ):
            raise ValueError("Color groups must be unique, trimmed, and nonblank")
        if page.business_area == "bom_color_groups":
            visible, required, checked = map(set, group_lists)
            if not visible or not required or checked != required:
                raise ValueError("Every required BOM color group must be checked")
            if response.production_stage == "development":
                if not required.issubset(visible):
                    raise ValueError("Development BOM groups must be visible")
            elif required != visible:
                raise ValueError("Bulk or ambiguous BOM review must cover all visible groups")
        elif any(group_lists):
            raise ValueError("Color-group fields are only valid on BOM pages")
        for annotation in page.annotations:
            x0, y0, x1, y1 = annotation.bbox
            if x0 < 0 or y0 < 0 or x1 > bounds["width"] or y1 > bounds["height"]:
                raise ValueError("Visual annotation is outside its page")
    return response


def is_existing_annotation(annotation, nodes):
    # Same text at a different location is a separate callout, not a duplicate.
    text = " ".join(annotation.text.casefold().split())
    for node in nodes:
        node_text = node["text"] if isinstance(node, dict) else node.text
        should_translate = (
            node.get("should_translate", False)
            if isinstance(node, dict)
            else bool(getattr(node, "should_translate", False))
        )
        if not should_translate or " ".join(node_text.casefold().split()) != text:
            continue
        a = annotation.bbox
        b = node["bbox"] if isinstance(node, dict) else node.bbox
        area = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
        if area / min((a[2]-a[0])*(a[3]-a[1]), max((b[2]-b[0])*(b[3]-b[1]), 1e-9)) >= .5:
            return True
    return False
