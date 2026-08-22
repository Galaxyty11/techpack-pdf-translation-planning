# Host Agent classification and translation contract

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

Translation response types are exact: `schema_version="1.1"`; binding hashes are lowercase 64-character strings; `attempt` is `0|1`; `items` is an array. Every item requires `item_id`, nonblank `translated_text`, `preserved_tokens: string[]`, `glossary_terms_used: string[]`, `mode: direct|faithful_digest`, `warnings: string[]` (required even when empty), and `translator`. The translator requires nonblank `host`, `execution_mode: main_agent|subagent|mixed`, nonblank `model`, the structurally required key `agent_role: string|null`, and nonblank `prompt_version`. `model` and non-null `agent_role` must be trimmed; use exact `unknown` when the host cannot report the model. `main_agent` may explicitly use `agent_role: null`; `subagent` and `mixed` require a meaningful nonblank role. Omitting `agent_role`, or using null/blank for delegated execution, is a Task 6 validation failure handled by the same one-correction rule, not a later workflow gate.

Echo all binding fields exactly. Return every requested `item_id` exactly once and no others; ordering is irrelevant. Match each requested mode. `preserved_tokens` must exactly equal the request's `locked_tokens` sequence, and the translated text must contain the same case-sensitive, boundary-safe occurrences in the same order and multiplicity. Report exactly the requested source terms in `glossary_terms_used`, use every authoritative target, and include provenance per item.

## One correction and stopping rules

Run `prepare-review` after saving the initial response. If it emits `correction-request.json`, correct only its `failed_item_ids` and `required_fixes`, retain the same binding fields, overwrite the same `translation-response.json` with a complete response envelope containing every requested item and `attempt: 1`, then rerun `prepare-review`. This is the only correction. Another failure, an Agent interruption, missing context, exhausted credits, or sub-agent failure stops translation and preserves the checkpoint; never fabricate results or bypass review.

Keep one model and prompt version where practical. Cache reuse requires matching request content, glossary, prompt version, host, reported model, and execution mode. A model recorded as `unknown` is cacheable only within the current job. Never record host or model credentials.
