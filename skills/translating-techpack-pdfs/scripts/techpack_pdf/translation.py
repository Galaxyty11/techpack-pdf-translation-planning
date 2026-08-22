"""Bound, deterministic translation exchange contracts for host Agents."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import TechpackError
from .glossary import Glossary, GlossaryHit, normalize_term
from .models import JobManifest
from .selection import Candidate, LockedText, LockedToken, validate_locked_tokens


TranslationMode = Literal["direct", "faithful_digest"]
_SCHEMA_VERSION = "1.1"
_STABLE_ITEM_ID = re.compile(r"^p[0-9]{3}-i[0-9]{3}$")
_SHA256 = r"^[0-9a-f]{64}$"


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
    item_id: str = Field(pattern=_STABLE_ITEM_ID.pattern)
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
    item_id: str = Field(pattern=_STABLE_ITEM_ID.pattern)
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


class _RequestEnvelope(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=_SHA256)
    glossary_sha256: str = Field(pattern=_SHA256)
    request_sha256: str = Field(pattern=_SHA256)
    attempt: Literal[0]
    items: list[_TranslationRequestItem]


class _ResponseEnvelope(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=_SHA256)
    glossary_sha256: str = Field(pattern=_SHA256)
    request_sha256: str = Field(pattern=_SHA256)
    attempt: Literal[0, 1]
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
    """A response failure with one correction request or a terminal stop."""

    def __init__(self, result: dict[str, Any], failed_item_ids: Sequence[str], error_codes: Sequence[str]) -> None:
        self.result = result
        self.failed_item_ids = tuple(failed_item_ids)
        self.error_codes = tuple(error_codes)
        self.correction_request = result if "attempt" in result else None
        code = "translation_invalid" if self.correction_request is not None else "human_review_required"
        super().__init__(code, "Translation response failed deterministic validation", {"status": result.get("status", "correction_required")})


def write_translation_request(candidates: Sequence[Candidate], path: str | Path, job: JobManifest) -> dict[str, Any]:
    """Persist a strict request envelope bound to exactly one job."""
    manifest = _validated_job(job)
    selected = sorted((candidate for candidate in candidates if candidate.should_translate), key=lambda candidate: candidate.item_id)
    item_ids = [candidate.item_id for candidate in selected]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("candidate item_id values must be unique")
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "job_id": manifest.job_id,
        "source_sha256": manifest.source.sha256,
        "glossary_sha256": manifest.glossary.sha256,
        "attempt": 0,
        "items": [_request_payload(candidate) for candidate in selected],
    }
    payload["request_sha256"] = _request_hash(payload)
    serialized = _RequestEnvelope.model_validate(payload).model_dump(mode="json")
    request_path = Path(path)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return serialized


def validate_translation_response(request: Any, response: Any, glossary: Glossary, job: JobManifest) -> list[ValidatedTranslation]:
    """Validate a response envelope against both a trusted request and manifest."""
    manifest = _validated_job(job)
    request_payload, request_path = _decode_input(request)
    request_envelope = _validated_request(request_payload, manifest)
    request_items = request_envelope.items
    response_payload: Any = None
    try:
        response_payload, _ = _decode_input(response)
        response_envelope = _ResponseEnvelope.model_validate(response_payload)
    except (json.JSONDecodeError, OSError, TypeError, ValidationError) as exc:
        codes, failed_ids = _schema_failure(exc, response_payload)
        _raise_validation_failure(request_envelope, request_path, 0, failed_ids, codes)
    if not _response_is_bound(response_envelope, request_envelope):
        _raise_validation_failure(request_envelope, request_path, response_envelope.attempt, [item.item_id for item in request_items], ["response_binding_mismatch"])
    if response_envelope.attempt == 1 and not _has_bound_correction(request_path, request_envelope):
        _raise_validation_failure(request_envelope, request_path, 0, [item.item_id for item in request_items], ["response_attempt_invalid"])
    request_ids = [item.item_id for item in request_items]
    response_ids = [item.item_id for item in response_envelope.items]
    duplicate_ids = sorted(item_id for item_id, count in Counter(response_ids).items() if count > 1)
    if duplicate_ids:
        _raise_validation_failure(request_envelope, request_path, response_envelope.attempt, duplicate_ids, ["duplicate_item_id"])
    if set(request_ids) != set(response_ids):
        _raise_validation_failure(request_envelope, request_path, response_envelope.attempt, sorted(set(request_ids).symmetric_difference(response_ids)), ["item_id_set_mismatch"])
    response_by_id = {item.item_id: item for item in response_envelope.items}
    failures: dict[str, set[str]] = {}
    for request_item in request_items:
        item_codes = _validate_item(request_item, response_by_id[request_item.item_id], glossary)
        if item_codes:
            failures[request_item.item_id] = item_codes
    if failures:
        _raise_validation_failure(request_envelope, request_path, response_envelope.attempt, sorted(failures), sorted({code for codes in failures.values() for code in codes}))
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


def translation_cache_key(request: Any, glossary_sha256: str, prompt_version: str, host: str, model: str, execution_mode: str, job_id: str | None = None) -> str:
    """Hash request items and provenance without needless known-model job scoping."""
    payload, _ = _decode_input(request)
    envelope = _validated_request(payload, None)
    if glossary_sha256 != envelope.glossary_sha256:
        raise ValueError("glossary_sha256 must match the translation request")
    for field_name, value in (("prompt_version", prompt_version), ("host", host), ("model", model), ("execution_mode", execution_mode)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be blank")
    if model == "unknown" and (not isinstance(job_id, str) or job_id != envelope.job_id):
        raise ValueError("job_id must match the translation request when model is unknown")
    material: list[list[Any]] = [
        ["request", [item.model_dump(mode="json") for item in envelope.items]],
        ["glossary_sha256", glossary_sha256],
        ["prompt_version", prompt_version],
        ["host", host],
        ["model", model],
        ["execution_mode", execution_mode],
    ]
    if model == "unknown":
        material.append(["job_id", job_id])
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _validated_job(job: JobManifest) -> JobManifest:
    try:
        return JobManifest.model_validate(job)
    except (TypeError, ValidationError):
        raise TechpackError("translation_request_invalid", "Translation request job is invalid", {"error_code": "translation_request_invalid"}) from None


def _validated_request(payload: Any, job: JobManifest | None) -> _RequestEnvelope:
    try:
        envelope = _RequestEnvelope.model_validate(payload)
    except ValidationError:
        raise TechpackError("translation_request_invalid", "Translation request does not match schema 1.1", {"error_code": "translation_request_invalid"}) from None
    if envelope.request_sha256 != _request_hash(envelope.model_dump(mode="json")):
        raise TechpackError("translation_request_invalid", "Translation request integrity check failed", {"error_code": "translation_request_invalid"})
    if job is not None and (envelope.job_id != job.job_id or envelope.source_sha256 != job.source.sha256 or envelope.glossary_sha256 != job.glossary.sha256):
        raise TechpackError("translation_request_invalid", "Translation request does not match its job", {"error_code": "translation_request_invalid"})
    return envelope


def _response_is_bound(response: _ResponseEnvelope, request: _RequestEnvelope) -> bool:
    return (response.schema_version == request.schema_version and response.job_id == request.job_id and response.source_sha256 == request.source_sha256 and response.glossary_sha256 == request.glossary_sha256 and response.request_sha256 == request.request_sha256)


def _has_bound_correction(request_path: Path | None, request: _RequestEnvelope) -> bool:
    if request_path is None:
        return False
    try:
        correction = json.loads(request_path.with_name("correction-request.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(correction, dict)
        and correction.get("schema_version") == _SCHEMA_VERSION
        and correction.get("job_id") == request.job_id
        and correction.get("source_sha256") == request.source_sha256
        and correction.get("glossary_sha256") == request.glossary_sha256
        and correction.get("request_sha256") == request.request_sha256
        and correction.get("attempt") == 1
    )


def _request_hash(payload: dict[str, Any]) -> str:
    material = dict(payload)
    material.pop("request_sha256", None)
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_payload(candidate: Candidate) -> dict[str, Any]:
    terms: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for hit in candidate.glossary_hits:
        key = (hit.source_term, hit.target_term)
        if key not in seen:
            terms.append({"source_term": hit.source_term, "target_term": hit.target_term})
            seen.add(key)
    return {"item_id": candidate.item_id, "source_text": candidate.source_text, "context": candidate.source_kind, "locked_tokens": list(candidate.locked_tokens), "glossary_terms": terms, "page_type": candidate.page_type.value, "mode": "faithful_digest" if candidate.page_type.value == "sample_review" else "direct"}


def _decode_input(value: Any) -> tuple[Any, Path | None]:
    if isinstance(value, Path):
        return json.loads(value.read_text(encoding="utf-8")), value
    if isinstance(value, str):
        if value.lstrip().startswith(("[", "{")):
            return json.loads(value), None
        path = Path(value)
        return json.loads(path.read_text(encoding="utf-8")), path
    return value, None


def _schema_failure(exc: Exception, raw_response: Any) -> tuple[list[str], list[str]]:
    codes: set[str] = set()
    ids: set[str] = set()
    raw_items = raw_response.get("items", []) if isinstance(raw_response, dict) else []
    if isinstance(raw_items, list):
        for item in raw_items:
            item_id = item.get("item_id") if isinstance(item, dict) else None
            if isinstance(item_id, str) and _STABLE_ITEM_ID.fullmatch(item_id):
                ids.add(item_id)
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
    return sorted(codes), sorted(ids)


def _validate_item(request: _TranslationRequestItem, response: _TranslationResponseItem, glossary: Glossary) -> set[str]:
    codes: set[str] = set()
    if response.mode != request.mode:
        codes.add("mode_mismatch")
    locked = _locked_text_from_request(request)
    if Counter(response.preserved_tokens) != Counter(request.locked_tokens) or not validate_locked_tokens(locked, response.translated_text):
        codes.add("locked_token_mismatch")
    requested_terms = [term.source_term for term in request.glossary_terms]
    if Counter(response.glossary_terms_used) != Counter(requested_terms):
        codes.add("glossary_terms_used_mismatch")
    normalized_translation = normalize_term(response.translated_text)
    for term in request.glossary_terms:
        target = _authoritative_target(term, glossary.match(request.source_text))
        if target and normalize_term(target) not in normalized_translation:
            codes.add("glossary_target_missing")
    return codes


def _authoritative_target(term: _GlossaryTerm, hits: Sequence[GlossaryHit]) -> str:
    source = normalize_term(term.source_term)
    for hit in hits:
        if source == normalize_term(hit.source_term):
            return hit.target_term
    for hit in hits:
        if source == normalize_term(hit.matched_text):
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


def _raise_validation_failure(request: _RequestEnvelope, request_path: Path | None, attempt: int, failed_ids: Sequence[str], error_codes: Sequence[str]) -> None:
    ids = sorted(set(failed_ids or [item.item_id for item in request.items]))
    codes = sorted(set(error_codes))
    if attempt >= 1:
        raise TranslationValidationError({"status": "human_review_required"}, ids, codes) from None
    result = {"schema_version": _SCHEMA_VERSION, "job_id": request.job_id, "source_sha256": request.source_sha256, "glossary_sha256": request.glossary_sha256, "request_sha256": request.request_sha256, "attempt": 1, "failed_item_ids": ids, "error_codes": codes, "required_fixes": [_required_fix(code) for code in codes]}
    if request_path is not None:
        request_path.with_name("correction-request.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise TranslationValidationError(result, ids, codes) from None


def _required_fix(error_code: str) -> str:
    fixes = {
        "duplicate_item_id": "Return each requested item_id exactly once.",
        "empty_translation": "Return a non-empty translated_text value.",
        "glossary_target_missing": "Use every required glossary target term in translated_text.",
        "glossary_terms_used_mismatch": "Report exactly the requested source terms in glossary_terms_used.",
        "invalid_json": "Return valid JSON matching the bound translation response schema.",
        "item_id_set_mismatch": "Return exactly one item for every requested item_id and no others.",
        "locked_token_mismatch": "Preserve the exact locked token multiset without additions or changes.",
        "mode_mismatch": "Return each item using the mode specified by its request.",
        "model_missing": "Set translator.model to a non-empty identifier or unknown.",
        "response_binding_mismatch": "Return an envelope bound to the exact request and job.",
        "response_attempt_invalid": "Use attempt 0 unless responding to the bound correction request.",
        "response_schema_invalid": "Return only fields allowed by the bound translation response schema.",
        "translator_missing": "Include complete translator provenance for every item.",
    }
    return fixes.get(error_code, "Correct the invalid response item and return it again.")
