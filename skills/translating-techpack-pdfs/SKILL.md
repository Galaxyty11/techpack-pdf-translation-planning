---
name: translating-techpack-pdfs
description: Use when an English apparel TechPack PDF needs reviewed Chinese production annotations.
---

# Translating TechPack PDFs

Set absolute `SKILL_DIR` to this file's directory and absolute `PY311` to configured Python 3.11. Stop if missing; never use unqualified `python` or cwd-relative CLI.

Confirm input/glossary/job paths. Read [the translation policy](references/translation-policy.md), then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" analyze "<absolute-input>" --glossary "<absolute-glossary>" --job-dir "<absolute-job-root>"
```

For `classification-request.json`, read [the Agent contract](references/agent-contract.md). The visual Host Agent classifies or validates strict JSON returned by a read-only sub-agent, then saves bound `classification-response.json` and runs:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" prepare-review --job "<absolute-job>"
```

If still `parsed`, stop; never skip unknown pages. For `translation-request.json`, the Host translates or validates strict JSON returned by a read-only sub-agent, saves `translation-response.json`, then reruns `prepare-review`.

Stop and require the user to explicitly approve, edit-and-approve, or skip every item in `review.html`, then export `review.json`. Do not apply before it exists.

Read [the review and apply gates](references/review-and-apply.md), then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" apply "<absolute-source.pdf>" --review "<absolute-job>/review.json" --output "<absolute-source.pdf>.annotated.pdf"
```

Translate only selected production content. Preserve locked-token order; never convert units or numbers. Deliver approved red editable FreeText annotations, never a full-document translation.

On failure, stop and report status/error; never bypass validation or review.
