---
name: translating-techpack-pdfs
description: Use when an English apparel TechPack PDF needs reviewed Chinese production annotations.
---

# Translating TechPack PDFs

Confirm the PDF or first-level PDF directory, the required XLSX/CSV glossary, and the job directory. Read [the translation policy](references/translation-policy.md), then run:

```text
python scripts/techpack_pdf_cli.py analyze <pdf-or-directory> --glossary <glossary> --job-dir <job-root>
```

When a job contains `translation-request.json`, read [the Agent contract](references/agent-contract.md). Have the Host Agent or a read-only translation sub-agent return the strict response envelope as `translation-response.json`, then run `python scripts/techpack_pdf_cli.py prepare-review --job <job-dir>`.

Stop for the user to review every item in `review.html` and export `review.json`. Do not apply before that file exists.

After review, read [the review and apply gates](references/review-and-apply.md), then run:

```text
python scripts/techpack_pdf_cli.py apply <source.pdf> --review <job-dir>/review.json --output <source.pdf>.annotated.pdf
```

Translate only selected production content. Preserve every locked token exactly; never convert units or numbers. The deliverable is the original document plus approved red, editable FreeText annotations, never a full-document translation.

If any command or gate fails, stop, preserve the job artifacts, and report the returned status or error code. Do not bypass validation or review.
