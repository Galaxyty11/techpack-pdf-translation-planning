"""Deterministic glossary loading, validation, and source-text matching."""

import csv
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import TechpackError


_REQUIRED_COLUMNS = frozenset({"source_term", "target_term"})
_OPTIONAL_DEFAULTS = {
    "aliases": "",
    "category": "general",
    "context": "",
    "do_not_translate": False,
    "priority": 0,
    "notes": "",
}
_BOOLEAN_VALUES = {
    "true": True,
    "yes": True,
    "y": True,
    "1": True,
    "false": False,
    "no": False,
    "n": False,
    "0": False,
}
_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "⁄": "/",
        "∕": "/",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
    }
)
_CONNECTOR = re.compile(r"\s*[-/]\s*")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class GlossaryEntry:
    """A validated glossary row, retaining reviewer-facing source values."""

    source_term: str
    target_term: str
    aliases: tuple[str, ...] = ()
    category: str = "general"
    context: str = ""
    do_not_translate: bool = False
    priority: int = 0
    notes: str = ""


@dataclass(frozen=True)
class GlossaryHit:
    """A non-overlapping glossary term selected from normalized source text."""

    source_term: str
    target_term: str
    matched_text: str
    start: int
    end: int
    do_not_translate: bool
    priority: int


@dataclass(frozen=True)
class _MatchTerm:
    entry: GlossaryEntry
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Glossary:
    """Validated entries and their compiled, deterministic matching forms."""

    entries: tuple[GlossaryEntry, ...]
    _terms: tuple[_MatchTerm, ...] = field(repr=False)

    def match(self, text: str) -> list[GlossaryHit]:
        """Return non-overlapping hits, prioritizing position, span, locks, then priority."""
        normalized_text = normalize_term(text)
        candidates: list[GlossaryHit] = []
        for term in self._terms:
            for found in term.pattern.finditer(normalized_text):
                candidates.append(
                    GlossaryHit(
                        source_term=term.entry.source_term,
                        target_term=term.entry.target_term,
                        matched_text=found.group(),
                        start=found.start(),
                        end=found.end(),
                        do_not_translate=term.entry.do_not_translate,
                        priority=term.entry.priority,
                    )
                )

        candidates.sort(
            key=lambda hit: (
                hit.start,
                -(hit.end - hit.start),
                -int(hit.do_not_translate),
                -hit.priority,
                hit.source_term.casefold(),
                hit.target_term,
            )
        )
        selected: list[GlossaryHit] = []
        for candidate in candidates:
            if all(candidate.end <= hit.start or candidate.start >= hit.end for hit in selected):
                selected.append(candidate)
        return selected


def normalize_term(text: str) -> str:
    """Canonicalize text for glossary comparisons without changing retained source values."""
    normalized = unicodedata.normalize("NFKC", str(text)).translate(_PUNCTUATION_TRANSLATION)
    return _SPACE.sub(" ", normalized).strip().casefold()


def load_glossary(path: Path) -> Glossary:
    """Load a CSV or XLSX glossary and reject malformed or ambiguous constraints."""
    glossary_path = Path(path)
    columns, rows = _read_rows(glossary_path)
    if not _REQUIRED_COLUMNS.issubset(columns):
        raise _invalid(1, "missing_required_column")

    entries: list[GlossaryEntry] = []
    seen_targets: dict[tuple[str, int, bool], str] = {}
    for row_number, row in rows:
        entry = _parse_entry(row_number, row)
        for term in (entry.source_term, *entry.aliases):
            key = (normalize_term(term), entry.priority, entry.do_not_translate)
            previous_target = seen_targets.get(key)
            if previous_target is not None and previous_target != entry.target_term:
                raise TechpackError(
                    "glossary_conflict",
                    "Glossary contains an unresolved term conflict",
                    {"row_number": row_number, "error_code": "glossary_conflict"},
                )
            seen_targets[key] = entry.target_term
        entries.append(entry)

    return Glossary(tuple(entries), _compile_terms(entries))


def _read_rows(path: Path) -> tuple[set[str], list[tuple[int, dict[str, Any]]]]:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            columns = {column for column in (reader.fieldnames or []) if column is not None}
            return columns, [(index, dict(row)) for index, row in enumerate(reader, start=2)]
    if suffix == ".xlsx":
        frame = pd.read_excel(path, engine="openpyxl", dtype=object)
        columns = {str(column) for column in frame.columns}
        return columns, [(index, dict(row)) for index, row in enumerate(frame.to_dict("records"), start=2)]
    raise _invalid(1, "unsupported_glossary_format")


def _parse_entry(row_number: int, row: dict[str, Any]) -> GlossaryEntry:
    source_term = _text(row.get("source_term"))
    if not normalize_term(source_term):
        raise _invalid(row_number, "source_term_empty")

    do_not_translate = _parse_boolean(row_number, row.get("do_not_translate", False))
    target_term = _text(row.get("target_term"))
    if not do_not_translate and not target_term.strip():
        raise _invalid(row_number, "target_term_empty")

    aliases = tuple(alias.strip() for alias in _text(row.get("aliases", "")).split("|") if alias.strip())
    return GlossaryEntry(
        source_term=source_term.strip(),
        target_term=target_term.strip(),
        aliases=aliases,
        category=_text(row.get("category", _OPTIONAL_DEFAULTS["category"])).strip() or "general",
        context=_text(row.get("context", _OPTIONAL_DEFAULTS["context"])).strip(),
        do_not_translate=do_not_translate,
        priority=_parse_priority(row_number, row.get("priority", 0)),
        notes=_text(row.get("notes", _OPTIONAL_DEFAULTS["notes"])).strip(),
    )


def _compile_terms(entries: list[GlossaryEntry]) -> tuple[_MatchTerm, ...]:
    terms: list[_MatchTerm] = []
    seen: set[tuple[str, str, int, bool]] = set()
    for entry in entries:
        for raw_term in (entry.source_term, *entry.aliases):
            normalized = normalize_term(raw_term)
            key = (normalized, entry.target_term, entry.priority, entry.do_not_translate)
            if key not in seen:
                terms.append(_MatchTerm(entry, _compile_pattern(normalized)))
                seen.add(key)
    return tuple(terms)


def _compile_pattern(normalized_term: str) -> re.Pattern[str]:
    pieces = _CONNECTOR.split(normalized_term)
    escaped_pieces = [re.escape(piece).replace(r"\ ", r"\s+") for piece in pieces]
    body = r"[\s\-/]*".join(escaped_pieces)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def _parse_boolean(row_number: int, value: Any) -> bool:
    if _is_missing(value) or (isinstance(value, str) and not value.strip()):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return bool(value)
    parsed = _BOOLEAN_VALUES.get(str(value).strip().casefold())
    if parsed is None:
        raise _invalid(row_number, "do_not_translate_invalid")
    return parsed


def _parse_priority(row_number: int, value: Any) -> int:
    if _is_missing(value) or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, bool):
        raise _invalid(row_number, "priority_invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise _invalid(row_number, "priority_invalid")
    normalized = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", normalized):
        return int(normalized)
    raise _invalid(row_number, "priority_invalid")


def _text(value: Any) -> str:
    return "" if _is_missing(value) else str(value)


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _invalid(row_number: int, error_code: str) -> TechpackError:
    return TechpackError(
        "glossary_invalid",
        "Glossary contains invalid data",
        {"row_number": row_number, "error_code": error_code},
    )
