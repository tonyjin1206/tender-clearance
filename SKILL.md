---
name: tender-clearance
description: 汇总同一采购项目多家供应商标书，交叉检查企业与文件属性，并基于可追溯的公开或授权证据生成投标清标风险报告。适用于清标、供应商关联核验、标书属性比对和失信风险核验；不用于代替人工作出资格或违法认定。
---

# 投标清标（tender-clearance）

对同一采购项目的多家供应商标书做文件盘点、主体与文件属性交叉检查、
外部证据覆盖整理和确定性风险规则评估，输出 JSON、Markdown、PDF、CSV
以及按需的 DOCX/Excel。结果始终是风险线索与证据整理，不自动作出串标、
违法失信、资格不合格或废标结论；I 级和主体歧义项必须人工复核。

## 核心边界

- Core 只处理本地输入、已授权导入物、版本化规则和已缓存结果；不联网、不安装
  OCR/浏览器依赖、不运行 OCR 模型。
- 扫描页由宿主 Agent 的 OCR Skill 处理。Core 生成 `ocr-job.v1`，只接受通过
  `ocr-result.v1` 校验的结果；无结果、低置信度、无坐标或校验失败都进入人工复核。
- SRM 是正式报告的前置门禁。正式报告前必须由用户提供 SRM 用户名和密码，
  在当前运行时登录并按公司名称 + 统一社会信用代码完成查询；登录成功但没有匹配信息
  可以继续生成报告，`blocked`、`failed`、`not_queried`、`needs_manual_review` 或只导入
  历史文件均不能替代本次查询。
- 不因同一模板、编辑工具、扫描仪型号、时间或名称相近单独认定串标。
- `blocked`、`failed`、`not_queried`、`needs_manual_review` 不得写成“无风险”；
  登录成功后的 `no_result` 只能表述为“本次查询未取得记录”。
- 用户授权的报告可按 `project.yaml` 的 `redaction_mode: none` 展示完整身份证号和手机号；
  密码、Cookie、令牌永远不得进入输出、日志、OCR 任务或异常。

## 开始前确认

先执行只读预检，宿主应把 JSON 计划一次性展示给用户，再集中确认范围、缺失依赖、OCR
Provider 和 SRM 授权；确认前不得安装依赖或启动正式流水线：

```bash
$PY scripts/preflight.py $P --profile report
```

预检完成后，所有确认的安装项一次性处理；后续阶段不调用 `input()`/`getpass()`，不在
报告渲染期间安装依赖或临时联网。SRM 凭据应在流水线启动前通过运行时环境提供，缺少凭据
直接阻断，不在中途询问。

1. 项目目录：含 `project.yaml`、`bids/<供应商>/`、`procurement/` 和可选
`external-evidence/`。
2. `project.yaml` 中的 `bid_deadline`、`procurement_rules_source`、
   `external_query_mode: live` 和包含 `srm` 的 `external_query_sources`。
3. 开始 SRM 查询前向用户获取用户名和密码；缺少任一项就继续请求，不进入报告阶段。
4. 是否已有人工导入的 OCR 结果或外部证据；缺失时必须如实展示缺口。

公共信息只从投标文件封面第一页提取；公司名称、统一社会信用代码、法定代表人、
授权代表及证件字段只从商务标提取；技术标和一览表只盘点文件、属性和证据状态。

## 标准流程

先读 `references/workflow.md`、`references/data-contract.md`、
`references/evidence-and-risk-rules.md`；涉及外部渠道再读
`references/external-sources.md`；生成报告前读 `references/report-spec.md`。

```bash
PY=<本 Skill 环境>/bin/python
P=<项目目录>

$PY scripts/preflight.py       $P --profile report
$PY scripts/inventory.py          $P
$PY scripts/extract_documents.py $P
$PY scripts/normalize_and_match.py $P
$PY scripts/import_external_evidence.py $P
$PY scripts/query_sources.py        $P   # 必须登录 SRM；无结果仍会写入 no_result
$PY scripts/assess_risk.py        $P
$PY scripts/render_report.py      $P --profile report
$PY scripts/validate_project.py   $P --stage final
```

`extract_documents.py` 只解析一次本地文档并生成 `output/interim/ocr-jobs.json`；同一源文件
的底层解析快照可供内容和属性阶段复用，阶段结果仍按文档身份与配置校验，避免同内容副本
串用路径或供应商信息。文件名识别为 `技术标` 的标书跳过正文、表格、媒体和 OCR，仅保留
盘点及轻量属性状态。
宿主 OCR 完成后导入：

```bash
$PY scripts/import_ocr_results.py $P --input <ocr-results.json>
$PY scripts/normalize_and_match.py $P
$PY scripts/assess_risk.py        $P
$PY scripts/render_report.py      $P --profile report
```

SRM 查询必须在报告前执行：

```bash
$PY scripts/query_sources.py $P
```

输出档位：

- `report`：JSON、Markdown、PDF、证据索引 CSV、人工复核 CSV；
- `review`：`report` + DOCX；
- `workpaper`：`review` + Excel 工作底稿。

档位产物写入 `output/exports/<profile>/`；根目录保留验收基准文件。

## OCR 契约

宿主必须声明 `ocr.capabilities.v1`，至少包含 provider、版本、处理位置、输入格式、
`zh-Hans`/英文/数字能力、置信度、坐标精度、模型名和推理引擎。任务与结果必须分别
满足 `ocr-job.v1` 和 `ocr-result.v1`；Core 校验契约版本、任务 ID、文档 ID、文件哈希、
页号、处理位置和失败状态，失败结果必须含不含敏感信息的 `detail`。

Core 只接受 `succeeded`、`failed`、`blocked`、`not_supported`、`cancelled` 五种结果状态。
有坐标时字段标签和值按空间邻近关系配对；`page_only` 只能产生低置信度候选。
OCR 结果默认不参与 I/II 级精确主体匹配，除非人工确认后形成新的可靠证据。

生产 OCR 不属于本 Skill 的安装包。推荐的独立 Provider Profile 是
PaddleOCR `PP-OCRv4_mobile_det` + `PP-OCRv4_mobile_rec`，以 ONNX Runtime CPU
处理；这是 Provider 选择，不是 Core 依赖，也不能在报告运行时临时安装。

## 外部证据

当前只保留中国政府采购网公开查询和富奥 SRM 授权证据/浏览器会话。正式报告必须使用
用户本次提供的 SRM 凭据完成登录查询；凭据只存在当前运行时内存。登录墙、验证码、
权限阻断或主体无法确认分别记录为 `blocked` / `needs_manual_review`，不重试绕过；
登录成功但无匹配信息记录为 `no_result`，允许继续报告但不得写成无风险。

所有发现必须带规则 ID、主体、事实、证据 ID/定位、证据强度、预警等级、状态和人工复核
要求；证据强度与预警等级分开。投标截止时间缺失时禁止产生“处罚处于有效期内”的结论。

## 参考与安装

- OCR、缓存与失败状态：`references/data-contract.md`
- 阶段边界与增量运行：`references/workflow.md`
- 外部证据：`references/external-sources.md`
- 报告和输出档位：`references/report-spec.md`
- Core/Provider/SRM 分层安装：`INSTALL.md`、`requirements-core.txt`
