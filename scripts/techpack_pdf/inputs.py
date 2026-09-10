"""Input validation and deterministic job creation."""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .models import FileArtifact, JobManifest


_CHUNK_SIZE = 1024 * 1024


def enumerate_pdfs(path: Path) -> list[Path]:
    """Return a single PDF or immediate directory PDFs in stable order."""
    path = Path(path)
    if path.is_file():
        if path.suffix.casefold() != ".pdf":
            raise ValueError("input must be a PDF file")
        return [path]
    if not path.is_dir():
        raise ValueError("input must be a PDF file or directory")

    pdfs = [entry for entry in path.iterdir() if entry.is_file() and entry.suffix.casefold() == ".pdf"]
    if not pdfs:
        raise ValueError("directory contains no PDF files")
    return sorted(pdfs, key=lambda entry: (entry.name.casefold(), entry.name))


def sha256_file(path: Path) -> str:
    """Hash a file in fixed 1 MiB chunks."""
    digest = sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def create_job(source: Path, glossary: Path, job_root: Path, now: datetime) -> JobManifest:
    """Create a fresh job directory with hashes bound to its two inputs."""
    source = Path(source)
    glossary = Path(glossary)
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise ValueError("source must be an existing PDF file")
    if not glossary.is_file() or glossary.suffix.casefold() not in {".csv", ".xlsx"}:
        raise ValueError("glossary must be an existing CSV or XLSX file")

    source_hash = sha256_file(source)
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_id = f"{source_hash[:12]}-{timestamp}"
    job_dir = Path(job_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    return JobManifest(
        job_id=job_id,
        source=FileArtifact(filename=source.name, sha256=source_hash, path=source),
        glossary=FileArtifact(filename=glossary.name, sha256=sha256_file(glossary), path=glossary),
        job_dir=job_dir,
        created_at=now.astimezone(timezone.utc),
    )
