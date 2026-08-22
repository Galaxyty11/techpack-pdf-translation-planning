from datetime import datetime, timezone
from hashlib import sha256

import pytest

from techpack_pdf.inputs import create_job, enumerate_pdfs, sha256_file


def test_directory_enumeration_is_shallow_and_deterministic(tmp_path):
    (tmp_path / "B.PDF").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "ignored.pdf").write_bytes(b"%PDF-1.4\n")

    assert [path.name for path in enumerate_pdfs(tmp_path)] == ["a.pdf", "B.PDF"]


def test_enumeration_rejects_empty_directory_and_non_pdf(tmp_path):
    with pytest.raises(ValueError, match="no PDF"):
        enumerate_pdfs(tmp_path)

    text_file = tmp_path / "notes.txt"
    text_file.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(ValueError, match="PDF"):
        enumerate_pdfs(text_file)


def test_sha256_file_is_repeatable_with_hand_derived_digest(tmp_path):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"abc")

    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_file(source) == expected
    assert sha256_file(source) == expected


def test_create_job_uses_source_digest_and_utc_timestamp(tmp_path):
    source = tmp_path / "source.pdf"
    glossary = tmp_path / "terms.csv"
    source.write_bytes(b"abc")
    glossary.write_text("source_term,target_term\nhem,下摆\n", encoding="utf-8")
    job_root = tmp_path / "jobs"
    local_now = datetime(2026, 8, 22, 20, 30, 15, tzinfo=timezone.utc)

    job = create_job(source, glossary, job_root, local_now)

    assert job.job_id == "ba7816bf8f01-20260822T203015Z"
    assert job.source.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert job.glossary.sha256 == sha256(glossary.read_bytes()).hexdigest()
    assert job.job_dir == job_root / "ba7816bf8f01-20260822T203015Z"
    assert job.job_dir.is_dir()


def test_create_job_refuses_to_reuse_an_existing_job_directory(tmp_path):
    source = tmp_path / "source.pdf"
    glossary = tmp_path / "terms.csv"
    source.write_bytes(b"abc")
    glossary.write_text("source_term,target_term\nhem,下摆\n", encoding="utf-8")
    now = datetime(2026, 8, 22, 20, 30, 15, tzinfo=timezone.utc)

    create_job(source, glossary, tmp_path / "jobs", now)
    with pytest.raises(FileExistsError):
        create_job(source, glossary, tmp_path / "jobs", now)
