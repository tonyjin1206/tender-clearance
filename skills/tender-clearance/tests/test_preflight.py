"""运行前预检：集中确认范围、依赖、OCR 和外部授权，不在阶段中途提问。"""

from __future__ import annotations

from preflight import build_plan


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
