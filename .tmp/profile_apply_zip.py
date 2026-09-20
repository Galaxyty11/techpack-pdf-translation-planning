from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path


SKILL = Path(r"D:\PDF翻译skill实施\.tmp\zip-analysis-20260917\techpack-pdf-translation-planning-main")
JOB = Path(r"D:\PDF翻译skill实施\.techpack-jobs\figure-coverage-export-final\88fa7b84c158-20260915T154312Z")
sys.path.insert(0, str(SKILL / "scripts"))

import pymupdf

from techpack_pdf.apply import _placements_from_review, _verify_temp, _write_annotations
from techpack_pdf.models import JobManifest
from techpack_pdf.review import load_review
from techpack_pdf.workflow import (
    _ExpectedOutputSnapshot,
    _load_state,
    _verify_apply_integrity_closure,
    _verify_bound_inputs,
)


def timed(label, operation):
    started = time.perf_counter()
    value = operation()
    elapsed = time.perf_counter() - started
    print(json.dumps({"step": label, "seconds": round(elapsed, 3)}, ensure_ascii=False), flush=True)
    return value


manifest = JobManifest.model_validate(json.loads((JOB / "manifest.json").read_text(encoding="utf-8")))
expected = _ExpectedOutputSnapshot.model_validate(
    json.loads((JOB / "expected-output.json").read_text(encoding="utf-8"))
)
state = _load_state(JOB, manifest)
source = Path(manifest.source.path)

timed("verify_bound_inputs", lambda: _verify_bound_inputs(JOB, manifest))
timed(
    "verify_apply_integrity_closure",
    lambda: _verify_apply_integrity_closure(JOB, manifest, state, expected),
)
review = timed(
    "load_review",
    lambda: load_review(JOB / "trusted-review.json", manifest, expected.output),
)
placements = _placements_from_review(
    tuple(item for item in review.items if item.review_status.value in {"approved", "approved_edited"})
)
print(json.dumps({"approved_annotations": len(placements)}, ensure_ascii=False), flush=True)

with tempfile.TemporaryDirectory(prefix="techpack-profile-", dir=str(SKILL.parent)) as directory:
    temporary = Path(directory) / "profile.pdf"
    timed("copy_source", lambda: shutil.copyfile(source, temporary))
    document = timed("open_writable_pdf", lambda: pymupdf.open(temporary))
    records = []
    try:
        for index, placement in enumerate(placements, start=1):
            record = timed(
                f"write_annotation_{index:02d}",
                lambda placement=placement: _write_annotations(document, (placement,))[0],
            )
            records.append(record)
        timed("save_incremental", document.saveIncr)
    finally:
        document.close()
    outcome = timed(
        "verify_temp",
        lambda: _verify_temp(source, temporary, placements, written=records),
    )
    print(json.dumps({"verification_problem": outcome.problem}, ensure_ascii=False), flush=True)
