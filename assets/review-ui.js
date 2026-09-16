(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.TechpackReviewUI = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const riskOrder = {high: 0, medium: 1, low: 2};
  const allowedStatuses = new Set(["approved", "approved_edited", "skipped"]);
  const itemFields = [
    "item_id", "page_index", "page_type", "source_text", "normalized_text",
    "source_bbox", "source_kind", "coordinate_confidence", "decision_reason",
    "locked_tokens", "glossary_hits", "suggested_translation", "reviewed_translation",
    "review_status", "risk_level", "translation_host", "translation_execution_mode",
    "translation_model", "translation_agent_role", "translation_prompt_version",
    "placement_strategy", "target_rect", "reviewed_target_rect", "font_size",
    "leader_line", "warnings", "reviewed_source_bbox", "reviewed_font_size"
  ];

  function clampMovedRect(rect, deltaX, deltaY, page) {
    const width = rect[2] - rect[0];
    const height = rect[3] - rect[1];
    const x0 = Math.min(Math.max(rect[0] + deltaX, 0), Math.max(page.width - width, 0));
    const y0 = Math.min(Math.max(rect[1] + deltaY, 0), Math.max(page.height - height, 0));
    return [x0, y0, x0 + width, y0 + height];
  }

  function rectanglesOverlap(left, right) {
    return left[0] < right[2] && left[2] > right[0]
      && left[1] < right[3] && left[3] > right[1];
  }

  function orderedUnreviewedIds(items, visible) {
    return items
      .map((item, originalIndex) => ({item, originalIndex}))
      .filter(({item}) => !item.review_status && visible(item))
      .sort((left, right) => {
        const riskDifference = (riskOrder[left.item.risk_level] ?? 3)
          - (riskOrder[right.item.risk_level] ?? 3);
        if (riskDifference) return riskDifference;
        const pageDifference = left.item.page_index - right.item.page_index;
        return pageDifference || left.originalIndex - right.originalIndex;
      })
      .map(({item}) => item.item_id);
  }

  function pageRectStyle(rect, page) {
    return {
      left: `${rect[0] / page.width * 100}%`,
      top: `${rect[1] / page.height * 100}%`,
      width: `${(rect[2] - rect[0]) / page.width * 100}%`,
      height: `${(rect[3] - rect[1]) / page.height * 100}%`,
    };
  }

  function initReviewPage(documentRef) {
    const doc = documentRef || (typeof document === "object" ? document : null);
    if (!doc) return null;
    const dataElement = doc.getElementById("review-data");
    if (!dataElement) return null;
    const data = JSON.parse(dataElement.textContent);
    const elementIds = [
      "page-type-filter", "risk-filter", "glossary-filter", "status-filter", "issue-filter",
      "item-list", "page-thumbnail", "translation-layer", "source-highlight",
      "target-highlight", "source-text", "suggested-translation", "reviewed-translation",
      "locked-tokens", "glossary-hits", "decision-reason", "coordinates", "layout-risk",
      "translator-provenance", "model-risk", "position-feedback", "stats", "item-title",
      "approve", "approve-edited", "skip", "export-review",
      "source-selection-layer", "reframe-source", "reset-source", "translation-font-size",
      "font-smaller", "font-larger", "reset-font", "adjustment-hint"
    ];
    const elements = Object.fromEntries(
      elementIds.map((id) => [id, doc.getElementById(id)])
    );
    const originalOrder = new Map(data.items.map((item, index) => [item.item_id, index]));
    const riskLabels = {high: "高风险", medium: "需留意", low: "低风险"};
    const statusLabels = {
      unreviewed: "待审核",
      approved: "已批准",
      approved_edited: "修改后批准",
      skipped: "已跳过"
    };
    const pageTypeLabels = data.review_navigation.page_type_labels || {};
    const openPages = new Set();
    let selectedId = null;
    let dragState = null;

    if (data.review_navigation.default_open_page_index !== null) {
      openPages.add(data.review_navigation.default_open_page_index);
    }

    function selectedItem() {
      return data.items.find((item) => item.item_id === selectedId) || null;
    }

    function pageFor(item) {
      return item
        ? data.pages.find((page) => page.page_index === item.page_index) || null
        : null;
    }

    function currentRect(item) {
      return item.reviewed_target_rect || item.target_rect;
    }

    function setText(id, value) {
      elements[id].textContent = value === null || value === undefined || value === ""
        ? "—"
        : String(value);
    }

    function renderLines(id, lines) {
      elements[id].replaceChildren();
      (Array.isArray(lines) ? lines : []).forEach((value) => {
        const paragraph = doc.createElement("p");
        paragraph.textContent = value;
        elements[id].appendChild(paragraph);
      });
    }

    function renderLayoutRisk(risk) {
      const box = doc.createElement("div");
      box.className = "risk-box";
      box.dataset.risk = risk && risk.level ? risk.level : "unknown";
      const label = doc.createElement("span");
      label.className = "risk-label";
      label.textContent = risk && risk.label ? risk.label : "请人工检查";
      const summary = doc.createElement("span");
      summary.textContent = risk && risk.summary ? risk.summary : "请人工确认排版是否合适。";
      box.append(label, summary);
      if (risk && Array.isArray(risk.warnings) && risk.warnings.length) {
        const list = doc.createElement("ul");
        list.className = "risk-warnings";
        risk.warnings.forEach((warning) => {
          const listItem = doc.createElement("li");
          listItem.textContent = warning;
          list.appendChild(listItem);
        });
        box.appendChild(list);
      }
      elements["layout-risk"].replaceChildren(box);
    }

    function addOption(select, value, label) {
      const option = doc.createElement("option");
      option.value = value;
      option.textContent = label || value;
      select.appendChild(option);
    }

    function itemIssues(item) {
      const issues = Array.isArray(item.warnings) ? [...item.warnings] : [];
      if (item.coordinate_confidence !== "high") issues.push("coordinate_confidence");
      if (item.translation_model === "unknown") issues.push("unknown_model");
      return issues;
    }

    function visible(item) {
      const status = item.review_status || "unreviewed";
      const glossary = Array.isArray(item.glossary_hits) && item.glossary_hits.length
        ? "hit"
        : "none";
      const issues = itemIssues(item);
      const issue = issues.length ? "any" : "none";
      const issueFilter = elements["issue-filter"].value;
      return (!elements["page-type-filter"].value
          || item.page_type === elements["page-type-filter"].value)
        && (!elements["risk-filter"].value
          || item.risk_level === elements["risk-filter"].value)
        && (!elements["glossary-filter"].value
          || glossary === elements["glossary-filter"].value)
        && (!elements["status-filter"].value
          || status === elements["status-filter"].value)
        && (!issueFilter || issue === issueFilter || issues.includes(issueFilter));
    }

    function itemRiskRank(item) {
      return Object.prototype.hasOwnProperty.call(riskOrder, item.risk_level)
        ? riskOrder[item.risk_level]
        : 3;
    }

    function prioritizedGroups() {
      const grouped = new Map();
      data.items.filter(visible).forEach((item) => {
        if (!grouped.has(item.page_index)) grouped.set(item.page_index, []);
        grouped.get(item.page_index).push(item);
      });
      const groups = [...grouped.entries()].map(([pageIndex, items]) => {
        items.sort((left, right) => {
          const reviewDifference = Number(Boolean(left.review_status))
            - Number(Boolean(right.review_status));
          if (reviewDifference) return reviewDifference;
          const riskDifference = itemRiskRank(left) - itemRiskRank(right);
          if (riskDifference) return riskDifference;
          return originalOrder.get(left.item_id) - originalOrder.get(right.item_id);
        });
        const riskCounts = {high: 0, medium: 0, low: 0};
        items.forEach((item) => {
          if (Object.prototype.hasOwnProperty.call(riskCounts, item.risk_level)) {
            riskCounts[item.risk_level] += 1;
          }
        });
        const highestRisk = [...items]
          .sort((left, right) => itemRiskRank(left) - itemRiskRank(right))[0].risk_level;
        return {
          pageIndex,
          items,
          riskCounts,
          highestRisk,
          unreviewedCount: items.filter((item) => !item.review_status).length,
          hasUnreviewedHighestRisk: items.some(
            (item) => !item.review_status && item.risk_level === highestRisk
          )
        };
      });
      groups.sort((left, right) => {
        const riskDifference = (riskOrder[left.highestRisk] ?? 3)
          - (riskOrder[right.highestRisk] ?? 3);
        if (riskDifference) return riskDifference;
        const reviewDifference = Number(!left.hasUnreviewedHighestRisk)
          - Number(!right.hasUnreviewedHighestRisk);
        return reviewDifference || left.pageIndex - right.pageIndex;
      });
      return groups;
    }

    function riskBadge(risk, count, className) {
      const badge = doc.createElement("span");
      badge.className = `${className} risk-${risk}`;
      badge.textContent = `${riskLabels[risk] || "请检查"}${
        count === null ? "" : ` ${count}`
      }`;
      return badge;
    }

    function renderList() {
      elements["item-list"].replaceChildren();
      const groups = prioritizedGroups();
      if (!groups.length) {
        const empty = doc.createElement("p");
        empty.className = "empty-list";
        empty.textContent = "当前筛选条件下没有审核项目";
        elements["item-list"].appendChild(empty);
        return;
      }
      if (!groups.some((group) => openPages.has(group.pageIndex))) {
        openPages.add(groups[0].pageIndex);
      }
      groups.forEach((group) => {
        const details = doc.createElement("details");
        details.className = "page-group";
        details.open = openPages.has(group.pageIndex)
          || group.items.some((item) => item.item_id === selectedId);
        const summary = doc.createElement("summary");
        summary.className = "page-group-summary";
        const summaryLine = doc.createElement("div");
        summaryLine.className = "page-summary-line";
        const title = doc.createElement("strong");
        title.className = "page-title";
        title.textContent = `第 ${group.pageIndex + 1} 页`;
        summaryLine.append(
          title,
          riskBadge(group.highestRisk, group.riskCounts[group.highestRisk], "page-risk-badge")
        );
        const meta = doc.createElement("div");
        meta.className = "page-meta";
        const progress = group.unreviewedCount
          ? `待审核 ${group.unreviewedCount} 项`
          : "本页已审核";
        const pageType = group.items[0].page_type;
        meta.textContent = `${progress} · ${pageTypeLabels[pageType] || pageType}`;
        summary.append(summaryLine, meta);
        const itemList = doc.createElement("div");
        itemList.className = "page-items";
        group.items.forEach((item) => {
          const button = doc.createElement("button");
          button.type = "button";
          button.className = `review-item${item.item_id === selectedId ? " active" : ""}`;
          button.dataset.risk = item.risk_level;
          button.title = item.item_id;
          const heading = doc.createElement("span");
          heading.className = "review-item-heading";
          heading.appendChild(riskBadge(item.risk_level, null, "item-badge"));
          const status = doc.createElement("span");
          status.className = "item-badge review-item-status";
          status.textContent = statusLabels[item.review_status || "unreviewed"] || "待审核";
          heading.appendChild(status);
          const source = doc.createElement("span");
          source.className = "review-item-source";
          source.textContent = item.source_text || "（无原文）";
          button.append(heading, source);
          button.addEventListener("click", () => selectItem(item.item_id));
          itemList.appendChild(button);
        });
        details.append(summary, itemList);
        details.addEventListener("toggle", () => {
          if (details.open) openPages.add(group.pageIndex);
          else openPages.delete(group.pageIndex);
        });
        elements["item-list"].appendChild(details);
      });
    }

    function applyRectStyle(element, rect, page) {
      Object.assign(element.style, pageRectStyle(rect, page));
    }

    function placeHighlight(id, rect, page) {
      const box = elements[id];
      if (!Array.isArray(rect) || rect.length !== 4 || !page) {
        box.style.display = "none";
        return;
      }
      applyRectStyle(box, rect, page);
      box.style.display = "block";
    }

    function overlayFor(itemId) {
      return [...elements["translation-layer"].children]
        .find((child) => child.dataset.itemId === itemId) || null;
    }

    function renderTranslationLayer() {
      const selected = selectedItem();
      const page = pageFor(selected);
      elements["translation-layer"].replaceChildren();
      if (!selected || !page) return;
      const canvasWidth = elements["page-thumbnail"].getBoundingClientRect().width;
      const fontScale = canvasWidth && page.width ? canvasWidth / page.width : 1;
      data.items
        .filter((item) => item.page_index === page.page_index)
        .filter((item) => item.review_status !== "skipped")
        .forEach((item) => {
          const rect = currentRect(item);
          if (!Array.isArray(rect) || rect.length !== 4) return;
          const overlay = doc.createElement("button");
          overlay.type = "button";
          overlay.className = `translation-box${item.item_id === selectedId ? " active" : ""}`;
          overlay.dataset.itemId = item.item_id;
          overlay.setAttribute("aria-label", `选择译文 ${item.item_id}`);
          overlay.textContent = item.reviewed_translation ?? item.suggested_translation ?? "";
          applyRectStyle(overlay, rect, page);
          overlay.style.fontSize = `${(item.reviewed_font_size ?? item.font_size ?? 5) * fontScale}px`;
          overlay.style.lineHeight = "1.35";
          overlay.style.padding = `${2 * fontScale}px`;
          overlay.addEventListener("click", () => selectItem(item.item_id));
          overlay.addEventListener("pointerdown", (event) => beginDrag(event, item));
          elements["translation-layer"].appendChild(overlay);
        });
    }

    function hideOverlay(itemId) {
      const overlay = overlayFor(itemId);
      if (overlay) overlay.remove();
    }

    function renderPositionFeedback() {
      const item = selectedItem();
      const rect = item && currentRect(item);
      const hasOverlap = Boolean(item && Array.isArray(rect) && data.items.some((candidate) => (
        candidate.item_id !== item.item_id
        && candidate.page_index === item.page_index
        && candidate.review_status !== "skipped"
        && Array.isArray(currentRect(candidate))
        && rectanglesOverlap(rect, currentRect(candidate))
      )));
      const overlay = item && overlayFor(item.item_id);
      const clipped = overlay && (overlay.scrollHeight > overlay.clientHeight + 1 || overlay.scrollWidth > overlay.clientWidth + 1);
      elements["position-feedback"].textContent = [
        hasOverlap ? "此位置与另一条译文重叠，请检查文字是否互相遮挡。" : "",
        clipped ? "当前字号下文字可能显示不全，请调小字号或缩短译文。" : ""
      ].filter(Boolean).join(" ");
      elements["position-feedback"].hidden = !hasOverlap && !clipped;
    }

    function renderPage() {
      const item = selectedItem();
      const page = pageFor(item);
      if (!item || !page) return;
      renderTranslationLayer();
      placeHighlight("source-highlight", item.reviewed_source_bbox || item.source_bbox, page);
      placeHighlight("target-highlight", currentRect(item), page);
      renderPositionFeedback();
    }

    function renderDetails(item) {
      elements["translation-font-size"].value = item.reviewed_font_size ?? item.font_size ?? 7;
      setText("item-title", `${item.item_id} · 第 ${item.page_index + 1} 页`);
      setText("source-text", item.source_text);
      setText("suggested-translation", item.suggested_translation);
      elements["reviewed-translation"].value = item.reviewed_translation
        ?? item.suggested_translation
        ?? "";
      setText("locked-tokens", (item.locked_tokens || []).join("、"));
      const glossaryLines = (item.glossary_hits || []).map((hit) => (
        hit.do_not_translate
          ? `${hit.matched_text || hit.source_term}（保持原文）`
          : `${hit.matched_text || hit.source_term} → ${hit.target_term}`
      ));
      renderLines("glossary-hits", glossaryLines.length ? glossaryLines : ["无"]);
      const explanation = data.business_explanations[item.item_id] || {};
      setText("decision-reason", explanation.decision_reason || "系统已选中这段文字，请人工确认。");
      renderLines("coordinates", explanation.coordinates || ["请人工检查原文和译文位置。"]);
      renderLayoutRisk(explanation.layout_risk);
      setText("translator-provenance", [
        item.translation_host,
        item.translation_execution_mode,
        item.translation_model,
        item.translation_agent_role,
        item.translation_prompt_version
      ].filter((value) => value !== null && value !== undefined).join(" · "));
      const risks = [];
      if (item.translation_model === "unknown") risks.push("模型未知");
      [
        ["translation_host", "宿主切换"],
        ["translation_model", "模型切换"],
        ["translation_execution_mode", "执行方式切换"],
        ["translation_prompt_version", "提示词版本切换"]
      ].forEach(([field, label]) => {
        if (new Set(data.items.map((candidate) => candidate[field])).size > 1) {
          risks.push(label);
        }
      });
      setText("model-risk", risks.join("；") || "无");
    }

    function selectItem(itemId) {
      const item = data.items.find((candidate) => candidate.item_id === itemId);
      if (!item) return;
      cancelSourceSelection();
      dragState = null;
      selectedId = itemId;
      openPages.add(item.page_index);
      renderDetails(item);
      renderList();
      const page = pageFor(item);
      if (!page) return;
      elements["page-thumbnail"].onload = renderPage;
      if (elements["page-thumbnail"].getAttribute("src") !== page.thumbnail) {
        elements["page-thumbnail"].src = page.thumbnail;
      }
      if (elements["page-thumbnail"].complete) renderPage();
    }

    function resetReviewedStatus(item) {
      if (!item.review_status) return false;
      item.review_status = null;
      return true;
    }

    function beginDrag(event, item, kind = "target") {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      if (selectedId !== item.item_id) selectItem(item.item_id);
      const rect = kind === "source" ? (item.reviewed_source_bbox || item.source_bbox) : currentRect(item);
      const page = pageFor(item);
      if (!Array.isArray(rect) || !page) return;
      dragState = {
        kind,
        item,
        page,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startRect: [...rect]
      };
    }

    function moveDrag(event) {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      event.preventDefault();
      const canvas = elements["page-thumbnail"].getBoundingClientRect();
      if (!canvas.width || !canvas.height) return;
      const deltaX = (event.clientX - dragState.startX) * dragState.page.width / canvas.width;
      const deltaY = (event.clientY - dragState.startY) * dragState.page.height / canvas.height;
      if (dragState.kind === "source" || dragState.kind === "source-draw") {
        const rect = dragState.kind === "source" ? clampMovedRect(dragState.startRect, deltaX, deltaY, dragState.page) : drawnRect(dragState.startRect, deltaX, deltaY, dragState.page);
        dragState.pendingRect = rect;
        placeHighlight("source-highlight", rect, dragState.page);
        return;
      }
      dragState.item.reviewed_target_rect = clampMovedRect(
        dragState.startRect,
        deltaX,
        deltaY,
        dragState.page
      );
      const statusChanged = resetReviewedStatus(dragState.item);
      const overlay = overlayFor(dragState.item.item_id);
      if (overlay) applyRectStyle(overlay, dragState.item.reviewed_target_rect, dragState.page);
      placeHighlight("target-highlight", dragState.item.reviewed_target_rect, dragState.page);
      renderPositionFeedback();
      if (statusChanged) {
        renderList();
        renderStats();
      }
    }

    function endDrag(event) {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      if (dragState.kind === "source" || dragState.kind === "source-draw") {
        const rect = dragState.pendingRect;
        if (event.type !== "pointercancel" && rect && rect[2] - rect[0] >= 2 && rect[3] - rect[1] >= 2) {
          dragState.item.reviewed_source_bbox = rect;
          resetReviewedStatus(dragState.item);
          renderList();
          renderStats();
        }
        cancelSourceSelection();
      }
      dragState = null;
      renderPage();
    }

    function cancelSourceSelection() {
      elements["source-selection-layer"].hidden = true;
      elements["reframe-source"].textContent = "重新框选原文";
    }

    function drawnRect(start, dx, dy, page) {
      const x = Math.max(0, Math.min(page.width, start[0] + dx));
      const y = Math.max(0, Math.min(page.height, start[1] + dy));
      return [Math.min(start[0], x), Math.min(start[1], y), Math.max(start[0], x), Math.max(start[1], y)];
    }

    function updateFont(value) {
      const item = selectedItem();
      if (!item) return;
      if (value !== null && (!Number.isFinite(value) || value < 5 || value > 24)) {
        elements["adjustment-hint"].textContent = "请输入 5–24 pt 的字号。";
        elements["translation-font-size"].value = item.reviewed_font_size ?? item.font_size ?? 7;
        return;
      }
      item.reviewed_font_size = value;
      resetReviewedStatus(item);
      elements["translation-font-size"].value = value ?? item.font_size ?? 7;
      elements["adjustment-hint"].textContent = value === null ? "已恢复自动字号，请重新确认。" : "将按此字号导出，请检查是否能完整显示；放不下时可缩短译文或减小字号。";
      renderAll();
    }

    function setStatus(requestedStatus) {
      const item = selectedItem();
      if (!item || !allowedStatuses.has(requestedStatus)) return;
      const edited = elements["reviewed-translation"].value.trim();
      if (requestedStatus !== "skipped" && !edited) return;
      const finalStatus = requestedStatus === "approved" && edited !== (item.suggested_translation || "").trim()
        ? "approved_edited" : requestedStatus;
      if (finalStatus !== "skipped") {
        item.reviewed_translation = finalStatus === "approved_edited" ? edited : null;
      }
      item.review_status = finalStatus;
      if (requestedStatus === "skipped") hideOverlay(item.item_id);
      renderAll();
      const nextId = orderedUnreviewedIds(data.items, visible)[0]
        || orderedUnreviewedIds(data.items, () => true)[0];
      if (nextId) selectItem(nextId);
      else elements["export-review"].focus();
    }

    function renderStats() {
      const counts = {unreviewed: 0, approved: 0, approved_edited: 0, skipped: 0};
      data.items.forEach((item) => {
        counts[item.review_status || "unreviewed"] += 1;
      });
      setText(
        "stats",
        `未审核 ${counts.unreviewed} · 已批准 ${counts.approved} · `
          + `已修改 ${counts.approved_edited} · 已跳过 ${counts.skipped} · `
          + `阻断 ${data.blocking_issues.length}`
      );
      elements["export-review"].disabled = counts.unreviewed !== 0
        || data.blocking_issues.length !== 0;
    }

    function exportReview() {
      if (data.blocking_issues.length
          || data.items.some((item) => !allowedStatuses.has(item.review_status))) return;
      if (data.items.some((item) => (
        item.review_status === "approved_edited" && !(item.reviewed_translation || "").trim()
      ))) return;
      if (data.items.some((item) => (
        item.review_status === "approved" && !(item.suggested_translation || "").trim()
      ))) return;
      const payload = {
        schema_version: "1.1",
        job_id: data.job_id,
        source: {
          filename: data.source.filename,
          sha256: data.source.sha256,
          page_count: data.source.page_count
        },
        glossary: {filename: data.glossary.filename, sha256: data.glossary.sha256},
        pipeline: {
          parser: data.pipeline.parser,
          translation_executor: data.pipeline.translation_executor,
          host: data.pipeline.host,
          execution_mode: data.pipeline.execution_mode,
          model: data.pipeline.model,
          prompt_version: data.pipeline.prompt_version
        },
        items: data.items.map((item) => (
          Object.fromEntries(itemFields.map((field) => [field,
            field === "reviewed_translation" && item.review_status === "skipped"
              && !(item.reviewed_translation || "").trim() ? null : item[field]
          ]))
        )),
        blocking_issues: [],
        review_completed_at: new Date().toISOString()
      };
      const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
        type: "application/json"
      });
      const link = doc.createElement("a");
      link.href = globalThis.URL.createObjectURL(blob);
      link.download = "review.json";
      link.click();
      globalThis.URL.revokeObjectURL(link.href);
    }

    function renderAll() {
      renderList();
      renderStats();
      renderTranslationLayer();
      renderPositionFeedback();
    }

    [...new Set(data.items.map((item) => item.page_type))].sort().forEach((value) => {
      addOption(elements["page-type-filter"], value, pageTypeLabels[value] || value);
    });
    [...new Set(data.items.flatMap(itemIssues))].sort().forEach((value) => {
      addOption(elements["issue-filter"], value, value);
    });
    ["page-type-filter", "risk-filter", "glossary-filter", "status-filter", "issue-filter"]
      .forEach((id) => elements[id].addEventListener("change", renderList));
    elements["reviewed-translation"].addEventListener("input", () => {
      const item = selectedItem();
      if (!item) return;
      item.reviewed_translation = elements["reviewed-translation"].value;
      const statusChanged = resetReviewedStatus(item);
      const overlay = overlayFor(item.item_id);
      if (overlay) overlay.textContent = item.reviewed_translation;
      else renderTranslationLayer();
      if (statusChanged) {
        renderList();
        renderStats();
      }
      renderPositionFeedback();
    });
    elements.approve.addEventListener("click", () => setStatus("approved"));
    elements["approve-edited"].addEventListener("click", () => setStatus("approved_edited"));
    elements.skip.addEventListener("click", () => setStatus("skipped"));
    elements["export-review"].addEventListener("click", exportReview);
    elements["translation-font-size"].addEventListener("change", () => updateFont(Number(elements["translation-font-size"].value)));
    elements["font-smaller"].addEventListener("click", () => updateFont(Math.max(5, Number(elements["translation-font-size"].value) - .5)));
    elements["font-larger"].addEventListener("click", () => updateFont(Math.min(24, Number(elements["translation-font-size"].value) + .5)));
    elements["reset-font"].addEventListener("click", () => updateFont(null));
    elements["reframe-source"].addEventListener("click", () => {
      const layer = elements["source-selection-layer"];
      layer.hidden = !layer.hidden;
      elements["reframe-source"].textContent = layer.hidden ? "重新框选原文" : "取消框选";
      elements["adjustment-hint"].textContent = layer.hidden ? "红框可拖动；修改后请重新确认。" : "请在页面上按住并拖出新的原文框。按 Esc 可取消。";
    });
    elements["reset-source"].addEventListener("click", () => {
      const item = selectedItem();
      if (!item) return;
      item.reviewed_source_bbox = null;
      resetReviewedStatus(item);
      cancelSourceSelection();
      renderAll(); renderPage();
    });
    elements["source-highlight"].addEventListener("pointerdown", (event) => {
      const item = selectedItem();
      if (item) beginDrag(event, item, "source");
    });
    elements["source-selection-layer"].addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      const item = selectedItem(), page = pageFor(item);
      const canvas = elements["page-thumbnail"].getBoundingClientRect();
      if (!item || !page || !canvas.width || !canvas.height) return;
      event.preventDefault();
      const x = Math.max(0, Math.min(page.width, (event.clientX - canvas.left) * page.width / canvas.width));
      const y = Math.max(0, Math.min(page.height, (event.clientY - canvas.top) * page.height / canvas.height));
      dragState = {kind: "source-draw", item, page, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, startRect: [x,y,x,y]};
    });
    doc.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { dragState = null; cancelSourceSelection(); renderPage(); }
    });
    doc.addEventListener("pointermove", moveDrag);
    doc.addEventListener("pointerup", endDrag);
    doc.addEventListener("pointercancel", endDrag);
    if (doc.defaultView) doc.defaultView.addEventListener("resize", renderPage);

    renderAll();
    const initialId = orderedUnreviewedIds(data.items, () => true)[0]
      || (data.items[0] && data.items[0].item_id);
    if (initialId) selectItem(initialId);
    return {data, selectItem, setStatus, renderAll};
  }

  return {
    clampMovedRect,
    rectanglesOverlap,
    orderedUnreviewedIds,
    pageRectStyle,
    initReviewPage,
  };
});

if (typeof document === "object") {
  globalThis.TechpackReviewUI.initReviewPage(document);
}
