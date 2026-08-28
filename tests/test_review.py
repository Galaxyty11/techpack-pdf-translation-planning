import hashlib
import inspect
import json
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser

import pymupdf
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.models import FileArtifact, JobManifest
from techpack_pdf.review import build_review_html, load_review


_MALICIOUS = "</script><script>alert(1)</script>"
_PNG_DATA_URI = "data:image/png;base64,iVBORw0KGgo="


class _HtmlProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.script_text: list[str] = []
        self.ids: set[str] = set()
        self._in_json_script = False

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if identifier := attributes.get("id"):
            self.ids.add(identifier)
        if tag == "script":
            self.scripts.append(attributes)
            self._in_json_script = attributes.get("type") == "application/json"

    def handle_endtag(self, tag) -> None:
        if tag == "script":
            self._in_json_script = False

    def handle_data(self, data) -> None:
        if self._in_json_script:
            self.script_text.append(data)


def test_build_review_html_is_offline_and_script_injection_safe(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    html = build_review_html(job, _review_output(_MALICIOUS))
    probe = _HtmlProbe()
    probe.feed(html)

    json_scripts = [script for script in probe.scripts if script.get("type") == "application/json"]
    assert len(json_scripts) == 1
    assert len(probe.scripts) == 2
    embedded = json.loads("".join(probe.script_text))
    assert embedded["items"][0]["source_text"] == _MALICIOUS
    assert embedded["items"][0]["suggested_translation"] == _MALICIOUS
    assert embedded["pages"][0]["thumbnail"] == _PNG_DATA_URI
    assert embedded["pipeline"]["parser"] == "pymupdf+mineru"
    assert embedded["pipeline"]["translation_executor"] == "host_agent"
    assert _MALICIOUS not in html
    assert "\\u003c/script>\\u003cscript>alert(1)\\u003c/script>" in html

    lowered = html.casefold()
    for forbidden in ("http://", "https://", "cdn", "fetch(", "websocket", "innerhtml"):
        assert forbidden not in lowered


def test_build_review_html_exposes_required_review_controls(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    html = build_review_html(job, _review_output("Shell 12 mm"))
    probe = _HtmlProbe()
    probe.feed(html)

    assert {
        "page-type-filter",
        "risk-filter",
        "glossary-filter",
        "status-filter",
        "issue-filter",
        "item-list",
        "page-thumbnail",
        "source-highlight",
        "target-highlight",
        "source-text",
        "reviewed-translation",
        "locked-tokens",
        "glossary-hits",
        "decision-reason",
        "coordinates",
        "layout-risk",
        "translator-provenance",
        "model-risk",
        "stats",
        "approve",
        "approve-edited",
        "skip",
        "export-review",
    } <= probe.ids
    assert "approved_edited" in html
    assert "blocking_issues" in html


def test_build_review_html_explains_business_fields_without_showing_raw_json(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["items"][0].update(
        decision_reason="page_rule",
        coordinate_confidence="high",
        target_rect=None,
        risk_level="low",
        warnings=[],
    )

    embedded = _embedded_payload(build_review_html(job, output))

    explanation = embedded["business_explanations"]["p001-i001"]
    assert explanation == {
        "decision_reason": "这段文字属于本页需要翻译的内容。",
        "coordinates": [
            "原文位置：已在左侧预览中用红框标出（定位准确）。",
            "译文位置：写入 PDF 时自动安排。",
        ],
        "layout_risk": {
            "level": "low",
            "label": "低风险",
            "summary": "未发现明显排版问题，通常可以直接审核。",
            "warnings": [],
        },
    }
    assert embedded["items"][0]["decision_reason"] == "page_rule"
    assert embedded["items"][0]["source_bbox"] == [10.0, 10.0, 90.0, 20.0]
    assert embedded["items"][0]["target_rect"] is None


def test_build_review_html_turns_layout_warnings_into_plain_chinese(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["items"][0].update(
        coordinate_confidence="low",
        risk_level="high",
        warnings=["coordinate_confidence", "translator_warning"],
    )

    embedded = _embedded_payload(build_review_html(job, output))

    explanation = embedded["business_explanations"]["p001-i001"]
    assert explanation["coordinates"] == [
        "原文位置：已在左侧预览中用红框标出（位置可能不准，请重点检查红框）。",
        "译文位置：已在左侧预览中用蓝框标出。",
    ]
    assert explanation["layout_risk"] == {
        "level": "high",
        "label": "请重点检查",
        "summary": "发现可能影响审核或排版的问题，请逐项确认。",
        "warnings": [
            "原文位置可能不够准确，请检查左侧红框。",
            "翻译过程提示需要人工检查。",
        ],
    }


def test_build_review_html_rejects_non_png_or_remote_thumbnail(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["pages"][0]["thumbnail"] = "https://example.invalid/page.png"

    with pytest.raises(TechpackError, match="review page data is invalid"):
        build_review_html(job, output)


@pytest.mark.parametrize("prefilled_status", ["approved", "approved_edited", "skipped"])
def test_build_review_html_unconditionally_resets_prefilled_review_state(
    tmp_path, prefilled_status
) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["items"][0].update(
        review_status=prefilled_status,
        reviewed_translation="</script><script>prefilledBypass()</script>",
    )

    html = build_review_html(job, output)
    probe = _HtmlProbe()
    probe.feed(html)
    embedded = json.loads("".join(probe.script_text))

    assert embedded["items"][0]["review_status"] is None
    assert embedded["items"][0]["reviewed_translation"] is None
    assert '<button id="export-review" type="button" disabled>' in html


def test_build_review_html_aggregates_mixed_execution_modes_from_all_items(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    second = deepcopy(output["items"][0])
    second.update(item_id="p001-i002", translation_execution_mode="main_agent")
    output["items"].append(second)
    output["pipeline"]["execution_mode"] = "mixed"

    embedded = _embedded_payload(build_review_html(job, output))

    assert embedded["pipeline"]["execution_mode"] == "mixed"


def test_build_review_html_aggregates_and_flags_all_provenance_switches(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    second = deepcopy(output["items"][0])
    second.update(
        item_id="p001-i002",
        translation_host="chatgpt",
        translation_execution_mode="main_agent",
        translation_model="gpt-5",
        translation_prompt_version="2.0",
    )
    output["items"].append(second)
    output["pipeline"].update(
        host="mixed", execution_mode="mixed", model="mixed", prompt_version="mixed"
    )

    html = build_review_html(job, output)
    embedded = _embedded_payload(html)

    assert embedded["pipeline"] == {
        "parser": "pymupdf+mineru",
        "translation_executor": "host_agent",
        "host": "mixed",
        "execution_mode": "mixed",
        "model": "mixed",
        "prompt_version": "mixed",
    }
    for risk_label in ("宿主切换", "模型切换", "执行方式切换", "提示词版本切换"):
        assert risk_label in html


def test_build_review_html_rejects_explicit_pipeline_contradiction(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    second = deepcopy(output["items"][0])
    second.update(item_id="p001-i002", translation_execution_mode="main_agent")
    output["items"].append(second)

    with pytest.raises(TechpackError) as caught:
        build_review_html(job, output)

    assert caught.value.code == "review_page_invalid"


def test_load_review_accepts_schema_1_1_and_all_three_explicit_statuses(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path, page_count=3)
    payload = _review_document(job, statuses=("approved", "approved_edited", "skipped"))
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    review = load_review(review_path, job, expected_output)

    assert review.schema_version == "1.1"
    assert [item.review_status.value for item in review.items] == [
        "approved",
        "approved_edited",
        "skipped",
    ]
    assert review.blocking_issues == []


def test_load_review_accepts_explicit_null_role_for_main_agent(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["pipeline"]["execution_mode"] = "main_agent"
    payload["items"][0].update(
        translation_execution_mode="main_agent",
        translation_agent_role=None,
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    review = load_review(review_path, job, expected_output)

    assert review.items[0].translation_agent_role is None


@pytest.mark.parametrize("execution_mode", ["subagent", "mixed"])
@pytest.mark.parametrize("agent_role", [None, "   "])
def test_load_review_rejects_invalid_delegated_role_as_schema_error(
    tmp_path,
    execution_mode,
    agent_role,
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    payload["items"][0].update(
        translation_execution_mode=execution_mode,
        translation_agent_role=agent_role,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_schema_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda payload: payload.pop("schema_version"), "review_schema_invalid"),
        (lambda payload: payload.update(schema_version="1.0"), "review_schema_invalid"),
        (lambda payload: payload["source"].update(filename="other.pdf"), "review_source_filename_mismatch"),
        (lambda payload: payload["source"].update(sha256="0" * 64), "review_source_hash_mismatch"),
        (lambda payload: payload["source"].update(page_count=99), "review_source_page_count_mismatch"),
        (lambda payload: payload["glossary"].update(filename="other.csv"), "review_glossary_filename_mismatch"),
        (lambda payload: payload["glossary"].update(sha256="0" * 64), "review_glossary_hash_mismatch"),
        (
            lambda payload: payload.update(
                job_id=payload["job_id"][:13] + "20260822T010000Z"
            ),
            "review_job_mismatch",
        ),
    ],
)
def test_load_review_rejects_stale_or_unbound_review(
    tmp_path, mutation, expected_code
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    mutation(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda payload: payload["items"][0].update(review_status=None), "review_status_incomplete"),
        (lambda payload: payload["items"][0].update(review_status="pending"), "review_schema_invalid"),
        (
            lambda payload: payload.update(blocking_issues=[{"code": "overlap"}]),
            "review_blocking_mismatch",
        ),
        (lambda payload: payload.update(review_completed_at=None), "review_incomplete"),
        (
            lambda payload: payload["items"][0].update(
                review_status="approved_edited", reviewed_translation=None
            ),
            "review_edited_translation_missing",
        ),
        (
            lambda payload: payload["items"][0].update(
                review_status="approved", suggested_translation=None
            ),
            "review_item_mismatch",
        ),
        (lambda payload: payload["items"][0].update(translation_model=""), "review_schema_invalid"),
        (lambda payload: payload["items"][0].update(translation_agent_role=None), "review_schema_invalid"),
    ],
)
def test_load_review_requires_complete_review_contract(
    tmp_path, mutation, expected_code
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    mutation(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == expected_code


def test_load_review_recomputes_current_input_hashes(tmp_path) -> None:
    job, _source, glossary = _job(tmp_path)
    payload = _review_document(job)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")
    glossary.write_text("source_term,target_term\nshell,面料\n", encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, _expected_output(payload))

    assert caught.value.code == "review_glossary_hash_mismatch"


@pytest.mark.parametrize(
    "invalid_timestamp",
    [0, 1787385600.0, "2026-08-22T08:00:00"],
)
def test_load_review_requires_iso_timestamp_with_timezone(
    tmp_path, invalid_timestamp
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    payload["review_completed_at"] = invalid_timestamp
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_timestamp_invalid"


def test_load_review_errors_do_not_leak_business_text(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    payload["items"][0]["source_text"] = "PRIVATE-CUSTOMER-MEASUREMENTS"
    payload["items"][0]["review_status"] = "pending"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True
    assert "PRIVATE-CUSTOMER-MEASUREMENTS" not in rendered


def test_load_review_has_no_bypass_or_ignore_parameter() -> None:
    assert list(inspect.signature(load_review).parameters) == ["path", "job", "expected_output"]


@pytest.mark.parametrize("missing_path", ["source", "glossary"])
def test_load_review_requires_job_manifest_input_paths(tmp_path, missing_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    artifact = getattr(job, missing_path).model_copy(update={"path": None})
    unbound_job = job.model_copy(update={missing_path: artifact})
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, unbound_job, _expected_output(payload))

    assert caught.value.code == "review_job_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda items: items.pop(), "review_item_set_mismatch"),
        (lambda items: items.append(deepcopy(items[0])), "review_item_duplicate"),
        (
            lambda items: items[0].update(source_text="REPLACED-CUSTOMER-CONTENT"),
            "review_item_mismatch",
        ),
        (
            lambda items: items.append(
                {**deepcopy(items[0]), "item_id": "p001-i999"}
            ),
            "review_item_set_mismatch",
        ),
    ],
)
def test_load_review_rejects_missing_extra_replaced_or_duplicate_items(
    tmp_path, mutation, expected_code
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    mutation(payload["items"])
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == expected_code


def test_load_review_rejects_pipeline_that_contradicts_item_provenance(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"].append(
        {
            **deepcopy(payload["items"][0]),
            "item_id": "p001-i002",
            "translation_execution_mode": "main_agent",
        }
    )
    payload["pipeline"]["execution_mode"] = "mixed"
    expected_output = _expected_output(payload)
    payload["pipeline"]["execution_mode"] = "subagent"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_pipeline_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("parser", "different-parser", "review_pipeline_mismatch"),
        ("translation_executor", "direct_api", "review_schema_invalid"),
        ("model", "different-model", "review_pipeline_mismatch"),
    ],
)
def test_load_review_binds_the_full_trusted_pipeline(
    tmp_path, field, value, expected_code
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    expected_output = _expected_output(payload)
    payload["pipeline"][field] = value
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == expected_code


def test_load_review_rejects_removed_trusted_blocking_issues(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["blocking_issues"] = [{"code": "overlap"}]
    expected_output = _expected_output(payload)
    payload["blocking_issues"] = []
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_blocking_mismatch"


def test_load_review_keeps_matching_trusted_blocking_issues_blocked(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["blocking_issues"] = [{"code": "overlap"}]
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_blocked"


@pytest.mark.parametrize(
    ("status", "suggested", "reviewed"),
    [
        ("approved", "大身 12", None),
        ("approved", "大身 12 mm 2", None),
        ("approved_edited", "大身 12 mm", "人工修改 13 mm"),
    ],
)
def test_load_review_revalidates_locked_tokens_in_the_final_translation(
    tmp_path, status, suggested, reviewed
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text="Shell 12 mm",
        normalized_text="shell 12 mm",
        locked_tokens=["12", "mm"],
        glossary_hits=[],
        suggested_translation=suggested,
        reviewed_translation=reviewed,
        review_status=status,
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_locked_token_mismatch"


def test_load_review_rejects_missing_authoritative_glossary_target(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text="Shell 12 mm",
        normalized_text="shell 12 mm",
        suggested_translation="外壳 12 mm",
        glossary_hits=[_glossary_hit("shell", "大身", "Shell")],
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_glossary_target_missing"


@pytest.mark.parametrize(
    "suggested_translation",
    ["使用艾克米 12 mm", "使用 acmetex 12 mm", "使用 AcmeTex AcmeTex 12 mm"],
)
def test_load_review_rejects_changed_removed_or_duplicated_do_not_translate_term(
    tmp_path, suggested_translation
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text="Use AcmeTex 12 mm",
        normalized_text="use acmetex 12 mm",
        locked_tokens=["12", "mm"],
        suggested_translation=suggested_translation,
        glossary_hits=[
            _glossary_hit("AcmeTex", "", "acmetex", do_not_translate=True, start=4)
        ],
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_glossary_dnt_mismatch"


@pytest.mark.parametrize("source_term", ["AcmeTex", "ＡｃｍｅＴｅｘ"])
def test_load_review_accepts_exact_dnt_spelling_projected_from_normalized_hit(
    tmp_path, source_term
) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text=f"Use {source_term} 12 mm",
        normalized_text="use acmetex 12 mm",
        locked_tokens=["12", "mm"],
        suggested_translation=f"使用 {source_term} 12 mm",
        glossary_hits=[
            _glossary_hit("AcmeTex", "", "acmetex", do_not_translate=True, start=4)
        ],
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    review = load_review(review_path, job, expected_output)

    assert review.items[0].suggested_translation == f"使用 {source_term} 12 mm"


def test_load_review_rejects_user_modified_dnt_hit_fields(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text="Use AcmeTex 12 mm",
        normalized_text="use acmetex 12 mm",
        locked_tokens=["12", "mm"],
        suggested_translation="使用 AcmeTex 12 mm",
        glossary_hits=[
            _glossary_hit("AcmeTex", "", "acmetex", do_not_translate=True, start=4)
        ],
    )
    expected_output = _expected_output(payload)
    payload["items"][0]["glossary_hits"][0]["start"] = 0
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, job, expected_output)

    assert caught.value.code == "review_item_mismatch"


def test_load_review_accepts_preserved_tokens_glossary_target_and_dnt(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0].update(
        source_text="Use AcmeTex shell 12 mm",
        normalized_text="use acmetex shell 12 mm",
        locked_tokens=["12", "mm"],
        suggested_translation="使用 AcmeTex 大身 12 mm",
        glossary_hits=[
            _glossary_hit("AcmeTex", "", "acmetex", do_not_translate=True, start=4),
            _glossary_hit("shell", "大身", "shell", start=12),
        ],
    )
    expected_output = _expected_output(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    review = load_review(review_path, job, expected_output)

    assert review.items[0].review_status.value == "approved"


def _job(tmp_path, page_count: int = 1):
    source = tmp_path / "techpack.pdf"
    document = pymupdf.open()
    for _ in range(page_count):
        document.new_page(width=200, height=300)
    document.save(source)
    document.close()
    glossary = tmp_path / "glossary.csv"
    glossary.write_text("source_term,target_term\nshell,大身\n", encoding="utf-8")
    source_hash = _sha256(source)
    job = JobManifest(
        job_id=f"{source_hash[:12]}-20260822T000000Z",
        source=FileArtifact(
            filename=source.name,
            sha256=source_hash,
            path=source,
            page_count=page_count,
        ),
        glossary=FileArtifact(
            filename=glossary.name,
            sha256=_sha256(glossary),
            path=glossary,
        ),
        job_dir=tmp_path / "job",
        created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    return job, source, glossary


def _review_output(text: str) -> dict:
    return {
        "pipeline": {
            "parser": "pymupdf+mineru",
            "translation_executor": "host_agent",
            "host": "codex",
            "execution_mode": "subagent",
            "model": "unknown",
            "prompt_version": "1.0",
        },
        "items": [_item(0, "approved", text=text)],
        "blocking_issues": [],
        "pages": [
            {
                "page_index": 0,
                "width": 200,
                "height": 300,
                "thumbnail": _PNG_DATA_URI,
            }
        ],
    }


def _review_document(job: JobManifest, statuses=("approved",)) -> dict:
    return {
        "schema_version": "1.1",
        "job_id": job.job_id,
        "source": {
            "filename": job.source.filename,
            "sha256": job.source.sha256,
            "page_count": job.source.page_count,
        },
        "glossary": {
            "filename": job.glossary.filename,
            "sha256": job.glossary.sha256,
        },
        "pipeline": {
            "parser": "pymupdf+mineru",
            "translation_executor": "host_agent",
            "host": "codex",
            "execution_mode": "subagent",
            "model": "unknown",
            "prompt_version": "1.0",
        },
        "items": [
            _item(index, status, text=f"Source {chr(65 + index)} 12 mm")
            for index, status in enumerate(statuses)
        ],
        "blocking_issues": [],
        "review_completed_at": "2026-08-22T08:00:00Z",
    }


def _item(page_index: int, status: str, *, text: str) -> dict:
    return {
        "item_id": f"p{page_index + 1:03d}-i001",
        "page_index": page_index,
        "page_type": "bom",
        "source_text": text,
        "normalized_text": text.casefold(),
        "source_bbox": [10.0, 10.0, 90.0, 20.0],
        "source_kind": "body",
        "coordinate_confidence": "high",
        "decision_reason": "field_rule",
        "locked_tokens": ["12", "mm"],
        "glossary_hits": [],
        "suggested_translation": text,
        "reviewed_translation": "人工修改 12 mm" if status == "approved_edited" else None,
        "review_status": status,
        "risk_level": "low",
        "translation_host": "codex",
        "translation_execution_mode": "subagent",
        "translation_model": "unknown",
        "translation_agent_role": "techpack-translator",
        "translation_prompt_version": "1.0",
        "placement_strategy": "same_region",
        "target_rect": [100.0, 10.0, 180.0, 30.0],
        "font_size": 6.0,
        "leader_line": None,
        "warnings": [],
    }


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_output(payload: dict) -> dict:
    expected = {
        "pipeline": deepcopy(payload["pipeline"]),
        "items": deepcopy(payload["items"]),
        "blocking_issues": deepcopy(payload["blocking_issues"]),
    }
    for item in expected["items"]:
        item["review_status"] = None
        item["reviewed_translation"] = None
    return expected


def _embedded_payload(html: str) -> dict:
    probe = _HtmlProbe()
    probe.feed(html)
    return json.loads("".join(probe.script_text))


def _glossary_hit(
    source_term: str,
    target_term: str,
    matched_text: str,
    *,
    do_not_translate: bool = False,
    start: int = 0,
) -> dict:
    return {
        "source_term": source_term,
        "target_term": target_term,
        "matched_text": matched_text,
        "start": start,
        "end": start + len(matched_text),
        "do_not_translate": do_not_translate,
        "priority": 1,
    }
