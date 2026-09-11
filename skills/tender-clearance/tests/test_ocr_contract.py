"""宿主 OCR 契约与导入测试（T13–T18 的最小回归）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


def _make_image_only_pdf(path: Path, text: str) -> None:
    import fitz

    src = fitz.open()
    page = src.new_page(width=595, height=200)
    page.insert_text((60, 100), text, fontname="china-s", fontsize=14)
    pix = page.get_pixmap(dpi=150)
    dst = fitz.open()
    dpage = dst.new_page(width=page.rect.width, height=page.rect.height)
    dpage.insert_image(dpage.rect, pixmap=pix)
    dst.save(str(path))
    dst.close()
    src.close()


def _prepare_scan(alpha_project, tmp_path):
    from tc.pipeline import run_stage

    project = tmp_path / "p"
    shutil.copytree(alpha_project, project)
    _make_image_only_pdf(
        project / "bids" / "huaxin-chuangyuan" / "附件-声明扫描件.pdf",
        "统一社会信用代码 91110108MA01CWY70M 供应商名称 华信创远集团有限公司",
    )
    run_stage("inventory.py", [], project)
    run_stage("extract_content.py", [], project)
    return project


def _result_for_first_job(project: Path, *, source_sha256: str | None = None) -> dict:
    jobs = json.loads((project / "output/interim/ocr-jobs.json").read_text(encoding="utf-8"))["jobs"]
    job = jobs[0]
    return {
        "contract_version": "ocr-result.v1",
        "job_id": job["job_id"],
        "document_id": job["document_id"],
        "source_sha256": source_sha256 or job["source_sha256"],
        "page": job["page"],
        "status": "succeeded",
        "provider": {
            "id": "paddleocr-text-recognition",
            "version": "test",
            "engine": "onnxruntime-cpu",
            "models": ["PP-OCRv4_mobile_det", "PP-OCRv4_mobile_rec"],
            "processing_location": "local",
        },
        "average_confidence": 0.91,
        "location_precision": "page_only",
        "blocks": [{"text": "统一社会信用代码 91110108MA01CWY70M 供应商名称 华信创远集团有限公司", "confidence": 0.91}],
        "completed_at": "2026-09-10T00:00:00+08:00",
    }


def test_ocr_job_generated_without_engine(alpha_project, tmp_path):
    project = _prepare_scan(alpha_project, tmp_path)
    jobs = json.loads((project / "output/interim/ocr-jobs.json").read_text(encoding="utf-8"))
    assert jobs["jobs"]
    assert jobs["jobs"][0]["contract_version"] == "ocr-job.v1"
    assert jobs["jobs"][0]["privacy_requirement"] == "local_only"


def test_invalid_ocr_result_is_rejected(alpha_project, tmp_path):
    from tc.pipeline import run_stage

    project = _prepare_scan(alpha_project, tmp_path)
    result = _result_for_first_job(project, source_sha256="0" * 64)
    src = tmp_path / "invalid.json"
    src.write_text(json.dumps({"results": [result]}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError):
        run_stage("import_ocr_results.py", ["--input", str(src)], project)
    errors = json.loads((project / "output/interim/ocr-import-errors.json").read_text(encoding="utf-8"))
    assert errors["status"] == "ocr_result_invalid"
    assert any("source_sha256" in e for e in errors["errors"])


def test_valid_page_only_ocr_is_low_confidence_and_imported(alpha_project, tmp_path):
    from tc.pipeline import run_stage

    project = _prepare_scan(alpha_project, tmp_path)
    src = tmp_path / "valid.json"
    src.write_text(json.dumps({"results": [_result_for_first_job(project)]}, ensure_ascii=False), encoding="utf-8")
    run_stage("import_ocr_results.py", ["--input", str(src)], project)
    content = json.loads((project / "output/interim/content.json").read_text(encoding="utf-8"))
    fields = [f for f in content["fields"] if f["method"] == "ocr_host"]
    assert fields
    assert all(f["low_confidence"] for f in fields)
    assert all(f["confidence"] <= 0.5 for f in fields)
    assert all(f["location"]["location_precision"] == "page_only" for f in fields)


def test_capabilities_reject_missing_coordinates_or_models():
    from tc.models import OCRCapabilities
    from tc.ocr_contract import validate_capabilities

    cap = OCRCapabilities(
        contract_version="ocr.capabilities.v1",
        provider_id="fixture",
        provider_version="1",
        processing_location="local",
        input_formats=["pdf_page"],
        languages=["zh-Hans", "en"],
        returns_confidence=True,
        returns_block_coordinates=False,
        model_names=[],
        location_precision="page_only",
    )
    reasons = validate_capabilities(cap)
    assert "缺少实际模型版本声明" in reasons


def test_second_content_run_hits_document_cache(alpha_project, tmp_path, monkeypatch):
    from tc import parsers
    from importlib import import_module

    build_content = import_module("extract_content").build_content
    project = tmp_path / "cached"
    shutil.copytree(alpha_project, project)

    def fail(*args, **kwargs):
        raise AssertionError("第二次运行不应重新解析已缓存文档")

    monkeypatch.setattr(parsers, "parse_pdf", fail)
    build_content(project)


def test_same_bytes_at_different_paths_do_not_reuse_document_result(alpha_project, tmp_path, monkeypatch):
    """源文件相同但文档身份不同：复用解析快照，不串用字段/证据身份。"""
    from tc import parsers
    from importlib import import_module

    build_content = import_module("extract_content").build_content
    project = tmp_path / "duplicate-document"
    shutil.copytree(alpha_project, project)
    source = project / "bids" / "huaxin-info" / "商务标-授权书.pdf"
    duplicate = project / "bids" / "huaxin-info" / "商务标副本.pdf"
    shutil.copy2(source, duplicate)

    from tc.pipeline import run_stage

    run_stage("inventory.py", [], project)

    def fail(*args, **kwargs):
        raise AssertionError("相同 SHA 的副本应复用解析快照，不重新打开 PDF")

    monkeypatch.setattr(parsers, "parse_pdf", fail)
    build_content(project)

    content = json.loads((project / "output/interim/content.json").read_text(encoding="utf-8"))
    inventory = json.loads((project / "output/interim/inventory.json").read_text(encoding="utf-8"))
    duplicate_doc = next(d for d in inventory["documents"] if d["relative_path"].endswith("商务标副本.pdf"))
    duplicate_content = next(d for d in content["documents"] if d["relative_path"].endswith("商务标副本.pdf"))
    assert duplicate_content["document_id"] == duplicate_doc["document_id"]
    assert any(f["relative_path"].endswith("商务标副本.pdf") for f in content["fields"])


def test_technical_filename_skips_docx_body_parser(alpha_project, tmp_path, monkeypatch):
    """技术标按文件名短路，避免大 DOCX 正文、表格和媒体被加载。"""
    from tc import parsers
    from importlib import import_module

    build_content = import_module("extract_content").build_content
    project = tmp_path / "technical-skip"
    shutil.copytree(alpha_project, project)

    def fail(*args, **kwargs):
        raise AssertionError("技术标不应进入 DOCX 正文解析")

    monkeypatch.setattr(parsers, "parse_docx", fail)
    content = build_content(project)
    technical = [d for d in content.documents if d.relative_path.endswith("技术标.docx")]
    assert technical
    assert all(d.status == "skipped" for d in technical)
    assert all("跳过正文扫描" in issue for d in technical for issue in d.issues)
