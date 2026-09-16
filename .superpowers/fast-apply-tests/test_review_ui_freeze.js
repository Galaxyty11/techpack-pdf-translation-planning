"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const ui = require(path.resolve(
  __dirname,
  "..",
  "..",
  ".worktrees",
  "figure-annotation-coverage",
  "assets",
  "review-ui.js"
));

const data = {
  schema_version: "1.1",
  job_id: "job-1",
  source: {filename: "input.pdf", sha256: "a".repeat(64), page_count: 1},
  glossary: {filename: "terms.xlsx", sha256: "b".repeat(64)},
  pipeline: {
    parser: "mineru",
    translation_executor: "host_agent",
    host: "codex",
    execution_mode: "main_agent",
    model: "unknown",
    prompt_version: "1.1"
  },
  blocking_issues: [],
  items: [
    {
      item_id: "p001-i001",
      page_index: 0,
      review_status: "approved",
      source_bbox: [1, 2, 31, 12],
      reviewed_source_bbox: null,
      target_rect: [40, 20, 100, 40],
      reviewed_target_rect: null,
      font_size: 7,
      reviewed_font_size: null,
      suggested_translation: "批准译文",
      reviewed_translation: null
    },
    {
      item_id: "p001-i002",
      page_index: 0,
      review_status: "approved_edited",
      source_bbox: [2, 50, 32, 60],
      reviewed_source_bbox: [3, 51, 33, 61],
      target_rect: [40, 50, 100, 70],
      reviewed_target_rect: [45, 55, 105, 75],
      font_size: 7,
      reviewed_font_size: 6.5,
      suggested_translation: "建议译文",
      reviewed_translation: "人工译文"
    },
    {
      item_id: "p001-i003",
      page_index: 0,
      review_status: "skipped",
      source_bbox: [2, 80, 32, 90],
      reviewed_source_bbox: null,
      target_rect: [40, 80, 100, 100],
      reviewed_target_rect: null,
      font_size: 6,
      reviewed_font_size: null,
      suggested_translation: "跳过译文",
      reviewed_translation: null
    }
  ]
};

const payload = ui.buildReviewPayload(data, "2026-09-16T01:02:03.000Z");
const approved = payload.items[0];
assert.deepEqual(approved.reviewed_source_bbox, [1, 2, 31, 12]);
assert.deepEqual(approved.reviewed_target_rect, [40, 20, 100, 40]);
assert.equal(approved.reviewed_font_size, 7);

const edited = payload.items[1];
assert.deepEqual(edited.reviewed_source_bbox, [3, 51, 33, 61]);
assert.deepEqual(edited.reviewed_target_rect, [45, 55, 105, 75]);
assert.equal(edited.reviewed_font_size, 6.5);

const skipped = payload.items[2];
assert.equal(skipped.reviewed_source_bbox, null);
assert.equal(skipped.reviewed_target_rect, null);
assert.equal(skipped.reviewed_font_size, null);
assert.equal(payload.review_completed_at, "2026-09-16T01:02:03.000Z");

console.log("review payload freezes effective approved layout");
