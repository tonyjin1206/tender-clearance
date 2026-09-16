# tender-clearance v0.3.1 性能优化审计报告

审计日期：2026-09-16
审计对象：Codex 本次改动（编排器 --resume/--from-stage、查询复用、导入增量合并、SKILL 收缩、版本 0.3.1）
审计方式：静态审计 + 虚构夹具实验复现（未使用真实标书、真实 SRM 会话或真实凭据）

## 结论：FAIL（问题集中在续跑/重试路径，均为新功能核心场景；修复面小而明确）

主体方向正确：编排器/复用/导入合并的主路径逻辑与 SKILL 收缩质量良好，105 项离线回归全部通过，SRM 门禁与等级上限未被弱化。但以下 4 个 P1 在"失败→修复→续跑"这一功能的目标场景下造成数据破坏或语义失真，须修复后复验。

## P1 问题（均已实验复现）

### P1-1 performance.json 覆盖写 → 二次失败后 --resume 退化为全量重跑
- 文件：`scripts/tc/pipeline.py` `_write_performance_report()`（约 L113-131）
- 复现：run1 在阶段3失败 → performance.json 含阶段1-3；`--resume` 续跑到阶段5再失败 → performance.json 只剩阶段3-5；再次 `--resume` 时阶段1/2 状态丢失，从 inventory 全量重跑（含 OCR 等最贵阶段）。
- 影响：违反验收标准 1（已完成阶段不得被 --resume 重复执行）；正是"多次修复续跑"这一目标场景。
- 修复建议：`_write_performance_report` 按 stage 名合并旧记录（同阶段保留本次最新状态，未涉及的阶段保留原状）。

### P1-2 查询复用路径证据链断裂（悬空证据引用 + 强度失真）
- 文件：`scripts/query_sources.py` 主流程（EvidenceBuilder 全新实例 + L290-293 无条件覆盖 `evidence-external-queries.json`）
- 复现（虚构夹具，fake 适配器）：同一 run 内第一次查询 match（1 条记录、1 条证据 EV-xxx）；第二次执行 query_sources 触发复用 → 复用的 query 与 record 仍引用 EV-xxx，但证据文件被覆盖为空（0 条）。
- 影响：违反验收标准 6；`assess_risk` 的 `min_strength` 对缺失证据按 default="A" 兜底 → 复用结果的证据强度被**虚增**为 A；`证据索引.csv` 丢行；审计追溯断链。
- 修复建议：与 import_external_evidence 的做法对齐——builder 输出时合并 `prior_evidence`（同 run、未被替换的保留），或复用分支把旧证据条目原样带回。

### P1-3 查询预算耗尽把既有 blocked/failed 降级为 not_queried 并删除其记录
- 文件：`scripts/query_sources.py` L193-196（refreshed_keys/replaced_evidence_ids 在预算判断之前标记）+ L215-219（占位无条件覆盖）
- 复现（虚构夹具，2 供应商，--max-total-seconds 1）：SUP-2 的既有 blocked 查询（含 1 条记录）被 not_queried 占位覆盖，记录被删（总数 2 → 0）。
- 影响：blocked（需人工处理）信息量高于 not_queried（未尝试），降级丢失"已尝试但受阻"语义；数据删除不可逆。违反验收标准 4 的反向情形（不滥用，但被销毁）。
- 修复建议：refreshed_keys/replaced_evidence 标记移入真正执行查询的分支；循环内占位赋值尊重 STATUS_PRECEDENCE（新状态信息量 ≥ 旧状态才覆盖）。

### P1-4 复用判定缺少 run_id 校验 → 跳过导入阶段时旧 run 结果被冒充为本次
- 文件：`scripts/query_sources.py` `_is_reusable_query()`（无 run_id 比较）
- 复现（虚构夹具）：inventory 重跑产生 run-2，external.json 仍为 run-1 的 no_match_verified；`run_pipeline --from-stage query_sources`（跳过 import_external_evidence）→ 旧结果 reused=1，merged.run=run-2，SRM 报告门禁将其视为"本次已认证查询"放行。
- 影响：违反验收标准 3 的"同一运行"约束；标书更新后旧查询结果可能被静默沿用。SKILL 路由 4 明确鼓励直接调用 query_sources，可达性真实存在。
- 修复建议：`_is_reusable_query` 增加 `ext.run.run_id == inventory.run.run_id` 前置判断（两个对象均已加载，零成本）；顺带校验 `adapter_version` 与当前适配器一致。

## P2 问题

1. **stream_output=True 时阶段失败丢失错误尾巴**（`pipeline.py` L161-167）：失败消息里的 `output[-2000:]` 恒为空，编排器只报 exit code。建议：阶段输出始终落盘 `output/interim/logs/<stage>.log`，失败时打印尾部 20 行——同时是"诊断不进对话上下文"的 token 优化点。
2. **SRM 凭据在无需查询时仍被强制要求**（`query_sources.py` L148-153）：只要 srm 在 live_sources 就收集凭据，即使全部 SRM 结果可复用、或 `--supplier-id` 仅定向政采渠道。SKILL 路由 4 的定向重试会踩到。建议：延迟到首个不可复用的 SRM 查询执行前再收集。
3. **复用判定未校验 adapter_version**：同 run 内隐式安全，但与 P1-4 修复一并补上成本极低。

## 验证通过项（对应验收标准）

1. 单次失败后 `--resume` 正确从首个未成功阶段继续；`--from-stage/--to-stage` 区间选择正确；与 `--resume` 互斥；无待执行阶段输出跳过原因 ✓
2. 同 run 重跑导入保留已有 live 结果；新 run 不继承旧查询（run_id 比较在 import 侧正确）✓
3. `_is_reusable_query` 主体代码/名称、渠道模式、状态（仅 match/no_match_verified/no_result）判断正确；`--refresh`、`--supplier-id` 边界正确；旧记录清理不波及未选中供应商 ✓
4. 局部规则/渲染续跑不再要求 SRM 凭据（有测试锁定）✓
5. SRM 报告门禁未弱化（not_queried/blocked/failed/导入不能出正式报告；needs_manual_review 需 queried_at）✓；规则文件 EXT-001/COV-001 强制复核保留，等级上限未放宽 ✓
6. 无新增凭据/敏感字段泄露路径；performance.json、query-timings.json 不含参数与凭据 ✓

## 验证命令与结果

```text
uv run --offline pytest tests/test_performance.py tests/test_external_evidence.py tests/test_ocr_contract.py tests/test_supplier_grouping.py -q
→ 40 passed
pytest tests/ --ignore=tests/test_srm_browser.py --ignore=tests/test_srm_playwright_driver.py --ignore=tests/test_srm_gate.py -q
→ 105 passed（比 Codex 报告的 90 多：本机可离线跑 srm_gate 测试）
python -m compileall -q scripts → OK
git diff --check → OK
run_pipeline.py --help → 新参数齐全
```

## 未验证项

- 需要真实浏览器/SRM 会话的 4 类测试（test_srm_browser / test_srm_playwright_driver / test_srm_gate 中依赖外部环境的用例）；
- 真实标书与真实 SRM 的端到端（按指引约束未使用）；
- P1 修复后的回归（待修复后复验）。

## 独立视角：本轮之外的效率优化建议（按收益排序，未实施）

1. **OCR 内容寻址缓存**：OCR 是最大单阶段墙钟（真实语料 19 页约 262s）。按（PDF sha256 + 页码 + 渲染 DPI + 引擎与模型版本 + 参数）缓存页级结果到 `output/cache/ocr/`，重跑/续跑只 OCR 增量页。这是"补材料后继续"路径的最大剩余成本。
2. **阶段日志落盘（同 P2-1 修复）**：所有阶段输出写 `output/interim/logs/`，编排器只打一行状态——agent 排障用 grep 读日志片段，不再把整段输出/整页图片读进对话（上一轮 11.9M 输入 token 的主要来源之一）。
3. **查询预算分层**：全局 300s 之外增加单供应商预算（如 90s）与单查询超时，避免一家供应商（如验证码等待）吃光预算把其他家挤成 not_queried——这正是 P1-3 现实触发来源。
4. **resume 的凭据预检**：`--resume` 即将执行 query_sources 且存在不可复用查询时，在启动前一次性提示需要 SRM_USER/SRM_PASSWORD（当前是阶段中途报错）。
5. **（暂缓）SRM 会话跨进程恢复**：resume 时免二次登录，涉及 Cookie 生命周期设计，待上述项稳定后另行评估。
