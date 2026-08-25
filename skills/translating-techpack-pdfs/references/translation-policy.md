# Translation policy

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
