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
import hashlib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc import parsers
from tc.canon import load_json, write_json
from tc.fields import extract_from_cells, scan_inline, scan_ocr_blocks
from tc.models import (
    ContentFile,
    DocumentContent,
    Evidence,
    ExtractionAnomaly,
    FieldRecord,
    InventoryFile,
    OCRJob,
    OCRJobFile,
    OCRProviderInfo,
    OCRResult,
    OCRResultFile,
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
    document_cache_key,
    ensure_output_dirs,
    load_project_config,
    load_run_info,
)
from tc.ocr_contract import job_for_page, safe_input_ref

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
    build_content(project_dir)


def build_content(project_dir: Path) -> ContentFile:
    """生成基础文本/字段候选，并合并已验证的宿主 OCR 结果。

    该函数供 CLI 和 `import_ocr_results.py` 复用；不会联网、安装依赖或调用 OCR 引擎。
    """
    cfg = load_project_config(project_dir)
    interim = project_dir / "output/interim"
    inventory = InventoryFile(**load_json(interim / "inventory.json"))
    out_dir, interim = ensure_output_dirs(project_dir)
    builder = EvidenceBuilder(load_run_info(interim), cfg.id_digest_salt, cfg.redaction_mode)
    imported_ocr = _load_ocr_results(interim)

    doc_contents: list[DocumentContent] = []
    fields: list[FieldRecord] = []
    anomalies: list[ExtractionAnomaly] = []
    jobs: list[OCRJob] = []
    generated_results: list[OCRResult] = []

    for doc in inventory.documents:
        if doc.category == "external_evidence":
            continue  # 外部证据由 import_external_evidence.py 处理
        if doc.extraction_status in ("empty", "password_protected", "corrupt", "unsupported", "skipped"):
            continue  # 清单中已记录原因
        if doc.category == "bid" and doc.bid_subtype == "technical":
            # 技术标通常占项目体量绝大部分；其正文不参与本版主体/公共字段识别，
            # 文件名已判定为 technical 时直接跳过正文读取、图片枚举和 OCR 任务。
            doc_contents.append(DocumentContent(
                document_id=doc.document_id,
                relative_path=doc.relative_path,
                supplier_dir=doc.supplier_dir,
                status="skipped",
                issues=["文件名识别为技术标，按性能策略跳过正文扫描；仅保留盘点结果"],
            ))
            continue
        path = project_dir / doc.relative_path
        try:
            cache_key = document_cache_key(doc, cfg, "content")
            cached = _load_document_cache(project_dir, doc.sha256, cache_key)
            if cached:
                dc = DocumentContent(**cached["content"])
                fr = [FieldRecord(**x) for x in cached.get("fields", [])]
                an = [ExtractionAnomaly(**x) for x in cached.get("anomalies", [])]
                parsed_document = parsers.deserialize_parsed(cached.get("parsed"))
                for evidence in cached.get("evidence", []):
                    builder.extend([Evidence(**evidence)])
                jobs.extend(OCRJob(**x) for x in cached.get("ocr_jobs", []))
            elif doc.media_type == "application/pdf":
                parsed_document = parsers.deserialize_parsed(_load_document_parsed(project_dir, doc.sha256))
                dc, fr, an, new_jobs, mock_results, parsed_document = _extract_pdf(
                    path, doc, cfg, builder, project_dir, parsed=parsed_document
                )
                jobs.extend(new_jobs)
                generated_results.extend(mock_results)
            elif doc.media_type.endswith("wordprocessingml.document"):
                parsed_document = parsers.deserialize_parsed(_load_document_parsed(project_dir, doc.sha256))
                dc, fr, an, parsed_document = _extract_docx(path, doc, builder, parsed=parsed_document)
            elif doc.media_type.endswith("spreadsheetml.sheet"):
                parsed_document = parsers.deserialize_parsed(_load_document_parsed(project_dir, doc.sha256))
                dc, fr, an, parsed_document = _extract_xlsx(path, doc, builder, parsed=parsed_document)
            elif doc.media_type.startswith("image/"):
                dc = DocumentContent(
                    document_id=doc.document_id, relative_path=doc.relative_path,
                    supplier_dir=doc.supplier_dir, status="partial", issues=["纯图片文件：无文本层，需宿主 OCR 或人工核对"],
                )
                fr, an = [], []
                parsed_document = None
                jobs.append(job_for_page(
                    document_id=doc.document_id, source_sha256=doc.sha256, page=1,
                    page_image_sha256=doc.sha256, input_ref=safe_input_ref(project_dir, path, 1),
                ))
            else:
                dc, fr, an = _extract_text(path, doc, builder)
                parsed_document = None
            if not cached:
                _write_document_cache(
                    project_dir, doc.sha256, dc, fr, an, builder,
                    jobs_for_doc(jobs, doc.document_id), parsed_document, cache_key=cache_key,
                )
        except Exception as exc:  # noqa: BLE001 - 任何解析异常都不应中断整体
            dc = DocumentContent(document_id=doc.document_id, relative_path=doc.relative_path,
                                 supplier_dir=doc.supplier_dir, status="failed", issues=[f"提取失败：{exc}"])
            fr, an = [], [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                            anomaly="extraction_failed", detail=str(exc)[:300])]
        doc_contents.append(dc)
        fields.extend(fr)
        anomalies.extend(an)

    # 只消费已经验证并落盘的 OCR 结果；结果状态为失败/阻断时也要显式保留缺口。
    for result in [*imported_ocr, *generated_results]:
        job = next((j for j in jobs if j.job_id == result.job_id), None)
        if job is None:
            anomalies.append(ExtractionAnomaly(anomaly="ocr_result_invalid", detail=f"结果 {result.job_id} 无对应任务"))
            continue
        doc = next((d for d in inventory.documents if d.document_id == result.document_id), None)
        dc = next((d for d in doc_contents if d.document_id == result.document_id), None)
        if doc is None or dc is None:
            continue
        if result.status != "succeeded":
            dc.status = "partial"
            dc.issues.append(f"ocr_{result.status}: 第 {result.page} 页：{result.detail or '未返回结果'}")
            anomalies.append(ExtractionAnomaly(
                document_id=doc.document_id, relative_path=doc.relative_path,
                anomaly="ocr_unavailable", detail=f"第 {result.page} 页 OCR 状态={result.status}：{result.detail or ''}",
            ))
            continue
        dc.ocr_pages.append(result.page)
        dc.scanned_pages = sorted(set(dc.scanned_pages))
        fields.extend(_scan_ocr_result(result, builder, doc, dc))

    # 未有结果的扫描页继续保留 ocr_unavailable；已有成功结果的页不重复报缺口。
    result_pages = {(r.document_id, r.page) for r in [*imported_ocr, *generated_results]}
    for dc in doc_contents:
        doc = next((d for d in inventory.documents if d.document_id == dc.document_id), None)
        if not doc:
            continue
        for page in dc.scanned_pages:
            if (dc.document_id, page) not in result_pages:
                anomaly = ExtractionAnomaly(
                    document_id=dc.document_id, relative_path=dc.relative_path,
                    anomaly="ocr_unavailable", detail=f"第 {page} 页为扫描页，尚未导入宿主 OCR 结果",
                )
                if not any(a.document_id == anomaly.document_id and a.detail == anomaly.detail for a in anomalies):
                    anomalies.append(anomaly)
                dc.status = "partial"

    result = ContentFile(
        run=inventory.run, documents=doc_contents, fields=fields, anomalies=anomalies
    )
    write_json(interim / "content.json", result.model_dump(mode="json"))
    ev = {"run": inventory.run.model_dump(mode="json"), "evidence": [e.model_dump(mode="json") for e in builder.items]}
    write_json(interim / "evidence-content.json", ev)
    write_json(interim / "ocr-jobs.json", OCRJobFile(run=inventory.run, jobs=sorted({j.job_id: j for j in jobs}.values(), key=lambda j: j.job_id)).model_dump(mode="json"))
    existing = {r.job_id: r for r in imported_ocr}
    existing.update({r.job_id: r for r in generated_results})
    write_json(interim / "ocr-results.json", OCRResultFile(
        run=inventory.run, results=sorted(existing.values(), key=lambda r: r.job_id)
    ).model_dump(mode="json"))
    typer.secho(
        f"[OK] 内容提取完成：{len(doc_contents)} 个文档，{len(fields)} 个字段候选，"
        f"{len(anomalies)} 条异常 → content.json",
        fg=typer.colors.GREEN,
    )
    return result


def tyver_exit_code():
    raise typer.Exit(code=2)


def _page_image_hash(parsed: ParsedPdf, page: int, source_sha256: str) -> str:
    hashes = [img.sha256 for img in parsed.images if img.page == page]
    if hashes:
        return hashlib.sha256("|".join(sorted(hashes)).encode("ascii")).hexdigest()
    return hashlib.sha256(f"{source_sha256}:page:{page}".encode("ascii")).hexdigest()


def _make_mock_result(job: OCRJob, text: str, confidence: float) -> OCRResult:
    """只供虚构夹具使用的确定性 Provider 结果，不代表生产 OCR 实现。"""
    from datetime import datetime, timezone

    from tc.models import OCRBlock

    return OCRResult(
        contract_version="ocr-result.v1",
        job_id=job.job_id,
        document_id=job.document_id,
        source_sha256=job.source_sha256,
        page=job.page,
        status="succeeded",
        provider=OCRProviderInfo(
            id="fixture-mock-ocr",
            version="1.0",
            engine="fixture",
            models=["fixture-xmp"],
            processing_location="local",
        ),
        average_confidence=confidence,
        location_precision="page_only",
        blocks=[OCRBlock(text=text, confidence=confidence)],
        completed_at=datetime.now(timezone.utc),
    )


def _load_ocr_results(interim: Path) -> list[OCRResult]:
    path = interim / "ocr-results.json"
    if not path.exists():
        return []
    try:
        return OCRResultFile(**load_json(path)).results
    except Exception:
        # 严格导入由 import_ocr_results.py 负责；直接运行 Core 时不把损坏结果当事实。
        return []


def jobs_for_doc(jobs: list[OCRJob], document_id: str) -> list[OCRJob]:
    return [j for j in jobs if j.document_id == document_id]


def _load_document_cache(project_dir: Path, source_sha256: str, cache_key: str) -> dict[str, Any] | None:
    path = project_dir / "output/cache/document" / f"{source_sha256}.json"
    if not path.exists():
        return None
    try:
        data = load_json(path)
        if (data.get("source_sha256") != source_sha256
                or data.get("content_cache_key") != cache_key
                or "content" not in data):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _write_document_cache(
    project_dir: Path,
    source_sha256: str,
    content: DocumentContent,
    fields: list[FieldRecord],
    anomalies: list[ExtractionAnomaly],
    builder: EvidenceBuilder,
    jobs: list[OCRJob],
    parsed_document: Any | None = None,
    *,
    cache_key: str,
) -> None:
    path = project_dir / "output/cache/document" / f"{source_sha256}.json"
    prior: dict[str, Any] = {}
    if path.exists():
        try:
            prior = load_json(path)
        except (OSError, ValueError):
            prior = {}
    document_id = content.document_id
    evidence = [e.model_dump(mode="json") for e in builder.items if e.document_id == document_id]
    payload = {
        "source_sha256": source_sha256,
        "content_cache_key": cache_key,
        "content": content.model_dump(mode="json"),
        "fields": [f.model_dump(mode="json") for f in fields],
        "anomalies": [a.model_dump(mode="json") for a in anomalies],
        "evidence": evidence,
        "ocr_jobs": [j.model_dump(mode="json") for j in jobs],
    }
    if parsed_document is not None:
        payload["parsed"] = parsers.serialize_parsed(parsed_document)
    # metadata.py may add its own cached section later; never discard it here.
    for key in ("metadata", "metadata_cache_key", "metadata_evidence", "metadata_anomalies"):
        if key in prior:
            payload[key] = prior[key]
    write_json(path, payload)


def _scan_ocr_result(result: OCRResult, builder: EvidenceBuilder, doc, dc: DocumentContent) -> list[FieldRecord]:
    blocks = [b.model_dump(mode="json") for b in result.blocks]
    hits = scan_ocr_blocks(
        blocks,
        average_confidence=result.average_confidence,
        location_precision=result.location_precision,
    )
    out: list[FieldRecord] = []
    for hit in hits:
        location: dict[str, Any] = {
            "kind": "pdf_page" if doc.media_type == "application/pdf" else "image_page",
            "page": result.page,
            "ocr": True,
            "ocr_job_id": result.job_id,
            "provider_id": result.provider.id,
            "provider_version": result.provider.version,
            "model_names": result.provider.models,
            "inference_engine": result.provider.engine,
            "processing_location": result.provider.processing_location,
            "location_precision": result.location_precision,
            "cover": result.page == 1 and _looks_like_cover("\n".join(b["text"] for b in blocks)),
        }
        out.append(_build_field_record(
            hit, builder, doc, location, method="ocr_host", confidence=min(hit.confidence, result.average_confidence or 0.0)
        ))
    return out


# --------------------------------------------------------------------- PDF


def _load_document_parsed(project_dir: Path, source_sha256: str) -> dict[str, Any] | None:
    path = project_dir / "output/cache/document" / f"{source_sha256}.json"
    if not path.exists():
        return None
    try:
        data = load_json(path)
        return data.get("parsed") if data.get("source_sha256") == source_sha256 else None
    except (OSError, ValueError, TypeError):
        return None


def _extract_pdf(
    path: Path,
    doc,
    cfg,
    builder: EvidenceBuilder,
    project_dir: Path,
    *,
    parsed: ParsedPdf | None = None,
):
    parsed = parsed or parsers.parse_pdf(path, ocr_provider="mock" if cfg.ocr_provider == "mock" else "none")
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    fields: list[FieldRecord] = []
    anomalies: list[ExtractionAnomaly] = []
    jobs: list[OCRJob] = []
    mock_results: list[OCRResult] = []
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
                page_image_hash = _page_image_hash(parsed, pno, doc.sha256)
                job = job_for_page(
                    document_id=doc.document_id, source_sha256=doc.sha256, page=pno,
                    page_image_sha256=page_image_hash, input_ref=safe_input_ref(project_dir, path, pno),
                )
                jobs.append(job)
                if cfg.ocr_provider == "mock":
                    ocr = parsers.get_mock_ocr_text(parsed.xmp_xml, pno)
                    if ocr:
                        text, conf = ocr
                        mock_results.append(_make_mock_result(job, text, conf))
        dc.text_units += text_units
    return dc, fields, anomalies, jobs, mock_results, parsed


# --------------------------------------------------------------------- DOCX


def _extract_docx(path: Path, doc, builder: EvidenceBuilder, *, parsed: ParsedDocx | None = None):
    parsed = parsed or parsers.parse_docx(path)
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
    return dc, fields, anomalies, parsed


# --------------------------------------------------------------------- XLSX


def _extract_xlsx(path: Path, doc, builder: EvidenceBuilder, *, parsed: ParsedXlsx | None = None):
    parsed = parsed or parsers.parse_xlsx(path)
    dc = DocumentContent(
        document_id=doc.document_id, relative_path=doc.relative_path,
        supplier_dir=doc.supplier_dir, status="ok",
    )
    fields: list[FieldRecord] = []
    anomalies = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                   anomaly=iss.kind, detail=iss.detail) for iss in parsed.issues]
    if doc.category == "bid" and getattr(doc, "bid_subtype", "unknown") == "bid_schedule":
        # 一览表只盘点和保留文件证据，不识别公共项或供应商主体字段。
        return dc, fields, anomalies, parsed
    for sheet in parsed.sheets:
        dc.text_units += len(sheet["cells"])
        hits = extract_from_cells(sheet["cells"], sheet["name"])
        for hit in hits:
            location = {"kind": "xlsx_cell", "sheet": sheet["name"],
                        "cell": hit.context.split("→")[-1] if "→" in hit.context else hit.context}
            fields.append(_build_field_record(hit, builder, doc, location, method="xlsx_cell", confidence=hit.confidence))
    return dc, fields, anomalies, parsed


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
        masked = value if builder.redaction_mode == "none" else mask_id_number(value)
        method = method + "+id_digest"
        # note（标签上下文）可能包含完整证件号，一并脱敏
        if hit.context and builder.redaction_mode != "none":
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
            masked = main if builder.redaction_mode == "none" else main[:3] + "****" + main[7:]
            if hit.context and builder.redaction_mode != "none":
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
