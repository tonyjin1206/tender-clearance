# 验证记录（VALIDATION）

## 2026-09-16：v0.3.0 OCR 进度可观测性与 Provider 优化

- OCR Provider 默认使用质量优先的 `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`，规整
  A4 页面默认关闭方向、展平和文本行方向预处理，并保留 PaddleOCR 文本块坐标。
- 新增 `TC_PROGRESS_V1` 模型过程事件：首次安装每 10 秒发送心跳，OCR 每完成一页发送
  页级事件；事件默认可折叠，不进入报告、证据或业务输出，不含凭据和 OCR 原文。
- 真实商务扫描件 19 页复测：19 页均取得文本块坐标，完整质量档耗时 262.4 秒；关键页
  正确识别供应商名称、项目编号、投标日期、报价 26.2 和税率 6%。
- 回归验证：121 passed；`compileall` 和 `git diff --check` 通过。

验证日期：2026-09-11　环境：macOS / Python 3.12.13 / uv venv

## 2026-09-15：首轮确认与性能可观测性

- 新增 `preflight.py --intake`：不读取 `project.yaml` 或业务文件，先输出评标截止时间、
  公开联网授权、SRM 授权/运行时凭据和敏感信息展示的四项确认。
- 新增 `run_pipeline.py`：每阶段在控制台输出墙钟耗时，并写入
  `output/interim/performance.json`；实时查询另写 `query-timings.json`，记录供应商 ID、
  渠道、状态和耗时，不记录账号、密码、Cookie 或查询页面内容。
- 实时查询默认总预算为 300 秒。预算耗尽或进程超时会停止流水线；未开始项保留
  `not_queried`，正式 SRM 报告门禁仍会阻止把它写成无风险。
- SRM 正式默认每家供应商使用独立浏览器会话，避免同一门户切换企业时残留画像 iframe；
  `SRM_BROWSER_REUSE_SESSION=1` 仅作为性能实验开关。验证码人工接管默认立即返回 `manual`，
  不再无提示等待 180 秒；`query_sources.py --supplier-id SUP-*` 支持只重试失败主体。

干净 Core 虚拟环境（新建 venv、仅安装 `requirements-core.txt`）与无 `output/` 的虚构项目副本：

| 检查 | 结果 |
|---|---|
| `preflight.py --intake` | 0.12s |
| `run_pipeline.py --offline-draft` | **1.00s**（盘点 0.193s、一次性解析 0.198s、匹配 0.110s、外部导入 0.099s、规则 0.110s、报告 0.290s） |
| 完整测试 | **101 passed**，5 个第三方依赖弃用警告 |
| Skill 结构 | `quick_validate.py` 通过 |
| 锁文件 | `uv lock --check` 通过 |

上述是小型虚构夹具的本机结果，文件系统可能已热缓存；不把它宣称为真实标书或实时 SRM 的
生产 SLO。实时网站与 OCR Provider 未在本次性能回归中连接。

## 2026-09-15：真实附件平铺上传验证

在独立临时项目中将 `/Users/moc/Desktop/投标文件/` 的 9 份 PDF 平铺放入
`bids/inbox/`，不建立供应商目录，不把文件名作为归组依据。验证结果：

- 盘点识别为 3 份商务、3 份技术、3 份投标一览/报价；内容提取生成 30 个页级 OCR 任务，
  包含纯扫描页以及“文字层 + 营业执照图片”页。
- 本机 macOS Vision OCR 导入后，自动证据识别出 3 家供应商；技术标按商务主体回归归组，
  不把招标人“富奥汽车零部件股份有限公司”建成供应商。
- 用户确认的三组三件套通过 `supplier-group-confirmations.json` 显式记录；`报价.pdf`
  的文字层与第三组归属冲突被保留为人工复核项，没有静默合并。
- 本地阶段性能：完整离线编排 **1.383 秒**；其中盘点 0.194 秒、一次性解析 0.152 秒、
  归组 0.103 秒、主体匹配 0.511 秒、规则 0.109 秒、报告渲染 0.209 秒。
- 公开查询 3 家均返回 `no_match_verified`；一次完整成功的 SRM 查询耗时
  **155.201 秒**：山清 `match` 28 条、鑫誉 `match` 55 条、赢天 `match` 36 条。
  随后只定向重试山清和鑫誉耗时 102.995 秒，结果去重后仍为 28/55/36 条；最终正式门禁
  `validate_project.py --stage final` 通过。

## 本次 V2 改造

已按《投标清标Skill开发方案.md》将 Core 与 OCR、外部刷新、导出运行时拆开：

- Core `pyproject.toml` 只声明本地解析、规则和报告所需依赖；DOCX/Excel、live、
  SRM 浏览器依赖改为 extras；`requirements-core.txt`、`uv.lock` 和 Windows
  `INSTALL.md` 已加入。
- Core 依赖清理：确认代码未使用 `pypdf`，已从基础依赖与锁文件移除，PDF 统一使用
  `pymupdf`。
- `parsers.py` 不再导入或运行 PaddleOCR；`tc/ocr_paddle.py` 仅作为迁移期废弃兼容层，
  Core 不调用它。
- 新增 `ocr.capabilities.v1`、`ocr-job.v1`、`ocr-result.v1` 模型和 Schema；
  `extract_documents.py` 生成任务，`import_ocr_results.py` 严格校验任务 ID、文档 ID、
  文件哈希、页号、契约版本和处理位置，错误写入 `ocr_result_invalid`，不生成字段。
- 扫描页 OCR 使用宿主结果；有坐标按空间邻近配对，`page_only` 仅产生低置信度候选。
  生产 OCR Provider 不进入 Core；PaddleOCR/ONNX Runtime 仅作为独立 Provider Profile。
- 文档、OCR 和外部资料分别写入 `output/cache/`；报告档位为 `report/review/workpaper`，
  根目录保留验收基准，档位副本写入 `output/exports/<profile>/`。
- `preflight.py` 只读汇总文件分类、平铺归组策略、缺失依赖、OCR/SRM 确认项；阶段
  运行不再中途提问。技术标正文/表格/媒体按文件类型短路，仅保留首页可见文本证据；扫描页不发起 OCR；
  商务页中的证照图片仍生成页级 OCR 任务。
- 文档缓存按源 SHA 复用解析快照，但阶段结果同时校验文档身份、分类、脱敏/Provider
  配置；同内容副本不会串用证据路径。
- 默认 `run_all()` 不调用 `query_sources.py`；外部刷新必须显式执行，报告重跑只消费缓存。
- SRM 浏览器会话适配器保留，仍使用宿主浏览器能力和运行时凭据，不改为 Core HTTP 客户端。

## 静态与单元验证

| 检查 | 命令 | 结果 |
|---|---|---|
| Skill frontmatter/结构 | `quick_validate.py .` | ✅ Skill is valid! |
| Schema 生成 | `.venv/bin/python scripts/gen_schemas.py` | ✅ 13 个 Schema 生成 |
| 单元与场景测试 | `.venv/bin/python -m pytest tests -q` | ✅ **101 passed**，5 个环境弃用警告 |
| 运行前预检 | `.venv/bin/python scripts/preflight.py tests/fixtures/project-alpha --profile report` | ✅ 只读输出 23 个输入文件；技术标正文短路并保留归组边界；无缺失 Core 依赖 |
| 锁文件一致性 | `uv lock --check` | ✅ 41 个包，已移除未使用的 `pypdf` |
| 干净虚构夹具完整流水线 | `run_all(require_srm=False)` + `/usr/bin/time -p` | ✅ 冷运行 1.15s、热运行 0.99s；仅代表当前 macOS 夹具，不宣称生产 p95 |
| Core 依赖静态检查 | 检查基础 dependencies / `requirements-core.txt` | ✅ 无 `paddle*`、OpenCV、Playwright、requests |
| Core OCR 调用检查 | `parsers.py`、`extract_content.py`、`pipeline.py` | ✅ 无 Paddle/模型调用；仅 mock 夹具可读 XMP |
| 有效项目验证 | `validate_project.py <alpha副本> --stage final` | ✅ exit 0 |

## 业务场景

既有 T01–T12 继续通过：主体不误合并、唯一标识分级、外部状态显性化、脱敏、
损坏文件容错、技术标/一览表字段边界和重复运行确定性。

新增 OCR 场景：

| 编号 | 场景 | 断言 | 测试 |
|---|---|---|---|
| T13 | 无文本层扫描页 | 只生成 `ocr-job.v1`，不调用 OCR 引擎 | `test_ocr_contract.py` |
| T14 | 哈希/任务身份不一致 | 整批拒绝，写 `ocr_result_invalid`，不生成字段 | `test_ocr_contract.py` |
| T15 | 合法 Provider 结果 | 记录 Provider、模型、引擎、位置，并重建字段 | `test_ocr_contract.py` |
| T16 | `page_only` 无坐标 | 候选置信度被压低，不参与精确匹配 | `test_ocr_contract.py` |
| T17 | Provider 能力不完整 | 能力声明返回拒绝原因，不安装依赖 | `test_ocr_contract.py` |
| T18 | 相同输入重复运行 | 文档缓存和 OCR 结果可复用，结构化输出稳定 | 既有重复运行测试 + 缓存路径检查 |

SRM 报告门禁、正式查询状态和报告章节/导出回归也随本次测试集通过；离线夹具仅通过
`--skip-srm-gate` 验证 Core 输出，不代表真实 SRM 查询已完成。

## 离线黑盒试跑

使用虚构 `tests/fixtures/project-alpha` 的干净临时副本运行；实际调用方式由测试夹具的
`run_all()` 编排，默认不联网、不调用宿主 OCR、不执行外部查询。报告输出包含 JSON、
Markdown、PDF、证据索引和人工复核清单；扫描页无 Provider 结果时为 `ocr_unavailable`
并进入人工复核，不转写为“无风险”。

## 尚未宣称通过的项目验收

- Windows 11 Core 安装耗时、Provider 安装耗时和包/模型体积：`[待确认]`，当前未在
  Windows 样机实测。
- PP-OCRv4/v5 真实扫描商务标召回率、字符准确率、冷启动和每页耗时：`[待确认]`，
  当前只验证 Core 契约和虚构 Provider 结果导入。
- “标准报告重跑 p95 ≤60 秒”：`[待确认]`，尚未冻结 `tests/perf/baseline.yaml`；
  不对完整清标耗时作宣传。
