from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKTREE = ROOT / ".worktrees" / "figure-annotation-coverage"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(WORKTREE / "scripts"))

import test_selection as fixtures  # noqa: E402
from techpack_pdf.models import PageType  # noqa: E402
from techpack_pdf.review import _PAGE_TYPE_LABELS  # noqa: E402
from techpack_pdf.selection import PageFeatures, classify_page, select_candidates  # noqa: E402


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("CAD Drawing", "technical_drawing"),
        ("BOM - Partial Colorways", "bom"),
        ("Size Chart", "measurement"),
        ("Customer Comments", "sample_review"),
        ("Print Artwork", "print_artwork"),
        ("BOM - All Colorways", "bom"),
        ("Packaging Information", "label_pack"),
    ],
)
def test_business_required_titles_classify_deterministically(
    title: str, expected: str
) -> None:
    result = classify_page(PageFeatures(title=title))

    assert result.page_type.value == expected
    assert result.confidence == 1.0
    assert result.classification_request is None


@pytest.mark.parametrize(
    "role",
    ["body", "production_instruction", "placement", "color_note", "artwork_text"],
)
def test_print_artwork_natural_language_is_selected(role: str) -> None:
    page = fixtures._page(
        PageType("print_artwork"),
        [fixtures._page_node("Place floral print at center front", role)],
    )

    candidates = select_candidates(page, fixtures._empty_glossary())

    assert len(candidates) == 1
    assert candidates[0].should_translate is True


def test_print_artwork_has_business_facing_review_label() -> None:
    assert _PAGE_TYPE_LABELS["print_artwork"] == "印花图稿"


@pytest.mark.parametrize("role", ["trademark", "brand", "logo", "nonlinguistic_graphic"])
def test_print_artwork_explicit_brand_and_graphic_roles_are_not_translated(role: str) -> None:
    page = fixtures._page(
        PageType("print_artwork"),
        [fixtures._page_node("NIKE", role)],
    )

    candidate = select_candidates(page, fixtures._empty_glossary())[0]

    assert candidate.should_translate is False
    assert candidate.decision_reason.value == "skipped_code"


def test_print_artwork_pure_dimension_is_not_translated() -> None:
    page = fixtures._page(
        PageType("print_artwork"),
        [fixtures._page_node("10 x 20 cm", "body")],
    )

    candidate = select_candidates(page, fixtures._empty_glossary())[0]

    assert candidate.should_translate is False
    assert candidate.decision_reason.value == "skipped_code"


def test_skill_records_the_business_scope_and_colorway_coverage() -> None:
    skill = (WORKTREE / "SKILL.md").read_text(encoding="utf-8")

    for phrase in (
        "CAD technical drawings",
        "partial color groups",
        "measurement tables",
        "customer comments",
        "print artwork",
        "all color groups",
        "packaging information",
    ):
        assert phrase in skill
    assert "Inspect every color-group block on each BOM page" in skill
