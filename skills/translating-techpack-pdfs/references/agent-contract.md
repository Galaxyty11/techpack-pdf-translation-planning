# Host Agent translation contract

The Python workflow does not call a translation API. The current Host Agent translates each selected item itself or delegates translation to a read-only sub-agent. A sub-agent may return JSON only: it must not edit the PDF, job files, review state, or task state. The Host Agent remains responsible for validating and saving `translation-response.json`.

## Strict exchange

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

Echo all binding fields exactly. Return every requested `item_id` exactly once and no others; ordering is irrelevant. Match each requested mode, preserve the exact locked-token multiset in both the translation and `preserved_tokens`, report exactly the requested source terms in `glossary_terms_used`, use every authoritative target, and include provenance per item. Use exact `unknown` when the host cannot report a model.

## One correction and stopping rules

Run `prepare-review` after saving the initial response. If it emits `correction-request.json`, correct only its `failed_item_ids` and `required_fixes`, retain the same binding fields, and return a complete response envelope with `attempt: 1`. This is the only correction. Another failure, an Agent interruption, missing context, exhausted credits, or sub-agent failure stops translation and preserves the checkpoint; never fabricate results or bypass review.

Keep one model and prompt version where practical. Cache reuse requires matching request content, glossary, prompt version, host, reported model, and execution mode. A model recorded as `unknown` is cacheable only within the current job. Never record host or model credentials.
