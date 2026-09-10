const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../skills/translating-techpack-pdfs/assets/review-ui.js");

const reviewElementIds = [
  "review-data", "page-type-filter", "risk-filter", "glossary-filter", "status-filter",
  "issue-filter", "item-list", "page-thumbnail", "translation-layer", "source-highlight",
  "target-highlight", "source-text", "suggested-translation", "reviewed-translation",
  "locked-tokens", "glossary-hits", "decision-reason", "coordinates", "layout-risk",
  "translator-provenance", "model-risk", "position-feedback", "stats", "item-title",
  "approve", "approve-edited", "skip", "export-review",
  "source-selection-layer", "reframe-source", "reset-source", "translation-font-size",
  "font-smaller", "font-larger", "reset-font", "adjustment-hint",
];

function domElement(tagName = "div") {
  const listeners = new Map();
  const attributes = new Map();
  return {
    tagName,
    children: [],
    style: {},
    dataset: {},
    textContent: "",
    value: "",
    src: "",
    complete: tagName === "img",
    parentNode: null,
    append(...children) {
      children.forEach((child) => this.appendChild(child));
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    replaceChildren(...children) {
      this.children.forEach((child) => { child.parentNode = null; });
      this.children = [];
      this.append(...children);
    },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
      this.parentNode = null;
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    emit(type, event = {}) {
      (listeners.get(type) || []).forEach((listener) => listener({target: this, ...event}));
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      if (name === "src") return this.src || null;
      return attributes.get(name) || null;
    },
    getBoundingClientRect() {
      return {left: 0, top: 0, width: 200, height: 300};
    },
    focus() {
      this.focused = true;
    },
    click() { this.clicked = true; },
  };
}

function reviewFixture() {
  const items = [
    reviewItem("A", "Suggested A", "high"),
    reviewItem("B", "Suggested B", "low"),
  ];
  const data = {
    schema_version: "1.1", job_id: "test-job",
    source: {filename: "test.pdf", sha256: "a".repeat(64), page_count: 1},
    glossary: {filename: "terms.xlsx", sha256: "b".repeat(64)},
    pipeline: {parser: "mineru", translation_executor: "host_agent", host: "codex", execution_mode: "main_agent", model: "gpt-test", prompt_version: "1.0"},
    items,
    pages: [{page_index: 0, width: 200, height: 300, thumbnail: "data:image/png;base64,AA=="}],
    business_explanations: Object.fromEntries(items.map((item) => [item.item_id, {
      decision_reason: "测试审核行为。",
      coordinates: ["原文和译文位置已在预览中标出。"],
      layout_risk: {level: "low", label: "低风险", summary: "请检查。", warnings: []},
    }])),
    review_navigation: {default_open_page_index: 0, page_type_labels: {bom: "物料表"}},
    blocking_issues: [],
  };
  const elements = Object.fromEntries(reviewElementIds.map((id) => [
    id,
    domElement(id === "page-thumbnail" ? "img" : "div"),
  ]));
  elements["review-data"].textContent = JSON.stringify(data);
  const document = {
    ...domElement(),
    defaultView: {addEventListener() {}},
    getElementById(id) { return elements[id] || null; },
    createElement(tagName) { return domElement(tagName); },
  };
  return {data, document, elements};
}

function reviewItem(itemId, suggestedTranslation, riskLevel) {
  return {
    item_id: itemId,
    page_index: 0,
    page_type: "bom",
    source_text: `Source ${itemId}`,
    source_bbox: [10, 10, 50, 20],
    coordinate_confidence: "high",
    locked_tokens: [],
    glossary_hits: [],
    suggested_translation: suggestedTranslation,
    reviewed_translation: null,
    review_status: null,
    risk_level: riskLevel,
    translation_host: "codex",
    translation_execution_mode: "main_agent",
    translation_model: "gpt-test",
    translation_agent_role: null,
    translation_prompt_version: "1.0",
    target_rect: itemId === "A" ? [60, 10, 120, 30] : [60, 40, 120, 60],
    reviewed_target_rect: null,
    font_size: 6,
    warnings: [],
  };
}

test("dragging clamps the unchanged rectangle inside the page", () => {
  assert.deepEqual(
    ui.clampMovedRect([100, 20, 180, 40], 250, -50, {width: 300, height: 200}),
    [220, 0, 300, 20]
  );
});

test("next review item is unreviewed and risk first", () => {
  const items = [
    {item_id: "low", page_index: 0, risk_level: "low", review_status: null},
    {item_id: "done", page_index: 0, risk_level: "high", review_status: "approved"},
    {item_id: "high", page_index: 2, risk_level: "high", review_status: null},
  ];
  assert.deepEqual(ui.orderedUnreviewedIds(items, () => true), ["high", "low"]);
});

test("touching edges is not a translation-box overlap", () => {
  assert.equal(ui.rectanglesOverlap([0, 0, 10, 10], [10, 0, 20, 10]), false);
  assert.equal(ui.rectanglesOverlap([0, 0, 10, 10], [9, 0, 20, 10]), true);
});

test("an empty translation draft survives redraw and reselection", () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);
  const editor = elements["reviewed-translation"];

  editor.value = "";
  editor.emit("input");
  review.selectItem("B");
  review.selectItem("A");
  const overlay = elements["translation-layer"].children.find(
    (candidate) => candidate.dataset.itemId === "A"
  );

  assert.equal(review.data.items[0].reviewed_translation, "");
  assert.equal(editor.value, "");
  assert.equal(overlay.textContent, "");
});

test("status actions advance to the next item and finally focus export", () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);

  elements.approve.emit("click");
  assert.equal(review.data.items[0].review_status, "approved");
  assert.match(elements["item-title"].textContent, /^B /);

  elements.approve.emit("click");
  assert.equal(review.data.items[1].review_status, "approved");
  assert.equal(elements["export-review"].focused, true);
});

test("font choice redraws, clears approval and survives selection", () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);
  review.data.items[0].review_status = "approved";
  elements["translation-font-size"].value = "12";
  elements["translation-font-size"].emit("change");
  assert.equal(review.data.items[0].reviewed_font_size, 12);
  assert.equal(review.data.items[0].review_status, null);
  assert.equal(elements["translation-layer"].children[0].style.fontSize, "12px");
  review.selectItem("B"); review.selectItem("A");
  assert.equal(Number(elements["translation-font-size"].value), 12);
  elements["translation-font-size"].value = "100";
  elements["translation-font-size"].emit("change");
  assert.equal(review.data.items[0].reviewed_font_size, 12);
});

test("source can be redrawn in reverse direction and dragged, without changing original", () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);
  const item = review.data.items[0];
  item.review_status = "approved";
  const event = {pointerId: 1, clientX: 90, clientY: 80, preventDefault() {}};
  elements["reframe-source"].emit("click");
  elements["source-selection-layer"].emit("pointerdown", event);
  document.emit("pointermove", {...event, clientX: 30, clientY: 20});
  document.emit("pointerup", event);
  assert.deepEqual(item.reviewed_source_bbox, [30, 20, 90, 80]);
  assert.deepEqual(item.source_bbox, [10, 10, 50, 20]);
  assert.equal(item.review_status, null);
  elements["source-highlight"].emit("pointerdown", event);
  document.emit("pointermove", {...event, clientX: 100, clientY: 90});
  document.emit("pointerup", event);
  assert.deepEqual(item.reviewed_source_bbox, [40, 30, 100, 90]);
});

test("export contains reviewed source coordinates and exact font choice", async () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);
  review.data.items[0].reviewed_source_bbox = [1, 2, 30, 40];
  review.data.items[0].reviewed_font_size = 11.5;
  review.data.items.forEach((item) => { item.review_status = "approved"; });
  let blob;
  const create = URL.createObjectURL, revoke = URL.revokeObjectURL;
  URL.createObjectURL = (value) => { blob = value; return "blob:test"; };
  URL.revokeObjectURL = () => {};
  try {
    elements["export-review"].emit("click");
    const payload = JSON.parse(await blob.text());
    assert.deepEqual(payload.items[0].reviewed_source_bbox, [1, 2, 30, 40]);
    assert.equal(payload.items[0].reviewed_font_size, 11.5);
    assert.deepEqual(payload.items[0].source_bbox, [10, 10, 50, 20]);
    review.selectItem("A");
    elements["reviewed-translation"].value = "";
    elements["reviewed-translation"].emit("input");
    elements.skip.emit("click");
    elements["export-review"].emit("click");
    const skipped = JSON.parse(await blob.text());
    assert.equal(skipped.items[0].review_status, "skipped");
    assert.equal(skipped.items[0].reviewed_translation, null);
    assert.equal(review.data.items[0].reviewed_translation, "");
  } finally { URL.createObjectURL = create; URL.revokeObjectURL = revoke; }
});

test("reapproval after formatting preserves the user's edited Chinese", () => {
  const {document, elements} = reviewFixture();
  const review = ui.initReviewPage(document);
  elements["reviewed-translation"].value = "用户修改的中文";
  elements["reviewed-translation"].emit("input");
  elements["approve-edited"].emit("click");
  review.selectItem("A");
  elements["font-larger"].emit("click");
  elements.approve.emit("click");
  assert.equal(review.data.items[0].review_status, "approved_edited");
  assert.equal(review.data.items[0].reviewed_translation, "用户修改的中文");
  review.selectItem("A");
  elements["reset-source"].emit("click");
  elements.approve.emit("click");
  assert.equal(review.data.items[0].reviewed_translation, "用户修改的中文");
});
