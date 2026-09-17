"""字段候选的来源质量与单值决策。

字段提取阶段必须保留所有候选；本模块只负责判断哪些候选可以进入
主体匹配或正式报告。任何多值、低质量或缺少可回溯定位的候选都不自动
选择，但会在返回值中保留证据 ID，供人工复核。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


PROJECT_FIELDS = ("project_name", "tenderer", "project_code", "bid_date")
SUPPLIER_FIELDS = (
    "company_name",
    "uscc",
    "legal_rep_name",
    "legal_rep_id",
    "bid_agent_name",
    "bid_agent_id",
)
CORE_FIELDS = frozenset((*PROJECT_FIELDS, *SUPPLIER_FIELDS))


@dataclass(frozen=True)
class FieldDecision:
    scope: str
    field: str
    status: str
    value: str | None
    normalized: str | None
    evidence_ids: tuple[str, ...]
    candidate_values: tuple[dict[str, Any], ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "field": self.field,
            "status": self.status,
            "value": self.value,
            "normalized": self.normalized,
            "evidence_ids": list(self.evidence_ids),
            "candidate_values": list(self.candidate_values),
            **({"reason": self.reason} if self.reason else {}),
        }


def _value(record: Any) -> str:
    return str(getattr(record, "normalized", None) or getattr(record, "value_masked", "") or "").strip()


def _location(record: Any) -> dict[str, Any]:
    location = getattr(record, "location", None)
    return location if isinstance(location, dict) else {}


def has_traceable_source(record: Any, *, core: bool = False) -> bool:
    """判断字段是否有可回看的页码、坐标或表格坐标。

    ``docx_body`` 的段落 index 不能等价替代页码；核心报告字段在缺少
    PDF/图片页码、OCR 坐标或 XLSX 单元格时不进入正式单值。
    """
    location = _location(record)
    kind = str(location.get("kind") or "")
    page = location.get("page")
    has_page = kind in {"pdf_page", "image_page"} and isinstance(page, int) and page >= 1
    has_bbox = any(isinstance(location.get(key), dict) for key in ("bbox", "label_bbox", "value_bbox"))
    has_cell = kind == "xlsx_cell" and bool(location.get("sheet")) and bool(location.get("cell"))
    if core:
        return has_page or has_bbox or has_cell
    return has_page or has_bbox or has_cell or (
        kind.startswith("docx_") and isinstance(location.get("index"), int)
    )


def is_match_eligible(record: Any) -> bool:
    """匹配前的最低门槛；低置信度和无定位字段永不参与精确匹配。"""
    if bool(getattr(record, "low_confidence", False)):
        return False
    field = str(getattr(record, "field", ""))
    return has_traceable_source(record, core=field in CORE_FIELDS)


def _candidate_payload(records: Iterable[Any]) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        value = _value(record)
        if not value:
            continue
        entry = grouped.setdefault(value, {
            "value": getattr(record, "value_masked", value),
            "normalized": getattr(record, "normalized", None),
            "evidence_ids": [],
            "locations": [],
        })
        evidence_id = str(getattr(record, "evidence_id", ""))
        if evidence_id and evidence_id not in entry["evidence_ids"]:
            entry["evidence_ids"].append(evidence_id)
        location = _location(record)
        if location and location not in entry["locations"]:
            entry["locations"].append(location)
    for entry in grouped.values():
        entry["evidence_ids"] = sorted(entry["evidence_ids"])
        entry["locations"] = sorted(entry["locations"], key=repr)
    return tuple(grouped[key] for key in sorted(grouped))


def resolve_single_value(records: Iterable[Any], *, scope: str, field: str) -> FieldDecision:
    """对一组候选做单值决策，绝不按频次或排序猜选。"""
    items = [record for record in records if _value(record)]
    candidates = _candidate_payload(items)
    evidence_ids = tuple(sorted({str(getattr(r, "evidence_id", "")) for r in items if getattr(r, "evidence_id", "")}))
    if not candidates:
        return FieldDecision(scope, field, "missing", None, None, evidence_ids, (), "未取得字段候选")
    if len(candidates) > 1:
        return FieldDecision(
            scope, field, "conflict", None, None, evidence_ids, candidates,
            f"发现 {len(candidates)} 个不同候选值，未自动选择",
        )
    candidate = candidates[0]
    eligible = [record for record in items if is_match_eligible(record)]
    if not eligible:
        return FieldDecision(
            scope, field, "needs_manual_review", None, None, evidence_ids, candidates,
            "候选缺少可回溯页码/坐标或置信度不足",
        )
    selected = max(
        eligible,
        key=lambda record: (
            float(getattr(record, "confidence", 0.0) or 0.0),
            str(getattr(record, "evidence_id", "")),
        ),
    )
    return FieldDecision(
        scope, field, "selected", str(getattr(selected, "value_masked", _value(selected))),
        getattr(selected, "normalized", None), evidence_ids, candidates,
    )


def decisions_for_fields(records: Iterable[Any]) -> list[FieldDecision]:
    """生成项目级和供应商级正式字段决策。"""
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for record in records:
        field = str(getattr(record, "field", ""))
        if field not in CORE_FIELDS:
            continue
        supplier_dir = getattr(record, "supplier_dir", None)
        scope = "project" if field in PROJECT_FIELDS else f"supplier:{supplier_dir or 'unassigned'}"
        grouped[(scope, field)].append(record)
    output: list[FieldDecision] = []
    for field in PROJECT_FIELDS:
        output.append(resolve_single_value(grouped.get(("project", field), []), scope="project", field=field))
    supplier_scopes = sorted({scope for scope, _field in grouped if scope.startswith("supplier:")})
    for scope in supplier_scopes:
        for field in SUPPLIER_FIELDS:
            output.append(resolve_single_value(grouped.get((scope, field), []), scope=scope, field=field))
    return output


def blocked_evidence_ids(records: Iterable[Any]) -> set[str]:
    """返回不能进入主体匹配的冲突/低质量候选证据。"""
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for record in records:
        supplier_dir = getattr(record, "supplier_dir", None)
        field = str(getattr(record, "field", ""))
        if supplier_dir and field in SUPPLIER_FIELDS:
            grouped[(str(supplier_dir), field)].append(record)
    blocked: set[str] = set()
    for (scope, field), items in grouped.items():
        decision = resolve_single_value(items, scope=f"supplier:{scope}", field=field)
        if decision.status != "selected":
            blocked.update(decision.evidence_ids)
    return blocked
