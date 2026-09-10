---
name: tender-clearance
description: 汇总同一采购项目多家供应商标书，交叉检查企业与文件属性，并基于可追溯的公开或授权证据生成投标清标风险报告。适用于清标、供应商关联核验、标书属性比对和失信风险核验；不用于代替人工作出资格或违法认定。
---

# 投标清标（tender-clearance）

对同一采购项目的多家供应商标书做交叉检查：先盘点并分类商务标、技术标和一览表；
公共信息只从投标文件封面第一页提取，投标人身份字段只从商务标提取，技术标和一览表
不做主体字段识别；再结合富奥 SRM 与中国政府采购网的真实、可追溯证据，输出按三级
预警（I 高 / II 中 / III 低）展示的清标报告。
**输出是风险线索与证据整理结果，不是违法或资格认定**；I 级与主体歧义项必须人工复核。

## 前置：向用户确认的信息

开始前应让用户提供（缺失时技能仍可离线运行，但相关能力受限）：

1. 项目目录（含 `project.yaml`、`bids/<供应商>/…`、`procurement/`、`external-evidence/`）；
2. 投标截止时间（写入 `project.yaml` 的 `bid_deadline`；缺失则禁止产生“处罚处于有效期内”的结论）；
3. 采购文件中的资格/否决条款来源（`procurement_rules_source`）；可选：
   运行 `scripts/draft_procurement_clauses.py` 从 procurement/ 生成条款映射草案，
   人工确认后设 `procurement_clauses_mapping`，命中发现会自动附对应条款引用；
4. 外部查询方式：`offline`（默认，不联网）/ `manual_import`（仅导入证据）/ `live`（仅访问 `external_query_sources` 明确启用的渠道）；
5. `external-evidence/` 下是否已有人工查询证据（JSON/CSV/快照+meta）。

### 本版识别范围（必须执行）

- 公共信息：招标人/采购人、项目名称、项目编号/编码、投标日期；只接受投标文件封面第一页，未能证明为封面的正文命中不得作为公共信息。
- 商务标：公司名称、统一社会信用代码、法定代表人姓名及证件掩码、授权代表姓名及证件掩码。
- 技术标：不提取主体、联系人或项目公共字段；只盘点文件并检查文件属性/证据状态。
- 一览表：不提取主体、联系人或项目公共字段；只盘点文件并检查文件属性/证据状态。
- SRM 工商、股东、分支机构、主要人员：只展示页面或授权导出实际返回的字段；未返回、未登录、权限受阻、页面无法结构化要分别记录，不能填“无”。
- 政府采购网：按供应商名称查询用户指定入口 `http://219.143.74.201/search/cr/`；需要截图时只引用实际导入/保存的证据截图，不以自动化摘要冒充截图。

渠道范围（2026-09-09 决策）：**外部仅保留中国政府采购网，内部仅保留富奥 SRM**；
信用中国、军队采购网、公开工商信息、司法公开信息渠道已移除。
中国政府采购网的失信名单查询已接入自动化（`external_query_mode: live` +
`external_query_sources: [government_procurement]`，限速 ≥5 秒/次，仅查询本项目供应商，
无需账号密码）。富奥 SRM 为内网系统，浏览器会话模式默认无头后台：让用户在对话中
提供账号密码，**仅在本会话内存中使用，绝不写入任何文件、输出或日志**；
也可让用户从 SRM 导出授权文件放入 `external-evidence/srm-authorized-export/` 后离线导入。

### 富奥 SRM 浏览器会话模式

需要查询 SRM 企业画像时，使用 `scripts/tc/srm_browser.py` 的 `SrmBrowserClient`，
由宿主的浏览器能力实现 `SrmBrowserDriver`。该模式不调用 `requests`、`curl`、
`fetch` 或已观察的 SRM XHR；浏览器只负责打开页面、输入运行时凭据、导航供应商画像、
读取可见页面，适配器负责状态机和结构化结果。

- 用户名、密码仅通过运行时回调提供，登录结束后清空；不得进入配置、日志、异常或结果；
- 遇验证码、登录墙、权限阻断或主体无法确认，分别返回 `blocked` 或
  `needs_manual_review`，不得重试绕过；
- 空 XHR、空分类或页面无法结构化不等于“无风险”，返回 `no_result` 并保留页面证据引用；
- 查询模式记录为 `browser_session`，页面事实与捕获的 XHR 必须分开标记；
- 该浏览器适配器不得接入 `srm.py` 的 HTTP/token 客户端。

## 工作流

1. **先读三份参考**：`references/workflow.md`、`references/data-contract.md`、
   `references/evidence-and-risk-rules.md`；涉及外部查询再读 `references/external-sources.md`；
   生成报告前读 `references/report-spec.md`。
2. **创建项目清单与哈希，再做提取**（顺序不可颠倒）：

```bash
python scripts/inventory.py <project_dir>
python scripts/extract_content.py <project_dir>
python scripts/extract_metadata.py <project_dir>
python scripts/normalize_and_match.py <project_dir>
python scripts/import_external_evidence.py <project_dir>
python scripts/query_sources.py <project_dir>
python scripts/assess_risk.py <project_dir>
python scripts/render_report.py <project_dir>
python scripts/render_worksheet.py <project_dir>      # 生成 清标底稿.xlsx（可选）
python scripts/validate_project.py <project_dir> --stage final
```

   （`python` 使用本 Skill 的 `.venv/bin/python`，或已安装 `pyproject.toml` 依赖的环境。）

3. **行为约束**：
   - 用统一社会信用代码优先识别企业；只有名称时形成候选，禁止把候选合并为同一主体；
   - 所有标书事实必须有证据定位（页码/单元格/属性/URL），所有规则命中必须带规则编号，
     所有外部结论必须带渠道、查询时间、主体键、状态和原始证据；
   - 外部查询默认只读；遇到验证码、登录墙、访问控制或不明确授权 → 停止自动化，
     生成“待人工查询/导入证据”任务（状态 `blocked`/`needs_manual_review`），
     不得绕过任何人机校验、登录或限频；
   - 在生成正式报告前运行 `validate_project.py`；
   - 报告同时输出 `清标结果.json`（验收基准）、`清标报告.md` + `清标报告.pdf`（审阅版）、
     `证据索引.csv`、`人工复核清单.csv`，DOCX 为可选展示副本；
   - 对 I 级与主体歧义项，在报告中显示“人工复核必需”。
4. **失败与空结果不得美化**：`not_queried`/`blocked`/`failed`/`no_result` 都不能写成
   “未发现风险”或“无失信记录”；OCR 低置信度字段不参与精确匹配，只进人工核对。
   可选 `ocr_provider: paddle`（需安装 paddleocr/paddlepaddle）对扫描页本地 OCR，
   字段置信度取 OCR 平均分并强制低置信度。

## 边界（不得执行）

- 不创建、修改或提交供应商档案、失信名单、评审结论；
- 不绕过网站登录、验证码、反爬、付费墙或访问限制；
- 不根据单一弱线索（同编辑软件、相近时间、同型号扫描仪）直接认定串通投标；
- 不在输出、日志、异常中保存完整身份证号、手机号、银行账户、密码或 Cookie；
- 不用模型判断修改规则引擎输出的预警等级（等级只能由 `rules/risk-rules.yaml` 的
  确定性规则产生）。
