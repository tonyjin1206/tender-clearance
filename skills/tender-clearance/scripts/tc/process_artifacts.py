"""把中间产物投影为过程工作底稿和报告安全输入。

``ocr-results.json`` 是受控的 OCR 审计层，包含文本块和质量字段；本模块不把它
复制进报告输入。过程底稿保留字段级质量信息供 Core 使用，``report-input.v1``
只保留报告所需的字段值、状态和证据引用，供宿主 Agent 传递路径或供本地渲染器读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canon import write_json
from .models import ContentFile, EntitiesFile, FindingsFile, InventoryFile


_REPORT_LOCATION_KEYS = {"kind", "page", "sheet", "cell", "table", "row", "col", "index", "cover"}


def _report_location(location: dict[str, Any]) -> dict[str, Any]:
    """只保留报告定位需要的字段，去掉 OCR Provider/坐标精度等运行细节。"""
    return {
        key: value for key, value in location.items()
        if key in _REPORT_LOCATION_KEYS and isinstance(value, (str, int, float, bool))
    }


def _review_state(field: Any) -> tuple[str, str | None]:
    if field.low_confidence:
        return "human_review_required", "ocr_low_confidence_or_location_uncertain"
    if field.method.startswith("ocr"):
        return "candidate", "ocr_field_requires_source_review_before_identity_use"
    return "candidate", None


def _field_fact(field: Any, *, include_quality: bool) -> dict[str, Any]:
    state, reason = _review_state(field)
    fact: dict[str, Any] = {
        "fact_id": field.evidence_id,
        "document_id": field.document_id,
        "relative_path": field.relative_path,
        "supplier_dir": field.supplier_dir,
        "field": field.field,
        "value": field.value_masked,
        "normalized": field.normalized,
        "method": field.method,
        "location": field.location if include_quality else _report_location(field.location),
        "evidence_id": field.evidence_id,
        "review_state": state,
    }
    if reason:
        fact["review_reason"] = reason
    if include_quality:
        fact["confidence"] = field.confidence
        fact["low_confidence"] = field.low_confidence
    return fact


def build_process_artifacts(
    project_dir: Path,
    *,
    inventory: InventoryFile,
    content: ContentFile,
    entities: EntitiesFile | None = None,
    findings: FindingsFile | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """写入过程底稿和不含质量数值/OCR 块的报告安全输入。"""
    interim = project_dir / "output" / "interim"
    process_facts = [_field_fact(field, include_quality=True) for field in content.fields]
    report_facts = [_field_fact(field, include_quality=False) for field in content.fields]
    expected_ocr_pages = sum(len(doc.scanned_pages) for doc in content.documents)
    completed_ocr_pages = sum(len(doc.ocr_pages) for doc in content.documents)
    review_count = sum(1 for field in content.fields if field.low_confidence)

    process_workpaper: dict[str, Any] = {
        "schema_version": "tender-clearance.process-workpaper.v1",
        "run": inventory.run.model_dump(mode="json"),
        "documents": [
            {
                "document_id": doc.document_id,
                "relative_path": doc.relative_path,
                "status": doc.status,
                "scanned_pages": doc.scanned_pages,
                "ocr_pages": doc.ocr_pages,
                "issues": doc.issues,
            }
            for doc in content.documents
        ],
        "facts": process_facts,
        "quality": {
            "expected_ocr_pages": expected_ocr_pages,
            "completed_ocr_pages": completed_ocr_pages,
            "low_confidence_facts": review_count,
            "ocr_results_complete": expected_ocr_pages == completed_ocr_pages,
        },
        "source_artifacts": {
            "ocr_results": "output/interim/ocr-results.json",
            "content": "output/interim/content.json",
            "evidence": "output/interim/evidence-content.json",
        },
    }

    report_input: dict[str, Any] = {
        "schema_version": "tender-clearance.report-input.v1",
        "run": inventory.run.model_dump(mode="json"),
        "facts": report_facts,
        "quality": {
            "expected_ocr_pages": expected_ocr_pages,
            "completed_ocr_pages": completed_ocr_pages,
            "review_required_facts": review_count,
            "ocr_results_complete": expected_ocr_pages == completed_ocr_pages,
        },
        "entities": entities.model_dump(mode="json") if entities else None,
        "findings": findings.model_dump(mode="json") if findings else None,
        "source_artifacts": {
            "process_workpaper": "output/interim/process-workpaper.json",
            "evidence_index": "output/证据索引.csv",
            "review_queue": "output/人工复核清单.csv",
        },
    }
    write_json(interim / "process-workpaper.json", process_workpaper)
    write_json(interim / "report-input.json", report_input)
    return process_workpaper, report_input

