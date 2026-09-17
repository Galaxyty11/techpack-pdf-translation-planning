from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import techpack_pdf.review_preview as review_preview
import techpack_pdf.workflow as workflow
from techpack_pdf.apply import ApplyResult
from techpack_pdf.errors import TechpackError
from techpack_pdf.models import FileArtifact, JobManifest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_apply_resume_never_rebuilds_the_pre_review_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    glossary = tmp_path / "terms.csv"
    source.write_bytes(b"source-placeholder")
    glossary.write_text("source_term,target_term\n", encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    review_path = job_dir / "review.json"
    review_path.write_text("{}", encoding="utf-8")
    output = source.with_name(source.name + ".annotated.pdf")
    job = JobManifest(
        job_id="job-1",
        source=FileArtifact(
            filename=source.name,
            sha256=_sha256(source),
            path=source,
            page_count=1,
        ),
        glossary=FileArtifact(
            filename=glossary.name,
            sha256=_sha256(glossary),
            path=glossary,
        ),
        job_dir=job_dir,
        created_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    state = SimpleNamespace(
        state=workflow.WorkflowState.APPLYING,
        artifacts=SimpleNamespace(apply_result="a" * 64),
    )
    expected = SimpleNamespace(output=object())

    monkeypatch.setattr(workflow, "_job_directory", lambda _path: job_dir)
    monkeypatch.setattr(workflow, "_load_job", lambda _directory: job)
    monkeypatch.setattr(workflow, "_load_state", lambda _directory, _job: state)
    monkeypatch.setattr(workflow, "_verify_bound_inputs", lambda *_args: None)
    monkeypatch.setattr(workflow, "_assert_snapshot_binding", lambda *_args: None)

    def load_model(_directory: Path, name: str, _model: object) -> object:
        if name == "expected-output.json":
            return expected
        raise TechpackError("missing_report", "test recovery stop", {})

    monkeypatch.setattr(workflow, "_load_model", load_model)

    def forbidden_closure(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "post-review apply must not rebuild translation or automatic layout"
        )

    monkeypatch.setattr(workflow, "_verify_apply_integrity_closure", forbidden_closure, raising=False)

    result = workflow._apply_locked(source, review_path, output)

    assert result.exit_code == 5
    assert result.state is workflow.WorkflowState.APPLYING
    assert result.status == "recovery_required"
    assert result.wait_reason == "apply_recovery"


def test_review_ready_apply_uses_frozen_review_without_any_layout_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    glossary = tmp_path / "terms.csv"
    source.write_bytes(b"source-placeholder")
    glossary.write_text("source_term,target_term\n", encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    review_path = job_dir / "review.json"
    review_path.write_text("{}", encoding="utf-8")
    output = source.with_name(source.name + ".annotated.pdf")
    review_digest = _sha256(review_path)
    job = JobManifest(
        job_id="job-1",
        source=FileArtifact(
            filename=source.name,
            sha256=_sha256(source),
            path=source,
            page_count=1,
        ),
        glossary=FileArtifact(
            filename=glossary.name,
            sha256=_sha256(glossary),
            path=glossary,
        ),
        job_dir=job_dir,
        created_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
    )

    def state(value: workflow.WorkflowState, revision: int) -> SimpleNamespace:
        return SimpleNamespace(
            state=value,
            revision=revision,
            expected_attempt=1,
            artifacts=SimpleNamespace(
                review=review_digest if value is not workflow.WorkflowState.REVIEW_READY else None,
                apply_result=None,
            ),
        )

    states = iter(
        (
            state(workflow.WorkflowState.REVIEW_READY, 1),
            state(workflow.WorkflowState.REVIEW_COMPLETED, 2),
            state(workflow.WorkflowState.APPLYING, 3),
            state(workflow.WorkflowState.APPLYING, 4),
        )
    )
    expected = SimpleNamespace(output=[])

    monkeypatch.setattr(workflow, "_job_directory", lambda _path: job_dir)
    monkeypatch.setattr(workflow, "_load_job", lambda _directory: job)
    monkeypatch.setattr(workflow, "_load_state", lambda _directory, _job: next(states))
    monkeypatch.setattr(workflow, "_verify_bound_inputs", lambda *_args: None)
    monkeypatch.setattr(workflow, "_assert_snapshot_binding", lambda *_args: None)
    monkeypatch.setattr(workflow, "_load_model", lambda *_args: expected)
    monkeypatch.setattr(workflow, "load_review", lambda *_args: None)
    monkeypatch.setattr(workflow, "_write_state", lambda *_args: None)
    monkeypatch.setattr(workflow, "_atomic_json_write", lambda *_args: None)
    monkeypatch.setattr(
        workflow,
        "_apply_result_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(model_dump=lambda **_kw: {}),
    )

    def publish_review(pending: Path, trusted: Path, *_args: object) -> None:
        trusted.write_bytes(pending.read_bytes())

    monkeypatch.setattr(workflow, "_publish_review_no_clobber", publish_review)

    def apply_frozen_review(
        _source: Path, _review: Path, _job: JobManifest, _items: object
    ) -> ApplyResult:
        output.write_bytes(b"annotated-pdf-placeholder")
        return ApplyResult(success=True, output_path=output)

    monkeypatch.setattr(workflow, "apply_review", apply_frozen_review)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("post-review apply must use the frozen human-reviewed layout")

    monkeypatch.setattr(workflow, "_verify_apply_integrity_closure", forbidden, raising=False)
    monkeypatch.setattr(workflow, "_trusted_output", forbidden)
    monkeypatch.setattr(workflow, "plan_review_items", forbidden)
    monkeypatch.setattr(review_preview, "plan_document_layout", forbidden)

    result = workflow._apply_locked(source, review_path, output)

    assert result.exit_code == 0
    assert result.state is workflow.WorkflowState.SUCCEEDED
    assert output.read_bytes() == b"annotated-pdf-placeholder"
