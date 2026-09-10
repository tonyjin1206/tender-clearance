#!/usr/bin/env python3
"""阶段二：提取文件属性与扫描设备线索。

输出每份文件的原始属性与分类（方案 6.2）：
- unique_id           明确的设备序列号 / 文件内部唯一 ID（可作 I 级候选线索）；
- limited_identifying 自定义作者、扫描仪型号、标题、公司等（单项 III 级，多项叠加 II 级）；
- common_or_modifiable 通用编辑工具、空白作者、创建时间、PDF 版本（仅 III 级线索）。

“设备码”只取文件中实际读出的序列号/唯一 ID，不得由相同型号推断。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc import parsers
from tc.canon import load_json, write_json
from tc.models import (
    DocumentMetadata,
    EvidenceFile,
    ExtractionAnomaly,
    InventoryFile,
    MetadataField,
    MetadataFile,
)
from tc.normalize import parse_pdf_date
from tc.projio import (
    EvidenceBuilder,
    ProjectError,
    ensure_output_dirs,
    load_project_config,
    load_run_info,
)

app = typer.Typer(help="提取文件属性与扫描线索（metadata.json + evidence-metadata.json）")

SERIAL_PATTERNS = [
    re.compile(r"(?:serial[-_ ]?(?:number|no\.?|n)?|imageuniqueid|bodyserialnumber)\s*[:=]\s*\"?([A-Za-z0-9][A-Za-z0-9\-_/]{3,40})\"?", re.IGNORECASE),
    re.compile(r"\bS/N\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-]{3,40})", re.IGNORECASE),
]


def classify(field: str, raw_value: str) -> str:
    key = field.lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1]
    if key in ("serialnumber", "imageuniqueid", "bodyserialnumber", "documentid", "internal_id"):
        return "unique_id"
    if key in ("creator", "producer", "appversion", "pdf_version", "createdate", "moddate", "created", "modified"):
        return "common_or_modifiable"
    return "limited_identifying"


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
) -> None:
    try:
        load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
    except ProjectError as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    _out, interim = ensure_output_dirs(project_dir)
    builder = EvidenceBuilder(load_run_info(interim), load_project_config(project_dir).id_digest_salt)

    documents: list[DocumentMetadata] = []
    anomalies: list[ExtractionAnomaly] = []

    for doc in inventory.documents:
        if doc.category == "external_evidence":
            continue
        if doc.extraction_status in ("empty", "password_protected", "corrupt", "unsupported", "skipped"):
            continue
        path = project_dir / doc.relative_path
        try:
            if doc.media_type == "application/pdf":
                dm, an = _pdf_metadata(path, doc, builder)
            elif doc.media_type.endswith("wordprocessingml.document"):
                dm, an = _docx_metadata(path, doc, builder)
            elif doc.media_type.endswith("spreadsheetml.sheet"):
                dm, an = _xlsx_metadata(path, doc, builder)
            elif doc.media_type.startswith("image/"):
                dm, an = _image_metadata(path, doc, builder)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            dm = DocumentMetadata(document_id=doc.document_id, relative_path=doc.relative_path, fields=[])
            an = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                    anomaly="metadata_failed", detail=str(exc)[:300])]
        documents.append(dm)
        anomalies.extend(an)

    result = MetadataFile(run=inventory.run, documents=documents, anomalies=anomalies)
    write_json(interim / "metadata.json", result.model_dump(mode="json"))
    ev = EvidenceFile(run=inventory.run, evidence=builder.items)
    write_json(interim / "evidence-metadata.json", ev.model_dump(mode="json"))
    n_unique = sum(1 for d in documents for f in d.fields if f.info_class == "unique_id")
    typer.secho(
        f"[OK] 属性提取完成：{len(documents)} 个文档，唯一标识线索 {n_unique} 条 → metadata.json",
        fg=typer.colors.GREEN,
    )


def _add(builder: EvidenceBuilder, doc, location: dict, field: str, raw_value: str, note: str | None = None):
    return builder.add(
        source_type="metadata",
        document_id=doc.document_id,
        location=location,
        field=field,
        raw_value=str(raw_value),
        method=location.get("kind", "metadata"),
        confidence=1.0,
        strength="A",
        note=note,
    )


def _field(builder: EvidenceBuilder, doc, field: str, raw_value: str, location: dict, note: str | None = None) -> MetadataField:
    ev = _add(builder, doc, location, field, raw_value, note)
    return MetadataField(
        field=field,
        raw_value=str(raw_value),
        info_class=classify(field, str(raw_value)),  # type: ignore[arg-type]
        evidence_id=ev.evidence_id,
        note=note,
    )


def _pdf_metadata(path: Path, doc, builder: EvidenceBuilder) -> tuple[DocumentMetadata, list[ExtractionAnomaly]]:
    parsed = parsers.parse_pdf(path)
    fields: list[MetadataField] = []
    anomalies = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                   anomaly=iss.kind, detail=iss.detail) for iss in parsed.issues]
    md = parsed.metadata
    prop_map = {
        "creator": "creator", "producer": "producer", "author": "author", "title": "title",
        "subject": "subject", "keywords": "keywords",
    }
    for key, name in prop_map.items():
        if md.get(key):
            fields.append(_field(builder, doc, f"pdf.{name}", md[key], {"kind": "pdf_metadata", "property": name}))
    for key in ("creationDate", "modDate"):
        if md.get(key):
            dt = parse_pdf_date(md[key])
            fields.append(_field(
                builder, doc, f"pdf.{key}", md[key],
                {"kind": "pdf_metadata", "property": key},
                note=None if dt else "无法解析为标准时间格式",
            ))
    # XMP：序列号/唯一 ID 与可读设备信息
    if parsed.xmp_xml:
        for rx in SERIAL_PATTERNS:
            for m in rx.finditer(parsed.xmp_xml):
                fields.append(_field(
                    builder, doc, "xmp.serial_number", m.group(1),
                    {"kind": "pdf_xmp", "match": m.group(0)[:80]},
                    note="文件内部读取的设备/文档唯一标识",
                ))
                break
        for xmp_key in ("xmp:CreatorTool", "pdf:Producer", "xmpMM:DocumentID", "stEvt:instanceID"):
            m = re.search(re.escape(xmp_key) + r">\s*([^<\s][^<]{0,200})", parsed.xmp_xml)
            if not m:
                m = re.search(re.escape(xmp_key) + r'="([^"\s][^"]{0,200})', parsed.xmp_xml)
            if m:
                cls = "unique_id" if "DocumentID" in xmp_key or "instanceID" in xmp_key else None
                f = _field(builder, doc, f"xmp.{xmp_key}", m.group(1), {"kind": "pdf_xmp", "property": xmp_key})
                if cls:
                    f.info_class = "unique_id"  # type: ignore[assignment]
                fields.append(f)
    # 嵌入文件
    for emb in parsed.embedded_files:
        fields.append(_field(builder, doc, "pdf.embedded_file", emb["name"],
                             {"kind": "pdf_embedded_file", "name": emb["name"], "sha256": emb["sha256"]},
                             note=f"内嵌文件 sha256={emb['sha256'][:12]}"))
    # 表单字段
    for ff in parsed.form_fields:
        fields.append(_field(builder, doc, "pdf.form_field", f"{ff['name']}={ff['value']}",
                             {"kind": "pdf_form", "page": ff["page"], "name": ff["name"]}))
    # 扫描页图像：EXIF 设备线索 + 指纹
    for img in parsed.images:
        loc = {"kind": "image_exif", "page": img.page, "image_index": img.index, "image_sha256": img.sha256}
        for name in ("Make", "Model", "Software", "Artist", "HostComputer", "BodySerialNumber", "ImageUniqueID", "DateTimeOriginal"):
            if name in img.exif:
                cls_field = "exif." + name
                f = _field(builder, doc, cls_field, img.exif[name], loc,
                           note="扫描图像 EXIF 属性" if name not in ("BodySerialNumber", "ImageUniqueID")
                           else "扫描图像中读取的设备唯一标识")
                fields.append(f)
    fingerprints = [f"{img.page}:{img.ahash}" for img in parsed.images if img.ahash]
    return DocumentMetadata(
        document_id=doc.document_id,
        relative_path=doc.relative_path,
        fields=fields,
        page_image_fingerprints=fingerprints,
    ), anomalies


def _docx_metadata(path: Path, doc, builder: EvidenceBuilder) -> tuple[DocumentMetadata, list[ExtractionAnomaly]]:
    parsed = parsers.parse_docx(path)
    fields: list[MetadataField] = []
    anomalies = [ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                   anomaly=iss.kind, detail=iss.detail) for iss in parsed.issues]
    for k, v in parsed.core_properties.items():
        fields.append(_field(builder, doc, f"docx.core.{k}", v, {"kind": "docx_property", "property": k}))
    for k, v in parsed.app_properties.items():
        fields.append(_field(builder, doc, f"docx.app.{k}", v, {"kind": "docx_property", "property": k}))
    for k, v in parsed.custom_properties.items():
        fields.append(_field(builder, doc, f"docx.custom.{k}", v, {"kind": "docx_property", "property": k},
                             note="自定义属性（有限辨识信息）"))
    return DocumentMetadata(document_id=doc.document_id, relative_path=doc.relative_path, fields=fields), anomalies


def _xlsx_metadata(path: Path, doc, builder: EvidenceBuilder) -> tuple[DocumentMetadata, list[ExtractionAnomaly]]:
    import openpyxl

    fields: list[MetadataField] = []
    anomalies: list[ExtractionAnomaly] = []
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True)
        props = wb.properties
        for k in ("creator", "lastModifiedBy", "title", "subject", "description", "keywords", "category",
                  "company", "lastPrinted", "created", "modified", "revision"):
            v = getattr(props, k, None)
            if v:
                fields.append(_field(builder, doc, f"xlsx.core.{k}", str(v), {"kind": "docx_property", "property": k}))
        wb.close()
    except Exception as exc:  # noqa: BLE001
        anomalies.append(ExtractionAnomaly(document_id=doc.document_id, relative_path=doc.relative_path,
                                           anomaly="metadata_failed", detail=f"XLSX 属性读取失败：{exc}"))
    return DocumentMetadata(document_id=doc.document_id, relative_path=doc.relative_path, fields=fields), anomalies


def _image_metadata(path: Path, doc, builder: EvidenceBuilder) -> tuple[DocumentMetadata, list[ExtractionAnomaly]]:
    img_desc = parsers._describe_image(path.read_bytes(), path.suffix.lstrip("."), 1, 0)
    fields: list[MetadataField] = []
    for name, v in img_desc.exif.items():
        fields.append(_field(builder, doc, f"exif.{name}", v, {"kind": "image_exif", "file": doc.relative_path}))
    return DocumentMetadata(document_id=doc.document_id, relative_path=doc.relative_path, fields=fields), []


if __name__ == "__main__":
    app()
