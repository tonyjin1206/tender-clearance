# tender-clearance（投标清标 Skill）

接收同一采购项目的多家供应商标书及经授权取得的企业风险资料，完成标书目录识别、
信息提取与来源定位、主体交叉检查、文件属性与扫描线索比对、公开/授权外部证据
查询或导入，形成**可复核、可追溯、按三级预警展示**的清标报告。

当前业务口径：公共信息仅从封面第一页提取；公司名称、社会信用代码、法人和授权代表
证件字段仅从商务标提取；技术标和一览表只保留分类、首页可见文本和归组证据，不发起 OCR，正文不做
主体字段提取。
报告另设 SRM 工商/股东/分支机构/主要人员章节以及政府采购网截图证据章节。未取得、未查询、
查询受阻和页面无结构化结果严格分开，任何缺口不得填充为“无风险”。

> **边界**：输出的是风险线索与证据整理结果，不是串通投标、违法失信或资格不合格的
> 最终认定。I 级预警与主体歧义项必须人工复核。

## 安装与环境

```bash
cd <发行包目录>
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -r requirements-core.txt
.venv/bin/python scripts/gen_schemas.py   # 可选：从 tc/models.py 重新生成 schemas/
```

DOCX、Excel、实时外部查询和 SRM 浏览器适配器分别安装 `.[review]`、`.[workpaper]`、
`.[live]`、`.[srm]`；生产 OCR 不属于本 Skill 安装包，按 `ocr.capabilities.v1` /
`ocr-result.v1` 契约由宿主 Agent 提供。Windows 安装见 `INSTALL.md`。

首次安装的长命令可由 `scripts/install_environment.py` 包装执行；它每 10 秒向编排模型
发出一条 `TC_PROGRESS_V1` 心跳，安装完成或失败时发终态事件。宿主界面应将这类事件作为
可折叠的模型过程信息展示，不作为普通用户答复。OCR Provider 每完成一页也发出一条同类
页级事件；事件只含阶段、页号、耗时、块数和状态，不含命令行、凭据或 OCR 原文。

报告渲染还会生成 `output/interim/process-workpaper.json` 和 `report-input.json`。过程底稿
保留字段级质量信息，报告输入不含 OCR 原文、文本块、坐标、模型详情或数值置信度；正式
报告要求全部 OCR 任务已有终态，缺页时需先补结果或提交明确失败/阻断状态。

## 上传后的首轮确认

文件一上传，先确认而不是先长时间执行：评标/投标截止时间、是否授权本次中国政府采购网
公开查询、是否授权 SRM 登录（凭据仅当次运行时提供）以及身份证号/手机号是否明文显示。
可先输出不读取业务文件的确认清单：

```bash
$PY scripts/preflight.py $P --intake --profile report
```

确认后才写入 `project.yaml` 的 `bid_deadline`、`external_query_mode`、
`external_query_sources` 与 `redaction_mode`；SRM 用户名和密码不得写入项目文件。

## 快速开始（离线证据导入模式）

```bash
P=<你的项目目录>          # 含 project.yaml / bids/inbox/ / procurement/ / external-evidence/
PY=.venv/bin/python

$PY scripts/preflight.py       $P --profile report
$PY scripts/run_pipeline.py    $P --profile report --offline-draft
```

`--offline-draft` 只用于本地/导入证据的预览，不会通过 SRM 正式报告门禁。取得本次 SRM
授权和凭据后，去掉该参数重跑，才能执行 `validate_project.py $P --stage final`。

`preflight.py` 只读检查文件名分类、平铺上传/归组策略、当前环境缺失依赖、OCR 和 SRM
授权要求。先把预检 JSON 展示给用户并一次性确认；确认后再统一安装缺失依赖和启动流水线，
后续阶段不临时提问、不临时安装。技术标正文按文件类型短路，扫描页也不进入 OCR；商务页
中的证照图片会生成页级 OCR 任务。平铺上传还必须完成 `resolve_supplier_groups.py`；该阶段
按正文/OCR 证据归组，不信任文件名、目录名或单份报价文件的孤立主体。内容与属性阶段复用
同一源文件的解析快照，但会校验文档身份和运行配置，防止同内容副本串用证据路径。

`run_pipeline.py` 每阶段输出耗时并实时转发长阶段进度，把汇总写到
`output/interim/performance.json`；实时查询还会生成 `query-timings.json`。默认实时查询总预算为
300 秒，超过后停止并保留真实的未查询/失败状态，不把等待一小时伪装成完成。补 OCR、人工归组
或阶段失败后使用 `--resume`，不要从头重跑；`--from-stage`/`--to-stage` 可做明确的局部运行。
live 查询默认复用同一运行、同一主体、同一适配器的成功结果，只有 `--refresh-queries` 才强制刷新。

宿主 OCR 完成 `output/interim/ocr-jobs.json` 对应任务后，显式执行
`$PY scripts/import_ocr_results.py $P --input <ocr-results.json>`，再重跑匹配、规则和报告。
平铺上传还需执行 `$PY scripts/resolve_supplier_groups.py $P`；若状态为
`needs_manual_review`，先补 OCR 或提供带理由的 `supplier-group-confirmations.json`，再继续。
正式报告必须先取得用户本次提供的 SRM 用户名和密码，登录后按公司名称 + 统一社会信用代码
查询；登录成功但没有匹配信息可以生成报告并标记 `no_result`，登录失败、受阻或未查询不能生成。
外部导入阶段是增量合并：同一运行中重跑不会清空已完成的 live 结果；新运行会重新建立当前运行的查询边界。
个别供应商失败时可用 `query_sources.py $P --supplier-id SUP-2` 定向重试，避免重新查询
全部供应商；实时记录按 `record_id` 去重。

产出（`$P/output/`）：`清标报告.md` + **`清标报告.pdf`**（宋体排印，每章另起页）、
`清标结果.json`、`证据索引.csv`、`人工复核清单.csv`；`review` 档位增加 DOCX，
`workpaper` 档位增加 Excel，档位副本位于 `output/exports/<profile>/`。

## 渠道现状（P0–P5）

| 批次 | 状态 | 说明 |
|---|---|---|
| P0 骨架与契约 | ✅ | JSON Schema（由 pydantic 模型生成）、项目验证器、示例项目 |
| P1 本地标书清标 | ✅ | PDF/DOCX/XLSX 提取、字段证据定位、元数据、交叉匹配、规则引擎 |
| P2 报告与审阅 | ✅ | Markdown/PDF/DOCX、证据索引、复核清单、脱敏检查（含 PDF 文本层） |
| P3 外部证据导入 | ✅ | JSON/CSV/快照导入、查询状态、覆盖矩阵 |
| P4 合法外部查询适配器 | ✅ 政采网已实测启用 | 渠道范围（2026-09-09 决策）：外部仅保留中国政府采购网（公开查询表单自动化），信用中国/军采/工商/司法已移除；适配器 + 可注入传输层，状态机全覆盖 |
| P5 富奥 SRM 接入 | ✅ 查询入口已改造，待真实页面复核 | 登录→主页“查企业”→搜索框唯一命中→企业详情/画像→身份核对→三类页面结构化提取；凭据仅运行时内存；默认无头（用户只对话），`SRM_BROWSER_HEADED=1` 为验证码人工接管；驱动 `scripts/tc/srm_playwright_driver.py`，自测 `scripts/srm_browser_selftest.py` |

渠道范围（2026-09-09 决策）：外部仅保留**中国政府采购网**（失信名单查询已自动化，
不需要账号密码），内部仅保留**富奥 SRM**；其余渠道已移除
（`references/external-sources.md`）。

## 源码验证

```bash
python -m pytest tests -q                  # 仅适用于包含 tests/ 的源码仓库
python tests/fixtures/make_fixtures.py     # 仅适用于源码仓库
python <quick_validate.py> .
```

源码测试夹具（`tests/fixtures/project-alpha`）全部为程序生成的虚构企业/人员/证件号；
禁止将真实投标文件作为测试夹具。精简发行包不包含 `tests/` 和测试夹具。

## 目录结构

见 SKILL.md 与 `references/`。核心契约：`schemas/*.schema.json`（由
`scripts/tc/models.py` 生成）；规则：`rules/risk-rules.yaml`（版本化）；
渠道：`rules/external-sources.yaml`。

## 待确认事项（方案 §11）

1. 富奥 SRM 实时查询 —— 查询入口已改为无头后台登录→主页“查企业”→搜索框唯一命中→
   企业详情/画像→三类页面结构化提取；本次代码改造后需在真实登录会话中复核页面选择器；
2. ~~公开渠道在部署环境的可用查询方式~~ —— 2026-09-09 决策：外部仅保留中国政府采购网，
   内部仅保留富奥 SRM；信用中国/军采/工商/司法渠道已移除；
3. 本项目采购文件的资格/否决条款映射与时间截点 —— 已提供草案工具：
   `scripts/draft_procurement_clauses.py` 生成候选条款 → 人工确认 → 命中发现自动附条款引用；
4. 图像相似度、电子签章等进阶能力（须另行定义误报率后加入）；
5. ~~是否需要 Excel 工作底稿~~ —— 已提供 `scripts/render_worksheet.py`（9 张工作表：
   说明/供应商对照/比对矩阵/风险发现/查询覆盖/外部记录/证据索引/文件清单/人工复核）。
6. OCR 生产服务不嵌入 Core。质量优先时，推荐宿主 Provider Profile 使用 PaddleOCR
   `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`，关闭规整 A4 页面不需要的方向/展平
   预处理，并返回文本块坐标；宿主先声明 `ocr.capabilities.v1`，再将 `ocr-result.v1`
   结果交给 `import_ocr_results.py`。`mobile` 模型只能作为经过真实样本验收的快速档，
   不得仅因速度直接替换质量档。无 Provider 时扫描页保持 `ocr_unavailable` 并进入人工复核。

7. 报告身份证号展示由 `project.yaml` 的 `redaction_mode` 控制；当前业务授权使用
   `none` 时，法人代表和授权代表身份证号直接展示完整号码。SRM 密码、Cookie、令牌始终不输出。
