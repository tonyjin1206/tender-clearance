#!/usr/bin/env python3
"""根据文件正文/OCR 证据确认供应商，再建立文件归组清单。

上传目录和文件名只提供弱提示，不能直接决定供应商。该阶段消费首页/商务标
文字层及已导入的 OCR 字段；无法确认主体的文件保留为 unassigned，正式流程
在人工或宿主 OCR 补齐前停止，不把文件硬塞进某个供应商目录。
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, stable_id, write_json
from tc.models import ContentFile, InventoryFile
from tc.normalize import normalize_company_name
from tc.projio import ProjectError, ensure_output_dirs, load_project_config

app = typer.Typer(help="用正文/OCR 主体证据确认供应商并建立文件分组")

ESSENTIAL_ROLES = {"business", "technical", "bid_schedule"}


def _load_manual_confirmations(project_dir: Path) -> tuple[dict[str, dict], list[dict], list[str]]:
    """读取用户/人工复核后的明确归组，不把它伪装成自动识别结果。"""
    path = project_dir / "supplier-group-confirmations.json"
    if not path.exists():
        return {}, [], []
    try:
        payload = load_json(path)
        assignments: dict[str, dict] = {}
        errors: list[str] = []
        for item in payload.get("assignments", []):
            doc_id = str(item.get("document_id", "")).strip()
            name = str(item.get("supplier_name", "")).strip()
            if not doc_id or not name:
                errors.append("存在缺少 document_id 或 supplier_name 的人工归组项")
                continue
            assignments[doc_id] = {
                "supplier_name": name,
                "normalized_name": normalize_company_name(name),
                "reason": str(item.get("reason", "人工确认")),
            }
        conflicts = list(payload.get("conflicts", []))
        return assignments, conflicts, errors
    except (OSError, TypeError, ValueError) as exc:
        return {}, [], [f"人工归组确认文件无法读取：{exc}"]


def _candidate_names(content: ContentFile, inventory: InventoryFile) -> dict[str, dict]:
    docs = {d.document_id: d for d in inventory.documents if d.category == "bid"}
    grouped: dict[str, dict] = {}
    for doc_id, doc in docs.items():
        fields = [f for f in content.fields if f.document_id == doc_id and f.field == "company_name"]
        scores: Counter[str] = Counter()
        raw_values: dict[str, Counter[str]] = defaultdict(Counter)
        evidence: dict[str, set[str]] = defaultdict(set)
        for field in fields:
            value = field.normalized or field.value_masked
            normalized = normalize_company_name(value)
            if not normalized:
                continue
            # 首页和标签命中比正文偶然出现更强；OCR 仍可用于归组，但保留低置信度。
            # 带“投标人/单位名称”等标签的候选远强于项目名称中偶然出现的
            # 招标人；后者保留为冲突证据，但不能把整包误归给采购人。
            score = 1.0 if field.location.get("via_label") else 0.25
            if field.location.get("page") == 1 or field.location.get("cover"):
                score += 1.0
            if field.method.startswith("ocr"):
                score += 0.25
            scores[normalized] += score
            raw_values[normalized][value] += score
            evidence[normalized].add(field.evidence_id)
        # OCR 误差会把同一家拆成“鑫誉环境检测/鑫誉环检测/舌林省鑫誉…”。
        # 先按相似度聚类，再比较整簇证据量，避免单个招标人名称压过多页的
        # 同一投标人 OCR 变体；代表值仍保留实际出现过的原始候选。
        clusters: list[dict] = []
        for name, score in scores.items():
            target = next(
                (cluster for cluster in clusters
                 if SequenceMatcher(None, name, cluster["representative"]).ratio() >= 0.72),
                None,
            )
            if target is None:
                clusters.append({"representative": name, "score": score, "members": [name]})
            else:
                target["members"].append(name)
                target["score"] += score
                if scores[name] > scores[target["representative"]]:
                    target["representative"] = name
        ranked = sorted(
            [(cluster["representative"], cluster["score"], cluster["members"]) for cluster in clusters],
            key=lambda item: (-item[1], item[0]),
        )
        if not ranked:
            grouped[doc_id] = {"status": "unassigned", "candidates": [], "evidence_ids": []}
            continue
        top_name, top_score, top_members = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        # 供应商与招标人同页出现且分值接近时，不猜；交给 OCR/人工确认。
        ambiguous = len(ranked) > 1 and (top_score - second_score) < 1.0
        if ambiguous:
            grouped[doc_id] = {
                "status": "ambiguous",
                "candidates": [n for n, _, _ in ranked[:4]],
                "evidence_ids": sorted(set().union(*(evidence[n] for n, _, _ in ranked[:4]))),
            }
            continue
        display = raw_values[top_name].most_common(1)[0][0]
        grouped[doc_id] = {
            "status": "assigned",
            "normalized_name": top_name,
            "display_name": display,
            "score": round(top_score, 3),
            "candidates": [n for n, _, _ in ranked[:4]],
            "evidence_ids": sorted(set().union(*(evidence[n] for n in top_members))),
            "role": doc.bid_subtype,
        }

    # 技术标/一览表首页常同时出现招标人和投标人。只接受能与商务标已
    # 识别主体相连的候选，绝不把项目业主名称自动建成供应商。
    anchors: dict[str, str] = {}
    for doc_id, item in grouped.items():
        if docs[doc_id].bid_subtype == "business" and item.get("status") == "assigned":
            anchors[item["normalized_name"]] = item["display_name"]
    for doc_id, item in grouped.items():
        if docs[doc_id].bid_subtype == "business" or not anchors:
            continue
        matched = []
        for candidate in item.get("candidates", []):
            for anchor in anchors:
                if candidate == anchor or SequenceMatcher(None, candidate, anchor).ratio() >= 0.80:
                    matched.append(anchor)
        matched = sorted(set(matched))
        if len(matched) == 1:
            item.update({
                "status": "assigned",
                "normalized_name": matched[0],
                "display_name": anchors[matched[0]],
                "score": max(float(item.get("score", 0.0)), 3.0),
                "evidence_ids": item.get("evidence_ids", []),
                "role": docs[doc_id].bid_subtype,
            })
        elif item.get("status") == "assigned" and item.get("normalized_name") not in anchors:
            item["status"] = "unassigned"
    return grouped


def _document_role(inventory: InventoryFile, doc_id: str, current: dict) -> str:
    """人工归组覆盖主体时仍继承盘点阶段识别出的 bid_subtype。"""
    role = str(current.get("role", "")).strip()
    if role:
        return role
    return next(
        (doc.bid_subtype for doc in inventory.documents
         if doc.document_id == doc_id and doc.category == "bid"),
        "unknown",
    )


@app.command()
def run(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
        content = ContentFile(**load_json(project_dir / "output/interim/content.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    _out, interim = ensure_output_dirs(project_dir)
    doc_candidates = _candidate_names(content, inventory)
    manual_assignments, manual_conflicts, confirmation_errors = _load_manual_confirmations(project_dir)
    inventory_doc_ids = {d.document_id for d in inventory.documents if d.category == "bid"}
    for doc_id, assignment in manual_assignments.items():
        if doc_id not in inventory_doc_ids:
            confirmation_errors.append(f"人工归组确认包含未知文档：{doc_id}")
            continue
        current = doc_candidates.get(doc_id, {})
        doc_candidates[doc_id] = {
            **current,
            "status": "assigned",
            "normalized_name": assignment["normalized_name"],
            "display_name": assignment["supplier_name"],
            "score": 5.0,
            "evidence_ids": current.get("evidence_ids", []),
            "candidates": current.get("candidates", []),
            "role": _document_role(inventory, doc_id, current),
            "confirmation": "manual",
            "confirmation_reason": assignment["reason"],
        }
    names: dict[str, dict] = {}
    for doc_id, item in doc_candidates.items():
        if item.get("status") != "assigned":
            continue
        name = item["normalized_name"]
        bucket = names.setdefault(name, {
            "group_id": stable_id("GRP", {"name": name}, length=12),
            "display_name": item["display_name"],
            "normalized_name": name,
            "document_ids": [],
            "roles": [],
            "evidence_ids": set(),
        })
        bucket["document_ids"].append(doc_id)
        bucket["roles"].append(doc_candidates[doc_id].get("role", "unknown"))
        bucket["evidence_ids"].update(item.get("evidence_ids", []))

    documents: list[dict] = []
    for doc in inventory.documents:
        if doc.category != "bid":
            continue
        item = doc_candidates.get(doc.document_id, {"status": "unassigned"})
        group = names.get(item.get("normalized_name")) if item.get("status") == "assigned" else None
        documents.append({
            "document_id": doc.document_id,
            "relative_path": doc.relative_path,
            "role": doc.bid_subtype,
            "status": item.get("status", "unassigned"),
            "group_id": group["group_id"] if group else None,
            "supplier_name": group["display_name"] if group else None,
            "candidates": item.get("candidates", []),
            "evidence_ids": item.get("evidence_ids", []),
            "confidence": min(1.0, float(item.get("score", 0.0)) / 5.0) if group else 0.0,
            "confirmation": item.get("confirmation", "automatic") if group else None,
            "confirmation_reason": item.get("confirmation_reason") if group else None,
        })

    groups: list[dict] = []
    for group in sorted(names.values(), key=lambda x: x["group_id"]):
        role_set = set(group["roles"])
        missing = sorted(ESSENTIAL_ROLES - role_set)
        groups.append({
            "group_id": group["group_id"],
            "supplier_name": group["display_name"],
            "normalized_name": group["normalized_name"],
            "document_ids": sorted(group["document_ids"]),
            "roles": sorted(role_set),
            "missing_roles": missing,
            "evidence_ids": sorted(group["evidence_ids"]),
            "confirmation": "confirmed" if not missing else "candidate",
        })

    unassigned = [d for d in documents if d["status"] != "assigned"]
    missing_roles = [g for g in groups if g["missing_roles"]]
    status = "resolved" if not unassigned and not missing_roles and groups and not confirmation_errors else "needs_manual_review"
    payload = {
        "schema_version": "tender-clearance.supplier-grouping.v1",
        "status": status,
        "run": inventory.run.model_dump(mode="json"),
        "groups": groups,
        "documents": documents,
        "conflicts": manual_conflicts,
        "unassigned_document_ids": [d["document_id"] for d in unassigned],
        "notes": [
            "供应商归组依据正文/OCR 主体证据，不依据目录名或文件名。",
            *(["部分归组来自用户/人工确认，已单独标记 confirmation=manual。"] if manual_assignments else []),
            *confirmation_errors,
            *([f"存在 {len(manual_conflicts)} 项文件内容与人工归组不一致，需在报告中人工复核。"] if manual_conflicts else []),
            *([f"{len(unassigned)} 个文件未确认供应商，需补充 OCR 或人工核对。"] if unassigned else []),
            *([f"{len(missing_roles)} 个供应商缺少完整商务/技术/一览表角色。"] if missing_roles else []),
        ],
    }
    write_json(interim / "supplier-grouping.json", payload)
    typer.secho(
        f"[OK] 供应商归组：{len(groups)} 家，已归组 {len(documents) - len(unassigned)}/{len(documents)} 个文件，状态={status} → supplier-grouping.json",
        fg=typer.colors.GREEN if status == "resolved" else typer.colors.YELLOW,
    )
    if status != "resolved":
        raise typer.Exit(code=3)


if __name__ == "__main__":
    app()
