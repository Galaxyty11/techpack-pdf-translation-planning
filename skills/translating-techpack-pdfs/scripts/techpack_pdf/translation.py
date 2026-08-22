"""Deterministic exchange contract for translations produced by a host Agent."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .errors import TechpackError
from .glossary import Glossary, GlossaryHit, normalize_term
from .selection import Candidate, LockedText, LockedToken, validate_locked_tokens


TranslationMode = Literal["direct", "faithful_digest"]
_STABLE_ITEM_ID = re.compile(r"^p[0-9]{3}-i[0-9]{3}$")
_REQUEST_ADAPTER = TypeAdapter(list["_TranslationRequestItem"])
_RESPONSE_ADAPTER = TypeAdapter(list["_TranslationResponseItem"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _GlossaryTerm(_StrictModel):
    source_term: str = Field(min_length=1)
    target_term: str

    @field_validator("source_term")
    @classmethod
    def source_term_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_term must not be blank")
        return value


class _TranslationRequestItem(_StrictModel):
    item_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    context: str
    locked_tokens: list[str]
    glossary_terms: list[_GlossaryTerm]
    page_type: str = Field(min_length=1)
    mode: TranslationMode

    @field_validator("item_id", "source_text", "page_type")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class Translator(_StrictModel):
    host: str = Field(min_length=1)
    execution_mode: Literal["main_agent", "subagent", "mixed"]
    model: str = Field(min_length=1)
    agent_role: str | None = None
    prompt_version: str = Field(min_length=1)

    @field_validator("host", "model", "prompt_version")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class _TranslationResponseItem(_StrictModel):
    item_id: str = Field(min_length=1)
    translated_text: str = Field(min_length=1)
    preserved_tokens: list[str]
    glossary_terms_used: list[str]
    mode: TranslationMode
    warnings: list[str]
    translator: Translator

    @field_validator("item_id", "translated_text")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class _ResponseEnvelope(_StrictModel):
    attempt: Literal[1]
    items: list[_TranslationResponseItem]


@dataclass(frozen=True)
class ValidatedTranslation:
    item_id: str
    translated_text: str
    preserved_tokens: tuple[str, ...]
    glossary_terms_used: tuple[str, ...]
    mode: TranslationMode
    warnings: tuple[str, ...]
    translator: Translator


class TranslationValidationError(TechpackError):
    """A validation failure with either one correction request or a terminal state."""

    def __init__(
        self,
        result: dict[str, Any],
        failed_item_ids: Sequence[str],
        error_codes: Sequence[str],
    ) -> None:
        self.result = result
        self.failed_item_ids = tuple(failed_item_ids)
        self.error_codes = tuple(error_codes)
        self.correction_request = result if "attempt" in result else None
        code = "translation_invalid" if self.correction_request is not None else "human_review_required"
        super().__init__(
            code,
            "Translation response failed deterministic validation",
            {"status": result.get("status", "correction_required")},
        )


def write_translation_request(
    candidates: Sequence[Candidate], path: str | Path
) -> list[dict[str, Any]]:
    """Write the smallest stable request payload for translatable candidates."""
    selected = sorted(
        (candidate for candidate in candidates if candidate.should_translate),
        key=lambda candidate: candidate.item_id,
    )
    item_ids = [candidate.item_id for candidate in selected]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("candidate item_id values must be unique")

    payload = [_request_payload(candidate) for candidate in selected]
    request_path = Path(path)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def validate_translation_response(
    request: Any,
    response: Any,
    glossary: Glossary,
) -> list[ValidatedTranslation]:
    """Validate a host response and join it to the request by stable item ID."""
    request_payload, request_path = _decode_input(request)
    request_attempt = request_payload.get("attempt", 0) if isinstance(request_payload, dict) else 0
    try:
        request_items = _validate_request_payload(request_payload)
    except ValidationError as exc:
        raise ValueError("translation request does not match its schema") from exc

    response_attempt = request_attempt
    try:
        response_payload, _ = _decode_input(response)
        if isinstance(response_payload, dict):
            response_attempt = max(response_attempt, response_payload.get("attempt", 0))
            envelope = _ResponseEnvelope.model_validate(response_payload)
            response_items = envelope.items
        else:
            response_items = _RESPONSE_ADAPTER.validate_python(response_payload)
    except (json.JSONDecodeError, OSError, TypeError, ValidationError) as exc:
        codes, failed_ids = _schema_failure(exc, response if "response_payload" not in locals() else response_payload)
        _raise_validation_failure(
            request_items,
            request_path,
            int(response_attempt) if response_attempt in (0, 1) else 0,
            failed_ids,
            codes,
        )

    request_ids = [item.item_id for item in request_items]
    response_ids = [item.item_id for item in response_items]
    duplicate_ids = sorted(item_id for item_id, count in Counter(response_ids).items() if count > 1)
    if duplicate_ids:
        _raise_validation_failure(
            request_items,
            request_path,
            response_attempt,
            duplicate_ids,
            ["duplicate_item_id"],
        )

    request_id_set = set(request_ids)
    response_id_set = set(response_ids)
    if request_id_set != response_id_set:
        mismatched = sorted(request_id_set.symmetric_difference(response_id_set))
        _raise_validation_failure(
            request_items,
            request_path,
            response_attempt,
            mismatched,
            ["item_id_set_mismatch"],
        )

    response_by_id = {item.item_id: item for item in response_items}
    failures: dict[str, set[str]] = {}
    for request_item in request_items:
        response_item = response_by_id[request_item.item_id]
        item_codes = _validate_item(request_item, response_item, glossary)
        if item_codes:
            failures[request_item.item_id] = item_codes

    if failures:
        _raise_validation_failure(
            request_items,
            request_path,
            response_attempt,
            sorted(failures),
            sorted({code for codes in failures.values() for code in codes}),
        )

    return [
        ValidatedTranslation(
            item_id=response_by_id[item_id].item_id,
            translated_text=response_by_id[item_id].translated_text,
            preserved_tokens=tuple(response_by_id[item_id].preserved_tokens),
            glossary_terms_used=tuple(response_by_id[item_id].glossary_terms_used),
            mode=response_by_id[item_id].mode,
            warnings=tuple(response_by_id[item_id].warnings),
            translator=response_by_id[item_id].translator,
        )
        for item_id in request_ids
    ]


def translation_cache_key(
    request: Any,
    glossary_sha256: str,
    prompt_version: str,
    host: str,
    model: str,
    execution_mode: str,
    job_id: str | None = None,
) -> str:
    """Hash canonical request and execution provenance in the mandated order."""
    request_payload, _ = _decode_input(request)
    request_items = _validate_request_payload(request_payload)
    canonical_request = [item.model_dump(mode="json") for item in request_items]
    if len(glossary_sha256) != 64 or any(character not in "0123456789abcdef" for character in glossary_sha256):
        raise ValueError("glossary_sha256 must be a lowercase SHA-256 hex digest")
    for field_name, value in (
        ("prompt_version", prompt_version),
        ("host", host),
        ("model", model),
        ("execution_mode", execution_mode),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be blank")
    if model == "unknown" and (not isinstance(job_id, str) or not job_id.strip()):
        raise ValueError("job_id is required when model is unknown")

    cache_material: list[list[Any]] = [
        ["request", canonical_request],
        ["glossary_sha256", glossary_sha256],
        ["prompt_version", prompt_version],
        ["host", host],
        ["model", model],
        ["execution_mode", execution_mode],
    ]
    if model == "unknown":
        cache_material.append(["job_id", job_id])
    canonical_material = json.dumps(
        cache_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_material.encode("utf-8")).hexdigest()


def _request_payload(candidate: Candidate) -> dict[str, Any]:
    glossary_terms: list[dict[str, str]] = []
    seen_terms: set[tuple[str, str]] = set()
    for hit in candidate.glossary_hits:
        key = (hit.source_term, hit.target_term)
        if key not in seen_terms:
            glossary_terms.append({"source_term": hit.source_term, "target_term": hit.target_term})
            seen_terms.add(key)
    return {
        "item_id": candidate.item_id,
        "source_text": candidate.source_text,
        "context": candidate.source_kind,
        "locked_tokens": list(candidate.locked_tokens),
        "glossary_terms": glossary_terms,
        "page_type": candidate.page_type.value,
        "mode": "faithful_digest" if candidate.page_type.value == "sample_review" else "direct",
    }


def _decode_input(value: Any) -> tuple[Any, Path | None]:
    if isinstance(value, Path):
        return json.loads(value.read_text(encoding="utf-8")), value
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            return json.loads(value), None
        path = Path(value)
        return json.loads(path.read_text(encoding="utf-8")), path
    return value, None


def _validate_request_payload(payload: Any) -> list[_TranslationRequestItem]:
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    return _REQUEST_ADAPTER.validate_python(payload)


def _schema_failure(exc: Exception, raw_response: Any) -> tuple[list[str], list[str]]:
    codes: set[str] = set()
    failed_ids: set[str] = set()
    raw_items = raw_response.get("items", []) if isinstance(raw_response, dict) else raw_response
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict):
                item_id = item.get("item_id")
                if isinstance(item_id, str) and _STABLE_ITEM_ID.fullmatch(item_id):
                    failed_ids.add(item_id)

    if isinstance(exc, ValidationError):
        for error in exc.errors():
            location = tuple(str(part) for part in error.get("loc", ()))
            if "translated_text" in location:
                codes.add("empty_translation")
            elif "translator" in location and "model" in location:
                codes.add("model_missing")
            elif location and location[-1] == "translator":
                codes.add("translator_missing")
            else:
                codes.add("response_schema_invalid")
    elif isinstance(exc, json.JSONDecodeError):
        codes.add("invalid_json")
    else:
        codes.add("response_schema_invalid")
    return sorted(codes), sorted(failed_ids)


def _validate_item(
    request: _TranslationRequestItem,
    response: _TranslationResponseItem,
    glossary: Glossary,
) -> set[str]:
    codes: set[str] = set()
    if response.mode != request.mode:
        codes.add("mode_mismatch")

    locked_text = _locked_text_from_request(request)
    if (
        Counter(response.preserved_tokens) != Counter(request.locked_tokens)
        or not validate_locked_tokens(locked_text, response.translated_text)
    ):
        codes.add("locked_token_mismatch")

    requested_terms = [term.source_term for term in request.glossary_terms]
    if Counter(response.glossary_terms_used) != Counter(requested_terms):
        codes.add("glossary_terms_used_mismatch")

    normalized_translation = normalize_term(response.translated_text)
    authoritative_hits = glossary.match(request.source_text)
    for term in request.glossary_terms:
        target = _authoritative_target(term, authoritative_hits)
        if target and normalize_term(target) not in normalized_translation:
            codes.add("glossary_target_missing")
    return codes


def _authoritative_target(
    term: _GlossaryTerm,
    authoritative_hits: Sequence[GlossaryHit],
) -> str:
    normalized_source = normalize_term(term.source_term)
    for hit in authoritative_hits:
        if normalized_source in {
            normalize_term(hit.source_term),
            normalize_term(hit.matched_text),
        }:
            return hit.target_term
    return term.target_term


def _locked_text_from_request(request: _TranslationRequestItem) -> LockedText:
    tokens: list[LockedToken] = []
    cursor = 0
    for value in request.locked_tokens:
        start = request.source_text.find(value, cursor)
        if start < 0:
            raise ValueError("request locked_tokens are not in source order")
        end = start + len(value)
        tokens.append(LockedToken(value=value, start=start, end=end, kind="request"))
        cursor = end
    return LockedText(text=request.source_text, tokens=tuple(tokens))


def _raise_validation_failure(
    request_items: Sequence[_TranslationRequestItem],
    request_path: Path | None,
    attempt: int,
    failed_item_ids: Sequence[str],
    error_codes: Sequence[str],
) -> None:
    if not failed_item_ids:
        failed_item_ids = [item.item_id for item in request_items]
    unique_ids = sorted(set(failed_item_ids))
    unique_codes = sorted(set(error_codes))
    if attempt >= 1:
        raise TranslationValidationError(
            {"status": "human_review_required"},
            unique_ids,
            unique_codes,
        ) from None

    result = {
        "attempt": 1,
        "failed_item_ids": unique_ids,
        "error_codes": unique_codes,
        "required_fixes": [_required_fix(code) for code in unique_codes],
    }
    if request_path is not None:
        correction_path = request_path.with_name("correction-request.json")
        correction_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    raise TranslationValidationError(result, unique_ids, unique_codes) from None


def _required_fix(error_code: str) -> str:
    fixes = {
        "duplicate_item_id": "Return each requested item_id exactly once.",
        "empty_translation": "Return a non-empty translated_text value.",
        "glossary_target_missing": "Use every required glossary target term in translated_text.",
        "glossary_terms_used_mismatch": "Report exactly the requested source terms in glossary_terms_used.",
        "invalid_json": "Return valid JSON matching the translation response schema.",
        "item_id_set_mismatch": "Return exactly one item for every requested item_id and no others.",
        "locked_token_mismatch": "Preserve the exact locked token multiset without additions or changes.",
        "mode_mismatch": "Return each item using the mode specified by its request.",
        "model_missing": "Set translator.model to a non-empty identifier or unknown.",
        "response_schema_invalid": "Return only fields allowed by the strict translation response schema.",
        "translator_missing": "Include complete translator provenance for every item.",
    }
    return fixes.get(error_code, "Correct the invalid response item and return it again.")
