"""Bound, deterministic translation exchange contracts for host Agents."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import TechpackError
from .glossary import Glossary, GlossaryHit, normalize_term
from .models import JobManifest
from .selection import Candidate, LockedText, LockedToken, validate_locked_tokens


TranslationMode = Literal["direct", "faithful_digest"]
_SCHEMA_VERSION = "1.1"
_STABLE_ITEM_ID = re.compile(r"^p[0-9]{3}-i[0-9]{3,}$")
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
    agent_role: str | None
    prompt_version: str = Field(min_length=1)

    @field_validator("host", "prompt_version")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def model_is_normalized(cls, value: str) -> str:
        return _normalize_model_identifier(value)

    @field_validator("agent_role")
    @classmethod
    def agent_role_is_normalized(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("agent_role must be nonblank and have no surrounding whitespace")
        return value

    @model_validator(mode="after")
    def delegated_execution_has_role(self) -> "Translator":
        if self.execution_mode in {"subagent", "mixed"} and self.agent_role is None:
            raise ValueError("delegated execution requires agent_role")
        return self


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
    try:
        request_path = Path(path)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, TypeError, UnicodeError, ValueError):
        raise TechpackError("translation_request_write_failed", "Translation request could not be written", {"error_code": "translation_request_write_failed"}) from None
    return serialized


def validate_translation_response(request: Any, response: Any, glossary: Glossary, job: JobManifest, *, expected_attempt: Literal[0, 1] = 0) -> list[ValidatedTranslation]:
    """Validate a response envelope against both a trusted request and manifest."""
    if expected_attempt not in (0, 1):
        raise ValueError("expected_attempt must be 0 or 1")
    manifest = _validated_job(job)
    try:
        request_payload, request_path = _decode_input(request)
    except _ExchangeInputError:
        raise TechpackError("translation_request_invalid", "Translation request could not be read", {"error_code": "translation_request_invalid"}) from None
    request_envelope = _validated_request(request_payload, manifest)
    request_items = request_envelope.items
    response_payload: Any = None
    try:
        response_payload, _ = _decode_input(response)
        response_envelope = _ResponseEnvelope.model_validate(response_payload)
    except _ExchangeInputError as exc:
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _trusted_item_ids(request_envelope), ["invalid_json" if exc.kind == "json" else "response_schema_invalid"])
    except ValidationError as exc:
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _trusted_item_ids(request_envelope), _schema_failure(exc))
    if response_envelope.attempt != expected_attempt:
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _trusted_item_ids(request_envelope), ["response_attempt_invalid"])
    if not _response_is_bound(response_envelope, request_envelope):
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _trusted_item_ids(request_envelope), ["response_binding_mismatch"])
    request_ids = [item.item_id for item in request_items]
    response_ids = [item.item_id for item in response_envelope.items]
    duplicate_ids = sorted(item_id for item_id, count in Counter(response_ids).items() if count > 1)
    if duplicate_ids:
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _missing_or_all_trusted_ids(request_envelope, response_ids), ["duplicate_item_id"])
    if set(request_ids) != set(response_ids):
        _raise_validation_failure(request_envelope, request_path, expected_attempt, _missing_or_all_trusted_ids(request_envelope, response_ids), ["item_id_set_mismatch"])
    response_by_id = {item.item_id: item for item in response_envelope.items}
    failures: dict[str, set[str]] = {}
    for request_item in request_items:
        item_codes = _validate_item(request_item, response_by_id[request_item.item_id], glossary)
        if item_codes:
            failures[request_item.item_id] = item_codes
    if failures:
        _raise_validation_failure(request_envelope, request_path, expected_attempt, sorted(failures), sorted({code for codes in failures.values() for code in codes}))
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
    try:
        payload, _ = _decode_input(request)
    except _ExchangeInputError:
        raise TechpackError("translation_request_invalid", "Translation request could not be read", {"error_code": "translation_request_invalid"}) from None
    envelope = _validated_request(payload, None)
    if glossary_sha256 != envelope.glossary_sha256:
        raise ValueError("glossary_sha256 must match the translation request")
    for field_name, value in (("prompt_version", prompt_version), ("host", host), ("execution_mode", execution_mode)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be blank")
    try:
        normalized_model = _normalize_model_identifier(model)
    except (TypeError, ValueError):
        raise ValueError("model must be nonblank and have no leading or trailing whitespace") from None
    if normalized_model == "unknown" and (not isinstance(job_id, str) or job_id != envelope.job_id):
        raise ValueError("job_id must match the translation request when model is unknown")
    material: list[list[Any]] = [
        ["request", [item.model_dump(mode="json") for item in envelope.items]],
        ["glossary_sha256", glossary_sha256],
        ["prompt_version", prompt_version],
        ["host", host],
        ["model", normalized_model],
        ["execution_mode", execution_mode],
    ]
    if normalized_model == "unknown":
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


class _ExchangeInputError(Exception):
    def __init__(self, kind: Literal["io", "json"]):
        self.kind = kind


def _decode_input(value: Any) -> tuple[Any, Path | None]:
    try:
        if isinstance(value, Path):
            return json.loads(value.read_text(encoding="utf-8")), value
        if isinstance(value, str):
            if value.lstrip().startswith(("[", "{")):
                return json.loads(value), None
            path = Path(value)
            return json.loads(path.read_text(encoding="utf-8")), path
        return value, None
    except json.JSONDecodeError:
        raise _ExchangeInputError("json") from None
    except (OSError, TypeError, UnicodeError, ValueError):
        raise _ExchangeInputError("io") from None


def _schema_failure(exc: ValidationError) -> list[str]:
    codes: set[str] = set()
    for error in exc.errors():
        location = tuple(str(part) for part in error.get("loc", ()))
        if "translated_text" in location:
            codes.add("empty_translation")
        elif "translator" in location and "model" in location:
            codes.add("model_missing")
        elif "translator" in location:
            codes.add("translator_missing")
        else:
            codes.add("response_schema_invalid")
    return sorted(codes)


def _validate_item(request: _TranslationRequestItem, response: _TranslationResponseItem, glossary: Glossary) -> set[str]:
    codes: set[str] = set()
    if response.mode != request.mode:
        codes.add("mode_mismatch")
    locked = _locked_text_from_request(request)
    if response.preserved_tokens != request.locked_tokens or not validate_locked_tokens(locked, response.translated_text):
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


def _trusted_item_ids(request: _RequestEnvelope) -> list[str]:
    return sorted({item.item_id for item in request.items})


def _missing_or_all_trusted_ids(request: _RequestEnvelope, response_ids: Sequence[str]) -> list[str]:
    missing = sorted(set(_trusted_item_ids(request)).difference(response_ids))
    return missing or _trusted_item_ids(request)


def _raise_validation_failure(request: _RequestEnvelope, request_path: Path | None, expected_attempt: Literal[0, 1], failed_ids: Sequence[str], error_codes: Sequence[str]) -> None:
    trusted_ids = set(_trusted_item_ids(request))
    ids = sorted(set(failed_ids).intersection(trusted_ids) or trusted_ids)
    codes = sorted(set(error_codes))
    if expected_attempt == 1:
        raise TranslationValidationError({"status": "human_review_required"}, ids, codes) from None
    result = {"schema_version": _SCHEMA_VERSION, "job_id": request.job_id, "source_sha256": request.source_sha256, "glossary_sha256": request.glossary_sha256, "request_sha256": request.request_sha256, "attempt": 1, "failed_item_ids": ids, "error_codes": codes, "required_fixes": [_required_fix(code) for code in codes]}
    if request_path is not None:
        try:
            request_path.with_name("correction-request.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, TypeError, UnicodeError, ValueError):
            raise TechpackError("translation_correction_write_failed", "Translation correction request could not be written", {"error_code": "translation_correction_write_failed"}) from None
    raise TranslationValidationError(result, ids, codes) from None


def _required_fix(error_code: str) -> str:
    fixes = {
        "duplicate_item_id": "Return each requested item_id exactly once.",
        "empty_translation": "Return a non-empty translated_text value.",
        "glossary_target_missing": "Use every required glossary target term in translated_text.",
        "glossary_terms_used_mismatch": "Report exactly the requested source terms in glossary_terms_used.",
        "invalid_json": "Return valid JSON matching the bound translation response schema.",
        "item_id_set_mismatch": "Return exactly one item for every requested item_id and no others.",
        "locked_token_mismatch": "Preserve the exact locked token sequence without reordering, additions, or changes.",
        "mode_mismatch": "Return each item using the mode specified by its request.",
        "model_missing": "Set translator.model to a non-empty identifier or unknown.",
        "response_binding_mismatch": "Return an envelope bound to the exact request and job.",
        "response_attempt_invalid": "Use attempt 0 unless responding to the bound correction request.",
        "response_schema_invalid": "Return only fields allowed by the bound translation response schema.",
        "translator_missing": "Include complete translator provenance for every item.",
    }
    return fixes.get(error_code, "Correct the invalid response item and return it again.")


def _normalize_model_identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("model must be nonblank and have no leading or trailing whitespace")
    return "unknown" if value.casefold() == "unknown" else value
