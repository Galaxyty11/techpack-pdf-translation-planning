import json
import hashlib
import os
import re
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pymupdf
import pytest
from pydantic import ValidationError

from techpack_pdf.errors import TechpackError
import techpack_pdf.workflow as workflow
from techpack_pdf.workflow import analyze, apply, prepare_review
from techpack_pdf.models import CoordinateConfidence


class _MinerUFixture:
    def parse_or_degrade(self, _source, _manifest):
        return {
            "pages": [
                {
                    "page_index": 0,
                    "title": "BOM",
                    "nodes": [
                        {
                            "text": "Shell 12 mm",
                            "bbox": [72, 72, 150, 86],
                            "field_role": "body",
                        }
                    ],
                }
            ]
        }


class _ManyNodesMinerUFixture:
    def parse_or_degrade(self, _source, _manifest):
        return {
            "pages": [
                {
                    "page_index": 0,
                    "title": "BOM",
                    "nodes": [
                        {
                            "text": f"Component {index} mm",
                            "bbox": [72, 72, 150, 86],
                            "field_role": "body",
                        }
                        for index in range(1, 1001)
                    ],
                }
            ]
        }


class _NativeOnlyFixture:
    def parse_or_degrade(self, _source, manifest):
        from techpack_pdf.mineru import DegradedPage, NativeOnlyDegradation

        return NativeOnlyDegradation(
            pages=tuple(DegradedPage(page.page_index) for page in manifest.pages)
        )


class _MutatingMinerUFixture(_MinerUFixture):
    def __init__(self, source, glossary):
        self._source = source
        self._glossary = glossary

    def parse_or_degrade(self, source, manifest):
        assert source != self._source
        self._source.write_bytes(self._source.read_bytes() + b"changed-during-analysis")
        self._glossary.write_text("source_term,target_term\nChanged,changed\n", encoding="utf-8")
        return super().parse_or_degrade(source, manifest)


class _ExplodingMinerUFixture:
    def parse_or_degrade(self, _source, _manifest):
        raise RuntimeError("must be redacted")


class _SnapshotMutatingMinerUFixture(_MinerUFixture):
    def parse_or_degrade(self, source, manifest):
        original = source.read_bytes()
        changed = source.with_name("changed-snapshot.pdf")
        changed.write_bytes(original + b"temporary snapshot mutation")
        os.replace(changed, source)
        restored = source.with_name("restored-snapshot.pdf")
        restored.write_bytes(original)
        os.replace(restored, source)
        return super().parse_or_degrade(source, manifest)


class _UnknownMinerUFixture:
    def parse_or_degrade(self, _source, _manifest):
        return {
            "pages": [
                {
                    "page_index": 0,
                    "title": "",
                    "nodes": [
                        {
                            "text": "Shell 12 mm",
                            "bbox": [72, 72, 150, 86],
                            "field_role": "body",
                        }
                    ],
                }
            ]
        }


class _DntMinerUFixture:
    def __init__(self, *, title="BOM"):
        self._title = title

    def parse_or_degrade(self, _source, _manifest):
        return {
            "pages": [
                {
                    "page_index": 0,
                    "title": self._title,
                    "nodes": [
                        {
                            "text": "Use AcmeTex fabric",
                            "bbox": [72, 72, 180, 86],
                            "field_role": "body",
                        }
                    ],
                }
            ]
        }


class _MixedClassificationFixture:
    def parse_or_degrade(self, _source, _manifest):
        return {
            "pages": [
                {
                    "page_index": 0,
                    "title": "BOM",
                    "nodes": [
                        {
                            "text": "Shell 12 mm",
                            "bbox": [72, 72, 150, 86],
                            "field_role": "body",
                        }
                    ],
                },
                {
                    "page_index": 1,
                    "title": "",
                    "nodes": [
                        {
                            "text": "Collar 5 mm",
                            "bbox": [72, 72, 150, 86],
                            "field_role": "body",
                        }
                    ],
                },
            ]
        }


def _techpack_pdf(path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "BOM")
    page.insert_text((72, 86), "Shell 12 mm")
    document.save(path)
    document.close()


def _two_page_techpack_pdf(path):
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 72), "BOM")
    first.insert_text((72, 86), "Shell 12 mm")
    second = document.new_page()
    second.insert_text((72, 72), "Unlabelled details")
    second.insert_text((72, 86), "Collar 5 mm")
    document.save(path)
    document.close()


def _dnt_techpack_pdf(path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "BOM")
    page.insert_text((72, 86), "Use AcmeTex fabric")
    document.save(path)
    document.close()


def _glossary(path):
    path.write_text("source_term,target_term\nShell,大身\n", encoding="utf-8")


def _dnt_glossary(path):
    path.write_text(
        "source_term,target_term,do_not_translate\nAcmeTex,,true\n",
        encoding="utf-8",
    )


def _response_for(request, *, attempt=0, translated_text="大身 12 mm"):
    payload = {
        key: request[key]
        for key in ("schema_version", "job_id", "source_sha256", "glossary_sha256", "request_sha256")
    }
    payload.update({"attempt": attempt, "items": [
        {
            "item_id": request["items"][0]["item_id"],
            "translated_text": translated_text,
            "preserved_tokens": ["12", "mm"],
            "glossary_terms_used": ["Shell"],
            "mode": "direct",
            "warnings": [],
            "translator": {
                "host": "codex",
                "execution_mode": "main_agent",
                "model": "unknown",
                "agent_role": "techpack-translator",
                "prompt_version": "1.0",
            },
        }
    ]})
    return payload


def _response_for_all(request, *, attempt=0):
    payload = {
        key: request[key]
        for key in ("schema_version", "job_id", "source_sha256", "glossary_sha256", "request_sha256")
    }
    payload["attempt"] = attempt
    payload["items"] = [
        {
            "item_id": item["item_id"],
            "translated_text": "大身 12 mm" if item["source_text"] == "Shell 12 mm" else item["source_text"],
            "preserved_tokens": item["locked_tokens"],
            "glossary_terms_used": [term["source_term"] for term in item["glossary_terms"]],
            "mode": item["mode"], "warnings": [],
            "translator": {"host": "codex", "execution_mode": "main_agent", "model": "unknown", "agent_role": "techpack-translator", "prompt_version": "1.0"},
        }
        for item in request["items"]
    ]
    return payload


def _classification_response(request, *, page_type="bom", confidence=0.95, evidence=None):
    return {
        key: request[key]
        for key in (
            "schema_version",
            "job_id",
            "source_sha256",
            "glossary_sha256",
            "request_sha256",
        )
    } | {
        "items": [
            {
                "page_index": item["page_index"],
                "page_type": page_type,
                "confidence": confidence,
                "evidence": ["agent:visual BOM structure"] if evidence is None else evidence,
            }
            for item in request["items"]
        ]
    }


def _write_approved_review(job_dir):
    match = re.search(r'<script id="review-data" type="application/json">(.*?)</script>', (job_dir / "review.html").read_text(encoding="utf-8"), re.S)
    assert match is not None
    review = json.loads(match.group(1))
    review.pop("pages")
    for item in review["items"]:
        item["review_status"] = "approved"
        item["reviewed_translation"] = None
    review["review_completed_at"] = "2026-08-22T12:01:00+00:00"
    path = job_dir / "review.json"
    path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return path


def test_analyze_creates_an_isolated_translation_request_and_waits_for_host(tmp_path):
    source = tmp_path / "techpack.pdf"
    glossary = tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_MinerUFixture(),
    )

    assert result.exit_code == 4
    assert result.state == "translation_requested"
    state = json.loads((result.job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "translation_requested"
    assert state["expected_attempt"] == 0
    assert state["wait_reason"] == "host_translation"
    request = json.loads((result.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    assert request["schema_version"] == "1.1"
    assert request["attempt"] == 0
    assert request["request_sha256"] == _request_hash(request)
    assert request["items"] == [
        {
            "item_id": "p001-i001",
            "source_text": "Shell 12 mm",
            "context": "body",
            "locked_tokens": ["12", "mm"],
            "glossary_terms": [{"source_term": "Shell", "target_term": "大身"}],
            "page_type": "bom",
            "mode": "direct",
        }
    ]
    assert not (result.job_dir / "translation-response.json").exists()


def test_analyze_preserves_stable_ids_beyond_999_candidates(tmp_path):
    source = tmp_path / "techpack.pdf"
    glossary = tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        mineru_client=_ManyNodesMinerUFixture(),
    )

    assert (result.exit_code, result.state) == (4, "translation_requested")
    request = json.loads(
        (result.job_dir / "translation-request.json").read_text(encoding="utf-8")
    )
    item_ids = [item["item_id"] for item in request["items"]]
    assert len(item_ids) == len(set(item_ids)) == 1000
    assert "p001-i1000" in item_ids


def test_analysis_accepts_empty_target_only_for_do_not_translate_hit(tmp_path):
    source, glossary = tmp_path / "dnt.pdf", tmp_path / "terms.csv"
    _dnt_techpack_pdf(source)
    _dnt_glossary(glossary)

    result = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        mineru_client=_DntMinerUFixture(),
    )

    assert (result.exit_code, result.state) == (4, "translation_requested")
    analysis = json.loads((result.job_dir / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["candidates"][0]["glossary_hits"] == [
        {
            "source_term": "AcmeTex",
            "target_term": "",
            "matched_text": "AcmeTex",
            "start": 4,
            "end": 11,
            "do_not_translate": True,
            "priority": 0,
        }
    ]
    request = json.loads(
        (result.job_dir / "translation-request.json").read_text(encoding="utf-8")
    )
    assert request["items"][0]["locked_tokens"] == ["AcmeTex"]
    assert request["items"][0]["glossary_terms"] == [
        {"source_term": "AcmeTex", "target_term": ""}
    ]


def test_glossary_hit_snapshot_rejects_empty_target_for_ordinary_hit():
    with pytest.raises(ValidationError):
        workflow._GlossaryHitSnapshot.model_validate(
            {
                "source_term": "Shell",
                "target_term": "",
                "matched_text": "Shell",
                "start": 0,
                "end": 5,
                "do_not_translate": False,
                "priority": 0,
            }
        )


def test_unknown_page_writes_bound_classification_request_and_waits_in_parsed(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_UnknownMinerUFixture(),
    )

    assert (result.exit_code, result.state, result.wait_reason) == (
        4,
        "parsed",
        "agent_classification",
    )
    request = json.loads(
        (result.job_dir / "classification-request.json").read_text(encoding="utf-8")
    )
    assert request["request_sha256"] == _request_hash(request)
    assert [{key: value for key, value in item.items() if key != "thumbnail"} for item in request["items"]] == [
        {
            "page_index": 0,
            "reason": "unknown",
            "title": "",
            "table_headers": [],
            "visual_features": [],
            "evidence": [],
        }
    ]
    assert Path(request["items"][0]["thumbnail"]) == Path("thumbnails/page-0001.png")
    assert not (result.job_dir / "translation-request.json").exists()
    state = json.loads((result.job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "parsed"
    assert state["wait_reason"] == "agent_classification"
    assert state["artifacts"]["classification_request"] == hashlib.sha256(
        (result.job_dir / "classification-request.json").read_bytes()
    ).hexdigest()


def test_valid_classification_response_resumes_same_job_into_translation_request(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request = json.loads((job.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(request)), encoding="utf-8"
    )

    resumed = prepare_review(job.job_dir)

    assert (resumed.exit_code, resumed.state) == (4, "translation_requested")
    translation_request = json.loads(
        (job.job_dir / "translation-request.json").read_text(encoding="utf-8")
    )
    assert [item["item_id"] for item in translation_request["items"]] == ["p001-i001"]
    assert translation_request["items"][0]["source_text"] == "Shell 12 mm"
    assert translation_request["items"][0]["page_type"] == "bom"
    state = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["artifacts"]["classification_response"] == hashlib.sha256(
        (job.job_dir / "classification-response.json").read_bytes()
    ).hexdigest()


def test_classification_rebuild_preserves_empty_target_dnt_hit_binding(tmp_path):
    source, glossary = tmp_path / "dnt.pdf", tmp_path / "terms.csv"
    _dnt_techpack_pdf(source)
    _dnt_glossary(glossary)
    job = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        mineru_client=_DntMinerUFixture(title=""),
    )
    request = json.loads(
        (job.job_dir / "classification-request.json").read_text(encoding="utf-8")
    )
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(request)), encoding="utf-8"
    )

    resumed = prepare_review(job.job_dir)

    assert resumed.state == "translation_requested"
    translation_request = json.loads(
        (job.job_dir / "translation-request.json").read_text(encoding="utf-8")
    )
    assert translation_request["items"][0]["locked_tokens"] == ["AcmeTex"]
    assert translation_request["items"][0]["glossary_terms"] == [
        {"source_term": "AcmeTex", "target_term": ""}
    ]
    rebuilt = json.loads((job.job_dir / "analysis.json").read_text(encoding="utf-8"))
    assert rebuilt["candidates"][0]["glossary_hits"][0]["matched_text"] == "AcmeTex"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("page_index", "0"),
        ("page_index", False),
        ("confidence", "0.95"),
        ("confidence", True),
    ],
)
def test_classification_response_rejects_coercible_numeric_types(
    tmp_path,
    field,
    invalid_value,
):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request = json.loads(
        (job.job_dir / "classification-request.json").read_text(encoding="utf-8")
    )
    response = _classification_response(request)
    response["items"][0][field] = invalid_value
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(response), encoding="utf-8"
    )

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_artifact_invalid"
    state = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "parsed"
    assert not (job.job_dir / "translation-request.json").exists()


def test_classification_response_accepts_json_integer_confidence(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request = json.loads(
        (job.job_dir / "classification-request.json").read_text(encoding="utf-8")
    )
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(request, confidence=1)), encoding="utf-8"
    )

    assert prepare_review(job.job_dir).state == "translation_requested"


def test_classification_request_rejects_coercible_page_index_type(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request_path = job.job_dir / "classification-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["items"][0]["page_index"] = "0"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    state_path = job.job_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["artifacts"]["classification_request"] = hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_artifact_invalid"
    assert not (job.job_dir / "translation-request.json").exists()


@pytest.mark.parametrize(
    ("page_type", "confidence", "evidence"),
    [
        ("bom", 0.79, ["agent:uncertain visual structure"]),
        ("bom", 0.95, []),
        ("unknown", 0.95, ["agent:no supported page type"]),
    ],
)
def test_unresolved_agent_classification_waits_without_mutation(
    tmp_path,
    page_type,
    confidence,
    evidence,
):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    before = (job.job_dir / "state.json").read_bytes()
    request = json.loads((job.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(
            request,
            page_type=page_type,
            confidence=confidence,
            evidence=evidence,
        )),
        encoding="utf-8",
    )

    resumed = prepare_review(job.job_dir)

    assert (resumed.exit_code, resumed.state, resumed.wait_reason) == (
        4,
        "parsed",
        "agent_classification",
    )
    assert (job.job_dir / "state.json").read_bytes() == before
    assert not (job.job_dir / "translation-request.json").exists()


def test_classification_response_with_invalid_binding_fails_closed(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request = json.loads((job.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    response = _classification_response(request)
    response["request_sha256"] = "0" * 64
    (job.job_dir / "classification-response.json").write_text(json.dumps(response), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_binding_mismatch"
    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "parsed"
    assert not (job.job_dir / "translation-request.json").exists()


def test_cross_job_classification_response_fails_closed(tmp_path):
    glossary = tmp_path / "terms.csv"
    source_a, source_b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _techpack_pdf(source_a)
    _techpack_pdf(source_b)
    _glossary(glossary)
    job_a = analyze(source_a, glossary, tmp_path / "jobs-a", mineru_client=_UnknownMinerUFixture())
    job_b = analyze(source_b, glossary, tmp_path / "jobs-b", mineru_client=_UnknownMinerUFixture())
    request_a = json.loads((job_a.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    (job_b.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(request_a)), encoding="utf-8"
    )

    with pytest.raises(TechpackError) as caught:
        prepare_review(job_b.job_dir)

    assert caught.value.code == "workflow_binding_mismatch"
    assert not (job_b.job_dir / "translation-request.json").exists()


def test_bound_classification_response_tamper_is_detected_after_resume(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_UnknownMinerUFixture())
    request = json.loads((job.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    response_path = job.job_dir / "classification-response.json"
    response_path.write_text(json.dumps(_classification_response(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "translation_requested"
    response_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_artifact_invalid"


def test_mixed_known_and_unknown_pages_wait_then_keep_every_candidate_on_resume(tmp_path):
    source, glossary = tmp_path / "mixed.pdf", tmp_path / "terms.csv"
    _two_page_techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MixedClassificationFixture())

    assert (job.exit_code, job.state, job.wait_reason) == (4, "parsed", "agent_classification")
    request = json.loads((job.job_dir / "classification-request.json").read_text(encoding="utf-8"))
    assert [item["page_index"] for item in request["items"]] == [1]
    (job.job_dir / "classification-response.json").write_text(
        json.dumps(_classification_response(request)), encoding="utf-8"
    )

    resumed = prepare_review(job.job_dir)

    assert resumed.state == "translation_requested"
    translation_request = json.loads(
        (job.job_dir / "translation-request.json").read_text(encoding="utf-8")
    )
    assert [(item["item_id"], item["source_text"]) for item in translation_request["items"]] == [
        ("p001-i001", "Shell 12 mm"),
        ("p002-i001", "Collar 5 mm"),
    ]


def test_prepare_review_waits_for_response_then_creates_offline_review(tmp_path):
    source = tmp_path / "techpack.pdf"
    glossary = tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    analyzed = analyze(
        source,
        glossary,
        tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_MinerUFixture(),
    )

    waiting = prepare_review(analyzed.job_dir)
    assert (waiting.exit_code, waiting.state) == (4, "translation_requested")
    assert not (analyzed.job_dir / "review.html").exists()

    request = json.loads((analyzed.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (analyzed.job_dir / "translation-response.json").write_text(
        json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8"
    )
    prepared = prepare_review(analyzed.job_dir)

    assert (prepared.exit_code, prepared.state) == (4, "review_ready")
    assert "__TECHPACK_REVIEW_DATA__" not in (analyzed.job_dir / "review.html").read_text(encoding="utf-8")
    assert json.loads((analyzed.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "review_ready"


def test_invalid_initial_response_creates_one_correction_then_attempt_one_can_recover(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    analyzed = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((analyzed.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (analyzed.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request, translated_text="")), encoding="utf-8")

    correction_wait = prepare_review(analyzed.job_dir)
    correction = json.loads((analyzed.job_dir / "correction-request.json").read_text(encoding="utf-8"))
    state = json.loads((analyzed.job_dir / "state.json").read_text(encoding="utf-8"))
    assert (correction_wait.exit_code, correction_wait.state) == (4, "translation_requested")
    assert correction["attempt"] == 1
    assert state["expected_attempt"] == 1
    assert state["wait_reason"] == "host_correction"

    (analyzed.job_dir / "translation-response.json").write_text('{"items": [', encoding="utf-8")
    terminal_wait = prepare_review(analyzed.job_dir)
    assert (terminal_wait.exit_code, terminal_wait.state) == (4, "translation_requested")
    assert json.loads((analyzed.job_dir / "correction-request.json").read_text(encoding="utf-8")) == correction
    assert json.loads((analyzed.job_dir / "state.json").read_text(encoding="utf-8"))["wait_reason"] == "human_review_required"

    (analyzed.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request, attempt=1)), encoding="utf-8")
    assert (prepare_review(analyzed.job_dir).exit_code, prepare_review(analyzed.job_dir).state) == (4, "review_ready")


def test_agent_failure_missing_response_and_input_change_remain_fail_closed(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    analyzed = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    assert prepare_review(analyzed.job_dir).exit_code == 4
    request = json.loads((analyzed.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (analyzed.job_dir / "agent-failure.json").write_text(json.dumps({
        "schema_version": "1.1", "job_id": request["job_id"], "source_sha256": request["source_sha256"],
        "glossary_sha256": request["glossary_sha256"], "request_sha256": request["request_sha256"],
        "status": "subagent_failed", "error_code": "agent_subagent_failed",
    }), encoding="utf-8")
    assert prepare_review(analyzed.job_dir).exit_code == 4
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(TechpackError) as caught:
        prepare_review(analyzed.job_dir)
    assert caught.value.code == "workflow_input_changed"


def test_apply_rejects_existing_exact_output_without_overwrite(tmp_path):
    source, review = tmp_path / "techpack.pdf", tmp_path / "review.json"
    _techpack_pdf(source)
    review.write_text("{}", encoding="utf-8")
    expected = source.with_name(source.name + ".annotated.pdf")
    expected.write_bytes(b"owned-by-user")
    with pytest.raises(TechpackError) as caught:
        apply(source, review, expected)
    assert caught.value.code == "output_exists"


def test_prepare_review_rejects_a_corrupt_state_snapshot(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "state.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        prepare_review(job_dir)

    assert caught.value.code == "workflow_state_invalid"


def test_directory_analysis_isolates_identical_pdfs_within_one_second(tmp_path):
    source_a = tmp_path / "a.pdf"
    source_b = tmp_path / "b.pdf"
    glossary = tmp_path / "terms.csv"
    _techpack_pdf(source_a)
    source_b.write_bytes(source_a.read_bytes())
    _glossary(glossary)

    batch = analyze(
        tmp_path,
        glossary,
        tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_MinerUFixture(),
    )

    assert [result.exit_code for result in batch.jobs] == [4, 4]
    assert batch.jobs[0].job_dir != batch.jobs[1].job_dir
    assert all((result.job_dir / "translation-request.json").is_file() for result in batch.jobs)
    assert batch.jobs[0].job_dir / "translation-request.json" != batch.jobs[1].job_dir / "translation-request.json"


def test_analysis_uses_hashed_job_snapshots_and_rejects_original_input_changes(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(
        source, glossary, tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_MutatingMinerUFixture(source, glossary),
    )

    assert result.exit_code == 2
    assert result.state == "failed"
    job_dir = next(path for path in (tmp_path / "jobs").iterdir() if path.name != ".techpack-pdf-trust")
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (job_dir / "input-source.pdf").is_file()
    assert (job_dir / "input-glossary.csv").is_file()
    assert hashlib.sha256((job_dir / "input-source.pdf").read_bytes()).hexdigest() == manifest["source"]["sha256"]
    assert hashlib.sha256((job_dir / "input-glossary.csv").read_bytes()).hexdigest() == manifest["glossary"]["sha256"]


def test_original_input_change_blocks_resume_but_restoring_manifest_bytes_recovers(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    original = source.read_bytes()
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    source.write_bytes(original + b"changed")
    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)
    assert caught.value.code == "workflow_input_changed"
    source.write_bytes(original)
    assert (prepare_review(job.job_dir).exit_code, prepare_review(job.job_dir).state) == (4, "translation_requested")


def test_analysis_rejects_snapshot_replacement_even_when_bytes_are_restored(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(source, glossary, tmp_path / "jobs", mineru_client=_SnapshotMutatingMinerUFixture())

    assert (result.exit_code, result.state) == (2, "failed")
    job_dir = next(path for path in (tmp_path / "jobs").iterdir() if path.name != ".techpack-pdf-trust")
    assert json.loads((job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "failed"


def test_non_techpack_analysis_exception_marks_its_created_job_failed_without_leaking(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_ExplodingMinerUFixture())

    assert (result.exit_code, result.state, result.input_index) == (2, "failed", 0)
    assert result.job_dir is not None
    job_dir = next(path for path in (tmp_path / "jobs").iterdir() if path.name != ".techpack-pdf-trust")
    assert result.job_dir == job_dir
    assert json.loads((job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "failed"


@pytest.mark.parametrize("failure_target", ["manifest.json", "state.json"])
def test_bootstrap_write_failure_retains_owned_job_identity_and_input_index(tmp_path, monkeypatch, failure_target):
    source, glossary = tmp_path / f"{failure_target}.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    root = tmp_path / "jobs"
    original = workflow._atomic_json_write

    def fail_bootstrap(path, value):
        if Path(path).name == failure_target:
            raise TechpackError("workflow_atomic_write_failed", "safe bootstrap failure", {})
        return original(path, value)

    monkeypatch.setattr(workflow, "_atomic_json_write", fail_bootstrap)
    result = analyze(
        source, glossary, root,
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        mineru_client=_MinerUFixture(),
    )

    assert (result.exit_code, result.state, result.input_index) == (2, "failed", 0)
    assert result.job_dir is not None and result.job_dir.is_absolute()
    assert result.job_dir.parent == root.resolve()
    assert result.to_dict()["job_dir"] == str(result.job_dir)


def test_native_only_bom_uses_native_evidence_and_marks_output_risk(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)

    result = analyze(
        source, glossary, tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_NativeOnlyFixture(),
    )

    assert (result.exit_code, result.state) == (4, "translation_requested")
    analysis = json.loads((result.job_dir / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["parser"] == "native_only"
    assert analysis["pages"][0]["page_type"] == "bom"
    assert analysis["pages"][0]["evidence"] == ["title:bill of materials"]
    request = json.loads((result.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (result.job_dir / "translation-response.json").write_text(json.dumps(_response_for_all(request)), encoding="utf-8")
    prepared = prepare_review(result.job_dir)
    assert prepared.state == "review_ready"
    expected = json.loads((result.job_dir / "expected-output.json").read_text(encoding="utf-8"))
    assert expected["output"]["items"][0]["risk_level"] == "high"
    assert "native_only_degradation" in expected["output"]["items"][0]["warnings"]


def test_prepare_accepts_explicit_null_agent_role_for_main_agent(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    response = _response_for(request)
    response["items"][0]["translator"]["agent_role"] = None
    (job.job_dir / "translation-response.json").write_text(json.dumps(response), encoding="utf-8")

    result = prepare_review(job.job_dir)

    assert result.state == "review_ready"
    expected = json.loads((job.job_dir / "expected-output.json").read_text(encoding="utf-8"))
    assert expected["output"]["items"][0]["translation_agent_role"] is None
    review_path = _write_approved_review(job.job_dir)
    output = source.with_name(source.name + ".annotated.pdf")

    applied = apply(source, review_path, output)

    assert (applied.exit_code, applied.state) == (0, "succeeded")
    assert output.is_file()


def test_expected_output_promotes_unknown_model_translation_warning_and_nonhigh_coordinate_risk(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    original = workflow._analysis_snapshot

    def with_medium_coordinate(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        candidate = snapshot.candidates[0].model_copy(update={"coordinate_confidence": CoordinateConfidence.MEDIUM})
        return snapshot.model_copy(update={"candidates": [candidate, *snapshot.candidates[1:]]})

    monkeypatch.setattr(workflow, "_analysis_snapshot", with_medium_coordinate)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    response = _response_for(request)
    response["items"][0]["warnings"] = ["translator_warning"]
    (job.job_dir / "translation-response.json").write_text(json.dumps(response), encoding="utf-8")

    assert prepare_review(job.job_dir).state == "review_ready"
    item = json.loads((job.job_dir / "expected-output.json").read_text(encoding="utf-8"))["output"]["items"][0]
    assert item["risk_level"] == "high"
    assert item["warnings"] == ["coordinate_confidence", "unknown_model", "translator_warning"]


def test_apply_failure_report_is_strictly_bound_and_redacts_problem_tree_content(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = job.job_dir / "review.json"
    review.write_text('{"secret":"DO-NOT-LEAK"}', encoding="utf-8")

    result = apply(source, review, source.with_name(source.name + ".annotated.pdf"))

    assert (result.exit_code, result.state) == (5, "failed")
    report = json.loads((job.job_dir / "apply-result.json").read_text(encoding="utf-8"))
    rendered = json.dumps(report, ensure_ascii=False)
    assert report["job_id"] == request["job_id"]
    assert report["problems"] == [{"code": "review_validation_failed", "status": None, "ownership": None, "resource_kind": None, "exists": None, "rollback": None, "nested": []}]
    assert "DO-NOT-LEAK" not in rendered
    assert not (job.job_dir / "trusted-review.json").exists()
    assert not list(job.job_dir.glob(".trusted-review.*.pending"))


def test_safe_problem_projection_preserves_operational_tree_without_paths_or_text():
    projected = workflow._safe_problem_tree({
        "code": "publish_rollback_failed", "message": "SECRET-TRANSLATION",
        "details": {
            "final_path": r"C:\\secret\\a.pdf", "final_exists": True, "ownership_mismatch": True,
            "cleanup_problem": {
                "code": "cleanup_failed", "message": "SECRET-NESTED",
                "details": {"temp_path": r"C:\\secret\\tmp.pdf", "temp_exists": False, "rollback": False},
            },
        },
    })

    value = projected.model_dump(mode="json")
    rendered = json.dumps(value)
    assert value["code"] == "publish_rollback_failed"
    assert value["ownership"] == "foreign_or_unknown"
    assert any(child["resource_kind"] == "final_output" and child["exists"] is True for child in value["nested"])
    assert any(child["code"] == "cleanup_failed" for child in value["nested"])
    assert "SECRET" not in rendered
    assert "C:\\secret" not in rendered


def test_apply_failure_report_marks_task8_retained_final_without_leaking_details(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    output = source.with_name(source.name + ".annotated.pdf")

    def retained_failure(*_args):
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(
            success=False, output_path=output,
            problems=({"code": "publish_rollback_failed", "message": "SECRET", "details": {"final_path": str(output), "final_exists": True, "ownership_mismatch": True}},),
        )

    monkeypatch.setattr(workflow, "apply_review", retained_failure)
    result = apply(source, review, output)

    assert (result.exit_code, result.state) == (5, "failed")
    report = json.loads((job.job_dir / "apply-result.json").read_text(encoding="utf-8"))
    assert report["final_output_retained"] is True
    assert report["problems"][0]["ownership"] == "foreign_or_unknown"
    assert "SECRET" not in json.dumps(report)


def test_apply_uses_immutable_bounded_review_snapshot_after_user_review_changes(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    original_load = workflow.load_review

    calls = []

    def load_then_mutate(path, *args):
        calls.append(os.path.basename(path))
        if len(calls) == 1:
            assert calls[0].startswith(".trusted-review.") and calls[0].endswith(".pending")
            assert not (job.job_dir / "trusted-review.json").exists()
        loaded = original_load(path, *args)
        if len(calls) == 1:
            review.write_text('{"changed":"untrusted"}', encoding="utf-8")
        return loaded

    monkeypatch.setattr(workflow, "load_review", load_then_mutate)
    output = source.with_name(source.name + ".annotated.pdf")
    result = apply(source, review, output)

    assert (result.exit_code, result.state) == (0, "succeeded")
    state = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    trusted = job.job_dir / "trusted-review.json"
    assert trusted.is_file()
    assert state["artifacts"]["review"] == hashlib.sha256(trusted.read_bytes()).hexdigest()
    assert len(calls) == 1 and calls[0].startswith(".trusted-review.")


def test_review_validation_hard_interruption_never_publishes_pending_as_trusted(tmp_path, monkeypatch):
    source, glossary = tmp_path / "validation-crash.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)

    def interrupt(path, *_args):
        assert Path(path).name.startswith(".trusted-review.")
        assert not (job.job_dir / "trusted-review.json").exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "load_review", interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(source, review, source.with_name(source.name + ".annotated.pdf"))

    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "review_ready"
    assert not (job.job_dir / "trusted-review.json").exists()
    assert not list(job.job_dir.glob(".trusted-review.*.pending"))


def test_review_completed_transition_failure_rolls_back_published_snapshot(tmp_path, monkeypatch):
    source, glossary = tmp_path / "transition-crash.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    original = workflow._write_state

    def interrupt(directory, manifest, state, *args):
        if state is workflow.WorkflowState.REVIEW_COMPLETED:
            raise KeyboardInterrupt
        return original(directory, manifest, state, *args)

    monkeypatch.setattr(workflow, "_write_state", interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(source, review, source.with_name(source.name + ".annotated.pdf"))

    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "review_ready"
    assert not (job.job_dir / "trusted-review.json").exists()
    assert not list(job.job_dir.glob(".trusted-review.*.pending"))


def test_review_ready_recovers_valid_published_snapshot_after_hard_crash(tmp_path, monkeypatch):
    source, glossary = tmp_path / "published-crash.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    pending = job.job_dir / ".trusted-review.0123456789abcdef0123456789abcdef.pending"
    pending.write_bytes(review.read_bytes())
    trusted = job.job_dir / "trusted-review.json"
    os.link(pending, trusted)
    review.unlink()
    state_before = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    assert state_before["state"] == "review_ready" and state_before["artifacts"]["review"] is None
    output = source.with_name(source.name + ".annotated.pdf")
    original_load = workflow.load_review
    validated = []

    def strict_load(path, *args):
        validated.append(Path(path).name)
        return original_load(path, *args)

    def succeed(*_args):
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(True, output)

    monkeypatch.setattr(workflow, "load_review", strict_load)
    monkeypatch.setattr(workflow, "apply_review", succeed)
    result = apply(source, review, output)

    assert (result.exit_code, result.state) == (0, "succeeded")
    assert validated == ["trusted-review.json"]
    final_state = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    assert final_state["artifacts"]["review"] == hashlib.sha256(trusted.read_bytes()).hexdigest()
    assert not pending.exists()


@pytest.mark.parametrize("existing_kind", ["invalid", "mismatched"])
def test_review_ready_preserves_unbound_existing_snapshot_and_waits_for_recovery(tmp_path, existing_kind):
    source, glossary = tmp_path / f"{existing_kind}.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    trusted = job.job_dir / "trusted-review.json"
    if existing_kind == "invalid":
        trusted.write_bytes(b"{}")
    else:
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["job_id"] = "foreign-job"
        trusted.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    review.unlink()
    trusted_before = trusted.read_bytes()
    state_before = (job.job_dir / "state.json").read_bytes()

    result = apply(source, review, source.with_name(source.name + ".annotated.pdf"))

    assert (result.exit_code, result.state, result.status, result.wait_reason) == (
        5, "review_ready", "recovery_required", "review_recovery",
    )
    assert trusted.read_bytes() == trusted_before
    assert (job.job_dir / "state.json").read_bytes() == state_before


def test_review_ready_reserved_reparse_snapshot_waits_without_terminalizing(tmp_path, monkeypatch):
    source, glossary = tmp_path / "reserved-reparse.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    trusted = job.job_dir / "trusted-review.json"
    trusted.write_bytes(review.read_bytes())
    trusted_before = trusted.read_bytes()
    state_before = (job.job_dir / "state.json").read_bytes()
    original_lstat = Path.lstat

    def reserved_reparse(path):
        details = original_lstat(path)
        if Path(path) == trusted:
            return SimpleNamespace(
                st_mode=details.st_mode,
                st_dev=details.st_dev,
                st_ino=details.st_ino,
                st_size=details.st_size,
                st_mtime_ns=details.st_mtime_ns,
                st_file_attributes=0x0400,
            )
        return details

    monkeypatch.setattr(Path, "lstat", reserved_reparse)
    result = apply(source, review, source.with_name(source.name + ".annotated.pdf"))

    assert (result.exit_code, result.state, result.status, result.wait_reason) == (
        5, "review_ready", "recovery_required", "review_recovery",
    )
    assert trusted.read_bytes() == trusted_before
    assert (job.job_dir / "state.json").read_bytes() == state_before


def test_apply_rejects_review_symlink_before_resolution(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    review = job.job_dir / "review.json"
    outside = tmp_path / "outside-review.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        os.symlink(outside, review)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")

    with pytest.raises(TechpackError) as caught:
        apply(source, review, source.with_name(source.name + ".annotated.pdf"))
    assert caught.value.code == "workflow_path_invalid"


def test_absolute_job_manifest_survives_cwd_change(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, glossary = workspace / "techpack.pdf", workspace / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    monkeypatch.chdir(workspace)
    job = analyze("techpack.pdf", "terms.csv", "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    monkeypatch.chdir(tmp_path)

    assert (prepare_review(job.job_dir).exit_code, prepare_review(job.job_dir).state) == (4, "translation_requested")
    manifest = json.loads((job.job_dir / "manifest.json").read_text(encoding="utf-8"))
    assert os.path.isabs(manifest["job_dir"])


def test_review_json_is_bounded_and_rejects_descriptor_identity_change(tmp_path, monkeypatch):
    review = tmp_path / "review.json"
    review.write_bytes(b"{" + b"x" * workflow._MAX_JSON_BYTES)
    with pytest.raises(TechpackError) as caught:
        workflow._bounded_binary_read(review)
    assert caught.value.code == "workflow_artifact_invalid"

    review.write_text('{"complete":true}', encoding="utf-8")
    original_fstat = workflow.os.fstat
    calls = 0

    def changed_identity(descriptor):
        nonlocal calls
        calls += 1
        details = original_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=details.st_dev, st_ino=details.st_ino + 1,
                st_size=details.st_size, st_mtime_ns=details.st_mtime_ns,
            )
        return details

    monkeypatch.setattr(workflow.os, "fstat", changed_identity)
    with pytest.raises(TechpackError) as caught:
        workflow._bounded_binary_read(review)
    assert caught.value.code == "workflow_artifact_invalid"


def test_reparse_attribute_helper_rejects_windows_reparse_points():
    fake = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x0400)
    assert workflow._is_reparse_or_link(fake)


def test_snapshot_digest_change_after_validation_fails_closed(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    original_load = workflow.load_review

    def load_then_tamper(path, *args):
        loaded = original_load(path, *args)
        Path(path).write_text('{"tampered":true}', encoding="utf-8")
        return loaded

    monkeypatch.setattr(workflow, "load_review", load_then_tamper)
    with pytest.raises(TechpackError) as caught:
        apply(source, review, source.with_name(source.name + ".annotated.pdf"))
    assert caught.value.code == "workflow_artifact_invalid"


def test_public_workflow_boundary_redacts_unexpected_error_and_marks_existing_job_failed(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    monkeypatch.setattr(workflow, "_trusted_output", lambda *_args: (_ for _ in ()).throw(RuntimeError(r"C:\\SECRET\\path")))

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_internal_error"
    assert "SECRET" not in str(caught.value)
    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "failed"


def test_state_revision_cas_rejects_same_revision_without_overwriting(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job_result = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    job = workflow._load_job(job_result.job_dir)
    state = workflow._load_state(job_result.job_dir, job)
    before = (job_result.job_dir / "state.json").read_text(encoding="utf-8")

    with pytest.raises(TechpackError) as caught:
        workflow._write_state(job_result.job_dir, job, workflow.WorkflowState.TRANSLATION_REQUESTED, state.revision, state.expected_attempt, "host_translation")

    assert caught.value.code == "workflow_state_conflict"
    assert (job_result.job_dir / "state.json").read_text(encoding="utf-8") == before


def test_applying_recovery_finishes_only_with_bound_success_report_and_retries_when_final_absent(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job_result = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job_result.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job_result.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job_result.job_dir).state == "review_ready"
    review = _write_approved_review(job_result.job_dir)
    job = workflow._load_job(job_result.job_dir)
    ready = workflow._load_state(job_result.job_dir, job)
    workflow._atomic_binary_write(job_result.job_dir / "trusted-review.json", review.read_bytes())
    workflow._write_state(job_result.job_dir, job, workflow.WorkflowState.REVIEW_COMPLETED, ready.revision + 1, ready.expected_attempt, None)
    completed = workflow._load_state(job_result.job_dir, job)
    workflow._write_state(job_result.job_dir, job, workflow.WorkflowState.APPLYING, completed.revision + 1, completed.expected_attempt, None)
    output = source.with_name(source.name + ".annotated.pdf")

    def retry_success(*_args):
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(True, output)

    monkeypatch.setattr(workflow, "apply_review", retry_success)
    assert (apply(source, review, output).exit_code, json.loads((job_result.job_dir / "state.json").read_text(encoding="utf-8"))["state"]) == (0, "succeeded")

    second_source, second_glossary = tmp_path / "b.pdf", tmp_path / "b.csv"
    _techpack_pdf(second_source)
    _glossary(second_glossary)
    second = analyze(second_source, second_glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 1, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    second_request = json.loads((second.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (second.job_dir / "translation-response.json").write_text(json.dumps(_response_for(second_request)), encoding="utf-8")
    assert prepare_review(second.job_dir).state == "review_ready"
    second_review = _write_approved_review(second.job_dir)
    second_job = workflow._load_job(second.job_dir)
    second_ready = workflow._load_state(second.job_dir, second_job)
    workflow._atomic_binary_write(second.job_dir / "trusted-review.json", second_review.read_bytes())
    workflow._write_state(second.job_dir, second_job, workflow.WorkflowState.REVIEW_COMPLETED, second_ready.revision + 1, second_ready.expected_attempt, None)
    second_completed = workflow._load_state(second.job_dir, second_job)
    workflow._write_state(second.job_dir, second_job, workflow.WorkflowState.APPLYING, second_completed.revision + 1, second_completed.expected_attempt, None)
    second_output = second_source.with_name(second_source.name + ".annotated.pdf")
    second_output.write_bytes(second_source.read_bytes())
    workflow._atomic_json_write(second.job_dir / "apply-result.json", workflow._apply_result_snapshot(
        second.job_dir, second_job, "succeeded", output_sha256=hashlib.sha256(second_output.read_bytes()).hexdigest(),
    ).model_dump(mode="json"))
    applying = workflow._load_state(second.job_dir, second_job)
    workflow._write_state(
        second.job_dir, second_job, workflow.WorkflowState.APPLYING,
        applying.revision + 1, applying.expected_attempt, None,
    )

    recovered = apply(second_source, second_review, second_output)
    assert (recovered.exit_code, recovered.state) == (0, "succeeded")


def test_concurrent_apply_has_one_winner_and_never_overwrites_final(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    output = source.with_name(source.name + ".annotated.pdf")
    entered, release = Event(), Event()
    calls = []

    def slow_success(*_args):
        calls.append("winner")
        entered.set()
        assert release.wait(5)
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(True, output)

    monkeypatch.setattr(workflow, "apply_review", slow_success)

    def invoke():
        try:
            return apply(source, review, output)
        except TechpackError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke)
        assert entered.wait(5)
        second = pool.submit(invoke)
        release.set()
        first_result, second_result = first.result(timeout=10), second.result(timeout=10)

    assert sum(isinstance(value, workflow.WorkflowResult) and value.exit_code == 0 for value in (first_result, second_result)) == 1
    busy = next(
        value for value in (first_result, second_result)
        if isinstance(value, workflow.WorkflowResult) and value.status == "workflow_busy"
    )
    assert (busy.exit_code, busy.state, busy.wait_reason) == (4, None, "concurrent_operation")
    assert calls == ["winner"]
    assert hashlib.sha256(output.read_bytes()).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()


def test_native_only_non_techpack_waits_for_host_visual_classification(tmp_path):
    source, glossary = tmp_path / "unknown.pdf", tmp_path / "terms.csv"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Commercial invoice")
    document.save(source)
    document.close()
    _glossary(glossary)

    result = analyze(
        source, glossary, tmp_path / "jobs",
        now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_NativeOnlyFixture(),
    )

    assert (result.exit_code, result.state, result.wait_reason) == (
        4,
        "parsed",
        "agent_classification",
    )
    assert (result.job_dir / "classification-request.json").is_file()
    assert not (result.job_dir / "translation-request.json").exists()


def test_batch_exit_priority_and_safe_input_indexes_preserve_independent_jobs(tmp_path, monkeypatch):
    source_a, source_b, source_c = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    glossary = tmp_path / "terms.csv"
    for source in (source_a, source_b, source_c):
        _techpack_pdf(source)
    _glossary(glossary)
    original = workflow._analyze_one

    def mixed(source, *args, **kwargs):
        if source.name == "a.pdf":
            raise TechpackError("mineru_unavailable", "safe")
        if source.name == "b.pdf":
            raise RuntimeError("unexpected")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(workflow, "_analyze_one", mixed)
    result = analyze(tmp_path, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())

    assert result.exit_code == 3
    assert result.state is None
    assert result.batch_status == "completed"
    assert "state" not in result.to_dict()
    assert result.to_dict()["batch_status"] == "completed"
    assert [(item.input_index, item.exit_code) for item in result.jobs] == [(0, 3), (1, 2), (2, 4)]
    assert result.jobs[2].job_dir is not None
    assert json.loads((result.jobs[2].job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "translation_requested"


def test_agent_failure_requires_exact_job_binding_and_cannot_regress_validated_state(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    failure = {
        "schema_version": "1.1", "job_id": request["job_id"], "source_sha256": request["source_sha256"],
        "glossary_sha256": request["glossary_sha256"], "request_sha256": request["request_sha256"],
        "status": "subagent_failed", "error_code": "agent_subagent_failed",
    }
    (job.job_dir / "agent-failure.json").write_text(json.dumps(failure), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "translation_requested"
    failure["job_id"] = "wrong-job"
    (job.job_dir / "agent-failure.json").write_text(json.dumps(failure), encoding="utf-8")
    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)
    assert caught.value.code == "workflow_binding_mismatch"


def test_agent_failure_cannot_move_a_persisted_translation_validated_job_backwards(tmp_path, monkeypatch):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request)), encoding="utf-8")
    monkeypatch.setattr(workflow, "_trusted_output", lambda *_args: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        workflow._prepare_review_locked(job.job_dir)
    before = json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))
    assert before["state"] == "translation_validated"
    failure = {
        "schema_version": "1.1", "job_id": request["job_id"], "source_sha256": request["source_sha256"],
        "glossary_sha256": request["glossary_sha256"], "request_sha256": request["request_sha256"],
        "status": "subagent_failed", "error_code": "agent_subagent_failed",
    }
    (job.job_dir / "agent-failure.json").write_text(json.dumps(failure), encoding="utf-8")
    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)
    assert caught.value.code == "workflow_state_conflict"
    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "translation_validated"


def test_stale_temporary_file_is_ignored_but_missing_bound_artifact_fails_closed(tmp_path):
    source, glossary = tmp_path / "techpack.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    (job.job_dir / ".state.json.interrupted.tmp").write_text("{not state", encoding="utf-8")
    assert prepare_review(job.job_dir).exit_code == 4
    (job.job_dir / "translation-request.json").unlink()
    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)
    assert caught.value.code == "workflow_artifact_invalid"


@pytest.mark.parametrize(
    ("state", "expected_attempt", "wait_reason", "artifacts"),
    [
        ("translation_requested", 1, "host_translation", {"analysis": "a" * 64, "request": "b" * 64}),
        ("translation_validated", 0, "human_review", {"analysis": "a" * 64, "request": "b" * 64, "response": "c" * 64}),
        ("review_ready", 0, None, {"analysis": "a" * 64, "request": "b" * 64, "response": "c" * 64, "expected_output": "d" * 64}),
    ],
)
def test_state_invariants_reject_contradictory_attempt_wait_reason_and_artifacts(tmp_path, state, expected_attempt, wait_reason, artifacts):
    directory = tmp_path / "job"
    directory.mkdir()
    payload = {
        "schema_version": "1.1", "job_id": "x", "source_sha256": "a" * 64,
        "glossary_sha256": "b" * 64, "state": state, "revision": 1,
        "expected_attempt": expected_attempt, "wait_reason": wait_reason,
        "artifacts": artifacts,
    }
    (directory / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TechpackError) as caught:
        prepare_review(directory)
    assert caught.value.code == "workflow_state_invalid"


def test_cross_job_response_swap_requires_bound_correction(tmp_path):
    source_a, source_b, glossary = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source_a)
    _techpack_pdf(source_b)
    _glossary(glossary)
    batch = analyze(tmp_path, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    first, second = batch.jobs
    first_request = json.loads((first.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (second.job_dir / "translation-response.json").write_text(json.dumps(_response_for(first_request)), encoding="utf-8")
    assert prepare_review(second.job_dir).exit_code == 4
    assert json.loads((second.job_dir / "state.json").read_text(encoding="utf-8"))["expected_attempt"] == 1
    assert json.loads((second.job_dir / "correction-request.json").read_text(encoding="utf-8"))["error_codes"] == ["response_binding_mismatch"]


def test_strict_state_rejects_extra_and_backward_snapshots(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    (directory / "state.json").write_text(json.dumps({"schema_version": "1.1", "job_id": "x", "source_sha256": "a" * 64, "glossary_sha256": "b" * 64, "state": "parsed", "revision": 1, "expected_attempt": 0, "wait_reason": None, "unexpected": True}), encoding="utf-8")
    with pytest.raises(TechpackError) as caught:
        prepare_review(directory)
    assert caught.value.code == "workflow_state_invalid"


def test_initialized_hard_interruption_resumes_same_job_to_translation_requested(tmp_path, monkeypatch):
    source, glossary = tmp_path / "initialized.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    original = workflow._snapshot_inputs

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "_snapshot_inputs", interrupt)
    with pytest.raises(KeyboardInterrupt):
        analyze(
            source, glossary, tmp_path / "jobs",
            now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
            mineru_client=_MinerUFixture(),
        )
    job_dir = next((tmp_path / "jobs").iterdir())
    assert json.loads((job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "initialized"

    monkeypatch.setattr(workflow, "_snapshot_inputs", original)
    monkeypatch.setattr(workflow, "MinerUClient", lambda: _MinerUFixture())
    resumed = prepare_review(job_dir)

    assert (resumed.exit_code, resumed.state, resumed.job_dir) == (4, "translation_requested", job_dir)
    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["revision"] == 2
    assert state["artifacts"]["analysis"] == hashlib.sha256((job_dir / "analysis.json").read_bytes()).hexdigest()
    assert state["artifacts"]["request"] == hashlib.sha256((job_dir / "translation-request.json").read_bytes()).hexdigest()


def test_parsed_hard_interruption_resumes_same_job_without_reanalysis(tmp_path, monkeypatch):
    source, glossary = tmp_path / "parsed.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    original = workflow._atomic_translation_request

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "_atomic_translation_request", interrupt)
    with pytest.raises(KeyboardInterrupt):
        analyze(
            source, glossary, tmp_path / "jobs",
            now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
            mineru_client=_MinerUFixture(),
        )
    job_dir = next((tmp_path / "jobs").iterdir())
    before_analysis = (job_dir / "analysis.json").read_bytes()
    assert json.loads((job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "parsed"

    monkeypatch.setattr(workflow, "_atomic_translation_request", original)
    monkeypatch.setattr(
        workflow,
        "inspect_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("parsed resume reanalyzed PDF")),
    )
    resumed = prepare_review(job_dir)

    assert (resumed.exit_code, resumed.state, resumed.job_dir) == (4, "translation_requested", job_dir)
    assert (job_dir / "analysis.json").read_bytes() == before_analysis
    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["revision"] == 2
    assert state["artifacts"]["request"] == hashlib.sha256((job_dir / "translation-request.json").read_bytes()).hexdigest()


def test_atomic_state_write_leaves_complete_previous_snapshot_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"complete":true}\n', encoding="utf-8")
    monkeypatch.setattr(workflow.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(TechpackError) as caught:
        workflow._atomic_json_write(path, {"complete": False})
    assert caught.value.code == "workflow_atomic_write_failed"
    assert path.read_text(encoding="utf-8") == '{"complete":true}\n'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_binary_mid_fsync_failure_cleans_exact_owned_temp(tmp_path, monkeypatch):
    path = tmp_path / "artifact.json"
    monkeypatch.setattr(workflow.os, "fsync", lambda *_args: (_ for _ in ()).throw(OSError("mid-fsync")))

    with pytest.raises(TechpackError) as caught:
        workflow._atomic_binary_write(path, b"partial")

    assert caught.value.code == "workflow_atomic_write_failed"
    assert not path.exists()
    assert not list(tmp_path.glob(".artifact.json.*.tmp"))


def test_atomic_replace_and_cleanup_failure_returns_combined_safe_code_without_foreign_delete(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"complete":true}\n', encoding="utf-8")
    monkeypatch.setattr(workflow.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup")))

    with pytest.raises(TechpackError) as caught:
        workflow._atomic_json_write(path, {"complete": False})

    assert caught.value.code == "workflow_atomic_write_cleanup_failed"
    assert path.read_text(encoding="utf-8") == '{"complete":true}\n'
    assert len(list(tmp_path.glob(".state.json.*.tmp"))) == 1


def test_atomic_cleanup_never_deletes_a_replaced_foreign_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"complete":true}\n', encoding="utf-8")

    def replace_with_foreign(temp, _target):
        Path(temp).unlink()
        Path(temp).write_text("foreign-owner", encoding="utf-8")
        raise OSError("replace")

    monkeypatch.setattr(workflow.os, "replace", replace_with_foreign)
    with pytest.raises(TechpackError) as caught:
        workflow._atomic_json_write(path, {"complete": False})

    assert caught.value.code == "workflow_atomic_write_cleanup_failed"
    leftovers = list(tmp_path.glob(".state.json.*.tmp"))
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "foreign-owner"


def test_stable_copy_never_deletes_a_replaced_foreign_temp_file(tmp_path, monkeypatch):
    source, target = tmp_path / "source.bin", tmp_path / "snapshot.bin"
    source.write_bytes(b"original")

    def replace_with_foreign(temp, _target):
        Path(temp).unlink()
        Path(temp).write_text("foreign-owner", encoding="utf-8")
        raise OSError("replace")

    monkeypatch.setattr(workflow.os, "replace", replace_with_foreign)
    with pytest.raises(TechpackError) as caught:
        workflow._stable_copy(source, target, hashlib.sha256(source.read_bytes()).hexdigest(), workflow._file_identity(source))

    assert caught.value.code == "workflow_input_snapshot_cleanup_failed"
    leftovers = list(tmp_path.glob(".snapshot.bin.*.tmp"))
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "foreign-owner"


def test_stable_copy_mid_fsync_failure_cleans_exact_owned_temp(tmp_path, monkeypatch):
    source, target = tmp_path / "source.bin", tmp_path / "snapshot.bin"
    source.write_bytes(b"original")
    monkeypatch.setattr(workflow.os, "fsync", lambda *_args: (_ for _ in ()).throw(OSError("mid-fsync")))

    with pytest.raises(TechpackError) as caught:
        workflow._stable_copy(
            source, target, hashlib.sha256(source.read_bytes()).hexdigest(), workflow._file_identity(source),
        )

    assert caught.value.code == "workflow_input_unavailable"
    assert not target.exists()
    assert not list(tmp_path.glob(".snapshot.bin.*.tmp"))


def test_canonical_request_cleanup_never_deletes_replaced_foreign_temp(tmp_path, monkeypatch):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    result = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    job = workflow._load_job(result.job_dir)
    analysis = workflow._load_model(result.job_dir, "analysis.json", workflow._AnalysisSnapshot)
    original = workflow.write_translation_request

    def replace_after_write(candidates, path, manifest):
        payload = original(candidates, path, manifest)
        Path(path).unlink()
        Path(path).write_text("foreign-owner", encoding="utf-8")
        return payload

    monkeypatch.setattr(workflow, "write_translation_request", replace_after_write)
    with pytest.raises(TechpackError) as caught:
        workflow._canonical_request_bytes(result.job_dir, analysis, job)

    assert caught.value.code == "workflow_binding_mismatch"
    leftovers = list(result.job_dir.glob(".canonical-request.*.tmp"))
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "foreign-owner"


def test_canonical_request_mid_write_failure_cleans_exact_owned_temp(tmp_path, monkeypatch):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    result = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    job = workflow._load_job(result.job_dir)
    analysis = workflow._load_model(result.job_dir, "analysis.json", workflow._AnalysisSnapshot)

    def fail_mid_write(_candidates, path, _manifest):
        Path(path).write_bytes(b"partial")
        raise TechpackError("translation_request_write_failed", "safe failure", {})

    monkeypatch.setattr(workflow, "write_translation_request", fail_mid_write)
    with pytest.raises(TechpackError) as caught:
        workflow._canonical_request_bytes(result.job_dir, analysis, job)

    assert caught.value.code == "workflow_binding_mismatch"
    assert not list(result.job_dir.glob(".canonical-request.*.tmp"))


def test_parent_directory_fsync_is_attempted_when_supported_and_ignored_when_unavailable(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(workflow.os, "name", "posix")
    monkeypatch.setattr(workflow.os, "open", lambda *_args: 71)
    monkeypatch.setattr(workflow.os, "fsync", lambda descriptor: calls.append(descriptor))
    monkeypatch.setattr(workflow.os, "close", lambda descriptor: calls.append(-descriptor))
    workflow._fsync_parent(tmp_path)
    assert calls == [71, -71]

    monkeypatch.setattr(workflow.os, "open", lambda *_args: (_ for _ in ()).throw(OSError("unsupported")))
    workflow._fsync_parent(tmp_path)


def test_workflow_json_read_rejects_escape_and_oversize_artifacts(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(TechpackError):
        workflow._read_json(job, "../outside.json")
    (job / "state.json").write_text('{"secret":"DO-NOT-LEAK"}', encoding="utf-8")
    monkeypatch.setattr(workflow, "_MAX_JSON_BYTES", 2)
    with pytest.raises(TechpackError) as caught:
        workflow._read_json(job, "state.json")
    assert "DO-NOT-LEAK" not in str(caught.value)


def test_expected_output_snapshot_rejects_arbitrary_untrusted_mapping():
    with pytest.raises(ValidationError):
        workflow._ExpectedOutputSnapshot.model_validate({
            "schema_version": "1.1", "job_id": "job", "source_sha256": "a" * 64,
            "glossary_sha256": "b" * 64, "output": {"untrusted_translation": "do not accept"},
        })


def test_public_operations_expose_a_per_job_exclusive_lock_boundary(tmp_path):
    assert hasattr(workflow, "_job_lock")
    with workflow._job_lock(tmp_path):
        assert (tmp_path / ".workflow.lock").exists()


def test_busy_job_lock_returns_waiting_without_mutating_state(tmp_path, monkeypatch):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    before = (job.job_dir / "state.json").read_bytes()
    acquired, release = Event(), Event()
    def hold_lock():
        with workflow._job_lock(job.job_dir):
            acquired.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        assert acquired.wait(3)
        try:
            contender = executor.submit(prepare_review, job.job_dir)
            started = time.monotonic()
            result = contender.result(timeout=2)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        holder.result(timeout=2)

    assert elapsed < 0.5
    assert (result.exit_code, result.state, result.status, result.wait_reason) == (
        4, None, "workflow_busy", "concurrent_operation",
    )
    assert "state" not in result.to_dict()
    assert (job.job_dir / "state.json").read_bytes() == before


def test_job_lock_rejects_preexisting_symlink_without_touching_its_target(tmp_path):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    target = tmp_path / "external-lock"
    target.write_bytes(b"external")
    lock = job.job_dir / ".workflow.lock"
    try:
        lock.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows runner")

    with pytest.raises(TechpackError):
        prepare_review(job.job_dir)

    assert target.read_bytes() == b"external"


def test_applying_recovery_rejects_unbound_success_report_even_with_matching_pdf(tmp_path):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review_path = _write_approved_review(job.job_dir)
    trusted = job.job_dir / "trusted-review.json"
    trusted.write_bytes(review_path.read_bytes())
    manifest = workflow._load_job(job.job_dir)
    state = workflow._load_state(job.job_dir, manifest)
    workflow._write_state(job.job_dir, manifest, workflow.WorkflowState.REVIEW_COMPLETED, state.revision + 1, 0, None)
    state = workflow._load_state(job.job_dir, manifest)
    workflow._write_state(job.job_dir, manifest, workflow.WorkflowState.APPLYING, state.revision + 1, 0, None)
    output = source.with_name(source.name + ".annotated.pdf")
    _techpack_pdf(output)
    fake = {
        "schema_version": "1.1", "job_id": manifest.job_id,
        "source_sha256": manifest.source.sha256, "glossary_sha256": manifest.glossary.sha256,
        "expected_output_sha256": workflow._sha256_artifact(job.job_dir, "expected-output.json"),
        "status": "succeeded", "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "final_output_retained": False, "problems": [], "unresolved": [], "unresolved_overlap_count": 0,
    }
    (job.job_dir / "apply-result.json").write_text(json.dumps(fake), encoding="utf-8")

    result = apply(source, review_path, output)

    assert (result.exit_code, result.state, result.status, result.wait_reason) == (
        5, "applying", "recovery_required", "apply_recovery",
    )
    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "applying"


def test_review_ready_fails_closed_when_bound_review_html_is_missing(tmp_path):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    (job.job_dir / "review.html").unlink()

    with pytest.raises(TechpackError) as caught:
        prepare_review(job.job_dir)

    assert caught.value.code == "workflow_artifact_invalid"


def test_safe_apply_problem_projector_maps_task8_kind_without_retaining_content():
    projected = workflow._safe_problem_tree({
        "code": "publish_failed",
        "details": {"kind": "temporary", "exists": True, "message": "DO-NOT-LEAK"},
    })

    assert projected.resource_kind == "temporary"
    assert projected.exists is True
    assert "DO-NOT-LEAK" not in projected.model_dump_json()


def test_real_approved_review_applies_editable_freetext_and_writes_bound_success_report(tmp_path):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", now=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8")
    assert prepare_review(job.job_dir).state == "review_ready"
    review_path = _write_approved_review(job.job_dir)
    output = source.with_name(source.name + ".annotated.pdf")

    result = apply(source, review_path, output)

    assert (result.exit_code, result.state) == (0, "succeeded"), result
    assert output.is_file()
    document = pymupdf.open(output)
    try:
        assert any(annotation.type[1] == "FreeText" for annotation in document[0].annots())
    finally:
        document.close()
    report = json.loads((job.job_dir / "apply-result.json").read_text(encoding="utf-8"))
    assert report["status"] == "succeeded"
    assert report["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_lightweight_integrity_creates_no_secret_or_external_trust_artifact(tmp_path):
    source, glossary = tmp_path / "a.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    result = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    directory = result.job_dir

    assert not (tmp_path / "jobs" / ".techpack-pdf-trust").exists()
    assert not list(tmp_path.rglob("*.trust.json"))
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert "trust_hmac" not in state


def test_review_completed_resume_uses_single_snapshot_without_original_review(tmp_path, monkeypatch):
    source, glossary = tmp_path / "resume.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job_result = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job_result.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job_result.job_dir / "translation-response.json").write_text(
        json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8",
    )
    assert prepare_review(job_result.job_dir).state == "review_ready"
    review = _write_approved_review(job_result.job_dir)
    job = workflow._load_job(job_result.job_dir)
    ready = workflow._load_state(job_result.job_dir, job)
    workflow._atomic_binary_write(job_result.job_dir / "trusted-review.json", review.read_bytes())
    workflow._write_state(
        job_result.job_dir, job, workflow.WorkflowState.REVIEW_COMPLETED,
        ready.revision + 1, ready.expected_attempt, None,
    )
    review.unlink()
    output = source.with_name(source.name + ".annotated.pdf")

    def succeed(*_args):
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(True, output)

    monkeypatch.setattr(workflow, "apply_review", succeed)
    result = apply(source, review, output)

    assert (result.exit_code, result.state) == (0, "succeeded")


def test_apply_detects_trusted_review_change_during_task8(tmp_path, monkeypatch):
    source, glossary = tmp_path / "mutating.pdf", tmp_path / "terms.csv"
    _techpack_pdf(source)
    _glossary(glossary)
    job = analyze(source, glossary, tmp_path / "jobs", mineru_client=_MinerUFixture())
    request = json.loads((job.job_dir / "translation-request.json").read_text(encoding="utf-8"))
    (job.job_dir / "translation-response.json").write_text(
        json.dumps(_response_for(request), ensure_ascii=False), encoding="utf-8",
    )
    assert prepare_review(job.job_dir).state == "review_ready"
    review = _write_approved_review(job.job_dir)
    output = source.with_name(source.name + ".annotated.pdf")

    def mutate_snapshot(_source, trusted_review, *_args):
        Path(trusted_review).write_bytes(b"{}")
        output.write_bytes(source.read_bytes())
        return workflow.ApplyResult(True, output)

    monkeypatch.setattr(workflow, "apply_review", mutate_snapshot)
    with pytest.raises(TechpackError) as caught:
        apply(source, review, output)

    assert caught.value.code == "workflow_artifact_invalid"
    assert json.loads((job.job_dir / "state.json").read_text(encoding="utf-8"))["state"] == "failed"
    report = json.loads((job.job_dir / "apply-result.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["final_output_retained"] is True


def _request_hash(payload):
    material = dict(payload)
    material.pop("request_sha256", None)
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
