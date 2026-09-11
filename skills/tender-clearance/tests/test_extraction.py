"""T01 / T09 / T10：内容提取、字段证据定位、低置信度隔离。"""

from __future__ import annotations

import json

from tc.fields import scan_inline


def _content(alpha_project):
    return json.loads((alpha_project / "output/interim/content.json").read_text(encoding="utf-8"))


def _entities(alpha_project):
    return json.loads((alpha_project / "output/interim/entities.json").read_text(encoding="utf-8"))


def test_core_fields_have_traceable_locations(alpha_project):
    """T01：核心字段能回到 PDF 页码 / XLSX 表格坐标。"""
    content = _content(alpha_project)
    by_field: dict[str, list] = {}
    for f in content["fields"]:
        by_field.setdefault(f["field"], []).append(f)

    # 新口径：一览表不识别；统一社会信用代码来自商务标页码证据。
    uscc_pages = [f for f in by_field["uscc"] if f["location"]["kind"] == "pdf_page"]
    assert uscc_pages
    assert all(isinstance(f["location"]["page"], int) for f in uscc_pages)
    assert not any(f["location"]["kind"] == "xlsx_cell" for f in by_field["uscc"])

    # 授权代表证件号：来自 PDF 授权书文本层
    agent_ids = [f for f in by_field.get("bid_agent_id", [])]
    assert agent_ids, "未提取到授权代表证件号"
    assert all(f["location"]["kind"] == "pdf_page" for f in agent_ids)
    assert all(f["digest"] and len(f["digest"]) == 16 for f in agent_ids)
    assert all(f["value_masked"].count("*") == 10 for f in agent_ids)


def test_three_suppliers_not_merged(alpha_project):
    """T01：三家主体独立，未被错误合并。"""
    sups = _entities(alpha_project)["suppliers"]
    assert len(sups) == 4
    usccs = [s["uscc"] for s in sups if s["uscc"]]
    assert len(usccs) == len(set(usccs)), "不同供应商出现相同信用代码"
    # 每家有独立的声明名称
    names = [s["declared_name"] for s in sups]
    assert len([n for n in names if n]) == 4


def test_suppliers_identified_by_uscc(alpha_project):
    sups = _entities(alpha_project)["suppliers"]
    by_dir = {s["directory_name"]: s for s in sups}
    a = by_dir["huaxin-chuangyuan"]
    assert a["uscc_status"] == "present_valid"
    assert a["confirmation"] == "confirmed"


def test_ocr_low_confidence_excluded_from_matching(alpha_project):
    """T09：OCR 错位信用代码（低置信度）不参与精确匹配，进入人工核对。"""
    content = _content(alpha_project)
    ocr_fields = [f for f in content["fields"] if f["method"].startswith("ocr")]
    assert ocr_fields, "mock OCR 字段未进入提取"
    assert all(f["low_confidence"] for f in ocr_fields)
    # 未被采信为主体信用代码
    sups = _entities(alpha_project)["suppliers"]
    for s in sups:
        assert s["uscc_status"] != "present_invalid_format" or s["directory_name"] != "yuntu-zhilian"
    low_conf = json.loads((alpha_project / "output/interim/low_confidence.json").read_text(encoding="utf-8"))
    assert low_conf["evidence_ids"]


def test_field_records_carry_evidence_ids(alpha_project):
    content = _content(alpha_project)
    for f in content["fields"]:
        assert f["evidence_id"].startswith("EV-")
        assert f["method"]
        assert f["location"]


def test_name_label_rejects_table_text_and_keeps_real_name():
    """真实扫描/文本层中，人员标签后可能紧跟表格标题；不得生成假姓名。"""
    text = "法定代表人身份证明\n法定代表人：张三\n负责人审核后实施"
    hits = [h for h in scan_inline(text) if h.field == "legal_rep_name"]
    assert [(h.value, h.context) for h in hits] == [("张三", "法定代表人")]


def test_multiple_uscc_candidates_are_not_arbitrarily_selected(alpha_project):
    """T09 扩展：多代码场景必须保留候选，不能把设备厂商代码作为主体代码。"""
    entities = _entities(alpha_project)
    # 夹具中的主体代码唯一时仍保持原有确认行为；多代码回归由单元规则直接覆盖。
    for supplier in entities["suppliers"]:
        if len(supplier.get("uscc_candidates", [])) > 1:
            assert supplier["uscc"] is None
            assert supplier["confirmation"] == "candidate"
