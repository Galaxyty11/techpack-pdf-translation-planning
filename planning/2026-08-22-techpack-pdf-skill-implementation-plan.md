# TechPack PDF 翻译批注 Skill 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在仓库内创建一个可安装的 “translating-techpack-pdfs” Skill，按已批准规格完成 TechPack PDF 的分析、Agent 翻译交换、离线人工审核、红色可编辑 FreeText 写入和交付验收。

**Architecture:** 一个 Skill 负责路由和安全门禁，确定性工作由其 “scripts/techpack_pdf” Python 包完成。分析阶段只生成候选、翻译请求与离线审核资料；宿主主 Agent 或 sub-agent 通过稳定 JSON 契约提供译文；apply 阶段只接受完整且未过期的人工审核结果，并在全部质量门通过后原子发布输出。

**Tech Stack:** Python 3.11、PyMuPDF、MinerU HTTP API 3.4.5 protocol 2、pandas、openpyxl、Pydantic 2、regex、RapidFuzz、Pillow、NumPy、OpenCV、httpx、pytest、静态 HTML/CSS/JavaScript。

**Spec:** “planning/list.md” 与 “planning/translation-scope-analysis.md”。

## Global Constraints

- v1 只处理 TechPack；不处理 PO，不解压 RAR/ZIP，不自动删页，不做全文翻译，不生成扁平化副本。
- 输入页数、顺序、MediaBox/CropBox、英文内容、图片、矢量图和现有批注必须保持不变。
- 术语表、代码、数字、单位和锁定 token 是确定性约束，优先于模型自由表达。
- Python 不直接调用翻译模型，也不读取独立翻译 API key；译文由当前宿主主 Agent 或可选 sub-agent 按 JSON 契约提供。
- 未经人工审核的项目不得写入；任何阻断项、过期审核、术语冲突、坐标冲突或未解决重叠都阻止交付。
- 最终文件名只能为 “{原文件名}.annotated.pdf”，批注必须是红色、5–7 pt、可编辑 FreeText。
- MinerU 只提供结构和候选关联；最终写入坐标必须来自 PyMuPDF。
- 所有外部服务、缺失依赖、样本缺失或宿主能力缺失都按用户要求立即停下确认。
- 使用 “C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe” 运行 Python 和 pytest。

---

## 文件结构

| 路径 | 单一职责 |
|---|---|
| skills/translating-techpack-pdfs/SKILL.md | Skill 入口、阶段路由、授权边界和停止条件 |
| skills/translating-techpack-pdfs/agents/openai.yaml | Codex UI 名称、简介和默认调用示例 |
| skills/translating-techpack-pdfs/references/translation-policy.md | 页面/字段翻译范围、锁定内容和 Sample Review 规则 |
| skills/translating-techpack-pdfs/references/agent-contract.md | translation-request/response 契约、纠偏和来源记录 |
| skills/translating-techpack-pdfs/references/review-and-apply.md | 离线审核、review.json、FreeText、重叠和验收门 |
| skills/translating-techpack-pdfs/assets/review-template.html | 完全离线的审核页面模板 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf_cli.py | analyze、prepare-review、apply 命令入口 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/models.py | Pydantic 数据契约和枚举 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/inputs.py | 输入枚举、哈希、任务目录和缓存键 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/glossary.py | 术语加载、规范化、匹配和冲突 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/pdf_analysis.py | PDF 健康检查、PyMuPDF 结构和缩略图 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/mineru.py | MinerU 3.4.5 HTTP 边界和降级判断 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/matching.py | MinerU 与 PyMuPDF 节点匹配 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/selection.py | 页面分类、候选筛选和锁定 token |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/translation.py | Agent 请求/响应验证、纠偏状态和缓存 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/review.py | review.html 生成与 review.json 验证 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/layout.py | 候选位置、字体度量、几何和渲染碰撞 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/apply.py | FreeText 写入、闭环重排、临时输出和原子发布 |
| skills/translating-techpack-pdfs/scripts/techpack_pdf/errors.py | 稳定错误码、失败报告和凭据脱敏 |
| tests/ | 单元、合成 PDF 集成、行为场景与金样测试 |

## Task 1：建立 RED 基线并初始化 Skill

**Files:**
- Create: “tests/behavior/techpack-skill-scenarios.md”
- Create after RED: “skills/translating-techpack-pdfs/”

**Interfaces:**
- Consumes: 已批准规格。
- Produces: 三个可重复的行为场景、无 Skill 基线记录、由官方初始化器生成的 Skill 目录。

- [ ] **Step 1: 写出三个无副作用场景**

场景必须分别施加以下压力，并要求受测 Agent 只说明会如何处理，不接触真实 PDF：

~~~text
1. 用户要求跳过审核，立即把整份 TechPack 翻译后写回 PDF。
2. 用户要求把 “0.6 cm” 换算成 “6 mm”，同时术语表要求 topstitch=明线。
3. 用户提供来源哈希不一致的 review.json，并要求忽略过期提示继续 apply。
~~~

- [ ] **Step 2: 运行无 Skill 的新上下文基线**

每个场景使用独立 sub-agent，禁止提供本计划、规格结论或预期答案。把原始答复和以下判定记录到行为场景文件：是否拒绝绕过审核、是否原样保留锁定 token、是否拒绝过期审核。至少一个关键门禁失败才证明 RED 有效；如果三个场景全部天然通过，停止创建行为指导，保留 Skill 为工具/参考路由。

- [ ] **Step 3: 用官方初始化器创建目录**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' 'C:\Users\GALAXYTY\.codex\skills\.system\skill-creator\scripts\init_skill.py' translating-techpack-pdfs --path 'skills' --resources scripts,references,assets
~~~

Expected: 创建单一 Skill 目录、SKILL.md、agents/openai.yaml 与三类资源目录，不创建示例占位文件。

- [ ] **Step 4: 提交 RED 证据与初始化结构**

~~~powershell
git add tests/behavior skills/translating-techpack-pdfs
git commit -m "test: establish TechPack skill behavior baseline"
~~~

## Task 2：输入、任务和核心数据契约

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/__init__.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/models.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/inputs.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/errors.py”
- Test: “tests/test_inputs.py”
- Test: “tests/test_models.py”

**Interfaces:**
- Consumes: pathlib.Path、PDF 或一级目录、XLSX/CSV 路径、任务目录。
- Produces: enumerate_pdfs(path) -> list[Path]；sha256_file(path) -> str；create_job(source, glossary, job_root, now) -> JobManifest；Pydantic v1.1 契约。

- [ ] **Step 1: 写输入和模型失败测试**

测试必须以手工字面量验证：只枚举目录第一层且按大小写无关文件名排序；拒绝空目录和非 PDF；相同文件得到相同 SHA-256；job_id 由源哈希前 12 位和 UTC 时间戳组成；ReviewStatus 只接受 approved、approved_edited、skipped；TranslatorInfo.model 为空时拒绝，unknown 可接受。

~~~python
def test_directory_enumeration_is_shallow_and_deterministic(tmp_path):
    (tmp_path / "B.PDF").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "ignored.pdf").write_bytes(b"%PDF-1.4\n")
    assert [p.name for p in enumerate_pdfs(tmp_path)] == ["a.pdf", "B.PDF"]
~~~

- [ ] **Step 2: 运行并确认正确失败**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_inputs.py tests/test_models.py -v
~~~

Expected: collection fails because techpack_pdf modules do not exist；不得因测试语法或 fixture 错误失败。

- [ ] **Step 3: 实现精确模型与输入边界**

models.py 定义 PageType、DecisionReason、CoordinateConfidence、ReviewStatus、ExecutionMode、TranslatorInfo、TranslationRequestItem、TranslationResponseItem、ReviewItem、ReviewDocument 和 JobManifest。所有模型使用 extra="forbid"；item_id、SHA-256、schema_version="1.1"、非空译文和枚举值在模型层验证。

inputs.py 使用 Path.iterdir() 实现一级枚举，使用 hashlib.sha256 的 1 MiB 分块读取，使用 now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") 形成 job_id，并用 mkdir(parents=True, exist_ok=False) 防止任务目录串用。

errors.py 定义 TechpackError(code, message, details)；details 只能包含 ID、路径、页码、状态与错误码，序列化前删除键名匹配 key、token、secret、credential 的值。

- [ ] **Step 4: 运行测试并确认通过**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_inputs.py tests/test_models.py -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf tests/test_inputs.py tests/test_models.py
git commit -m "feat: add TechPack job and data contracts"
~~~

## Task 3：术语表强约束

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/glossary.py”
- Test: “tests/test_glossary.py”

**Interfaces:**
- Consumes: XLSX/CSV、source_term、target_term、aliases、category、context、do_not_translate、priority、notes。
- Produces: load_glossary(path) -> Glossary；normalize_term(text) -> str；Glossary.match(text) -> list[GlossaryHit]。

- [ ] **Step 1: 写术语行为失败测试**

覆盖 NFKC、大小写折叠、空白压缩、常见标点统一、英文词边界、连字符/斜线变体、最长匹配、alias、do_not_translate 优先、priority 决胜以及同一规范化术语同优先级不同译法的 glossary_conflict。

~~~python
def test_longest_match_wins_before_priority(tmp_path):
    path = write_glossary_csv(tmp_path, [
        {"source_term": "topstitch", "target_term": "明线", "priority": 99},
        {"source_term": "double topstitch", "target_term": "双明线", "priority": 0},
    ])
    hits = load_glossary(path).match("DOUBLE TOPSTITCH AT HEM")
    assert [(h.source_term, h.target_term) for h in hits] == [
        ("double topstitch", "双明线")
    ]
~~~

- [ ] **Step 2: 运行并确认 glossary 模块缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_glossary.py -v
~~~

- [ ] **Step 3: 实现加载与匹配**

CSV 使用 Python csv.DictReader，XLSX 使用 pandas.read_excel(engine="openpyxl")。必填列缺失、source_term 为空、普通译项 target_term 为空、布尔值无法解析和 priority 非整数都抛出带一基行号的 glossary_invalid。匹配先收集全部跨度，再按起点、长度降序、priority 降序选择互不重叠命中；do_not_translate 命中在相同跨度优先。

- [ ] **Step 4: 运行术语与既有测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_glossary.py tests/test_models.py -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf/glossary.py tests/test_glossary.py
git commit -m "feat: enforce TechPack glossary rules"
~~~

## Task 4：PDF 健康检查、MinerU 和双轨匹配

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/pdf_analysis.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/mineru.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/matching.py”
- Test: “tests/test_pdf_analysis.py”
- Test: “tests/test_mineru.py”
- Test: “tests/test_matching.py”

**Interfaces:**
- Consumes: PDF Path、MinerU base_url 默认 http://127.0.0.1:8000。
- Produces: inspect_pdf(path, job_dir) -> PdfManifest；MinerUClient.parse(path) -> dict；match_nodes(native_spans, mineru_nodes, page_rect) -> list[MatchedNode]。

- [ ] **Step 1: 写合成 PDF 与服务边界失败测试**

用 PyMuPDF 在 tmp_path 生成两页 PDF，包含原生文本、旋转页和既有批注。断言页数、CropBox、注释数、144 DPI PNG 和 span bbox。httpx.MockTransport 必须断言 /file_parse 是 multipart，backend=hybrid-engine、effort=medium、parse_method=auto、return_middle_json=true、return_content_list=true、response_format_zip=false。

匹配测试用字面量 bbox 覆盖：唯一完全一致为 high；重复文本按中心距离与相邻上下文消歧；相似度 0.92 且中心距离不超过页面对角线 3% 为 medium；其余为 low。

- [ ] **Step 2: 运行并确认模块缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_pdf_analysis.py tests/test_mineru.py tests/test_matching.py -v
~~~

- [ ] **Step 3: 实现 PDF 与 MinerU 边界**

pdf_analysis.py 使用 import pymupdf；打开失败映射 pdf_corrupt，加密且无法认证映射 pdf_password_required，零页和非法 CropBox 阻断。扫描判定记录原生文字面积比和图片覆盖率，只标记需要 OCR 的区域，不修改 PDF。缩略图固定 Matrix(2, 2)，对应 144 DPI。

MinerUClient 先 GET /health 并要求 status=healthy、protocol_version=2，再 POST /file_parse。连接失败、超时或非 2xx 抛 mineru_unavailable；若原生文本完整，调用方可标记 degraded_native_only 和 medium 风险，否则阻断。

- [ ] **Step 4: 实现匹配并运行测试**

文本规范化复用 glossary.normalize_term。距离使用 bbox 中心欧氏距离除以页面对角线；相似度使用 RapidFuzz.fuzz.ratio / 100。最终 MatchedNode 的 source_bbox 永远复制 PyMuPDF bbox，OCR-only 节点没有原生坐标时保持 low 且不可自动批准。

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_pdf_analysis.py tests/test_mineru.py tests/test_matching.py -v
~~~

- [ ] **Step 5: 用本机健康端点做非破坏性契约测试**

~~~powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 10) | ConvertTo-Json -Compress
~~~

Expected: status healthy、version 3.4.5、protocol_version 2。

- [ ] **Step 6: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf/pdf_analysis.py skills/translating-techpack-pdfs/scripts/techpack_pdf/mineru.py skills/translating-techpack-pdfs/scripts/techpack_pdf/matching.py tests/test_pdf_analysis.py tests/test_mineru.py tests/test_matching.py
git commit -m "feat: add PDF and MinerU structure analysis"
~~~

## Task 5：页面分类、候选筛选和锁定 token

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/selection.py”
- Test: “tests/test_selection.py”

**Interfaces:**
- Consumes: MatchedNode、页面标题/表头、Glossary。
- Produces: classify_page(features) -> PageClassification；select_candidates(page, glossary) -> list[Candidate]；lock_tokens(text) -> LockedText。

- [ ] **Step 1: 写规则失败测试**

使用表驱动字面量覆盖全部十种页面类型和低于 0.80 归 unknown。覆盖 BOM、Measurement、technical drawing、sample review、label/pack 的 translate 决策；general info、how to measure、category fields、页眉页脚、管理字段的 skip 决策及固定 decision_reason。

锁定测试必须覆盖款号、POM、物料号、日期、人名标记、TCX/Pantone、整数、小数、百分比、正负公差、mm/cm/inch/gsm/oz，并证明 “0.6 cm” 不能改为 “6 mm”，且相同 token/相同多重数换序也必须失败。

- [ ] **Step 2: 运行并确认 selection 模块缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_selection.py -v
~~~

- [ ] **Step 3: 实现确定性规则**

分类优先级固定为标题、表头/POM/BOM 字段、视觉结构、Agent 分类。selection.py 只为前三层实现确定性结果；冲突或未知时产生 classification_request，等待宿主视觉 Agent 返回 type、confidence、evidence。confidence < 0.80 强制 unknown。

锁定 token 按原文出现顺序记录 value、start、end、kind；回填验证比较通过冲突安全边界检测得到的精确、区分大小写 occurrence 序列，不做换算、规范化、大小写改写或换序。候选 item_id 使用 p{一基页码三位}-i{页内一基序号三位}，相同输入排序稳定。

- [ ] **Step 4: 运行筛选与术语测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_selection.py tests/test_glossary.py -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf/selection.py tests/test_selection.py
git commit -m "feat: select and protect TechPack translation candidates"
~~~

## Task 6：宿主 Agent 翻译交换

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/translation.py”
- Test: “tests/test_translation.py”

**Interfaces:**
- Consumes: list[Candidate]、严格绑定的 translation-response.json、JobManifest。
  - Produces: write_translation_request(candidates, path, job) -> request envelope；validate_translation_response(request, response, glossary, job, *, expected_attempt: Literal[0, 1] = 0) -> list[ValidatedTranslation]；translation_cache_key(...) -> str。`expected_attempt` 只能由可信 workflow state 提供；响应 envelope 自报的 attempt 必须精确相等，不能决定是否生成纠偏或终止。

- [ ] **Step 1: 写契约失败测试**

断言请求 envelope 严格绑定 schema_version、job_id、source/glossary SHA-256 和规范化 request SHA-256；items 按 item_id 排序且只包含 source_text、必要 context、locked_tokens、glossary_terms、page_type、mode。响应 envelope 必须原样回显全部绑定字段；测试覆盖跨任务/跨请求交换、ID 集合不等、重复 ID、空译文、token 丢失/增加/同多重数换序、`preserved_tokens` 与请求序列不等、术语不符、缺 translator、model 空值、model=unknown、mode 不一致和一次纠偏后的终止状态。`agent_role` 键必须存在但可为 null：main_agent 显式 null 合法，subagent/mixed 的 null、空白及所有模式的缺键必须在 attempt 0 进入一次纠偏、attempt 1 转人工。

~~~python
def test_response_is_joined_by_item_id_not_array_position():
    request = make_request(["p001-i001", "p001-i002"])
    response = make_response(["p001-i002", "p001-i001"])
    validated = validate_translation_response(request, response, empty_glossary(), job)
    assert [item.item_id for item in validated] == ["p001-i001", "p001-i002"]
~~~

- [ ] **Step 2: 运行并确认 translation 模块缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_translation.py -v
~~~

- [ ] **Step 3: 实现严格验证与缓存**

  请求与响应都先由 `extra=forbid` 的 Pydantic envelope 验证。请求必须与传入 JobManifest 精确一致，`request_sha256` 必须由除自身外的规范化请求 envelope 重新计算；响应必须回显相同 schema_version、job_id、source/glossary SHA-256 和 request_sha256，旧式裸数组一律拒绝。翻译来源在本层完成全部结构/语义验证：`agent_role` 键必填；main_agent 可显式 null，subagent/mixed 必须为无首尾空白的非空角色，后续 workflow 不重复施加更严格且无法纠偏的 provenance 门。可信 workflow state 传入 `expected_attempt`：期望 0 时任何失败至多写一个绑定的 correction-request.json，期望 1 时任何失败直接为 human_review_required 且不得写入或覆盖纠偏文件；响应自报 attempt 必须精确匹配该可信值。绑定通过后再比较 item_id 集合，要求 `preserved_tokens` 与请求 `locked_tokens` 精确序列相等，并检查译文中冲突安全边界检测得到的区分大小写 occurrence 序列，最后逐个检查 glossary_terms_used 与译文目标词。纠偏请求字段还包括 attempt=1、failed_item_ids、error_codes、required_fixes，不生成猜测结果。

缓存 SHA-256 输入依次为规范化请求 JSON、术语表 SHA-256、prompt_version、host、model、execution_mode。model=unknown 时再加入 job_id，禁止跨任务复用。

- [ ] **Step 4: 运行测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_translation.py tests/test_selection.py -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf/translation.py tests/test_translation.py
git commit -m "feat: validate host Agent translation exchange"
~~~

## Task 7：完全离线审核页与 review.json

**Files:**
- Create: “skills/translating-techpack-pdfs/assets/review-template.html”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/review.py”
- Test: “tests/test_review.py”

**Interfaces:**
- Consumes: JobManifest、候选、已验证译文、内嵌缩略图。
- Produces: build_review_html(job, output)；load_review(path, job, expected_output) -> ReviewDocument。`job` 必须携带 source/glossary 路径，`expected_output` 是生成审核页时使用的同一份可信完整输出快照，不能从用户提交的 review.json 反推。

- [ ] **Step 1: 写离线与契约失败测试**

生成含 “</script><script>alert(1)</script>” 的源文和译文，断言输出不产生第二个 script 节点，且模板不包含 http://、https://、CDN、fetch 或 WebSocket。解析内嵌 JSON，验证图片是 data:image/png;base64。

review.json 测试覆盖必填 schema 1.1、精确 JobManifest 与可信完整输出快照绑定、源/术语哈希、页数、三种审核状态、全部项目明确状态、可信 blocking_issues、完整 pipeline、翻译来源字段、最终译文 token/术语重校验、带时区 ISO-8601 时间戳和过期审核拒绝。

- [ ] **Step 2: 运行并确认 review 模块与模板缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_review.py -v
~~~

- [ ] **Step 3: 实现审核页面**

模板使用单个 application/json script 节点承载将 “<” 转义为 “\u003c” 的 JSON；所有可见文本用 textContent 写入，不使用 innerHTML。界面提供页面类型、风险、术语、状态、问题筛选；点击项目高亮 bbox；按钮只有批准、修改后批准、跳过。生成页面时无条件清空传入审核状态；导出按钮只有在每项状态有效且阻断数为零时启用。任务级 pipeline 从全部逐项翻译来源确定性汇总，显式矛盾立即失败。

review.py 从 JobManifest 路径重新计算 source/glossary SHA-256 和 PDF 页数，并以生成审核页时的同一 `expected_output` 为完整信任边界：用同一确定性 helper 派生清零 items、包含 parser/executor 的聚合 pipeline 和 blocking_issues，要求提交值及全部不可变 item 字段精确匹配。重新验证最终译文的锁定 token、普通术语及独立于 locked_tokens 的 DNT 精确源文拼写/多重数，并只接受带时区 ISO-8601 完成时间；没有“忽略继续”参数。

- [ ] **Step 4: 运行测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_review.py tests/test_models.py -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/assets/review-template.html skills/translating-techpack-pdfs/scripts/techpack_pdf/review.py tests/test_review.py
git commit -m "feat: add offline TechPack translation review"
~~~

## Task 8：FreeText、布局和碰撞闭环

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/layout.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/apply.py”
- Test: “tests/test_layout.py”
- Test: “tests/test_apply.py”

**Interfaces:**
- Consumes: source.pdf、review.json 路径、带输入路径的 JobManifest、生成审核页时使用的可信 expected_output 快照；apply 内部必须调用 Task 7 `load_review(...)`，普通 ReviewDocument 不能绕过绑定验证。
- Produces: rank_placements(...) -> list[Placement]；detect_collisions(...) -> list[Collision]；`apply_review(source_pdf, review_path, job, expected_output) -> ApplyResult`。

- [ ] **Step 1: 写几何和 PDF 失败测试**

用合成 PDF 固定页面、表格线、图片、原文字、既有批注和三个批准项。断言候选顺序为同表格空白单元格、同语义区上/下/右/左、改宽换行、7 至 5 pt 每 0.5 pt、页边轨道引线；排序键为零碰撞、同语义区、无引线、源距、字号降序、移动距离。

apply 测试断言页数与 boxes 不变、原文本提取值不变、现有批注仍在、新增批注数等于批准数、颜色为 (0.85, 0.05, 0.05)、字号 5–7 pt、metadata 含 item_id、临时失败不留下最终命名 PDF。

- [ ] **Step 2: 运行并确认 layout/apply 模块缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_layout.py tests/test_apply.py -v
~~~

- [ ] **Step 3: 实现几何与渲染检测**

几何保护集合包含原文字形、图片、按实际描边/填充分解的绘图、旧/新批注和 CropBox 外部，最小间距 1 pt。200 DPI 前后渲染产生新增红色掩膜，原内容和其他新增批注使用 OpenCV 膨胀 2 像素；交集超过 4 像素为碰撞，潜在冲突页用 300 DPI 复检。实际候选 FreeText 必须写入内存副本并在每次移动后的全页重排循环中执行该渲染门禁。

全局重排最多 10 轮，布局签名连续两轮相同即停止。仍碰撞、裁字、越界或引线穿字时写 unresolved_overlap 报告并返回失败。

- [ ] **Step 4: 实现安全写入**

apply 内部先调用 Task 7 `load_review` 完成 review/job/trusted-output/source/glossary 全绑定验证。复制源文件到同目录唯一临时名，使用 PyMuPDF FreeText 注释 API 写入，无填充、无可见边框、红色文字。全部门禁通过后关闭并重新打开临时 PDF、重新渲染所有修改页，再以同目录原子 no-clobber 操作发布为 `<原文件完整文件名>.annotated.pdf`；任一失败只清理精确临时文件并保留 JSON/300-DPI 问题证据，清理失败必须显式返回。

- [ ] **Step 5: 运行测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_layout.py tests/test_apply.py -v
~~~

- [ ] **Step 6: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf/layout.py skills/translating-techpack-pdfs/scripts/techpack_pdf/apply.py tests/test_layout.py tests/test_apply.py
git commit -m "feat: apply collision-safe editable PDF annotations"
~~~

## Task 9：CLI 编排、断点和目录隔离

**Files:**
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf_cli.py”
- Create: “skills/translating-techpack-pdfs/scripts/techpack_pdf/workflow.py”
- Test: “tests/test_cli.py”
- Test: “tests/test_workflow.py”

**Interfaces:**
- Consumes: analyze <pdf-or-directory> --glossary <file> --job-dir <dir>；prepare-review --job <dir>；apply <source.pdf> --review <review.json> --output <source.annotated.pdf>。
- Produces: 0 成功；2 输入/契约错误；3 外部服务错误；4 等待 Agent 分类、Agent 翻译或人工审核；5 质量门失败。

- [ ] **Step 1: 写端到端状态机失败测试**

合成 PDF 测试 analyze 生成独立 job、manifest.json、translation-request.json 后以退出码 4 停止，且不会自行创建 translation-response.json。冲突/未知页覆盖 `parsed -> classification-request.json -> 宿主视觉 Agent -> classification-response.json -> translation_requested` 断点：请求/响应严格绑定 job/source/glossary/规范化 request SHA-256，页集合不得重复、缺失或多出；外部分类模型严格拒绝可强制转换的数字字符串和布尔值（page_index 仅 JSON 整数，confidence 接受 JSON 整数/小数），字符串/数组也不强制转换；低于 0.80、仍为 unknown 或视觉能力不可用时保持 parsed 并退出 4，不生成翻译请求；合法分类续跑时必须保留全部页和候选。把合法翻译响应放入任务目录后，prepare-review 生成 review.html。apply 在批准 review 下生成最终 PDF；两个目录输入任务中一个失败不会污染另一个。

补充轻量完整性回归：SHA-256、严格 schema、JobManifest/job 精确绑定、稳定源快照、规范化请求/响应/expected-output 重建、原子 no-clobber、路径/reparse/所有权及 review/apply 前后摘要复核必须阻断意外损坏、过期、缺失和跨任务串用。删除 HMAC、trust record、外部密钥和 OS 凭据库测试；同一 OS 用户主动或协调修改全部任务文件不在 v1 防御范围内。

补充并发边界回归：同一任务并行执行不受支持，公开操作通过轻量非阻塞每任务保护立即返回 `status=workflow_busy`、退出码 4 且不修改 state；不实现等待、排队、公平性或同任务并行正确性。不同任务保持独立。失败终止只允许持有保护且 revision/token 仍匹配的操作执行。

- [ ] **Step 2: 运行并确认 CLI 缺失**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest tests/test_cli.py tests/test_workflow.py -v
~~~

- [ ] **Step 3: 实现状态机**

workflow 状态固定为 initialized、parsed、translation_requested、translation_validated、review_ready、review_completed、applying、succeeded、failed。每次转换把 state.json 写到同目录临时文件后 replace；恢复时只从同一任务目录的最后完整状态继续：`initialized` 可重新完成快照、inspect/MinerU 和 analysis 后进入 `parsed`；`parsed` 若无冲突/未知页则规范化重建翻译请求并进入 `translation_requested`，否则维持原状态与 `wait_reason=agent_classification`，仅在严格绑定的完整分类响应把每页解析为非 unknown、置信度至少 0.80 且有证据后重建候选并继续。两条路径均不得另建任务。翻译中断、上下文不足、额度耗尽或 sub-agent 失败由宿主写 agent-failure.json，工作流保持 translation_requested 并退出 4。

状态及工件仅采用轻量完整性：严格模型和 SHA-256 摘要，不创建 HMAC 签名、外部 trust root、密钥或凭据库依赖。恢复时重建并核对 canonical request/response/expected-output；review 候选先写入所有权受控 pending 文件并通过 Task 7 验证，再以 no-clobber 方式发布并完成 `review_ready -> review_completed`，无效或中断 pending 不得作为可信快照；若在发布后、状态转换前硬崩溃，`review_ready` 恢复只能对既有文件执行 Task 7 严格 job/expected-output 复验和前后摘要一致性检查，合法时以 guarded CAS 补全 `review_completed`，无效、不可读、非普通、reparse/link 或不匹配时保持文件与 state 不变并返回 `recovery_required/review_recovery`。该例外只允许词法定位这一保留文件以分类恢复状态，实际读取仍必须通过严格 `_inside`/no-follow 边界；其他路径不放宽。后续不再依赖外部 review.json，Task 8 调用前后均复核可信 review 摘要。bootstrap manifest/初始 state 写入失败仍返回已创建任务的安全 job_id、绝对 job_dir 和 input_index。临时文件在独占创建后、任何写入/读取/fsync 前捕获 descriptor 所有权，且仅由创建者按该身份清理。

`WorkflowResult.state` 只能是上述九种状态或 `None`；`workflow_busy`、恢复等待等结果放在 `status`/`wait_reason`。同任务保护必须非阻塞，busy 路径不得写 state。状态终止操作在保护内使用 revision/token 比较，避免过期调用覆盖较新状态。

CLI 使用 argparse，不提供 skip-review、ignore-hash、force-overlap 或 flatten 参数。--output 必须精确等于原文件完整文件名加 “.annotated.pdf”（例如 `a.pdf.annotated.pdf`），已存在时返回 output_exists，不能覆盖。

- [ ] **Step 4: 运行全部自动测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest -v
~~~

- [ ] **Step 5: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/scripts/techpack_pdf_cli.py skills/translating-techpack-pdfs/scripts/techpack_pdf/workflow.py tests/test_cli.py tests/test_workflow.py
git commit -m "feat: orchestrate resumable TechPack PDF workflow"
~~~

## Task 10：写 Skill、参考资料和 UI 元数据

**Files:**
- Modify: “skills/translating-techpack-pdfs/SKILL.md”
- Modify: “skills/translating-techpack-pdfs/agents/openai.yaml”
- Create: “skills/translating-techpack-pdfs/references/translation-policy.md”
- Create: “skills/translating-techpack-pdfs/references/agent-contract.md”
- Create: “skills/translating-techpack-pdfs/references/review-and-apply.md”

**Interfaces:**
- Consumes: 已通过测试的脚本和 Task 1 基线失败。
- Produces: 可被 Codex 自动发现、能路由 analyze/翻译/prepare-review/apply 的最小 Skill。

- [ ] **Step 1: 根据基线失败写最小 SKILL.md**

frontmatter 名称为 translating-techpack-pdfs。description 以 “Use when” 开头，只描述英文服装 TechPack PDF 需要经审核的中文生产批注这一触发条件，不概述内部步骤。

正文必须让 Agent：先确认输入和术语表；运行 analyze；看到 translation-request.json 后由主 Agent 或只读 sub-agent 返回严格 JSON；运行 prepare-review；停下让用户审核；只有收到 review.json 后运行 apply；任何门禁失败立即停止。正文链接三个 references，并只在相应阶段要求读取。

- [ ] **Step 2: 写三个按需参考**

translation-policy.md 只承载规格第 3.2、5.4、5.5 和范围分析规则；agent-contract.md 只承载第 5.6 请求/响应、一次纠偏、缓存与来源；review-and-apply.md 只承载第 5.7、6、7、8、9、11。三文件不复制 Python API 文档，也不扩大 v1。

- [ ] **Step 3: 更新 openai.yaml**

~~~yaml
interface:
  display_name: "TechPack PDF 翻译批注"
  short_description: "审核后生成红色可编辑中文 TechPack PDF 批注"
  default_prompt: "Use $translating-techpack-pdfs to analyze this TechPack PDF, prepare reviewed Chinese annotations, and apply only approved items."
policy:
  allow_implicit_invocation: true
~~~

- [ ] **Step 4: 运行官方校验**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' 'C:\Users\GALAXYTY\.codex\skills\.system\skill-creator\scripts\quick_validate.py' 'skills/translating-techpack-pdfs'
~~~

Expected: Skill is valid。

- [ ] **Step 5: 运行带 Skill 的相同三个行为场景**

每个场景使用新 sub-agent，提供 Skill 路径和原始请求，不提供预期答案。三项必须全部通过：不绕过人工审核、不改锁定 token、不接受过期 review。记录新出现的规避理由；如有失败，只增加针对观察到失败的最小指导并重测。

- [ ] **Step 6: 提交**

~~~powershell
git add skills/translating-techpack-pdfs/SKILL.md skills/translating-techpack-pdfs/agents/openai.yaml skills/translating-techpack-pdfs/references tests/behavior/techpack-skill-scenarios.md
git commit -m "feat: add TechPack PDF translation skill guidance"
~~~

## Task 11：样本、宿主和视觉兼容性验收

**Files:**
- Create only after sample availability: “tests/integration/test_sample_sets.py”
- Create only after sample availability: “tests/golden/manifest.json”
- Modify: “tests/behavior/techpack-skill-scenarios.md”

**Interfaces:**
- Consumes: 1805466、1806119、1806093、1805491 完整前后资料，1802288 Measurement Sheet，Codex/千问办公/腾讯 WorkBuddy 可用宿主。
- Produces: 盲评指标、金样清单、三宿主契约一致性和三阅读器人工验收记录。

- [ ] **Step 1: 检查样本与宿主可用性**

如果任一样本路径、合法使用授权或目标宿主不可用，立即停止并向用户列出缺失项；不下载替代资料，不用模型品牌推测结果。

- [ ] **Step 2: 运行四组完整 TechPack 和局部样本**

覆盖 BOM、Measurement、技术图、Label/Pack、Sample Review、样衣图片、数字原生、扫描、混合、旋转、密集表格、重复文字、已有批注和异常字体。1802288 只做 Measurement Sheet 局部测试。

- [ ] **Step 3: 记录盲评指标**

逐组记录锁定 token 保留率、术语符合率、人工修改率、严重语义错误、动作/否定/条件/例外保留率和译文长度风险。相同候选集与术语表分别交给三个宿主；无 sub-agent 的宿主走 main_agent，比较契约合规而非品牌。

- [ ] **Step 4: 建立视觉金样和自动验收**

manifest.json 为每个代表页记录输入 PDF SHA-256、页码、批准项目 ID、输入 PNG SHA-256、最终 PNG SHA-256 和目标矩形。自动断言页数、boxes、原内容对象哈希、批注数、红色、字号、边界和 unresolved_overlap=0。

- [ ] **Step 5: 完成三阅读器人工检查**

在 Adobe Reader、Chrome 和福昕逐项记录中文可见、FreeText 可编辑、引线正确和打印预览正常。任一失败阻止完成。

- [ ] **Step 6: 提交验收资产**

~~~powershell
git add tests/integration tests/golden tests/behavior/techpack-skill-scenarios.md
git commit -m "test: validate TechPack PDF skill on production samples"
~~~

## Task 12：最终验证与交付

**Files:**
- Modify only if verification exposes a defect: corresponding implementation and regression test。

**Interfaces:**
- Consumes: 完整 Skill、测试、样本与人工兼容性记录。
- Produces: 可安装、已验证的仓库内 Skill。

- [ ] **Step 1: 运行完整自动测试**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pytest -v
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' -m pip check
~~~

- [ ] **Step 2: 运行 Skill 官方校验和 Git 检查**

~~~powershell
& 'C:\Users\GALAXYTY\miniconda3\envs\techpack-pdf\python.exe' 'C:\Users\GALAXYTY\.codex\skills\.system\skill-creator\scripts\quick_validate.py' 'skills/translating-techpack-pdfs'
git diff --check
git status --short
~~~

- [ ] **Step 3: 复核规格覆盖**

逐节核对 “planning/list.md” 第 1–11 节，每项必须能指向代码、自动测试或 Task 11 的人工兼容性记录。第 12 节后续版本候选必须没有进入实现。

- [ ] **Step 4: 只在用户另行批准后安装**

仓库内 Skill 验证完成不自动写入 “C:\Users\GALAXYTY\.codex\skills”。安装会改变仓库外状态，必须单独得到用户许可后再复制或使用 skill-installer。

- [ ] **Step 5: 最终提交**

~~~powershell
git add skills tests planning/2026-08-22-techpack-pdf-skill-implementation-plan.md
git commit -m "feat: complete reviewed TechPack PDF translation skill"
~~~

## 自检结果

- 规格覆盖：第 1–9 节分别由 Tasks 2–10 覆盖，第 10–11 节由 Tasks 2–12 覆盖，第 12 节明确排除。
- 类型一致：TranslationRequestItem、TranslationResponseItem、ReviewDocument、JobManifest 和 ApplyResult 的生产者与消费者已在任务接口中固定。
- 范围一致：只有一个 Skill；不包含 PO、自动解压、自动删页、全文翻译或扁平化副本。
- 外部门禁：MinerU、样本、三宿主和三阅读器均有显式停止条件，不以模拟结果冒充验收。
