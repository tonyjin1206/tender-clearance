#!/usr/bin/env python3
"""生成供应商身份快速结果，供全文 OCR 完成前启动 SRM 分支。

本脚本只读取已生成的 inventory/content 和可选 supplier-grouping，不联网、不登录、
不修改原始标书。它输出的是候选身份，不替代正式 normalize_and_match 的主体门禁。
宿主可在导入一批 priority=identity_fast 的 OCR 结果后调用本脚本，随后按候选
逐供应商启动 SRM 查询；全文 OCR 仍需继续完成，正式报告仍受完整 OCR 门禁控制。
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, stable_id, write_json
from tc.models import ContentFile, InventoryFile
from tc.normalize import normalize_company_name
from tc.projio import ProjectError, ensure_output_dirs

app = typer.Typer(help="生成供应商身份快速结果，不触发 SRM 查询")

_COMPANY_RE = re.compile(r"[一-龥A-Za-z0-9（）()]{4,80}(?:股份有限公司|有限责任公司|有限公司|集团公司|分公司|支公司)$")


def _clean(value: str) -> str:
    value = re.sub(r"\s+", "", str(value or ""))
    return re.sub(r"[（(](?:公章|盖章|签章)[）)]$", "", value).strip()


def build_identity(project_dir: Path) -> dict:
    interim = project_dir / "output/interim"
    inventory = InventoryFile(**load_json(interim / "inventory.json"))
    content = ContentFile(**load_json(interim / "content.json"))

    document_groups: dict[str, str] = {}
    grouping_data: dict = {}
    grouping_path = interim / "supplier-grouping.json"
    if grouping_path.exists():
        try:
            grouping = load_json(grouping_path)
            grouping_data = grouping if isinstance(grouping, dict) else {}
            document_groups = {
                str(item["document_id"]): str(item["group_id"])
                for item in grouping.get("documents", [])
                if item.get("status") == "assigned" and item.get("group_id")
            }
        except (OSError, ValueError, TypeError):
            document_groups = {}

    documents = {doc.document_id: doc for doc in inventory.documents}
    buckets: dict[str, dict] = defaultdict(lambda: {
        "names": set(), "uscc": set(), "document_ids": set(), "evidence_ids": set(),
    })
    doc_name_keys: dict[str, set[str]] = defaultdict(set)
    for field in content.fields:
        if field.field != "company_name" or field.low_confidence:
            continue
        value = _clean(field.normalized or field.value_masked)
        if not value:
            continue
        if not _COMPANY_RE.fullmatch(value):
            continue
        # 身份快速结果只接受封面/商务身份页，避免把正文中的甲方、设备厂商
        # 当成投标人主体。
        doc = documents.get(field.document_id)
        is_business = bool(doc and doc.bid_subtype in {"business", "cover"})
        is_cover = bool(field.location.get("cover") and field.location.get("page") == 1)
        if not is_business and not is_cover:
            continue
        key = normalize_company_name(value)
        bucket = buckets[key]
        bucket["names"].add(value)
        bucket["document_ids"].add(field.document_id)
        bucket["evidence_ids"].add(field.evidence_id)
        doc_name_keys[field.document_id].add(key)

    # 同一身份页上的代码归入该页已有的公司名候选；没有同页名称时不猜归属。
    for field in content.fields:
        if field.field != "uscc" or field.low_confidence:
            continue
        value = _clean(field.normalized or field.value_masked)
        if not value:
            continue
        for key in doc_name_keys.get(field.document_id, set()):
            buckets[key]["uscc"].add(value.upper())
            buckets[key]["evidence_ids"].add(field.evidence_id)

    candidates = []
    resolved_groups = grouping_data.get("groups", []) if grouping_data.get("status") == "resolved" else []
    if resolved_groups:
        # 归组结果是平铺上传场景的上游身份边界；优先使用它，避免商务正文中
        # 出现的甲方、设备厂商或合作方名称被误当成投标人。
        fields_by_doc = defaultdict(list)
        for field in content.fields:
            fields_by_doc[field.document_id].append(field)
        for group in sorted(resolved_groups, key=lambda item: str(item.get("group_id", ""))):
            doc_ids = {str(value) for value in group.get("document_ids", [])}
            codes = sorted({
                _clean(field.normalized or field.value_masked).upper()
                for doc_id in doc_ids for field in fields_by_doc.get(doc_id, [])
                if field.field == "uscc" and not field.low_confidence and _clean(field.normalized or field.value_masked)
            })
            name = _clean(group.get("supplier_name"))
            normalized_name = normalize_company_name(name)
            candidates.append({
                "supplier_id": stable_id("SID", {"name": normalized_name}),
                "supplier_dir": str(group.get("group_id") or normalized_name),
                "display_name": name,
                "normalized_name": normalized_name,
                "uscc": codes[0] if len(codes) == 1 else None,
                "uscc_candidates": codes,
                "document_ids": sorted(doc_ids),
                "evidence_ids": sorted({str(value) for value in group.get("evidence_ids", [])}),
                "status": "ready" if len(codes) <= 1 else "needs_manual_review",
            })

    if not resolved_groups:
        candidates = []
    for normalized_name in sorted(buckets):
        if resolved_groups:
            break
        bucket = buckets[normalized_name]
        names = sorted(bucket["names"])
        if len(names) != 1:
            continue
        supplier_dir = next(
            (document_groups.get(doc_id) for doc_id in bucket["document_ids"] if document_groups.get(doc_id)),
            normalized_name,
        )
        codes = sorted(bucket["uscc"])
        candidates.append({
            "supplier_id": stable_id("SID", {"name": normalized_name}),
            "supplier_dir": supplier_dir,
            "display_name": names[0],
            "normalized_name": normalized_name,
            "uscc": codes[0] if len(codes) == 1 else None,
            "uscc_candidates": codes,
            "document_ids": sorted(bucket["document_ids"]),
            "evidence_ids": sorted(bucket["evidence_ids"]),
            "status": "ready" if len(codes) <= 1 else "needs_manual_review",
        })

    status = "ready" if candidates else "pending"
    payload = {
        "schema_version": "tender-clearance.identity-candidates.v1",
        "run": inventory.run.model_dump(mode="json"),
        "status": status,
        "source": "cover_and_business_identity_pages",
        "candidates": candidates,
        "notes": [
            "身份快速结果仅用于提前启动 SRM；正式主体确认仍由 normalize_and_match 完成。",
            "全文 OCR 未完成前不得生成正式报告。",
        ],
    }
    _out, interim = ensure_output_dirs(project_dir)
    write_json(interim / "identity-candidates.json", payload)
    return payload


@app.command()
def run(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    try:
        payload = build_identity(project_dir)
    except (ProjectError, FileNotFoundError, ValueError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.secho(
        f"[OK] 身份快速结果：{len(payload['candidates'])} 家，状态={payload['status']} "
        "→ output/interim/identity-candidates.json",
        fg=typer.colors.GREEN if payload["status"] == "ready" else typer.colors.YELLOW,
    )


if __name__ == "__main__":
    app()
