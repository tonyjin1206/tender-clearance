"""OCR 上下文防火墙、过程底稿和报告安全输入回归。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import typer

from tc.canon import write_json
from tc.process_artifacts import ocr_completion, validate_report_input
from tc.progress import emit_progress


FORBIDDEN_CONTEXT_KEYS = {
    "text", "raw_text", "ocr_text", "blocks", "confidence", "average_confidence",
    "bbox", "provider", "provider_id", "provider_version", "model", "models",
    "inference_engine", "location_precision", "processing_location",
}


def _keys(value):
    if isinstance(value, dict):
        yield from value.keys()
        for child in value.values():
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_progress_event_is_context_safe(capsys):
    emit_progress(
        "ocr", "running", event="page_completed", message="这里是 OCR 原文",
        page=2, total_pages=5, job_id="OCRJ-1", text="身份证原文",
        blocks=[{"text": "身份证原文", "confidence": 0.99}], confidence=0.99,
        bbox={"x": 0.1}, provider="paddle", model="secret-model",
    )
    line = next(item for item in capsys.readouterr().err.splitlines() if item.startswith("TC_PROGRESS_V1 "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["message"] == "阶段进度更新"
    assert payload["page"] == 2
    assert not (FORBIDDEN_CONTEXT_KEYS & set(_keys(payload)))
    assert "身份证原文" not in line


def test_process_workpaper_keeps_quality_but_report_input_does_not(alpha_project):
    process = json.loads((alpha_project / "output/interim/process-workpaper.json").read_text(encoding="utf-8"))
    report_input = json.loads((alpha_project / "output/interim/report-input.json").read_text(encoding="utf-8"))

    assert process["schema_version"] == "tender-clearance.process-workpaper.v1"
    assert any("confidence" in fact for fact in process["facts"])
    assert report_input["schema_version"] == "tender-clearance.report-input.v1"
    assert not (FORBIDDEN_CONTEXT_KEYS & set(_keys(report_input)))
    assert all("confidence" not in fact for fact in report_input["facts"])
    assert all("low_confidence" not in fact for fact in report_input["facts"])
    assert validate_report_input(report_input) == []

    root = Path(__file__).resolve().parent.parent / "schemas"
    jsonschema.validate(process, json.loads((root / "process-workpaper.schema.json").read_text(encoding="utf-8")))
    jsonschema.validate(report_input, json.loads((root / "report-input.schema.json").read_text(encoding="utf-8")))


def test_ocr_completion_marks_missing_result_as_pending(alpha_project, tmp_path):
    project = tmp_path / "incomplete"
    project.mkdir()
    interim = project / "output/interim"
    interim.mkdir(parents=True)
    inventory = json.loads((alpha_project / "output/interim/inventory.json").read_text(encoding="utf-8"))
    write_json(interim / "ocr-jobs.json", {
        "run": inventory["run"],
        "jobs": [{
            "contract_version": "ocr-job.v1",
            "job_id": "OCRJ-missing",
            "document_id": "DOC-scan",
            "source_sha256": "a" * 64,
            "page": 1,
            "page_image_sha256": "b" * 64,
            "input_ref": "bids/供应商A/商务标.pdf#page=1",
            "languages": ["zh-Hans", "en"],
            "mode": "accurate",
            "purpose": "tender_clearance_field_candidates",
            "privacy_requirement": "local_only",
        }],
    })
    status = ocr_completion(project)
    assert status["expected_jobs"] == 1
    assert status["terminal"] is False
    assert status["pending_job_ids"] == ["OCRJ-missing"]


def test_formal_report_blocks_when_ocr_result_is_missing(alpha_project, tmp_path, monkeypatch):
    project = tmp_path / "formal-incomplete"
    shutil.copytree(alpha_project, project)
    interim = project / "output/interim"
    jobs = json.loads((interim / "ocr-jobs.json").read_text(encoding="utf-8"))
    jobs["jobs"].append({
        "contract_version": "ocr-job.v1",
        "job_id": "OCRJ-formal-missing",
        "document_id": "DOC-formal-scan",
        "source_sha256": "c" * 64,
        "page": 1,
        "page_image_sha256": "d" * 64,
        "input_ref": "bids/供应商A/商务标.pdf#page=1",
        "languages": ["zh-Hans", "en"],
        "mode": "accurate",
        "purpose": "tender_clearance_field_candidates",
        "privacy_requirement": "local_only",
    })
    write_json(interim / "ocr-jobs.json", jobs)

    import render_report
    monkeypatch.setattr(render_report, "validate_srm_report_gate", lambda *_args: None)
    with pytest.raises(typer.Exit) as exc_info:
        render_report.run(project, profile="report", docx=False, skip_srm_gate=False)
    assert exc_info.value.exit_code == 5
