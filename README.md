# 投标清标 Skill

用于多家投标文件的目录识别、证据定位、主体交叉检查、文件属性比对、外部证据导入和三级预警报告生成。

输出是风险线索与证据整理结果，不是串通投标、违法失信或资格不合格的最终认定。I 级预警与主体歧义项必须人工复核。

## 安装

需要 Python 3.12+：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

扫描件 OCR：

```bash
.venv/bin/python -m pip install -e '.[ocr]'
```

## 运行

项目目录需包含 `project.yaml`、`bids/`，可选 `procurement/` 与 `external-evidence/`。按顺序运行：

```bash
PY=.venv/bin/python
$PY scripts/inventory.py <项目目录>
$PY scripts/extract_content.py <项目目录>
$PY scripts/extract_metadata.py <项目目录>
$PY scripts/normalize_and_match.py <项目目录>
$PY scripts/import_external_evidence.py <项目目录>
$PY scripts/query_sources.py <项目目录>
$PY scripts/assess_risk.py <项目目录>
$PY scripts/render_report.py <项目目录>
$PY scripts/render_worksheet.py <项目目录>
$PY scripts/validate_project.py <项目目录> --stage final
```

结果写入项目目录的 `output/`，包括 Markdown/PDF 报告、JSON 验收基准、证据索引、人工复核清单和 Excel 工作底稿。

外部渠道仅保留中国政府采购网和富奥 SRM。未查询、阻断、失败和页面无结构化结果会保留原状态，不会被改写为“无风险”。
