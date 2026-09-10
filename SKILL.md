---
name: translating-techpack-pdfs
description: Use when an English apparel TechPack PDF needs reviewed Chinese production annotations.
---

# Translating TechPack PDFs

## First use

Ask for the user's MinerU API base URL on first use, for example `http://127.0.0.1:8000`. Reuse an address already provided in the conversation. Confirm the endpoint before submitting the PDF. Stop and explain missing runtime, unavailable API or invalid glossary; do not install dependencies or silently change parsers.

This repository root is the skill directory. For installation, place its contents in a folder named `translating-techpack-pdfs` inside the host's skills directory, keeping SKILL.md, scripts, assets and agents together. Set absolute `SKILL_DIR` to that folder and absolute `PY311` to the configured Python 3.11 executable. Check existing dependencies with that interpreter: pymupdf, pydantic, httpx, pandas, openpyxl, numpy, cv2 and rapidfuzz. The adjacent requirements.txt records the verified environment's versions; install it only after user approval. Never use unqualified Python or a cwd-relative CLI. No separate model API key is needed: the current host Agent translates with its own model, optionally using read-only sub-agents.

Find an existing configured Python 3.11 interpreter in the user's environment or ask for its path if none is known. Confirm absolute source, glossary and job-root paths. Accept a PDF or one-level PDF folder, and an XLSX/CSV glossary using the columns in the glossary gate below. The CLI uses the local MinerU default. For another user-provided API address, use the existing Python interface from a host-created helper with `SKILL_DIR/scripts` on its import path:

```python
from techpack_pdf.mineru import MinerUClient
from techpack_pdf.workflow import analyze
result = analyze(source, glossary, job_root, mineru_client=MinerUClient(base_url=mineru_api))
print(result.to_dict())
```

Do not invent a CLI API-address flag. Follow the translation policy below.

## Workflow

For the confirmed default endpoint:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" analyze "<absolute-input>" --glossary "<absolute-glossary>" --job-dir "<absolute-job-root>"
```

Retain the returned job directory. Await a running process by its session handle; do not launch analyze again to poll progress, since that creates another job. Read the structured state and wait reason; exit code 4 normally means waiting for agent work or human review.

When classification-request.json exists, inspect the requested thumbnails, follow the Agent contract below and save the matching classification-response.json. Then run:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" prepare-review --job "<absolute-job>"
```

When translation-request.json appears, translate its exact item set, validate the response contract, save translation-response.json and rerun prepare-review. A sub-agent returns data only; the host validates and writes it. Never copy another job's request, response or expected-output files into this job.

Open review.html. Review page by page, with higher-risk items first. Users can edit Chinese, drag translation boxes, move the red source box or redraw it with “重新框选原文”, and choose a 5–24 pt font or restore automatic sizing. Source re-framing corrects location without rewriting or retranslating source text. Edits, movement and font changes reset the item to pending review. Each approval, edited approval or skip advances to the next unreviewed item.

Only the user decides approvals. After every item has a decision, export with “完成审核并导出”. Copy the supplied export unchanged to the matching job's review.json; preserve an earlier export if one exists. Confirm its job identity and review details.

The JSON is downloaded by the user's browser, usually to Downloads; ask them to attach it or supply its exact path. In-page changes are held in memory, not autosaved: keep the page open until the completed review is exported. Reopening the HTML starts from its embedded state. Do not regenerate an actively reviewed page without preserving the user's exported decisions. The current workflow has no supported in-place retry for a failed apply task; retain the report and diagnose the failure before preparing a fresh task with properly regenerated bindings.

A dragged translation box stays fixed. Automatic sizing can wrap and shrink to 5 pt; an explicit reviewed size must be honored exactly. If text does not fit, explain which item needs shorter text or a smaller font. Do not silently move the box or reduce an explicit font.

After following the application gates below:

```text
"<PY311>" "<SKILL_DIR>/scripts/techpack_pdf_cli.py" apply "<absolute-source.pdf>" --review "<absolute-job>/review.json" --output "<absolute-source.pdf>.annotated.pdf"
```

The browser exports review data, not PDF. Deliver approved red editable FreeText annotations while preserving the source. Do not reset failed job states by hand. Retain the failed task and report; diagnose or use a supported fresh task.

## Runtime resources

All runtime guidance is in this entrypoint. Retain scripts/, assets/ and agents/openai.yaml: executable code and UI assets cannot be replaced with prose. User-facing packages exclude planning, tests, development logs, generated jobs, private samples and Python caches.

## Translation policy

## Glossary gate

The glossary is required and must contain non-empty `source_term` plus `target_term` unless the row is a do-not-translate entry. `aliases` is a `|`-separated string. Optional-field defaults are: `aliases=""`, `category="general"`, `context=""`, `do_not_translate=false`, `priority=0`, and `notes=""`.

- Match after Unicode NFKC normalization, case folding, whitespace compression, and common punctuation normalization; preserve the displayed source form.
- Prefer word-boundary matches, the longest match, then higher priority. A do-not-translate hit overrides an ordinary translation.
- Stop on conflicting targets for the same normalized term at the same priority. Never choose one silently.
- Treat the glossary target as authoritative. A failed target or do-not-translate check may use the one correction cycle defined in the Agent contract, but cannot be waived.

## Page and candidate scope

Classify with deterministic title, table, and existing visual-structure rules before model judgment. The only page types are `general_info`, `bom`, `measurement`, `technical_drawing`, `label_pack`, `sample_review`, `style_sample`, `how_to_measure`, `construction_detail`, `category_fields`, and `unknown`. Use the bound visual Agent checkpoint only for a conflict or unknown result. Without visual capability, with empty evidence, or below 0.80 confidence, keep the job at `parsed` for manual classification; do not silently drop the page or generate its translation request.

Select only content a factory must execute or verify:

- BOM: materials, composition, weight, use, components, and process notes.
- Measurement: POM descriptions and style-specific measuring instructions.
- Technical drawings and construction details: actionable sewing, structure, spacing, placement, or finishing instructions.
- Sample/style review: faithfully condense conclusions, problems, exceptions, corrective actions, conditions, negation, and measurements.
- Label/pack: names, sequence, folding, placement, and factory operations.

Skip administrative fields, headers, footers, page numbers, system metadata, generic How To Measure guidance, blank templates, repeated content, pure codes, and pure numbers. General information is selected only when it changes production or delivery. Preserve every page in its original order; default-skip never means delete.

## Text constraints

Lock and preserve the exact spelling, value, multiplicity, and order of style/order/POM/material/vendor codes, SKU and barcodes; people and customer names; dates, seasons, versions, and statuses; TCX/Pantone and other identifiers; sizes, quantities, measurements, tolerances, percentages, currency values, and units such as `mm`, `cm`, `inch`, `gsm`, and `oz`.

Translate only the remaining natural language. Never convert, round, normalize, or rewrite locked tokens. Use concise apparel-industry Chinese when space is limited, without dropping an action, negation, condition, exception, or production conclusion.

## Host Agent classification and translation contract

The Python workflow does not call a model API. The current Host Agent performs an optional visual classification checkpoint and translates each selected item itself or delegates to a read-only sub-agent. A sub-agent returns JSON only: it must not edit the PDF, job files, review state, or task state. The Host Agent validates and saves each response file.

Every envelope, item, and nested object below is `extra=forbid`: return every required key and no undocumented key.

## Visual classification checkpoint

When `classification-request.json` exists, read it without recreating its page set. It is a strict schema 1.1 object with `job_id: string`, lowercase 64-character `source_sha256`, `glossary_sha256`, canonical `request_sha256`, and `items: array`. Each request item contains one stable `page_index: integer`, `reason: unknown|conflict|low_confidence`, `title: string`, `table_headers: string[]`, `visual_features: string[]`, `evidence: string[]`, and the minimal job-local `thumbnail: string` reference. Types are strict: every string is a JSON string and every array is a JSON array; `page_index` is a JSON integer, never a numeric string or boolean.

Return `classification-response.json` with the exact five binding fields and exactly one item for every requested page, with no duplicate, missing, or extra page:

```json
{
  "schema_version": "1.1",
  "job_id": "<exact request value>",
  "source_sha256": "<exact request value>",
  "glossary_sha256": "<exact request value>",
  "request_sha256": "<exact request value>",
  "items": [
    {
      "page_index": 11,
      "page_type": "technical_drawing",
      "confidence": 0.94,
      "evidence": ["callout arrows connected to garment construction lines"]
    }
  ]
}
```

`page_type` is one of `general_info|bom|measurement|technical_drawing|label_pack|sample_review|style_sample|how_to_measure|construction_detail|category_fields|unknown`; `confidence` is a JSON number from 0 through 1 (integer or decimal, never a numeric string or boolean); `evidence` is a required JSON array of trimmed non-empty JSON strings when populated. The workflow continues only for non-`unknown` results with confidence at least 0.80 and non-empty evidence. Otherwise it keeps the same job at `parsed` with `wait_reason=agent_classification`; overwrite the response only after obtaining a valid manual/visual classification, then rerun `prepare-review`.

## Translation exchange

Read the generated `translation-request.json`; do not recreate or broaden its item set. It is a strict schema 1.1 envelope containing `job_id`, `source_sha256`, `glossary_sha256`, `request_sha256`, `attempt: 0`, and `items`. Each item supplies the stable `item_id`, source text, context, locked tokens, required glossary terms, page type, and either `direct` or `faithful_digest` mode.

Return a strict object, never a bare array or prose:

```json
{
  "schema_version": "1.1",
  "job_id": "<exact request value>",
  "source_sha256": "<exact request value>",
  "glossary_sha256": "<exact request value>",
  "request_sha256": "<exact request value>",
  "attempt": 0,
  "items": [
    {
      "item_id": "p012-i004",
      "translated_text": "领口明线距边 0.6 cm",
      "preserved_tokens": ["0.6", "cm"],
      "glossary_terms_used": ["topstitch"],
      "mode": "direct",
      "warnings": [],
      "translator": {
        "host": "codex",
        "execution_mode": "main_agent",
        "model": "unknown",
        "agent_role": null,
        "prompt_version": "1.0"
      }
    }
  ]
}
```

Translation response types are exact: `schema_version="1.1"`; binding hashes are lowercase 64-character strings; `attempt` is `0|1`; `items` is an array. Every item requires `item_id`, nonblank `translated_text`, `preserved_tokens: string[]`, `glossary_terms_used: string[]`, `mode: direct|faithful_digest`, `warnings: string[]` (required even when empty), and `translator`. The translator requires nonblank `host`, `execution_mode: main_agent|subagent|mixed`, nonblank `model`, the structurally required key `agent_role: string|null`, and nonblank `prompt_version`. `model` and non-null `agent_role` must be trimmed; use exact `unknown` when the host cannot report the model. `main_agent` may explicitly use `agent_role: null` or a meaningful nonblank role; `subagent` and `mixed` require a meaningful nonblank role. Omitting `agent_role`, or using null/blank for delegated execution, is a translation validation failure handled by the same one-correction rule, not a later workflow gate. The validated value is persisted as the structurally required nullable `translation_agent_role` review field under the same execution-mode rule; review loading must accept a bound `main_agent` null.

Echo all binding fields exactly. Return every requested `item_id` exactly once and no others; ordering is irrelevant. Match each requested mode. `preserved_tokens` must exactly equal the request's `locked_tokens` sequence, and the translated text must contain the same case-sensitive, boundary-safe occurrences in the same order and multiplicity. Report exactly the requested source terms in `glossary_terms_used`, use every authoritative target, and include provenance per item.

## One correction and stopping rules

Run `prepare-review` after saving the initial response. If it emits `correction-request.json`, correct only its `failed_item_ids` and `required_fixes`, retain the same binding fields, overwrite the same `translation-response.json` with a complete response envelope containing every requested item and `attempt: 1`, then rerun `prepare-review`. This is the only correction. Another failure, an Agent interruption, missing context, exhausted credits, or sub-agent failure stops translation and preserves the checkpoint; never fabricate results or bypass review.

Keep one model and prompt version where practical. Cache reuse requires matching request content, glossary, prompt version, host, reported model, and execution mode. A model recorded as `unknown` is cacheable only within the current job. Never record host or model credentials.

## Review and apply gates

## Offline review

`prepare-review` creates a self-contained `review.html`; it does not create approval. The page must remain offline and display the source location, suggested translation, locked tokens, glossary hits, selection reason, coordinate confidence, layout risk, and translation provenance. Generation clears all incoming review states.

Stop for the user to decide every item as `approved`, `approved_edited`, or `skipped`. Export `review.json` only when every item has a decision and no blocking issue remains. Never infer approval from a suggested translation or edit `review.json` on the user's behalf.

## Review trust boundary

Apply accepts the job's schema 1.1 review bound to its source, glossary and trusted candidate snapshot. Editable fields are `review_status`, `reviewed_translation`, `reviewed_target_rect`, `reviewed_source_bbox` and `reviewed_font_size`; other item fields stay bound. Optional fields remain compatible with old reviews. Source re-framing must have finite coordinates and positive area within the same page; it does not alter recognized text or trigger translation. Translation dragging retains the original box dimensions. Explicit font size is finite and 5–24 pt, and must be honored exactly. Recheck locked tokens and authoritative glossary terms before writing.

The local operating-system user, Host Agent, and processes with job-directory write access are trusted. v1 detects accidental corruption, missing or stale artifacts, cross-job exchange, and uncoordinated changes using SHA-256, strict schemas, exact job binding, stable input snapshots, canonical artifact reconstruction, path/reparse checks, and before/after digest checks. It does not defend against the same user deliberately or coordinately rewriting all job files, and uses no HMAC, external secret, credential store, or external trust record.

Different jobs are independent. Parallel execution of the same job is unsupported. Its non-blocking per-job guard returns `status=workflow_busy`, exit code 4, without waiting, queuing, or changing state; retry only after the active operation ends.

## PDF application

Apply only `approved` and `approved_edited` items; `skipped` items add nothing. A valid all-skipped review still produces a gated faithful copy. Preserve page count, order, MediaBox/CropBox, all original English, images, vectors, existing annotations, and page settings.

Annotations are editable red FreeText, with no visible fill or border. Automatic CJK sizing remains 5–7 pt; an explicit reviewed size may be 5–24 pt and must not be silently reduced. Use the reviewed source box as the placement reference. A dragged translation box stays fixed; if text cannot fit, report the item for shorter text or a smaller user-selected font. Automatic placement otherwise follows the existing empty-cell, whitespace and margin rules.

Geometry and rendered-pixel checks must pass after placement and after global reflow. Stop after at most 10 global rounds or two unchanged rounds. Any overlap, clipping, boundary violation, unsafe leader line, unreadable appearance, or other unresolved issue blocks publication.

Publish only `<complete-source-filename>.annotated.pdf` by atomic no-clobber; never overwrite an existing output. The final PDF must reopen and rerender, contain one traceable annotation per approved item, preserve exact locked tokens and glossary terms, and keep FreeText editable at the planned automatic size or the exact explicit reviewed size. Any failed acceptance gate returns failure and leaves no misleading final PDF.
