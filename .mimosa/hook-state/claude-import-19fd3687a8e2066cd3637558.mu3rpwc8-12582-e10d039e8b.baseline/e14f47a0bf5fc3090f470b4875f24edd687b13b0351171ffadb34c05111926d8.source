"""采购条款映射：草案提取 + 评估注释集成。"""

from __future__ import annotations

import json

import pytest
import yaml


def _make_procurement_docx(project: "Path") -> "Path":
    import docx

    pdir = project / "procurement"
    pdir.mkdir(exist_ok=True)
    path = pdir / "资格条款.docx"
    d = docx.Document()
    d.add_paragraph("本项目资格审查：投标人被列入政府采购严重违法失信行为记录名单的，"
                    "其投标将被拒绝，并在一年内禁止参加本项目采购活动。")
    d.add_paragraph("投标人在开标前应当按招标文件要求缴纳保证金。")
    d.save(str(path))
    return path


def test_draft_procurement_clauses_extracts_candidates(alpha_project, tmp_path):
    import shutil

    from tc.pipeline import run_stage

    project = tmp_path / "p"
    shutil.copytree(alpha_project, project)
    doc = _make_procurement_docx(project)
    run_stage("draft_procurement_clauses.py", [], project)
    draft = yaml.safe_load((project / "采购条款映射草案.yaml").read_text(encoding="utf-8"))
    assert draft["draft"] is True
    assert draft["rule_mapping"] == {}
    hits = [c for c in draft["clauses"]
            if "严重违法失信" in c["quote"] and c["source_file"].endswith("资格条款.docx")]
    assert hits, "应从测试 docx 提取到含失信关键词的资格条款"
    assert hits[0]["confirmed"] is False
    assert "DIS-003" in hits[0]["suggested_rules"]
    assert hits[0]["location"]["kind"] == "docx_body"


def test_assess_annotates_confirmed_clause_mapping(alpha_project, tmp_path):
    """确认映射后，DIS-003 发现自动附条款引用；等级不变（注释性）。"""
    import shutil

    from tc.pipeline import run_stage

    project = tmp_path / "p"
    shutil.copytree(alpha_project, project)
    alpha_project = project
    mapping = {
        "clauses": [{"id": "CL-001", "quote": "投标人被列入政府采购严重违法失信行为记录名单的，其投标将被拒绝",
                     "confirmed": True}],
        "rule_mapping": {"DIS-003": ["CL-001"]},
    }
    map_path = alpha_project / "采购条款映射.yaml"
    map_path.write_text(yaml.safe_dump(mapping, allow_unicode=True), encoding="utf-8")

    cfg_path = alpha_project / "project.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    if "procurement_clauses_mapping" not in text:
        text += "\nprocurement_clauses_mapping: 采购条款映射.yaml\n"
    cfg_path.write_text(text, encoding="utf-8")

    run_stage("assess_risk.py", [], alpha_project)
    findings = json.loads((alpha_project / "output/interim/findings.json").read_text(encoding="utf-8"))
    dis = [f for f in findings["findings"] if f["rule_id"] == "DIS-003"
           and f.get("supplier_ids")]
    assert dis, "应存在已归属的 DIS-003 发现"
    assert any("对应采购条款" in (f.get("procurement_clause_note") or "") for f in dis), \
        "确认映射后应附条款引用"
    # 等级只由规则决定：II 不变
    assert all(f["level"] == "II" for f in dis)
