from __future__ import annotations

import sys
from pathlib import Path

from techpack_pdf.apply import apply_review
from techpack_pdf.models import JobManifest
from techpack_pdf.workflow import _ExpectedOutputSnapshot, _load_model


WORKTREE = Path(r"D:\PDF翻译skill实施\.worktrees\figure-annotation-coverage")
JOB_DIR = Path(r"D:\PDF翻译skill实施\.techpack-jobs\figure-coverage-test\88fa7b84c158-20260914T144518Z")
SOURCE = Path(r"D:\PDF翻译skill实施\.tmp\figure-export-diagnostic\input-source.pdf")


def trace_apply_exception(frame, event, arg):
    if event == "exception" and frame.f_code.co_name == "apply_review":
        exc_type, exc_value, _ = arg
        print(
            f"APPLY_EXCEPTION line={frame.f_lineno} "
            f"type={exc_type.__name__} value={exc_value!r}",
            flush=True,
        )
    return trace_apply_exception


job = _load_model(JOB_DIR, "manifest.json", JobManifest)
job = job.model_copy(
    update={"source": job.source.model_copy(update={"path": str(SOURCE)})}
)
expected = _load_model(
    JOB_DIR, "expected-output.json", _ExpectedOutputSnapshot
)
sys.settrace(trace_apply_exception)
try:
    result = apply_review(SOURCE, JOB_DIR / "review.json", job, expected.output)
finally:
    sys.settrace(None)
print(result)
