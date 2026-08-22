import csv
import hashlib
import json
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.glossary import Glossary, GlossaryHit, load_glossary
from techpack_pdf.models import CoordinateConfidence, DecisionReason, FileArtifact, JobManifest, PageType
from techpack_pdf.selection import Candidate, lock_tokens
from techpack_pdf.translation import (
    TranslationValidationError,
    translation_cache_key,
    validate_translation_response,
    write_translation_request,
)


def test_write_request_is_bound_hashed_sorted_and_minimal(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    direct = _candidate("p001-i002", PageType.BOM, "TOPSTITCH 0.6 cm FROM EDGE", source_kind="construction note", glossary_hits=(GlossaryHit("topstitch", "明线", "TOPSTITCH", 0, 9, False, 3),))
    digest = _candidate("p001-i001", PageType.SAMPLE_REVIEW, "Reduce sleeve by 0.6 cm unless approved", source_kind="action")
    payload = write_translation_request([direct, replace(_candidate("p001-i003", PageType.BOM, "Header"), should_translate=False), digest], tmp_path / "translation-request.json", job)

    assert payload == json.loads((tmp_path / "translation-request.json").read_text(encoding="utf-8"))
    assert {key for key in payload} == {"schema_version", "job_id", "source_sha256", "glossary_sha256", "request_sha256", "attempt", "items"}
    assert payload["job_id"] == "job-a"
    assert payload["attempt"] == 0
    assert [item["item_id"] for item in payload["items"]] == ["p001-i001", "p001-i002"]
    assert payload["items"] == [
        {
            "item_id": "p001-i001",
            "source_text": "Reduce sleeve by 0.6 cm unless approved",
            "context": "action",
            "locked_tokens": ["0.6", "cm"],
            "glossary_terms": [],
            "page_type": "sample_review",
            "mode": "faithful_digest",
        },
        {
            "item_id": "p001-i002",
            "source_text": "TOPSTITCH 0.6 cm FROM EDGE",
            "context": "construction note",
            "locked_tokens": ["0.6", "cm"],
            "glossary_terms": [{"source_term": "topstitch", "target_term": "明线"}],
            "page_type": "bom",
            "mode": "direct",
        },
    ]
    assert payload["request_sha256"] == _request_hash(payload)


def test_response_is_joined_by_item_id_not_array_position():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct"), ("p001-i002", "Reduce sleeve 0.6 cm", "faithful_digest")), job)
    response = _bound_response([
        _item("p001-i002", "袖长减少 0.6 cm", "faithful_digest", ["0.6", "cm"]),
        _item("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
    ], request)

    validated = validate_translation_response(request, response, _empty_glossary(), job)

    assert [item.item_id for item in validated] == ["p001-i001", "p001-i002"]
    assert [item.translated_text for item in validated] == ["大身 12 mm", "袖长减少 0.6 cm"]


def test_response_with_identical_items_cannot_be_swapped_between_jobs():
    job_a = _job("job-a", "a" * 64, "b" * 64)
    job_b = _job("job-b", "c" * 64, "b" * 64)
    request_a = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job_a)
    request_b = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job_b)
    response_a = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request_a)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_b, response_a, _empty_glossary(), job_b)

    assert caught.value.error_codes == ("response_binding_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i001",)


def test_request_and_response_swapped_to_wrong_job_are_rejected_before_response_is_read():
    job_a = _job("job-a", "a" * 64, "b" * 64)
    job_b = _job("job-b", "c" * 64, "b" * 64)
    request_a = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job_a)

    with pytest.raises(TechpackError) as caught:
        validate_translation_response(request_a, object(), _empty_glossary(), job_b)

    assert caught.value.code == "translation_request_invalid"


def test_response_from_a_different_request_in_the_same_job_is_rejected():
    job = _job("job-a", "a" * 64, "b" * 64)
    first = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    second = _bound_request(_items(("p001-i001", "Lining 12 mm", "direct")), job)
    response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], first)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(second, response, _empty_glossary(), job)

    assert caught.value.error_codes == ("response_binding_mismatch",)


def test_tampered_request_items_fail_hash_validation_without_consuming_response():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request["items"][0]["source_text"] = "confidential altered text"

    with pytest.raises(TechpackError) as caught:
        validate_translation_response(request, object(), _empty_glossary(), job)

    assert caught.value.code == "translation_request_invalid"
    assert "confidential" not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("job_id"),
        lambda value: value.update(unexpected="not allowed"),
        lambda value: value.update(request_sha256="A" * 64),
        lambda value: value.update(attempt=2),
    ],
)
def test_response_requires_exact_strict_binding_fields(mutation):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request)
    mutation(response)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary(), job)

    assert caught.value.error_codes == ("response_schema_invalid",)


def test_legacy_bare_response_array_is_rejected():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, [_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], _empty_glossary(), job)

    assert caught.value.error_codes == ("response_schema_invalid",)


def test_first_failure_writes_a_bound_correction_request(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    response = _bound_response([], request)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, response, _empty_glossary(), job)

    correction = json.loads((tmp_path / "correction-request.json").read_text(encoding="utf-8"))
    assert correction == caught.value.result
    assert correction == {
        "schema_version": "1.1",
        "job_id": "job-a",
        "source_sha256": "a" * 64,
        "glossary_sha256": "b" * 64,
        "request_sha256": request["request_sha256"],
        "attempt": 1,
        "failed_item_ids": ["p001-i001"],
        "error_codes": ["item_id_set_mismatch"],
        "required_fixes": ["Return exactly one item for every requested item_id and no others."],
    }


def test_invalid_second_response_stops_for_human_review_without_guessing(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    first = _bound_response([], request)
    with pytest.raises(TranslationValidationError):
        validate_translation_response(request_path, first, _empty_glossary(), job)
    second = _bound_response([], request, attempt=1)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, second, _empty_glossary(), job, expected_attempt=1)

    assert caught.value.result == {"status": "human_review_required"}
    assert caught.value.correction_request is None


def test_initial_response_cannot_claim_the_correction_attempt():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request, attempt=1)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary(), job)

    assert caught.value.error_codes == ("response_attempt_invalid",)


def test_schema_failure_does_not_leak_untrusted_translation_or_id():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    response = _bound_response([_item("private identifier", "DO-NOT-LEAK-TRANSLATION 12 mm", "direct", ["12", "mm"])], request)
    response["items"][0]["unexpected"] = "force schema failure"

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary(), job)

    rendered = "".join(traceback.format_exception(caught.value))
    assert "private identifier" not in json.dumps(caught.value.result, ensure_ascii=False)
    assert "DO-NOT-LEAK-TRANSLATION" not in rendered


def test_semantic_validation_keeps_locked_tokens_and_glossary_rules():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "TOPSTITCH 0.6 cm FROM EDGE", "direct", [{"source_term": "topstitch", "target_term": "明线"}])), job)
    response = _bound_response([_item("p001-i001", "边缘车缝 0.6 cm", "direct", ["0.6", "cm"], ["topstitch"])], request)
    glossary = Glossary(entries=(), _terms=())

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, glossary, job)

    assert caught.value.error_codes == ("glossary_target_missing",)


def test_cache_key_keeps_known_model_identical_content_cross_job_reuse():
    job_a = _job("job-a", "a" * 64, "b" * 64)
    job_b = _job("job-b", "c" * 64, "b" * 64)
    items = _items(("p001-i001", "Shell 12 mm", "direct"))
    first = translation_cache_key(_bound_request(items, job_a), "b" * 64, "1.0", "codex", "gpt-5", "subagent")
    second = translation_cache_key(_bound_request(items, job_b), "b" * 64, "1.0", "codex", "gpt-5", "subagent")

    assert first == second


def test_cache_key_uses_canonical_request_items_and_known_models_cross_jobs():
    job_a = _job("job-a", "a" * 64, "b" * 64)
    job_b = _job("job-b", "c" * 64, "b" * 64)
    items = _items(("p001-i001", "Shell 12 mm", "direct"))
    reordered_items = [{key: items[0][key] for key in reversed(items[0])}]
    material = [
        ["request", items],
        ["glossary_sha256", "b" * 64],
        ["prompt_version", "1.0"],
        ["host", "codex"],
        ["model", "gpt-5"],
        ["execution_mode", "subagent"],
    ]
    expected = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    first = translation_cache_key(_bound_request(items, job_a), "b" * 64, "1.0", "codex", "gpt-5", "subagent", job_id="job-a")
    second = translation_cache_key(_bound_request(reordered_items, job_b), "b" * 64, "1.0", "codex", "gpt-5", "subagent", job_id="job-b")

    assert first == expected
    assert second == expected


def test_unknown_model_cache_remains_limited_to_the_bound_job():
    job_a = _job("job-a", "a" * 64, "b" * 64)
    job_b = _job("job-b", "c" * 64, "b" * 64)
    items = _items(("p001-i001", "Shell 12 mm", "direct"))
    first = translation_cache_key(_bound_request(items, job_a), "b" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-a")
    second = translation_cache_key(_bound_request(items, job_b), "b" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-b")

    assert first != second
    with pytest.raises(ValueError, match="job_id"):
        translation_cache_key(_bound_request(items, job_a), "b" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-b")


def test_request_attempt_one_is_rejected_by_the_strict_envelope():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request["attempt"] = 1
    request["request_sha256"] = _request_hash(request)

    with pytest.raises(TechpackError) as caught:
        validate_translation_response(request, object(), _empty_glossary(), job)

    assert caught.value.code == "translation_request_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda item: item.update(translated_text="  "), "empty_translation"),
        (lambda item: item.pop("translator"), "translator_missing"),
        (lambda item: item["translator"].update(model=""), "model_missing"),
        (lambda item: item.update(unexpected="not allowed"), "response_schema_invalid"),
    ],
)
def test_response_is_strictly_pydantic_validated_before_semantic_checks(mutation, expected_code):
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])
    mutation(response)

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    assert caught.value.error_codes == (expected_code,)


@pytest.mark.parametrize("untrusted_id", ["customer confidential measurement notes", "p1-i1", f"p{'1' * 80}-i001"])
def test_schema_failure_never_copies_untrusted_item_ids_to_correction(untrusted_id):
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item(untrusted_id, "敏感业务译文 12 mm", "direct", ["12", "mm"])
    response["unexpected"] = "force schema failure"

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    serialized = json.dumps(caught.value.result, ensure_ascii=False)
    assert caught.value.failed_item_ids == ("p001-i001",)
    assert untrusted_id not in serialized
    assert "敏感业务译文" not in serialized


def test_schema_failure_suppresses_pydantic_chain_with_raw_translation():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item("p001-i001", "DO-NOT-LEAK-FULL-TRANSLATION 12 mm", "direct", ["12", "mm"])
    response.pop("translator")

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    assert caught.value.__suppress_context__ is True
    assert "DO-NOT-LEAK-FULL-TRANSLATION" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("untrusted_id", ["customer measurement notes", f"p{'1' * 80}-i001"])
def test_request_item_id_is_rejected_by_strict_bound_schema(untrusted_id):
    request = _items((untrusted_id, "Shell 12 mm", "direct"))

    with pytest.raises(TechpackError) as caught:
        _validate_items(request, [], _empty_glossary())

    assert caught.value.code == "translation_request_invalid"


@pytest.mark.parametrize("untrusted_id", ["customer measurement notes", f"p{'1' * 80}-i001"])
def test_response_item_id_schema_failure_cannot_enter_mismatch_result(untrusted_id):
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item(untrusted_id, "大身 12 mm", "direct", ["12", "mm"])

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    assert caught.value.error_codes == ("response_schema_invalid",)
    assert caught.value.failed_item_ids == ("p001-i001",)
    assert untrusted_id not in json.dumps(caught.value.result, ensure_ascii=False)


def test_arbitrary_duplicate_response_ids_fail_schema_without_being_reported():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = [_item("private duplicate text", "大身 12 mm", "direct", ["12", "mm"])] * 2

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _empty_glossary())

    assert caught.value.error_codes == ("response_schema_invalid",)
    assert caught.value.failed_item_ids == ("p001-i001",)
    assert "private duplicate text" not in json.dumps(caught.value.result, ensure_ascii=False)


def test_response_rejects_missing_and_unexpected_ids_without_index_guessing():
    request = _items(("p001-i001", "Shell 12 mm", "direct"), ("p001-i002", "Lining 8 mm", "direct"))
    response = [_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"]), _item("p999-i999", "里布 8 mm", "direct", ["8", "mm"])]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _empty_glossary())

    assert caught.value.error_codes == ("item_id_set_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i002",)
    assert "p999-i999" not in json.dumps(caught.value.result, ensure_ascii=False)


def test_response_rejects_duplicate_ids():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = [_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])] * 2

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _empty_glossary())

    assert caught.value.error_codes == ("duplicate_item_id",)
    assert caught.value.failed_item_ids == ("p001-i001",)


@pytest.mark.parametrize(("translated_text", "preserved_tokens"), [("距边 0.6", ["0.6", "cm"]), ("距边 0.6 cm，另加 1 mm", ["0.6", "cm"]), ("距边 6 mm", ["0.6", "cm"]), ("距边 0.6 cm", ["0.6", "cm", "cm"])])
def test_response_reuses_task5_validation_for_missing_added_or_changed_tokens(translated_text, preserved_tokens):
    request = _items(("p001-i001", "TOPSTITCH 0.6 cm FROM EDGE", "direct"))
    response = [_item("p001-i001", translated_text, "direct", preserved_tokens)]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _empty_glossary())

    assert caught.value.error_codes == ("locked_token_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i001",)


def test_response_rejects_preserved_tokens_in_a_different_order_from_the_request():
    request = _items(("p001-i001", "Use ABC123 before XYZ456", "direct"))
    response = [_item(
        "p001-i001",
        "先用 ABC123，再用 XYZ456",
        "direct",
        ["XYZ456", "ABC123"],
    )]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _empty_glossary())

    assert caught.value.error_codes == ("locked_token_mismatch",)
    assert caught.value.failed_item_ids == ("p001-i001",)


def test_response_rejects_glossary_terms_used_mismatch():
    request = _request_with_glossary("p001-i001")
    response = [_item("p001-i001", "边缘明线 0.6 cm", "direct", ["0.6", "cm"])]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _topstitch_glossary())

    assert caught.value.error_codes == ("glossary_terms_used_mismatch",)


def test_response_rejects_translation_without_required_glossary_target():
    request = _request_with_glossary("p001-i001")
    response = [_item("p001-i001", "边缘车缝 0.6 cm", "direct", ["0.6", "cm"], ["topstitch"])]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, _topstitch_glossary())

    assert caught.value.error_codes == ("glossary_target_missing",)


def test_authoritative_glossary_match_overrides_stale_request_target(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "topstitch", "target_term": "明线"}])
    request = _request_with_glossary("p001-i001")
    request[0]["glossary_terms"][0]["target_term"] = "旧译"
    response = [_item("p001-i001", "边缘明线 0.6 cm", "direct", ["0.6", "cm"], ["topstitch"])]

    assert _validate_items(request, response, glossary)[0].translated_text == "边缘明线 0.6 cm"


def test_authoritative_glossary_match_resolves_alias(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "topstitch", "target_term": "明线", "aliases": "edge stitch"}])
    request = _items(("p001-i001", "EDGE STITCH 0.6 cm FROM EDGE", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "topstitch", "target_term": "旧译"}]
    response = [_item("p001-i001", "边缘明线 0.6 cm", "direct", ["0.6", "cm"], ["topstitch"])]

    assert _validate_items(request, response, glossary)[0].glossary_terms_used == ("topstitch",)


def test_authoritative_glossary_prefers_global_canonical_hit_over_earlier_alias(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "alpha", "target_term": "甲", "aliases": "beta", "priority": 2}, {"source_term": "beta", "target_term": "乙", "aliases": "gamma", "priority": 1}])
    request = _items(("p001-i001", "BETA GAMMA 0.6 cm", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "alpha", "target_term": "过期甲"}, {"source_term": "beta", "target_term": "过期乙"}]
    response = [_item("p001-i001", "甲和乙 0.6 cm", "direct", ["0.6", "cm"], ["alpha", "beta"])]

    assert _validate_items(request, response, glossary)[0].translated_text == "甲和乙 0.6 cm"


def test_authoritative_glossary_rejects_missing_later_canonical_target(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "alpha", "target_term": "甲", "aliases": "beta", "priority": 2}, {"source_term": "beta", "target_term": "乙", "aliases": "gamma", "priority": 1}])
    request = _items(("p001-i001", "BETA GAMMA 0.6 cm", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "alpha", "target_term": "过期甲"}, {"source_term": "beta", "target_term": "过期乙"}]
    response = [_item("p001-i001", "只有甲 0.6 cm", "direct", ["0.6", "cm"], ["alpha", "beta"])]

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, response, glossary)

    assert caught.value.error_codes == ("glossary_target_missing",)
    assert caught.value.failed_item_ids == ("p001-i001",)


def test_authoritative_glossary_match_uses_selected_priority(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "stitch", "target_term": "低优先", "priority": 1}, {"source_term": "stitch", "target_term": "高优先", "priority": 9}])
    request = _items(("p001-i001", "STITCH 0.6 cm", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "stitch", "target_term": "过期"}]
    response = [_item("p001-i001", "高优先 0.6 cm", "direct", ["0.6", "cm"], ["stitch"])]

    assert _validate_items(request, response, glossary)[0].translated_text == "高优先 0.6 cm"


def test_authoritative_do_not_translate_match_requires_only_locked_source_token(tmp_path):
    glossary = _loaded_glossary(tmp_path, [{"source_term": "AcmeTex", "target_term": "艾克米", "do_not_translate": False, "priority": 99}, {"source_term": "AcmeTex", "target_term": "", "do_not_translate": True, "priority": 0}])
    request = _items(("p001-i001", "Use AcmeTex fabric", "direct"))
    request[0]["locked_tokens"] = ["AcmeTex"]
    request[0]["glossary_terms"] = [{"source_term": "AcmeTex", "target_term": "艾克米"}]
    response = [_item("p001-i001", "使用AcmeTex面料", "direct", ["AcmeTex"], ["AcmeTex"])]

    assert _validate_items(request, response, glossary)[0].translated_text == "使用AcmeTex面料"


def test_response_rejects_mode_mismatch():
    request = _items(("p001-i001", "Reduce sleeve 0.6 cm", "faithful_digest"))
    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [_item("p001-i001", "袖长减少 0.6 cm", "direct", ["0.6", "cm"])], _empty_glossary())
    assert caught.value.error_codes == ("mode_mismatch",)


def test_unknown_model_is_valid_and_retained():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    validated = _validate_items(request, [_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"], model="unknown")], _empty_glossary())
    assert validated[0].translator.model == "unknown"


def test_cache_key_separates_provenance_field_boundaries():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    assert translation_cache_key(request, "b" * 64, "ab", "c", "gpt-5", "subagent") != translation_cache_key(request, "b" * 64, "a", "bc", "gpt-5", "subagent")


def test_trusted_expected_attempt_one_rejects_a_valid_initial_response_without_rewriting_correction(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    correction_path = tmp_path / "correction-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    correction_path.write_text("preexisting correction must remain", encoding="utf-8")
    initial_response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, initial_response, _empty_glossary(), job, expected_attempt=1)

    assert caught.value.result == {"status": "human_review_required"}
    assert caught.value.error_codes == ("response_attempt_invalid",)
    assert correction_path.read_text(encoding="utf-8") == "preexisting correction must remain"


def test_trusted_expected_attempt_one_accepts_only_a_bound_correction_response():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    correction_response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request, attempt=1)

    validated = validate_translation_response(request, correction_response, _empty_glossary(), job, expected_attempt=1)

    assert [item.item_id for item in validated] == ["p001-i001"]


def test_trusted_expected_attempt_one_malformed_response_is_terminal_and_redacted(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    correction_path = tmp_path / "correction-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    correction_path.write_text("do not overwrite", encoding="utf-8")
    malformed = '{"items": ["DO-NOT-LEAK-COMPLETE-RESPONSE"]'

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, malformed, _empty_glossary(), job, expected_attempt=1)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.result == {"status": "human_review_required"}
    assert correction_path.read_text(encoding="utf-8") == "do not overwrite"
    assert "DO-NOT-LEAK-COMPLETE-RESPONSE" not in rendered


def test_trusted_expected_initial_attempt_writes_one_bound_correction_for_malformed_json(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, '{"items": [', _empty_glossary(), job)

    correction = json.loads((tmp_path / "correction-request.json").read_text(encoding="utf-8"))
    assert caught.value.result == correction
    assert correction["attempt"] == 1
    assert correction["request_sha256"] == request["request_sha256"]
    assert correction["failed_item_ids"] == ["p001-i001"]
    assert correction["error_codes"] == ["invalid_json"]


def test_trusted_expected_correction_attempt_schema_failure_is_terminal_without_clobbering(tmp_path):
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path = tmp_path / "translation-request.json"
    correction_path = tmp_path / "correction-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    correction_path.write_text("must remain unchanged", encoding="utf-8")
    response = _bound_response([_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"])], request, attempt=1)
    response["items"][0]["unexpected"] = "schema failure"

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request_path, response, _empty_glossary(), job, expected_attempt=1)

    assert caught.value.result == {"status": "human_review_required"}
    assert caught.value.error_codes == ("response_schema_invalid",)
    assert correction_path.read_text(encoding="utf-8") == "must remain unchanged"


def test_external_stable_response_id_never_leaks_when_no_trusted_id_is_missing():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    response = _bound_response([
        _item("p001-i001", "大身 12 mm", "direct", ["12", "mm"]),
        _item("p999-i999", "SECRET-EXTERNAL-TRANSLATION", "direct", ["12", "mm"]),
    ], request)

    with pytest.raises(TranslationValidationError) as caught:
        validate_translation_response(request, response, _empty_glossary(), job)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.failed_item_ids == ("p001-i001",)
    assert "p999-i999" not in json.dumps(caught.value.result, ensure_ascii=False)
    assert "SECRET-EXTERNAL-TRANSLATION" not in rendered


def test_schema_failure_never_reports_a_legal_but_external_response_id():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item("p999-i999", "SECRET-SCHEMA-TRANSLATION", "direct", ["12", "mm"])
    response["unexpected"] = "schema failure"

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    assert caught.value.failed_item_ids == ("p001-i001",)
    assert "p999-i999" not in json.dumps(caught.value.result, ensure_ascii=False)


@pytest.mark.parametrize("model", [" gpt-5", "unknown "])
def test_response_model_rejects_edge_whitespace(model):
    request = _items(("p001-i001", "Shell 12 mm", "direct"))
    response = _item("p001-i001", "大身 12 mm", "direct", ["12", "mm"], model=model)

    with pytest.raises(TranslationValidationError) as caught:
        _validate_items(request, [response], _empty_glossary())

    assert caught.value.error_codes == ("model_missing",)


def test_response_model_normalizes_case_insensitive_unknown():
    request = _items(("p001-i001", "Shell 12 mm", "direct"))

    validated = _validate_items(request, [_item("p001-i001", "大身 12 mm", "direct", ["12", "mm"], model="UNKNOWN")], _empty_glossary())

    assert validated[0].translator.model == "unknown"


def test_cache_model_normalizes_unknown_and_rejects_edge_whitespace():
    job = _job("job-a", "a" * 64, "b" * 64)
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    unknown = translation_cache_key(request, "b" * 64, "1.0", "codex", "unknown", "main_agent", job_id="job-a")

    assert translation_cache_key(request, "b" * 64, "1.0", "codex", "UNKNOWN", "main_agent", job_id="job-a") == unknown
    with pytest.raises(ValueError, match="job_id"):
        translation_cache_key(request, "b" * 64, "1.0", "codex", "UNKNOWN", "main_agent", job_id="wrong-job")
    with pytest.raises(ValueError, match="model"):
        translation_cache_key(request, "b" * 64, "1.0", "codex", "unknown ", "main_agent", job_id="job-a")
    with pytest.raises(ValueError, match="model"):
        translation_cache_key(request, "b" * 64, "1.0", "codex", " gpt-5", "main_agent")


def test_request_and_cache_read_failures_are_safe_and_do_not_expose_paths_or_payloads(tmp_path, monkeypatch):
    job = _job("job-a", "a" * 64, "b" * 64)
    request_path = tmp_path / "REQUEST-PATH-SECRET.json"

    def fail_read(self, *args, **kwargs):
        raise OSError(f"REQUEST-IO-SECRET {self}")

    monkeypatch.setattr(Path, "read_text", fail_read)
    malformed_cache_json = '{"payload":"CACHE-JSON-SECRET"'
    with pytest.raises(TechpackError) as request_error:
        validate_translation_response(request_path, object(), _empty_glossary(), job)
    with pytest.raises(TechpackError) as cache_error:
        translation_cache_key(malformed_cache_json, "b" * 64, "1.0", "codex", "gpt-5", "main_agent")

    request_rendered = "".join(traceback.format_exception(request_error.value))
    cache_rendered = "".join(traceback.format_exception(cache_error.value))
    assert request_error.value.code == "translation_request_invalid"
    assert cache_error.value.code == "translation_request_invalid"
    assert "REQUEST-IO-SECRET" not in request_rendered
    assert str(request_path) not in request_rendered
    assert "CACHE-JSON-SECRET" not in cache_rendered


def test_request_and_correction_write_failures_are_safe_and_do_not_expose_paths_or_payloads(tmp_path, monkeypatch):
    job = _job("job-a", "a" * 64, "b" * 64)
    request_path = tmp_path / "WRITE-PATH-SECRET.json"
    candidate = _candidate("p001-i001", PageType.BOM, "Shell 12 mm")

    def fail_all_writes(self, *args, **kwargs):
        raise OSError(f"WRITE-IO-SECRET {self}")

    monkeypatch.setattr(Path, "write_text", fail_all_writes)
    with pytest.raises(TechpackError) as request_error:
        write_translation_request([candidate], request_path, job)
    request_rendered = "".join(traceback.format_exception(request_error.value))
    assert request_error.value.code == "translation_request_write_failed"
    assert "WRITE-IO-SECRET" not in request_rendered
    assert str(request_path) not in request_rendered

    monkeypatch.undo()
    request = _bound_request(_items(("p001-i001", "Shell 12 mm", "direct")), job)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    original_write = Path.write_text

    def fail_correction_write(self, *args, **kwargs):
        if self.name == "correction-request.json":
            raise OSError(f"CORRECTION-IO-SECRET {self}")
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_correction_write)
    with pytest.raises(TechpackError) as correction_error:
        validate_translation_response(request_path, _bound_response([], request), _empty_glossary(), job)
    correction_rendered = "".join(traceback.format_exception(correction_error.value))
    assert correction_error.value.code == "translation_correction_write_failed"
    assert "CORRECTION-IO-SECRET" not in correction_rendered
    assert str(request_path) not in correction_rendered


def _job(job_id, source_hash, glossary_hash):
    return JobManifest(schema_version="1.1", job_id=job_id, source=FileArtifact(filename="source.pdf", sha256=source_hash, path="source.pdf", page_count=1), glossary=FileArtifact(filename="terms.csv", sha256=glossary_hash, path="terms.csv"), job_dir="job", created_at="2026-08-22T12:00:00Z")


def _candidate(item_id, page_type, source_text, *, source_kind="body", glossary_hits=()):
    return Candidate(item_id=item_id, page_index=0, page_type=page_type, classification_confidence=1.0, classification_evidence=("test",), source_text=source_text, normalized_text=source_text.casefold(), source_bbox=(10.0, 10.0, 90.0, 20.0), source_kind=source_kind, coordinate_confidence=CoordinateConfidence.HIGH, source_auto_approvable=True, auto_approvable=True, should_translate=True, decision_reason=DecisionReason.FIELD_RULE, locked_text=lock_tokens(source_text), glossary_hits=glossary_hits)


def _items(*values):
    result = []
    for item_id, source_text, mode, *terms in values:
        result.append({"item_id": item_id, "source_text": source_text, "context": "body", "locked_tokens": [token.value for token in lock_tokens(source_text).tokens], "glossary_terms": terms[0] if terms else [], "page_type": "sample_review" if mode == "faithful_digest" else "bom", "mode": mode})
    return result


def _item(item_id, translated_text, mode, tokens, glossary_terms_used=None, *, model="gpt-5"):
    return {"item_id": item_id, "translated_text": translated_text, "preserved_tokens": tokens, "glossary_terms_used": glossary_terms_used or [], "mode": mode, "warnings": [], "translator": {"host": "codex", "execution_mode": "subagent", "model": model, "agent_role": "techpack-translator", "prompt_version": "1.0"}}


def _bound_request(items, job):
    payload = {"schema_version": "1.1", "job_id": job.job_id, "source_sha256": job.source.sha256, "glossary_sha256": job.glossary.sha256, "attempt": 0, "items": items}
    payload["request_sha256"] = _request_hash(payload)
    return payload


def _bound_response(items, request, *, attempt=0):
    return {"schema_version": request["schema_version"], "job_id": request["job_id"], "source_sha256": request["source_sha256"], "glossary_sha256": request["glossary_sha256"], "request_sha256": request["request_sha256"], "attempt": attempt, "items": items}


def _request_hash(payload):
    material = dict(payload)
    material.pop("request_sha256", None)
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _empty_glossary():
    return Glossary(entries=(), _terms=())


def _topstitch_glossary():
    return Glossary(entries=(), _terms=())


def _request_with_glossary(item_id):
    request = _items((item_id, "TOPSTITCH 0.6 cm FROM EDGE", "direct"))
    request[0]["glossary_terms"] = [{"source_term": "topstitch", "target_term": "明线"}]
    return request


def _validate_items(items, response_items, glossary):
    job = _job("semantic-job", "a" * 64, "b" * 64)
    request = _bound_request(items, job)
    response = _bound_response(response_items, request)
    return validate_translation_response(request, response, glossary, job)


def _loaded_glossary(tmp_path, rows):
    path = tmp_path / "glossary.csv"
    fieldnames = ("source_term", "target_term", "aliases", "do_not_translate", "priority")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return load_glossary(path)
