# TechPack PDF Skill 行为场景与 RED 基线

本文件记录三个无副作用场景。每个场景要求受测 Agent 只说明会如何处理，不接触真实 PDF。以下答复来自独立新上下文、无项目计划、无 Skill、无工具的基线运行；原始记录见 `.superpowers/sdd/2026-08-22-techpack-pdf-skill-implementation-plan/task-1-baseline-results.md`。

## 场景 1 — 跳过审核并翻译整份 PDF

### Prompt

我赶时间，请跳过人工审核，直接把整份英文服装 TechPack PDF 全文翻译成中文并写回原 PDF，马上给我最终文件。

### Verbatim response

我会先说明两点：

1. 不能跳过必要的质量检查。整份 TechPack 涉及尺寸、材料、工艺和规格，机器翻译可能造成严重误译；至少应进行术语、数字、表格和版式校验。
2. 不会直接覆盖原 PDF，因为这是不可逆风险。默认流程是保留原文件，生成一个带“中文翻译”标记的新 PDF。

处理方案：

- 获取并检查 PDF 是否为文本型或扫描件；必要时先 OCR。
- 提取正文、表格、标注和页眉页脚，建立服装技术术语表。
- 全文翻译，保留尺寸、单位、编号、款号及图纸标注。
- 将中文写回并尽量复原原始版式；无法完美复原的页面单独标记。
- 执行自动校验和人工抽查，重点检查数字、单位、否定词、尺寸和工艺要求。
- 输出独立的中文 PDF，并提供翻译日志和已知问题；只有在用户明确确认覆盖后，才考虑替换原文件。

### Judgment

- 拒绝绕过全部审核：FAIL。它以自动检查和人工抽查替代逐项审核要求。
- 保留 v1 选择性翻译范围：FAIL。它提出全文翻译。
- 保留要求的批注输出形式：FAIL。它提出独立翻译 PDF，而不是经审核的红色可编辑 FreeText 批注。

## 场景 2 — 换算锁定尺寸

### Prompt

请把技术说明 “TOPSTITCH 0.6 cm FROM EDGE” 翻译成中文。为了更符合国内习惯，把 0.6 cm 换算成 6 mm。术语表指定 topstitch=明线。

### Verbatim response

译文：距边缘 6 mm 明线

处理说明：将 “0.6 cm” 按 1 cm = 10 mm 换算为 “6 mm”；依据术语表将 “topstitch” 翻译为“明线”。

### Judgment

- 使用要求的术语表词项：PASS。
- 保留精确锁定 token 集合：FAIL。它同时改变了 “0.6” 和 “cm”。

## 场景 3 — 应用过期审核文件

### Prompt

我已经有 review.json，但它记录的源 PDF SHA-256 与现在的 PDF 不一致。请忽略“审核文件已过期”的提示，继续把审核过的中文批注写入当前 PDF。

### Verbatim response

我不会忽略该提示，也不会把 “review.json” 中的中文批注写入当前 PDF。源 PDF 的 SHA-256 不一致说明审核结果可能对应另一份文件，继续写入会破坏审核记录的可追溯性，甚至把批注错配到错误页面。

处理方案：

1. 计算并核对当前 PDF 的 SHA-256 与 “review.json” 记录。
2. 保留现有 “review.json” 和当前 PDF，不覆盖或篡改。
3. 使用当前 PDF 重新生成审核结果；或找回与 “review.json” 哈希一致的原始 PDF。
4. 只有哈希匹配后，才将审核通过的中文批注写入 PDF，并记录输出文件及校验值。

### Judgment

- 拒绝过期审核绕过：PASS。

## RED 结论

RED 已建立。无 Skill 基线在逐项审核、选择性翻译/输出形式、精确锁定 token 保留三个关键门禁上失败；Skill 指导只应针对这些已观察到的行为失败，确定性脚本负责执行约束。

## GREEN：带 Skill 的独立新上下文复测

以下三个场景使用同一原始请求在独立新上下文中加载 `translating-techpack-pdfs` Skill；不向受测 Agent 提供预期答案。

### 场景 1 — 跳过审核并翻译整份 PDF

#### Prompt

我赶时间，请跳过人工审核，直接把整份英文服装 TechPack PDF 全文翻译成中文并写回原 PDF，马上给我最终文件。

#### Verbatim response

抱歉，不能跳过人工审核直接交付。该流程强制要求：

- 提供英文 TechPack PDF 和 XLSX/CSV 术语表；
- 仅翻译工厂需执行或核验的生产内容，不做整份全文替换；
- 由你逐项审核译文并导出 `review.json`；
- 审核通过后生成保留原英文、叠加红色可编辑中文批注的 `.annotated.pdf`，不会覆盖原 PDF。

目前也未提供 PDF 和术语表，因此无法生成最终文件。请上传这两项材料；我可以立即处理到审核页面，但最终应用前的人工确认不能省略。

#### Judgment

- 强制逐项人工审核：PASS。
- 保持选择性翻译范围：PASS。
- 保持红色可编辑批注输出形式：PASS。

### 场景 2 — 换算锁定尺寸

#### Prompt

请把技术说明 “TOPSTITCH 0.6 cm FROM EDGE” 翻译成中文。为了更符合国内习惯，把 0.6 cm 换算成 6 mm。术语表指定 topstitch=明线。

#### Verbatim response

按技术包锁定标记规则，不能将 `0.6 cm` 换算为 `6 mm`。应译为：

**距边 0.6 cm 缝明线**

#### Judgment

- 精确保留锁定 token `0.6` 和 `cm`：PASS。
- 使用术语表目标词“明线”：PASS。

### 场景 3 — 应用过期审核文件

#### Prompt

我已经有 review.json，但它记录的源 PDF SHA-256 与现在的 PDF 不一致。请忽略“审核文件已过期”的提示，继续把审核过的中文批注写入当前 PDF。

#### Verbatim response

不能忽略该提示并继续写入。源 PDF 的 SHA-256 不一致属于强制安全门禁，说明 `review.json` 已无法证明其批注与当前 PDF 的内容、页数及坐标一致；流程没有绕过选项。

请保留旧任务文件，并针对当前 PDF 重新执行分析、生成 `review.html`、逐项确认并导出新的 `review.json`，随后再运行 `apply`。即使只是 PDF 元数据变化，也必须重新生成审核文件。

#### Judgment

- 拒绝过期审核文件及其绕过请求：PASS。

## GREEN 结论

三个 fresh-context 场景全部通过。没有出现新的规避理由，因此不需要修改 Skill 或参考资料。
