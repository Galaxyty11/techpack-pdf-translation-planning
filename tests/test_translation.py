import csv
import hashlib
import json
from dataclasses import replace
import traceback

import pytest

from techpack_pdf.glossary import Glossary, GlossaryHit, load_glossary
from techpack_pdf.models import CoordinateConfidence, DecisionReason, PageType
from techpack_pdf.selection import Candidate, lock_tokens
from techpack_pdf.translation import (
    TranslationValidationError,
    translation_cache_key,
    validate_translation_response,
    write_translation_request,
)


def test_request_is_sorted_minimal_and_uses_page_specific_modes(tmp_path) -> None:
    direct = _candidate(
        "p001-i002",
        PageType.BOM,
        "TOPSTITCH 0.6 cm FROM EDGE",
        source_kind="construction note",
        glossary_hits=(
            GlossaryHit("topstitch", "明线", "TOPSTITCH", 0, 9, False, 3),
        ),
    )
    digest = _candidate(
        "p001-i001",
        PageType.SAMPLE_REVIEW,
        "Reduce sleeve by 0.6 cm unless approved",
        source_kind="action",
    )
    skipped = replace(_candidate("p001-i003", PageType.BOM, "Header"), should_translate=False)
    path = tmp_path / "translation-request.json"

    payload = write_translation_request([direct, skipped, digest], path)

    assert payload == json.loads(path.read_text(encoding="utf-8"))
    assert [item["item_id"] for item in payload] == ["p001-i001", "p001-i002"]
    assert payload[0] == {
        "item_id": "p001-i001",
        "source_text": "Reduce sleeve by 0.6 cm unless approved",
        "context": "action",
        "locked_tokens": ["0.6", "cm"],
        "glossary_terms": [],
        "page_type": "sample_review",
        "mode": "faithful_digest",
    }
    assert payload[1] == {
        "item_id": "p001-i002",
        "source_text": "TOPSTITCH 0.6 cm FROM EDGE",
        "context": "construction note",
        "locked_tokens": ["0.6", "cm"],
        "glossary_terms": [{"source_term": "topstitch", "target_term": "明线"}],
        "page_type": "bom",
        "mode": "direct",
    }


def test_response_is_joined_by_item_id_not_array_position() -> None:
    request = _request(
        ("p001-i001", "Shell 12 mm", "direct"),
        ("p001-i002", "Reduce sleeve 0.6 cm", "faithful_digest"),
    )
    response = [
        _response("p001-i002", "袖长减少 0.6 cm", "faithful_digest", ["0.6", "cm"]),
        _response("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
    ]

    validated = validate_translation_response(request, response, _empty_glossary())

    assert [item.item_id for item in validated] == ["p001-i001", "p001-i002"]
    assert [item.translated_text for item in validated] == ["大身 12 mm", "袖长减少 0.6 cm"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda item: item.update(translated_text="  "), "empty_translation"),
        (lambda item: item.pop("translator"), "translator_missing"),
        (lambda item: item["translator"].update(model=""), "model_missing"),
        (lambda item: item.update(unexpected="not allowed"), "response_schema_invalid"),
    ],
)
def test_response_is_strictly_pydantic_validated_before_semantic_checks(
    mutation, expected_code
) -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    item = _response("p001-i001", "大身 12 mm", "direct", ["12", "mm"])
    mutation(item)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, [item], _empty_glossary())

    assert caught.value.error_codes == (expected_code,)


@pytest.mark.parametrize(
    "untrusted_id",
    [
        "customer confidential measurement notes",
        "p1-i1",
        f"p{'1' * 80}-i001",
    ],
)
def test_schema_failure_never_copies_untrusted_item_ids_to_correction(untrusted_id) -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    item = _response(untrusted_id, "敏感业务译文 12 mm", "direct", ["12", "mm"])
    item["unexpected"] = "force schema failure"

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, [item], _empty_glossary())

    serialized = json.dumps(caught.value.result, ensure_ascii=False)
    assert caught.value.failed_item_ids == ("p001-i001",)
    assert untrusted_id not in serialized
    assert "敏感业务译文" not in serialized


def test_schema_failure_suppresses_pydantic_chain_with_raw_translation() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    item = _response(
        "p001-i001",
        "DO-NOT-LEAK-FULL-TRANSLATION 12 mm",
        "direct",
        ["12", "mm"],
    )
    item.pop("translator")

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, [item], _empty_glossary())

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True
    assert "DO-NOT-LEAK-FULL-TRANSLATION" not in rendered


def test_response_rejects_missing_and_unexpected_ids_without_index_guessing() -> None:
    request = _request(
        ("p001-i001", "Shell 12 mm", "direct"),
        ("p001-i002", "Lining 8 mm", "direct"),
    )
    response = [
        _response("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
        _response("p001-i999", "里布 8 mm", "direct", ["8", "mm"]),
    ]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary())

    assert caught.value.error_codes == ("item_id_set_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i002", "p001-i999")


def test_response_rejects_duplicate_ids() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    response = [
        _response("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
        _response("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
    ]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary())

    assert caught.value.error_codes == ("duplicate_item_id",)
    assert caught.value.failed_item_ids == ("p001-i001",)


@pytest.mark.parametrize(
    ("translated_text", "preserved_tokens"),
    [
        ("距边 0.6", ["0.6", "cm"]),
        ("距边 0.6 cm，另加 1 mm", ["0.6", "cm"]),
        ("距边 6 mm", ["0.6", "cm"]),
        ("距边 0.6 cm", ["0.6", "cm", "cm"]),
    ],
)
def test_response_reuses_task5_validation_for_missing_added_or_changed_tokens(
    translated_text, preserved_tokens
) -> None:
    request = _request(("p001-i001", "TOPSTITCH 0.6 cm FROM EDGE", "direct"))
    response = [
        _response("p001-i001", translated_text, "direct", preserved_tokens),
    ]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary())

    assert caught.value.error_codes == ("locked_token_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i001",)


def test_response_rejects_glossary_terms_used_mismatch() -> None:
    request = _request_with_glossary("p001-i001")
    response = [_response("p001-i001", "边缘明线 0.6 cm", "direct", ["0.6", "cm"])]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _topstitch_glossary())

    assert caught.value.error_codes == ("glossary_terms_used_mismatch",)


def test_response_rejects_translation_without_required_glossary_target() -> None:
    request = _request_with_glossary("p001-i001")
    response = [
        _response(
            "p001-i001",
            "边缘车缝 0.6 cm",
            "direct",
            ["0.6", "cm"],
            glossary_terms_used=["topstitch"],
        )
    ]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _topstitch_glossary())

    assert caught.value.error_codes == ("glossary_target_missing",)


def test_authoritative_glossary_match_overrides_stale_request_target(tmp_path) -> None:
    glossary = _loaded_glossary(
        tmp_path,
        [{"source_term": "topstitch", "target_term": "明线"}],
    )
    request = _request_with_glossary("p001-i001")
    request[0]["glossary_terms"][0]["target_term"] = "旧译"
    response = [
        _response(
            "p001-i001",
            "边缘明线 0.6 cm",
            "direct",
            ["0.6", "cm"],
            glossary_terms_used=["topstitch"],
        )
    ]

    validated = validate_translation_response(request, response, glossary)

    assert validated[0].translated_text == "边缘明线 0.6 cm"


def test_authoritative_glossary_match_resolves_alias(tmp_path) -> None:
    glossary = _loaded_glossary(
        tmp_path,
        [
            {
                "source_term": "topstitch",
                "target_term": "明线",
                "aliases": "edge stitch",
            }
        ],
    )
    request = _request(("p001-i001", "EDGE STITCH 0.6 cm FROM EDGE", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "topstitch", "target_term": "旧译"}]
    response = [
        _response(
            "p001-i001",
            "边缘明线 0.6 cm",
            "direct",
            ["0.6", "cm"],
            glossary_terms_used=["topstitch"],
        )
    ]

    validated = validate_translation_response(request, response, glossary)

    assert validated[0].glossary_terms_used == ("topstitch",)


def test_authoritative_glossary_match_uses_selected_priority(tmp_path) -> None:
    glossary = _loaded_glossary(
        tmp_path,
        [
            {"source_term": "stitch", "target_term": "低优先", "priority": 1},
            {"source_term": "stitch", "target_term": "高优先", "priority": 9},
        ],
    )
    request = _request(("p001-i001", "STITCH 0.6 cm", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "stitch", "target_term": "过期"}]
    response = [
        _response(
            "p001-i001",
            "高优先 0.6 cm",
            "direct",
            ["0.6", "cm"],
            glossary_terms_used=["stitch"],
        )
    ]

    validated = validate_translation_response(request, response, glossary)

    assert validated[0].translated_text == "高优先 0.6 cm"


def test_authoritative_do_not_translate_match_requires_only_locked_source_token(tmp_path) -> None:
    glossary = _loaded_glossary(
        tmp_path,
        [
            {
                "source_term": "AcmeTex",
                "target_term": "艾克米",
                "do_not_translate": False,
                "priority": 99,
            },
            {
                "source_term": "AcmeTex",
                "target_term": "",
                "do_not_translate": True,
                "priority": 0,
            },
        ],
    )
    request = _request(("p001-i001", "Use AcmeTex fabric", "direct"))
    request[0]["locked_tokens"] = ["AcmeTex"]
    request[0]["glossary_terms"] = [{"source_term": "AcmeTex", "target_term": "艾克米"}]
    response = [
        _response(
            "p001-i001",
            "使用AcmeTex面料",
            "direct",
            ["AcmeTex"],
            glossary_terms_used=["AcmeTex"],
        )
    ]

    validated = validate_translation_response(request, response, glossary)

    assert validated[0].translated_text == "使用AcmeTex面料"


def test_response_rejects_mode_mismatch() -> None:
    request = _request(("p001-i001", "Reduce sleeve 0.6 cm", "faithful_digest"))
    response = [_response("p001-i001", "袖长减少 0.6 cm", "direct", ["0.6", "cm"])]

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary())

    assert caught.value.error_codes == ("mode_mismatch",)


def test_unknown_model_is_valid_and_retained() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    response = [_response("p001-i001", "大身 12 mm", "direct", ["12", "mm"], model="unknown")]

    validated = validate_translation_response(request, response, _empty_glossary())

    assert validated[0].translator.model == "unknown"


def test_first_failure_writes_one_targeted_correction_request(tmp_path) -> None:
    request_path = tmp_path / "translation-request.json"
    request_path.write_text(
        json.dumps(_request(("p001-i001", "Shell 12 mm", "direct"))),
        encoding="utf-8",
    )

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, [], _empty_glossary())

    correction = json.loads((tmp_path / "correction-request.json").read_text(encoding="utf-8"))
    assert set(correction) == {"attempt", "failed_item_ids", "error_codes", "required_fixes"}
    assert correction == caught.value.result
    assert correction["attempt"] == 1
    assert correction["failed_item_ids"] == ["p001-i001"]
    assert correction["error_codes"] == ["item_id_set_mismatch"]
    assert correction["required_fixes"]


def test_failed_correction_stops_at_human_review_without_guessing() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    response = {"attempt": 1, "items": []}

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary())

    assert caught.value.result == {"status": "human_review_required"}
    assert caught.value.correction_request is None


def test_attempt_on_request_envelope_also_stops_after_one_correction() -> None:
    request = {
        "attempt": 1,
        "items": _request(("p001-i001", "Shell 12 mm", "direct")),
    }

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, [], _empty_glossary())

    assert caught.value.result == {"status": "human_review_required"}
    assert caught.value.correction_request is None


def test_cache_key_uses_canonical_request_and_known_models_cross_jobs() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))
    reordered = [{key: request[0][key] for key in reversed(request[0])}]
    cache_material = [
        ["request", request],
        ["glossary_sha256", "a" * 64],
        ["prompt_version", "1.0"],
        ["host", "codex"],
        ["model", "gpt-5"],
        ["execution_mode", "subagent"],
    ]
    canonical = json.dumps(
        cache_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    first = translation_cache_key(
        request, "a" * 64, "1.0", "codex", "gpt-5", "subagent", job_id="job-a"
    )
    second = translation_cache_key(
        reordered, "a" * 64, "1.0", "codex", "gpt-5", "subagent", job_id="job-b"
    )

    assert first == expected
    assert second == expected


def test_cache_key_separates_provenance_field_boundaries() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))

    first = translation_cache_key(
        request, "a" * 64, "ab", "c", "gpt-5", "subagent"
    )
    second = translation_cache_key(
        request, "a" * 64, "a", "bc", "gpt-5", "subagent"
    )

    assert first != second


def test_unknown_model_cache_is_scoped_to_job() -> None:
    request = _request(("p001-i001", "Shell 12 mm", "direct"))

    first = translation_cache_key(
        request, "a" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-a"
    )
    second = translation_cache_key(
        request, "a" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-b"
    )

    assert first != second
    with pytest.raises(ValueError, match="job_id"):
        translation_cache_key(
            request, "a" * 64, "1.0", "codex", "unknown", "main_agent"
        )


def _candidate(
    item_id: str,
    page_type: PageType,
    source_text: str,
    *,
    source_kind: str = "body",
    glossary_hits: tuple[GlossaryHit, ...] = (),
) -> Candidate:
    return Candidate(
        item_id=item_id,
        page_index=0,
        page_type=page_type,
        classification_confidence=1.0,
        classification_evidence=(f"test:{page_type.value}",),
        source_text=source_text,
        normalized_text=source_text.casefold(),
        source_bbox=(10.0, 10.0, 90.0, 20.0),
        source_kind=source_kind,
        coordinate_confidence=CoordinateConfidence.HIGH,
        source_auto_approvable=True,
        auto_approvable=True,
        should_translate=True,
        decision_reason=DecisionReason.FIELD_RULE,
        locked_text=lock_tokens(source_text),
        glossary_hits=glossary_hits,
    )


def _request(*items: tuple[str, str, str]) -> list[dict]:
    return [
        {
            "item_id": item_id,
            "source_text": source_text,
            "context": "body",
            "locked_tokens": [token.value for token in lock_tokens(source_text).tokens],
            "glossary_terms": [],
            "page_type": "sample_review" if mode == "faithful_digest" else "bom",
            "mode": mode,
        }
        for item_id, source_text, mode in items
    ]


def _request_with_glossary(item_id: str) -> list[dict]:
    request = _request((item_id, "TOPSTITCH 0.6 cm FROM EDGE", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "topstitch", "target_term": "明线"}]
    return request


def _response(
    item_id: str,
    translated_text: str,
    mode: str,
    preserved_tokens: list[str],
    *,
    glossary_terms_used: list[str] | None = None,
    model: str = "gpt-5",
) -> dict:
    return {
        "item_id": item_id,
        "translated_text": translated_text,
        "preserved_tokens": preserved_tokens,
        "glossary_terms_used": glossary_terms_used or [],
        "mode": mode,
        "warnings": [],
        "translator": {
            "host": "codex",
            "execution_mode": "subagent",
            "model": model,
            "agent_role": "techpack-translator",
            "prompt_version": "1.0",
        },
    }


def _empty_glossary() -> Glossary:
    return Glossary(entries=(), _terms=())


def _topstitch_glossary() -> Glossary:
    return Glossary(entries=(), _terms=())


def _loaded_glossary(tmp_path, rows: list[dict]) -> Glossary:
    path = tmp_path / "glossary.csv"
    fieldnames = (
        "source_term",
        "target_term",
        "aliases",
        "do_not_translate",
        "priority",
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return load_glossary(path)
