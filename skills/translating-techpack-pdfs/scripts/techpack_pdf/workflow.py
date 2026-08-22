"""Resumable, fail-closed orchestration for the TechPack PDF pipeline."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .apply import ApplyResult, apply_review
from .errors import TechpackError
from .glossary import Glossary, load_glossary
from .inputs import create_job, enumerate_pdfs, sha256_file
from .matching import MatchedNode, match_nodes
from .mineru import MinerUClient, NativeOnlyDegradation
from .models import (
    CoordinateConfidence,
    DecisionReason,
    FileArtifact,
    JobManifest,
    PageType,
)
from .pdf_analysis import PdfManifest, inspect_pdf
from .review import build_review_html, load_review
from .selection import PageFeatures, PageNode, SelectionPage, classify_page, select_candidates
from .translation import TranslationValidationError, validate_translation_response, write_translation_request


_SCHEMA_VERSION = "1.1"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_STATES = (
    "initialized",
    "parsed",
    "translation_requested",
    "translation_validated",
    "review_ready",
    "review_completed",
    "applying",
    "succeeded",
    "failed",
)
_TECHPACK_TYPES = frozenset(PageType) - {PageType.UNKNOWN}


class WorkflowState(StrEnum):
    INITIALIZED = "initialized"
    PARSED = "parsed"
    TRANSLATION_REQUESTED = "translation_requested"
    TRANSLATION_VALIDATED = "translation_validated"
    REVIEW_READY = "review_ready"
    REVIEW_COMPLETED = "review_completed"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StateSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: WorkflowState
    revision: int = Field(ge=0)
    expected_attempt: Literal[0, 1]
    wait_reason: Literal["host_translation", "host_correction", "agent_failure", "human_review_required", "human_review"] | None


class _AnalysisNode(_StrictModel):
    text: str = Field(min_length=1)
    bbox: list[float] = Field(min_length=4, max_length=4)
    field_role: str = Field(min_length=1)


class _AnalysisPage(_StrictModel):
    page_index: int = Field(ge=0)
    page_type: PageType
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    thumbnail: str = Field(min_length=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    nodes: list[_AnalysisNode]


class _CandidateSnapshot(_StrictModel):
    item_id: str = Field(pattern=r"^p[0-9]{3}-i[0-9]{3}$")
    page_index: int = Field(ge=0)
    page_type: PageType
    classification_confidence: float = Field(ge=0, le=1)
    classification_evidence: list[str]
    source_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    source_bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    source_kind: str = Field(min_length=1)
    coordinate_confidence: CoordinateConfidence
    source_auto_approvable: bool
    auto_approvable: bool
    should_translate: bool
    decision_reason: DecisionReason
    locked_tokens: list[str]
    glossary_hits: list[dict[str, Any]]


class _AnalysisSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser: str = Field(min_length=1)
    pages: list[_AnalysisPage] = Field(min_length=1)
    candidates: list[_CandidateSnapshot]


class _ExpectedOutputSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: dict[str, Any]


class _AgentFailure(_StrictModel):
    schema_version: Literal["1.1"]
    status: Literal["interrupted", "context_exhausted", "credit_exhausted", "subagent_failed"]
    error_code: str = Field(min_length=1)


@dataclass(frozen=True)
class WorkflowResult:
    exit_code: int
    state: str
    job_dir: Path | None = None
    jobs: tuple["WorkflowResult", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"exit_code": self.exit_code, "state": self.state}
        if self.job_dir is not None:
            result["job_id"] = self.job_dir.name
        if self.jobs:
            result["jobs"] = [job.to_dict() for job in self.jobs]
        return result


def analyze(
    source: str | Path,
    glossary_path: str | Path,
    job_root: str | Path,
    *,
    now: datetime | None = None,
    mineru_client: MinerUClient | None = None,
) -> WorkflowResult:
    """Generate one isolated host-translation request per input PDF."""
    source_path = Path(source)
    pdfs = enumerate_pdfs(source_path)
    clock = now or datetime.now(timezone.utc)
    results: list[WorkflowResult] = []
    for offset, pdf in enumerate(pdfs):
        try:
            results.append(
                _analyze_one(
                    pdf,
                    Path(glossary_path),
                    Path(job_root),
                    clock + timedelta(seconds=offset),
                    mineru_client or MinerUClient(),
                )
            )
        except TechpackError as error:
            results.append(WorkflowResult(_exit_for(error), "failed"))
        except (OSError, ValueError, ValidationError):
            results.append(WorkflowResult(2, "failed"))
    if len(results) == 1:
        return results[0]
    return WorkflowResult(max(result.exit_code for result in results), "completed", jobs=tuple(results))


def prepare_review(job_dir: str | Path) -> WorkflowResult:
    """Validate host output and emit the offline review page, never a review JSON."""
    directory = _job_directory(job_dir)
    _load_state_payload(directory)
    job = _load_job(directory)
    state = _load_state(directory, job)
    _verify_bound_inputs(job)
    if state.state not in {WorkflowState.TRANSLATION_REQUESTED, WorkflowState.TRANSLATION_VALIDATED, WorkflowState.REVIEW_READY}:
        raise _workflow_error("workflow_state_conflict", "Job cannot prepare review from its current state")
    if state.state is WorkflowState.REVIEW_READY:
        return WorkflowResult(4, state.state, directory)

    if _artifact_exists(directory, "agent-failure.json"):
        _load_model(directory, "agent-failure.json", _AgentFailure)
        if state.wait_reason != "agent_failure":
            _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, state.revision + 1, state.expected_attempt, "agent_failure")
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)
    if not _artifact_exists(directory, "translation-response.json"):
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)

    analysis = _load_model(directory, "analysis.json", _AnalysisSnapshot)
    _assert_snapshot_binding(analysis, job)
    request = _read_json(directory, "translation-request.json")
    response = _response_input(directory)
    glossary = load_glossary(Path(job.glossary.path))
    try:
        translations = validate_translation_response(request, response, glossary, job, expected_attempt=state.expected_attempt)
    except TranslationValidationError as error:
        if error.correction_request is not None:
            _atomic_json_write(directory / "correction-request.json", error.correction_request)
            _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, state.revision + 1, 1, "host_correction")
            return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)
        _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, state.revision + 1, 1, "human_review_required")
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)

    if state.state is WorkflowState.TRANSLATION_REQUESTED:
        _write_state(directory, job, WorkflowState.TRANSLATION_VALIDATED, state.revision + 1, state.expected_attempt, None)
        state = _load_state(directory, job)
    output = _trusted_output(directory, job, analysis, translations)
    snapshot = _ExpectedOutputSnapshot(
        schema_version=_SCHEMA_VERSION,
        job_id=job.job_id,
        source_sha256=job.source.sha256,
        glossary_sha256=job.glossary.sha256,
        output=output,
    )
    _atomic_json_write(directory / "expected-output.json", snapshot.model_dump(mode="json"))
    html_output = _with_absolute_thumbnails(directory, output)
    _atomic_text_write(directory / "review.html", build_review_html(job, html_output))
    _write_state(directory, job, WorkflowState.REVIEW_READY, state.revision + 1, state.expected_attempt, "human_review")
    return WorkflowResult(4, WorkflowState.REVIEW_READY, directory)


def apply(
    source: str | Path,
    review: str | Path,
    output: str | Path,
) -> WorkflowResult:
    """Apply a completed review only from its bound job directory."""
    source_path = Path(source).resolve(strict=False)
    review_path = Path(review).resolve(strict=False)
    output_path = Path(output).resolve(strict=False)
    expected_path = source_path.with_name(source_path.name + ".annotated.pdf")
    if output_path != expected_path or output_path.exists():
        raise _workflow_error("output_invalid", "Output path is not available for this job")
    directory = _job_directory(review_path.parent)
    if review_path.parent != directory or review_path.name != "review.json":
        raise _workflow_error("review_path_invalid", "Review must be the job review.json")
    job = _load_job(directory)
    state = _load_state(directory, job)
    _verify_bound_inputs(job)
    if source_path != Path(job.source.path).resolve(strict=False):
        raise _workflow_error("source_job_mismatch", "Source does not match the job")
    if state.state not in {WorkflowState.REVIEW_READY, WorkflowState.REVIEW_COMPLETED}:
        raise _workflow_error("workflow_state_conflict", "Job is not ready to apply")
    expected = _load_model(directory, "expected-output.json", _ExpectedOutputSnapshot)
    _assert_snapshot_binding(expected, job)

    # This is the same authoritative validation that apply_review repeats internally.
    try:
        load_review(review_path, job, expected.output)
    except TechpackError:
        _write_state(directory, job, WorkflowState.FAILED, state.revision + 1, state.expected_attempt, None)
        return WorkflowResult(5, WorkflowState.FAILED, directory)
    if state.state is WorkflowState.REVIEW_READY:
        _write_state(directory, job, WorkflowState.REVIEW_COMPLETED, state.revision + 1, state.expected_attempt, None)
        state = _load_state(directory, job)
    _write_state(directory, job, WorkflowState.APPLYING, state.revision + 1, state.expected_attempt, None)
    result = apply_review(source_path, review_path, job, expected.output)
    if _apply_succeeded(result, expected_path):
        applying = _load_state(directory, job)
        _write_state(directory, job, WorkflowState.SUCCEEDED, applying.revision + 1, applying.expected_attempt, None)
        return WorkflowResult(0, WorkflowState.SUCCEEDED, directory)
    applying = _load_state(directory, job)
    _write_state(directory, job, WorkflowState.FAILED, applying.revision + 1, applying.expected_attempt, None)
    _atomic_json_write(directory / "apply-result.json", _safe_apply_result(result))
    return WorkflowResult(5, WorkflowState.FAILED, directory)


def _analyze_one(
    source: Path,
    glossary_path: Path,
    job_root: Path,
    now: datetime,
    mineru_client: MinerUClient,
) -> WorkflowResult:
    if source.is_symlink() or glossary_path.is_symlink():
        raise _workflow_error("input_path_invalid", "Input paths must not be symlinks")
    glossary = load_glossary(glossary_path)
    job = create_job(source.resolve(strict=True), glossary_path.resolve(strict=True), job_root, now)
    directory = _job_directory(job.job_dir)
    _atomic_json_write(directory / "manifest.json", job.model_dump(mode="json"))
    _write_state(directory, job, WorkflowState.INITIALIZED, 0, 0, None)
    try:
        pdf = inspect_pdf(source, directory)
        job = job.model_copy(
            update={
                "source": job.source.model_copy(update={"page_count": pdf.page_count}),
            }
        )
        _atomic_json_write(directory / "manifest.json", job.model_dump(mode="json"))
        parsed = mineru_client.parse_or_degrade(source, pdf)
        analysis = _analysis_snapshot(directory, job, pdf, parsed, glossary)
        _ensure_techpack_gate(analysis)
        _atomic_json_write(directory / "analysis.json", analysis.model_dump(mode="json"))
        _write_state(directory, job, WorkflowState.PARSED, 1, 0, None)
        _atomic_translation_request(directory, _candidates_from_analysis(analysis), job)
        _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, 2, 0, "host_translation")
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)
    except TechpackError:
        current = _load_state(directory, job)
        _write_state(directory, job, WorkflowState.FAILED, current.revision + 1, current.expected_attempt, None)
        raise


def _analysis_snapshot(
    directory: Path,
    job: JobManifest,
    pdf: PdfManifest,
    parsed: dict[str, Any] | NativeOnlyDegradation,
    glossary: Glossary,
) -> _AnalysisSnapshot:
    raw_pages = _raw_pages(parsed, pdf)
    pages: list[_AnalysisPage] = []
    candidates: list[_CandidateSnapshot] = []
    for page in pdf.pages:
        raw = raw_pages.get(page.page_index, {})
        raw_nodes = _raw_nodes(raw, page)
        matched = match_nodes(
            [
                {"text": span.text, "bbox": list(span.bbox)}
                for span in page.native_spans
            ],
            raw_nodes,
            list(page.crop_box),
        )
        classification = classify_page(
            PageFeatures(
                title=_string(raw.get("title", "")),
                table_headers=tuple(_string(item) for item in _list(raw.get("table_headers", []))),
                visual_features=tuple(_string(item) for item in _list(raw.get("visual_features", []))),
            )
        )
        selection = SelectionPage(
            page_index=page.page_index,
            classification=classification,
            nodes=tuple(
                PageNode(node, _string(raw_nodes[position].get("field_role", "body")) or "body")
                for position, node in enumerate(matched)
            ),
        )
        page_candidates = select_candidates(selection, glossary)
        pages.append(
            _AnalysisPage(
                page_index=page.page_index,
                page_type=classification.page_type,
                confidence=classification.confidence,
                evidence=list(classification.evidence),
                thumbnail=_relative_artifact(directory, page.thumbnail_path),
                width=page.crop_box[2] - page.crop_box[0],
                height=page.crop_box[3] - page.crop_box[1],
                nodes=[
                    _AnalysisNode(
                        text=node.text,
                        bbox=list(node.source_bbox or node.mineru_bbox or (0.0, 0.0, 0.0, 0.0)),
                        field_role=_string(raw_nodes[position].get("field_role", "body")) or "body",
                    )
                    for position, node in enumerate(matched)
                    if node.text.strip()
                ],
            )
        )
        candidates.extend(_candidate_snapshot(candidate) for candidate in page_candidates)
    return _AnalysisSnapshot(
        schema_version=_SCHEMA_VERSION,
        job_id=job.job_id,
        source_sha256=job.source.sha256,
        glossary_sha256=job.glossary.sha256,
        parser="native_only" if isinstance(parsed, NativeOnlyDegradation) else "mineru",
        pages=pages,
        candidates=candidates,
    )


def _raw_pages(parsed: dict[str, Any] | NativeOnlyDegradation, pdf: PdfManifest) -> dict[int, dict[str, Any]]:
    if isinstance(parsed, NativeOnlyDegradation):
        return {
            page.page_index: {
                "title": "",
                "nodes": [{"text": span.text, "bbox": list(span.bbox), "field_role": "body"} for span in page.native_spans],
            }
            for page in pdf.pages
        }
    raw = parsed.get("pages") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        raise _workflow_error("mineru_invalid_response", "MinerU response has no pages")
    pages: dict[int, dict[str, Any]] = {}
    for value in raw:
        if not isinstance(value, dict) or not isinstance(value.get("page_index"), int):
            raise _workflow_error("mineru_invalid_response", "MinerU page is invalid")
        index = value["page_index"]
        if index in pages or index < 0 or index >= pdf.page_count:
            raise _workflow_error("mineru_invalid_response", "MinerU page index is invalid")
        pages[index] = value
    if set(pages) != set(range(pdf.page_count)):
        raise _workflow_error("mineru_invalid_response", "MinerU pages are incomplete")
    return pages


def _raw_nodes(raw: Mapping[str, Any], page: Any) -> list[dict[str, Any]]:
    nodes = raw.get("nodes", [])
    if not isinstance(nodes, list):
        raise _workflow_error("mineru_invalid_response", "MinerU nodes are invalid")
    result: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not _string(node.get("text", "")).strip():
            raise _workflow_error("mineru_invalid_response", "MinerU node is invalid")
        bbox = node.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise _workflow_error("mineru_invalid_response", "MinerU node coordinates are invalid")
        result.append({"text": _string(node["text"]), "bbox": bbox, "field_role": _string(node.get("field_role", "body"))})
    return result


def _ensure_techpack_gate(analysis: _AnalysisSnapshot) -> None:
    evidence = [
        page
        for page in analysis.pages
        if page.page_type in _TECHPACK_TYPES and any(item.strip() for item in page.evidence)
    ]
    if not evidence:
        raise _workflow_error("techpack_evidence_missing", "Document has no TechPack classification evidence")


def _candidate_snapshot(candidate: Any) -> _CandidateSnapshot:
    return _CandidateSnapshot(
        item_id=candidate.item_id,
        page_index=candidate.page_index,
        page_type=candidate.page_type,
        classification_confidence=candidate.classification_confidence,
        classification_evidence=list(candidate.classification_evidence),
        source_text=candidate.source_text,
        normalized_text=candidate.normalized_text,
        source_bbox=list(candidate.source_bbox) if candidate.source_bbox is not None else None,
        source_kind=candidate.source_kind,
        coordinate_confidence=candidate.coordinate_confidence,
        source_auto_approvable=candidate.source_auto_approvable,
        auto_approvable=candidate.auto_approvable,
        should_translate=candidate.should_translate,
        decision_reason=candidate.decision_reason,
        locked_tokens=list(candidate.locked_tokens),
        glossary_hits=[asdict(hit) for hit in candidate.glossary_hits],
    )


def _candidates_from_analysis(analysis: _AnalysisSnapshot) -> list[Any]:
    """Adapt only the attributes required by the upstream request writer."""
    @dataclass(frozen=True)
    class _Candidate:
        item_id: str
        should_translate: bool
        source_text: str
        source_kind: str
        locked_tokens: tuple[str, ...]
        glossary_hits: tuple[Any, ...]
        page_type: PageType

    @dataclass(frozen=True)
    class _Hit:
        source_term: str
        target_term: str

    return [
        _Candidate(
            item_id=item.item_id,
            should_translate=item.should_translate,
            source_text=item.source_text,
            source_kind=item.source_kind,
            locked_tokens=tuple(item.locked_tokens),
            glossary_hits=tuple(_Hit(hit["source_term"], hit["target_term"]) for hit in item.glossary_hits),
            page_type=item.page_type,
        )
        for item in analysis.candidates
    ]


def _atomic_translation_request(directory: Path, candidates: Sequence[Any], job: JobManifest) -> None:
    temp = directory / f".translation-request.{uuid.uuid4().hex}.tmp"
    try:
        write_translation_request(candidates, temp, job)
        with temp.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, directory / "translation-request.json")
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _trusted_output(
    directory: Path,
    job: JobManifest,
    analysis: _AnalysisSnapshot,
    translations: Sequence[Any],
) -> dict[str, Any]:
    translated = {translation.item_id: translation for translation in translations}
    items: list[dict[str, Any]] = []
    for candidate in analysis.candidates:
        if not candidate.should_translate:
            continue
        translation = translated.get(candidate.item_id)
        if translation is None or candidate.source_bbox is None:
            raise _workflow_error("trusted_output_invalid", "Trusted candidate output is incomplete")
        items.append(
            {
                "item_id": candidate.item_id,
                "page_index": candidate.page_index,
                "page_type": candidate.page_type.value,
                "source_text": candidate.source_text,
                "normalized_text": candidate.normalized_text,
                "source_bbox": candidate.source_bbox,
                "source_kind": candidate.source_kind,
                "coordinate_confidence": candidate.coordinate_confidence.value,
                "decision_reason": candidate.decision_reason.value,
                "locked_tokens": candidate.locked_tokens,
                "glossary_hits": candidate.glossary_hits,
                "suggested_translation": translation.translated_text,
                "reviewed_translation": None,
                "review_status": None,
                "risk_level": "low" if candidate.auto_approvable else "high",
                "translation_host": translation.translator.host,
                "translation_execution_mode": translation.translator.execution_mode,
                "translation_model": translation.translator.model,
                "translation_agent_role": translation.translator.agent_role,
                "translation_prompt_version": translation.translator.prompt_version,
                "placement_strategy": None,
                "target_rect": None,
                "font_size": None,
                "leader_line": None,
                "warnings": list(translation.warnings),
            }
        )
    pages = [
        {
            "page_index": page.page_index,
            "width": page.width,
            "height": page.height,
            "thumbnail": page.thumbnail,
        }
        for page in analysis.pages
    ]
    return {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job.job_id,
        "parser": analysis.parser,
        "translation_executor": "host_agent",
        "pages": pages,
        "items": items,
        "blocking_issues": [],
    }


def _with_absolute_thumbnails(directory: Path, output: Mapping[str, Any]) -> dict[str, Any]:
    converted = json.loads(json.dumps(output))
    for page in converted["pages"]:
        page["thumbnail"] = str(_inside(directory, page["thumbnail"]))
    return converted


def _load_job(directory: Path) -> JobManifest:
    job = _load_model(directory, "manifest.json", JobManifest)
    if job.job_dir.resolve(strict=False) != directory:
        raise _workflow_error("workflow_job_invalid", "Manifest job directory does not match")
    if job.source.path is None or job.glossary.path is None:
        raise _workflow_error("workflow_job_invalid", "Manifest input paths are missing")
    return job


def _load_state(directory: Path, job: JobManifest) -> _StateSnapshot:
    state = _load_state_payload(directory)
    _assert_snapshot_binding(state, job)
    if state.state.value not in _STATES:
        raise _workflow_error("workflow_state_invalid", "Workflow state is unknown")
    return state


def _load_state_payload(directory: Path) -> _StateSnapshot:
    try:
        return _StateSnapshot.model_validate(_read_json(directory, "state.json"))
    except (ValidationError, TechpackError):
        raise _workflow_error("workflow_state_invalid", "Workflow state does not match its schema") from None


def _write_state(directory: Path, job: JobManifest, state: WorkflowState, revision: int, expected_attempt: Literal[0, 1], wait_reason: Literal["host_translation", "host_correction", "agent_failure", "human_review_required", "human_review"] | None) -> None:
    snapshot = _StateSnapshot(
        schema_version=_SCHEMA_VERSION,
        job_id=job.job_id,
        source_sha256=job.source.sha256,
        glossary_sha256=job.glossary.sha256,
        state=state,
        revision=revision,
        expected_attempt=expected_attempt,
        wait_reason=wait_reason,
    )
    _atomic_json_write(directory / "state.json", snapshot.model_dump(mode="json"))


def _verify_bound_inputs(job: JobManifest) -> None:
    source = Path(job.source.path)
    glossary = Path(job.glossary.path)
    if source.is_symlink() or glossary.is_symlink():
        raise _workflow_error("workflow_input_invalid", "Job input paths must not be symlinks")
    try:
        if sha256_file(source) != job.source.sha256 or sha256_file(glossary) != job.glossary.sha256:
            raise _workflow_error("workflow_input_changed", "Job inputs no longer match their manifest")
    except OSError:
        raise _workflow_error("workflow_input_unavailable", "Job input is unavailable") from None


def _assert_snapshot_binding(snapshot: Any, job: JobManifest) -> None:
    if (
        snapshot.job_id != job.job_id
        or snapshot.source_sha256 != job.source.sha256
        or snapshot.glossary_sha256 != job.glossary.sha256
    ):
        raise _workflow_error("workflow_binding_mismatch", "Workflow artifact does not match the job")


def _job_directory(path: str | Path) -> Path:
    directory = Path(path)
    if directory.is_symlink() or not directory.is_dir():
        raise _workflow_error("workflow_job_invalid", "Job directory is invalid")
    return directory.resolve(strict=True)


def _inside(directory: Path, name: str) -> Path:
    relative = Path(name) if isinstance(name, str) else None
    if relative is None or not name or relative.is_absolute() or ".." in relative.parts:
        raise _workflow_error("workflow_artifact_invalid", "Artifact path is invalid")
    path = directory / relative
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(directory)
    except ValueError:
        raise _workflow_error("workflow_artifact_invalid", "Artifact path escapes the job")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != directory):
        raise _workflow_error("workflow_artifact_invalid", "Artifact path is invalid")
    return path


def _artifact_exists(directory: Path, name: str) -> bool:
    path = _inside(directory, name)
    return path.exists()


def _read_json(directory: Path, name: str) -> Any:
    path = _inside(directory, name)
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            raise OSError
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _workflow_error("workflow_artifact_invalid", "Workflow JSON artifact is invalid") from None


def _response_input(directory: Path) -> Path | object:
    """Keep bounded path safety while letting Task 6 own malformed-response policy."""
    path = _inside(directory, "translation-response.json")
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return object()
    except OSError:
        return object()
    return path


def _load_model(directory: Path, name: str, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(_read_json(directory, name))
    except ValidationError:
        code = "workflow_state_invalid" if name == "state.json" else "workflow_artifact_invalid"
        raise _workflow_error(code, "Workflow artifact does not match its schema") from None


def _atomic_json_write(path: Path, value: Any) -> None:
    _atomic_text_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _atomic_text_write(path: Path, text: str) -> None:
    directory = path.parent
    temp = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _relative_artifact(directory: Path, path: Path) -> str:
    try:
        return str(path.resolve(strict=True).relative_to(directory))
    except ValueError:
        raise _workflow_error("workflow_artifact_invalid", "Artifact is outside the job") from None


def _apply_succeeded(result: ApplyResult, expected_output: Path) -> bool:
    return (
        result.success
        and result.output_path is not None
        and result.output_path.resolve(strict=False) == expected_output
        and not result.problems
        and not result.unresolved_overlaps
        and expected_output.exists()
    )


def _safe_apply_result(result: ApplyResult) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "success": False,
        "problem_codes": [str(problem.get("code", "apply_failed")) for problem in result.problems],
        "unresolved_overlap_count": len(result.unresolved_overlaps),
        "status": "failed",
    }


def _exit_for(error: TechpackError) -> int:
    return 3 if error.code in {"mineru_unavailable", "mineru_invalid_response"} else 2


def _workflow_error(code: str, message: str) -> TechpackError:
    return TechpackError(code, message, {"error_code": code})


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


__all__ = ["WorkflowResult", "WorkflowState", "analyze", "apply", "prepare_review"]
