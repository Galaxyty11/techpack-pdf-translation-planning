import json
import hashlib
from datetime import datetime, timezone

import pymupdf
import pytest

from techpack_pdf.errors import TechpackError
import techpack_pdf.workflow as workflow
from techpack_pdf.workflow import analyze, apply, prepare_review


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


def _techpack_pdf(path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "BOM")
    page.insert_text((72, 86), "Shell 12 mm")
    document.save(path)
    document.close()


def _glossary(path):
    path.write_text("source_term,target_term\nShell,大身\n", encoding="utf-8")


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
    (analyzed.job_dir / "agent-failure.json").write_text(json.dumps({"schema_version": "1.1", "status": "subagent_failed", "error_code": "safe"}), encoding="utf-8")
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
    assert caught.value.code == "output_invalid"


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


def test_atomic_state_write_leaves_complete_previous_snapshot_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"complete":true}\n', encoding="utf-8")
    monkeypatch.setattr(workflow.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        workflow._atomic_json_write(path, {"complete": False})
    assert path.read_text(encoding="utf-8") == '{"complete":true}\n'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


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


def _request_hash(payload):
    material = dict(payload)
    material.pop("request_sha256", None)
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
