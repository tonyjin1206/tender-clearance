"""报告验收：T10 / T12 + 章节完整性与可追溯性。"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil

from pathlib import Path

import pytest

from tc.pipeline import run_all

_ID18 = re.compile(r"(?<![0-9A-Za-z])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")
_MOBILE = re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])")

OUTPUT_FILES = ["清标报告.md", "清标结果.json", "证据索引.csv", "人工复核清单.csv"]

SECTIONS = [
    "封面信息页",
    "基本信息页",
    "工商信息（富奥 SRM）",
    "股东信息（富奥 SRM）",
    "分支机构（富奥 SRM）",
    "主要人员（富奥 SRM）",
    "政府采购网失信信息及证据截图",
    "PDF、Office 与扫描件文件线索",
    "清标结论与三级预警",
    "证据与人工复核",
]


def test_report_outputs_exist(alpha_project):
    for name in OUTPUT_FILES:
        assert (alpha_project / "output" / name).exists(), name


def test_report_has_required_sections(alpha_project):
    text = (alpha_project / "output/清标报告.md").read_text(encoding="utf-8")
    for s in SECTIONS:
        assert s in text, f"缺少章节：{s}"
    # 结论边界声明存在（短语不跨行）
    assert "风险线索与证据整理结果" in text
    assert "人工复核后作出" in text


def test_t10_no_full_id_or_mobile_in_outputs(alpha_project):
    """T10：所有对外输出只含掩码/摘要，无完整身份证号/手机号。"""
    for name in OUTPUT_FILES:
        text = (alpha_project / "output" / name).read_text(encoding="utf-8")
        assert not _ID18.search(text), f"{name} 疑似包含完整身份证号"
        assert not _MOBILE.search(text), f"{name} 疑似包含完整手机号"
    # 掩码确实存在
    text = (alpha_project / "output/清标报告.md").read_text(encoding="utf-8")
    assert "****" in text


def test_findings_traceable_via_evidence_index(alpha_project):
    """9.3-4：任一发现可凭证据 ID 回到证据索引与原始文件定位。"""
    findings = json.loads((alpha_project / "output/interim/findings.json").read_text(encoding="utf-8"))
    idx_text = (alpha_project / "output/证据索引.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(idx_text)))
    by_id = {r["evidence_id"]: r for r in rows}
    checked = 0
    for f in findings["findings"]:
        for eid in f["evidence_ids"]:
            assert eid in by_id, f"{f['finding_id']} 的证据 {eid} 不在索引中"
            row = by_id[eid]
            assert row["location"] and row["source_type"]
            checked += 1
    assert checked > 0


def test_evidence_index_points_to_real_documents(alpha_project):
    idx_text = (alpha_project / "output/证据索引.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(idx_text)))
    doc_ids = {d["document_id"]: d["relative_path"]
               for d in json.loads((alpha_project / "output/interim/inventory.json").read_text(encoding="utf-8"))["documents"]}
    for r in rows:
        if r["document_path"]:
            assert r["document_path"] in doc_ids.values() or r["source_type"] == "external_evidence"


def test_review_list_contains_i_and_ambiguity(alpha_project, alpha_findings):
    text = (alpha_project / "output/人工复核清单.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    kinds = {r["类别"] for r in rows}
    assert "风险发现" in kinds
    assert "低置信度字段" in kinds  # T09 进入人工核对
    assert "读取失败文件" in kinds  # T11（密码/损坏/空文件）进入人工核对
    # I 级全部进入
    ids = {r["编号"] for r in rows if r["类别"] == "风险发现"}
    for f in alpha_findings["findings"]:
        if f["status"] == "human_review_required":
            assert f["finding_id"] in ids


def test_t12_two_runs_deterministic(alpha_project, tmp_path):
    """T12：相同输入与规则版本连续运行两次，结构化结果除运行标识/时间外逐字段一致。"""
    dst = tmp_path / "second-run"
    shutil.copytree(alpha_project, dst)
    shutil.rmtree(dst / "output")
    (dst / "output").mkdir()
    run_all(dst, require_srm=False)

    def scrub(o):
        """剥离运行标识与时间字段（T12 允许两者跨运行不同）。"""
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items() if k not in ("run", "collected_at")}
        if isinstance(o, list):
            return [scrub(x) for x in o]
        return o

    a = json.loads((alpha_project / "output/清标结果.json").read_text(encoding="utf-8"))
    b = json.loads((dst / "output/清标结果.json").read_text(encoding="utf-8"))
    assert scrub(a) == scrub(b)


def test_report_no_accusatory_language(alpha_project, alpha_findings):
    """弱线索场景不得被表述为违法/串标事实。"""
    text = (alpha_project / "output/清标报告.md").read_text(encoding="utf-8")
    for banned in ("串标成立", "围标", "认定串通", "违法认定", "同一制作方"):
        assert banned not in text
