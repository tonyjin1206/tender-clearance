#!/usr/bin/env python3
"""阶段一（续）：提取标书内容与字段候选。

- XLSX：单元格值 + 表头标签关联，记录 sheet/坐标；
- DOCX：正文、表格、页眉页脚、核心/扩展属性；
- PDF：文本层逐页提取，扫描页按 OCR 配置处理（默认不可用 → 人工核对）；
- 每个字段保留原文（脱敏后）、提取方法、置信度与证据定位。

未知格式、密码保护、损坏文件与 OCR 失败均进入异常清单。
"""

from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc import parsers
from tc.canon import load_json, write_json
from tc.fields import extract_from_cells, scan_inline
from tc.models import (
    ContentFile,
    DocumentContent,
    Evidence,
    ExtractionAnomaly,
    FieldRecord,
    InventoryFile,
)
from tc.normalize import (
    id_digest,
    mask_id_number,
    normalize_address,
    normalize_company_name,
    normalize_email,
    normalize_phone,
    validate_uscc,
)
from tc.parsers import ParsedDocx, ParsedPdf, ParsedXlsx
from tc.projio import (
    EvidenceBuilder,
    ProjectError,
    ensure_output_dirs,
    load_project_config,
    load_run_info,
)

LOW_CONFIDENCE_THRESHOLD = 0.85

app = typer.Typer(help="提取标书文本与字段候选（content.json + evidence-content.json）")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
    except ProjectError as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    except FileNotFoundError:
        typer.secho("[错误] 缺少 output/interim/inventory.json，请先运行 inventory.py", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    out_dir, interim = ensure_output_dirs(project_dir)
    builder = EvidenceBuilder(load_run_info(interim), cfg.id_digest_salt)

    doc_contents: list[DocumentContent] = []
    fields: list[FieldRecord] = []
    anomalies: list[ExtractionAnomaly] = []

    for doc in inventory.documents:
        if doc.category == "external_evidence":
            continue  # 外部证据由 import_external_evidence.py 处理
        if doc.extraction_status in ("empty", "password_protected", "corrupt", "unsupported", "skipped"):
            continue  # 清单中已记录原因
        path = project_dir / doc.relative_path
        try:
            if doc.media_type == "application/pdf":
                dc, fr, an = _extract_pdf(path, doc, cfg, builder)
            elif doc.media_type.endswith("wordprocessingml.document"):
                dc, fr, an = _extract_docx(path, doc, builder)
            elif doc.media_type.endswith("spreadsheetml.sheet"):
                dc, fr, an = _extract_xlsx(path, doc, builder)
            elif doc.media_type.startswith("image/"):
                dc = DocumentContent(
                    document_id=doc.document_id, relative_path=doc.relative_path,
                    supplier_dir=doc.supplier_dir, status="partial", issues=["纯图片文件：无文本层，需 OCR 或人工核对"],
                )
                fr, an = [], [
                    ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                      anomaly="ocr_unavailable", detail="图片文件未接入 OCR，内容未经提取")
                ]
            else:
                dc, fr, an = _extract_text(path, doc, builder)
        except Exception as exc:  # noqa: BLE001 - 任何解析异常都不应中断整体
            dc = DocumentContent(document_id=doc.document_id, relative_path=doc.relative_path,
                                 supplier_dir=doc.supplier_dir, status="failed", issues=[f"提取失败：{exc}"])
            fr, an = [], [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                            anomaly="extraction_failed", detail=str(exc)[:300])]
        doc_contents.append(dc)
        fields.extend(fr)
        anomalies.extend(an)

    result = ContentFile(
        run=inventory.run, documents=doc_contents, fields=fields, anomalies=anomalies
    )
    write_json(interim / "content.json", result.model_dump(mode="json"))
    ev = {"run": inventory.run.model_dump(mode="json"), "evidence": [e.model_dump(mode="json") for e in builder.items]}
    write_json(interim / "evidence-content.json", ev)
    typer.secho(
        f"[OK] 内容提取完成：{len(doc_contents)} 个文档，{len(fields)} 个字段候选，"
        f"{len(anomalies)} 条异常 → content.json",
        fg=typer.colors.GREEN,
    )


def tyver_exit_code():
    raise typer.Exit(code=2)


# --------------------------------------------------------------------- PDF


def _extract_pdf(path: Path, doc, cfg, builder: EvidenceBuilder):
    parsed: ParsedPdf = parsers.parse_pdf(path, ocr_provider=cfg.ocr_provider)
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    fields: list[FieldRecord] = []
    anomalies: list[ExtractionAnomaly] = []
    for issue in parsed.issues:
        dc.issues.append(f"{issue.kind}: {issue.detail}")
    # PDF 文档属性作为证据（extract_metadata.py 深入分析，这里不重复）
    for pno, page in enumerate(parsed.pages, start=1):
        text_units = 0
        if page.has_text_layer:
            text_units += _scan_text_block(
                page.text, builder, fields, dc, doc,
                location={"kind": "pdf_page", "page": pno, "cover": pno == 1 and _looks_like_cover(page.text)},
                method="pdf_text_layer", confidence=1.0,
            )
        else:
            if page.image_count > 0:
                dc.scanned_pages.append(pno)
                ocr = None
                ocr_method = "ocr_mock"
                if cfg.ocr_provider == "mock":
                    ocr = parsers.get_mock_ocr_text(parsed.xmp_xml, pno)
                elif cfg.ocr_provider == "paddle":
                    ocr = parsers.get_paddle_ocr_text(parsed, pno)
                    ocr_method = "ocr_paddle"
                if ocr:
                    text, conf = ocr
                    dc.ocr_pages.append(pno)
                    text_units += _scan_text_block(
                        text, builder, fields, dc, doc,
                        location={"kind": "pdf_page", "page": pno, "ocr": True, "cover": pno == 1 and _looks_like_cover(text)},
                        method=ocr_method, confidence=conf,
                    )
                else:
                    anomalies.append(ExtractionAnomaly(
                        document_id=doc.document_id, relative_path=doc.relative_path,
                        anomaly="ocr_unavailable",
                        detail=f"第 {pno} 页为扫描页且未接入 OCR，内容需人工核对",
                    ))
                    dc.status = "partial"
        dc.text_units += text_units
    return dc, fields, anomalies


# --------------------------------------------------------------------- DOCX


def _extract_docx(path: Path, doc, builder: EvidenceBuilder):
    parsed: ParsedDocx = parsers.parse_docx(path)
    cover_like = _looks_like_cover("\n".join(parsed.paragraphs[:8]))
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    fields: list[FieldRecord] = []
    anomalies = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                   anomaly=iss.kind, detail=iss.detail) for iss in parsed.issues]
    for i, text in enumerate(parsed.paragraphs):
        dc.text_units += _scan_text_block(
            text, builder, fields, dc, doc,
            location={"kind": "docx_body", "index": i, "cover": cover_like and i < 8}, method="docx_body", confidence=1.0,
        )
    for table in parsed.tables:
        for ri, row in enumerate(table["rows"]):
            for ci, cell in enumerate(row):
                if not cell:
                    continue
                dc.text_units += _scan_text_block(
                    cell, builder, fields, dc, doc,
                    location={"kind": "docx_table", "table": table["index"], "row": ri, "col": ci, "cover": cover_like},
                    method="docx_table", confidence=1.0,
                )
    for i, text in enumerate(parsed.header_footer_text):
        dc.text_units += _scan_text_block(
            text, builder, fields, dc, doc,
            location={"kind": "docx_header_footer", "index": i}, method="docx_header_footer", confidence=1.0,
        )
    return dc, fields, anomalies


# --------------------------------------------------------------------- XLSX


def _extract_xlsx(path: Path, doc, builder: EvidenceBuilder):
    parsed: ParsedXlsx = parsers.parse_xlsx(path)
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    fields: list[FieldRecord] = []
    anomalies = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                   anomaly=iss.kind, detail=iss.detail) for iss in parsed.issues]
    if doc.category == "bid" and getattr(doc, "bid_subtype", "unknown") == "bid_schedule":
        # 一览表只盘点和保留文件证据，不识别公共项或供应商主体字段。
        return dc, fields, anomalies
    for sheet in parsed.sheets:
        dc.text_units += len(sheet["cells"])
        hits = extract_from_cells(sheet["cells"], sheet["name"])
        for hit in hits:
            location = {"kind": "xlsx_cell", "sheet": sheet["name"],
                        "cell": hit.context.split("→")[-1] if "→" in hit.context else hit.context}
            fields.append(_build_field_record(hit, builder, doc, location, method="xlsx_cell", confidence=hit.confidence))
    return dc, fields, anomalies


# --------------------------------------------------------------------- 文本


def _extract_text(path: Path, doc, builder: EvidenceBuilder):
    text, issue = parsers.read_text_file(path)
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    anomalies: list[ExtractionAnomaly] = []
    fields: list[FieldRecord] = []
    if issue:
        dc.status = "failed"
        anomalies.append(ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                           anomaly=issue.kind, detail=issue.detail))
    dc.text_units = _scan_text_block(
        text, builder, fields, dc, doc,
        location={"kind": "docx_body", "index": 0}, method="plain_text", confidence=1.0,
    )
    return dc, fields, anomalies


# --------------------------------------------------------------------- 公共


def _scan_text_block(
    text: str,
    builder: EvidenceBuilder,
    out_fields: list[FieldRecord],
    dc: DocumentContent,
    doc,
    location: dict[str, Any],
    method: str,
    confidence: float,
) -> int:
    """对一段文本做字段扫描，产出证据与字段记录。返回非空单元数。"""
    text = text.strip()
    if not text:
        return 0
    hits = scan_inline(text)
    for hit in hits:
        if doc.category == "bid":
            # 本版业务口径：技术标和一览表不识别主体/联系人信息；未知分类也不
            # 猜测为商务标。公共项目字段只接受封面页（商务标/封面分类的第 1 页）。
            subtype = getattr(doc, "bid_subtype", "unknown")
            if subtype in ("technical", "bid_schedule", "unknown"):
                continue
            if hit.field in {"project_name", "project_code", "tenderer", "bid_date"}:
                page = location.get("page")
                if page != 1 or not location.get("cover", False):
                    continue
        out_fields.append(
            _build_field_record(hit, builder, doc, dict(location), method=method, confidence=min(confidence, hit.confidence))
        )
    return 1


def _looks_like_cover(text: str) -> bool:
    """只把有封面语义的第一页当作公共项来源，避免授权书正文冒充封面。"""
    return (
        ("投标文件" in text or "投标书" in text or "封面" in text)
        and "授权委托书" not in text
    )


def _build_field_record(
    hit, builder: EvidenceBuilder, doc, location: dict[str, Any], method: str, confidence: float
) -> FieldRecord:
    field = hit.field
    value = hit.value
    digest: str | None = None
    normalized: str | None = None
    masked = value

    if field in ("legal_rep_id", "bid_agent_id", "id_number"):
        digest = builder.digest_of(value)
        masked = mask_id_number(value)
        method = method + "+id_digest"
        # note（标签上下文）可能包含完整证件号，一并脱敏
        if hit.context:
            hit.context = hit.context.replace(value, masked)
    elif field == "uscc":
        code, ok, status = validate_uscc(value)
        normalized = code if code else None
        masked = value
        if code and not ok:
            hit.context = (hit.context + "；" if hit.context else "") + f"统一社会信用代码格式异常（{status}）"
    elif field == "phone":
        main, ext = normalize_phone(value)
        if len(main) == 11 and main.startswith("1"):
            # 手机号属个人敏感信息：规范化值只存受控摘要，展示用掩码
            normalized = "P" + builder.digest_of(main)
            masked = main[:3] + "****" + main[7:]
            if hit.context:
                hit.context = hit.context.replace(value, masked).replace(main, masked)
        else:
            normalized = main + ("转" + ext if ext else "")
    elif field == "email":
        normalized = normalize_email(value)
    elif field == "address":
        normalized = normalize_address(value)
    elif field in ("project_name", "project_code", "tenderer", "bid_date"):
        normalized = value.replace(" ", "").strip()
        if field == "project_code":
            m = re.search(r"[A-Za-z0-9][A-Za-z0-9_\-/]{3,}", normalized)
            normalized = m.group(0) if m else normalized
    elif field in ("company_name",):
        normalized = normalize_company_name(value)
    else:
        normalized = value.replace(" ", "")

    low = confidence < LOW_CONFIDENCE_THRESHOLD
    source_type = "bid_document" if doc.category == "bid" else "procurement_document"
    ev = builder.add(
        source_type=source_type,
        document_id=doc.document_id,
        location=location,
        field=field,
        raw_value=masked,
        normalized_value=normalized,
        method=method,
        confidence=confidence,
        strength="A",
        note=hit.context or None,
        sha256=None,
    )
    return FieldRecord(
        document_id=doc.document_id,
        relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir,
        field=field,
        value_masked=masked,
        normalized=normalized,
        digest=digest,
        confidence=confidence,
        method=method,
        location=location,
        evidence_id=ev.evidence_id,
        low_confidence=low,
        category=doc.category,
    )


if __name__ == "__main__":
    app()
