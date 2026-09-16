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

## 上传后的第一条回复

用户上传项目文件后，**先立即询问，后读取或解析任何业务文件**。首轮必须一次性确认：

1. 评标/投标截止时间（ISO 8601）；
2. 是否授权本次访问中国政府采购网公开查询；
3. 是否授权本次 SRM 登录查询；授权时通过当前会话安全输入或运行时环境提供账号和密码；
4. 身份证号、手机号在报告中明文显示还是脱敏。

不要因文件盘点、OCR、依赖安装或运行到外部查询阶段后才追问。密码绝不写入
`project.yaml`、日志或报告。宿主可先运行不读取项目配置和业务文件的首轮确认：

```bash
$PY scripts/preflight.py $P --intake --profile report
```

收到答案后，将截止时间、联网授权和脱敏选择写入 `project.yaml`，仅将 SRM 凭据保留为
`SRM_USER` / `SRM_PASSWORD` 的本次进程环境变量。然后执行完整只读预检；确认前不得安装
依赖或启动正式流水线：

```bash
$PY scripts/preflight.py $P --profile report
```

预检完成后，所有确认的安装项一次性处理；后续阶段不调用 `input()`/`getpass()`，不在
报告渲染期间安装依赖或临时联网。SRM 凭据应在流水线启动前通过运行时环境提供，缺少凭据
直接阻断，不在中途询问。

首次安装长命令必须通过 `scripts/install_environment.py` 或等价的宿主包装器执行。包装器
每 10 秒向编排模型发出 `TC_PROGRESS_V1` 心跳，完成或失败时发终态事件；宿主 UI 将其作为
默认折叠的模型过程信息展示，不转成普通用户答复。事件不得包含完整命令、安装日志、凭据
或令牌。

1. 项目目录：含 `project.yaml`、`bids/inbox/`（原始文件可平铺上传）、`procurement/`
和可选 `external-evidence/`。历史上已人工整理的 `bids/<供应商>/` 目录仍兼容，
但新流程不得把目录名当作供应商身份。
2. `project.yaml` 中的 `bid_deadline`、`procurement_rules_source`、
   `external_query_mode: live` 和包含 `srm` 的 `external_query_sources`。
3. 开始 SRM 查询前向用户获取用户名和密码；缺少任一项就继续请求，不进入报告阶段。
4. 是否已有人工导入的 OCR 结果或外部证据；缺失时必须如实展示缺口。

公共信息只从投标文件封面第一页提取；公司名称、统一社会信用代码、法定代表人、
授权代表及证件字段只从商务标提取。技术标和一览表只保留文件分类、首页可见文本和归组证据，不发起 OCR
归组，不读取正文主体字段；归组完成后再进入主体匹配。

## 标准流程

先读 `references/workflow.md`、`references/data-contract.md`、
`references/evidence-and-risk-rules.md`；涉及外部渠道再读
`references/external-sources.md`；生成报告前读 `references/report-spec.md`。

```bash
PY=<本 Skill 环境>/bin/python
P=<项目目录>

$PY scripts/preflight.py       $P --profile report
$PY scripts/run_pipeline.py    $P --profile report --live-query-timeout-seconds 300
$PY scripts/validate_project.py   $P --stage final
```

`run_pipeline.py` 在每个阶段输出耗时，并在 `output/interim/performance.json` 写入总耗时、
阶段耗时和失败状态；实时查询同时生成 `query-timings.json`（供应商 ID、渠道、状态与耗时，
不含凭据）。默认给实时查询 300 秒总预算，超时会停止，不生成误导性的正式报告；可按项目
规模显式调整 `--live-query-timeout-seconds`。

`extract_documents.py` 只解析一次本地文档并生成 `output/interim/ocr-jobs.json`；同一源文件
的底层解析快照可供内容和属性阶段复用，阶段结果仍按文档身份与配置校验，避免同内容副本
串用路径或供应商信息。技术标按文件类型跳过正文、表格、媒体，仅保留首页可见文本证据；扫描页不发起 OCR。商务页即使已有文字层而包含营业执照、资质证书等图片，也会生成页级 OCR 任务。
平铺上传时，`resolve_supplier_groups.py` 消费商务标正文/OCR 和技术标/一览表首页证据，
只在每份文件主体已确认且每家同时具备商务、技术、一览表角色后返回 `resolved`；招标人名称、
未识别扫描页和文件内容冲突进入人工复核，不按文件名或目录名硬合并。
宿主 OCR 完成后导入：

```bash
$PY scripts/import_ocr_results.py $P --input <ocr-results.json>
$PY scripts/resolve_supplier_groups.py $P
$PY scripts/normalize_and_match.py $P
$PY scripts/assess_risk.py        $P
$PY scripts/render_report.py      $P --profile report
```

OCR Provider 每完成一页必须发出一条 `TC_PROGRESS_V1` 页级事件，至少包含页号、总页数、
耗时或完成状态；事件只包含块数、置信度和坐标精度等统计信息，不包含 OCR 原文、身份证号
或手机号。失败页发 `page_failed` 事件后仍须按 `ocr-result.v1` 返回失败状态。

SRM 查询必须在报告前执行：

```bash
$PY scripts/query_sources.py $P
```

若只有个别供应商因验证码、限流或画像加载失败，可使用
`query_sources.py $P --supplier-id SUP-2` 定向重试；其他已成功的渠道结果保留，实时记录
按 `record_id` 去重，不重复累加。

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

生产 OCR 不属于本 Skill 的安装包。质量优先的独立 Provider Profile 是
PaddleOCR `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`，默认关闭规整 A4 页面不需要的
方向/展平预处理，并返回文本块坐标；这是 Provider 选择，不是 Core 依赖，也不能在报告
运行时临时安装。`mobile` 模型只能作为经过真实样本验收的快速档，不得仅因速度直接替换
质量档。客户已有本地 Agent OCR 时，可以直接复用，但仍必须通过能力声明、坐标和真实页
验收；不满足时保留人工复核。

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
