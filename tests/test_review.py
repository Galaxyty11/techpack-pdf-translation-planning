import hashlib
import inspect
import json
import traceback
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


def test_build_review_html_rejects_non_png_or_remote_thumbnail(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["pages"][0]["thumbnail"] = "https://example.invalid/page.png"

    with pytest.raises(TechpackError, match="review page data is invalid"):
        build_review_html(job, output)


def test_build_review_html_never_defaults_risky_or_conflicting_item_to_approved(tmp_path) -> None:
    job, _source, _glossary = _job(tmp_path)
    output = _review_output("Shell 12 mm")
    output["items"][0].update(
        coordinate_confidence="low",
        decision_reason="low_confidence",
        risk_level="high",
        warnings=["coordinate_conflict"],
        review_status="approved",
    )

    html = build_review_html(job, output)
    probe = _HtmlProbe()
    probe.feed(html)
    embedded = json.loads("".join(probe.script_text))

    assert embedded["items"][0]["review_status"] is None


def test_load_review_accepts_schema_1_1_and_all_three_explicit_statuses(tmp_path) -> None:
    job, source, glossary = _job(tmp_path, page_count=3)
    payload = _review_document(job, statuses=("approved", "approved_edited", "skipped"))
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    review = load_review(review_path, source, glossary)

    assert review.schema_version == "1.1"
    assert [item.review_status.value for item in review.items] == [
        "approved",
        "approved_edited",
        "skipped",
    ]
    assert review.blocking_issues == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda payload: payload.update(schema_version="1.0"), "review_schema_invalid"),
        (lambda payload: payload["source"].update(filename="other.pdf"), "review_source_filename_mismatch"),
        (lambda payload: payload["source"].update(sha256="0" * 64), "review_source_hash_mismatch"),
        (lambda payload: payload["source"].update(page_count=99), "review_source_page_count_mismatch"),
        (lambda payload: payload["glossary"].update(filename="other.csv"), "review_glossary_filename_mismatch"),
        (lambda payload: payload["glossary"].update(sha256="0" * 64), "review_glossary_hash_mismatch"),
        (lambda payload: payload.update(job_id="f" * 12 + "-20260822T000000Z"), "review_job_mismatch"),
    ],
)
def test_load_review_rejects_stale_or_unbound_review(
    tmp_path, mutation, expected_code
) -> None:
    job, source, glossary = _job(tmp_path)
    payload = _review_document(job)
    mutation(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, source, glossary)

    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda payload: payload["items"][0].update(review_status=None), "review_status_incomplete"),
        (lambda payload: payload["items"][0].update(review_status="pending"), "review_schema_invalid"),
        (lambda payload: payload.update(blocking_issues=[{"code": "overlap"}]), "review_blocked"),
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
            "review_translation_missing",
        ),
        (lambda payload: payload["items"][0].update(translation_model=""), "review_schema_invalid"),
        (lambda payload: payload["items"][0].update(translation_agent_role=None), "review_provenance_incomplete"),
    ],
)
def test_load_review_requires_complete_review_contract(
    tmp_path, mutation, expected_code
) -> None:
    job, source, glossary = _job(tmp_path)
    payload = _review_document(job)
    mutation(payload)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, source, glossary)

    assert caught.value.code == expected_code


def test_load_review_recomputes_current_input_hashes(tmp_path) -> None:
    job, source, glossary = _job(tmp_path)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_review_document(job)), encoding="utf-8")
    glossary.write_text("source_term,target_term\nshell,面料\n", encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, source, glossary)

    assert caught.value.code == "review_glossary_hash_mismatch"


def test_load_review_errors_do_not_leak_business_text(tmp_path) -> None:
    job, source, glossary = _job(tmp_path)
    payload = _review_document(job)
    payload["items"][0]["source_text"] = "PRIVATE-CUSTOMER-MEASUREMENTS"
    payload["items"][0]["review_status"] = "pending"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        load_review(review_path, source, glossary)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True
    assert "PRIVATE-CUSTOMER-MEASUREMENTS" not in rendered


def test_load_review_has_no_bypass_or_ignore_parameter() -> None:
    assert list(inspect.signature(load_review).parameters) == ["path", "source", "glossary"]


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
            "execution_mode": "mixed" if len(statuses) > 1 else "subagent",
            "model": "unknown",
            "prompt_version": "1.0",
        },
        "items": [
            _item(index, status, text=f"Source {index} 12 mm")
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
        "glossary_hits": [{"source_term": "shell", "target_term": "大身"}],
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
