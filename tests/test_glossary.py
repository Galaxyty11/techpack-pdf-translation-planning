import csv

import pandas as pd
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.glossary import load_glossary, normalize_term


def write_glossary_csv(tmp_path, rows, filename="glossary.csv"):
    path = tmp_path / filename
    fieldnames = [
        "source_term",
        "target_term",
        "aliases",
        "category",
        "context",
        "do_not_translate",
        "priority",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_normalize_term_applies_nfkc_casefold_whitespace_and_punctuation_rules():
    assert normalize_term("  \uff24\uff2f\uff35\uff22\uff2c\uff25\u3000TOPSTITCH\u2014AT\uff0fHEM  ") == "double topstitch-at/hem"


def test_longest_match_wins_before_priority(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {"source_term": "topstitch", "target_term": "\u660e\u7ebf", "priority": 99},
        {"source_term": "double topstitch", "target_term": "\u53cc\u660e\u7ebf", "priority": 0},
    ])

    hits = load_glossary(path).match("DOUBLE TOPSTITCH AT HEM")

    assert [(hit.source_term, hit.target_term) for hit in hits] == [
        ("double topstitch", "\u53cc\u660e\u7ebf"),
    ]


def test_aliases_and_hyphen_slash_variants_match_the_canonical_term(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {
            "source_term": "top-stitch / hem",
            "target_term": "\u4e0b\u6446\u660e\u7ebf",
            "aliases": "hem topstitch|hem-topstitch",
        },
    ])

    hits = load_glossary(path).match("Use TOPSTITCH / HEM and hem topstitch.")

    assert [(hit.source_term, hit.target_term, hit.matched_text) for hit in hits] == [
        ("top-stitch / hem", "\u4e0b\u6446\u660e\u7ebf", "topstitch / hem"),
        ("top-stitch / hem", "\u4e0b\u6446\u660e\u7ebf", "hem topstitch"),
    ]


def test_english_terms_require_word_boundaries(tmp_path):
    path = write_glossary_csv(tmp_path, [{"source_term": "hem", "target_term": "\u4e0b\u6446"}])

    hits = load_glossary(path).match("hemline, theme, and HEM.")

    assert [(hit.matched_text, hit.start, hit.end) for hit in hits] == [("hem", 20, 23)]


def test_english_terms_do_not_match_inside_underscore_tokens(tmp_path):
    path = write_glossary_csv(tmp_path, [{"source_term": "hem", "target_term": "\u4e0b\u6446"}])

    assert load_glossary(path).match("hem_line") == []


def test_do_not_translate_wins_for_the_same_span_even_with_lower_priority(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {"source_term": "POM", "target_term": "\u6d4b\u91cf\u70b9", "priority": 100},
        {"source_term": "POM", "target_term": "", "do_not_translate": "true", "priority": 0},
    ])

    hits = load_glossary(path).match("POM A12")

    assert [(hit.source_term, hit.target_term, hit.do_not_translate, hit.priority) for hit in hits] == [
        ("POM", "", True, 0),
    ]


def test_priority_breaks_ties_after_matching_span_length(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {"source_term": "hem", "target_term": "\u4e0b\u6446", "priority": 1},
        {"source_term": "HEM", "target_term": "\u4e0b\u6446-\u4f18\u5148", "priority": 2},
    ])

    hits = load_glossary(path).match("Hem")

    assert [(hit.target_term, hit.priority) for hit in hits] == [("\u4e0b\u6446-\u4f18\u5148", 2)]


def test_xlsx_is_loaded_with_optional_metadata_and_defaults(tmp_path):
    path = tmp_path / "glossary.xlsx"
    pd.DataFrame([
        {"source_term": "rib", "target_term": "\u7f57\u7eb9", "category": "fabric", "priority": 3},
    ]).to_excel(path, index=False)

    glossary = load_glossary(path)

    assert [(entry.source_term, entry.category, entry.priority, entry.do_not_translate) for entry in glossary.entries] == [
        ("rib", "fabric", 3, False),
    ]


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([{"source_term": "", "target_term": "\u4e0b\u6446"}], "source_term_empty"),
        ([{"source_term": "hem", "target_term": ""}], "target_term_empty"),
        ([{"source_term": "hem", "target_term": "\u4e0b\u6446", "do_not_translate": "perhaps"}], "do_not_translate_invalid"),
        ([{"source_term": "hem", "target_term": "\u4e0b\u6446", "priority": "1.5"}], "priority_invalid"),
    ],
)
def test_invalid_row_values_raise_safe_glossary_errors(tmp_path, rows, expected_code):
    path = write_glossary_csv(tmp_path, rows)

    with pytest.raises(TechpackError) as raised:
        load_glossary(path)

    assert raised.value.code == "glossary_invalid"
    assert raised.value.details == {"row_number": 2, "error_code": expected_code}


def test_missing_required_column_raises_safe_glossary_error(tmp_path):
    path = tmp_path / "missing.csv"
    path.write_text("source_term,aliases\nhem,edge\n", encoding="utf-8")

    with pytest.raises(TechpackError) as raised:
        load_glossary(path)

    assert raised.value.code == "glossary_invalid"
    assert raised.value.details == {"row_number": 1, "error_code": "missing_required_column"}


def test_same_normalized_term_and_priority_with_distinct_translations_blocks_loading(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {"source_term": "Hem", "target_term": "\u4e0b\u6446", "priority": 5},
        {"source_term": "  HEM ", "target_term": "\u8863\u6446", "priority": 5},
    ])

    with pytest.raises(TechpackError) as raised:
        load_glossary(path)

    assert raised.value.code == "glossary_conflict"
    assert raised.value.details == {"row_number": 3, "error_code": "glossary_conflict"}
