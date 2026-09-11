#!/usr/bin/env python3
"""阶段一：创建项目清单与文件哈希。

递归盘点 bids/、procurement/、external-evidence/ 下的输入文件，
生成文件清单、MIME 检测、大小、页数、SHA-256 与读取异常。
原始标书只读，不做任何写回。
"""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc import parsers
from tc.canon import file_sha256, stable_id, write_json
from tc.models import (
    DocumentRecord,
    ExtractionAnomaly,
    InventoryFile,
    SourceId,
)
from tc.parsers import ParseIssue
from tc.projio import (
    ProjectError,
    ensure_output_dirs,
    load_project_config,
    make_run_info,
)

app = typer.Typer(help="盘点项目输入文件并生成清单（inventory.json）")

CATEGORY_DIRS = {
    "bids": "bid",
    "procurement": "procurement",
    "external-evidence": "external_evidence",
}
EXTERNAL_SOURCE_DIRS: dict[str, SourceId] = {
    "government-procurement": "government_procurement",
    "srm-authorized-export": "srm",
}


def _light_probe(path: Path, media_type: str) -> tuple[int | None, str, str | None]:
    """返回 (页数, 状态, 状态说明)。只做只读探测，不修复、不破解。"""
    if path.stat().st_size == 0:
        return None, "empty", "零字节文件"
    if media_type == "application/pdf":
        return _probe_pdf(path)
    if path.suffix.lower() in (".docx", ".xlsx"):
        return _probe_ooxml(path)
    if media_type.startswith("image/"):
        try:
            from PIL import Image

            with Image.open(path) as img:
                img.verify()
            return None, "ok", None
        except Exception as exc:  # noqa: BLE001
            return None, "corrupt", f"图片无法读取：{exc}"
    return None, "ok", None


def _probe_pdf(path: Path) -> tuple[int | None, str, str | None]:
    from tc.parsers import import_fitz

    fitz = import_fitz()
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        kind = "password_protected" if ("password" in msg or "encryption" in msg) else "corrupt"
        detail = str(exc).replace(str(path), path.name)
        return None, kind, f"PDF 打开失败：{detail}"
    try:
        if doc.needs_pass:
            return None, "password_protected", "PDF 受密码保护"
        return int(doc.page_count), "ok", None
    except Exception as exc:  # noqa: BLE001
        return None, "corrupt", f"PDF 读取失败：{exc}"
    finally:
        doc.close()


def _probe_ooxml(path: Path) -> tuple[int | None, str, str | None]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        if path.suffix.lower() == ".docx" and "word/document.xml" not in names:
            return None, "corrupt", "缺少 word/document.xml"
        if path.suffix.lower() == ".xlsx" and "xl/workbook.xml" not in names:
            return None, "corrupt", "缺少 xl/workbook.xml"
        return None, "ok", None
    except zipfile.BadZipFile as exc:
        return None, "corrupt", f"OOXML 容器损坏：{exc}"


def _classify_bid_document(path: Path, media_type: str) -> str:
    """按文件名和格式做保守分类；分类不替代人工确认。"""
    name = path.name.lower()
    if path.suffix.lower() in (".xlsx", ".xls") or "一览" in name or "报价" in name:
        return "bid_schedule"
    if "技术" in name or "technical" in name or "tech" in name:
        return "technical"
    if "封面" in name or "cover" in name:
        return "cover"
    if ("商务" in name or "business" in name or "授权" in name or "营业执照" in name
            or "声明" in name or "扫描" in name):
        return "business"
    # 未能从文件名确认时，保守标为 unknown；后续不得按商务标抽取主体字段。
    return "unknown"


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
    except ProjectError as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    out_dir, interim = ensure_output_dirs(project_dir)
    run_info = make_run_info(cfg, rules_version=_load_rules_version())

    documents: list[DocumentRecord] = []
    anomalies: list[ExtractionAnomaly] = []

    for dir_name, category in CATEGORY_DIRS.items():
        base = project_dir / dir_name
        if not base.exists():
            anomalies.append(
                ExtractionAnomaly(anomaly="missing_directory", detail=f"缺少输入目录 {dir_name}/（如属正常可忽略）")
            )
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(project_dir).as_posix()
            media = parsers.detect_media_type(path)
            size = path.stat().st_size
            sha = file_sha256(path)
            supplier_dir = None
            if category == "bid":
                try:
                    supplier_dir = path.relative_to(base).parts[0]
                except IndexError:
                    supplier_dir = None
                    anomalies.append(
                        ExtractionAnomaly(relative_path=rel, anomaly="bid_file_without_supplier_dir", detail="bids/ 根下的文件未归入任何供应商目录")
                    )
            page_count, status, detail = _light_probe(path, media)
            documents.append(
                DocumentRecord(
                    document_id=stable_id("DOC", {"path": rel, "sha256": sha}),
                    relative_path=rel,
                    supplier_dir=supplier_dir,
                    category=category,  # type: ignore[arg-type]
                    bid_subtype=_classify_bid_document(path, media) if category == "bid" else "unknown",
                    media_type=media,
                    size_bytes=size,
                    page_count=page_count,
                    sha256=sha,
                    extraction_status=status,  # type: ignore[arg-type]
                    status_detail=detail,
                    modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                )
            )

    supplier_dirs = sorted(
        {d.supplier_dir for d in documents if d.category == "bid" and d.supplier_dir}
    )
    for mapped in cfg.supplier_directory_mapping:
        if mapped not in supplier_dirs:
            anomalies.append(
                ExtractionAnomaly(anomaly="mapping_mismatch", detail=f"supplier_directory_mapping 中的目录“{mapped}”不存在于 bids/")
            )
    if not supplier_dirs:
        anomalies.append(ExtractionAnomaly(anomaly="no_supplier_directories", detail="bids/ 下未发现供应商目录"))

    # 外部证据子目录 → source_id 的映射只做提示性检查
    ext_base = project_dir / "external-evidence"
    if ext_base.exists():
        for sub in sorted(p for p in ext_base.iterdir() if p.is_dir()):
            if sub.name not in EXTERNAL_SOURCE_DIRS and not sub.name.startswith("."):
                anomalies.append(
                    ExtractionAnomaly(anomaly="unknown_external_source_dir", detail=f"external-evidence/{sub.name} 不在标准渠道目录内，导入时按 other 处理")
                )

    result = InventoryFile(
        run=run_info, documents=documents, anomalies=anomalies, supplier_dirs=supplier_dirs
    )
    write_json(interim / "inventory.json", result.model_dump(mode="json"))
    typer.secho(
        f"[OK] 盘点完成：{len(documents)} 个文件，{len(supplier_dirs)} 家供应商目录，"
        f"{len(anomalies)} 条异常 → {interim / 'inventory.json'}",
        fg=typer.colors.GREEN,
    )
    bad = [d for d in documents if d.extraction_status not in ("ok",)]
    if bad:
        for d in bad:
            typer.secho(f"  [异常] {d.relative_path}: {d.extraction_status} — {d.status_detail or ''}", fg=typer.colors.YELLOW)


def _load_rules_version() -> str:
    import yaml

    rules_path = Path(__file__).resolve().parent.parent / "rules" / "risk-rules.yaml"
    try:
        data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        return str(data.get("version", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    app()
