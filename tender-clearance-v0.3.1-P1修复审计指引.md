# tender-clearance v0.3.1 P1 修复审计指引

## 审计目标

复核上一轮审计报告中 4 个 P1 是否已修复，并确认修复没有放宽 SRM 门禁、主体确认、证据归属或敏感信息边界。

审计对象：

- `/Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance/scripts/tc/pipeline.py`
- `/Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance/scripts/query_sources.py`
- `/Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance/tests/test_performance.py`
- `/Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance/tests/test_external_evidence.py`

上一轮报告：`/Users/moc/tender-clearance-v0.3.1-性能优化审计.md`。

## 修复内容

### P1-1：performance.json 不再覆盖历史阶段

`pipeline._write_performance_report()` 现在先读取已有 `performance.json`，按 `stage` 合并：

- 本次实际执行的阶段覆盖同名旧记录；
- 本次未执行的阶段保留旧记录；
- 阶段顺序保持首次出现顺序；
- 当前运行的 `status`、`failure_type`、`selected_stages` 仍单独更新。

验收：第一次在阶段 3 失败，续跑在阶段 5 失败，再执行 `--resume` 时，阶段 1、2、3、4 的成功状态不能丢失；不得重新执行已成功的 OCR/解析阶段。

### P1-2：查询复用不再产生悬空证据 ID

`query_sources.py` 在同一 `run_id` 下加载已有 `evidence-external-queries.json`，写回时：

- 保留未被真正刷新替换的旧证据；
- 删除被实际刷新查询替换的旧证据；
- 合并本次 `EvidenceBuilder` 新产生的证据；
- 复用查询、记录引用的每个 `evidence_id` 都必须能在证据文件中找到。

验收：第一次 live 查询产生 1 条查询证据，第二次同 run 复用后，证据文件不能变成空文件；`assess_risk` 不得因缺证据而使用 `default="A"` 兜底虚增强度。

### P1-3：预算耗尽不再销毁既有 blocked/failed 结果

`refreshed_keys` 和 `replaced_evidence_ids` 只在真正调用适配器的分支标记。预算已耗尽时：

- 同一 run 已有查询结果：原查询、原记录、原证据保持不变；
- 没有历史查询：才新增 `not_queried` 占位；
- 不得把 `blocked`、`failed` 或已有命中结果降级成 `not_queried`；
- 不得删除未启动查询对应的记录。

额外边界：如果 `external.json` 属于旧 run，即使预算耗尽也不能把旧 live 结果冒充当前 run，必须生成当前 run 的 `not_queried` 占位或执行实际查询。

### P1-4：查询复用增加 run_id 边界

`_is_reusable_query()` 增加 `same_run` 前置条件，调用点使用：

```python
same_run = ext.run.run_id == inventory.run.run_id
```

只有 `same_run=True` 才允许复用；`--from-stage query_sources` 跳过导入阶段时，如果 inventory 已产生新 run_id，旧 run 的 `match` / `no_match_verified` / `no_result` 必须重新查询，不能计入 `reused_count`，也不能让 SRM 报告门禁把旧结果视为本次认证查询。

## 建议审计顺序

1. 静态检查 `pipeline.py` 的阶段记录合并是否以阶段名为键，确认失败续跑不会丢失旧阶段。
2. 静态检查 `query_sources.py` 的三条边界：`same_run`、证据合并、预算分支刷新标记位置。
3. 检查复用分支没有调用适配器、没有删除记录、没有创建新证据。
4. 检查真实适配器分支才会清理同键旧记录与旧证据。
5. 检查跨 run 场景不会通过复用路径进入 SRM 成功门禁。
6. 检查没有新增凭据、Cookie、完整身份证号或手机号写入日志/计时文件的路径。

## 可复现验证

```bash
cd /Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance
/Users/moc/.local/bin/uv run --offline pytest tests/test_performance.py tests/test_external_evidence.py tests/test_ocr_contract.py tests/test_supplier_grouping.py -q
/Users/moc/.local/bin/uv run --offline pytest -q --ignore=tests/test_srm_browser.py --ignore=tests/test_srm_playwright_driver.py
/Users/moc/.local/bin/uv run --offline python -m compileall -q scripts tests
cd /Users/moc/workspace/Tender_Evaluation_Report
git diff --check
```

本次已验证：定向回归 `41 passed`；排除两组真实浏览器驱动测试后的回归 `109 passed`；编译检查和 `git diff --check` 通过。

## 验收标准

```text
结论：PASS / FAIL / NEEDS_REVIEW
P1-1：多次失败续跑是否保留已成功阶段
P1-2：复用查询/记录的 evidence_ids 是否全部可解析，证据强度是否真实
P1-3：预算耗尽是否保留 blocked/failed/命中结果及其记录
P1-4：不同 run_id 是否禁止复用并阻止旧结果通过本次 SRM 门禁
门禁：SRM、主体确认、等级上限是否保持
验证命令与结果：
未验证项：
```

本轮只修复上述 4 个 P1。上一轮报告中的 P2（stream 失败错误尾巴、凭据延迟收集、`adapter_version` 校验）未作为本次修复范围，审计时请单独标记，不要将其误判为已完成。
