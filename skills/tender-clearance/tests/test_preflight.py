"""运行前预检：集中确认范围、依赖、OCR 和外部授权，不在阶段中途提问。"""

from __future__ import annotations

from pathlib import Path

from preflight import build_intake_plan, build_plan


def test_intake_plan_needs_no_project_config_or_document_reading():
    plan = build_intake_plan("report")

    assert plan["status"] == "awaiting_user_input"
    assert [q["id"] for q in plan["questions"]] == [
        "bid_deadline",
        "public_web_authorization",
        "srm_credentials",
        "sensitive_display",
        "tender_template",
    ]
    assert "do_not_parse_or_extract_uploaded_documents" in plan["before_answers"]
    assert plan["questions"][2]["runtime_only"] is True


def test_preflight_is_read_only_and_classifies_technical_bids(alpha_project):
    plan = build_plan(alpha_project, "report")

    assert plan["status"] == "needs_confirmation"
    assert plan["input_summary"]["technical_body_scan"] == "skip_by_filename"
    assert plan["input_summary"]["bid_filename_classification"]["technical"] >= 1
    assert plan["environment"]["required_packages"]
    assert [q["id"] for q in plan["questions"]][:3] == [
        "execution_scope", "environment_install", "ocr_provider"
    ]
    assert plan["after_confirmation"] == [
        "install_all_confirmed_packages_once",
        "run_pipeline_without_mid_run_questions",
        "never_install_or_network_during_report_render",
    ]


def test_preflight_explains_template_accuracy_benefit_and_dependency(tmp_path: Path):
    (tmp_path / "bids").mkdir()
    (tmp_path / "procurement").mkdir()
    (tmp_path / "procurement" / "空白招标模板.docx").write_bytes(b"not parsed by preflight")
    (tmp_path / "project.yaml").write_text(
        "project_id: P\nproject_name: 测试\nbid_deadline: '2026-09-01T09:00:00+08:00'\n"
        "run_date: '2026-09-01'\nexternal_query_mode: offline\n"
        "redaction_mode: standard\nretention_policy: project_local\n",
        encoding="utf-8",
    )

    plan = build_plan(tmp_path, "report")

    template = plan["input_summary"]["tender_template"]
    assert template["status"] == "provided"
    assert "大幅提高" in template["message"]
    assert plan["environment"]["required_packages"]["docx"] == "python-docx"
    assert any(q["id"] == "tender_template" and not q["required"] for q in plan["questions"])
