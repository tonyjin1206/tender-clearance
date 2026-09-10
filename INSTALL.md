# 投标清标 Skill 生产安装包

需要 Python 3.12+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

扫描件 OCR：`.venv/bin/python -m pip install -e "[ocr]"`。

项目目录需包含 `project.yaml`、`bids/`，可选 `procurement/` 与 `external-evidence/`。

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

结果写入项目目录 `output/`。未查询、阻断、失败和无结构化结果会保留原状态。
