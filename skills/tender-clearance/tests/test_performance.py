"""阶段耗时记录与超时边界。"""

from __future__ import annotations

import json

import pytest

from tc import pipeline


def test_pipeline_writes_stage_timings(alpha_project):
    data = __import__("json").loads(
        (alpha_project / "output/interim/performance.json").read_text(encoding="utf-8")
    )

    assert data["schema_version"] == "tender-clearance.performance.v1"
    assert data["status"] == "succeeded"
    assert data["total_seconds"] >= 0
    assert [item["stage"] for item in data["stages"]] == [
        "inventory",
        "extract_documents",
        "normalize_and_match",
        "analyze_bid_data",
        "import_external_evidence",
        "assess_risk",
        "render_report",
    ]
    assert all(item["elapsed_seconds"] >= 0 for item in data["stages"])


def test_stage_timeout_returns_timing(monkeypatch, tmp_path):
    (tmp_path / "slow.py").write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "SCRIPTS", tmp_path)

    with pytest.raises(pipeline.StageExecutionError) as exc_info:
        pipeline.run_stage("slow.py", [], tmp_path, timeout_seconds=0.01)

    timing = exc_info.value.timing
    assert timing.stage == "slow"
    assert timing.status == "timed_out"
    assert timing.exit_code is None


def test_stage_selection_supports_resume_and_ranges():
    stages = [(name, []) for name, _ in pipeline.STAGES]
    prior = {
        "inventory": "succeeded",
        "extract_documents": "succeeded",
        "resolve_supplier_groups": "failed",
    }

    resumed = pipeline._select_stage_range(stages, resume=True, prior_status=prior)
    assert resumed[0][0] == "resolve_supplier_groups.py"
    assert pipeline._select_stage_range(
        stages, from_stage="normalize_and_match", to_stage="assess_risk"
    ) == [
        ("normalize_and_match.py", []),
        ("analyze_bid_data.py", []),
        ("import_external_evidence.py", []),
        ("assess_risk.py", []),
    ]


def test_stage_selection_rejects_ambiguous_controls():
    stages = [("inventory.py", []), ("render_report.py", [])]
    with pytest.raises(pipeline.ProjectError):
        pipeline._select_stage_range(stages, resume=True, from_stage="inventory")


def test_performance_report_preserves_completed_stages_across_resume(tmp_path):
    first = [
        pipeline.StageTiming("inventory", 1.0, "succeeded", 0),
        pipeline.StageTiming("extract_documents", 2.0, "succeeded", 0),
        pipeline.StageTiming("resolve_supplier_groups", 3.0, "failed", 1),
    ]
    pipeline._write_performance_report(
        tmp_path, 0.0, first, "failed", selected_stages=[item.stage for item in first]
    )
    second = [
        pipeline.StageTiming("resolve_supplier_groups", 1.0, "succeeded", 0),
        pipeline.StageTiming("normalize_and_match", 2.0, "failed", 1),
    ]
    pipeline._write_performance_report(
        tmp_path, 0.0, second, "failed", selected_stages=[item.stage for item in second]
    )

    data = json.loads((tmp_path / "output/interim/performance.json").read_text(encoding="utf-8"))
    assert [item["stage"] for item in data["stages"]] == [
        "inventory", "extract_documents", "resolve_supplier_groups", "normalize_and_match"
    ]
    assert {item["stage"]: item["status"] for item in data["stages"]} == {
        "inventory": "succeeded",
        "extract_documents": "succeeded",
        "resolve_supplier_groups": "succeeded",
        "normalize_and_match": "failed",
    }
    resumed = pipeline._select_stage_range(
        [(name, []) for name, _ in pipeline.STAGES],
        resume=True,
        prior_status={item["stage"]: item["status"] for item in data["stages"]},
    )
    assert resumed[0][0] == "normalize_and_match.py"


def test_partial_report_run_does_not_require_srm_credentials(monkeypatch, tmp_path):
    (tmp_path / "project.yaml").write_text(
        "project_id: P1\n"
        "project_name: 测试项目\n"
        "bid_deadline: 2026-09-01T09:00:00+08:00\n"
        "run_date: 2026-09-01\n"
        "external_query_mode: live\n"
        "external_query_sources: [srm]\n"
        "redaction_mode: standard\n"
        "retention_policy: project_local\n",
        encoding="utf-8",
    )
    (tmp_path / "bids").mkdir()
    calls = []

    def fake_run_stage(script, args, project_dir, **kwargs):
        calls.append((script, args, kwargs.get("env")))
        return pipeline.StageTiming(script.removesuffix(".py"), 0.0, "succeeded", 0)

    monkeypatch.setattr(pipeline, "run_stage", fake_run_stage)
    pipeline.run_all(
        tmp_path,
        profile="report",
        from_stage="assess_risk",
        to_stage="render_report",
    )
    assert [item[0] for item in calls] == ["assess_risk.py", "render_report.py"]
    assert all(item[2] is None for item in calls)
