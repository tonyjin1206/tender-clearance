#!/usr/bin/env python3
"""阶段三：主体候选识别与交叉匹配。

- 优先以统一社会信用代码识别企业；缺失时以名称/地址/法定代表人形成候选，
  候选不得自动合并为同一主体；
- 先生成比对矩阵（matches.json），再由规则引擎生成发现，禁止跳过中间数据；
- 低置信度（OCR）字段不参与精确匹配，仅进入人工核对。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, stable_id, write_json
from tc.models import (
    ContactItem,
    ContentFile,
    CrossMatch,
    EntitiesFile,
    InventoryFile,
    MatchesFile,
    Party,
    Supplier,
)
import yaml as _yaml

from tc.normalize import company_search_key, name_similarity, normalize_company_name
from tc.projio import ProjectError, ensure_output_dirs, load_project_config

LOW_CONFIDENCE = 0.85
_RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "risk-rules.yaml"


def _rules_params() -> dict:
    try:
        data = _yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))
        return data.get("params", {})
    except Exception:  # noqa: BLE001
        return {}

# 字段 → (参与匹配的类别, 匹配方式)
ENTITY_FIELDS = {
    "uscc": "uscc",
    "legal_rep_id": "legal_rep_id_digest",
    "bid_agent_id": "bid_agent_id_digest",
    "legal_rep_name": "legal_rep_name",
    "bid_agent_name": "bid_agent_name",
    "shareholder_name": "shareholder_name",
    "contact_name": "contact_name",
    "phone": "phone",
    "email": "email",
    "address": "address",
    "company_name": "company_name",
}

app = typer.Typer(help="主体识别与交叉匹配（entities.json + matches.json）")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
        content = ContentFile(**load_json(project_dir / "output/interim/content.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    _out, interim = ensure_output_dirs(project_dir)

    supplier_dirs = inventory.supplier_dirs
    suppliers: dict[str, Supplier] = {}
    for i, d in enumerate(supplier_dirs, start=1):
        suppliers[d] = Supplier(
            supplier_id=f"SUP-{i}",
            directory_name=d,
            display_name=cfg.supplier_directory_mapping.get(d, d),
        )

    parties: list[Party] = []
    contacts: list[ContactItem] = []
    notes: list[str] = []
    low_conf_fields: list = []

    # ---- 主体字段归集 -------------------------------------------------------
    supplier_fields: dict[str, dict[str, list]] = {d: {} for d in supplier_dirs}
    for fr in content.fields:
        if fr.low_confidence:
            low_conf_fields.append(fr)
            continue
        if not fr.supplier_dir or fr.supplier_dir not in supplier_fields:
            continue
        supplier_fields[fr.supplier_dir].setdefault(fr.field, []).append(fr)

    for d, supplier in suppliers.items():
        sf = supplier_fields[d]
        # 声明名称：优先 supplier_name/company_name 标签命中，其次公司名频次
        company_hits = sf.get("company_name", [])
        declared = _most_common([h.normalized or h.value_masked for h in company_hits if h.normalized])
        supplier.declared_name = declared
        supplier.normalized_name = normalize_company_name(declared) if declared else None
        supplier.name_search_key = company_search_key(declared) if declared else None
        # 统一社会信用代码：取格式有效的；多个不一致 → 冲突备注
        uscc_hits = sf.get("uscc", [])
        valid_codes = sorted({h.normalized for h in uscc_hits if h.normalized and len(h.normalized) == 18 and _valid_uscc(h.normalized)})
        invalid_codes = sorted({h.normalized for h in uscc_hits if h.normalized and not (len(h.normalized) == 18 and _valid_uscc(h.normalized))})
        if len(valid_codes) == 1:
            supplier.uscc = valid_codes[0]
            supplier.uscc_status = "present_valid"
            supplier.uscc_candidates = valid_codes
            supplier.uscc_evidence_ids = sorted({h.evidence_id for h in uscc_hits if h.normalized == valid_codes[0]})
            supplier.confirmation = "confirmed"
        elif len(valid_codes) > 1:
            # 同页可能同时出现投标人和设备厂商代码。保留候选并清空主代码，
            # 让人工按证据定位确认，避免把排序后的第一个误归属给投标人。
            supplier.uscc = None
            supplier.uscc_status = "present_valid"
            supplier.uscc_candidates = valid_codes
            supplier.uscc_evidence_ids = sorted({h.evidence_id for h in uscc_hits})
            supplier.confirmation = "candidate"
            supplier.confirmation_note = f"发现多个不同的有效格式统一社会信用代码（{len(valid_codes)} 个），主体待人工确认"
            notes.append(f"供应商 {d}：多个有效格式信用代码，未自动选择")
        elif invalid_codes:
            supplier.uscc = invalid_codes[0]
            supplier.uscc_status = "present_invalid_format"
            supplier.uscc_candidates = invalid_codes
            supplier.uscc_evidence_ids = sorted({h.evidence_id for h in uscc_hits if h.normalized == invalid_codes[0]})
            supplier.confirmation = "candidate"
            supplier.confirmation_note = "统一社会信用代码格式/校验位异常，主体待人工确认"
        else:
            supplier.uscc_status = "absent"
            supplier.confirmation = "candidate" if declared else "unconfirmed"
            if not declared:
                supplier.confirmation_note = "未提取到公司名称与统一社会信用代码"
        supplier.evidence_ids = sorted({h.evidence_id for hs in sf.values() for h in hs})

        # 人员
        parties.extend(_build_parties(d, supplier.supplier_id, sf))

        # 联系方式
        for kind, fld in (("phone", "phone"), ("email", "email"), ("address", "address")):
            seen: dict[str, list] = {}
            for h in sf.get(fld, []):
                key = h.normalized or h.value_masked
                seen.setdefault(key, []).append(h)
            for key, hs in sorted(seen.items()):
                contacts.append(ContactItem(
                    kind=kind,  # type: ignore[arg-type]
                    normalized_value=key,
                    raw_masked=hs[0].value_masked,
                    supplier_id=supplier.supplier_id,
                    evidence_ids=sorted({h.evidence_id for h in hs}),
                ))

    # ---- 两两比对矩阵 -------------------------------------------------------
    matches: list[CrossMatch] = []
    ids = [d for d in supplier_dirs]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            matches.extend(_match_pair(ids[i], ids[j], supplier_fields))

    entities = EntitiesFile(
        run=inventory.run,
        suppliers=[suppliers[d] for d in supplier_dirs],
        parties=parties,
        contacts=contacts,
        notes=notes + [
            f"低置信度字段 {len(low_conf_fields)} 项未参与精确匹配，已进入人工核对（如 OCR 识别结果）"
        ] if low_conf_fields else notes,
    )
    write_json(interim / "entities.json", entities.model_dump(mode="json"))
    matches_file = MatchesFile(run=inventory.run, matches=matches)
    write_json(interim / "matches.json", matches_file.model_dump(mode="json"))

    low_conf_ids = sorted({fr.evidence_id for fr in low_conf_fields})
    write_json(interim / "low_confidence.json", {"run": inventory.run.model_dump(mode="json"), "evidence_ids": low_conf_ids})

    typer.secho(
        f"[OK] 主体识别完成：{len(suppliers)} 家供应商、{len(parties)} 名人员、"
        f"{len(contacts)} 条联系方式；比对矩阵 {len(matches)} 条 → entities.json / matches.json",
        fg=typer.colors.GREEN,
    )


def _valid_uscc(code: str) -> bool:
    from tc.normalize import uscc_valid

    return uscc_valid(code)


def _most_common(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))
    return best[0]


PARTY_ROLES = {
    "legal_rep_name": "legal_rep",
    "bid_agent_name": "bid_agent",
    "shareholder_name": "shareholder",
    "contact_name": "contact",
}
PARTY_ID_FIELDS = {"legal_rep_name": "legal_rep_id", "bid_agent_name": "bid_agent_id"}


def _build_parties(dir_name: str, supplier_id: str, sf: dict[str, list]) -> list[Party]:
    parties: list[Party] = []
    for fld, role in PARTY_ROLES.items():
        seen: set[str] = set()
        id_field = PARTY_ID_FIELDS.get(fld)
        id_digests: dict[str, list] = {}
        if id_field:
            for h in sf.get(id_field, []):
                if h.digest:
                    id_digests.setdefault(h.digest, []).append(h)
        for h in sf.get(fld, []):
            name = h.normalized or h.value_masked
            if name in seen:
                continue
            seen.add(name)
            conf_method_ok = h.confidence >= LOW_CONFIDENCE and not h.method.startswith("ocr")
            parties.append(Party(
                party_id=stable_id("PTY", {"supplier": supplier_id, "role": role, "name": name}),
                name=name,
                role=role,  # type: ignore[arg-type]
                id_digest=None,
                id_mask=None,
                supplier_id=supplier_id,
                evidence_ids=[h.evidence_id],
                confirmation="confirmed" if conf_method_ok else "unconfirmed",
            ))
        # 仅有证件摘要而无姓名的授权代表/法人
        if id_field:
            existing = {p.id_digest for p in parties if p.role == role and p.id_digest}
            for dg, hs in id_digests.items():
                if dg in existing:
                    continue
                existing.add(dg)
                parties.append(Party(
                    party_id=stable_id("PTY", {"supplier": supplier_id, "role": role, "digest": dg}),
                    name=f"（未识别姓名，证件摘要 {dg[:6]}…）",
                    role=role,  # type: ignore[arg-type]
                    id_digest=dg,
                    id_mask=hs[0].value_masked,
                    supplier_id=supplier_id,
                    evidence_ids=sorted({x.evidence_id for x in hs}),
                    confirmation="confirmed" if all(x.confidence >= LOW_CONFIDENCE and not x.method.startswith("ocr") for x in hs) else "unconfirmed",
                ))
    return parties


def _match_pair(dir_a: str, dir_b: str, supplier_fields: dict) -> list[CrossMatch]:
    out: list[CrossMatch] = []
    sf_a, sf_b = supplier_fields[dir_a], supplier_fields[dir_b]
    for field in ("uscc", "legal_rep_id", "bid_agent_id", "phone", "email", "contact_name",
                  "legal_rep_name", "bid_agent_name", "shareholder_name", "address"):
        va = _collect_values(sf_a.get(field, []), field)
        vb = _collect_values(sf_b.get(field, []), field)
        for key_a, (disp_a, ev_a) in va.items():
            for key_b, (disp_b, ev_b) in vb.items():
                if not key_a or not key_b:
                    continue
                if field == "address":
                    # 行政区划线索键（region::前缀）只作候选，不构成“完全一致”
                    both_region = key_a.startswith("region::") and key_b.startswith("region::")
                    if both_region and key_a == key_b:
                        mtype = "candidate"
                    elif key_a == key_b:
                        mtype = "exact"
                    else:
                        continue
                elif key_a == key_b:
                    mtype = "exact"
                elif field in ("legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name"):
                    continue  # 姓名仅精确匹配；相似姓名只是候选（不允许模糊认定）
                elif field == "company_name":
                    continue  # 公司名称在单独逻辑中处理
                else:
                    continue
                out.append(CrossMatch(
                    match_id=stable_id("MT", {"a": dir_a, "b": dir_b, "field": field, "va": key_a, "vb": key_b}),
                    field=_match_field_name(field),  # type: ignore[arg-type]
                    supplier_a=dir_a,
                    supplier_b=dir_b,
                    match_type=mtype,  # type: ignore[arg-type]
                    value_a=disp_a,
                    value_b=disp_b,
                    similarity=None,
                    evidence_ids=sorted(set(ev_a) | set(ev_b)),
                    note=None,
                ))
    # 公司名称：检索键一致或高相似 → 仅候选
    ca = _collect_values(sf_a.get("company_name", []), "company_name")
    cb = _collect_values(sf_b.get("company_name", []), "company_name")
    for key_a, (disp_a, ev_a) in ca.items():
        for key_b, (disp_b, ev_b) in cb.items():
            if not key_a or not key_b:
                continue
            sim = name_similarity(key_a, key_b)
            same = company_search_key(key_a) == company_search_key(key_b)
            if same or sim >= _rules_params().get("company_name_similarity", 0.75):
                out.append(CrossMatch(
                    match_id=stable_id("MT", {"a": dir_a, "b": dir_b, "field": "company_name", "va": key_a, "vb": key_b}),
                    field="company_name",
                    supplier_a=dir_a,
                    supplier_b=dir_b,
                    match_type="candidate",
                    value_a=disp_a,
                    value_b=disp_b,
                    similarity=round(sim, 3),
                    evidence_ids=sorted(set(ev_a) | set(ev_b)),
                    note="名称相近仅为主体候选线索，不构成同一主体认定" if not same else "名称检索键一致，主体关系待人工确认",
                ))
    return out


def _match_field_name(field: str) -> str:
    return {
        "uscc": "uscc",
        "legal_rep_id": "legal_rep_id_digest",
        "bid_agent_id": "bid_agent_id_digest",
        "phone": "phone",
        "email": "email",
        "contact_name": "contact_name",
        "legal_rep_name": "legal_rep_name",
        "bid_agent_name": "bid_agent_name",
        "shareholder_name": "shareholder_name",
        "address": "address",
        "company_name": "company_name",
    }[field]


def _collect_values(hits: list, field: str) -> dict[str, tuple[str, list[str]]]:
    """返回 {匹配键: (展示值, 证据ID列表)}。

    证件类字段以受控摘要为匹配键（掩码只作展示）；地址保留原值并附区域键。
    """
    out: dict[str, tuple[str, list[str]]] = {}
    for h in hits:
        if field == "address":
            key = h.normalized or h.value_masked
            disp = h.value_masked
            ev = h.evidence_id
            out.setdefault(key, (disp, []))[1].append(ev)
            rk = _region_key(key)
            if rk:
                out.setdefault(f"region::{rk}", (f"（同一行政区划线索）{disp}", []))[1].append(ev)
            continue
        if field in ("legal_rep_id", "bid_agent_id") and h.digest:
            key = h.digest
        else:
            key = h.normalized or h.value_masked
        disp = h.value_masked
        out.setdefault(key, (disp, []))[1].append(h.evidence_id)
    return out


def _region_key(addr: str) -> str:
    from tc.normalize import address_region_key

    return address_region_key(addr)


if __name__ == "__main__":
    app()
