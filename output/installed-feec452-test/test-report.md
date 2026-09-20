# 安装版 PDF 测试记录

- GitHub 提交：feec452fa50c0c718f17ee0c05e98ca8a5aabfcd
- 安装目录：C:/Users/GALAXYTY/.codex/skills/translating-techpack-pdfs
- MinerU：本机 Docker mineru-api，http://127.0.0.1:8000；OpenAPI 检查正常。
- Python：C:/Users/GALAXYTY/miniconda3/envs/techpack-pdf/python.exe，3.11.15；8 项依赖可导入。
- 输入 PDF：1802288_SP26_TechPack_EN-US_12-30-2025 05 03 19 AM_V008856.PDF，实际 1 页，页脚标注 1 of 4。
- 术语来源：SS27+Measurement+中英文对照.xlsx；读取两列内容生成 glossary.csv，107 条通过校验，未修改原文件或译法。
- Job：e898c5d57555-20260917T083119Z。

## 结果

解析、实图分类、全页图文覆盖检查、19 条翻译校验及审核页生成完成。16 条尺寸描述加 3 条视觉补漏（POINT TO POINT、修订备注、IT BLOCK 引用）。状态为 review_ready / human_review。

真实 Edge 浏览器检查：19 个审核条目，页面 JavaScript 错误 0，源图成功加载；19 条未审核，批准 0、修改 0、跳过 0、阻断 0；导出按钮禁用。未代替用户批准，未生成最终批注 PDF。

## 本次发现

1. 单页 19 条译文的预览排版耗时约 12 分钟。页面有 2199 个保护对象（1314 个文字、885 个绘图对象）；代码逐候选位置进行碰撞检查。该信息是性能调查线索，不代表已完成根因分析。
2. p001-i052（腰高）、p001-i118（下摆/脚口切线高）、p001-i151（内长）被标记 manual_placement_required。腰高被放到页头附近；长术语显示空间不足，应在审核页调整位置和字号后批准。
3. BOTTOM HEM HEIGHT 保留术语表完整译法“下摆切线高（上装）/脚口切线高（下装）”。括号间距差异导致 BOTTOM OPENING [SHORTS] 和 INSEAM [SHORTS] 未自动命中术语；本次译文仍采用用户术语表中的“脚口松量”“内长”。
4. 模型标识按技能的未知值规则记录 unknown，界面因此对所有条目显示模型信息风险；这不等于 19 条均存在排版碰撞。

## 后续

在本任务 review.html 中逐项审核，调整中文、位置和字号，完成后点击“完成审核并导出”。保留页面直至导出（修改不会自动保存）。收到用户导出的 JSON 后，核验任务绑定并运行 apply 和规定的六项验收，方可交付最终批注 PDF。
