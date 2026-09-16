#!/usr/bin/env python3
"""阶段四（导入通道）：导入授权取得的外部查询证据。

支持（方案 6.4）：
- JSON：结构化导入（命中记录 / 无匹配 / 股权关系），契约见 references/data-contract.md；
- CSV：记录型导入（每行一条记录，必需列 record_kind，其余列进入 fields）；
- PDF / 网页保存件（html）/ 截图（png/jpg）：作为快照证据导入，须有同名 .meta.json
  （查询人、查询时间、URL/渠道、查询主体、可见结果）；无法证明来源的截图证据强度最高 C。

主体归属：统一社会信用代码一致 → confirmed；仅名称一致 → candidate（不自动合并）；
无法归属 → 记录保留 subject_confirmation=unconfirmed，不归属供应商。
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, stable_id, write_json
from tc.models import (
    EvidenceFile,
    ExternalEvidenceFile,
    ExternalQuery,
    ExternalRecord,
    InventoryFile,
    EntitiesFile,
    OwnershipRelation,
)
from tc.normalize import iso_date
from tc.projio import EvidenceBuilder, ProjectError, ensure_output_dirs, load_project_config
from tc.sources import IMPORT_DIRS

STRENGTH_BY_MODE = {
    "official_api": "B",
    "public_web": "B",
    "manual_import": "C",
    "screenshot": "C",
}

app = typer.Typer(help="导入外部证据（external.json + evidence-external.json）")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
        entities = EntitiesFile(**load_json(project_dir / "output/interim/entities.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    _out, interim = ensure_output_dirs(project_dir)
    builder = EvidenceBuilder(inventory.run, cfg.id_digest_salt, cfg.redaction_mode)

    ext_base = project_dir / "external-evidence"
    queries: list[ExternalQuery] = []
    records: list[ExternalRecord] = []
    ownership: list[OwnershipRelation] = []
    notes: list[str] = []

    if ext_base.exists():
        for source_id, sub in IMPORT_DIRS.items():
            base = ext_base / sub
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or path.name.endswith(".meta.json"):
                    continue
                try:
                    qs, rs, ow, note = _import_file(path, source_id, entities, builder, project_dir)
                except Exception as exc:  # noqa: BLE001
                    qs, rs, ow = _failed_query(source_id, path, str(exc))
                    note = None
                queries.extend(qs)
                records.extend(rs)
                ownership.extend(ow)
                if note:
                    notes.append(note)

    result = ExternalEvidenceFile(
        run=inventory.run, queries=queries, records=records, ownership=ownership
    )
    write_json(interim / "external.json", result.model_dump(mode="json"))
    ev = EvidenceFile(run=inventory.run, evidence=builder.items)
    write_json(interim / "evidence-external.json", ev.model_dump(mode="json"))
    write_json(interim / "external-notes.json", {"run": inventory.run.model_dump(mode="json"), "notes": notes})

    typer.secho(
        f"[OK] 外部证据导入完成：{len(queries)} 条查询记录，{len(records)} 条风险/业务记录，"
        f"{len(ownership)} 条股权关系 → external.json",
        fg=typer.colors.GREEN,
    )


# ----------------------------------------------------------------- 归属


def _attribute(entities: EntitiesFile, uscc: str | None, name: str | None) -> tuple[str | None, str]:
    """返回 (supplier_id, confirmation)。仅代码一致才 confirmed；仅名称一致为 candidate。"""
    if uscc:
        for s in entities.suppliers:
            if s.uscc and s.uscc == uscc and s.uscc_status == "present_valid":
                return s.supplier_id, "confirmed"
    if name:
        from tc.normalize import normalize_company_name

        key = normalize_company_name(name)
        for s in entities.suppliers:
            if s.normalized_name and s.normalized_name == key:
                return s.supplier_id, "candidate" if not uscc else "candidate"
    return None, "unconfirmed"


def _supplier_dir_of(entities: EntitiesFile, supplier_id: str | None) -> str | None:
    if not supplier_id:
        return None
    for s in entities.suppliers:
        if s.supplier_id == supplier_id:
            return s.directory_name
    return None


# ----------------------------------------------------------------- JSON 导入


def _import_json_file(path: Path, source_id: str, entities: EntitiesFile, builder: EvidenceBuilder, project_dir: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"records": data}
    channel = data.get("channel", source_id)
    uscc = data.get("uscc")
    name = data.get("supplier_name") or data.get("name")
    supplier_directory = data.get("supplier_directory")
    if supplier_directory and not uscc and not name:
        for s in entities.suppliers:
            if s.directory_name == supplier_directory:
                supplier_id, confirmation = s.supplier_id, "candidate"
                uscc, name = s.uscc, s.declared_name
                break
        else:
            fq, fr, fo = _failed_query(source_id, path, f"supplier_directory“{supplier_directory}”未匹配到供应商")
            return [fq], fr, fo, None
    else:
        supplier_id, confirmation = _attribute(entities, uscc, name)
    queried_at = _parse_dt(data.get("queried_at"))
    status = data.get("status", "match" if data.get("records") else "needs_manual_review")
    query_mode = data.get("query_mode", "manual_import")
    querier = data.get("querier")
    source_url = data.get("source_url")
    strength = data.get("evidence_strength") or (
        "B" if query_mode in ("official_api", "public_web") else "C"
    )
    sha = _sha256_of(path)

    qid = stable_id("Q", {"source": source_id, "uscc": uscc, "name": name, "path": _rel(path, project_dir)})
    ev = builder.add(
        source_type="external_evidence",
        document_id=None,
        location={"kind": "import_record", "file": _rel(path, project_dir), "format": "json",
                  "url": source_url, "querier": querier, "queried_at": data.get("queried_at")},
        field=f"external.{channel}",
        raw_value=f"导入文件：{path.name}（status={status}）",
        method="manual_import",
        strength=strength,  # type: ignore[arg-type]
        sha256=sha,
        note=f"渠道={channel}；主体={name or ''}{'/' + uscc if uscc else ''}；查询人={querier or '未填写'}",
    )
    query_supplier_id = supplier_id if confirmation == "confirmed" else None
    query_status = status
    query_detail = data.get("note")
    if status == "match" and confirmation != "confirmed":
        query_status = "needs_manual_review"
        query_detail = query_detail or "导入主体仅按名称候选，未按统一社会信用代码确认；不得自动归属"
    query = ExternalQuery(
        query_id=qid,
        source_id=source_id,  # type: ignore[arg-type]
        subject_supplier_id=query_supplier_id,
        subject_key={"uscc": uscc, "name": name},
        query_mode=query_mode,  # type: ignore[arg-type]
        queried_at=queried_at,
        status=query_status,  # type: ignore[arg-type]
        record_count=len(data.get("records", [])),
        evidence_ids=[ev.evidence_id],
        detail=query_detail,
        adapter_version="import/0.3.0",
    )
    records: list[ExternalRecord] = []
    for i, item in enumerate(data.get("records", [])):
        if not isinstance(item, dict):
            continue
        rec_uscc = item.get("subject_uscc", uscc)
        rec_name = item.get("subject_name", name)
        sup, conf = (supplier_id, confirmation) if not item.get("subject_uscc") and not item.get("subject_name") else _attribute(entities, rec_uscc, rec_name)
        # 主体未确认的记录不归属供应商（仅名称一致为候选，不得自动合并）
        if conf != "confirmed":
            sup = None
        records.append(ExternalRecord(
            record_id=stable_id("R", {"source": source_id, "path": _rel(path, project_dir), "index": i, "fields": item}),
            source_id=source_id,  # type: ignore[arg-type]
            supplier_id=sup,
            subject_confirmation=conf,  # type: ignore[arg-type]
            subject_name=rec_name,
            subject_uscc=rec_uscc,
            record_kind=item.get("record_kind", "dishonesty"),  # type: ignore[arg-type]
            fields=item.get("fields", {k: v for k, v in item.items() if k not in ("record_kind", "effective_from", "effective_to", "subject_uscc", "subject_name")}),
            effective_from=iso_date(str(item["effective_from"])) if item.get("effective_from") else None,
            effective_to=iso_date(str(item["effective_to"])) if item.get("effective_to") else None,
            evidence_ids=[ev.evidence_id],
        ))
    # 查询记录的主体归属：以记录级确认为准（供覆盖矩阵使用）
    if supplier_id is None:
        attributed = [r.supplier_id for r in records if r.supplier_id and r.subject_confirmation == "confirmed"]
        if attributed:
            supplier_id = attributed[0]
            confirmation = "confirmed"
    if name is None and uscc is None and records:
        # 主体键写在记录级的导入（如同名案件）：回填到查询记录，供覆盖矩阵对齐
        first = records[0]
        if first.subject_name or first.subject_uscc:
            query.subject_key = {"uscc": first.subject_uscc, "name": first.subject_name}
    query.subject_supplier_id = supplier_id if confirmation == "confirmed" else None
    own: list[OwnershipRelation] = []
    for i, item in enumerate(data.get("ownership", [])):
        if not isinstance(item, dict):
            continue
        sup, conf = _attribute(entities, item.get("to_company_uscc", uscc), item.get("to_company_name", name))
        own.append(OwnershipRelation(
            relation_id=stable_id("OWN", {"source": source_id, "path": _rel(path, project_dir), "index": i, "item": item}),
            from_party_name=item.get("from_party_name"),
            from_party_digest=builder.digest_of(item["from_party_id_number"]) if item.get("from_party_id_number") else None,
            from_company_name=item.get("from_company_name"),
            from_company_uscc=item.get("from_company_uscc"),
            to_company_name=item.get("to_company_name", name or ""),
            to_company_uscc=item.get("to_company_uscc", uscc),
            share_ratio=item.get("share_ratio"),
            relation_date=iso_date(str(item["relation_date"])) if item.get("relation_date") else None,
            relation_end_date=iso_date(str(item["relation_end_date"])) if item.get("relation_end_date") else None,
            supplier_id=sup,
            subject_confirmation=conf,  # type: ignore[arg-type]
            evidence_ids=[ev.evidence_id],
        ))
    return [query], records, own, None


def _failed_query(source_id: str, path: Path, detail: str):
    qid = stable_id("Q", {"source": source_id, "path": str(path), "failed": detail[:50]})
    return ExternalQuery(
        query_id=qid,
        source_id=source_id,  # type: ignore[arg-type]
        subject_supplier_id=None,
        subject_key={"file": path.name},
        query_mode="manual_import",
        queried_at=None,
        status="failed",
        record_count=0,
        evidence_ids=[],
        detail=f"导入文件解析失败：{detail[:300]}",
        adapter_version="import/0.3.0",
    ), [], []


# ----------------------------------------------------------------- CSV 导入


def _import_csv_file(path: Path, source_id: str, entities: EntitiesFile, builder: EvidenceBuilder, project_dir: Path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [_failed_query(source_id, path, "CSV 无数据行")], [], [], None
    missing = [c for c in ("record_kind",) if c not in rows[0]]
    if missing:
        return [_failed_query(source_id, path, f"CSV 缺少必需列：{missing}")], [], [], None
    records: list[ExternalRecord] = []
    queries: list[ExternalQuery] = []
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row.get("uscc", ""), row.get("subject_name", row.get("name", "")))
        groups.setdefault(key, []).append(row)
    for (uscc, name), group in groups.items():
        supplier_id, confirmation = _attribute(entities, uscc or None, name or None)
        ev = builder.add(
            source_type="external_evidence",
            document_id=None,
            location={"kind": "import_record", "file": _rel(path, project_dir), "format": "csv", "rows": len(group)},
            field=f"external.{source_id}",
            raw_value=f"导入文件：{path.name}（{len(group)} 行）",
            method="manual_import",
            strength="C",
            sha256=_sha256_of(path),
            note=f"渠道={source_id}；主体={name or ''}{'/' + uscc if uscc else ''}",
        )
        qid = stable_id("Q", {"source": source_id, "uscc": uscc, "name": name, "path": _rel(path, project_dir)})
        queries.append(ExternalQuery(
            query_id=qid,
            source_id=source_id,  # type: ignore[arg-type]
            subject_supplier_id=supplier_id,
            subject_key={"uscc": uscc or None, "name": name or None},
            query_mode="manual_import",
            queried_at=_parse_dt(group[0].get("queried_at")),
            status="match",
            record_count=len(group),
            evidence_ids=[ev.evidence_id],
            detail="CSV 导入（视为已查询到记录）",
            adapter_version="import/0.3.0",
        ))
        for i, row in enumerate(group):
            fields = {k: v for k, v in row.items() if k not in ("record_kind", "effective_from", "effective_to", "uscc", "subject_name", "name", "queried_at") and v}
            sup, conf = supplier_id, confirmation
            if row.get("subject_uscc"):
                sup, conf = _attribute(entities, row.get("subject_uscc"), row.get("subject_name") or name)
            if conf != "confirmed":
                sup = None  # 主体未确认不归属
            records.append(ExternalRecord(
                record_id=stable_id("R", {"source": source_id, "path": _rel(path, project_dir), "index": i, "row": row}),
                source_id=source_id,  # type: ignore[arg-type]
                supplier_id=sup,
                subject_confirmation=conf,  # type: ignore[arg-type]
                subject_name=row.get("subject_name") or name or None,
                subject_uscc=row.get("subject_uscc") or (uscc or None),
                record_kind=row.get("record_kind", "dishonesty"),  # type: ignore[arg-type]
                fields=fields,
                effective_from=iso_date(row.get("effective_from", "")),
                effective_to=iso_date(row.get("effective_to", "")),
                evidence_ids=[ev.evidence_id],
            ))
    return queries, records, [], None


def _import_csv_wrapper(path: Path, source_id: str, entities: EntitiesFile, builder: EvidenceBuilder, project_dir: Path):
    """CSV 可能包含多个主体，各自产生一条 query 记录。"""
    queries, records, own, note = _import_csv_file(path, source_id, entities, builder, project_dir)
    if not queries:
        queries = [_failed_query(source_id, path, "无数据")[0]]
    return queries, records, own, note


# ----------------------------------------------------------------- 快照导入


def _import_snapshot(path: Path, source_id: str, entities: EntitiesFile, builder: EvidenceBuilder, project_dir: Path):
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    required = ["queried_by", "queried_at", "url_or_channel", "subject", "visible_result"]
    missing = [k for k in required if not meta.get(k)]
    uscc = meta.get("uscc")
    name = meta.get("subject")
    supplier_id, confirmation = _attribute(entities, uscc, name)
    strength = "C" if missing else ("B" if meta.get("verified_official") else "C")
    if missing:
        detail = "截图/快照缺少来源说明（%s），证据强度上限为 C" % ", ".join(missing)
    else:
        detail = None
    ev = builder.add(
        source_type="external_evidence",
        document_id=None,
        location={"kind": "snapshot", "file": _rel(path, project_dir), "url": meta.get("url_or_channel"),
                  "querier": meta.get("queried_by"), "queried_at": meta.get("queried_at")},
        field=f"external.{source_id}",
        raw_value=f"快照：{path.name}；可见结果：{meta.get('visible_result', '未填写')}",
        method="snapshot_import",
        strength=strength,  # type: ignore[arg-type]
        sha256=_sha256_of(path),
        note=detail or f"查询人={meta.get('queried_by')}；时间={meta.get('queried_at')}",
    )
    qid = stable_id("Q", {"source": source_id, "path": _rel(path, project_dir), "snapshot": True})
    query = ExternalQuery(
        query_id=qid,
        source_id=source_id,  # type: ignore[arg-type]
        subject_supplier_id=supplier_id,
        subject_key={"uscc": uscc, "name": name},
        query_mode="manual_import",
        queried_at=_parse_dt(meta.get("queried_at")),
        status=str(meta.get("status", "needs_manual_review")),  # type: ignore[arg-type]
        record_count=0,
        evidence_ids=[ev.evidence_id],
        detail=detail or "截图/快照证据：记录状态以 meta.json 为准，需人工核对原件",
        adapter_version="import/0.3.0",
    )
    records: list[ExternalRecord] = []
    if meta.get("status") == "match" and meta.get("visible_result") and supplier_id:
        records.append(ExternalRecord(
            record_id=stable_id("R", {"source": source_id, "path": _rel(path, project_dir), "snapshot": True}),
            source_id=source_id,  # type: ignore[arg-type]
            supplier_id=supplier_id,
            subject_confirmation=confirmation,  # type: ignore[arg-type]
            subject_name=name,
            subject_uscc=uscc,
            record_kind="dishonesty",
            fields={"visible_result": meta.get("visible_result"), "url_or_channel": meta.get("url_or_channel")},
            evidence_ids=[ev.evidence_id],
        ))
    return [query], records, [], None


# ----------------------------------------------------------------- 分发


def _import_file(path: Path, source_id: str, entities: EntitiesFile, builder: EvidenceBuilder, project_dir: Path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _import_json_file(path, source_id, entities, builder, project_dir)
    if suffix == ".csv":
        return _import_csv_wrapper(path, source_id, entities, builder, project_dir)
    if suffix in (".png", ".jpg", ".jpeg", ".pdf", ".html", ".htm"):
        return _import_snapshot(path, source_id, entities, builder, project_dir)
    return _failed_query(source_id, path, f"不支持的导入格式：{suffix}"), [], [], None


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        d = iso_date(str(text))
        return datetime(d.year, d.month, d.day) if d else None


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, project_dir: Path) -> str:
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return path.name


if __name__ == "__main__":
    app()
