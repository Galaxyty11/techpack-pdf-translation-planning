import json

import pytest

import techpack_pdf_cli
from techpack_pdf.errors import TechpackError
from techpack_pdf_cli import main
from techpack_pdf.workflow import WorkflowResult


@pytest.mark.parametrize("flag", ["--skip-review", "--ignore-hash", "--force-overlap", "--flatten", "--overwrite"])
def test_cli_rejects_forbidden_bypass_flags(capsys, flag):
    assert main(["analyze", "input.pdf", "--glossary", "terms.csv", "--job-dir", "jobs", flag]) == 2

    result = json.loads(capsys.readouterr().err)
    assert result == {"code": "input_error", "status": "failed"}


def test_cli_rejects_apply_output_that_is_not_the_task8_final_path(capsys, tmp_path):
    source = tmp_path / "a.pdf"
    review = tmp_path / "review.json"
    source.write_bytes(b"%PDF-1.4\n")
    review.write_text("{}", encoding="utf-8")

    assert main(["apply", str(source), "--review", str(review), "--output", str(tmp_path / "other.pdf")]) == 2
    assert json.loads(capsys.readouterr().err) == {"code": "output_invalid", "status": "failed"}


def test_cli_reports_preexisting_exact_final_as_output_exists(capsys, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TechpackError("output_exists", "must not overwrite", {})

    monkeypatch.setattr(techpack_pdf_cli, "apply", fail)
    assert main(["apply", "a.pdf", "--review", "review.json", "--output", "a.pdf.annotated.pdf"]) == 2
    assert json.loads(capsys.readouterr().err) == {"code": "output_exists", "status": "failed"}


@pytest.mark.parametrize(("code", "expected"), [("mineru_unavailable", 3), ("workflow_quality_failed", 5), ("workflow_input_changed", 2)])
def test_cli_maps_safe_error_categories_without_traceback(capsys, monkeypatch, code, expected):
    def fail(*_args, **_kwargs):
        raise TechpackError(code, "SECRET-DO-NOT-LEAK", {"error_code": code})

    monkeypatch.setattr(techpack_pdf_cli, "analyze", fail)
    assert main(["analyze", "input.pdf", "--glossary", "terms.csv", "--job-dir", "jobs"]) == expected
    rendered = capsys.readouterr().err
    assert "SECRET-DO-NOT-LEAK" not in rendered
    assert json.loads(rendered)["code"] == code


def test_cli_catches_unexpected_exception_as_one_safe_json_result(capsys, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError(r"C:\\SECRET\\path and traceback must not leak")

    monkeypatch.setattr(techpack_pdf_cli, "analyze", fail)
    assert main(["analyze", "input.pdf", "--glossary", "terms.csv", "--job-dir", "jobs"]) == 2
    rendered = capsys.readouterr().err
    assert json.loads(rendered) == {"code": "internal_error", "status": "failed"}
    assert "SECRET" not in rendered
    assert "Traceback" not in rendered


def test_cli_emits_busy_as_status_without_inventing_workflow_state(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(
        techpack_pdf_cli,
        "prepare_review",
        lambda *_args: WorkflowResult(
            4, None, tmp_path / "job", status="workflow_busy", wait_reason="concurrent_operation",
        ),
    )

    assert main(["prepare-review", "--job", str(tmp_path / "job")]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "workflow_busy"
    assert payload["wait_reason"] == "concurrent_operation"
    assert "state" not in payload


def test_workflow_result_rejects_non_state_status_in_state_field():
    with pytest.raises(ValueError):
        WorkflowResult(4, "workflow_busy")


def test_cli_includes_absolute_owned_job_directory_when_bootstrap_failed(capsys, monkeypatch, tmp_path):
    job_dir = (tmp_path / "jobs" / "safe-job-id").resolve()
    monkeypatch.setattr(
        techpack_pdf_cli,
        "analyze",
        lambda *_args: WorkflowResult(2, "failed", job_dir, input_index=0),
    )

    assert main(["analyze", "input.pdf", "--glossary", "terms.csv", "--job-dir", "jobs"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == "safe-job-id"
    assert payload["job_dir"] == str(job_dir)
    assert payload["input_index"] == 0
