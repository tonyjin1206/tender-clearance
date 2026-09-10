#!/usr/bin/env python3
"""阶段六：渲染清标报告与结构化结果。

生成（方案 7）：
- output/清标结果.json     全量结构化结果（验收基准）
- output/清标报告.md       审阅版 Markdown（验收基准）
- output/证据索引.csv      证据登记表（utf-8-sig）
- output/人工复核清单.csv  I 级/主体歧义/低置信度/缺口（utf-8-sig）
- output/清标报告.docx     展示副本（python-docx 生成；可选）

渲染前运行完整性校验与脱敏检查：任一输出含完整身份证号/手机号即失败。
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, write_json
from tc.models import (
    AlertLevel,
    ContentFile,
    EntitiesFile,
    Evidence,
    EvidenceFile,
    ExternalEvidenceFile,
    FindingsFile,
    Finding,
    InventoryFile,
    MatchesFile,
    MetadataFile,
    ReportBundle,
    ROLE_LABELS,
    SOURCE_LABELS,
)
from tc.projio import ProjectError, ensure_output_dirs, load_project_config

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "清标报告.md.jinja"

# 边界用字母数字整体排除：避免把哈希/摘要中的数字串误判为手机号或证件号
_ID18_ANY = re.compile(r"(?<![0-9A-Za-z])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")
_MOBILE_ANY = re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])")

app = typer.Typer(help="渲染清标报告与结构化结果")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    docx: bool = typer.Option(True, help="同时生成 DOCX 展示副本"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        interim = project_dir / "output/interim"
        inventory = InventoryFile(**load_json(interim / "inventory.json"))
        entities = EntitiesFile(**load_json(interim / "entities.json"))
        matches = MatchesFile(**load_json(interim / "matches.json"))
        meta = MetadataFile(**load_json(interim / "metadata.json"))
        ext = ExternalEvidenceFile(**load_json(interim / "external.json"))
        findings = FindingsFile(**load_json(interim / "findings.json"))
        content = ContentFile(**load_json(interim / "content.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    out_dir, interim = ensure_output_dirs(project_dir)

    evidence: dict[str, Evidence] = {}
    for name in ("evidence-content.json", "evidence-metadata.json", "evidence-external.json",
                 "evidence-external-queries.json"):
        p = interim / name
        if p.exists():
            for e in load_json(p).get("evidence", []):
                ev = Evidence(**e)
                evidence[ev.evidence_id] = ev
    low_conf_ids = set()
    lc = interim / "low_confidence.json"
    if lc.exists():
        low_conf_ids = set(load_json(lc).get("evidence_ids", []))

    bundle = ReportBundle(
        run=inventory.run, project=cfg, inventory=inventory, entities=entities,
        matches=matches, metadata=meta, external=ext, findings=findings,
        evidence=EvidenceFile(run=inventory.run, evidence=sorted(evidence.values(), key=lambda x: x.evidence_id)),
    )
    result_path = out_dir / "清标结果.json"
    write_json(result_path, bundle.model_dump(mode="json"))

    ctx = _build_context(cfg, inventory, entities, meta, ext, findings, evidence, low_conf_ids, content)
    md_path = out_dir / "清标报告.md"
    _render_markdown(ctx, md_path)

    # PDF 审阅版（与 Markdown 同源同内容；失败不影响验收基准输出）
    pdf_path = out_dir / "清标报告.pdf"
    try:
        from tc.md2pdf import md_to_pdf

        md_to_pdf(md_path.read_text(encoding="utf-8"), pdf_path)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"[提示] PDF 生成失败（不影响验收基准输出）：{exc}", fg=typer.colors.YELLOW)

    _render_evidence_csv(evidence, inventory, out_dir / "证据索引.csv")
    _render_review_csv(findings, entities, low_conf_ids, inventory, content, out_dir / "人工复核清单.csv")
    if docx:
        try:
            _render_docx(ctx, out_dir / "清标报告.docx")
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"[提示] DOCX 生成失败（不影响验收基准输出）：{exc}", fg=typer.colors.YELLOW)

    # 脱敏检查：所有对外输出不得包含完整身份证号/手机号（PDF 提取文本一并检查）
    check_paths = [result_path, md_path, out_dir / "证据索引.csv", out_dir / "人工复核清单.csv"]
    if pdf_path.exists():
        check_paths.append(pdf_path)
    for path in check_paths:
        if path.suffix == ".pdf":
            import fitz

            text = "\n".join(page.get_text() for page in fitz.open(path))
        else:
            text = path.read_text(encoding="utf-8")
        hits = _find_sensitive(text)
        if hits:
            typer.secho(f"[错误] 脱敏检查失败：{path.name} 中发现疑似完整证件号/手机号：{hits}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=3)

    typer.secho(
        f"[OK] 报告已生成 → {out_dir / '清标报告.md'}、清标结果.json、证据索引.csv、人工复核清单.csv"
        + ("、清标报告.pdf" if pdf_path.exists() else ""),
        fg=typer.colors.GREEN,
    )


def _find_sensitive(text: str) -> list[str]:
    hits = [m.group(0) for m in _MOBILE_ANY.finditer(text)]
    for m in _ID18_ANY.finditer(text):
        hits.append(m.group(0))
    return hits[:5]


# --------------------------------------------------------------------- 上下文


def _sup_name(entities: EntitiesFile) -> dict[str, str]:
    return {s.supplier_id: s.display_name for s in entities.suppliers}


_LEVEL_LABEL: dict[AlertLevel, str] = {"I": "I 级（高）", "II": "II 级（中）", "III": "III 级（低）"}
_STATUS_LABEL = {
    "open": "待复核",
    "human_review_required": "人工复核必需",
    "resolved": "已复核",
}
_DOMAIN_LABEL = {
    "identity": "主体身份",
    "metadata": "文件属性",
    "ownership": "股权关联",
    "judicial": "司法经营",
    "dishonesty": "违法失信",
    "coverage": "查询覆盖",
}


def _frow(f: Finding, names: dict[str, str]) -> dict:
    return {
        "finding_id": f.finding_id,
        "rule_id": f.rule_id,
        "domain": f.domain,
        "domain_label": _DOMAIN_LABEL.get(f.domain, f.domain),
        "level": f.level,
        "level_label": _LEVEL_LABEL[f.level],
        "evidence_strength": f.evidence_strength,
        "supplier_ids": f.supplier_ids,
        "suppliers_label": "、".join(names.get(s, s) for s in f.supplier_ids) or "（未归属供应商）",
        "fact": f.fact,
        "status": f.status,
        "status_label": _STATUS_LABEL[f.status],
        "recommendation": f.recommendation,
        "clause_note": f.procurement_clause_note,
        "evidence_ids": f.evidence_ids,
        "related_ids": f.related_ids,
        "subject_confirmation": f.subject_confirmation,
    }


def _build_context(cfg, inventory, entities, meta, ext, findings_file, evidence, low_conf_ids, content) -> dict:
    names = _sup_name(entities)
    sups = entities.suppliers
    findings = findings_file.findings
    # 新口径：公共项只接受封面第一页；主体信息只接受商务标。
    project_values: dict[str, list[str]] = {k: [] for k in ("tenderer", "project_name", "project_code", "bid_date")}
    for fr in content.fields:
        if fr.field in project_values and fr.normalized:
            project_values[fr.field].append(fr.value_masked)
    project_identity = {k: sorted(set(v)) for k, v in project_values.items()}
    business_rows = []
    for s in sups:
        def party(role: str, attr: str) -> str:
            vals = [getattr(p, attr) for p in entities.parties if p.supplier_id == s.supplier_id and p.role == role]
            vals = [str(v) for v in vals if v]
            return "、".join(sorted(set(vals))) or "未取得"
        business_rows.append({
            "supplier": s.display_name,
            "company": s.declared_name or "未取得",
            "uscc": s.uscc or ("候选：" + "、".join(s.uscc_candidates) if s.uscc_candidates else "未取得"),
            "legal_name": party("legal_rep", "name"),
            "legal_id": party("legal_rep", "id_mask"),
            "agent_name": party("bid_agent", "name"),
            "agent_id": party("bid_agent", "id_mask"),
        })

    srm_registration = []
    ownership_rows_raw = []
    for r in ext.records:
        if r.record_kind == "registration":
            srm_registration.append({"supplier": names.get(r.supplier_id or "", r.subject_name or "未归属"), "fields": r.fields, "evidence_ids": r.evidence_ids})
    for o in ext.ownership:
        ownership_rows_raw.append({
            "supplier": names.get(o.supplier_id or "", "未归属"),
            "shareholder": o.from_party_name or o.from_company_name or "未取得",
            "amount": "未返回" if o.share_ratio is None else str(o.share_ratio),
            "time": str(o.relation_date) if o.relation_date else "未返回",
            "ratio": str(o.share_ratio) if o.share_ratio is not None else "未返回",
            "method": "未返回", "evidence_ids": o.evidence_ids,
        })
    government_screenshots = []
    for ev in evidence.values():
        if ev.field == "external.government_procurement":
            loc = ev.location
            if loc.get("kind") in ("snapshot", "url"):
                government_screenshots.append({"label": "中国政府采购网查询证据", "ref": loc.get("path") or loc.get("url") or "未提供"})
    coverage_map = {(c.supplier_id, c.source_id): c.status for c in findings_file.coverage}
    sources = ["government_procurement", "srm"]

    # 摘要
    supplier_rows = []
    unfinished_by_sup: dict[str, int] = {}
    for c in findings_file.coverage:
        if c.status not in ("match", "no_match_verified"):
            unfinished_by_sup[c.supplier_id] = unfinished_by_sup.get(c.supplier_id, 0) + 1
    review_set = set(findings_file.human_review_queue)
    for s in sups:
        rows = [f for f in findings if s.supplier_id in f.supplier_ids]
        supplier_rows.append({
            "name": s.display_name,
            "i": sum(1 for f in rows if f.level == "I"),
            "ii": sum(1 for f in rows if f.level == "II"),
            "iii": sum(1 for f in rows if f.level == "III"),
            "review": sum(1 for f in rows if f.finding_id in review_set),
            "unfinished": unfinished_by_sup.get(s.supplier_id, 0),
        })
    coverage_matrix = [
        {
            "supplier": s.display_name,
            "cells": [coverage_map.get((s.supplier_id, sid), "not_queried") for sid in sources],
        }
        for s in sups
    ]
    coverage_sep = "|---" * (1 + len(sources)) + "|"
    headline = [_frow(f, names) for f in findings if f.level == "I"]

    # 公司信息交叉表
    def contacts_of(sup_id: str, kind: str) -> list[str]:
        return sorted({c.raw_masked for c in entities.contacts if c.supplier_id == sup_id and c.kind == kind}) or ["—"]

    def parties_of(sup_id: str, role: str) -> list[str]:
        return sorted({p.name for p in entities.parties if p.supplier_id == sup_id and p.role == role}) or ["—"]

    cross_tables = []
    if sups:
        cols = [s.display_name for s in sups]

        def table(title: str, getter) -> dict:
            """行=值（共同值标注），列=供应商，命中打勾。"""
            seen: dict[str, list] = {}
            for s in sups:
                for v in getter(s.supplier_id):
                    if v == "—":
                        continue  # 占位符（无数据）不得显示为共同值
                    seen.setdefault(v, []).append(s.display_name)
            rows = []
            for idx, (v, owners) in enumerate(sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0])), start=1):
                rows.append({
                    "label": f"{idx}. {v}" + ("（共同）" if len(owners) > 1 else ""),
                    "cells": ["✔" if s.display_name in owners else "" for s in sups],
                })
            return {"title": title, "columns": cols, "rows": rows,
                    "sep": "|---" * (1 + len(cols)) + "|"}

        cross_tables.append({
            "title": "统一社会信用代码",
            "columns": cols,
            "rows": [{
                "label": f"{idx}. {s.display_name}：{s.uscc or '（未提取）'}"
                         + ("" if s.uscc_status == "present_valid" else f"［{s.uscc_status}］"),
                "cells": ["✔" if c is s else "" for c in sups],
            } for idx, s in enumerate(sups, start=1)],
            "sep": "|---" * (1 + len(cols)) + "|",
        })
        cross_tables.append(table("联系电话", lambda sid: contacts_of(sid, "phone")))
        cross_tables.append(table("电子邮箱", lambda sid: contacts_of(sid, "email")))
        cross_tables.append(table("地址", lambda sid: contacts_of(sid, "address")))
        cross_tables.append(table("法定代表人", lambda sid: parties_of(sid, "legal_rep")))
        cross_tables.append(table("授权代表", lambda sid: parties_of(sid, "bid_agent")))
        cross_tables.append(table("股东", lambda sid: parties_of(sid, "shareholder")))
        cross_tables.append(table("联系人", lambda sid: parties_of(sid, "contact")))

    # 分域发现
    metadata_findings = [_frow(f, names) for f in findings if f.domain == "metadata"]
    ownership_judicial = [_frow(f, names) for f in findings if f.domain in ("ownership", "judicial")]
    dishonesty_findings = [_frow(f, names) for f in findings if f.domain == "dishonesty"]
    review_findings = [_frow(f, names) for f in findings if f.finding_id in review_set or f.status == "human_review_required"]

    # 股权
    ownership_rows = [
        {
            "relation_id": o.relation_id,
            "from_label": o.from_party_name or o.from_company_name or o.from_company_uscc or "—",
            "to_company_name": o.to_company_name + (f"（供应商 {names.get(o.supplier_id, o.supplier_id)}）" if o.supplier_id and o.supplier_id in names else ""),
            "share_ratio": o.share_ratio,
            "relation_date": str(o.relation_date) if o.relation_date else None,
            "relation_end_date": str(o.relation_end_date) if o.relation_end_date else None,
            "confirmation_label": {"confirmed": "已确认", "candidate": "候选", "unconfirmed": "未确认"}[o.subject_confirmation],
            "evidence_ids": o.evidence_ids,
        }
        for o in ext.ownership
    ]

    # 司法记录
    judicial_rows = []
    for r in ext.records:
        if r.record_kind != "judicial_case":
            continue
        judicial_rows.append({
            "supplier": names.get(r.supplier_id or "", "（未归属）"),
            "record_id": r.record_id,
            "confirmation_label": {"confirmed": "已确认", "candidate": "候选", "unconfirmed": "未确认"}[r.subject_confirmation],
            "desc": "；".join(f"{k}={v}" for k, v in list(r.fields.items())[:5]) or r.subject_name or "",
            "evidence_ids": r.evidence_ids,
        })

    # 违法失信按渠道
    dishonesty_sources = []
    for sid, label in SOURCE_LABELS.items():
        status_rows = [
            {"line": f"{s.display_name}={coverage_map.get((s.supplier_id, sid), 'not_queried')}"}
            for s in sups
        ]
        records = []
        for r in ext.records:
            if r.source_id != sid or r.record_kind not in ("dishonesty", "penalty"):
                continue
            f = r.fields
            records.append({
                "supplier": names.get(r.supplier_id or "", "（未归属）"),
                "behavior": str(f.get("行为") or f.get("title") or f.get("visible_result") or f.get("当事人") or "—"),
                "penalty": str(f.get("处罚") or f.get("处理结果") or "—"),
                "reference": str(f.get("依据") or f.get("处罚决定文号") or f.get("处理编号") or f.get("决定书文号") or "—"),
                "date": str(f.get("处理日期") or f.get("处罚日期") or f.get("发布日期") or "—"),
                "validity": (f"{r.effective_from or '未知'} ~ {r.effective_to or '未载明'}") if (r.effective_from or r.effective_to) else "未载明",
                "status": "match" if (r.subject_confirmation == "confirmed") else r.subject_confirmation,
                "evidence_ids": r.evidence_ids,
            })
        other_kinds = [r for r in ext.records if r.source_id == sid and r.record_kind not in ("dishonesty", "penalty")]
        dishonesty_sources.append({
            "label": label, "status_rows": status_rows, "records": records,
            "other_note": ("该渠道的司法/股权类记录见第 5 章。" if (not records and other_kinds) else None),
        })

    # 复核缺口
    gaps: list[str] = []
    for a in inventory.anomalies:
        gaps.append(f"清单异常：{a.detail or a.anomaly}")
    for a in content.anomalies:
        gaps.append(f"提取异常：{a.detail or a.anomaly}")
    if low_conf_ids:
        gaps.append(f"低置信度字段（如 OCR）{len(low_conf_ids)} 项未参与匹配，需人工核对（见人工复核清单.csv）")
    for c in findings_file.coverage:
        if c.status not in ("match", "no_match_verified"):
            gaps.append(f"查询覆盖：{names.get(c.supplier_id, c.supplier_id)} × {SOURCE_LABELS.get(c.source_id, c.source_id)} = {c.status}")
    for s in sups:
        if s.uscc_status != "present_valid":
            gaps.append(f"主体确认：{s.display_name} 统一社会信用代码状态为 {s.uscc_status}，相关外部记录暂不归属")
    if cfg.procurement_rules_source in (None, "", "unspecified"):
        gaps.append("采购文件资格/否决条款未提供：失信记录的资格影响只能标注“待采购人确认”")
    if cfg.bid_deadline is None:
        gaps.append("project.yaml 未提供 bid_deadline：禁止产生“处罚处于有效期内”的结论")

    # 附录
    queries_rows = [
        {
            "source": SOURCE_LABELS.get(q.source_id, q.source_id),
            "supplier": names.get(q.subject_supplier_id or "", "（未归属）"),
            "status": q.status,
            "mode": q.query_mode,
            "queried_at": str(q.queried_at) if q.queried_at else None,
            "detail": (q.detail or "")[:120],
        }
        for q in sorted(ext.queries, key=lambda x: (x.source_id, x.subject_supplier_id or ""))
    ]
    appendix = {
        "documents": [
            {
                "relative_path": d.relative_path,
                "category": d.category,
                "size_bytes": d.size_bytes,
                "short_sha": d.sha256[:16],
                "status": d.extraction_status,
                "detail": d.status_detail,
            }
            for d in inventory.documents
        ],
        "rule_ids": sorted({f.rule_id for f in findings}),
        "queries": queries_rows,
    }

    deadline_display = "未提供（无法判断处罚/禁入是否处于有效期）" if cfg.bid_deadline is None else str(cfg.bid_deadline)

    return {
        "project": cfg,
        "run": inventory.run,
        "project_identity": project_identity,
        "business_rows": business_rows,
        "srm_registration": srm_registration,
        "srm_ownership": ownership_rows_raw,
        "srm_branches": [],
        "srm_personnel": [],
        "government_screenshots": government_screenshots,
        "summary": {
            "deadline_display": deadline_display,
            "supplier_count": len(sups),
            "supplier_names": [s.display_name for s in sups],
            "bid_docs": sum(1 for d in inventory.documents if d.category == "bid"),
            "proc_docs": sum(1 for d in inventory.documents if d.category == "procurement"),
            "ext_dir": sum(1 for d in inventory.documents if d.category == "external_evidence"),
            "supplier_rows": supplier_rows,
            "coverage_matrix": coverage_matrix,
            "coverage_sep": coverage_sep,
            "coverage_sources": [SOURCE_LABELS.get(s, s) for s in sources],
            "coverage_gaps": sum(unfinished_by_sup.values()),
            "headline_findings": headline,
        },
        "coverage_sources": [SOURCE_LABELS.get(s, s) for s in sources],
        "suppliers": sups,
        "cross_tables": cross_tables,
        "metadata_findings": metadata_findings,
        "ownership": ownership_rows,
        "judicial_rows": judicial_rows,
        "ownership_judicial_findings": ownership_judicial,
        "dishonesty_sources": dishonesty_sources,
        "dishonesty_findings": dishonesty_findings,
        "review_findings": review_findings,
        "review_gaps": gaps,
        "appendix": appendix,
    }


# --------------------------------------------------------------------- 渲染


def _render_markdown(ctx: dict, path: Path) -> None:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_PATH.parent)), autoescape=False,
                      trim_blocks=True, lstrip_blocks=True)
    tpl = env.get_template(TEMPLATE_PATH.name)
    text = tpl.render(**ctx)
    path.write_text(text, encoding="utf-8")


def _render_evidence_csv(evidence: dict[str, Evidence], inventory: InventoryFile, path: Path) -> None:
    doc_path = {d.document_id: d.relative_path for d in inventory.documents}
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["evidence_id", "source_type", "document_path", "location", "field",
                    "raw_value_masked", "normalized", "method", "confidence", "strength",
                    "collected_at", "source_sha256", "note"])
        for ev in sorted(evidence.values(), key=lambda x: x.evidence_id):
            w.writerow([
                ev.evidence_id, ev.source_type,
                doc_path.get(ev.document_id or "", ev.document_id or ""),
                _loc_str(ev),
                ev.field, ev.raw_value, ev.normalized_value or "", ev.method,
                ev.confidence if ev.confidence is not None else "", ev.strength,
                ev.collected_at.isoformat(), ev.sha256 or "", ev.note or "",
            ])


def _loc_str(ev: Evidence) -> str:
    parts = []
    for k, v in ev.location.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return " ".join(parts)[:300]


def _render_review_csv(findings_file, entities, low_conf_ids, inventory, content, path: Path) -> None:
    names = _sup_name(entities)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["类别", "编号", "预警级别", "涉及供应商", "内容", "规则/原因", "证据强度", "建议动作"])
        for f in findings_file.findings:
            if f.status == "human_review_required":
                w.writerow(["风险发现", f.finding_id, f.level,
                            "、".join(names.get(s, s) for s in f.supplier_ids),
                            f.fact, f.rule_id, f.evidence_strength, f.recommendation])
        for eid in sorted(low_conf_ids):
            w.writerow(["低置信度字段", eid, "—", "—", "OCR/低置信度提取结果未参与匹配，需人工核对原件", "低置信度", "D", "对照原件核对字段值"])
        for a in inventory.anomalies:
            w.writerow(["清单异常", "—", "—", "—", a.detail or a.anomaly, a.anomaly, "—", "人工检查文件并补录"])
        for d in inventory.documents:
            if d.extraction_status not in ("ok", "partial", "skipped"):
                w.writerow(["读取失败文件", "—", "—", "—",
                            f"{d.relative_path}：{d.extraction_status}（{d.status_detail or ''}）",
                            d.extraction_status, "—", "人工检查文件并补录完整版本"])
        for a in content.anomalies:
            w.writerow(["提取异常", "—", "—", a.relative_path or "—",
                        f"{a.relative_path or ''}：{a.detail or a.anomaly}".strip("："),
                        a.anomaly, "—", "人工核对文件内容"])


def _render_docx(ctx: dict, path: Path) -> None:
    import docx
    from docx.shared import Pt

    d = docx.Document()
    d.add_heading(f"投标清标报告：{ctx['project'].project_name}", level=0)
    p = d.add_paragraph(
        f"项目编号 {ctx['project'].project_id}；投标截止时间 {ctx['summary']['deadline_display']}；"
        f"运行标识 {ctx['run'].run_id}。本报告为风险线索整理结果，非最终认定；"
        "标注“人工复核必需”的条目须人工复核后使用。"
    )
    p.runs[0].font.size = Pt(9)
    for s in ctx["summary"]["supplier_rows"]:
        d.add_paragraph(
            f"{s['name']}：I 级 {s['i']} 项；II 级 {s['ii']} 项；III 级 {s['iii']} 项；"
            f"必须人工复核 {s['review']} 项；未完成查询 {s['unfinished']} 项。"
        )
    d.add_heading("I 级预警（人工复核必需）", level=1)
    for f in ctx["summary"]["headline_findings"]:
        d.add_paragraph(f"{f['finding_id']}（{f['rule_id']}）：{f['fact']}", style="List Bullet")
    d.add_heading("文件属性风险检查", level=1)
    for f in ctx["metadata_findings"]:
        d.add_paragraph(f"[{f['level_label']}|{f['evidence_strength']}] {f['fact']}（{f['finding_id']}，规则 {f['rule_id']}）")
    d.add_heading("股权关系与司法经营风险", level=1)
    for o in ctx["ownership"]:
        d.add_paragraph(f"股权：{o['from_label']} → {o['to_company_name']}（{o['confirmation_label']}）")
    for r in ctx["judicial_rows"]:
        d.add_paragraph(f"司法：{r['supplier']}（{r['confirmation_label']}）：{r['desc']}")
    for f in ctx["ownership_judicial_findings"]:
        d.add_paragraph(f"[{f['level_label']}] {f['fact']}")
    d.add_heading("违法失信信息", level=1)
    for src in ctx["dishonesty_sources"]:
        d.add_paragraph(f"{src['label']}：" + "；".join(r["line"] for r in src["status_rows"]))
        for r in src["records"]:
            d.add_paragraph(f"  · {r['supplier']}：{r['behavior']} / {r['penalty']}（{r['date']}，有效期 {r['validity']}）", style="List Bullet")
    d.add_heading("人工复核与待补资料", level=1)
    for f in ctx["review_findings"]:
        d.add_paragraph(f"{f['finding_id']}：{f['fact']}", style="List Bullet")
    for g in ctx["review_gaps"]:
        d.add_paragraph(g, style="List Bullet")
    d.add_heading("附录：输入文件清单", level=1)
    t = d.add_table(rows=1, cols=4)
    hdr = t.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text = "文件", "类别", "状态", "SHA-256（前16位）"
    for doc in ctx["appendix"]["documents"]:
        cells = t.add_row().cells
        cells[0].text = doc["relative_path"]
        cells[1].text = doc["category"]
        cells[2].text = doc["status"]
        cells[3].text = doc["short_sha"]
    d.save(str(path))


if __name__ == "__main__":
    app()
