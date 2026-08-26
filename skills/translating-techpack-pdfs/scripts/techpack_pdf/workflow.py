"""Resumable, fail-closed orchestration for the TechPack PDF pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

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
    ReviewItem,
)
from .pdf_analysis import PdfManifest, inspect_pdf
from .review import build_review_html, load_review
from .selection import (
    AgentClassification,
    PageClassification,
    PageFeatures,
    PageNode,
    SelectionPage,
    classify_page,
    select_candidates,
)
from .translation import TranslationValidationError, validate_translation_response, write_translation_request


_SCHEMA_VERSION = "1.1"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_TRUSTED_REVIEW_NAME = "trusted-review.json"
_PENDING_REVIEW_NAME = re.compile(r"^\.trusted-review\.[0-9a-f]{32}\.pending$")
_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_ACTIVE_JOB_GUARDS: dict[tuple[int, str], str] = {}
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
_TRANSITIONS = {
    None: {"initialized"}, "initialized": {"parsed", "failed"},
    "parsed": {"translation_requested", "failed"},
    "translation_requested": {"translation_requested", "translation_validated", "failed"},
    "translation_validated": {"review_ready", "failed"},
    "review_ready": {"review_completed", "failed"},
    "review_completed": {"applying", "failed"}, "applying": {"applying", "succeeded", "failed"},
    "succeeded": set(), "failed": set(),
}


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
    wait_reason: Literal[
        "agent_classification",
        "host_translation",
        "host_correction",
        "agent_failure",
        "human_review_required",
        "human_review",
    ] | None
    artifacts: "_ArtifactDigests"


class _ArtifactDigests(_StrictModel):
    analysis: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classification_request: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classification_response: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_output: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_html: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    correction_request: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    apply_result: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _AnalysisNode(_StrictModel):
    text: str = Field(min_length=1)
    bbox: list[float] = Field(min_length=4, max_length=4)
    field_role: str = Field(min_length=1)


class _PendingClassification(_StrictModel):
    reason: Literal["unknown", "conflict", "low_confidence"]
    title: str
    table_headers: list[str]
    visual_features: list[str]
    evidence: list[str]


class _AnalysisPage(_StrictModel):
    page_index: int = Field(ge=0)
    page_type: PageType
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    thumbnail: str = Field(min_length=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    nodes: list[_AnalysisNode]
    classification_request: _PendingClassification | None = None


class _ClassificationRequestItem(_StrictModel):
    page_index: StrictInt = Field(ge=0)
    reason: Literal["unknown", "conflict", "low_confidence"]
    title: StrictStr
    table_headers: list[StrictStr]
    visual_features: list[StrictStr]
    evidence: list[StrictStr]
    thumbnail: StrictStr = Field(min_length=1)


class _ClassificationRequestEnvelope(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[_ClassificationRequestItem] = Field(min_length=1)


class _ClassificationResponseItem(_StrictModel):
    page_index: StrictInt = Field(ge=0)
    page_type: PageType
    confidence: StrictFloat = Field(ge=0, le=1)
    evidence: list[StrictStr]

    @field_validator("evidence")
    @classmethod
    def evidence_is_nonblank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or item != item.strip() for item in value):
            raise ValueError("evidence must contain trimmed nonblank strings")
        return value


class _ClassificationResponseEnvelope(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[_ClassificationResponseItem] = Field(min_length=1)


class _GlossaryHitSnapshot(_StrictModel):
    source_term: str = Field(min_length=1)
    target_term: str
    matched_text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    do_not_translate: bool
    priority: int

    @model_validator(mode="after")
    def target_matches_translation_policy(self) -> "_GlossaryHitSnapshot":
        if not self.do_not_translate and not self.target_term.strip():
            raise ValueError("target_term may be empty only for do_not_translate hits")
        return self


class _CandidateSnapshot(_StrictModel):
    item_id: str = Field(pattern=r"^p[0-9]{3}-i[0-9]{3,}$")
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
    glossary_hits: list[_GlossaryHitSnapshot]


class _AnalysisSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser: str = Field(min_length=1)
    pages: list[_AnalysisPage] = Field(min_length=1)
    candidates: list[_CandidateSnapshot]


class _ExpectedPage(_StrictModel):
    page_index: int = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    thumbnail: str = Field(min_length=1)


class _TrustedExpectedOutput(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    parser: str = Field(min_length=1)
    translation_executor: Literal["host_agent"]
    pages: list[_ExpectedPage] = Field(min_length=1)
    items: list[ReviewItem]
    blocking_issues: list[dict[str, Any]] = Field(max_length=0)


class _ExpectedOutputSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: _TrustedExpectedOutput


class _AgentFailure(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["interrupted", "context_exhausted", "credit_exhausted", "subagent_failed"]
    error_code: Literal[
        "agent_interrupted", "agent_context_exhausted", "agent_credit_exhausted", "agent_subagent_failed"
    ]


class _ApplyResultSnapshot(_StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "failed"]
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_output_retained: bool = False
    problems: list["_SafeApplyProblem"]
    unresolved: list["_SafeApplyProblem"]
    unresolved_overlap_count: int = Field(ge=0)


class _SafeApplyProblem(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    ownership: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resource_kind: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    exists: bool | None = None
    rollback: bool | None = None
    nested: list["_SafeApplyProblem"] = Field(default_factory=list)


class _WorkflowBusy(Exception):
    pass


@dataclass(frozen=True)
class _JobGuard:
    key: str
    token: str


def _guard_is_active(guard: _JobGuard) -> bool:
    with _THREAD_LOCK_GUARD:
        return _ACTIVE_JOB_GUARDS.get((threading.get_ident(), guard.key)) == guard.token


@contextmanager
def _job_lock(directory: Path):
    """Try once to hold the lightweight per-job guard; same-job waiting is unsupported."""
    key = str(directory)
    with _THREAD_LOCK_GUARD:
        local = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not local.acquire(blocking=False):
        raise _WorkflowBusy
    stream = None
    os_acquired = False
    guard = _JobGuard(key, uuid.uuid4().hex)
    try:
        path = _inside(directory, ".workflow.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        before = path.lstat()
        opened = os.fstat(stream.fileno())
        if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode) or _file_stat_identity(before) != _file_stat_identity(opened):
            raise _workflow_error("workflow_lock_invalid", "Workflow lock is invalid")
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if opened.st_size == 0:
                stream.write(b"0")
                stream.flush()
                before = path.lstat()
            stream.seek(0)
            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
            os_acquired = True
        except OSError:
            raise _WorkflowBusy from None
        if _file_stat_identity(before) != _file_stat_identity(path.lstat()):
            raise _workflow_error("workflow_lock_invalid", "Workflow lock changed while being acquired")
        with _THREAD_LOCK_GUARD:
            _ACTIVE_JOB_GUARDS[(threading.get_ident(), key)] = guard.token
        yield guard
    finally:
        with _THREAD_LOCK_GUARD:
            _ACTIVE_JOB_GUARDS.pop((threading.get_ident(), key), None)
        try:
            if stream is not None and os_acquired and os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            elif stream is not None and os_acquired:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                local.release()


@dataclass(frozen=True)
class WorkflowResult:
    exit_code: int
    state: WorkflowState | None
    job_dir: Path | None = None
    jobs: tuple["WorkflowResult", ...] = ()
    input_index: int | None = None
    batch_status: Literal["completed"] | None = None
    status: Literal["workflow_busy", "recovery_required"] | None = None
    wait_reason: Literal[
        "agent_classification",
        "concurrent_operation",
        "apply_recovery",
        "review_recovery",
    ] | None = None

    def __post_init__(self) -> None:
        if self.state is not None:
            object.__setattr__(self, "state", WorkflowState(self.state))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"exit_code": self.exit_code}
        if self.state is not None:
            result["state"] = self.state
        if self.job_dir is not None:
            result["job_id"] = self.job_dir.name
            result["job_dir"] = str(self.job_dir)
        if self.input_index is not None:
            result["input_index"] = self.input_index
        if self.jobs:
            result["jobs"] = [job.to_dict() for job in self.jobs]
        if self.batch_status is not None:
            result["batch_status"] = self.batch_status
        if self.status is not None:
            result["status"] = self.status
        if self.wait_reason is not None:
            result["wait_reason"] = self.wait_reason
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
    try:
        source_path = _absolute_user_path(source, must_exist=True, regular=False)
        glossary_absolute = _absolute_user_path(glossary_path, must_exist=True, regular=True)
        root_absolute = _absolute_user_path(job_root, must_exist=False, regular=False)
        pdfs = enumerate_pdfs(source_path)
    except TechpackError as error:
        return WorkflowResult(_exit_for(error), "failed", input_index=0)
    except Exception:
        return WorkflowResult(2, "failed", input_index=0)
    clock = now or datetime.now(timezone.utc)
    results: list[WorkflowResult] = []
    for offset, pdf in enumerate(pdfs):
        try:
            one = _analyze_one(
                    pdf,
                    glossary_absolute,
                    root_absolute,
                    clock + timedelta(seconds=offset),
                    mineru_client or MinerUClient(),
                )
            results.append(WorkflowResult(
                one.exit_code, one.state, one.job_dir, one.jobs, offset,
                status=one.status, wait_reason=one.wait_reason,
            ))
        except TechpackError as error:
            results.append(WorkflowResult(_exit_for(error), "failed", input_index=offset))
        except Exception:
            results.append(WorkflowResult(2, "failed", input_index=offset))
    if len(results) == 1:
        return results[0]
    return WorkflowResult(_batch_exit_code(results), None, jobs=tuple(results), batch_status="completed")


def prepare_review(job_dir: str | Path) -> WorkflowResult:
    directory = _job_directory(job_dir)
    try:
        with _job_lock(directory) as guard:
            try:
                return _prepare_review_locked(directory)
            except TechpackError:
                raise
            except Exception:
                _mark_job_failed(directory, guard)
                raise _workflow_error("workflow_internal_error", "Workflow operation failed") from None
    except _WorkflowBusy:
        return WorkflowResult(
            4, None, directory, status="workflow_busy", wait_reason="concurrent_operation",
        )
    except TechpackError:
        raise


def _prepare_review_locked(job_dir: str | Path) -> WorkflowResult:
    """Validate host output and emit the offline review page, never a review JSON."""
    directory = _job_directory(job_dir)
    _load_state_payload(directory)
    job = _load_job(directory)
    state = _load_state(directory, job)
    if state.state is WorkflowState.INITIALIZED:
        job = _resume_initialized_job(directory, job, MinerUClient())
        state = _load_state(directory, job)
    if state.state is WorkflowState.PARSED:
        analysis = _load_model(directory, "analysis.json", _AnalysisSnapshot)
        _assert_snapshot_binding(analysis, job)
        effective = _effective_analysis(directory, job, analysis)
        if effective is None:
            return WorkflowResult(
                4,
                WorkflowState.PARSED,
                directory,
                wait_reason="agent_classification",
            )
        _atomic_translation_request(directory, _candidates_from_analysis(effective), job)
        _write_state(
            directory,
            job,
            WorkflowState.TRANSLATION_REQUESTED,
            state.revision + 1,
            state.expected_attempt,
            "host_translation",
        )
        state = _load_state(directory, job)
    _verify_bound_inputs(directory, job)
    if state.state not in {WorkflowState.TRANSLATION_REQUESTED, WorkflowState.TRANSLATION_VALIDATED, WorkflowState.REVIEW_READY}:
        raise _workflow_error("workflow_state_conflict", "Job cannot prepare review from its current state")
    if state.state is WorkflowState.REVIEW_READY:
        return WorkflowResult(4, state.state, directory)

    if _artifact_exists(directory, "agent-failure.json"):
        failure = _load_model(directory, "agent-failure.json", _AgentFailure)
        request = _read_json(directory, "translation-request.json")
        _assert_agent_failure_binding(failure, job, request)
        if state.state is not WorkflowState.TRANSLATION_REQUESTED:
            raise _workflow_error("workflow_state_conflict", "Agent failure cannot regress validated translation")
        if state.wait_reason != "agent_failure":
            _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, state.revision + 1, state.expected_attempt, "agent_failure")
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)
    if not _artifact_exists(directory, "translation-response.json"):
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)

    analysis = _effective_analysis(directory, job)
    if analysis is None:
        raise _workflow_error(
            "workflow_state_invalid",
            "Validated classification is missing",
        )
    request = _read_json(directory, "translation-request.json")
    response = _response_input(directory)
    glossary = load_glossary(_snapshot_glossary_path(directory, job))
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
    review_path = _absolute_user_path(review, must_exist=False, regular=False)
    directory = _job_directory(review_path.parent)
    source_path = _absolute_user_path(source, must_exist=True, regular=True)
    output_path = _absolute_user_path(output, must_exist=False, regular=False)
    expected_path = source_path.with_name(source_path.name + ".annotated.pdf")
    if output_path != expected_path:
        raise _workflow_error("output_invalid", "Output path is not available for this job")
    if output_path.exists() and not (directory / "state.json").is_file():
        raise _workflow_error("output_exists", "Output already exists")
    try:
        with _job_lock(directory) as guard:
            try:
                return _apply_locked(source, review_path, output)
            except TechpackError as error:
                if error.code == "workflow_artifact_invalid":
                    _mark_job_failed(directory, guard)
                raise
            except Exception:
                _mark_job_failed(directory, guard)
                raise _workflow_error("workflow_internal_error", "Workflow operation failed") from None
    except _WorkflowBusy:
        return WorkflowResult(
            4, None, directory, status="workflow_busy", wait_reason="concurrent_operation",
        )
    except TechpackError:
        raise


def _apply_locked(
    source: str | Path,
    review: str | Path,
    output: str | Path,
) -> WorkflowResult:
    """Apply a completed review only from its bound job directory."""
    source_path = _absolute_user_path(source, must_exist=True, regular=True)
    review_path = _absolute_user_path(review, must_exist=False, regular=False)
    output_path = _absolute_user_path(output, must_exist=False, regular=False)
    expected_path = source_path.with_name(source_path.name + ".annotated.pdf")
    if output_path != expected_path:
        raise _workflow_error("output_invalid", "Output path is not available for this job")
    directory = _job_directory(review_path.parent)
    if review_path.parent != directory or review_path.name != "review.json":
        raise _workflow_error("review_path_invalid", "Review must be the job review.json")
    job = _load_job(directory)
    state = _load_state(directory, job)
    _verify_bound_inputs(directory, job)
    if source_path != _absolute_user_path(job.source.path, must_exist=True, regular=True):
        raise _workflow_error("source_job_mismatch", "Source does not match the job")
    expected = _load_model(directory, "expected-output.json", _ExpectedOutputSnapshot)
    _assert_snapshot_binding(expected, job)
    _verify_apply_integrity_closure(directory, job, state, expected)
    resuming_apply = state.state is WorkflowState.APPLYING
    if resuming_apply:
        # A published final is recoverable only if this applying state already
        # authenticated the exact success report.  An unbound report could have
        # been placed after a crash and must never complete the job.
        if state.artifacts.apply_result is None:
            if _artifact_exists(directory, "apply-result.json") or expected_path.exists():
                return WorkflowResult(
                    5, WorkflowState.APPLYING, directory,
                    status="recovery_required", wait_reason="apply_recovery",
                )
        else:
            try:
                report = _load_model(directory, "apply-result.json", _ApplyResultSnapshot)
                expected_digest = _sha256_artifact(directory, "expected-output.json")
                review_digest = _sha256_artifact(directory, _TRUSTED_REVIEW_NAME)
                final = _absolute_user_path(expected_path, must_exist=True, regular=True)
                valid = (
                    report.job_id == job.job_id and report.source_sha256 == job.source.sha256
                    and report.glossary_sha256 == job.glossary.sha256 and report.status == "succeeded"
                    and report.expected_output_sha256 == expected_digest and report.review_sha256 == review_digest
                    and report.output_sha256 == sha256_file(final)
                )
            except TechpackError:
                valid = False
            if valid:
                _write_state(directory, job, WorkflowState.SUCCEEDED, state.revision + 1, state.expected_attempt, None)
                return WorkflowResult(0, WorkflowState.SUCCEEDED, directory)
            return WorkflowResult(
                5, WorkflowState.APPLYING, directory,
                status="recovery_required", wait_reason="apply_recovery",
            )
    if not resuming_apply and output_path.exists():
        raise _workflow_error("output_exists", "Output already exists")
    if state.state not in {WorkflowState.REVIEW_READY, WorkflowState.REVIEW_COMPLETED, WorkflowState.APPLYING}:
        raise _workflow_error("workflow_state_conflict", "Job is not ready to apply")
    # Keep this reserved lexical path inspectable only for the review_ready
    # crash-recovery exception.  Every read still goes through strict _inside /
    # bounded Task 7 validation; all other artifact paths retain _inside here.
    trusted_review = directory / _TRUSTED_REVIEW_NAME
    trusted_review_digest = state.artifacts.review
    if state.state is WorkflowState.REVIEW_READY:
        try:
            trusted_review.lstat()
            published_after_crash = True
        except FileNotFoundError:
            published_after_crash = False
        except OSError:
            published_after_crash = True
        if published_after_crash:
            try:
                before_recovery = _sha256_artifact(directory, _TRUSTED_REVIEW_NAME)
                load_review(trusted_review, job, expected.output)
                after_recovery = _sha256_artifact(directory, _TRUSTED_REVIEW_NAME)
            except TechpackError:
                return WorkflowResult(
                    5,
                    WorkflowState.REVIEW_READY,
                    directory,
                    status="recovery_required",
                    wait_reason="review_recovery",
                )
            if before_recovery != after_recovery:
                return WorkflowResult(
                    5,
                    WorkflowState.REVIEW_READY,
                    directory,
                    status="recovery_required",
                    wait_reason="review_recovery",
                )
            trusted_review_digest = after_recovery
            _write_state(
                directory,
                job,
                WorkflowState.REVIEW_COMPLETED,
                state.revision + 1,
                state.expected_attempt,
                None,
            )
            state = _load_state(directory, job)
            _cleanup_linked_review_pending(directory, _owner_identity(trusted_review.lstat()))
        else:
            review_path = _absolute_user_path(review_path, must_exist=True, regular=True)
            review_bytes = _bounded_binary_read(review_path)
            pending_review = _inside(directory, f".trusted-review.{uuid.uuid4().hex}.pending")
            pending_owner = _atomic_binary_write(pending_review, review_bytes)
            trusted_review_digest = _sha256_bytes(review_bytes)
            try:
                try:
                    load_review(pending_review, job, expected.output)
                except TechpackError:
                    _atomic_json_write(directory / "apply-result.json", _apply_result_snapshot(
                        directory,
                        job,
                        "failed",
                        review_sha256_override=trusted_review_digest,
                        problems=({"code": "review_validation_failed"},),
                    ).model_dump(mode="json"))
                    _write_state(directory, job, WorkflowState.FAILED, state.revision + 1, state.expected_attempt, None)
                    return WorkflowResult(5, WorkflowState.FAILED, directory)
                if (
                    _owner_identity(pending_review.lstat()) != pending_owner
                    or _sha256_artifact(directory, pending_review.name) != trusted_review_digest
                ):
                    raise _workflow_error("workflow_artifact_invalid", "Review candidate changed during validation")
                _publish_review_no_clobber(pending_review, trusted_review, pending_owner, trusted_review_digest)
                try:
                    _write_state(
                        directory,
                        job,
                        WorkflowState.REVIEW_COMPLETED,
                        state.revision + 1,
                        state.expected_attempt,
                        None,
                    )
                except BaseException:
                    if not _unlink_owned(trusted_review, pending_owner):
                        raise _workflow_error("workflow_atomic_cleanup_failed", "Validated review rollback failed") from None
                    raise
                state = _load_state(directory, job)
            finally:
                if pending_review.exists() and not _unlink_owned(pending_review, pending_owner):
                    raise _workflow_error("workflow_atomic_cleanup_failed", "Review candidate cleanup failed") from None
    else:
        # Resume validates the one already-bound review snapshot; external review.json is irrelevant.
        load_review(trusted_review, job, expected.output)
    if state.state is WorkflowState.REVIEW_COMPLETED:
        _write_state(directory, job, WorkflowState.APPLYING, state.revision + 1, state.expected_attempt, None)
    before_task8_review = _sha256_artifact(directory, _TRUSTED_REVIEW_NAME)
    if trusted_review_digest is None or before_task8_review != trusted_review_digest:
        raise _workflow_error("workflow_artifact_invalid", "Trusted review changed before PDF apply")
    result = apply_review(source_path, trusted_review, job, expected.output)
    if _sha256_artifact(directory, _TRUSTED_REVIEW_NAME) != before_task8_review:
        _atomic_json_write(directory / "apply-result.json", _apply_result_snapshot(
            directory,
            job,
            "failed",
            problems=({"code": "review_snapshot_changed"},),
            final_output_retained=expected_path.exists(),
        ).model_dump(mode="json"))
        raise _workflow_error("workflow_artifact_invalid", "Trusted review changed during PDF apply")
    if _apply_succeeded(result, expected_path):
        _atomic_json_write(directory / "apply-result.json", _apply_result_snapshot(
            directory, job, "succeeded", output_sha256=sha256_file(expected_path),
        ).model_dump(mode="json"))
        applying = _load_state(directory, job)
        # Bind the committed success-report digest while remaining in
        # ``applying``; only that checkpoint may recover to ``succeeded``.
        _write_state(directory, job, WorkflowState.APPLYING, applying.revision + 1, applying.expected_attempt, None)
        bound = _load_state(directory, job)
        _write_state(directory, job, WorkflowState.SUCCEEDED, bound.revision + 1, bound.expected_attempt, None)
        return WorkflowResult(0, WorkflowState.SUCCEEDED, directory)
    applying = _load_state(directory, job)
    _atomic_json_write(directory / "apply-result.json", _apply_result_snapshot(
        directory, job, "failed", problems=result.problems, unresolved=result.unresolved_overlaps,
        final_output_retained=expected_path.exists(),
    ).model_dump(mode="json"))
    _write_state(directory, job, WorkflowState.FAILED, applying.revision + 1, applying.expected_attempt, None)
    return WorkflowResult(5, WorkflowState.FAILED, directory)


def _canonical_request_bytes(directory: Path, analysis: _AnalysisSnapshot, job: JobManifest) -> bytes:
    """Rebuild the exact Task 6 request without accepting a job-local substitute."""
    path = _inside(directory, f".canonical-request.{uuid.uuid4().hex}.tmp")
    owned_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            owned_identity = (opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
        write_translation_request(_candidates_from_analysis(analysis), path, job)
        details = path.lstat()
        if (
            owned_identity is None
            or _is_reparse_or_link(details)
            or not stat.S_ISREG(details.st_mode)
            or (details.st_dev, details.st_ino) != owned_identity
        ):
            raise OSError
        return _bounded_binary_read(path)
    except (OSError, TechpackError):
        raise _workflow_error("workflow_binding_mismatch", "Workflow request does not match bound analysis") from None
    finally:
        try:
            if path.exists() and owned_identity is not None:
                details = path.lstat()
                if (
                    not _is_reparse_or_link(details)
                    and stat.S_ISREG(details.st_mode)
                    and (details.st_dev, details.st_ino) == owned_identity
                ):
                    path.unlink()
        except OSError:
            pass


def _verify_apply_integrity_closure(
    directory: Path,
    job: JobManifest,
    state: _StateSnapshot,
    expected: _ExpectedOutputSnapshot,
) -> None:
    """Reconstruct every bound upstream contract before editing a PDF."""
    try:
        analysis = _effective_analysis(directory, job)
        if analysis is None:
            raise ValueError
        request_bytes = _bounded_binary_read(_inside(directory, "translation-request.json"))
        if request_bytes != _canonical_request_bytes(directory, analysis, job):
            raise ValueError
        request = json.loads(request_bytes.decode("utf-8"))
        canonical_ids = [item.item_id for item in analysis.candidates if item.should_translate]
        request_ids = [item["item_id"] for item in request.get("items", [])]
        if request_ids != canonical_ids or len(request_ids) != len(set(request_ids)):
            raise ValueError
        translations = validate_translation_response(
            request,
            _response_input(directory),
            load_glossary(_snapshot_glossary_path(directory, job)),
            job,
            expected_attempt=state.expected_attempt,
        )
        translated_ids = [item.item_id for item in translations]
        if translated_ids != canonical_ids or len(translated_ids) != len(set(translated_ids)):
            raise ValueError
        rebuilt = _trusted_output(directory, job, analysis, translations)
        if rebuilt.model_dump(mode="json") != expected.output.model_dump(mode="json"):
            raise ValueError
    except (KeyError, UnicodeError, json.JSONDecodeError, TranslationValidationError, TechpackError, ValueError):
        raise _workflow_error("workflow_binding_mismatch", "Workflow artifacts do not form a bound closure") from None


def _analyze_one(
    source: Path,
    glossary_path: Path,
    job_root: Path,
    now: datetime,
    mineru_client: MinerUClient,
) -> WorkflowResult:
    source = _absolute_user_path(source, must_exist=True, regular=True)
    glossary_path = _absolute_user_path(glossary_path, must_exist=True, regular=True)
    job_root = _absolute_user_path(job_root, must_exist=False, regular=False)
    job = create_job(source, glossary_path, job_root, now)
    directory = Path(job.job_dir)
    try:
        directory = _job_directory(directory)
        _atomic_json_write(directory / "manifest.json", job.model_dump(mode="json"))
        _write_state(directory, job, WorkflowState.INITIALIZED, 0, 0, None)
        job = _resume_initialized_job(directory, job, mineru_client)
        analysis = _effective_analysis(directory, job)
        if analysis is None:
            return WorkflowResult(
                4,
                WorkflowState.PARSED,
                directory,
                wait_reason="agent_classification",
            )
        _atomic_translation_request(directory, _candidates_from_analysis(analysis), job)
        _write_state(directory, job, WorkflowState.TRANSLATION_REQUESTED, 2, 0, "host_translation")
        return WorkflowResult(4, WorkflowState.TRANSLATION_REQUESTED, directory)
    except Exception as error:
        try:
            current = _load_state(directory, job)
            if current.state is not WorkflowState.SUCCEEDED:
                _write_state(directory, job, WorkflowState.FAILED, current.revision + 1, current.expected_attempt, None)
        except Exception:
            pass
        if isinstance(error, TechpackError):
            return WorkflowResult(_exit_for(error), WorkflowState.FAILED, directory)
        return WorkflowResult(2, WorkflowState.FAILED, directory)


def _resume_initialized_job(
    directory: Path,
    job: JobManifest,
    mineru_client: MinerUClient,
) -> JobManifest:
    """Deterministically rebuild an initialized job through its parsed checkpoint."""
    source = _absolute_user_path(job.source.path, must_exist=True, regular=True)
    glossary_path = _absolute_user_path(job.glossary.path, must_exist=True, regular=True)
    identities = _input_identities(source, glossary_path)
    snapshot_source, snapshot_glossary = _snapshot_inputs(directory, job, identities)
    snapshot_identities = _input_identities(snapshot_source, snapshot_glossary)
    _verify_snapshot_inputs(directory, job, snapshot_identities)
    glossary = load_glossary(snapshot_glossary)
    _verify_snapshot_inputs(directory, job, snapshot_identities)
    pdf = inspect_pdf(snapshot_source, directory)
    _verify_snapshot_inputs(directory, job, snapshot_identities)
    if pdf.sha256 != job.source.sha256:
        raise _workflow_error("workflow_input_changed", "Input snapshot does not match manifest")
    job = job.model_copy(update={
        "source": job.source.model_copy(update={"page_count": pdf.page_count}),
    })
    _atomic_json_write(directory / "manifest.json", job.model_dump(mode="json"))
    parsed = mineru_client.parse_or_degrade(snapshot_source, pdf)
    _verify_snapshot_inputs(directory, job, snapshot_identities)
    analysis = _analysis_snapshot(directory, job, pdf, parsed, glossary)
    classification_pending = _classification_request_from_analysis(analysis, job) is not None
    if not classification_pending:
        _ensure_techpack_gate(analysis)
    _verify_snapshot_inputs(directory, job, snapshot_identities)
    _verify_original_inputs(job, identities)
    _atomic_json_write(directory / "analysis.json", analysis.model_dump(mode="json"))
    if classification_pending:
        _write_classification_request(directory, analysis, job)
    _write_state(
        directory,
        job,
        WorkflowState.PARSED,
        1,
        0,
        "agent_classification" if classification_pending else None,
    )
    return job


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
                classification_request=(
                    _PendingClassification(
                        reason=classification.classification_request.reason,
                        title=classification.classification_request.title,
                        table_headers=list(classification.classification_request.table_headers),
                        visual_features=list(classification.classification_request.visual_features),
                        evidence=list(classification.classification_request.evidence),
                    )
                    if classification.classification_request is not None
                    else None
                ),
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


def _classification_request_from_analysis(
    analysis: _AnalysisSnapshot,
    job: JobManifest,
) -> _ClassificationRequestEnvelope | None:
    items = [
        {
            "page_index": page.page_index,
            "reason": page.classification_request.reason,
            "title": page.classification_request.title,
            "table_headers": page.classification_request.table_headers,
            "visual_features": page.classification_request.visual_features,
            "evidence": page.classification_request.evidence,
            "thumbnail": page.thumbnail,
        }
        for page in analysis.pages
        if page.classification_request is not None
    ]
    if not items:
        return None
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job.job_id,
        "source_sha256": job.source.sha256,
        "glossary_sha256": job.glossary.sha256,
        "items": items,
    }
    payload["request_sha256"] = _classification_request_hash(payload)
    return _ClassificationRequestEnvelope.model_validate(payload)


def _classification_request_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("request_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _write_classification_request(
    directory: Path,
    analysis: _AnalysisSnapshot,
    job: JobManifest,
) -> bool:
    request = _classification_request_from_analysis(analysis, job)
    if request is None:
        return False
    _atomic_json_write(
        directory / "classification-request.json",
        request.model_dump(mode="json"),
    )
    return True


def _load_classification_request(
    directory: Path,
    analysis: _AnalysisSnapshot,
    job: JobManifest,
) -> _ClassificationRequestEnvelope:
    expected = _classification_request_from_analysis(analysis, job)
    if expected is None:
        raise _workflow_error(
            "workflow_state_invalid",
            "Classification request has no pending page",
        )
    try:
        actual = _load_model(
            directory,
            "classification-request.json",
            _ClassificationRequestEnvelope,
        )
    except TechpackError:
        raise _workflow_error(
            "workflow_artifact_invalid",
            "Classification request is invalid",
        ) from None
    actual_payload = actual.model_dump(mode="json")
    if (
        actual.request_sha256 != _classification_request_hash(actual_payload)
        or actual_payload != expected.model_dump(mode="json")
    ):
        raise _workflow_error(
            "workflow_binding_mismatch",
            "Classification request does not match analysis",
        )
    return actual


def _load_classification_response(
    directory: Path,
    request: _ClassificationRequestEnvelope,
) -> _ClassificationResponseEnvelope:
    try:
        response = _load_model(
            directory,
            "classification-response.json",
            _ClassificationResponseEnvelope,
        )
    except TechpackError:
        raise _workflow_error(
            "workflow_artifact_invalid",
            "Classification response is invalid",
        ) from None
    if (
        response.schema_version != request.schema_version
        or response.job_id != request.job_id
        or response.source_sha256 != request.source_sha256
        or response.glossary_sha256 != request.glossary_sha256
        or response.request_sha256 != request.request_sha256
    ):
        raise _workflow_error(
            "workflow_binding_mismatch",
            "Classification response does not match its request",
        )
    requested_pages = [item.page_index for item in request.items]
    response_pages = [item.page_index for item in response.items]
    if (
        len(response_pages) != len(set(response_pages))
        or set(response_pages) != set(requested_pages)
    ):
        raise _workflow_error(
            "workflow_artifact_invalid",
            "Classification response page set is invalid",
        )
    return response


def _effective_analysis(
    directory: Path,
    job: JobManifest,
    analysis: _AnalysisSnapshot | None = None,
) -> _AnalysisSnapshot | None:
    base = analysis or _load_model(directory, "analysis.json", _AnalysisSnapshot)
    _assert_snapshot_binding(base, job)
    pending = _classification_request_from_analysis(base, job)
    if pending is None:
        return base
    request = _load_classification_request(directory, base, job)
    if not _artifact_exists(directory, "classification-response.json"):
        return None
    response = _load_classification_response(directory, request)
    classified = _apply_classification_response(
        base,
        response,
        load_glossary(_snapshot_glossary_path(directory, job)),
    )
    if classified is not None:
        _ensure_techpack_gate(classified)
    return classified


def _apply_classification_response(
    analysis: _AnalysisSnapshot,
    response: _ClassificationResponseEnvelope,
    glossary: Glossary,
) -> _AnalysisSnapshot | None:
    responses = {item.page_index: item for item in response.items}
    pages: list[_AnalysisPage] = []
    candidates: list[_CandidateSnapshot] = []
    unresolved = False
    for page in analysis.pages:
        existing = [item for item in analysis.candidates if item.page_index == page.page_index]
        pending = page.classification_request
        if pending is None:
            pages.append(page)
            candidates.extend(existing)
            continue
        agent = responses[page.page_index]
        classification = classify_page(
            PageFeatures(
                title=pending.title,
                table_headers=tuple(pending.table_headers),
                visual_features=tuple(pending.visual_features),
                agent_result=AgentClassification(
                    page_type=agent.page_type,
                    confidence=agent.confidence,
                    evidence=tuple(agent.evidence),
                ),
            )
        )
        if (
            classification.page_type is PageType.UNKNOWN
            or classification.confidence < 0.80
            or not any(item.strip() for item in classification.evidence)
        ):
            unresolved = True
            pages.append(page)
            candidates.extend(existing)
            continue
        pages.append(page.model_copy(update={
            "page_type": classification.page_type,
            "confidence": classification.confidence,
            "evidence": list(classification.evidence),
            "classification_request": None,
        }))
        candidates.extend(
            _reselect_page_candidates(
                page.page_index,
                classification,
                existing,
                glossary,
            )
        )
    if unresolved:
        return None
    if [item.item_id for item in candidates] != [item.item_id for item in analysis.candidates]:
        raise _workflow_error(
            "workflow_artifact_invalid",
            "Classification changed the candidate set",
        )
    return analysis.model_copy(update={"pages": pages, "candidates": candidates})


def _reselect_page_candidates(
    page_index: int,
    classification: PageClassification,
    existing: Sequence[_CandidateSnapshot],
    glossary: Glossary,
) -> list[_CandidateSnapshot]:
    nodes: list[PageNode] = []
    for position, candidate in enumerate(existing):
        source_bbox = (
            tuple(candidate.source_bbox)
            if candidate.source_bbox is not None
            else None
        )
        nodes.append(PageNode(
            MatchedNode(
                mineru_index=position,
                native_index=position if source_bbox is not None else None,
                text=candidate.source_text,
                source_bbox=source_bbox,
                mineru_bbox=source_bbox,
                coordinate_confidence=candidate.coordinate_confidence,
                similarity=1.0 if source_bbox is not None else 0.0,
                distance_ratio=0.0 if source_bbox is not None else None,
                auto_approvable=candidate.source_auto_approvable,
            ),
            candidate.source_kind,
        ))
    selected = select_candidates(
        SelectionPage(
            page_index=page_index,
            classification=classification,
            nodes=tuple(nodes),
        ),
        glossary,
    )
    rebuilt = [_candidate_snapshot(candidate) for candidate in selected]
    if [item.item_id for item in rebuilt] != [item.item_id for item in existing]:
        raise _workflow_error(
            "workflow_artifact_invalid",
            "Classification changed stable candidate identities",
        )
    return rebuilt


def _raw_pages(parsed: dict[str, Any] | NativeOnlyDegradation, pdf: PdfManifest) -> dict[int, dict[str, Any]]:
    if isinstance(parsed, NativeOnlyDegradation):
        return {
            page.page_index: {
                "title": _native_title(page),
                "table_headers": _native_table_headers(page),
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
            glossary_hits=tuple(_Hit(hit.source_term, hit.target_term) for hit in item.glossary_hits),
            page_type=item.page_type,
        )
        for item in analysis.candidates
    ]


def _atomic_translation_request(directory: Path, candidates: Sequence[Any], job: JobManifest) -> None:
    temp = directory / f".translation-request.{uuid.uuid4().hex}.tmp"
    owned_identity: tuple[int, int] | None = None
    primary_failed = False
    cleanup_failed = False
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            owned_identity = _owner_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        write_translation_request(candidates, temp, job)
        with temp.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if _owner_identity(temp.lstat()) != owned_identity:
            raise OSError
        os.replace(temp, directory / "translation-request.json")
        _fsync_parent(directory)
    except (OSError, TechpackError):
        primary_failed = True
    finally:
        try:
            if temp.exists() and owned_identity is not None and _owner_identity(temp.lstat()) == owned_identity:
                temp.unlink()
            elif temp.exists():
                cleanup_failed = True
        except OSError:
            cleanup_failed = True
    if primary_failed:
        code = "workflow_atomic_write_cleanup_failed" if cleanup_failed else "workflow_atomic_write_failed"
        raise _workflow_error(code, "Workflow artifact could not be committed")
    if cleanup_failed:
        raise _workflow_error("workflow_atomic_cleanup_failed", "Workflow temporary artifact could not be cleaned")


def _trusted_output(
    directory: Path,
    job: JobManifest,
    analysis: _AnalysisSnapshot,
    translations: Sequence[Any],
) -> _TrustedExpectedOutput:
    translated = {translation.item_id: translation for translation in translations}
    items: list[dict[str, Any]] = []
    for candidate in analysis.candidates:
        if not candidate.should_translate:
            continue
        translation = translated.get(candidate.item_id)
        if translation is None or candidate.source_bbox is None:
            raise _workflow_error("trusted_output_invalid", "Trusted candidate output is incomplete")
        warnings = _quality_warnings(candidate, analysis.parser, translation)
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
                "glossary_hits": [hit.model_dump(mode="json") for hit in candidate.glossary_hits],
                "suggested_translation": translation.translated_text,
                "reviewed_translation": None,
                "review_status": None,
                "risk_level": "high" if warnings or not candidate.auto_approvable else "low",
                "translation_host": translation.translator.host,
                "translation_execution_mode": translation.translator.execution_mode,
                "translation_model": translation.translator.model,
                "translation_agent_role": translation.translator.agent_role,
                "translation_prompt_version": translation.translator.prompt_version,
                "placement_strategy": None,
                "target_rect": None,
                "font_size": None,
                "leader_line": None,
                "warnings": warnings,
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
    return _TrustedExpectedOutput.model_validate({
        "schema_version": _SCHEMA_VERSION,
        "job_id": job.job_id,
        "parser": analysis.parser,
        "translation_executor": "host_agent",
        "pages": pages,
        "items": items,
        "blocking_issues": [],
    })


def _quality_warnings(candidate: _CandidateSnapshot, parser: str, translation: Any) -> list[str]:
    warnings: list[str] = []
    if parser == "native_only":
        warnings.append("native_only_degradation")
    if candidate.coordinate_confidence is not CoordinateConfidence.HIGH:
        warnings.append("coordinate_confidence")
    if translation.translator.model == "unknown":
        warnings.append("unknown_model")
    for warning in translation.warnings:
        if isinstance(warning, str) and warning.strip():
            warnings.append(warning)
    return list(dict.fromkeys(warnings))


def _apply_result_snapshot(
    directory: Path,
    job: JobManifest,
    status: Literal["succeeded", "failed"],
    *,
    output_sha256: str | None = None,
    review_sha256_override: str | None = None,
    final_output_retained: bool = False,
    problems: Sequence[Any] = (),
    unresolved: Sequence[Any] = (),
) -> _ApplyResultSnapshot:
    return _ApplyResultSnapshot(
        schema_version=_SCHEMA_VERSION,
        job_id=job.job_id,
        source_sha256=job.source.sha256,
        glossary_sha256=job.glossary.sha256,
        expected_output_sha256=sha256_file(_inside(directory, "expected-output.json")),
        review_sha256=review_sha256_override or sha256_file(_inside(directory, _TRUSTED_REVIEW_NAME)),
        status=status,
        output_sha256=output_sha256,
        final_output_retained=final_output_retained,
        problems=[_safe_problem_tree(problem) for problem in problems],
        unresolved=[_safe_problem_tree(problem) for problem in unresolved],
        unresolved_overlap_count=len(unresolved),
    )


_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_RESOURCE_KINDS = {
    "output", "final_output", "temporary", "snapshot", "failure_report", "annotation", "resource",
}
_TASK8_KIND_TO_RESOURCE_KIND = {
    "output": "output", "final_output": "final_output", "pdf": "final_output",
    "temporary": "temporary", "temp": "temporary", "staging": "temporary",
    "snapshot": "snapshot", "input_snapshot": "snapshot",
    "failure_report": "failure_report", "report": "failure_report",
    "annotation": "annotation", "freetext": "annotation", "redaction": "annotation",
    "resource": "resource", "file": "resource",
}
_SAFE_OWNERSHIP = {"owned", "foreign_or_unknown", "unknown"}


def _safe_problem_tree(value: Any) -> _SafeApplyProblem:
    """Project Task 8 diagnostics to a structural, non-content report tree."""
    source = value if isinstance(value, Mapping) else {}
    details = source.get("details") if isinstance(source.get("details"), Mapping) else {}
    code = _safe_identifier(source.get("code"), "apply_failed")
    status = _safe_identifier(source.get("status") or details.get("status"), None)
    ownership_value = source.get("ownership") or details.get("ownership")
    if details.get("ownership_mismatch") is True:
        ownership_value = "foreign_or_unknown"
    ownership = _safe_identifier(ownership_value, None)
    if ownership not in _SAFE_OWNERSHIP:
        ownership = None
    raw_kind = source.get("resource_kind") or details.get("resource_kind")
    if raw_kind is None:
        raw_kind = source.get("kind") or details.get("kind")
    kind = _TASK8_KIND_TO_RESOURCE_KIND.get(raw_kind) if isinstance(raw_kind, str) else None
    if kind is None:
        kind = _safe_identifier(raw_kind, None)
    if kind not in _SAFE_RESOURCE_KINDS:
        kind = _kind_for_code(code)
    exists = _bool_or_none(source.get("exists") if "exists" in source else details.get("exists"))
    rollback = _bool_or_none(source.get("rollback") if "rollback" in source else details.get("rollback"))
    children: list[_SafeApplyProblem] = []
    for child in _nested_problem_values(details):
        children.append(_safe_problem_tree(child))
    for key, child_exists in details.items():
        if key.endswith("_exists") and isinstance(child_exists, bool):
            child_kind = _kind_for_exists_key(key)
            children.append(_SafeApplyProblem(
                code=code, resource_kind=child_kind, exists=child_exists,
                ownership=ownership, rollback=rollback,
            ))
    return _SafeApplyProblem(
        code=code, status=status, ownership=ownership, resource_kind=kind,
        exists=exists, rollback=rollback, nested=children,
    )


def _nested_problem_values(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nested: list[Mapping[str, Any]] = []
    for value in details.values():
        if isinstance(value, Mapping):
            nested.append(value)
        elif isinstance(value, list):
            nested.extend(item for item in value if isinstance(item, Mapping))
    return nested


def _safe_identifier(value: Any, fallback: str | None) -> str | None:
    return value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else fallback


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _kind_for_code(code: str) -> str | None:
    if code.startswith("publish_"):
        return "final_output"
    if code.startswith("cleanup_") or code.startswith("temp_"):
        return "temporary"
    if code.startswith("unresolved_"):
        return "annotation"
    return None


def _kind_for_exists_key(key: str) -> str:
    if key.startswith("final_") or key.startswith("output_"):
        return "final_output"
    if key.startswith("temp_"):
        return "temporary"
    if key.startswith("snapshot_"):
        return "snapshot"
    if key.startswith("report_"):
        return "failure_report"
    return "resource"


def _with_absolute_thumbnails(directory: Path, output: Mapping[str, Any]) -> dict[str, Any]:
    material = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    converted = json.loads(json.dumps(material))
    for page in converted["pages"]:
        page["thumbnail"] = str(_inside(directory, page["thumbnail"]))
    return converted


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_job(directory: Path) -> JobManifest:
    job = _load_model(directory, "manifest.json", JobManifest)
    if _absolute_user_path(job.job_dir, must_exist=True, regular=False) != directory:
        raise _workflow_error("workflow_job_invalid", "Manifest job directory does not match")
    if job.source.path is None or job.glossary.path is None:
        raise _workflow_error("workflow_job_invalid", "Manifest input paths are missing")
    return job


def _load_state(directory: Path, job: JobManifest) -> _StateSnapshot:
    state = _load_state_payload(directory)
    _assert_snapshot_binding(state, job)
    if state.state.value not in _STATES:
        raise _workflow_error("workflow_state_invalid", "Workflow state is unknown")
    _verify_state_artifacts(directory, state)
    return state


def _verify_state_artifacts(directory: Path, state: _StateSnapshot) -> None:
    names = {
        "analysis": "analysis.json",
        "classification_request": "classification-request.json",
        "classification_response": "classification-response.json",
        "request": "translation-request.json", "response": "translation-response.json",
        "expected_output": "expected-output.json", "review_html": "review.html", "review": _TRUSTED_REVIEW_NAME,
        "correction_request": "correction-request.json", "apply_result": "apply-result.json",
    }
    for field, name in names.items():
        digest = getattr(state.artifacts, field)
        if digest is None:
            continue
        try:
            if _sha256_artifact(directory, name) != digest:
                raise OSError
        except OSError:
            raise _workflow_error("workflow_artifact_invalid", "Workflow artifact digest does not match state") from None
    required = {
        WorkflowState.TRANSLATION_REQUESTED: ("analysis", "request"),
        WorkflowState.TRANSLATION_VALIDATED: ("analysis", "request", "response"),
        WorkflowState.REVIEW_READY: ("analysis", "request", "response", "expected_output", "review_html"),
    }
    if any(getattr(state.artifacts, field) is None for field in required.get(state.state, ())):
        raise _workflow_error("workflow_state_invalid", "Workflow state lacks required artifacts")


def _verify_state_invariants(state: _StateSnapshot) -> None:
    required = {
        WorkflowState.INITIALIZED: (),
        WorkflowState.PARSED: ("analysis",),
        WorkflowState.TRANSLATION_REQUESTED: ("analysis", "request"),
        WorkflowState.TRANSLATION_VALIDATED: ("analysis", "request", "response"),
        WorkflowState.REVIEW_READY: ("analysis", "request", "response", "expected_output", "review_html"),
        WorkflowState.REVIEW_COMPLETED: ("analysis", "request", "response", "expected_output", "review_html", "review"),
        WorkflowState.APPLYING: ("analysis", "request", "response", "expected_output", "review_html", "review"),
        WorkflowState.SUCCEEDED: ("analysis", "request", "response", "expected_output", "review_html", "review", "apply_result"),
        WorkflowState.FAILED: (),
    }
    if any(getattr(state.artifacts, field) is None for field in required[state.state]):
        raise _workflow_error("workflow_state_invalid", "Workflow state lacks required artifacts")
    allowed = {
        WorkflowState.INITIALIZED: set(),
        WorkflowState.PARSED: {"analysis", "classification_request"},
        WorkflowState.TRANSLATION_REQUESTED: {"analysis", "classification_request", "classification_response", "request", "correction_request"},
        WorkflowState.TRANSLATION_VALIDATED: {"analysis", "classification_request", "classification_response", "request", "response", "correction_request"},
        WorkflowState.REVIEW_READY: {"analysis", "classification_request", "classification_response", "request", "response", "expected_output", "review_html", "correction_request"},
        WorkflowState.REVIEW_COMPLETED: {"analysis", "classification_request", "classification_response", "request", "response", "expected_output", "review_html", "review", "correction_request"},
        WorkflowState.APPLYING: {"analysis", "classification_request", "classification_response", "request", "response", "expected_output", "review_html", "review", "correction_request", "apply_result"},
        WorkflowState.SUCCEEDED: {"analysis", "classification_request", "classification_response", "request", "response", "expected_output", "review_html", "review", "correction_request", "apply_result"},
        WorkflowState.FAILED: {"analysis", "classification_request", "classification_response", "request", "response", "expected_output", "review_html", "review", "correction_request", "apply_result"},
    }
    if state.state is not WorkflowState.FAILED and any(
        getattr(state.artifacts, field) is not None and field not in allowed[state.state]
        for field in _ArtifactDigests.model_fields
    ):
        raise _workflow_error("workflow_state_invalid", "Workflow state contains future artifacts")
    valid_waits = {
        WorkflowState.INITIALIZED: {None},
        WorkflowState.PARSED: {None, "agent_classification"},
        WorkflowState.TRANSLATION_REQUESTED: {"host_translation", "host_correction", "agent_failure", "human_review_required"},
        WorkflowState.TRANSLATION_VALIDATED: {None},
        WorkflowState.REVIEW_READY: {"human_review"},
        WorkflowState.REVIEW_COMPLETED: {None},
        WorkflowState.APPLYING: {None},
        WorkflowState.SUCCEEDED: {None},
        WorkflowState.FAILED: {None},
    }
    if state.wait_reason not in valid_waits[state.state]:
        raise _workflow_error("workflow_state_invalid", "Workflow wait reason is contradictory")
    if state.state in {WorkflowState.INITIALIZED, WorkflowState.PARSED} and state.expected_attempt != 0:
        raise _workflow_error("workflow_state_invalid", "Workflow attempt is contradictory")
    if state.state is WorkflowState.INITIALIZED and state.revision != 0:
        raise _workflow_error("workflow_state_invalid", "Workflow revision is contradictory")
    if state.state is WorkflowState.PARSED and state.revision < 1:
        raise _workflow_error("workflow_state_invalid", "Workflow revision is contradictory")
    if state.state is WorkflowState.PARSED:
        if state.wait_reason == "agent_classification" and state.artifacts.classification_request is None:
            raise _workflow_error("workflow_state_invalid", "Workflow state lacks classification request")
        if state.wait_reason is None and state.artifacts.classification_request is not None:
            raise _workflow_error("workflow_state_invalid", "Workflow classification wait is contradictory")
        if state.artifacts.classification_response is not None:
            raise _workflow_error("workflow_state_invalid", "Parsed state cannot bind classification response")
    if state.state not in {WorkflowState.INITIALIZED, WorkflowState.PARSED, WorkflowState.FAILED}:
        if (state.artifacts.classification_request is None) != (state.artifacts.classification_response is None):
            raise _workflow_error("workflow_state_invalid", "Workflow classification artifacts are incomplete")
    if state.state is WorkflowState.TRANSLATION_REQUESTED:
        if state.wait_reason == "host_translation" and state.expected_attempt != 0:
            raise _workflow_error("workflow_state_invalid", "Workflow attempt is contradictory")
        if state.wait_reason in {"host_correction", "human_review_required"} and state.expected_attempt != 1:
            raise _workflow_error("workflow_state_invalid", "Workflow attempt is contradictory")
        if state.wait_reason in {"host_correction", "human_review_required"} and state.artifacts.correction_request is None:
            raise _workflow_error("workflow_state_invalid", "Workflow state lacks its correction request")


def _load_state_payload(directory: Path) -> _StateSnapshot:
    try:
        state = _StateSnapshot.model_validate(_read_json(directory, "state.json"))
    except (ValidationError, TechpackError):
        raise _workflow_error("workflow_state_invalid", "Workflow state does not match its schema") from None
    _verify_state_invariants(state)
    return state


def _write_state(directory: Path, job: JobManifest, state: WorkflowState, revision: int, expected_attempt: Literal[0, 1], wait_reason: Literal["agent_classification", "host_translation", "host_correction", "agent_failure", "human_review_required", "human_review"] | None) -> None:
    path = directory / "state.json"
    previous: _StateSnapshot | None = None
    if path.exists():
        previous = _load_state(directory, job)
        if previous.revision + 1 != revision or state.value not in _TRANSITIONS[previous.state.value]:
            raise _workflow_error("workflow_state_conflict", "Workflow transition is not legal")
    elif revision != 0 or state is not WorkflowState.INITIALIZED:
        raise _workflow_error("workflow_state_conflict", "Workflow state has no valid predecessor")
    artifacts = _artifact_digests(directory)
    if state is WorkflowState.TRANSLATION_REQUESTED:
        artifacts = artifacts.model_copy(update={"response": None})
    snapshot = _StateSnapshot(
        schema_version=_SCHEMA_VERSION,
        job_id=job.job_id,
        source_sha256=job.source.sha256,
        glossary_sha256=job.glossary.sha256,
        state=state,
        revision=revision,
        expected_attempt=expected_attempt,
        wait_reason=wait_reason,
        artifacts=artifacts,
    )
    _verify_state_invariants(snapshot)
    _atomic_json_write(path, snapshot.model_dump(mode="json"))


def _mark_job_failed(directory: Path, guard: _JobGuard) -> None:
    """Best-effort terminalization while the exact nonblocking job guard is active."""
    if not _guard_is_active(guard) or guard.key != str(directory):
        return
    try:
        job = _load_job(directory)
        before = _bounded_binary_read(_inside(directory, "state.json"))
        state = _StateSnapshot.model_validate(json.loads(before.decode("utf-8")))
        _assert_snapshot_binding(state, job)
        _verify_state_invariants(state)
        if state.state not in {WorkflowState.SUCCEEDED, WorkflowState.FAILED}:
            artifacts = _artifact_digests(directory)
            failed = _StateSnapshot(
                schema_version=_SCHEMA_VERSION,
                job_id=job.job_id,
                source_sha256=job.source.sha256,
                glossary_sha256=job.glossary.sha256,
                state=WorkflowState.FAILED,
                revision=state.revision + 1,
                expected_attempt=state.expected_attempt,
                wait_reason=None,
                artifacts=artifacts,
            )
            _verify_state_invariants(failed)
            if _bounded_binary_read(_inside(directory, "state.json")) != before:
                return
            _atomic_json_write(directory / "state.json", failed.model_dump(mode="json"))
    except Exception:
        return


def _artifact_digests(directory: Path) -> _ArtifactDigests:
    names = {
        "analysis": "analysis.json",
        "classification_request": "classification-request.json",
        "classification_response": "classification-response.json",
        "request": "translation-request.json",
        "response": "translation-response.json", "expected_output": "expected-output.json",
        "review_html": "review.html", "review": _TRUSTED_REVIEW_NAME,
        "correction_request": "correction-request.json", "apply_result": "apply-result.json",
    }
    values: dict[str, str | None] = {}
    for field, name in names.items():
        path = _inside(directory, name)
        try:
            values[field] = _sha256_artifact(directory, name) if path.is_file() else None
        except (OSError, TechpackError):
            values[field] = None
    return _ArtifactDigests.model_validate(values)


def _snapshot_source_path(directory: Path) -> Path:
    return _inside(directory, "input-source.pdf")


def _snapshot_glossary_path(directory: Path, job: JobManifest) -> Path:
    suffix = Path(job.glossary.filename).suffix.casefold()
    if suffix not in {".csv", ".xlsx"}:
        raise _workflow_error("workflow_input_invalid", "Job glossary suffix is invalid")
    return _inside(directory, f"input-glossary{suffix}")


def _input_identities(source: Path, glossary: Path) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    return (_file_identity(source), _file_identity(glossary))


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        raise _workflow_error("workflow_input_unavailable", "Job input is unavailable") from None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _snapshot_inputs(
    directory: Path,
    job: JobManifest,
    identities: tuple[tuple[int, int, int, int], tuple[int, int, int, int]],
) -> tuple[Path, Path]:
    source = Path(job.source.path)
    glossary = Path(job.glossary.path)
    source_snapshot = _snapshot_source_path(directory)
    glossary_snapshot = _snapshot_glossary_path(directory, job)
    _stable_copy(source, source_snapshot, job.source.sha256, identities[0])
    _stable_copy(glossary, glossary_snapshot, job.glossary.sha256, identities[1])
    return source_snapshot, glossary_snapshot


def _stable_snapshot_sha256(path: Path) -> str:
    """Hash one regular snapshot through a no-follow descriptor and stable identity."""
    try:
        before = path.lstat()
        if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if _file_stat_identity(before) != _file_stat_identity(opened):
                raise OSError
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            current = path.lstat()
            if _file_stat_identity(before) != _file_stat_identity(opened) or _file_stat_identity(opened) != _file_stat_identity(after) or _file_stat_identity(after) != _file_stat_identity(current):
                raise OSError
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    except OSError:
        raise _workflow_error("workflow_input_changed", "Job input snapshot changed during analysis") from None


def _verify_snapshot_inputs(
    directory: Path,
    job: JobManifest,
    identities: tuple[tuple[int, int, int, int], tuple[int, int, int, int]],
) -> None:
    if (
        _file_identity(_snapshot_source_path(directory)) != identities[0]
        or _file_identity(_snapshot_glossary_path(directory, job)) != identities[1]
        or
        _stable_snapshot_sha256(_snapshot_source_path(directory)) != job.source.sha256
        or _stable_snapshot_sha256(_snapshot_glossary_path(directory, job)) != job.glossary.sha256
    ):
        raise _workflow_error("workflow_input_changed", "Job input snapshot does not match its manifest")


def _stable_copy(source: Path, target: Path, expected_sha256: str, identity: tuple[int, int, int, int]) -> None:
    if source.is_symlink() or not source.is_file():
        raise _workflow_error("workflow_input_invalid", "Job input is invalid")
    if _file_identity(source) != identity:
        raise _workflow_error("workflow_input_changed", "Job input changed while being snapshotted")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    owned_identity: tuple[int, int] | None = None
    primary: TechpackError | None = None
    cleanup_failed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        owned_identity = _owner_identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as writer:
            with source.open("rb") as reader:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if _owner_identity(temporary.lstat()) != owned_identity:
            raise OSError
        if _file_identity(source) != identity or sha256_file(temporary) != expected_sha256:
            raise _workflow_error("workflow_input_changed", "Job input changed while being snapshotted")
        os.replace(temporary, target)
        _fsync_parent(target.parent)
    except TechpackError as error:
        primary = error
    except OSError:
        primary = _workflow_error("workflow_input_unavailable", "Job input snapshot is unavailable")
    finally:
        try:
            if temporary.exists():
                details = temporary.lstat()
                if owned_identity is None or _owner_identity(details) != owned_identity:
                    cleanup_failed = True
                else:
                    temporary.unlink()
        except OSError:
            cleanup_failed = True
    if cleanup_failed:
        raise _workflow_error("workflow_input_snapshot_cleanup_failed", "Job input snapshot cleanup failed") from None
    if primary is not None:
        raise primary


def _verify_original_inputs(job: JobManifest, identities: tuple[tuple[int, int, int, int]]) -> None:
    source = Path(job.source.path)
    glossary = Path(job.glossary.path)
    if (
        _file_identity(source) != identities[0]
        or _file_identity(glossary) != identities[1]
        or sha256_file(source) != job.source.sha256
        or sha256_file(glossary) != job.glossary.sha256
    ):
        raise _workflow_error("workflow_input_changed", "Job inputs no longer match their manifest")


def _verify_bound_inputs(directory: Path, job: JobManifest) -> None:
    source = _absolute_user_path(job.source.path, must_exist=True, regular=True)
    glossary = _absolute_user_path(job.glossary.path, must_exist=True, regular=True)
    try:
        if sha256_file(source) != job.source.sha256 or sha256_file(glossary) != job.glossary.sha256:
            raise _workflow_error("workflow_input_changed", "Job inputs no longer match their manifest")
        if (
            sha256_file(_snapshot_source_path(directory)) != job.source.sha256
            or sha256_file(_snapshot_glossary_path(directory, job)) != job.glossary.sha256
        ):
            raise _workflow_error("workflow_input_changed", "Job input snapshots no longer match their manifest")
    except OSError:
        raise _workflow_error("workflow_input_unavailable", "Job input is unavailable") from None


def _assert_agent_failure_binding(failure: _AgentFailure, job: JobManifest, request: Any) -> None:
    try:
        request_hash = request["request_sha256"]
    except (KeyError, TypeError):
        raise _workflow_error("workflow_artifact_invalid", "Translation request is invalid") from None
    if (
        failure.job_id != job.job_id
        or failure.source_sha256 != job.source.sha256
        or failure.glossary_sha256 != job.glossary.sha256
        or failure.request_sha256 != request_hash
    ):
        raise _workflow_error("workflow_binding_mismatch", "Agent failure does not match the job")


def _native_title(page: Any) -> str:
    spans = [span for span in page.native_spans if span.text.strip()]
    if not spans:
        return ""
    upper = [span for span in spans if span.bbox[1] <= page.crop_box[3] * 0.30]
    pool = upper or spans
    selected = sorted(pool, key=lambda span: (-span.font_size, span.bbox[1], span.bbox[0], span.text))[0]
    return selected.text


def _native_table_headers(page: Any) -> list[str]:
    headers = {"pom", "description", "tolerance", "material", "composition", "weight", "supplier", "component"}
    found: list[str] = []
    for span in page.native_spans:
        for token in span.text.casefold().replace("/", " ").replace(",", " ").split():
            if token in headers and token not in found:
                found.append(token)
    return found


def _batch_exit_code(results: Sequence[WorkflowResult]) -> int:
    for code in (3, 2, 5, 4, 0):
        if any(result.exit_code == code for result in results):
            return code
    return 2


def _assert_snapshot_binding(snapshot: Any, job: JobManifest) -> None:
    if (
        snapshot.job_id != job.job_id
        or snapshot.source_sha256 != job.source.sha256
        or snapshot.glossary_sha256 != job.glossary.sha256
    ):
        raise _workflow_error("workflow_binding_mismatch", "Workflow artifact does not match the job")


def _job_directory(path: str | Path) -> Path:
    directory = _absolute_user_path(path, must_exist=True, regular=False)
    if not directory.is_dir():
        raise _workflow_error("workflow_job_invalid", "Job directory is invalid")
    return directory


def _absolute_user_path(value: str | Path, *, must_exist: bool, regular: bool) -> Path:
    try:
        raw = Path(value)
        path = Path(os.path.abspath(raw if raw.is_absolute() else Path.cwd() / raw))
    except (OSError, TypeError, ValueError):
        raise _workflow_error("workflow_path_invalid", "Path is invalid") from None
    _assert_no_reparse_components(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise _workflow_error("workflow_path_invalid", "Path is unavailable") from None
        return path
    except OSError:
        raise _workflow_error("workflow_path_invalid", "Path is unavailable") from None
    if _is_reparse_or_link(details):
        raise _workflow_error("workflow_path_invalid", "Path is invalid")
    if regular and not stat.S_ISREG(details.st_mode):
        raise _workflow_error("workflow_path_invalid", "Path is invalid")
    return path


def _assert_no_reparse_components(path: Path) -> None:
    current = path
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise _workflow_error("workflow_path_invalid", "Path is unavailable") from None
        else:
            if _is_reparse_or_link(details):
                raise _workflow_error("workflow_path_invalid", "Path is invalid")
        if current == current.parent:
            return
        current = current.parent


def _is_reparse_or_link(details: Any) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)


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
    try:
        _assert_no_reparse_components(path)
    except TechpackError:
        raise _workflow_error("workflow_artifact_invalid", "Artifact path is invalid") from None
    return path


def _artifact_exists(directory: Path, name: str) -> bool:
    path = _inside(directory, name)
    return path.exists()


def _bounded_binary_read(path: Path) -> bytes:
    """Read a regular file from one stable descriptor, bounded and race checked."""
    try:
        before = path.lstat()
        if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode):
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _file_stat_identity(before) != _file_stat_identity(opened) or opened.st_size > _MAX_JSON_BYTES:
                raise OSError
            parts: list[bytes] = []
            remaining = _MAX_JSON_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                parts.append(chunk)
                remaining -= len(chunk)
            value = b"".join(parts)
            after = os.fstat(descriptor)
            if (
                len(value) > _MAX_JSON_BYTES
                or _file_stat_identity(opened) != _file_stat_identity(after)
                or len(value) != after.st_size
            ):
                raise OSError
            return value
        finally:
            os.close(descriptor)
    except OSError:
        raise _workflow_error("workflow_artifact_invalid", "Workflow JSON artifact is invalid") from None


def _file_stat_identity(details: Any) -> tuple[int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _owner_identity(details: Any) -> tuple[int, int]:
    """Identity fields that remain stable while an exclusively created file is written."""
    return (details.st_dev, details.st_ino)


def _unlink_owned(path: Path, owner: tuple[int, int]) -> bool:
    """Remove only the exact regular file object captured by its creator."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if _is_reparse_or_link(details) or not stat.S_ISREG(details.st_mode) or _owner_identity(details) != owner:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _publish_review_no_clobber(
    pending: Path,
    trusted: Path,
    owner: tuple[int, int],
    digest: str,
) -> None:
    """Atomically expose one validated pending review without overwriting any file."""
    published = False
    try:
        if _owner_identity(pending.lstat()) != owner or trusted.exists():
            raise OSError
        os.link(pending, trusted, follow_symlinks=False)
        published = True
        _fsync_parent(trusted.parent)
        details = trusted.lstat()
        if (
            _is_reparse_or_link(details)
            or not stat.S_ISREG(details.st_mode)
            or _owner_identity(details) != owner
            or _sha256_artifact(trusted.parent, trusted.name) != digest
        ):
            raise OSError
    except BaseException as error:
        if published and not _unlink_owned(trusted, owner):
            raise _workflow_error("workflow_atomic_cleanup_failed", "Validated review rollback failed") from None
        if isinstance(error, (OSError, TechpackError)):
            raise _workflow_error("workflow_artifact_invalid", "Validated review could not be published") from None
        raise


def _cleanup_linked_review_pending(directory: Path, trusted_owner: tuple[int, int]) -> None:
    """Remove only crash-left pending names that are hard links to the validated review."""
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        raise _workflow_error("workflow_atomic_cleanup_failed", "Review recovery cleanup failed") from None
    for candidate in entries:
        if not _PENDING_REVIEW_NAME.fullmatch(candidate.name):
            continue
        try:
            details = candidate.lstat()
        except OSError:
            continue
        if (
            _is_reparse_or_link(details)
            or not stat.S_ISREG(details.st_mode)
            or _owner_identity(details) != trusted_owner
        ):
            continue
        if not _unlink_owned(candidate, trusted_owner):
            raise _workflow_error("workflow_atomic_cleanup_failed", "Review recovery cleanup failed") from None


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _sha256_artifact(directory: Path, name: str) -> str:
    return _sha256_bytes(_bounded_binary_read(_inside(directory, name)))


def _read_json(directory: Path, name: str) -> Any:
    path = _inside(directory, name)
    try:
        return json.loads(_bounded_binary_read(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _workflow_error("workflow_artifact_invalid", "Workflow JSON artifact is invalid") from None


def _response_input(directory: Path) -> Path | object:
    """Keep bounded path safety while letting Task 6 own malformed-response policy."""
    path = _inside(directory, "translation-response.json")
    try:
        return _bounded_binary_read(path).decode("utf-8")
    except (OSError, UnicodeError, TechpackError):
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
    _atomic_binary_write(path, text.encode("utf-8"))


def _atomic_binary_write(path: Path, value: bytes) -> tuple[int, int]:
    directory = path.parent
    temp = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    owned_identity: tuple[int, int] | None = None
    primary_failed = False
    cleanup_failed = False
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        owned_identity = _owner_identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if _owner_identity(temp.lstat()) != owned_identity:
            raise OSError
        os.replace(temp, path)
        _fsync_parent(directory)
    except OSError:
        primary_failed = True
    finally:
        try:
            if temp.exists():
                details = temp.lstat()
                if owned_identity is not None and _owner_identity(details) != owned_identity:
                    cleanup_failed = True
                elif owned_identity is not None:
                    temp.unlink()
                else:
                    cleanup_failed = True
        except OSError:
            cleanup_failed = True
    if primary_failed:
        code = "workflow_atomic_write_cleanup_failed" if cleanup_failed else "workflow_atomic_write_failed"
        raise _workflow_error(code, "Workflow artifact could not be committed")
    if cleanup_failed:
        raise _workflow_error("workflow_atomic_cleanup_failed", "Workflow temporary artifact could not be cleaned")
    if owned_identity is None:
        raise _workflow_error("workflow_atomic_write_failed", "Workflow artifact ownership was not captured")
    return owned_identity


def _fsync_parent(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (AttributeError, OSError):
        return


def _relative_artifact(directory: Path, path: Path) -> str:
    try:
        return str(Path(os.path.abspath(path)).relative_to(directory))
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
