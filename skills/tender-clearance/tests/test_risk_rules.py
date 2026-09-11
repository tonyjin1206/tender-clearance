"""规则引擎测试：T02 / T03 / T04 / T06 / 等级上限。"""

from __future__ import annotations


def _sup(alpha_project):
    import json

    ents = json.loads((alpha_project / "output/interim/entities.json").read_text(encoding="utf-8"))
    return {s["directory_name"]: s["supplier_id"] for s in ents["suppliers"]}


def _findings_of(alpha_findings, rule_id):
    return [f for f in alpha_findings["findings"] if f["rule_id"] == rule_id]


def test_t02_shared_agent_id_is_level_i_with_two_evidences(alpha_findings, alpha_project):
    """T02：两家授权代表证件摘要一致 → ID-002 I 级、含证据、要求人工复核。"""
    sup = _sup(alpha_project)
    a, b = sup["huaxin-chuangyuan"], sup["zhongheng-taida"]
    hits = _findings_of(alpha_findings, "ID-002")
    assert hits, "未产生 ID-002"
    f = next(x for x in hits if set(x["supplier_ids"]) == {a, b})
    assert f["level"] == "I"
    assert f["evidence_strength"] == "A"
    assert len(f["evidence_ids"]) >= 2
    assert f["status"] == "human_review_required"
    assert f["finding_id"] in alpha_findings["human_review_queue"]


def test_t03_wps_and_close_time_only_meta003_iii(alpha_findings, alpha_project):
    """T03：仅 Producer=WPS + 创建时间相近 → 只产生 META-003 III 级，不得出现断言性表述。"""
    sup = _sup(alpha_project)
    a, b = sup["huaxin-chuangyuan"], sup["zhongheng-taida"]
    metas = [f for f in alpha_findings["findings"]
             if set(f["supplier_ids"]) == {a, b} and f["domain"] == "metadata"]
    weak = [f for f in metas if f["rule_id"] == "META-003"]
    assert weak, "未产生 META-003 弱线索"
    assert all(f["level"] == "III" for f in weak)
    # 两家商务标的 Producer=WPS 与相近创建时间被如实列出
    assert any("WPS" in f["fact"] and "创建时间相近" in f["fact"] for f in weak)
    # 工具/时间不产生更强的元数据发现（META-001 II 仅可能来自叠加的有限辨识字段）
    strong = [f for f in metas if f["rule_id"] == "META-001" and f["level"] == "II"]
    assert not strong, "编辑工具/创建时间不得升级为 II 级"
    for f in weak:
        for banned in ("同一制作方", "串标", "围标", "同一主体", "违法"):
            assert banned not in f["fact"], f"事实表述出现断言性词语：{banned}"


def test_t04_same_serial_i_vs_same_model_iii(alpha_findings, alpha_project):
    """T04：相同设备序列号 → META-002 I 级候选；仅相同型号 → III 级。"""
    sup = _sup(alpha_project)
    a, b = sup["huaxin-chuangyuan"], sup["zhongheng-taida"]
    c = sup["yuntu-zhilian"]
    m2 = _findings_of(alpha_findings, "META-002")
    pair = next(f for f in m2 if set(f["supplier_ids"]) == {a, b})
    assert pair["level"] == "I"
    # 序列号原文可在证据索引回溯（值存在，不在 fact 中裸奔）
    assert pair["evidence_ids"]
    # C 与其他家之间：只允许 META-001/META-003 的 III 级
    for f in alpha_findings["findings"]:
        if c in f["supplier_ids"] and f["domain"] == "metadata":
            assert f["level"] == "III", "仅相同型号不得升级"


def test_t06_ownership_effective_vs_historical(alpha_findings, alpha_project):
    """T06：仅在投标截止日有效的关系产生 OWN-001 II；历史关系 III 且注明日期。"""
    sup = _sup(alpha_project)
    b, c = sup["zhongheng-taida"], sup["yuntu-zhilian"]
    own = _findings_of(alpha_findings, "OWN-001")
    effective = [f for f in own if {b, c} == set(f["supplier_ids"]) and f["level"] == "II"]
    assert effective, "截点内共同股东未产生 OWN-001 II"
    assert all(f["evidence_strength"] in ("B", "C") for f in effective)

    a = sup["huaxin-chuangyuan"]
    historical = [f for f in own if {a, b} == set(f["supplier_ids"]) and f["level"] == "III"]
    assert historical, "历史关系应降为 III"
    assert any("2025-06-30" in f["fact"] or "至 2025" in f["fact"] for f in historical)


def test_level_caps_enforced(alpha_findings):
    """等级上限：D 级证据 ≤ III；主体未确认 ≤ III。"""
    for f in alpha_findings["findings"]:
        if f["evidence_strength"] == "D":
            assert f["level"] == "III"
        if f["subject_confirmation"] != "confirmed":
            assert f["level"] == "III"


def test_all_findings_have_rule_version_and_evidence_semantics(alpha_findings):
    for f in alpha_findings["findings"]:
        assert f["rules_version"]
        assert f["finding_id"].startswith("FD-")
        # COV-001 之外不存没有证据的断言
        if f["rule_id"] != "COV-001":
            assert f["evidence_ids"], f["finding_id"]
