"""报告生成前的富奥 SRM 查询门禁。"""

from __future__ import annotations

from .models import EntitiesFile, ExternalEvidenceFile


REPORT_ALLOWED_SRM_STATUSES = {"match", "no_match_verified", "no_result"}
LIVE_QUERY_MODES = {"official_api", "browser_session"}


class SrmReportGateError(ValueError):
    """SRM 登录/查询未达到报告生成条件。"""


def validate_srm_report_gate(entities: EntitiesFile, external: ExternalEvidenceFile) -> None:
    """要求每个投标人都有本次运行的已认证 SRM 查询。

    ``no_result`` / ``no_match_verified`` 表示登录及查询已完成但没有可用记录，
    按业务要求允许出报告；``not_queried``、``needs_manual_review``、``blocked``、
    ``failed`` 或手工导入均不能替代本次 SRM 登录查询。
    """
    by_supplier: dict[str, list] = {}
    for query in external.queries:
        if query.source_id == "srm" and query.subject_supplier_id:
            by_supplier.setdefault(query.subject_supplier_id, []).append(query)

    problems: list[str] = []
    for supplier in entities.suppliers:
        candidates = by_supplier.get(supplier.supplier_id, [])
        if not candidates:
            problems.append(f"{supplier.display_name}=缺少 SRM 查询记录")
            continue
        # 同一主体可能有历史导入和本次刷新记录；只接受最近一次已认证实时查询。
        live = [q for q in candidates if q.query_mode in LIVE_QUERY_MODES and q.queried_at is not None]
        if not live:
            problems.append(f"{supplier.display_name}=未完成本次 SRM 登录查询（现有记录不是实时认证查询）")
            continue
        query = sorted(
            live,
            key=lambda q: ((q.queried_at.isoformat() if q.queried_at else ""), q.query_id),
        )[-1]
        if query.status not in REPORT_ALLOWED_SRM_STATUSES:
            problems.append(f"{supplier.display_name}={query.status}：{query.detail or 'SRM 查询未完成'}")

    if problems:
        raise SrmReportGateError("SRM 报告门禁未通过：" + "；".join(problems))
