# Review and apply gates

## Offline review

`prepare-review` creates a self-contained `review.html`; it does not create approval. The page must remain offline and display the source location, suggested translation, locked tokens, glossary hits, selection reason, coordinate confidence, layout risk, and translation provenance. Generation clears all incoming review states.

Stop for the user to decide every item as `approved`, `approved_edited`, or `skipped`. Export `review.json` only when every item has a decision and no blocking issue remains. Never infer approval from a suggested translation or edit `review.json` on the user's behalf.

## Review trust boundary

Apply accepts only the job's strict schema 1.1 `review.json`, bound to the same `job_id`, source/glossary filenames and SHA-256 values, source page count, pipeline provenance, candidate set, and trusted `expected-output.json`. Except for `review_status` and `reviewed_translation`, every item field must match the trusted snapshot. Approved text is rechecked for exact locked tokens, authoritative glossary targets, and independently preserved do-not-translate source text. Any stale, cross-job, missing, extra, duplicate, edited, or unresolved item stops apply; there is no ignore option.

The local operating-system user, Host Agent, and processes with job-directory write access are trusted. v1 detects accidental corruption, missing or stale artifacts, cross-job exchange, and uncoordinated changes using SHA-256, strict schemas, exact job binding, stable input snapshots, canonical artifact reconstruction, path/reparse checks, and before/after digest checks. It does not defend against the same user deliberately or coordinately rewriting all job files, and uses no HMAC, external secret, credential store, or external trust record.

Different jobs are independent. Parallel execution of the same job is unsupported. Its non-blocking per-job guard returns `status=workflow_busy`, exit code 4, without waiting, queuing, or changing state; retry only after the active operation ends.

## PDF application

Apply only `approved` and `approved_edited` items; `skipped` items add nothing. A valid all-skipped review still produces a gated faithful copy. Preserve page count, order, MediaBox/CropBox, all original English, images, vectors, existing annotations, and page settings.

Annotations are editable FreeText with red RGB `(0.85, 0.05, 0.05)`, no visible fill or border, and measured CJK text at 7 pt down to a hard 5 pt minimum. Keep them inside the CropBox and away from original text, images, table lines, arrows, rulers, existing annotations, and other new annotations. Place deterministically in an empty table cell, nearby semantic whitespace, a rewrapped rectangle, a smaller permitted font, then a margin lane with a safe leader line.

Geometry and rendered-pixel checks must pass after placement and after global reflow. Stop after at most 10 global rounds or two unchanged rounds. Any overlap, clipping, boundary violation, unsafe leader line, unreadable appearance, or other unresolved issue blocks publication.

Publish only `<complete-source-filename>.annotated.pdf` by atomic no-clobber; never overwrite an existing output. The final PDF must reopen and rerender, contain one traceable annotation per approved item, preserve exact locked tokens and glossary terms, keep all FreeText editable and visible at 5–7 pt, and have no unresolved overlap or error report. Any failed acceptance gate returns failure and leaves no misleading final PDF.
