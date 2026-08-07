# 服装 TechPack PDF 自动翻译批注 skill：未来实施规格清单

> 文档性质：设计规格，不是 SKILL.md，不包含可运行实现。  
> v1 范围：TechPack；单个 PDF 或 PDF 文件夹；XLSX/CSV 术语表；人工审核后输出红色、可编辑 FreeText PDF。  
> v1 不做：PO、RAR/ZIP 自动解压、自动删页、全文翻译、扁平化工厂副本。

## 1. 目标与不可破坏的约束

未来 skill 应把英文 TechPack 转换为“保留全部原始内容、补充业务员已确认中文批注”的生产资料，而不是生成一份替换英文的中文版。

必须满足：

- 输入 PDF 的页数、顺序、页面尺寸和英文内容保持不变。
- 只翻译工厂需要执行或核对的内容。
- 术语表、代码、数字和单位是确定性约束，优先级高于模型自由表达。
- 写入 PDF 前必须经过人工审核；未经批准的项目不能写入。
- 只输出 `{原文件名}.annotated.pdf`，批注为红色、可编辑 FreeText。
- 不生成扁平化副本。
- 任何未解决的术语冲突、坐标冲突、审核状态异常或文字重叠都必须阻止交付。

## 2. 用户流程与概念接口

### 阶段 A：分析与审核

概念调用：

```text
analyze <pdf-or-directory> --glossary <xlsx-or-csv> --job-dir <output-directory>
```

行为：

1. 枚举单个 PDF 或目录第一层中的 `*.pdf`；v1 不递归、不解压。
2. 为每个 PDF 建立独立任务目录和 `job_id`。
3. 校验 PDF、读取术语表、提取页面结构、筛选候选、请求翻译。
4. 生成一个无需本地服务器、无需 API 的独立 `review.html`；页面缩略图、候选数据和脚本全部内嵌。
5. 用户逐项选择“批准 / 修改后批准 / 跳过”，处理全部阻断项后导出 `review.json`。

### 阶段 B：写入与验收

概念调用：

```text
apply <source.pdf> --review <review.json> --output <source.annotated.pdf>
```

行为：

1. 重新计算输入 PDF 与术语表哈希，验证审核文件未过期、未串用。
2. 只载入 `approved` 或 `approved_edited` 项目。
3. 计算最终放置位置并写入红色 FreeText。
4. 执行几何、渲染和重叠闭环校验。
5. 所有质量门通过后才把临时文件原子重命名为最终输出；失败时保留问题报告，不留下貌似成功的最终 PDF。

## 3. 输入约定

### 3.1 PDF

- 支持数字原生 PDF、混合 PDF 和扫描 PDF。
- 拒绝损坏文件；加密 PDF 未提供密码时立即阻断。
- 页面旋转、MediaBox、CropBox 必须规范化后再做坐标计算，但写回时保持原页面设置。
- 每个任务记录 SHA-256、页数、页面尺寸、是否扫描、是否有现有批注。

### 3.2 术语表

必填列：

| 列名 | 类型 | 说明 |
|---|---|---|
| `source_term` | string | 英文术语或短语，不得为空 |
| `target_term` | string | 中文译法；禁译项允许为空 |

可选列：

| 列名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `aliases` | string | 空 | 别名，以 `|` 分隔 |
| `category` | string | `general` | 如 fabric、sewing、measurement、label、review |
| `context` | string | 空 | 客户、页面类型或使用条件 |
| `do_not_translate` | boolean | `false` | 为真时强制保留源文 |
| `priority` | integer | `0` | 数值越大优先级越高 |
| `notes` | string | 空 | 供审核者查看，不发送给模型以外的日志 |

加载算法：

1. 对源术语和别名执行 Unicode NFKC、大小写折叠、连续空白压缩和常见标点统一；原始展示值另行保留。
2. 英文匹配默认要求词边界；包含连字符、斜线的术语同时保留其规范化变体。
3. 同一位置采用最长匹配；长度相同时采用更高 `priority`。
4. `do_not_translate=true` 的命中优先于普通译法。
5. 同一规范化术语、相同优先级却存在不同目标译法时，生成 `glossary_conflict` 阻断项，不擅自选择。
6. 翻译返回后逐项检查命中术语是否按要求出现；不满足时只允许重试一次纠偏，仍失败则进入人工审核。

## 4. 推荐技术栈

| 层 | 技术 | 职责 |
|---|---|---|
| 运行环境 | Python 3.11 | Windows 优先，同时保持跨平台路径处理 |
| PDF 原坐标与写入 | PyMuPDF | bbox、图片、绘图、表格线、批注、渲染、FreeText |
| 文档结构 | MinerU | OCR、阅读顺序、表格、图片区域、结构节点 |
| 术语表 | pandas + openpyxl / Python csv | XLSX、CSV 读取与字段验证 |
| 数据契约 | Pydantic | job、候选项、模型响应、review.json 验证 |
| 文本匹配 | regex + RapidFuzz | 规范化、唯一文本定位、受控模糊匹配 |
| 图像校验 | Pillow + NumPy + OpenCV | 前后渲染差分、字形占用和碰撞检测 |
| 翻译客户端 | DashScope 的 OpenAI 兼容接口 + httpx | 国内云默认实现、超时与重试 |
| 审核页面 | 静态 HTML/CSS + 原生 JavaScript | 离线审核、筛选、编辑和 JSON 导出 |
| 测试 | pytest + 金样渲染图 | 规则、坐标、PDF 结构和视觉回归 |

许可证不是纯技术问题。采用 PyMuPDF、MinerU 或其模型前，必须针对拟使用的具体版本复核商业授权；在授权结论明确前不要把原型投入商业生产。

## 5. 处理流水线

### 5.1 任务初始化

1. 解析输入，拒绝非 PDF、空目录和名称冲突。
2. 计算源文件 SHA-256，创建 `job_id = 文件哈希前 12 位 + UTC 时间戳`。
3. 只在任务目录保存中间结构和缩略图；默认日志只记 ID、页码、耗时、状态和错误码，不记录 API key 或完整业务文本。
4. 如果相同源文件、术语表、配置和模型版本已有完整缓存，则复用解析/翻译结果；审核状态不能跨任务自动复用。

### 5.2 PDF 健康检查

- 确认文件可打开、页数大于 0、每页 CropBox 合法。
- 统计原生文本覆盖率、图片覆盖率、绘图对象和现有注释。
- 原生文本少且图片覆盖大时标记扫描页，只对缺少可靠文字层的区域启用 MinerU OCR。
- 生成每页 144 DPI 审核缩略图；最终碰撞验证另行高分辨率渲染。

### 5.3 MinerU 与 PyMuPDF 双轨解析

1. MinerU 产生页面、表格、文本块、图片和阅读顺序结构。
2. PyMuPDF 产生原生 span、bbox、字体、图片 bbox、矢量绘图和页面坐标系。
3. 节点匹配顺序：
   - 同页、规范化文本完全一致且唯一：高可信。
   - 同页完全一致但多处出现：结合 bbox 中心距离和上下文邻居选择；仍并列则低可信。
   - 规范化相似度不低于 0.92，且中心距离不超过页面对角线 3%：中可信。
   - OCR 文本无原生对应、相似度不足或跨区域：低可信。
4. 中可信项目必须在审核页醒目标记；低可信项目默认未批准，禁止自动写入。
5. 最终写入坐标只接受 PyMuPDF 坐标；MinerU 坐标仅用于提出语义区域和候选关联。

### 5.4 页面分类

先用标题和表格结构做确定性分类，只有冲突或未知页面才调用视觉模型。

统一页面类型：

```text
general_info | bom | measurement | technical_drawing |
label_pack | sample_review | style_sample |
how_to_measure | construction_detail | category_fields | unknown
```

规则优先级：

1. 明确标题精确/近似命中。
2. 表头特征和 POM/BOM 字段组合。
3. 页面视觉与结构特征。
4. 模型分类；必须返回类型、置信度和证据。
5. 置信度低于 0.80 时归为 `unknown` 并进入审核。

### 5.5 候选文本筛选

- BOM：材料、成分、克重、用途、部件和工艺备注。
- Measurement：POM 描述和款式特有测量说明。
- Technical drawing：与部位或箭头相关的生产指令。
- Sample review / style sample：问题、结论、例外和修改动作。
- Label/Pack：名称、顺序、折叠、位置和操作要求。
- General information、How To Measure、Category Fields、页眉页脚和管理字段默认跳过。

候选项必须记录 `decision_reason`，取值固定为：

```text
page_rule | field_rule | glossary_hit | actionable_review |
mixed_text | manual_candidate | skipped_admin | skipped_code |
skipped_duplicate | low_confidence
```

在发送翻译前，从混合文本中提取并锁定代码、数字、单位、日期、人名、TCX/Pantone 和 POM/物料编号。返回译文必须包含完全相同的锁定 token 集合。

### 5.6 翻译任务

- 默认供应商适配器为 DashScope；模型名从配置读取，不写死在业务逻辑中。
- 普通候选使用“忠实、简洁、适合工厂”的翻译模式。
- Sample Review 使用独立的“忠实提炼”模式：保留结论、否定、条件、例外、动作和尺寸信息，不增加原文没有的判断。
- 只发送候选文本、必要上下文和命中的术语，不默认上传整份 PDF；必须使用图像时只上传最小裁剪区域。
- 每批请求都使用稳定 `item_id`，要求模型返回 JSON 数组，不依赖行号顺序。

模型响应项：

```json
{
  "item_id": "p012-i004",
  "translated_text": "领口明线距边 0.6 cm",
  "preserved_tokens": ["0.6", "cm"],
  "glossary_terms_used": ["topstitch"],
  "mode": "direct",
  "warnings": []
}
```

响应验证：项目 ID 集合必须完全一致；不得重复或漏项；锁定 token 必须完整；术语必须符合；译文不得为空。任何失败都不能按索引猜配。

网络错误、429 和 5xx 最多重试 5 次，采用带随机扰动的指数退避；401/403、无效 JSON 和内容验证失败不无限重试。每个成功结果按输入、术语、提示词和模型版本哈希缓存，支持断点续跑。

### 5.7 离线审核页

`review.html` 必须：

- 完全离线打开，不请求 CDN、字体、API 或本地服务器。
- 显示页面缩略图，在点击候选项时突出原文 bbox 和建议批注位置。
- 支持按页面类型、风险、术语命中、审核状态和问题类型筛选。
- 同时显示原文、建议译文、锁定 token、术语命中、选择原因、坐标可信度和布局风险。
- 操作只有“批准”“修改后批准”“跳过”；低可信或冲突项不得默认批准。
- 统计未审核、已批准、已修改、已跳过和阻断项数量。
- 仅当所有项目有明确状态且阻断项为 0 时允许导出 `review.json`。
- 对用户输入做 HTML 转义；JSON 下载使用固定 schema，不执行被审核文本中的 HTML/JavaScript。

## 6. review.json 数据契约

顶层结构：

```json
{
  "schema_version": "1.0",
  "job_id": "...",
  "source": {"filename": "...", "sha256": "...", "page_count": 30},
  "glossary": {"filename": "...", "sha256": "..."},
  "pipeline": {"parser": "...", "translation_provider": "dashscope", "model": "...", "prompt_version": "..."},
  "items": [],
  "blocking_issues": [],
  "review_completed_at": "ISO-8601"
}
```

每个 `items[]` 至少包含：

```text
item_id, page_index, page_type, source_text, normalized_text,
source_bbox, source_kind, coordinate_confidence,
decision_reason, locked_tokens, glossary_hits,
suggested_translation, reviewed_translation, review_status,
risk_level, placement_strategy, target_rect, font_size,
leader_line, warnings
```

`review_status` 只允许：

```text
approved | approved_edited | skipped
```

`source.sha256`、`glossary.sha256`、页数或 `schema_version` 不匹配时，apply 阶段立即失败，不能提供“忽略并继续”选项。

## 7. FreeText 写入规范

- 文字颜色：RGB `(0.85, 0.05, 0.05)`，不使用填充背景和可见边框。
- 中文字体：优先使用 PyMuPDF 可稳定显示的简体中文 CJK 字体配置；必须在 Adobe Reader、Chrome 和福昕完成 appearance 验证。
- 默认字号 7 pt；只在自动重排阶段按 `7 → 6.5 → 6 → 5.5 → 5 pt` 下降，5 pt 为不可突破的可读下限。
- 译文按真实字体度量换行，不能仅用字符数估算矩形尺寸。
- 批注 metadata 至少写入 `item_id`、来源页和工具版本，便于追踪；不把 API key 或整段上下文写入 metadata。
- 原始页面已有注释时全部保留，新批注不得复用或覆盖其对象。

## 8. 重叠检测与自动重排闭环

### 8.1 受保护内容

受保护对象包括：

- PyMuPDF 提取的原始文字字形/bbox。
- 图片区域和非空白图像像素。
- 表格线、箭头、标尺和其他矢量绘图。
- 原 PDF 已有批注和已经放置的新 FreeText。
- CropBox 边界外区域。

源文本本身也受保护，不能被中文覆盖。引线允许指向源文本附近，但引线不能穿过文字字形、其他批注正文或关键图示。

### 8.2 双重检测

1. **几何检测**：候选矩形必须在 CropBox 内，并与受保护 bbox 保持至少 1 pt 间距；表格空白单元格按其内部净区域计算。
2. **渲染检测**：
   - 分别渲染写入前和写入后页面。
   - 常规页面使用 200 DPI；发现潜在冲突的页面以 300 DPI 复检。
   - 由前后差分得到新增红色字形掩膜，由原页面得到非背景内容掩膜。
   - 原内容和其他新批注掩膜膨胀 2 像素后，与红色字形相交超过 4 像素即判为碰撞。
   - 同时检测字形裁切、越界、FreeText 间重叠和引线穿字。

### 8.3 候选位置顺序

对每个冲突批注按固定顺序生成候选：

1. 同一表格行的空白 Placement/备注单元格。
2. 同一语义区域内，原文上、下、右、左的最近空白位置。
3. 在不改变字号的情况下改变矩形宽度和换行。
4. 按 0.5 pt 步长缩小到 5 pt，并重复步骤 1–3。
5. 移到页边空白批注轨道，增加连接源文本的引线。

候选采用确定性字典序选择：零碰撞且不越界 > 同一语义区域 > 无引线 > 与源文本距离最短 > 字号最大 > 移动距离最小。相同输入必须得到相同布局。

### 8.4 全局迭代与失败边界

1. 每移动一个批注，立即复查它与所有原内容和其他批注的关系。
2. 一页完成局部调整后重新执行整页几何与渲染检测。
3. 最多运行 10 轮全局重排；连续两轮布局未变化时提前停止。
4. 仍有碰撞、裁字或越界时写入 `unresolved_overlap`，记录页码、项目 ID、碰撞对象、尝试过的位置和最后渲染图。
5. 任一 `unresolved_overlap` 存在时不生成最终命名的 PDF，只生成失败报告供人工处理。

## 9. 错误处理与安全要求

| 场景 | 行为 |
|---|---|
| PDF 损坏或无密码加密 | 分析前阻断 |
| 术语表缺列、空值或冲突 | 生成明确行号与原因，阻断 |
| MinerU 失败但原生文本完整 | 允许降级为 PyMuPDF，所有受影响页面标中风险 |
| OCR/坐标无法可靠匹配 | 低可信，默认未批准 |
| API 鉴权失败 | 立即停止，不重试，不打印密钥 |
| API 限流/服务错误 | 最多 5 次指数退避，之后任务失败，可断点续跑 |
| 模型响应漏项或 token 被改 | 校验失败；一次纠偏后转人工 |
| review.json 与输入不匹配 | apply 阶段阻断 |
| FreeText 无法消除重叠 | `unresolved_overlap`，阻止交付 |
| 最终 PDF 重新打开失败 | 删除临时输出，任务失败 |

数据最小化：默认只向 DashScope 发送已经入选的短文本和术语；视觉理解确有必要时仅发送页面裁剪。`DASHSCOPE_API_KEY` 仅从环境变量或企业密钥管理器读取。日志、review.html 和错误报告均不得包含密钥；默认日志不记录完整 PDF 文本。

## 10. 测试清单

### 10.1 单元测试

- Unicode/空白/标点规范化和英文词边界。
- 最长术语匹配、alias、禁译项、priority 和同级冲突。
- 代码、日期、TCX、POM、数字、公差和单位的锁定与回填。
- 页面标题规则和字段级 translate/skip 决策。
- 结构节点唯一匹配、多候选匹配、模糊阈值和低可信降级。
- 模型 JSON 漏项、重复 ID、空译文和 token 丢失。
- review.json schema、哈希和状态验证。
- 候选位置排序、字号下限、页边引线和 10 轮终止条件。

### 10.2 集成与场景测试

- 用 1805466、1806119、1806093、1805491 四组资料覆盖 BOM、Measurement、技术图、Label/Pack、Sample Review 和样衣图片页。
- 1802288 只用于 Measurement Sheet 局部测试，不用于整包对照。
- 覆盖数字原生页、扫描页、混合页、旋转页、密集表格、无空白技术图、重复文字、已有批注、异常字体。
- 覆盖术语冲突、断网、429/5xx、401/403、无效 JSON、任务中断恢复、过期审核文件和输出重名。
- 验证目录输入时每个 PDF 独立失败或成功，不让一个文件的状态污染其他文件。

### 10.3 视觉金样与兼容性

- 为代表性 BOM、Measurement、技术图和 Sample Review 页面保存“输入渲染、批准译文、目标区域和最终渲染”金样。
- 自动检查页数、MediaBox/CropBox、原内容对象哈希、批注数量、文字颜色、字号范围、页面边界和未解决问题数。
- 在 Adobe Reader、Chrome 和福昕中验证中文显示、FreeText 可编辑、引线位置和打印预览。

## 11. 验收门槛

一个 PDF 只有同时满足以下条件才算成功：

- 输出页数、顺序、尺寸与输入完全一致。
- 原始英文、图片、矢量图和现有批注未被删除或覆盖。
- 新增 FreeText 数量与 review.json 中批准项目数一致。
- 每个新增项目都能追溯到唯一 `item_id`。
- 所有术语和锁定 token 验证通过。
- 所有 FreeText 在 5–7 pt 范围、位于 CropBox 内、可编辑且跨三种阅读器可见。
- 几何与 300 DPI 复检后 `unresolved_overlap = 0`。
- 最终 PDF 可重新打开、重新渲染且错误报告为空。

任何一项失败都必须返回失败状态和问题清单，不能用警告代替验收门槛。

## 12. 后续版本候选

只有在 v1 TechPack 达到验收门槛后再考虑：

- 收集成对 PO 样本，另建 PO 页面与字段规则。
- RAR/ZIP 安全解压、目录层级映射和重复文件处理。
- 经业务员确认后恢复可选的扁平化工厂副本。
- 多客户术语表、客户级规则覆盖和术语版本审批。
- 批量任务仪表板、成本统计和人工修改回流。

以上候选不应提前进入 v1，避免扩大首版风险面。

