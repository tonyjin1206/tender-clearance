#!/usr/bin/env python3
"""从已落盘的商务标解析快照提取报价线索，不重新打开 PDF、不调用 OCR。"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, write_json
from tc.models import EntitiesFile, InventoryFile
from tc.normalize import normalize_company_name
from tc.projio import EvidenceBuilder, ProjectError, ensure_output_dirs, load_project_config, load_run_info

app = typer.Typer(help="分析商务标报价线索")
_AMOUNT = re.compile(r"(?<![\d.])([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)(?![\d.])")


def _amounts(text: str) -> list[float]:
    out = []
    for m in _AMOUNT.finditer(text):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0 <= value < 1_000_000_000:
            out.append(value)
    return out


def _page_texts(project_dir: Path, doc) -> list[tuple[int, str]]:
    cache = project_dir / "output/cache/document" / f"{doc.sha256}.json"
    if not cache.exists():
        return []
    data = load_json(cache)
    parsed = ((data.get("parsed") or {}).get("value") or {})
    pages = parsed.get("pages") or []
    out = [(int(p.get("page", i + 1)), str(p.get("text") or "")) for i, p in enumerate(pages)]
    ocr_path = project_dir / "output/interim/ocr-results.json"
    if ocr_path.exists():
        for result in load_json(ocr_path).get("results", []):
            if result.get("document_id") != doc.document_id or result.get("status") != "succeeded":
                continue
            text = "\n".join(str(b.get("text") or "") for b in result.get("blocks", []))
            for index, (page, current) in enumerate(out):
                if page == result.get("page") and len(text) > len(current):
                    out[index] = (page, text)
    return out


def _sum_after(lines: list[str], start: int) -> float | None:
    """读取标题后的第一处“合计”数值，限制窗口避免串入下一节。"""
    for index in range(start, min(len(lines), start + 140)):
        if "合计" not in lines[index]:
            continue
        found: list[float] = []
        decimal_found: list[float] = []
        currency_found: list[float] = []
        for candidate in lines[index:index + 5]:
            values = _amounts(candidate)
            if values:
                found.extend(values)
                if "元" in candidate:
                    currency_found.extend(values)
            for match in re.findall(r"[0-9]+\.[0-9]+", candidate):
                decimal_found.append(float(match))
        if found:
            # SRM/扫描文本有时把“1 项 25.9 万元”拆成两行，取合计行窗口内
            # 最后一个金额，避免把数量误当成总价。
            return decimal_found[-1] if decimal_found else (currency_found[-1] if currency_found else None)
    return None


def build_analysis(project_dir: Path) -> dict:
    inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
    entities = EntitiesFile(**load_json(project_dir / "output/interim/entities.json"))
    sid_by_name = {normalize_company_name(s.declared_name or s.display_name): s.supplier_id for s in entities.suppliers}
    grouping_path = project_dir / "output/interim/supplier-grouping.json"
    group_by_doc: dict[str, str | None] = {}
    if grouping_path.exists():
        grouping = load_json(grouping_path)
        group_by_doc.update({
            doc_id: sid_by_name.get(normalize_company_name(g.get("supplier_name")))
            for g in grouping.get("groups", []) for doc_id in g.get("document_ids", [])
        })
    # 兼容已有“bids/<供应商目录>/”项目，不要求先经过平铺归组阶段。
    for doc in inventory.documents:
        if doc.document_id in group_by_doc or not doc.supplier_dir:
            continue
        group_by_doc[doc.document_id] = next(
            (s.supplier_id for s in entities.suppliers if s.directory_name == doc.supplier_dir), None
        )
    suppliers: dict[str, dict] = {}
    for doc in inventory.documents:
        if doc.category != "bid" or doc.bid_subtype != "business":
            continue
        supplier_id = group_by_doc.get(doc.document_id)
        if not supplier_id:
            continue
        entry = suppliers.setdefault(supplier_id, {"supplier_id": supplier_id, "documents": [], "totals": [], "components": []})
        entry["documents"].append(doc.relative_path)
        for page, text in _page_texts(project_dir, doc):
            lines = [re.sub(r"\s+", "", line) for line in text.splitlines() if line.strip()]
            page_head = "".join(lines[:12])
            if "投标一览表" in page_head or "投标分项报价" in page_head:
                for index, line in enumerate(lines):
                    if "合计" in line:
                        value = _sum_after(lines, index)
                        if value is not None:
                            entry["totals"].append({"amount": value, "page": page, "document": doc.relative_path, "document_id": doc.document_id})
                            break
            for label in ("易损件", "备品备件", "专用工具"):
                start = next((i for i, line in enumerate(lines) if label in line and "清单" in line), None)
                if start is None:
                    continue
                value = _sum_after(lines, start)
                if value is not None:
                    entry["components"].append({"name": label, "amount": value, "page": page, "document": doc.relative_path, "document_id": doc.document_id})
    for entry in suppliers.values():
        if entry["totals"]:
            counts = Counter(round(x["amount"], 6) for x in entry["totals"])
            chosen = counts.most_common(1)[0][0]
            entry["total"] = chosen
            entry["total_evidence"] = [x for x in entry["totals"] if round(x["amount"], 6) == chosen]
        else:
            entry["total"] = None
            entry["total_evidence"] = []
        unique = {}
        for item in entry["components"]:
            unique.setdefault(item["name"], item)
        entry["components"] = list(unique.values())
        entry["arithmetic_status"] = "已取得总价线索；完整分项合计未取得，不能闭合复核" if entry["total"] is not None else "未取得总价线索"
        entry["anomaly_status"] = "未取得足够可比单价和清单基准，不能认定超高价、超低价或不平衡报价"
    return {"schema_version": "tender-clearance.bid-analysis.v1", "suppliers": list(suppliers.values())}


@app.command()
def run(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    try:
        _out, interim = ensure_output_dirs(project_dir)
        cfg = load_project_config(project_dir)
        analysis = build_analysis(project_dir)
        builder = EvidenceBuilder(load_run_info(interim), cfg.id_digest_salt, cfg.redaction_mode)
        evidence_by_id = {x["evidence_id"]: x for x in load_json(interim / "evidence-content.json").get("evidence", [])} \
            if (interim / "evidence-content.json").exists() else {}
        for supplier in analysis["suppliers"]:
            ids: list[str] = []
            for item in [*supplier.get("total_evidence", []), *supplier.get("components", [])]:
                field = "bid.total_quote" if "name" not in item else f"bid.component.{item['name']}"
                ev = builder.add(
                    source_type="bid_document", document_id=item.get("document_id"),
                    location={"kind": "pdf_page", "page": item["page"]}, field=field,
                    raw_value=f"{item['amount']:.2f}", normalized_value=f"{item['amount']:.2f}",
                    method="derived_from_business_pdf_text", confidence=1.0, strength="B",
                    note="商务标报价分析阶段从已落盘 PDF 文本快照提取；未重新扫描文件",
                )
                evidence_by_id[ev.evidence_id] = ev.model_dump(mode="json")
                ids.append(ev.evidence_id)
            supplier["evidence_ids"] = sorted(set(ids))
        if evidence_by_id:
            write_json(interim / "evidence-content.json", {
                "run": load_run_info(interim).model_dump(mode="json"),
                "evidence": list(evidence_by_id.values()),
            })
        write_json(interim / "bid-analysis.json", analysis)
    except (ProjectError, FileNotFoundError, ValueError) as exc:
        typer.secho(f"[错误] 报价分析失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.secho("[OK] 商务标报价线索分析完成 → bid-analysis.json", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
