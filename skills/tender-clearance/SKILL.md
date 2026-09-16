---
name: tender-clearance
description: 对同一采购项目多家供应商标书做文件盘点、主体与属性交叉检查、授权外部证据整理，并生成可追溯的风险线索报告。
---

# 投标清标

这是一个“证据整理 + 确定性规则评估”流程，不替代人工认定串标、违法失信、资格不合格或废标。I 级线索、主体歧义和低置信度证据必须人工复核。

## 先判断交互状态

- 仅询问/设计：只回答，不运行脚本、不改文件。
- 用户上传标书并要求处理：第一条回复只一次性确认以下 4 项，确认前不读取、解析、安装或联网：
  1. 评标/投标截止时间（ISO 8601）；
  2. 是否授权访问中国政府采购网；
  3. 是否授权本次 SRM 登录查询；授权时仅通过当前会话或 `SRM_USER`/`SRM_PASSWORD` 提供凭据；
  4. 身份证号/手机号明文显示还是脱敏。
- 用户明确说“加载 skill”但未要求处理文件：只确认已加载，不启动流水线。

凭据永远不能进入 `project.yaml`、命令回显、日志、OCR 任务或报告。不得用历史 SRM 导出替代本次 live 查询。

## 核心边界

- Core 只读本地输入、已授权导入物、规则和缓存；不联网、不安装、不运行生产 OCR。
- 宿主 OCR 只处理 `ocr-jobs.json` 中的任务，回传并校验 `ocr-result.v1`；不要逐页人工读取来替代批量 OCR。无结果、失败、无坐标或置信度不足都保留缺口并进入人工复核。
- 公共项只从封面第一页提取；企业主体字段只从商务标提取；技术标/一览表只读首页用于分类和归组，不读正文主体字段。
- 统一社会信用代码校验通过才可确认主体；仅名称相同是候选，不自动归属外部记录。
- `blocked`、`failed`、`not_queried`、`needs_manual_review` 不是“无风险”；`no_result` 只能写“本次查询未取得可判定记录”。
- 不因同模板、编辑器、扫描仪、时间或名称相近单独认定风险。

## 标准执行路由

定义：`P` 为项目目录，`PY` 为该 Skill 环境中的 Python。项目应含 `project.yaml`、`bids/`、`procurement/`，原始文件只读。

1. 首轮确认后写入项目配置，再运行一次只读预检：

   ```bash
   $PY scripts/preflight.py $P --profile report
   ```

   只安装预检列出的缺失依赖，并在流水线启动前准备好 SRM 凭据；不要中途 `input()`/`getpass()`，不要用 `sleep` 轮询。

2. 预检通过后只运行一次主入口：

   ```bash
   $PY scripts/run_pipeline.py $P --profile report --live-query-timeout-seconds 300
   ```

   主入口会按阶段输出简短状态、实时转发长阶段进度，并写入 `output/interim/performance.json` 和 `query-timings.json`。不要把每个阶段拆成独立模型回合，不要重复读取参考文档或完整 JSON；失败时先看这两个摘要文件。

3. 若因 OCR 或人工归组暂停，补齐材料后从断点继续：

   ```bash
   $PY scripts/import_ocr_results.py $P --input <ocr-results.json>
   $PY scripts/run_pipeline.py $P --profile report --resume
   ```

   `--resume` 只执行上次未成功阶段及其后续阶段；也可使用 `--from-stage normalize_and_match`、`--to-stage assess_risk` 做明确的局部运行。不要从头重跑。

4. 只有在确需重新访问所有 live 渠道时才使用 `--refresh-queries`。单一供应商失败时优先定向重试，再从规则阶段继续：

   ```bash
   $PY scripts/query_sources.py $P --supplier-id SUP-2
   $PY scripts/run_pipeline.py $P --profile report --from-stage assess_risk
   ```

   默认会复用同一主体、同一 live 适配器已有的 `match`、`no_match_verified` 或 `no_result`；不会复用导入记录、失败、阻断或主体待确认状态。

## 阶段和输出

- `inventory.py`：文件哈希、分类、读取异常；
- `extract_documents.py`：文本/字段候选、元数据、OCR 任务；
- `resolve_supplier_groups.py`：基于正文/OCR 证据归组；未 `resolved` 不进入正式报告；
- `normalize_and_match.py` + `analyze_bid_data.py`：主体、人员、联系方式和文件属性交叉匹配；
- `import_external_evidence.py` + `query_sources.py`：导入与授权 live 查询；导入阶段增量合并，不清空已有 live 结果；
- `assess_risk.py`：规则、证据强度、等级、覆盖矩阵和人工复核队列；
- `render_report.py`：报告和导出副本；报告阶段不触发 OCR、联网或安装。它同时生成
  `output/interim/process-workpaper.json` 与 `report-input.json`；前者保留字段级质量信息，
  后者不含 OCR 原文、文本块、坐标、模型详情或数值置信度。

正式报告前，`ocr-jobs.json` 中的每个任务必须在 `ocr-results.json` 中有明确终态；缺页时
正式报告阻断，离线草稿可继续但必须保留人工复核缺口。

`report` 输出 JSON、Markdown、PDF、证据索引 CSV、人工复核 CSV；`review` 另含 DOCX；`workpaper` 另含 Excel。正式报告前必须运行：

```bash
$PY scripts/validate_project.py $P --stage final
```

## 按需读取参考资料

不要每次启动都加载全部 references。只有遇到对应分支才读取：

- `references/workflow.md`：阶段依赖、增量运行和测试流程；
- `references/data-contract.md`：OCR、字段、缓存或脱敏契约；
- `references/external-sources.md`：渠道、授权和 SRM 状态；
- `references/report-spec.md`：报告字段、章节或档位；
- `references/evidence-and-risk-rules.md`：规则解释或发现口径；
- `schemas/process-workpaper.schema.json`、`schemas/report-input.schema.json`：过程底稿与报告安全输入契约。

详细安全/保留策略见 `references/security-and-retention.md`；安装只看 `INSTALL.md` 和预检结果。
