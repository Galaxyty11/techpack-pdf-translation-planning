from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pymupdf
import pytest

from techpack_pdf.glossary import load_glossary


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "golden" / "manifest.json"
EXPECTED_SCOPES = {
    "1802288": "measurement_only",
    "1805466": "full_techpack",
    "1805491": "full_techpack",
    "1806093": "full_techpack",
    "1806119": "full_techpack",
}


def _configured_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for production-sample acceptance")
    return Path(value).resolve(strict=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_count(path: Path) -> int:
    with pymupdf.open(path) as document:
        return document.page_count


def test_task11_manifest_binds_authorized_sample_inputs() -> None:
    sample_root = _configured_path("TECHPACK_TASK11_SAMPLE_ROOT")
    glossary_path = _configured_path("TECHPACK_TASK11_GLOSSARY")

    assert MANIFEST_PATH.is_file(), "Task 11 production golden manifest is missing"
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "1.0"
    assert set(manifest["samples"]) == set(EXPECTED_SCOPES)
    assert manifest["glossary"] == {
        "filename": glossary_path.name,
        "sha256": _sha256(glossary_path),
        "entry_count": len(load_glossary(glossary_path).entries),
    }

    for sample_id, scope in EXPECTED_SCOPES.items():
        sample = manifest["samples"][sample_id]
        assert sample["scope"] == scope
        for role, directory in (
            ("source", "source"),
            ("translated_reference", "translated-reference"),
        ):
            expected = sample[role]
            assert Path(expected["filename"]).name == expected["filename"]
            pdf_path = (sample_root / directory / expected["filename"]).resolve(strict=True)
            assert pdf_path.parent == (sample_root / directory).resolve(strict=True)
            assert expected["sha256"] == _sha256(pdf_path)
            assert expected["page_count"] == _page_count(pdf_path)


def test_task11_production_runs_reach_verified_succeeded_state() -> None:
    run_root = _configured_path("TECHPACK_TASK11_RUN_ROOT")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert set(manifest.get("runs", {})) == set(EXPECTED_SCOPES)
    for sample_id, run in manifest["runs"].items():
        sample_directory = Path(run["directory"])
        job_directory = Path(run["job_directory"])
        assert not sample_directory.is_absolute() and len(sample_directory.parts) == 1
        assert not job_directory.is_absolute() and len(job_directory.parts) == 1

        directory = (run_root / sample_directory).resolve(strict=True)
        job = (directory / "jobs" / job_directory).resolve(strict=True)
        source_filename = manifest["samples"][sample_id]["source"]["filename"]
        source = (directory / "input" / source_filename).resolve(strict=True)
        output = source.with_name(source.name + ".annotated.pdf")
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        apply_result = json.loads((job / "apply-result.json").read_text(encoding="utf-8"))

        assert state["state"] == "succeeded"
        assert state["job_id"] == run["job_id"] == apply_result["job_id"]
        assert apply_result["status"] == "succeeded"
        assert output.is_file()
        assert apply_result["output_sha256"] == _sha256(output)
        assert _page_count(output) == manifest["samples"][sample_id]["source"]["page_count"]
