from __future__ import annotations

import csv

import pytest

from techpack_pdf.selection import (
    AgentClassification,
    DecisionReason,
    PageClassification,
    PageFeatures,
    PageNode,
    PageType,
    SelectionPage,
    classify_page,
    lock_tokens,
    select_candidates,
    validate_locked_tokens,
)
from techpack_pdf.glossary import Glossary, load_glossary
from techpack_pdf.matching import MatchedNode
from techpack_pdf.models import CoordinateConfidence


@pytest.mark.parametrize(
    ("title", "expected_type", "expected_evidence"),
    [
        ("Style General Information", "general_info", "title:style general information"),
        ("Bill of Materials", "bom", "title:bill of materials"),
        ("Measurement Sheet", "measurement", "title:measurement sheet"),
        ("Style Additional Images", "technical_drawing", "title:style additional images"),
        ("Label and Pack", "label_pack", "title:label and pack"),
        ("Sample Style Review", "sample_review", "title:sample style review"),
        ("Style Sample", "style_sample", "title:style sample"),
        ("How To Measure", "how_to_measure", "title:how to measure"),
        ("Construction Detail", "construction_detail", "title:construction detail"),
        ("Style Category Fields", "category_fields", "title:style category fields"),
    ],
)
def test_title_rules_classify_all_ten_concrete_page_types(
    title: str, expected_type: str, expected_evidence: str
) -> None:
    result = classify_page(PageFeatures(title=title))

    assert (
        result.page_type.value,
        result.confidence,
        result.evidence,
        result.classification_request,
    ) == (expected_type, 1.0, (expected_evidence,), None)


def test_classification_uses_title_then_table_then_visual_structure() -> None:
    title_wins = classify_page(
        PageFeatures(
            title="Style General Information",
            table_headers=("Material", "Composition", "Weight", "Supplier"),
            visual_features=("technical drawing", "callout arrows"),
        )
    )
    table_wins = classify_page(
        PageFeatures(
            table_headers=("POM", "Description", "Tolerance", "XS", "S", "M"),
            visual_features=("technical drawing", "callout arrows"),
        )
    )
    visual_wins = classify_page(
        PageFeatures(visual_features=("technical drawing", "callout arrows"))
    )

    assert (title_wins.page_type, title_wins.confidence, title_wins.evidence) == (
        PageType.GENERAL_INFO,
        1.0,
        ("title:style general information",),
    )
    assert (table_wins.page_type, table_wins.confidence, table_wins.evidence) == (
        PageType.MEASUREMENT,
        0.95,
        ("table:pom+description+tolerance",),
    )
    assert (visual_wins.page_type, visual_wins.confidence, visual_wins.evidence) == (
        PageType.TECHNICAL_DRAWING,
        0.9,
        ("visual:technical drawing+callout arrows",),
    )


def test_unknown_and_conflicting_features_create_classification_requests() -> None:
    unknown = classify_page(PageFeatures())
    conflict = classify_page(PageFeatures(title="BOM Measurement Sheet"))

    assert (unknown.page_type, unknown.confidence, unknown.evidence) == (
        PageType.UNKNOWN,
        0.0,
        (),
    )
    assert unknown.classification_request is not None
    assert unknown.classification_request.reason == "unknown"
    assert (conflict.page_type, conflict.confidence) == (PageType.UNKNOWN, 0.0)
    assert conflict.evidence == (
        "title:bill of materials",
        "title:measurement sheet",
    )
    assert conflict.classification_request is not None
    assert conflict.classification_request.reason == "conflict"


def test_agent_classification_below_point_eight_remains_unknown_with_evidence() -> None:
    low = classify_page(
        PageFeatures(
            agent_result=AgentClassification(
                page_type=PageType.BOM,
                confidence=0.79,
                evidence=("agent:material table",),
            )
        )
    )
    accepted = classify_page(
        PageFeatures(
            agent_result=AgentClassification(
                page_type=PageType.BOM,
                confidence=0.80,
                evidence=("agent:material table",),
            )
        )
    )

    assert (low.page_type, low.confidence, low.evidence) == (
        PageType.UNKNOWN,
        0.79,
        ("agent:material table",),
    )
    assert low.classification_request is not None
    assert low.classification_request.reason == "low_confidence"
    assert (accepted.page_type, accepted.confidence, accepted.evidence) == (
        PageType.BOM,
        0.80,
        ("agent:material table",),
    )
    assert accepted.classification_request is None


@pytest.mark.parametrize(
    ("page_type", "field_role", "text", "expected_reason"),
    [
        ("bom", "material", "Shell fabric", "field_rule"),
        ("measurement", "pom_description", "Back neck width", "field_rule"),
        ("technical_drawing", "production_instruction", "Double stitch at hem", "field_rule"),
        ("sample_review", "action", "Reduce sleeve length", "actionable_review"),
        ("style_sample", "fit_issue", "Collar stands away from neck", "actionable_review"),
        ("label_pack", "folding_instruction", "Fold sleeves to back", "field_rule"),
    ],
)
def test_page_and_field_rules_select_translatable_candidates(
    page_type: str, field_role: str, text: str, expected_reason: str
) -> None:
    page = _page(PageType(page_type), [_page_node(text, field_role)])

    candidates = select_candidates(page, _empty_glossary())

    assert [(candidate.should_translate, candidate.decision_reason.value) for candidate in candidates] == [
        (True, expected_reason)
    ]


@pytest.mark.parametrize(
    ("page_type", "field_role", "text", "expected_reason"),
    [
        ("general_info", "admin", "Designer: Jane Doe", "skipped_admin"),
        ("how_to_measure", "generic_instruction", "Measure straight across", "skipped_admin"),
        ("category_fields", "category_field", "Product category", "skipped_admin"),
        ("bom", "header_footer", "Confidential - page 2", "skipped_admin"),
        ("bom", "admin", "System status approved", "skipped_admin"),
        ("bom", "material", "MAT-1007", "skipped_code"),
    ],
)
def test_skip_rules_are_deterministic(
    page_type: str, field_role: str, text: str, expected_reason: str
) -> None:
    candidates = select_candidates(
        _page(PageType(page_type), [_page_node(text, field_role)]),
        _empty_glossary(),
    )

    assert [(candidate.should_translate, candidate.decision_reason.value) for candidate in candidates] == [
        (False, expected_reason)
    ]


def test_candidate_ids_and_duplicate_decisions_are_stable_in_page_order() -> None:
    page = _page(
        PageType.BOM,
        [
            _page_node("Shell fabric", "material"),
            _page_node("Zipper", "component"),
            _page_node("Shell fabric", "material"),
        ],
        page_index=4,
    )

    first = select_candidates(page, _empty_glossary())
    second = select_candidates(page, _empty_glossary())

    expected = [
        ("p005-i001", True, "field_rule"),
        ("p005-i002", True, "field_rule"),
        ("p005-i003", False, "skipped_duplicate"),
    ]
    assert [(item.item_id, item.should_translate, item.decision_reason.value) for item in first] == expected
    assert [(item.item_id, item.should_translate, item.decision_reason.value) for item in second] == expected


def test_skipped_node_does_not_suppress_a_later_eligible_duplicate() -> None:
    page = _page(
        PageType.BOM,
        [
            _page_node("Shell fabric", "header_footer"),
            _page_node("Shell fabric", "material"),
        ],
    )

    candidates = select_candidates(page, _empty_glossary())

    assert [(item.should_translate, item.decision_reason.value) for item in candidates] == [
        (False, "skipped_admin"),
        (True, "field_rule"),
    ]


@pytest.mark.parametrize(
    ("field_role", "text", "expected_reason"),
    [
        ("body", "Shell fabric", "page_rule"),
        ("material", "Use 12 mm elastic", "mixed_text"),
        ("marketing_copy", "Seasonal inspiration", "manual_candidate"),
    ],
)
def test_candidate_decision_reasons_are_exercised_by_real_selection_behavior(
    field_role: str,
    text: str,
    expected_reason: str,
) -> None:
    candidate = select_candidates(
        _page(PageType.BOM, [_page_node(text, field_role)]),
        _empty_glossary(),
    )[0]

    assert candidate.decision_reason.value == expected_reason
    assert candidate.should_translate is (expected_reason != "manual_candidate")


def test_candidate_low_confidence_reason_uses_page_classification_confidence() -> None:
    page = SelectionPage(
        page_index=0,
        classification=PageClassification(
            page_type=PageType.BOM,
            confidence=0.79,
            evidence=("agent:possible material table",),
        ),
        nodes=(_page_node("Shell fabric", "material"),),
    )

    candidate = select_candidates(page, _empty_glossary())[0]

    assert (candidate.should_translate, candidate.decision_reason.value) == (
        False,
        "low_confidence",
    )
    assert (candidate.classification_confidence, candidate.classification_evidence) == (
        0.79,
        ("agent:possible material table",),
    )
    assert candidate.auto_approvable is False


def test_candidate_retains_classification_and_source_coordinate_provenance() -> None:
    page = SelectionPage(
        page_index=2,
        classification=PageClassification(
            page_type=PageType.BOM,
            confidence=0.95,
            evidence=("table:material+composition", "visual:dense material table"),
        ),
        nodes=(_page_node("Shell fabric", "material"),),
    )

    candidate = select_candidates(page, _empty_glossary())[0]

    assert (
        candidate.classification_confidence,
        candidate.classification_evidence,
        candidate.coordinate_confidence,
        candidate.source_auto_approvable,
        candidate.auto_approvable,
    ) == (
        0.95,
        ("table:material+composition", "visual:dense material table"),
        CoordinateConfidence.HIGH,
        True,
        True,
    )


def test_low_coordinate_confidence_blocks_candidate_auto_approval() -> None:
    page = _page(
        PageType.BOM,
        [
            _page_node(
                "Shell fabric",
                "material",
                coordinate_confidence=CoordinateConfidence.LOW,
                auto_approvable=True,
            )
        ],
    )

    candidate = select_candidates(page, _empty_glossary())[0]

    assert candidate.source_auto_approvable is True
    assert candidate.coordinate_confidence is CoordinateConfidence.LOW
    assert candidate.auto_approvable is False


def test_glossary_hits_are_retained_and_do_not_translate_hits_are_locked(tmp_path) -> None:
    glossary_path = tmp_path / "glossary.csv"
    with glossary_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("source_term", "target_term", "do_not_translate"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "source_term": "AcmeTex",
                "target_term": "",
                "do_not_translate": "true",
            }
        )

    candidate = select_candidates(
        _page(PageType.BOM, [_page_node("Shell AcmeTex 220 gsm", "material")]),
        load_glossary(glossary_path),
    )[0]

    assert candidate.decision_reason is DecisionReason.GLOSSARY_HIT
    assert [(hit.source_term, hit.do_not_translate) for hit in candidate.glossary_hits] == [
        ("AcmeTex", True)
    ]
    assert [(token.value, token.kind) for token in candidate.locked_text.tokens] == [
        ("AcmeTex", "glossary"),
        ("220", "number"),
        ("gsm", "unit"),
    ]
    assert validate_locked_tokens(candidate.locked_text, "大身 AcmeTex 220 gsm") is True


def test_glossary_lock_offsets_are_projected_back_to_exact_source_text(tmp_path) -> None:
    glossary_path = tmp_path / "glossary.csv"
    glossary_path.write_text(
        "source_term,target_term,do_not_translate\nAcmeTex,,true\n",
        encoding="utf-8",
    )
    source = "  Shell  ＡｃｍｅＴｅｘ 220 gsm"

    candidate = select_candidates(
        _page(PageType.BOM, [_page_node(source, "material")]),
        load_glossary(glossary_path),
    )[0]

    assert [(token.value, token.kind) for token in candidate.locked_text.tokens] == [
        ("ＡｃｍｅＴｅｘ", "glossary"),
        ("220", "number"),
        ("gsm", "unit"),
    ]


def test_lock_tokens_records_exact_order_offsets_and_all_protected_kinds() -> None:
    text = (
        "STYLE ST-2048; POM A12; MATERIAL MAT-7788; DATE 2026-08-22; "
        "DESIGNER: Jane Doe; 19-4052 TCX; Pantone 18-1664; qty 12; "
        "width 0.6 cm; ratio 95%; tol ±0.5 mm and +1/-1 inch; 220 gsm; 2.5 oz"
    )

    locked = lock_tokens(text)

    expected = [
        ("ST-2048", "style_code"),
        ("POM A12", "pom_code"),
        ("MAT-7788", "material_code"),
        ("2026-08-22", "date"),
        ("Jane Doe", "person"),
        ("19-4052 TCX", "color_code"),
        ("Pantone 18-1664", "color_code"),
        ("12", "number"),
        ("0.6", "number"),
        ("cm", "unit"),
        ("95%", "percentage"),
        ("±0.5", "tolerance"),
        ("mm", "unit"),
        ("+1/-1", "tolerance"),
        ("inch", "unit"),
        ("220", "number"),
        ("gsm", "unit"),
        ("2.5", "number"),
        ("oz", "unit"),
    ]
    assert [(token.value, token.kind) for token in locked.tokens] == expected
    assert [text[token.start : token.end] for token in locked.tokens] == [value for value, _ in expected]
    assert [token.start for token in locked.tokens] == sorted(token.start for token in locked.tokens)


def test_locked_token_validation_compares_exact_multisets_without_conversion() -> None:
    source = lock_tokens("ALLOW 0.6 cm TWICE: 0.6 cm")

    assert validate_locked_tokens(source, "允许 0.6 cm 两次：0.6 cm") is True
    assert validate_locked_tokens(source, "允许 6 mm 两次：6 mm") is False
    assert validate_locked_tokens(source, "允许 0.6 cm") is False
    assert validate_locked_tokens(source, "允许 0.6 cm 两次：0.6 cm 另加 1 mm") is False


def test_locked_person_remains_valid_when_the_surrounding_label_is_translated() -> None:
    source = lock_tokens("DESIGNER: Jane Doe; tolerance 0.6 cm")

    assert validate_locked_tokens(source, "设计师：Jane Doe；公差 0.6 cm") is True


def test_locked_token_validation_does_not_count_tokens_inside_longer_tokens() -> None:
    source = lock_tokens("1 11")

    assert [(token.value, token.kind) for token in source.tokens] == [
        ("1", "number"),
        ("11", "number"),
    ]
    assert validate_locked_tokens(source, "1 11") is True
    assert validate_locked_tokens(source, "1 12") is False


def test_locked_numeric_occurrences_allow_cjk_adjacency_without_identifier_substrings() -> None:
    source = lock_tokens("12 12")

    assert validate_locked_tokens(source, "数量12件和12件") is True
    assert validate_locked_tokens(source, "数量12件") is False
    assert validate_locked_tokens(source, "数量12件和13件") is False
    assert validate_locked_tokens(source, "数量A12B件和12件") is False
    assert validate_locked_tokens(source, "数量112件和12件") is False


def test_dnt_occurrences_allow_cjk_adjacency_but_remain_case_and_identifier_exact(tmp_path) -> None:
    glossary_path = tmp_path / "glossary.csv"
    glossary_path.write_text(
        "source_term,target_term,do_not_translate\nAcmeTex,,true\n",
        encoding="utf-8",
    )
    candidate = select_candidates(
        _page(PageType.BOM, [_page_node("Use AcmeTex fabric", "material")]),
        load_glossary(glossary_path),
    )[0]

    assert validate_locked_tokens(candidate.locked_text, "使用AcmeTex面料") is True
    assert validate_locked_tokens(candidate.locked_text, "使用acmetex面料") is False
    assert validate_locked_tokens(candidate.locked_text, "使用XAcmeTexY面料") is False
    assert validate_locked_tokens(candidate.locked_text, "使用AcmeTexPlus面料") is False
    assert validate_locked_tokens(candidate.locked_text, "使用AcmeTex面料和AcmeTex里料") is False


@pytest.mark.parametrize(
    ("text", "expected_value", "expected_kind"),
    [
        ("款式STYLE ST-2048面料", "ST-2048", "style_code"),
        ("测量POM A12说明", "POM A12", "pom_code"),
        ("物料MATERIAL MAT-7788说明", "MAT-7788", "material_code"),
        ("日期2026-08-22确认", "2026-08-22", "date"),
        ("人员DESIGNER: Jane Doe确认", "Jane Doe", "person"),
        ("颜色19-4052 TCX确认", "19-4052 TCX", "color_code"),
        ("使用ABC123面料", "ABC123", "generic_code"),
        ("公差±0.5范围", "±0.5", "tolerance"),
        ("增长50%幅度", "50%", "percentage"),
        ("长度cm宽度", "cm", "unit"),
        ("数量12件", "12", "number"),
    ],
)
def test_all_lexical_protection_patterns_allow_cjk_adjacency(
    text: str,
    expected_value: str,
    expected_kind: str,
) -> None:
    locked = lock_tokens(text)

    assert [(token.value, token.kind) for token in locked.tokens] == [
        (expected_value, expected_kind),
    ]
    assert text[locked.tokens[0].start : locked.tokens[0].end] == expected_value


def test_lexical_patterns_do_not_extract_inside_latin_digit_identifiers() -> None:
    locked = lock_tokens("prefixcmSuffix XABC123Y A50%B écm中文 éABC123中文")
    values = [token.value for token in locked.tokens]

    assert "cm" not in values
    assert "ABC123" not in values
    assert "50%" not in values


@pytest.mark.parametrize(
    ("text", "forbidden_value"),
    [
        ("e\u0301cm", "cm"),
        ("e\u0301ABC123", "ABC123"),
        ("cm\u0301", "cm"),
        ("ABC123\u0301", "ABC123"),
    ],
)
def test_lexical_tokens_do_not_split_decomposed_latin_graphemes(
    text: str,
    forbidden_value: str,
) -> None:
    values = [token.value for token in lock_tokens(text).tokens]

    assert forbidden_value not in values


def test_exact_locked_value_rejects_a_trailing_combining_mark() -> None:
    source = lock_tokens("cm")

    assert validate_locked_tokens(source, "长度cm\u0301宽度") is False
    assert validate_locked_tokens(source, "长度cm宽度") is True


@pytest.mark.parametrize(
    ("text", "forbidden_value"),
    [
        ("éSTYLE ST-2048", "ST-2048"),
        ("e\u0301STYLE ST-2048", "ST-2048"),
        ("éMATERIAL MAT-7788", "MAT-7788"),
        ("e\u0301MATERIAL MAT-7788", "MAT-7788"),
        ("éDESIGNER: Jane Doe", "Jane Doe"),
        ("e\u0301DESIGNER: Jane Doe", "Jane Doe"),
    ],
)
def test_named_group_tokens_require_the_full_marker_match_to_be_boundary_safe(
    text: str,
    forbidden_value: str,
) -> None:
    values = [token.value for token in lock_tokens(text).tokens]

    assert forbidden_value not in values


def test_marked_person_exact_value_allows_cjk_adjacency_during_validation() -> None:
    source = lock_tokens("DESIGNER: Jane Doe")

    assert validate_locked_tokens(source, "审核人Jane Doe确认") is True


def test_longer_detected_percentage_overrides_shorter_source_number_occurrence() -> None:
    source = lock_tokens("50")

    assert validate_locked_tokens(source, "增长50%幅度") is False


def test_do_not_translate_glossary_span_supersedes_overlapping_numeric_locks(tmp_path) -> None:
    glossary_path = tmp_path / "glossary.csv"
    glossary_path.write_text(
        "source_term,target_term,do_not_translate\nAcmeTex 220 gsm,,true\n",
        encoding="utf-8",
    )

    candidate = select_candidates(
        _page(PageType.BOM, [_page_node("Use AcmeTex 220 gsm at body", "material")]),
        load_glossary(glossary_path),
    )[0]

    assert [(token.value, token.kind) for token in candidate.locked_text.tokens] == [
        ("AcmeTex 220 gsm", "glossary"),
    ]
    assert validate_locked_tokens(candidate.locked_text, "大身使用 AcmeTex 220 gsm") is True
    assert validate_locked_tokens(candidate.locked_text, "大身使用 AcmeTex 220 GSM") is False


def test_undelimited_uppercase_alphanumeric_codes_are_locked() -> None:
    locked = lock_tokens("Use ABC123 and A12")

    assert [(token.value, token.kind) for token in locked.tokens] == [
        ("ABC123", "generic_code"),
        ("A12", "generic_code"),
    ]


def test_undelimited_code_matching_respects_case_and_word_boundaries() -> None:
    locked = lock_tokens("preABC123post abc123 version2 123ABCdef")

    assert locked.tokens == ()


def test_glossary_projection_keeps_combining_marks_in_the_exact_locked_span(tmp_path) -> None:
    glossary_path = tmp_path / "glossary.csv"
    glossary_path.write_text(
        "source_term,target_term,do_not_translate\ncafé,,true\n",
        encoding="utf-8",
    )
    source = "Use Cafe\u0301 fabric"

    candidate = select_candidates(
        _page(PageType.BOM, [_page_node(source, "material")]),
        load_glossary(glossary_path),
    )[0]

    assert [(token.value, token.kind) for token in candidate.locked_text.tokens] == [
        ("Cafe\u0301", "glossary"),
    ]
    assert source[candidate.locked_text.tokens[0].start : candidate.locked_text.tokens[0].end] == "Cafe\u0301"
    assert validate_locked_tokens(candidate.locked_text, "使用 Cafe\u0301ine 面料") is False


def _empty_glossary() -> Glossary:
    return Glossary(entries=(), _terms=())


def _page(
    page_type: PageType,
    nodes: list[PageNode],
    *,
    page_index: int = 0,
) -> SelectionPage:
    return SelectionPage(
        page_index=page_index,
        classification=PageClassification(
            page_type=page_type,
            confidence=1.0,
            evidence=(f"test:{page_type.value}",),
        ),
        nodes=tuple(nodes),
    )


def _page_node(
    text: str,
    field_role: str,
    *,
    coordinate_confidence: CoordinateConfidence = CoordinateConfidence.HIGH,
    auto_approvable: bool = True,
) -> PageNode:
    return PageNode(
        matched_node=MatchedNode(
            mineru_index=0,
            native_index=0,
            text=text,
            source_bbox=(10.0, 10.0, 90.0, 20.0),
            mineru_bbox=(10.0, 10.0, 90.0, 20.0),
            coordinate_confidence=coordinate_confidence,
            similarity=1.0,
            distance_ratio=0.0,
            auto_approvable=auto_approvable,
        ),
        field_role=field_role,
    )
