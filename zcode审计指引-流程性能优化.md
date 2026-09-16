# tender-clearance 流程性能优化审计指引

## 审计目标

确认本次优化能把“补 OCR、人工归组、单供应商查询失败后的继续执行”从全量重跑改为增量续跑，并且没有放宽 SRM 门禁、主体确认、证据归属或敏感信息边界。

验收重点不是代码行数，而是：

1. 已完成阶段不会被 `--resume` 重复执行；
2. 同一运行中重跑外部导入不会清空已完成的 live 结果；
3. live 查询只复用同一主体、同一适配器产生的成功结果；
4. 失败/阻断/待人工确认/导入结果不会被误当成成功缓存；
5. 局部规则与报告重跑不要求无关的 SRM 凭据；
6. 状态、证据强度和人工复核边界保持原有含义。

## 日志基线

原始 zcode 会话标识：`sess_778cb8b5-cb12-4fd7-9509-430f60d976f1`。

对应模型 I/O 日志：`/Users/moc/.zcode/cli/rollout/model-io-sess_778cb8b5-cb12-4fd7-9509-430f60d976f1.jsonl`。

- 108 次模型请求；75 次 Bash、32 次 Read、4 次 Edit、3 次 Write；
- 累计 `input_tokens=11,876,517`、`output_tokens=41,325`，缓存读取约 11,556,096；
- 模型请求墙钟累计约 27.8 分钟，首个请求到最终交付的日志时间跨度约 40 分钟；
- 主要浪费：安装/路径反复试错、参考文档重复加载、先完整流水线后补 OCR、逐页人工读取、`sleep` 轮询、补查询后再次从头跑；
- 关键设计缺陷：`import_external_evidence.py` 重写 `external.json`，随后 live 查询默认刷新所有渠道，导致已有查询结果被丢弃或重复访问。

日志中包含真实凭据操作痕迹；审计时不得回显、复制或写入任何凭据。

## 本次改动范围

### 1. 编排器

文件：`skills/tender-clearance/scripts/tc/pipeline.py`、`skills/tender-clearance/scripts/run_pipeline.py`

- 新增 `--resume`：根据 `output/interim/performance.json` 从首个未成功阶段继续；
- 新增 `--from-stage` / `--to-stage`：接受脚本名或阶段名，执行连续的明确区间；
- `--resume` 与 `--from-stage` 互斥；无待执行阶段时明确输出跳过原因；
- 长阶段由主入口直接转发阶段输出，编排器不需要 `sleep` 轮询；
- 只有本次选中 `query_sources.py` 时才要求 SRM 凭据；规则/渲染局部续跑不应被无关凭据阻断；
- `performance.json` 新增 `selected_stages`，保留已有阶段耗时和失败状态。

### 2. 外部查询复用

文件：`skills/tender-clearance/scripts/query_sources.py`

- 默认仅复用同一运行中、同一主体、同一 live 适配器产生的 `match`、`no_match_verified`、`no_result`；
- 导入记录、`failed`、`blocked`、`needs_manual_review`、`not_queried` 不复用；
- 主体统一社会信用代码不一致时，即使名称相同也不得复用；
- `--refresh` 强制刷新选中的 live 渠道；`--supplier-id` 保持定向重试；
- `query-timings.json` 记录 `reused`、强制刷新和本次替换键数量；
- 替换查询时清理对应旧证据和记录，复用查询时保留记录，不重复累加。

### 3. 外部证据增量导入

文件：`skills/tender-clearance/scripts/import_external_evidence.py`

- 同一运行、无新导入文件时保留已有 live 查询、记录、股权关系和外部证据；
- 同一运行有新导入文件时只替换对应渠道的旧 `manual_import` 证据；
- 新运行不会继承上一运行的查询结果，避免把历史 live 状态冒充为本次查询；
- 查询、记录、股权关系和证据均按稳定 ID 去重。

### 4. Skill 指令收缩

文件：`skills/tender-clearance/SKILL.md`、`skills/tender-clearance/README.md`、`skills/tender-clearance/references/workflow.md`

- `SKILL.md` 从长篇重复说明改为交互状态、核心边界、标准路由、输出契约和按需参考资料；
- 明确“首轮一次性确认、主入口一次执行、补材料使用 `--resume`、不要逐页人工 OCR、不要 `sleep`”；
- 明确报告阶段不联网、不安装、不触发 OCR；
- 详细契约改为按分支读取，避免每次调用把全部 references 加入上下文。

### 5. 版本一致性

文件：`skills/tender-clearance/pyproject.toml`、`skills/tender-clearance/uv.lock`、`skills/tender-clearance/scripts/tc/__init__.py`

版本统一到 `0.3.1`，避免发行元数据与运行时 `tool_version` 不一致。

## 建议审计顺序

先做静态审计，再做虚构夹具测试；不要把真实投标文件、真实 SRM 会话或真实凭据用于回归。

1. 检查 `run_all()` 的阶段构造顺序：先选择阶段区间，再决定是否要求 SRM 凭据；
2. 检查 `--resume` 在 `resolve_supplier_groups` 失败、`query_sources` 超时和完整成功三种 `performance.json` 下的选择结果；
3. 检查同一 `run_id` 与不同 `run_id` 的外部导入合并边界；
4. 检查 `_is_reusable_query()` 的主体代码、主体名称、渠道和状态条件；
5. 检查旧记录清理不会删除未选供应商或未替换渠道的记录；
6. 检查 `--refresh-queries` 只给查询阶段传递 `--refresh`；
7. 检查输出没有新增密码、Cookie、令牌、完整身份证号或手机号泄露路径；
8. 检查原有 SRM 门禁和“主体未确认最高 III 级”的规则没有被改弱。

## 可复现验证

```bash
cd /Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance
/Users/moc/.local/bin/uv run --offline pytest tests/test_performance.py tests/test_external_evidence.py tests/test_ocr_contract.py tests/test_supplier_grouping.py -q
/Users/moc/.local/bin/uv run --offline python scripts/run_pipeline.py --help
/Users/moc/.local/bin/uv run --offline python -m compileall -q scripts
```

当前已验证：上述定向测试 `40 passed`；排除 4 个 SRM/浏览器测试后的更宽回归为 `90 passed`；编译检查和 `git diff --check` 通过。全量测试未作为通过依据：执行到 41 项后进入需要浏览器/外部环境的测试并持续等待，已中止，需在具备相应隔离环境时单独审计。

## 审计结论格式

请按以下字段返回，不要只给“通过/不通过”：

```text
结论：PASS / FAIL / NEEDS_REVIEW
P0/P1/P2 问题：文件、行号、复现条件、实际影响
续跑行为：resume / from-stage / to-stage
查询复用：主体、渠道、状态、refresh 边界
数据安全：凭据、敏感字段、旧记录清理
验证命令与结果：
未验证项：
```
