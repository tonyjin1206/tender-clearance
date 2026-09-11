# 投标清标 Skill · 项目状态与交接文档

> 交接对象：Codex（接续开发）。请先通读本文档，再读 `skills/tender-clearance/README.md`、
> `SKILL.md`、`VALIDATION.md`、`references/external-sources.md`（四份均已与代码同步）。
> 本文回答：做完了什么 / 怎么构成的 / 哪些验证过 / 有什么已知问题 / 接下来做什么 /
> 哪些红线不能碰。

---

## 1. 项目一句话

`skills/tender-clearance/` 是一个投标清标 Skill：汇总同一采购项目多家供应商标书，
做主体交叉核验、文件属性比对、外部失信查询，输出可追溯的三级预警（I/II/III）
清标报告（MD + PDF + JSON + CSV + XLSX + 可选 DOCX）。**输出是风险线索整理，
不是违法/资格认定**；等级只由确定性规则产生，模型不得修改。

## 2. 完成状态总览（截至 2026-09-10）

| 模块 | 状态 | 说明 |
|---|---|---|
| P0 骨架与契约 | ✅ | pydantic 模型为唯一事实来源，`schemas/` 由 `gen_schemas.py` 生成 |
| P1 本地清标 | ✅ | PDF/DOCX/XLSX 提取、字段证据定位、元数据、交叉匹配、规则引擎 |
| P2 报告与审阅 | ✅ | MD + **PDF**（ReportLab，系统宋体，每章分页）+ DOCX、证据索引、复核清单、脱敏检查（含 PDF 文本层） |
| P3 外部证据导入 | ✅ | JSON/CSV/快照导入（仅政采与 SRM 目录） |
| P4 外部查询适配器 | ✅ | **外部唯一渠道：中国政府采购网**（免登录公开表单自动化，实测） |
| P5 富奥 SRM | ✅ | **内部唯一渠道**：浏览器会话模式，Playwright 无头后台，端到端实测 |
| Excel 工作底稿 | ✅ | `render_worksheet.py` → 清标底稿.xlsx（9 表，确定性输出） |
| 采购条款映射 | ✅ | `draft_procurement_clauses.py` 草案 → 人工确认 → 评估自动附条款引用（仅注释） |
| OCR | ✅ | PaddleOCR（本地推理），`ocr_provider: paddle`，字段强制低置信度 |

**测试：85 项 pytest 全绿**；`quick_validate.py` 通过；`validate_project.py --stage final`

2026-09-10 工作流程口径更新后：**86 项 pytest 全绿**；报告已增加封面信息页、基本信息页、SRM 企业信息页及政府采购网截图证据位；投标文件 `bid_subtype` 已进入 Inventory Schema，技术标和一览表不再产生主体字段。
对夹具与真实项目均通过。

## 3. 关键用户决策（历史，务必遵守）

1. **渠道范围（2026-09-10）**：外部仅保留中国政府采购网，内部仅保留富奥 SRM。
   信用中国、军队采购网、公开工商信息、司法公开信息已**整体移除**
   （SourceId、DIS-001/DIS-002 规则、导入目录、夹具、模板全部收缩，规则版本 1.1.0）。
   不要恢复这些渠道，不要再引用。
2. **SRM 走网页登录**（不用开放 API 的 token，用户认为对普通用户太麻烦）：
   用户只对话提供账号密码 → 无头后台浏览器完成登录与查询。默认无头；
   `SRM_BROWSER_HEADED=1` 仅用于验证码人工接管。不弹窗是对产品的要求。
3. **"登录后带参数直达画像"已验证不成立**（portrait_id 必须从实际会话观察），
   标准路径（档案检索→画像）是最终方案。不要重试直达思路。
4. 开放 API 形态（appKey/appSecret）保留在 `tc/srm.py` + `rules/srm-api.yaml`
   作为内部可选通道，与浏览器客户端严格分离，两者不得互相调用。

## 4. 架构与关键文件

```
skills/tender-clearance/
├── SKILL.md / README.md / VALIDATION.md     # 入口文档（已同步）
├── scripts/
│   ├── inventory.py → extract_content.py → extract_metadata.py
│   ├── normalize_and_match.py → import_external_evidence.py
│   ├── query_sources.py → assess_risk.py → render_report.py
│   ├── render_worksheet.py（xlsx）· draft_procurement_clauses.py（条款草案）
│   ├── validate_project.py（final 校验）
│   ├── srm_browser_selftest.py（SRM 单次查询自测 CLI）
│   └── tc/                                   # 核心库
│       ├── models.py（pydantic 契约）· parsers.py（PDF/DOCX/XLSX 解析）
│       ├── sources.py（HttpSourceAdapter + SrmAdapter 调度）
│       ├── srm_browser.py（浏览器客户端：状态机+凭据内存生命周期）
│       ├── srm_playwright_driver.py（Playwright 驱动，真实 DOM 校准）
│       ├── srm.py（HTTP/token 客户端，内部可选）· ocr_paddle.py（OCR 适配器）
│       └── md2pdf.py（Markdown→PDF）
├── rules/  risk-rules.yaml（v1.1.0）· external-sources.yaml（两渠道）· srm-api.yaml
├── templates/清标报告.md.jinja
├── tests/  82 项（T01–T12 场景断言 + 适配器状态机 + SRM + OCR + 底稿 + PDF）
└── references/  workflow / data-contract / evidence-and-risk-rules /
      external-sources / security-and-retention / report-spec
```

### 4.1 政采网自动化（已实测）

`POST https://www.ccgp.gov.cn/cr/list`（orgName/orgCode/…/gp 页码），免登录、
无验证码；无命中时页面明示"没有该企业的相关记录"→ `no_match_verified`；
命中为 10 列 HTML 表格（`html_table` 解析器）；按 orgCode 一致性判 confirmed/
candidate；限速 5 秒。配置在 `rules/external-sources.yaml`。

### 4.2 SRM 浏览器会话（已端到端实测，无头）

链路：CAS iframe 登录（#username/#password/#submit_btn_login，验证码 #inputCode
→ 人工接管）→ 工作台"搜索服务名称"打开供应商档案（树状导航不稳定，勿用）→
高级查询按钮 `i.yonicon-gaojichaxun` → 输入框 `input#yssupplierInputcode` →
提交按钮 `button.btnDirectSearch` → 结果为 **div 网格 + Shadow DOM**、名称列截断
（按关键字前缀匹配行，画像页再做名称+信用代码精确核对）→ "更全面企业信息" →
企业画像 iframe（URL 含 `/intellid/portrait/`）→ 基本信息/司法风险/经营风险
（分类计数格式"分类名 (数字)"；表头/表体分离渲染，逐表配对）。
凭据仅内存（`BrowserCredentials`，query 后 clear）；结果只存页面证据引用，
不存 Cookie/token/HTML；空分类/禁用分类/空 XHR 一律不解释为"无风险"。
record_kind：registration / judicial_case / judicial_summary / operating_risk /
operating_summary（摘要记录不产生风险等级）。

### 4.3 OCR（PaddleOCR）

`tc/ocr_paddle.py`：引擎单例、PaddleOCR 2.x/3.x API 自适应、测试钩子
`TC_OCR_FAKE_TEXT`（环境变量注入假文本，供子进程流水线测试）。无文本层页渲染
200dpi 位图 OCR；字段 confidence<0.85 强制 low_confidence（不参与精确匹配、
进人工核对）。依赖已装入 skill venv，声明为可选 extra。

## 5. 真实数据验证（2026-09-10，Desktop/清标项目-2026）

三家吉林环保公司标书（9 个 PDF）跑通全流程，`validate final` 通过。
**这次验证暴露的真实问题（Codex 的首要开发线索）**：

1. **【字段提取噪声】** 扫描件中"法定代表人"字段提取到"身份证明""负责制"等
   表格标签词，导致 52 条 ID-003W 弱相似告警（全是噪声）。
   根因：`extract_content.py` 的 `_scan_text_block` 上下文匹配在 OCR 文本
   （无表格结构、行序错乱）上误配 label。改进方向：OCR 通道的 label-value
   配对加"值合理性"校验（人名≠常见标签词表、值长度/字符类型约束），
   或按 OCR 行坐标做空间配对。
2. **【多代码归属歧义】** 山清技术标第 13 页（设备证书页）OCR 出两个非吉林前缀
   信用代码（设备厂商的），与该公司自己的代码混在同页。当前取"最后一个匹配"
   可能选错。改进方向：代码候选取"距供应商名称最近的"或全部保留为
   `uscc_candidates` 由人工挑选，而不是单值。
3. **【主体确认门槛】** 三家都停在 candidate（无跨文件代码交叉确认），
   政采网查询因此全部 needs_manual_review（设计如此：同名不自动归属）。
   改进方向：同一供应商多份文件提取到一致代码时自动升 confirmed（已有此逻辑？
   需核实 `normalize_and_match.py`；若已有，检查为何未生效——可能各文件
   OCR 值不一致）。
4. 赢天公司三份文件文本层+OCR 均无 18 位代码（正则复核过）——如实标注了
   absent，属正常；人工补录后重跑即可。

## 6. 剩余工作（按优先级，均在现有成果上迭代）

### 6.0 本次接续已完成

- 人员标签值增加长度与常见表格/正文词过滤，避免“身份证明、负责制、审核后实施”等误识别为姓名；新增回归测试。
- `Supplier.uscc_candidates` 保留同一供应商发现的全部有效统一社会信用代码；多个代码时不再任意选择 `uscc`，保持 `candidate` 并要求人工按证据确认。
- SRM 批量浏览器查询完成后通过 `SrmAdapter.close()` 显式销毁复用会话；新增一次登录、多次查询、关闭会话测试。
- 本次回归：85 项 pytest、Skill quick validation、虚构项目 final 校验全部通过。

以下原始问题清单中的前三项和 SRM 批量项已由上述改动处理；真实项目副本的串行 PaddleOCR 回归也已完成，旧的 52 条告警数字不再适用。

真实副本回归结果：9 个文档、154 个字段候选、0 条异常；`legal_rep_name` 从 26 条降为 2 条，其中 1 条置信度 0.7903，已被低置信度规则隔离；两家多代码供应商均保留 `uscc_candidates` 且不任意选主代码。

1. **真实样例串行回归 §5.1**：✅ 已完成；新增人员值过滤后候选从 26 条降为 2 条，低置信度项已隔离。
2. **真实样例串行回归 §5.2**：✅ 已完成；同页多代码输出 `uscc_candidates`、主代码为空并进入人工复核。
3. **主体确认逻辑已核实**：单一有效代码自动 `confirmed`；多代码或低置信度代码保持 `candidate`，相关夹具测试已通过。
4. **SRM 流水线批量**：已完成 `keep_session` 会话复用；`query_sources.py` 批量查询结束后调用 `SrmAdapter.close()` 显式关闭浏览器上下文，并有生命周期测试。
5. **SRM 端到端再验证**：用户测试密码已出现在对话中建议重置；需用户提供
   新凭据跑 `scripts/srm_browser_selftest.py`。
6. （可选）报告封面页内容充实（当前第 1 页只有标题）；
   采购条款映射需要用户提供真实采购文件后走一遍草案→确认流程。

## 7. 硬约束（不可违反）

1. **只读**：外部查询只做查询类操作；不得创建/修改/提交任何业务数据。
2. **不绕过**：验证码/登录墙/限频/接口签名（如曾探测到的军采 nsssjss）一律
   停止自动化，如实标 blocked/manual。PaddleOCR 等本地手段不涉及绕过。
3. **凭据仅内存**：账号密码/Cookie/token 只存在于进程内存或环境变量；
   不写入任何文件、日志、输出、异常。`BrowserCredentials.__repr__` 已防泄漏，
   新代码保持同等纪律。
4. **脱敏**：所有输出（含 PDF 文本层）不得出现完整身份证号/手机号；
   `render_report.py` 有硬校验（exit 3）。SRM 的 legalRepMobile 默认不采集。
5. **等级只由规则产生**：`rules/risk-rules.yaml` 确定性规则；模型/新代码
   不得改级。`no_match_verified` 仅表示"已成功查询且无匹配"，空结果不美化。
6. **状态诚实**：blocked/failed/no_result/not_queried 不得写成"无风险"。
7. **测试夹具全部虚构**；真实标书、真实响应报文不得进仓库。
8. 渠道范围：不要恢复已移除的四个渠道（§3.1）。

## 8. 验证命令（改动后必跑）

```bash
cd skills/tender-clearance
.venv/bin/python -m pytest tests -q                                  # 基线 82 passed
.venv/bin/python /Users/moc/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
# 真实项目回归（存在时）：
.venv/bin/python scripts/validate_project.py /Users/moc/Desktop/清标项目-2026 --stage final
```

注意：`tests/conftest.py` 的 `alpha_project` 是**会话级共享夹具**——测试里要修改
项目时必须先 `shutil.copytree` 到 tmp_path（历史上有测试因污染共享夹具导致
T12 确定性测试失败）。`run_stage` 是**子进程执行**——monkeypatch 传不进去，
需要用环境变量钩子（参考 `TC_OCR_FAKE_TEXT` 的做法）。playwright 的
`Locator.all()` 在当前版本**不接受 timeout 参数**（传参会 TypeError）。
PaddleOCR 3.x API 与 2.x 不同（无 show_log；结果为 dict 含 rec_texts/rec_scores），
`ocr_paddle.py` 已做双版本兼容，改动时保持。

## 9. 环境与凭据

- Python：`skills/tender-clearance/.venv`（3.12，uv 管理；依赖含 reportlab、
  paddleocr/paddlepaddle、playwright+chromium、requests 等）。
- 本机网络可能带 `http_proxy=127.0.0.1:7890` 且该代理可能未运行——
  访问 SRM/政采时注意绕过（`session.trust_env=False` 或 `--noproxy '*'`）。
- SRM 凭据：向用户索要，运行时经环境变量 `SRM_USER`/`SRM_PASSWORD` 或 getpass
  注入；旧测试密码已建议用户重置。
- 真实样例项目：`/Users/moc/Desktop/清标项目-2026/`（含三份真实标书，
  属敏感数据，不要提交进任何仓库）。

—— 接手后请在 `VALIDATION.md` 追加你的验证记录，并在完成大项时更新本文件的状态表。
