"""Deterministic page classification, candidate selection, and token locking."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from .glossary import Glossary, GlossaryHit, normalize_term
from .matching import BBox, MatchedNode
from .models import CoordinateConfidence, DecisionReason, PageType


_TITLE_RULES: tuple[tuple[PageType, str, tuple[str, ...]], ...] = (
    (PageType.GENERAL_INFO, "style general information", ("style general information", "general information")),
    (PageType.BOM, "bill of materials", ("bill of materials", "bom")),
    (PageType.MEASUREMENT, "measurement sheet", ("measurement sheet",)),
    (
        PageType.TECHNICAL_DRAWING,
        "style additional images",
        ("style additional images", "technical drawing"),
    ),
    (PageType.LABEL_PACK, "label and pack", ("label and pack", "label pack")),
    (PageType.SAMPLE_REVIEW, "sample style review", ("sample style review", "sample review")),
    (PageType.STYLE_SAMPLE, "style sample", ("style sample", "fit page")),
    (PageType.HOW_TO_MEASURE, "how to measure", ("how to measure",)),
    (PageType.CONSTRUCTION_DETAIL, "construction detail", ("construction detail",)),
    (PageType.CATEGORY_FIELDS, "style category fields", ("style category fields", "category fields")),
)

_ADMIN_ROLES = frozenset(
    {
        "admin",
        "administrative",
        "category_field",
        "header",
        "footer",
        "header_footer",
        "page_number",
        "system_field",
        "template_title",
    }
)
_BOM_ROLES = frozenset(
    {"body", "material", "composition", "weight", "use", "component", "process_note"}
)
_MEASUREMENT_ROLES = frozenset(
    {"body", "pom_description", "measurement_instruction", "style_measurement_instruction"}
)
_TECHNICAL_ROLES = frozenset(
    {"body", "production_instruction", "callout", "construction_instruction"}
)
_LABEL_PACK_ROLES = frozenset(
    {
        "body",
        "name",
        "sequence",
        "folding_instruction",
        "position",
        "operation_instruction",
    }
)
_REVIEW_ROLES = frozenset(
    {"body", "issue", "conclusion", "exception", "action", "fit_issue", "correction", "caption"}
)
_ACTIONABLE_REVIEW_ROLES = frozenset(
    {"issue", "conclusion", "exception", "action", "fit_issue", "correction"}
)


@dataclass(frozen=True)
class AgentClassification:
    page_type: PageType
    confidence: float
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ClassificationRequest:
    reason: Literal["unknown", "conflict", "low_confidence"]
    title: str
    table_headers: tuple[str, ...]
    visual_features: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PageClassification:
    page_type: PageType
    confidence: float
    evidence: tuple[str, ...] = ()
    classification_request: ClassificationRequest | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class PageFeatures:
    title: str = ""
    table_headers: tuple[str, ...] = ()
    visual_features: tuple[str, ...] = ()
    agent_result: AgentClassification | None = None


@dataclass(frozen=True)
class LockedToken:
    value: str
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class LockedText:
    text: str
    tokens: tuple[LockedToken, ...]


@dataclass(frozen=True)
class PageNode:
    matched_node: MatchedNode
    field_role: str = "body"


@dataclass(frozen=True)
class SelectionPage:
    page_index: int
    classification: PageClassification
    nodes: tuple[PageNode, ...] = ()

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("page_index must be non-negative")


@dataclass(frozen=True)
class Candidate:
    item_id: str
    page_index: int
    page_type: PageType
    classification_confidence: float
    classification_evidence: tuple[str, ...]
    source_text: str
    normalized_text: str
    source_bbox: BBox | None
    source_kind: str
    coordinate_confidence: CoordinateConfidence
    source_auto_approvable: bool
    auto_approvable: bool
    should_translate: bool
    decision_reason: DecisionReason
    locked_text: LockedText
    glossary_hits: tuple[GlossaryHit, ...] = field(default_factory=tuple)

    @property
    def locked_tokens(self) -> tuple[str, ...]:
        return tuple(token.value for token in self.locked_text.tokens)


def classify_page(features: PageFeatures) -> PageClassification:
    """Classify title, then table, then visual structure before using an Agent result."""
    stages = (
        _title_classifications(features.title),
        _table_classifications(features.table_headers),
        _visual_classifications(features.visual_features),
    )
    conflict_evidence: tuple[str, ...] = ()
    conflict = False
    for matches, confidence in stages:
        if len(matches) == 1:
            page_type, evidence = matches[0]
            return PageClassification(page_type, confidence, (evidence,))
        if len(matches) > 1:
            conflict_evidence = tuple(evidence for _, evidence in matches)
            conflict = True
            break

    agent = features.agent_result
    if agent is not None:
        evidence = conflict_evidence + tuple(agent.evidence)
        if agent.confidence >= 0.80 and agent.page_type is not PageType.UNKNOWN and agent.evidence:
            return PageClassification(agent.page_type, agent.confidence, evidence)
        return PageClassification(
            PageType.UNKNOWN,
            agent.confidence,
            evidence,
            _classification_request(
                features,
                "low_confidence" if agent.confidence < 0.80 else "unknown",
                evidence,
            ),
        )

    reason: Literal["unknown", "conflict", "low_confidence"] = "conflict" if conflict else "unknown"
    return PageClassification(
        PageType.UNKNOWN,
        0.0,
        conflict_evidence,
        _classification_request(features, reason, conflict_evidence),
    )


def select_candidates(page: SelectionPage, glossary: Glossary) -> list[Candidate]:
    """Return one stable, reviewable selection decision per page node."""
    selected: list[Candidate] = []
    seen: set[str] = set()
    for position, page_node in enumerate(page.nodes, start=1):
        node = page_node.matched_node
        text = node.text
        normalized = normalize_term(text)
        hits = tuple(glossary.match(text))
        locked = _with_glossary_locks(lock_tokens(text), hits)
        should_translate, reason = _candidate_decision(
            page.classification,
            page_node.field_role,
            text,
            normalized,
            locked,
            hits,
            normalized in seen,
        )
        if normalized and should_translate:
            seen.add(normalized)
        selected.append(
            Candidate(
                item_id=f"p{page.page_index + 1:03d}-i{position:03d}",
                page_index=page.page_index,
                page_type=page.classification.page_type,
                classification_confidence=page.classification.confidence,
                classification_evidence=page.classification.evidence,
                source_text=text,
                normalized_text=normalized,
                source_bbox=node.source_bbox,
                source_kind=page_node.field_role,
                coordinate_confidence=node.coordinate_confidence,
                source_auto_approvable=node.auto_approvable,
                auto_approvable=(
                    should_translate
                    and page.classification.confidence >= 0.80
                    and page.classification.page_type is not PageType.UNKNOWN
                    and node.auto_approvable
                    and node.coordinate_confidence is CoordinateConfidence.HIGH
                    and node.source_bbox is not None
                ),
                should_translate=should_translate,
                decision_reason=reason,
                locked_text=locked,
                glossary_hits=hits,
            )
        )
    return selected


def lock_tokens(text: str) -> LockedText:
    """Record protected token occurrences exactly as written and in source order."""
    found: list[LockedToken] = []
    occupied: list[tuple[int, int]] = []
    for kind, pattern, group_name in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group_name) if group_name else match.span()
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            found.append(LockedToken(text[start:end], start, end, kind))
            occupied.append((start, end))
    found.sort(key=lambda token: (token.start, token.end))
    return LockedText(text=text, tokens=tuple(found))


def validate_locked_tokens(source: LockedText, translated_text: str) -> bool:
    """Compare the exact case-sensitive multiset of source and returned protected values."""
    expected = Counter(token.value for token in source.tokens)
    source_occurrences = _source_token_occurrences(source, translated_text)
    detected = [
        token
        for token in lock_tokens(translated_text).tokens
        if not any(
            _overlaps(token.start, token.end, item.start, item.end)
            for item in source_occurrences
        )
    ]
    actual = Counter(token.value for token in (*source_occurrences, *detected))
    return actual == expected


def _source_token_occurrences(source: LockedText, text: str) -> tuple[LockedToken, ...]:
    occurrences: list[LockedToken] = []
    values = {(token.value, token.kind) for token in source.tokens}
    for value, kind in sorted(values, key=lambda item: (-len(item[0]), item[0], item[1])):
        pattern = re.compile(re.escape(value))
        for match in pattern.finditer(text):
            if _is_exact_token_occurrence(text, match.start(), match.end(), value) and not any(
                _overlaps(match.start(), match.end(), token.start, token.end)
                for token in occurrences
            ):
                occurrences.append(LockedToken(value, match.start(), match.end(), kind))
    occurrences.sort(key=lambda token: (token.start, token.end))
    return tuple(occurrences)


def _is_exact_token_occurrence(text: str, start: int, end: int, value: str) -> bool:
    normalized_value = normalize_term(value)
    if _is_latin_identifier_char(normalized_value[0]):
        if start > 0 and _is_latin_identifier_char(text[start - 1]):
            return False
    if _is_latin_identifier_char(normalized_value[-1]):
        if end < len(text) and _is_latin_identifier_char(text[end]):
            return False
    return True


def _is_latin_identifier_char(character: str) -> bool:
    if character == "_" or character.isdecimal():
        return True
    return character.isalpha() and "LATIN" in unicodedata.name(character, "")


def _title_classifications(title: str) -> tuple[list[tuple[PageType, str]], float]:
    normalized = normalize_term(title)
    matches: list[tuple[PageType, str]] = []
    for page_type, canonical, aliases in _TITLE_RULES:
        if any(_contains_phrase(normalized, alias) for alias in aliases):
            matches.append((page_type, f"title:{canonical}"))
    return matches, 1.0


def _table_classifications(headers: tuple[str, ...]) -> tuple[list[tuple[PageType, str]], float]:
    normalized = {normalize_term(header) for header in headers}
    matches: list[tuple[PageType, str]] = []
    if "pom" in normalized and {"description", "tolerance"}.issubset(normalized):
        matches.append((PageType.MEASUREMENT, "table:pom+description+tolerance"))
    if "material" in normalized and len(normalized.intersection({"composition", "weight", "supplier", "component"})) >= 2:
        matches.append((PageType.BOM, "table:material+composition/weight/supplier/component"))
    return matches, 0.95


def _visual_classifications(features: tuple[str, ...]) -> tuple[list[tuple[PageType, str]], float]:
    normalized = {normalize_term(feature) for feature in features}
    matches: list[tuple[PageType, str]] = []
    if {"technical drawing", "callout arrows"}.issubset(normalized):
        matches.append((PageType.TECHNICAL_DRAWING, "visual:technical drawing+callout arrows"))
    if {"sample photos", "review annotations"}.issubset(normalized):
        matches.append((PageType.SAMPLE_REVIEW, "visual:sample photos+review annotations"))
    return matches, 0.9


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def _classification_request(
    features: PageFeatures,
    reason: Literal["unknown", "conflict", "low_confidence"],
    evidence: tuple[str, ...],
) -> ClassificationRequest:
    return ClassificationRequest(
        reason=reason,
        title=features.title,
        table_headers=features.table_headers,
        visual_features=features.visual_features,
        evidence=evidence,
    )


def _candidate_decision(
    classification: PageClassification,
    field_role: str,
    text: str,
    normalized: str,
    locked: LockedText,
    glossary_hits: tuple[GlossaryHit, ...],
    duplicate: bool,
) -> tuple[bool, DecisionReason]:
    role = normalize_term(field_role).replace(" ", "_")
    if classification.page_type is PageType.UNKNOWN or classification.confidence < 0.80:
        return False, DecisionReason.LOW_CONFIDENCE
    if not normalized or role in _ADMIN_ROLES:
        return False, DecisionReason.SKIPPED_ADMIN
    if classification.page_type in {
        PageType.HOW_TO_MEASURE,
        PageType.CATEGORY_FIELDS,
    }:
        if classification.page_type is PageType.HOW_TO_MEASURE and role == "special_measurement_instruction":
            return _translate_reason(DecisionReason.FIELD_RULE, text, locked, glossary_hits)
        return False, DecisionReason.SKIPPED_ADMIN
    if classification.page_type is PageType.GENERAL_INFO:
        if role in {"special_note", "production_note", "delivery_note"}:
            return _translate_reason(DecisionReason.FIELD_RULE, text, locked, glossary_hits)
        return False, DecisionReason.SKIPPED_ADMIN
    if _is_code_only(text, locked):
        return False, DecisionReason.SKIPPED_CODE
    if duplicate:
        return False, DecisionReason.SKIPPED_DUPLICATE

    base_reason: DecisionReason | None = None
    page_type = classification.page_type
    if page_type is PageType.BOM and role in _BOM_ROLES:
        base_reason = DecisionReason.PAGE_RULE if role == "body" else DecisionReason.FIELD_RULE
    elif page_type is PageType.MEASUREMENT and role in _MEASUREMENT_ROLES:
        base_reason = DecisionReason.PAGE_RULE if role == "body" else DecisionReason.FIELD_RULE
    elif page_type is PageType.TECHNICAL_DRAWING and role in _TECHNICAL_ROLES:
        base_reason = DecisionReason.PAGE_RULE if role == "body" else DecisionReason.FIELD_RULE
    elif page_type is PageType.LABEL_PACK and role in _LABEL_PACK_ROLES:
        base_reason = DecisionReason.PAGE_RULE if role == "body" else DecisionReason.FIELD_RULE
    elif page_type in {PageType.SAMPLE_REVIEW, PageType.STYLE_SAMPLE} and role in _REVIEW_ROLES:
        base_reason = (
            DecisionReason.ACTIONABLE_REVIEW
            if role in _ACTIONABLE_REVIEW_ROLES
            else DecisionReason.PAGE_RULE
        )
    elif page_type is PageType.CONSTRUCTION_DETAIL and role in {
        "production_instruction",
        "actionable_instruction",
    }:
        base_reason = DecisionReason.FIELD_RULE

    if base_reason is None:
        return False, DecisionReason.MANUAL_CANDIDATE
    return _translate_reason(base_reason, text, locked, glossary_hits)


def _translate_reason(
    base_reason: DecisionReason,
    text: str,
    locked: LockedText,
    glossary_hits: tuple[GlossaryHit, ...],
) -> tuple[bool, DecisionReason]:
    if glossary_hits:
        return True, DecisionReason.GLOSSARY_HIT
    if locked.tokens and not _is_code_only(text, locked):
        return True, DecisionReason.MIXED_TEXT
    return True, base_reason


def _is_code_only(text: str, locked: LockedText) -> bool:
    characters = list(text)
    for token in locked.tokens:
        characters[token.start : token.end] = " " * (token.end - token.start)
    remainder = "".join(characters)
    return not re.sub(r"[\W_]+", "", remainder, flags=re.UNICODE)


def _with_glossary_locks(locked: LockedText, hits: tuple[GlossaryHit, ...]) -> LockedText:
    tokens = list(locked.tokens)
    for hit in hits:
        if not hit.do_not_translate:
            continue
        projected = _project_normalized_span(locked.text, hit.start, hit.end)
        if projected is None:
            continue
        start, end = projected
        tokens = [token for token in tokens if not _overlaps(start, end, token.start, token.end)]
        value = locked.text[start:end]
        tokens.append(LockedToken(value, start, end, "glossary"))
    tokens.sort(key=lambda token: (token.start, token.end))
    return LockedText(locked.text, tuple(tokens))


def _project_normalized_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    normalized_length = len(normalize_term(text))
    if start < 0 or end <= start or end > normalized_length:
        return None
    prefix_lengths = [len(normalize_term(text[:index])) for index in range(len(text) + 1)]
    source_start = next(
        (index - 1 for index, length in enumerate(prefix_lengths) if length > start),
        None,
    )
    source_end = next(
        (index for index, length in enumerate(prefix_lengths) if length >= end),
        None,
    )
    while source_end is not None and source_end < len(text) and unicodedata.combining(text[source_end]):
        source_end += 1
    if source_start is None or source_end is None or source_start >= source_end:
        return None
    return source_start, source_end


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and left_end > right_start


_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    (
        "style_code",
        re.compile(
            r"\bSTYLE(?:\s*(?:NO\.?|NUMBER|#))?\s*[:#-]?\s*(?P<token>[A-Z0-9]*[A-Z][A-Z0-9._/-]*\d[A-Z0-9._/-]*)",
            re.IGNORECASE,
        ),
        "token",
    ),
    (
        "pom_code",
        re.compile(r"\bPOM\s*(?:(?:NO\.?|CODE)\s*[:#]?\s*)?[A-Z][A-Z0-9._/-]*\d[A-Z0-9._/-]*\b", re.IGNORECASE),
        None,
    ),
    (
        "material_code",
        re.compile(
            r"\b(?:MATERIAL|ARTICLE|SUPPLIER)(?:\s*(?:NO\.?|NUMBER|#))?\s*[:#-]?\s*(?P<token>[A-Z0-9]*[A-Z][A-Z0-9._/-]*\d[A-Z0-9._/-]*)",
            re.IGNORECASE,
        ),
        "token",
    ),
    (
        "date",
        re.compile(r"(?<!\w)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\w)"),
        None,
    ),
    (
        "person",
        re.compile(
            r"\b(?:DESIGNER|REVIEWER|CUSTOMER|NAME)\s*:\s*(?P<token>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3})",
        ),
        "token",
    ),
    ("color_code", re.compile(r"(?<!\w)\d{2}-\d{4}\s+TCX\b", re.IGNORECASE), None),
    (
        "color_code",
        re.compile(r"\bPANTONE(?:\s+[A-Z])?\s+\d{2}-\d{4}(?:\s+TCX)?\b", re.IGNORECASE),
        None,
    ),
    (
        "generic_code",
        re.compile(r"(?<!\w)(?=[A-Z0-9._/-]*[A-Z])(?=[A-Z0-9._/-]*\d)[A-Z0-9]+(?:[-_/][A-Z0-9.]+)*(?!\w)"),
        None,
    ),
    (
        "tolerance",
        re.compile(r"(?<!\w)(?:±\s*\d+(?:\.\d+)?|[+-]\d+(?:\.\d+)?\s*/\s*[+-]\d+(?:\.\d+)?)(?!\w)"),
        None,
    ),
    ("percentage", re.compile(r"(?<!\w)[+-]?\d+(?:\.\d+)?%(?!\w)"), None),
    ("unit", re.compile(r"(?<!\w)(?:mm|cm|inch|gsm|oz)(?!\w)", re.IGNORECASE), None),
    ("number", re.compile(r"(?<![A-Za-z0-9_])[+-]?\d+(?:\.\d+)?(?![A-Za-z0-9_])"), None),
)


__all__ = [
    "AgentClassification",
    "Candidate",
    "ClassificationRequest",
    "DecisionReason",
    "LockedText",
    "LockedToken",
    "PageClassification",
    "PageFeatures",
    "PageNode",
    "PageType",
    "SelectionPage",
    "classify_page",
    "lock_tokens",
    "select_candidates",
    "validate_locked_tokens",
]
