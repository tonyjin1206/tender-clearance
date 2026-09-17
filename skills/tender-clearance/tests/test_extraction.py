"""T01 / T09 / T10：内容提取、字段证据定位、低置信度隔离。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tc.fields import scan_inline, scan_ocr_blocks, scan_template_metric_blocks
from tc.field_decisions import decisions_for_fields, resolve_single_value
from extract_content import _ocr_priority


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


def test_authorization_scan_assigns_two_id_cards_by_visible_left_right_layout():
    """授权书两张身份证按可见左右位置分别归属法人和授权代表。"""
    blocks = [
        {"text": "法人代表 授权代表", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.10, "width": 0.30, "height": 0.03}},
        {"text": "公民身份号码", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.60, "width": 0.12, "height": 0.03}},
        {"text": "220123196503170641", "confidence": 0.98,
         "bbox": {"x": 0.25, "y": 0.60, "width": 0.20, "height": 0.03}},
        {"text": "公民身份号码", "confidence": 0.98,
         "bbox": {"x": 0.60, "y": 0.60, "width": 0.12, "height": 0.03}},
        {"text": "220281198706113434", "confidence": 0.98,
         "bbox": {"x": 0.75, "y": 0.60, "width": 0.20, "height": 0.03}},
    ]

    hits = scan_ocr_blocks(blocks, average_confidence=0.98, location_precision="block")
    by_value = {h.value: h.field for h in hits if h.field in {"legal_rep_id", "bid_agent_id"}}

    assert by_value == {
        "220123196503170641": "legal_rep_id",
        "220281198706113434": "bid_agent_id",
    }


def test_ocr_spatial_candidate_retains_label_relation_and_bboxes():
    """OCR 候选必须带可回看的标签关系和标签/值块坐标。"""
    blocks = [
        {"text": "统一社会信用代码", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.20, "width": 0.20, "height": 0.03}},
        {"text": "91350100M000100Y43", "confidence": 0.97,
         "bbox": {"x": 0.32, "y": 0.20, "width": 0.30, "height": 0.03}},
    ]

    hits = scan_ocr_blocks(blocks, average_confidence=0.98, location_precision="block")
    hit = next(h for h in hits if h.field == "uscc")

    assert hit.label == "统一社会信用代码"
    assert hit.label_relation == "same_line_right"
    assert hit.label_bbox == blocks[0]["bbox"]
    assert hit.value_bbox == blocks[1]["bbox"]


def test_ocr_project_fields_reject_body_noise_values():
    blocks = [
        {"text": "招标人", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.20, "width": 0.12, "height": 0.03}},
        {"text": "1）将本项目投标保证金汇入为该项目设置的指定账户", "confidence": 0.98,
         "bbox": {"x": 0.25, "y": 0.20, "width": 0.55, "height": 0.03}},
        {"text": "项目名称", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.30, "width": 0.12, "height": 0.03}},
        {"text": "单价", "confidence": 0.98,
         "bbox": {"x": 0.25, "y": 0.30, "width": 0.10, "height": 0.03}},
    ]
    hits = scan_ocr_blocks(blocks, average_confidence=0.98, location_precision="block")
    assert not [hit for hit in hits if hit.field in {"tenderer", "project_name"}]


def test_ocr_cover_title_recovers_unlabelled_project_and_tenderer():
    blocks = [
        {"text": "富奥汽车零部件股份有限公司传动轴分公司", "confidence": 0.98,
         "bbox": {"x": 0.20, "y": 0.20, "width": 0.50, "height": 0.03}},
        {"text": "含油污泥鉴定国内邀请招标项目", "confidence": 0.98,
         "bbox": {"x": 0.30, "y": 0.25, "width": 0.40, "height": 0.03}},
        {"text": "项目编号：CG2608110001", "confidence": 0.98,
         "bbox": {"x": 0.35, "y": 0.32, "width": 0.30, "height": 0.03}},
        {"text": "投标文件", "confidence": 0.98,
         "bbox": {"x": 0.42, "y": 0.46, "width": 0.15, "height": 0.03}},
    ]
    hits = scan_ocr_blocks(blocks, average_confidence=0.98, location_precision="block")
    assert any(h.field == "tenderer" and "传动轴分公司" in h.value for h in hits)
    assert any(h.field == "project_name" and h.value.endswith("国内邀请招标项目") for h in hits)


def test_identity_pages_are_marked_for_fast_ocr_priority():
    doc = SimpleNamespace(bid_subtype="business")
    assert _ocr_priority(doc, 1, "") == "identity_fast"
    assert _ocr_priority(doc, 8, "统一社会信用代码") == "identity_fast"
    assert _ocr_priority(doc, 8, "技术参数与响应") == "full"


def test_template_metric_anchor_pairs_only_nearby_value_block():
    metrics = [{"metric_id": "M-abc123", "label": "额定功率"}]
    blocks = [
        {"text": "额定功率", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.20, "width": 0.12, "height": 0.03}},
        {"text": "15kW", "confidence": 0.96,
         "bbox": {"x": 0.25, "y": 0.20, "width": 0.10, "height": 0.03}},
        {"text": "额定电压", "confidence": 0.98,
         "bbox": {"x": 0.10, "y": 0.70, "width": 0.12, "height": 0.03}},
        {"text": "220V", "confidence": 0.96,
         "bbox": {"x": 0.25, "y": 0.70, "width": 0.10, "height": 0.03}},
    ]

    hits = scan_template_metric_blocks(metrics=metrics, blocks=blocks,
                                       average_confidence=0.98, location_precision="block")

    assert len(hits) == 1
    assert hits[0].field == "metric:M-abc123"
    assert hits[0].value == "15kW"
    assert hits[0].label_relation == "template_metric_same_line_right"
    assert hits[0].label_bbox == blocks[0]["bbox"]
    assert hits[0].value_bbox == blocks[1]["bbox"]


def test_template_metric_page_only_requires_inline_label_value():
    metrics = [{"metric_id": "M-abc123", "label": "额定功率"}]
    blocks = [{"text": "额定功率"}, {"text": "15kW"}]

    assert scan_template_metric_blocks(
        blocks, metrics, average_confidence=0.98, location_precision="page_only"
    ) == []
    inline = scan_template_metric_blocks(
        [{"text": "额定功率：15kW", "confidence": 0.98}], metrics,
        average_confidence=0.98, location_precision="page_only",
    )
    assert inline[0].value == "15kW"
    assert inline[0].confidence <= 0.5


def test_conflicting_core_candidates_are_kept_for_review_not_selected():
    """同一范围的多值候选不得按频次或排序进入正式字段。"""
    records = [
        SimpleNamespace(
            field="project_code", value_masked="PRJ-A", normalized="PRJ-A",
            evidence_id="EV-A", confidence=1.0, low_confidence=False,
            location={"kind": "pdf_page", "page": 1, "cover": True},
        ),
        SimpleNamespace(
            field="project_code", value_masked="PRJ-B", normalized="PRJ-B",
            evidence_id="EV-B", confidence=0.99, low_confidence=False,
            location={"kind": "pdf_page", "page": 1, "cover": True},
        ),
    ]

    decision = resolve_single_value(records, scope="project", field="project_code")

    assert decision.status == "conflict"
    assert decision.value is None
    assert decision.evidence_ids == ("EV-A", "EV-B")
    assert {item["normalized"] for item in decision.candidate_values} == {"PRJ-A", "PRJ-B"}


def test_field_decisions_select_repeated_single_value_and_keep_evidence():
    records = [
        SimpleNamespace(
            field="project_name", value_masked="项目甲", normalized="项目甲",
            evidence_id="EV-2", confidence=0.91, low_confidence=False,
            location={"kind": "pdf_page", "page": 1, "cover": True},
        ),
        SimpleNamespace(
            field="project_name", value_masked="项目甲", normalized="项目甲",
            evidence_id="EV-1", confidence=0.99, low_confidence=False,
            location={"kind": "pdf_page", "page": 1, "cover": True},
        ),
    ]

    decision = decisions_for_fields(records)[0]

    assert decision.status == "selected"
    assert decision.value == "项目甲"
    assert decision.evidence_ids == ("EV-1", "EV-2")
    assert decision.candidate_values[0]["evidence_ids"] == ["EV-1", "EV-2"]


def test_multiple_uscc_candidates_are_not_arbitrarily_selected(alpha_project):
    """T09 扩展：多代码场景必须保留候选，不能把设备厂商代码作为主体代码。"""
    entities = _entities(alpha_project)
    # 夹具中的主体代码唯一时仍保持原有确认行为；多代码回归由单元规则直接覆盖。
    for supplier in entities["suppliers"]:
        if len(supplier.get("uscc_candidates", [])) > 1:
            assert supplier["uscc"] is None
            assert supplier["confirmation"] == "candidate"
