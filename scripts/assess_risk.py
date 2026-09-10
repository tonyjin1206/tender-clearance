#!/usr/bin/env python3
"""阶段五：确定性规则引擎。

- 规则声明与等级来自 rules/risk-rules.yaml（版本化），逻辑由本脚本确定性执行；
- 模型可协助解释，但不得修改规则输出或预警等级；
- 统一强制等级上限（方案 2.2）：D 级证据 ≤ III 级；主体未确认 ≤ III 级；
  仅名称模糊/候选线索 ≤ III 级；
- 证据强度 = 该发现引用证据中的最弱强度（min）。

输出：interim/findings.json（findings + coverage + human_review_queue）。
"""

from __future__ import annotations

import fnmatch
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer
import yaml

from tc.canon import load_json, stable_id, write_json
from tc.models import (
    AlertLevel,
    ContentFile,
    CoverageEntry,
    EntitiesFile,
    Evidence,
    EvidenceStrength,
    ExternalEvidenceFile,
    ExternalRecord,
    FindingsFile,
    Finding,
    InventoryFile,
    MatchesFile,
    MetadataFile,
    OwnershipRelation,
    ProjectConfig,
)
from tc.projio import ProjectError, ensure_output_dirs, load_project_config

RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "risk-rules.yaml"

ALL_SOURCES = ("government_procurement", "srm")
DIS_SOURCE_RULE = {
    "government_procurement": "DIS-003",
}
_CAP_RANK = {"III": 1, "II": 2, "I": 3}
_STRENGTH_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

app = typer.Typer(help="规则引擎：生成风险发现、查询覆盖与人工复核队列（findings.json）")


class RuleBook:
    def __init__(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.version = str(data.get("version", "unknown"))
        self.params = data.get("params", {})
        self.level_caps = data.get("level_caps", {})
        self.rules = {r["id"]: r for r in data.get("rules", [])}

    def level(self, rule_id: str) -> AlertLevel:
        return self.rules[rule_id]["level"]  # type: ignore[no-any-return]

    def requires_review(self, rule_id: str) -> bool:
        return bool(self.rules.get(rule_id, {}).get("requires_human_review", False))


def _finding(
    book: RuleBook,
    rule_id: str,
    key: dict,
    *,
    supplier_ids: list[str],
    evidence_ids: list[str],
    fact: str,
    strength: EvidenceStrength,
    related: list[str] | None = None,
    subject_confirmation: str = "confirmed",
    recommendation: str = "",
    clause_note: str | None = None,
    missing: list[str] | None = None,
    level_override: AlertLevel | None = None,
) -> Finding:
    level: AlertLevel = level_override or book.level(rule_id)  # type: ignore[assignment]
    if _cap_rank(level) > _cap_rank(book.level_caps.get("evidence_D", "III")) and strength == "D":
        level = book.level_caps["evidence_D"]  # type: ignore[assignment]
    if subject_confirmation != "confirmed" and _cap_rank(level) > _cap_rank(book.level_caps.get("subject_unconfirmed", "III")):
        level = book.level_caps["subject_unconfirmed"]  # type: ignore[assignment]
    status = "human_review_required" if (level == "I" or book.requires_review(rule_id) or subject_confirmation != "confirmed") else "open"
    return Finding(
        finding_id=stable_id("FD", {"rule": rule_id, "key": key}),
        rule_id=rule_id,
        rules_version=book.version,
        domain=book.rules[rule_id]["domain"],  # type: ignore[arg-type]
        level=level,
        evidence_strength=strength,
        supplier_ids=supplier_ids,
        subject_confirmation=subject_confirmation,  # type: ignore[arg-type]
        fact=fact,
        evidence_ids=sorted(set(evidence_ids)),
        related_ids=related or [],
        status=status,  # type: ignore[arg-type]
        recommendation=recommendation or "核对原始标书/证据后由人工复核确认或排除",
        procurement_clause_note=clause_note,
        missing_materials=missing or [],
    )


def _cap_rank(level: str) -> int:
    return _CAP_RANK.get(level, 1)


def min_strength(ev_ids: list[str], evmap: dict[str, Evidence], default: EvidenceStrength = "A") -> EvidenceStrength:
    """发现强度 = 引用证据中最弱者（rank 最大）。无已知证据时用 default。"""
    names = {v: k for k, v in _STRENGTH_RANK.items()}
    ranks = [_STRENGTH_RANK[evmap[i].strength] for i in ev_ids if i in evmap]
    if not ranks:
        return default
    return names[max(ranks)]  # type: ignore[return-value]


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    rules_path: Path = typer.Option(None, help="规则文件（默认 rules/risk-rules.yaml）"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        interim = project_dir / "output/interim"
        inventory = InventoryFile(**load_json(interim / "inventory.json"))
        entities = EntitiesFile(**load_json(interim / "entities.json"))
        matches = MatchesFile(**load_json(interim / "matches.json"))
        meta = MetadataFile(**load_json(interim / "metadata.json"))
        ext = ExternalEvidenceFile(**load_json(interim / "external.json"))
        content = ContentFile(**load_json(interim / "content.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    evmap: dict[str, Evidence] = {}
    for name in ("evidence-content.json", "evidence-metadata.json", "evidence-external.json",
                 "evidence-external-queries.json"):
        p = interim / name
        if p.exists():
            for e in load_json(p).get("evidence", []):
                ev = Evidence(**e)
                evmap[ev.evidence_id] = ev

    book = RuleBook(rules_path or RULES_PATH)
    findings: list[Finding] = []
    findings += _identity_rules(book, entities, matches, evmap)
    findings += _metadata_rules(book, cfg, inventory, entities, meta, evmap)
    findings += _ownership_rules(book, cfg, ext, evmap)
    findings += _external_record_rules(book, cfg, ext, evmap, project_dir=project_dir)
    coverage = _build_coverage(ext, entities)
    findings += _coverage_rule(book, coverage, evmap)

    findings.sort(key=lambda f: (f.rule_id, f.finding_id))
    queue = sorted(f.finding_id for f in findings if f.status == "human_review_required")
    result = FindingsFile(run=inventory.run, findings=findings, coverage=coverage, human_review_queue=queue)
    write_json(interim / "findings.json", result.model_dump(mode="json"))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
    typer.secho(
        f"[OK] 规则评估完成（规则版本 {book.version}）：I 级 {counts.get('I', 0)}、"
        f"II 级 {counts.get('II', 0)}、III 级 {counts.get('III', 0)}；人工复核 {len(queue)} 项 → findings.json",
        fg=typer.colors.GREEN,
    )


def _sup_id(entities: EntitiesFile, directory: str) -> str:
    for s in entities.suppliers:
        if s.directory_name == directory:
            return s.supplier_id
    return directory


# --------------------------------------------------------------------- ID 规则

_ID_FIELD_RULE = {
    "uscc": "ID-001",
    "legal_rep_id_digest": "ID-002",
    "bid_agent_id_digest": "ID-002",
    "phone": "ID-003",
    "email": "ID-003",
    "contact_name": "ID-003",
    "address": "ID-003",
}
_NAME_FIELDS = ("legal_rep_name", "bid_agent_name", "shareholder_name")


def _field_label(field: str) -> str:
    return {
        "uscc": "统一社会信用代码",
        "legal_rep_id_digest": "法定代表人证件号（受控摘要）",
        "bid_agent_id_digest": "授权代表证件号（受控摘要）",
        "legal_rep_name": "法定代表人姓名",
        "bid_agent_name": "授权代表姓名",
        "shareholder_name": "股东姓名",
        "contact_name": "联系人",
        "phone": "联系电话",
        "email": "电子邮箱",
        "address": "地址",
        "company_name": "公司名称",
    }.get(field, field)


def _identity_rules(book: RuleBook, entities: EntitiesFile, matches: MatchesFile, evmap) -> list[Finding]:
    out: list[Finding] = []
    for m in matches.matches:
        rule = _ID_FIELD_RULE.get(m.field)
        a, b = _sup_id(entities, m.supplier_a), _sup_id(entities, m.supplier_b)
        pair = sorted([a, b])
        strength = min_strength(m.evidence_ids, evmap, "A")
        if rule in ("ID-001", "ID-002") and m.match_type == "exact":
            out.append(_finding(
                book, rule,
                {"rule": rule, "pair": pair, "field": m.field, "value": m.value_a},
                supplier_ids=pair,
                evidence_ids=m.evidence_ids,
                fact=(
                    f"不同供应商（{pair[0]} 与 {pair[1]}）的标书材料中，{_field_label(m.field)}完全一致"
                    f"（比对值：{m.value_a}）。该线索需人工核对原件、证据来源与生成链路后才能进一步定性。"
                ),
                strength=strength,
                related=[m.match_id],
                recommendation="人工核对两份标书原件中的对应字段及其证据定位，确认是否存在笔误、代填或其他原因",
            ))
        elif rule == "ID-003" and m.match_type == "exact":
            out.append(_finding(
                book, "ID-003",
                {"rule": "ID-003", "pair": pair, "field": m.field, "value": m.value_a},
                supplier_ids=pair,
                evidence_ids=m.evidence_ids,
                fact=f"供应商 {pair[0]} 与 {pair[1]} 的{_field_label(m.field)}完全一致（{m.value_a}）。",
                strength=strength,
                related=[m.match_id],
            ))
        elif m.field == "address" and m.match_type == "candidate":
            out.append(_finding(
                book, "ID-003W",
                {"rule": "ID-003W", "pair": pair, "field": m.field, "value": m.value_a},
                supplier_ids=pair,
                evidence_ids=m.evidence_ids,
                fact=f"供应商 {pair[0]} 与 {pair[1]} 的地址属于同一行政区划线索（{m.value_a}）；仅此不构成有效关联。",
                strength=strength,
                related=[m.match_id],
                level_override="III",
            ))
        elif m.field in _NAME_FIELDS and m.match_type == "exact":
            out.append(_finding(
                book, "ID-003W",
                {"rule": "ID-003W-name", "pair": pair, "field": m.field, "value": m.value_a},
                supplier_ids=pair,
                evidence_ids=m.evidence_ids,
                fact=(
                    f"供应商 {pair[0]} 与 {pair[1]} 的标书中出现同名{_field_label(m.field)}（{m.value_a}）。"
                    "同名仅为候选线索：需结合主体信息、股权或公开证据确认是否为同一自然人。"
                ),
                strength=strength,
                related=[m.match_id],
                level_override="III",
            ))
        elif m.field == "company_name" and m.match_type == "candidate":
            out.append(_finding(
                book, "ID-003W",
                {"rule": "ID-003W-company", "pair": pair, "field": "company_name", "value": m.value_a},
                supplier_ids=pair,
                evidence_ids=m.evidence_ids,
                fact=(
                    f"供应商 {pair[0]} 与 {pair[1]} 的名称检索相似（相似度 {m.similarity}）。"
                    "名称相近仅为主体候选线索；两者主体各自独立，相关记录不合并、不互相归属。"
                ),
                strength=strength,
                related=[m.match_id],
                level_override="III",
            ))
    return out


# --------------------------------------------------------------------- META 规则


def _doc_supplier(inventory: InventoryFile, document_id: str) -> str | None:
    for d in inventory.documents:
        if d.document_id == document_id:
            return d.supplier_dir
    return None


def _mentions_project(value: str, project_name: str) -> bool:
    return bool(project_name) and project_name in value


def _parse_dt(text: str) -> datetime | None:
    from datetime import timezone

    from tc.normalize import parse_pdf_date

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = parse_pdf_date(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 无时区元数据按 UTC 统一比较
    return dt


def _is_limited_field(key: str, book: RuleBook) -> bool:
    pats = [x.lower() for x in book.params.get("limited_metadata_fields", [])]
    return any(fnmatch.fnmatch(key, pat) for pat in pats)


def _is_unique_field(key: str, book: RuleBook) -> bool:
    pats = [x.lower() for x in book.params.get("unique_metadata_fields", [])]
    return any(fnmatch.fnmatch(key, pat) for pat in pats)


def _is_generic(field: str, value: str, book: RuleBook) -> bool:
    v = (value or "").strip().lower()
    key = field.rsplit(".", 1)[-1]
    if key in ("author", "lastmodifiedby"):
        return v in [x.lower() for x in book.params.get("generic_author_values", [])]
    if key in ("creator", "producer", "application"):
        return v in [x.lower() for x in book.params.get("generic_creator_values", [])]
    return not v


def _leaf_key(field: str) -> str:
    return field.lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1]


def _independence_key(leaf: str) -> str:
    """Make/Model/Software 同属设备身份，相关性高，计数为一个独立字段。"""
    return "device(exif)" if leaf in ("make", "model", "software") else leaf


def _metadata_rules(book: RuleBook, cfg: ProjectConfig, inventory: InventoryFile,
                    entities: EntitiesFile, meta: MetadataFile, evmap) -> list[Finding]:
    params = book.params
    window_min = float(params.get("metadata_time_window_minutes", 30))
    need_for_ii = int(params.get("metadata_independent_fields_required_for_ii", 2))

    # 供应商目录 → 该供应商文件的字段集合
    unique_map: dict[str, set[str]] = {}      # 标识值 → 供应商目录集合
    unique_ev: dict[str, set[str]] = {}
    limited_map: dict[tuple[str, str], set[str]] = {}  # (field, value) → 供应商目录集合
    limited_ev: dict[tuple[str, str], set[str]] = {}
    tool_map: dict[tuple[str, str], set[str]] = {}     # (leaf, value) → 供应商集合（弱线索）
    tool_ev: dict[tuple[str, str], set[str]] = {}
    created: dict[str, list] = {}             # 供应商目录 → [(dt, ev_id)]

    for dm in meta.documents:
        sup = _doc_supplier(inventory, dm.document_id)
        if not sup:
            continue
        for f in dm.fields:
            if not f.evidence_id:
                continue
            key = f.field.lower()
            val = f.raw_value.strip()
            if not val:
                continue
            if _is_unique_field(key, book):
                uval = val.upper()
                unique_map.setdefault(uval, set()).add(sup)
                unique_ev.setdefault(uval, set()).add(f.evidence_id)
                continue
            leaf = _leaf_key(key)
            if leaf in ("producer", "creator", "application", "creatortool"):
                tool_map.setdefault((leaf, val), set()).add(sup)
                tool_ev.setdefault((leaf, val), set()).add(f.evidence_id)
                continue
            if leaf in ("creationdate", "created", "datetimeoriginal"):
                dt = _parse_dt(val)
                if dt:
                    created.setdefault(sup, []).append((dt, f.evidence_id))
                continue
            if _is_limited_field(key, book) and not _is_generic(f.field, val, book) \
                    and not _mentions_project(val, cfg.project_name):
                lkey = (_independence_key(leaf), val)
                limited_map.setdefault(lkey, set()).add(sup)
                limited_ev.setdefault(lkey, set()).add(f.evidence_id)

    sup_dirs = sorted({s.directory_name for s in entities.suppliers})
    out: list[Finding] = []
    for i in range(len(sup_dirs)):
        for j in range(i + 1, len(sup_dirs)):
            a, b = sup_dirs[i], sup_dirs[j]
            sa, sb = _sup_id(entities, a), _sup_id(entities, b)
            pair = sorted([sa, sb])

            shared_unique = sorted(v for v, sups in unique_map.items() if {a, b} <= sups)
            if shared_unique:
                evs = sorted(set().union(*(unique_ev[v] for v in shared_unique)))
                out.append(_finding(
                    book, "META-002",
                    {"rule": "META-002", "pair": pair, "values": shared_unique},
                    supplier_ids=pair,
                    evidence_ids=evs,
                    fact=(
                        f"供应商 {pair[0]} 与 {pair[1]} 的文件中出现完全相同的设备/文档唯一标识"
                        f"（{len(shared_unique)} 项，值见证据索引）。这是 I 级候选线索：仍需人工核验原件与"
                        "生成链路，不得仅据此作出认定。"
                    ),
                    strength=min_strength(evs, evmap, "A"),
                    recommendation="人工核验两份文件原件、该标识的完整上下文与文件来源链路",
                ))

            shared_limited = sorted((k for k, sups in limited_map.items() if {a, b} <= sups))
            if shared_limited:
                evs = sorted(set().union(*(limited_ev[k] for k in shared_limited)))
                fields = sorted({"扫描设备属性" if k[0] == "device(exif)" else k[0] for k in shared_limited})
                level: AlertLevel = "II" if len(fields) >= need_for_ii else "III"
                out.append(_finding(
                    book, "META-001",
                    {"rule": "META-001", "pair": pair, "values": [list(k) for k in shared_limited]},
                    supplier_ids=pair,
                    evidence_ids=evs,
                    fact=(
                        f"供应商 {pair[0]} 与 {pair[1]} 的文件存在 {len(shared_limited)} 项相同的非通用元数据"
                        f"（字段：{'、'.join(fields)}）。元数据同源属线索而非结论，可能由正常制作流程产生。"
                    ),
                    strength=min_strength(evs, evmap, "A"),
                    level_override=level,
                ))

            weak_reasons: list[str] = []
            weak_ev: set[str] = set()
            for (leaf, val), sups in sorted(tool_map.items()):
                if {a, b} <= sups:
                    weak_reasons.append(f"编辑工具（{leaf}）一致：{val}")
                    weak_ev |= tool_ev.get((leaf, val), set())
            for da, eva in created.get(a, []):
                for db_, evb in created.get(b, []):
                    delta = abs((da - db_).total_seconds()) / 60
                    if delta <= window_min:
                        weak_reasons.append(f"创建时间相近（相差约 {delta:.0f} 分钟）")
                        weak_ev.update({eva, evb})
            if weak_reasons:
                out.append(_finding(
                    book, "META-003",
                    {"rule": "META-003", "pair": pair, "reasons": sorted(set(weak_reasons))},
                    supplier_ids=pair,
                    evidence_ids=sorted(weak_ev),
                    fact=(
                        f"供应商 {pair[0]} 与 {pair[1]} 的文件存在弱同源线索：{'；'.join(sorted(set(weak_reasons)))}。"
                        "此类属常见或可轻易修改的信息，仅作为线索列出，不得单独用于任何否决性判断。"
                    ),
                    strength=min_strength(sorted(weak_ev), evmap, "A"),
                    level_override="III",
                ))
    return out


# --------------------------------------------------------------------- OWN / JUD / DIS


def _edge_desc(e: OwnershipRelation) -> str:
    target = e.to_company_name or ""
    bits = []
    if e.from_company_name:
        bits.append(f"{e.from_company_name} 出资")
    if e.share_ratio is not None:
        bits.append(f"持股 {e.share_ratio}%")
    when = f"自 {e.relation_date or '未知日期'} 起" + (f" 至 {e.relation_end_date}" if e.relation_end_date else "")
    return f"{target}（{'，'.join(bits) if bits else '关联'}，{when}）"


def _effective_at(e: OwnershipRelation, deadline) -> bool:
    if deadline is None:
        return True  # 无截点时不判定，事实照常列出并注明
    dl = deadline.date() if hasattr(deadline, "date") else deadline
    start_ok = (e.relation_date is None) or (e.relation_date <= dl)
    end_ok = (e.relation_end_date is None) or (e.relation_end_date >= dl)
    return start_ok and end_ok


def _fmt_deadline(deadline) -> str:
    return "未提供" if deadline is None else str(deadline)[:16]


def _ownership_rules(book: RuleBook, cfg: ProjectConfig, ext: ExternalEvidenceFile, evmap) -> list[Finding]:
    deadline = cfg.bid_deadline
    out: list[Finding] = []
    groups: dict[str, list[OwnershipRelation]] = {}
    for e in ext.ownership:
        key = e.from_party_digest or e.from_party_name or e.from_company_name or ""
        if key:
            groups.setdefault(key, []).append(e)
    for party_key, es in sorted(groups.items()):
        by_supplier: dict[str, OwnershipRelation] = {}
        for e in es:
            if e.supplier_id:
                by_supplier.setdefault(e.supplier_id, e)
        if len(by_supplier) < 2:
            continue
        sups = sorted(by_supplier)
        for i in range(len(sups)):
            for j in range(i + 1, len(sups)):
                ea, eb = by_supplier[sups[i]], by_supplier[sups[j]]
                pair = sorted([sups[i], sups[j]])
                ev = sorted(set(ea.evidence_ids) | set(eb.evidence_ids))
                subject = "confirmed" if all(
                    e.subject_confirmation == "confirmed" for e in (ea, eb)
                ) else "candidate"
                strength = min_strength(ev, evmap, "C")
                label = ea.from_party_name or ea.from_company_name or party_key[:8]
                if _effective_at(ea, deadline) and _effective_at(eb, deadline):
                    out.append(_finding(
                        book, "OWN-001",
                        {"rule": "OWN-001", "party": party_key, "pair": pair},
                        supplier_ids=pair,
                        evidence_ids=ev,
                        fact=(
                            f"导入的股权资料显示：{label} 在投标截止日（{_fmt_deadline(deadline)}）同时与"
                            f"供应商 {pair[0]}、{pair[1]} 存在股权/控制关系（{_edge_desc(ea)}；{_edge_desc(eb)}）。"
                        ),
                        strength=strength,
                        subject_confirmation=subject,
                        related=[ea.relation_id, eb.relation_id],
                        recommendation="人工核对股权原始凭证与截点有效性，并结合采购文件条款判断影响",
                    ))
                else:
                    note = []
                    if not _effective_at(ea, deadline):
                        note.append(f"{_edge_desc(ea)}（不在截点内）")
                    if not _effective_at(eb, deadline):
                        note.append(f"{_edge_desc(eb)}（不在截点内）")
                    out.append(_finding(
                        book, "OWN-001",
                        {"rule": "OWN-001-history", "party": party_key, "pair": pair},
                        supplier_ids=pair,
                        evidence_ids=ev,
                        fact=(
                            f"导入的股权资料显示 {label} 与供应商 {pair[0]}、{pair[1]} 存在股权关系，但生效区间"
                            f"未完全覆盖投标截止日（{_fmt_deadline(deadline)}）：{'；'.join(note)}。历史关系仅作低级线索列出。"
                        ),
                        strength=strength,
                        subject_confirmation=subject,
                        related=[ea.relation_id, eb.relation_id],
                        level_override="III",
                    ))
    return out


def _record_desc(rec: ExternalRecord) -> str:
    f = rec.fields
    title = f.get("行为") or f.get("处罚") or f.get("处理结果") or f.get("title") or f.get("visible_result") or ""
    no = f.get("处罚决定文号") or f.get("处理编号") or f.get("案号") or f.get("决定书文号") or ""
    date = f.get("处理日期") or f.get("处罚日期") or f.get("发布日期") or ""
    who = rec.subject_name or f.get("当事人") or ""
    bits = [str(x) for x in (who, title, no, date) if x]
    if bits:
        return f"[{rec.source_id}] 查到记录：{'；'.join(bits)}"
    return f"[{rec.source_id}] 查到记录：{'、'.join(f'{k}={v}' for k, v in list(rec.fields.items())[:6])}"


def _judicial_fact(rec: ExternalRecord) -> str:
    f = rec.fields
    bits = [str(b) for b in (
        rec.subject_name or f.get("当事人") or "",
        f.get("案号") or "", f.get("法院") or "", f.get("案由") or "",
    ) if b]
    return f"[{rec.source_id}] 查到司法/经营风险记录：{'；'.join(bits) if bits else str(f)[:120]}"


def _validity_note(rec: ExternalRecord, deadline) -> str:
    bits = []
    if rec.effective_from or rec.effective_to:
        bits.append(f"记录载明期间：{rec.effective_from or '未知'} 至 {rec.effective_to or '未载明'}")
    dl = deadline.date() if hasattr(deadline, "date") else deadline if deadline else None
    if dl is None:
        bits.append("项目未提供投标截止日，无法判断是否处于有效期")
    elif rec.effective_from and rec.effective_to:
        if rec.effective_from <= dl <= rec.effective_to:
            bits.append(f"以投标截止日（{dl}）判断，该记录可能处于有效期内；以处罚决定原文为准")
        elif dl > rec.effective_to:
            bits.append(f"以投标截止日（{dl}）判断，记录载明期间已届满；以处罚决定原文为准")
        else:
            bits.append(f"以投标截止日（{dl}）判断，记录载明期间尚未开始或无法判断")
    else:
        bits.append(f"投标截止日为 {dl}；记录未载明完整有效期，需人工核对处罚决定原文")
    return "；".join(bits) + "。"


def _load_clause_mapping(cfg: ProjectConfig, project_dir: Path | None = None) -> dict[str, list[str]]:
    """读取已确认的采购条款映射：rule_id -> [「条款引文」, …]。仅注释，不改等级。"""
    if not cfg.procurement_clauses_mapping:
        return {}
    base = project_dir or Path(".")
    path = base / cfg.procurement_clauses_mapping
    if not path.exists():
        path = Path(cfg.procurement_clauses_mapping)
    if not path.exists():
        return {}
    try:
        import yaml as _yaml

        data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 映射文件损坏不应阻断评估，按无映射处理
        return {}
    clauses = {c.get("id"): (c.get("quote") or "") for c in (data.get("clauses") or [])
               if isinstance(c, dict) and c.get("id")}
    mapping: dict[str, list[str]] = {}
    for rule_id, clause_ids in (data.get("rule_mapping") or {}).items():
        quotes = [clauses[cid][:80] for cid in (clause_ids or []) if cid in clauses]
        if quotes:
            mapping[str(rule_id)] = quotes
    return mapping


def _external_record_rules(book: RuleBook, cfg: ProjectConfig, ext: ExternalEvidenceFile, evmap,
                           project_dir: Path | None = None) -> list[Finding]:
    clause_mapping = _load_clause_mapping(cfg, project_dir)
    out: list[Finding] = []
    clause_available = cfg.procurement_rules_source not in (None, "", "unspecified")
    for rec in ext.records:
        strength = min_strength(rec.evidence_ids, evmap, "C")
        if rec.record_kind in ("dishonesty", "penalty"):
            rule = DIS_SOURCE_RULE.get(rec.source_id)
            if not rule:
                continue
            desc = _record_desc(rec)
            validity = _validity_note(rec, cfg.bid_deadline)
            quotes = clause_mapping.get(rule) or clause_mapping.get(rule.split("-unconfirmed")[0]) or []
            quote_note = ("对应采购条款: " + "；".join(f"「{q}」" for q in quotes)) if quotes else None
            if rec.subject_confirmation == "confirmed" and rec.supplier_id:
                out.append(_finding(
                    book, rule,
                    {"rule": rule, "record": rec.record_id},
                    supplier_ids=[rec.supplier_id],
                    evidence_ids=rec.evidence_ids,
                    fact=desc + validity,
                    strength=strength,
                    subject_confirmation="confirmed",
                    related=[rec.record_id],
                    clause_note=quote_note if quote_note else (
                        None if clause_available else "存在记录，资格影响待采购人确认"),
                    missing=[] if (clause_available or quote_note) else ["采购文件资格/否决条款"],
                    recommendation="人工核对处罚/处理决定原文、有效期与采购文件资格条款的对应关系",
                ))
            else:
                out.append(_finding(
                    book, rule,
                    {"rule": rule + "-unconfirmed", "record": rec.record_id},
                    supplier_ids=[],
                    evidence_ids=rec.evidence_ids,
                    fact=desc + " 记录主体未经确认，不归属任何供应商。",
                    strength=strength,
                    subject_confirmation=rec.subject_confirmation,  # type: ignore[arg-type]
                    related=[rec.record_id],
                    level_override="III",
                ))
        elif rec.record_kind == "judicial_case":
            fact = _judicial_fact(rec)
            if rec.subject_confirmation == "confirmed" and rec.supplier_id:
                out.append(_finding(
                    book, "JUD-001",
                    {"rule": "JUD-001", "record": rec.record_id},
                    supplier_ids=[rec.supplier_id],
                    evidence_ids=rec.evidence_ids,
                    fact=fact + " 该记录与本项目资格条件的相关性待确认。",
                    strength=strength,
                    subject_confirmation="confirmed",
                    related=[rec.record_id],
                    clause_note=None if clause_available else "存在记录，资格影响待采购人确认",
                    missing=[] if clause_available else ["采购文件资格/否决条款"],
                ))
            else:
                out.append(_finding(
                    book, "JUD-001",
                    {"rule": "JUD-001-unconfirmed", "record": rec.record_id},
                    supplier_ids=[],
                    evidence_ids=rec.evidence_ids,
                    fact=fact + " 记录主体未经确认（缺少统一社会信用代码或其他主体标识），不归属任何供应商。",
                    strength=strength,
                    subject_confirmation="unconfirmed",
                    related=[rec.record_id],
                    level_override="III",
                ))
    return out


# --------------------------------------------------------------------- 覆盖


def _build_coverage(ext: ExternalEvidenceFile, entities: EntitiesFile) -> list[CoverageEntry]:
    from tc.normalize import normalize_company_name

    out = []
    for s in entities.suppliers:
        for sid in ALL_SOURCES:
            q = next((x for x in ext.queries if x.source_id == sid and x.subject_supplier_id == s.supplier_id), None)
            if q is None and s.normalized_name:
                # 主体未归属但名称一致的导入记录：视为该供应商的 needs/blocked 等状态线索
                q = next((x for x in ext.queries
                          if x.source_id == sid and x.subject_supplier_id is None
                          and normalize_company_name(str((x.subject_key or {}).get("name") or "")) == s.normalized_name), None)
            if q:
                out.append(CoverageEntry(supplier_id=s.supplier_id, source_id=sid,  # type: ignore[arg-type]
                                         status=q.status, detail=q.detail, query_id=q.query_id))
            else:
                out.append(CoverageEntry(supplier_id=s.supplier_id, source_id=sid,  # type: ignore[arg-type]
                                         status="not_queried", detail="无查询/导入记录"))
    return out


def _coverage_rule(book: RuleBook, coverage: list[CoverageEntry], evmap) -> list[Finding]:
    by_supplier: dict[str, list[CoverageEntry]] = {}
    for c in coverage:
        if c.status not in ("match", "no_match_verified"):
            by_supplier.setdefault(c.supplier_id, []).append(c)
    out = []
    for sup, items in sorted(by_supplier.items()):
        bad = sorted(items, key=lambda c: (c.source_id, c.status))
        desc = "；".join(f"{c.source_id}={c.status}" for c in bad)
        out.append(_finding(
            book, "COV-001",
            {"rule": "COV-001", "supplier": sup, "sources": [(c.source_id, c.status) for c in bad]},
            supplier_ids=[sup],
            evidence_ids=[],
            fact=(
                f"供应商 {sup} 的外部查询覆盖存在缺口：{desc}。"
                "未完成或受限的查询不产生任何“未发现风险”结论。"
            ),
            strength="D",
            recommendation="补齐人工查询或授权导入；对 blocked/failed 渠道在复核时说明原因",
        ))
    return out


if __name__ == "__main__":
    app()
