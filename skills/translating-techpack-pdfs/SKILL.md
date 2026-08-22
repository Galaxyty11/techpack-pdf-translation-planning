---
name: translating-techpack-pdfs
description: Use when an English apparel TechPack PDF needs reviewed Chinese production annotations.
---

# Translating TechPack PDFs

Set `SKILL_DIR` to this `SKILL.md`'s absolute directory and `PY311` to the configured Python 3.11 interpreter's absolute path. Stop if missing; never use unqualified `python` or a cwd-relative CLI.

Confirm input, required XLSX/CSV glossary, and job directory. Read [the translation policy](references/translation-policy.md), then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" analyze "<absolute-input>" --glossary "<absolute-glossary>" --job-dir "<absolute-job-root>"
```

For `classification-request.json`, read [the Agent contract](references/agent-contract.md). The visual Host Agent or read-only sub-agent saves bound `classification-response.json`; then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" prepare-review --job "<absolute-job>"
```

If classification stays `parsed`, stop; do not skip unknown pages. For `translation-request.json`, the Host Agent or read-only sub-agent saves strict `translation-response.json`; rerun `prepare-review`.

The user reviews every item in `review.html` and exports `review.json`. Do not apply before it exists.

After review, read [the review and apply gates](references/review-and-apply.md), then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" apply "<absolute-source.pdf>" --review "<absolute-job>/review.json" --output "<absolute-source.pdf>.annotated.pdf"
```

Translate selected production content only. Preserve locked tokens and order; never convert units or numbers. Deliver approved red editable FreeText annotations, never a full-document translation.

On failure, stop, preserve artifacts, and report the status or error code. Never bypass validation or review.
