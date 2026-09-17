#!/usr/bin/env python3
"""阶段四（查询通道）：按适配器执行外部查询，或如实记录未查询状态。

- external_query_mode=offline / manual_import：不发起任何网络访问，
  逐供应商 × 渠道生成 not_queried（或沿用导入结果）；
- external_query_mode=live：仅访问 project.yaml 中明确启用的渠道（external_query_sources）；
  遇到验证码/登录墙/限流 → blocked；主体无信用代码 → needs_manual_review；
- 查询结果与导入证据合并写入 external.json，同一供应商×渠道仅保留信息量最高的状态。

禁止：绕过人机校验、登录未授权账户、突破限频。真实站点接入需在授权环境验收。
"""

from __future__ import annotations
import getpass
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import content_hash, load_json, stable_id, write_json
from tc.models import (
    Evidence,
    EvidenceFile,
    EntitiesFile,
    ExternalEvidenceFile,
    ExternalQuery,
    InventoryFile,
    ProjectConfig,
    QueryMode,
    Supplier,
)
from tc.projio import EvidenceBuilder, ProjectError, ensure_output_dirs, load_project_config
from tc.normalize import normalize_company_name
from tc.sources import (
    AdapterResult,
    QuerySubject,
    SourceConfig,
    build_adapters,
    load_source_config,
)

# 状态优先级：数字越大越有信息量；合并时保留高者
STATUS_PRECEDENCE = {
    "not_queried": 0,
    "needs_manual_review": 1,
    "no_result": 2,
    "failed": 3,
    "blocked": 4,
    "no_match_verified": 5,
    "match": 6,
}

ALL_SOURCES = ["government_procurement", "srm"]
LIVE_QUERY_MODES = {
    "government_procurement": {"official_api", "public_web"},
    "srm": {"browser_session"},
}
REUSABLE_LIVE_STATUSES = {"match", "no_match_verified", "no_result"}

app = typer.Typer(help="执行外部查询并生成查询覆盖记录（合并入 external.json）")


def _srm_cli_status_callback(event) -> None:
    """把库层脱敏事件转换为不回显详情的 CLI 提示。"""
    state = getattr(event, "state", "")
    attempt = int(getattr(event, "attempt", 0) or 0)
    messages = {
        "awaiting_manual_login": "请在已打开的 SRM 浏览器窗口完成账号、密码和验证码输入。",
        "reopening": f"检测到浏览器关闭，正在进行第 {attempt} 次有限重开。",
        "authenticated": "SRM 登录成功，开始查询主体。",
        "manual_review": "人工登录未完成或已超时，未继续主体查询，请人工复核。",
        "failed": "SRM 浏览器操作失败或已关闭，请人工复核。",
        "blocked": "SRM 登录或访问被阻断，已停止查询。",
    }
    message = messages.get(state)
    if message:
        typer.secho(f"[SRM] {message}", fg=typer.colors.YELLOW if state != "authenticated" else typer.colors.GREEN)


def _query_subject_for_supplier(supplier) -> QuerySubject:
    """从实体构造外部查询主体；优先采用归组确认后的显示名称。"""
    return QuerySubject(
        supplier.supplier_id,
        supplier.display_name or supplier.declared_name,
        supplier.uscc,
    )


def _is_reusable_query(
    sid: str,
    supplier,
    query: ExternalQuery | None,
    *,
    live_sources: set[str],
    refresh: bool,
    same_run: bool,
) -> bool:
    """判断是否可复用同一运行的成功 live 结果，禁止跨运行借用旧查询。"""
    if refresh or not same_run or sid not in live_sources or query is None:
        return False
    if query.status not in REUSABLE_LIVE_STATUSES:
        return False
    if query.query_mode not in LIVE_QUERY_MODES.get(sid, set()):
        return False
    key = query.subject_key or {}
    old_uscc = str(key.get("uscc") or "").strip().upper()
    new_uscc = str(supplier.uscc or "").strip().upper()
    if old_uscc and new_uscc and old_uscc != new_uscc:
        return False
    old_name = normalize_company_name(str(key.get("name") or ""))
    new_name = normalize_company_name(str(supplier.display_name or supplier.declared_name or ""))
    return bool(
        (old_uscc and new_uscc and old_uscc == new_uscc)
        or (old_name and old_name == new_name)
    )


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    config_path: Path = typer.Option(None, help="外部渠道配置文件（默认 rules/external-sources.yaml）"),
    interactive: bool = typer.Option(
        False, "--interactive", help="允许在命令启动时交互输入 SRM 凭据；流水线默认只读环境变量"
    ),
    max_total_seconds: int = typer.Option(
        300,
        min=0,
        help="实时外部查询总预算（秒）；0 表示不设预算。预算耗尽后未启动的查询保留 not_queried。",
    ),
    supplier_id: list[str] = typer.Option(
        [], "--supplier-id", help="只刷新指定供应商 ID；可重复传入，用于定向重试失败主体。"
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="强制刷新选定的 live 渠道；默认复用同一主体已成功的本次结果。",
    ),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
        entities_path = project_dir / "output/interim/entities.json"
        if entities_path.exists():
            entities = EntitiesFile(**load_json(entities_path))
        else:
            identity_path = project_dir / "output/interim/identity-candidates.json"
            if not identity_path.exists():
                raise ProjectError("缺少 entities.json；请先导入身份快速 OCR 并运行 prepare_identity.py")
            identity = load_json(identity_path)
            provisional = []
            for item in identity.get("candidates", []):
                provisional.append(Supplier(
                    supplier_id=str(item["supplier_id"]),
                    directory_name=str(item.get("supplier_dir") or item["normalized_name"]),
                    display_name=str(item["display_name"]),
                    declared_name=str(item["display_name"]),
                    normalized_name=str(item.get("normalized_name") or ""),
                    name_search_key=str(item.get("normalized_name") or "") or None,
                    uscc=item.get("uscc"),
                    uscc_status="present_valid" if item.get("uscc") else "absent",
                    uscc_candidates=list(item.get("uscc_candidates") or []),
                    confirmation="candidate",
                    confirmation_note="身份快速阶段候选；待全文主体阶段复核",
                ))
            if not provisional:
                raise ProjectError("identity-candidates.json 尚未形成可查询的供应商名称")
            entities = EntitiesFile(
                run=inventory.run, suppliers=provisional, parties=[], contacts=[],
                notes=["本次外部查询使用身份快速阶段候选，待全文主体阶段复核"],
            )
        ext_path = project_dir / "output/interim/external.json"
        ext = ExternalEvidenceFile(**load_json(ext_path)) if ext_path.exists() else ExternalEvidenceFile(
            run=inventory.run, queries=[], records=[], ownership=[]
        )
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    _out, interim = ensure_output_dirs(project_dir)
    same_run = ext.run.run_id == inventory.run.run_id
    builder = EvidenceBuilder(inventory.run, cfg.id_digest_salt, cfg.redaction_mode)
    prior_query_evidence: list[Evidence] = []
    if same_run:
        try:
            evidence_data = load_json(interim / "evidence-external-queries.json")
            prior_query_evidence = [Evidence(**item) for item in evidence_data.get("evidence", [])]
        except (FileNotFoundError, ProjectError, TypeError, ValueError):
            prior_query_evidence = []

    queries: dict[tuple[str, str], ExternalQuery] = {}
    adapter_records: list = []
    for q in ext.queries:
        key = (q.source_id, q.subject_supplier_id or "")
        if key not in queries or STATUS_PRECEDENCE[q.status] > STATUS_PRECEDENCE[queries[key].status]:
            queries[key] = q
    prior_queries = dict(queries)

    checkpoint_path = interim / "srm-query-checkpoint.json"
    checkpoint: dict[str, object] = {
        "schema_version": "tender-clearance.srm-query-checkpoint.v1",
        "run_id": inventory.run.run_id,
        "completed": {},
        "last_supplier_id": None,
    }
    if checkpoint_path.exists():
        try:
            saved = load_json(checkpoint_path)
            if saved.get("run_id") == inventory.run.run_id:
                checkpoint.update(saved)
        except (OSError, ValueError, TypeError):
            pass

    def persist_partial_snapshot() -> None:
        """每个主体/渠道完成后原子保存，进程中断也不丢已取得结果。"""
        refreshed_records = [
            r for r in ext.records
            if (r.source_id, r.supplier_id or "") not in refreshed_keys
            and not (set(r.evidence_ids) & replaced_evidence_ids)
        ]
        record_map = {r.record_id: r for r in [*refreshed_records, *adapter_records]}
        merged = ExternalEvidenceFile(
            run=inventory.run,
            queries=sorted(queries.values(), key=lambda q: (q.source_id, q.subject_supplier_id or "", q.query_id)),
            records=list(record_map.values()),
            ownership=ext.ownership,
        )
        write_json(interim / "external.json", merged.model_dump(mode="json"))
        evidence_map = {
            evidence.evidence_id: evidence
            for evidence in prior_query_evidence
            if evidence.evidence_id not in replaced_evidence_ids
        }
        evidence_map.update({evidence.evidence_id: evidence for evidence in builder.items})
        write_json(
            interim / "evidence-external-queries.json",
            EvidenceFile(run=inventory.run, evidence=list(evidence_map.values())).model_dump(mode="json"),
        )

    def record_checkpoint(supplier_id: str, status: str) -> None:
        checkpoint["last_supplier_id"] = supplier_id
        completed = checkpoint.setdefault("completed", {})
        if status in {"match", "no_result", "no_match_verified"}:
            completed[supplier_id] = status  # type: ignore[index]
        write_json(checkpoint_path, checkpoint)

    source_configs = load_source_config(config_path)
    live_sources = list(cfg.external_query_sources) if cfg.external_query_mode == "live" else []
    live_source_set = set(live_sources)

    runtime_credentials: dict[str, tuple[str, str]] = {}
    # 人工可见浏览器是默认路径；只有明确指定 runtime 才读取 SRM_USER/
    # SRM_PASSWORD，避免把密码问题带入用户交互流程。
    manual_browser_login = os.environ.get("SRM_BROWSER_LOGIN_MODE", "manual").strip().lower() != "runtime"
    if os.environ.get("SRM_BROWSER_MANUAL_LOGIN", "") == "1":
        manual_browser_login = True
    if "srm" in live_sources and not manual_browser_login:
        try:
            runtime_credentials["srm"] = _collect_srm_credentials(allow_interactive=interactive)
        except ProjectError as exc:
            typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

    adapters = {}
    if live_sources:
        adapters = build_adapters({sid: source_configs.get(sid, SourceConfig(source_id=sid, label=sid))
                                   for sid in live_sources},
                                  runtime_credentials=runtime_credentials,
                                  login_modes={"srm": "manual" if manual_browser_login else "runtime"},
                                  status_callbacks=(
                                      {"srm": _srm_cli_status_callback}
                                      if manual_browser_login else {}
                                  ))
    if manual_browser_login and "srm" in live_sources:
        typer.secho(
            "[SRM] 人工登录模式：即将打开可见浏览器，请在窗口内输入账号、密码并完成验证码；"
            "程序不会读取、填充或保存凭据。",
            fg=typer.colors.YELLOW,
        )

    now = datetime.now(timezone.utc)
    query_started = time.perf_counter()
    query_timings: list[dict[str, object]] = []
    new_queries: list[ExternalQuery] = []
    selected_supplier_ids = set(supplier_id) if supplier_id else {s.supplier_id for s in entities.suppliers}
    refreshed_keys: set[tuple[str, str]] = set()
    replaced_evidence_ids: set[str] = set()
    reused_count = 0
    for supplier in entities.suppliers:
        if supplier.supplier_id not in selected_supplier_ids:
            continue
        # 平铺归组/人工确认后的 display_name 才是参与投标的供应商主体；
        # declared_name 可能来自商务页中的招标人或模板抬头，不能直接用于 SRM。
        subject = _query_subject_for_supplier(supplier)
        for sid in ALL_SOURCES:
            key = (sid, supplier.supplier_id)
            if key in queries and sid not in live_sources:
                continue
            if _is_reusable_query(
                sid,
                supplier,
                queries.get(key),
                live_sources=live_source_set,
                refresh=refresh,
                same_run=same_run,
            ):
                reused_count += 1
                query_timings.append({
                    "source_id": sid,
                    "supplier_id": supplier.supplier_id,
                    "status": "reused",
                    "elapsed_seconds": 0.0,
                })
                continue
            if cfg.external_query_mode != "live" or sid not in live_sources:
                # 未发起查询：但若存在“主体未归属、名称与该供应商一致”的导入记录，
                # 如实标记为待人工确认，而不是 not_queried。
                same_name = None
                if supplier.normalized_name:
                    same_name = next(
                        (q for q in ext.queries
                         if q.source_id == sid and q.subject_supplier_id is None
                         and normalize_company_name(str((q.subject_key or {}).get("name") or "")) == supplier.normalized_name),
                        None,
                    )
                if same_name is not None:
                    queries[key] = _placeholder_query(
                        sid, subject, "needs_manual_review",
                        f"存在同名主体的导入记录（{same_name.query_id}），主体待人工确认", now)
                else:
                    queries[key] = _placeholder_query(sid, subject, "not_queried",
                                                      f"external_query_mode={cfg.external_query_mode}，未发起查询", now)
                persist_partial_snapshot()
            elif max_total_seconds and time.perf_counter() - query_started >= max_total_seconds:
                # 预算耗尽表示“本次未启动”，不能抹掉此前已有的 blocked/match
                # 结果及其记录；只有没有历史结果的主体才新增 not_queried 占位。
                if key not in prior_queries or not same_run:
                    queries[key] = _placeholder_query(
                        sid, subject, "not_queried",
                        f"本次实时外部查询已达到 {max_total_seconds}s 总预算，未启动此查询", now,
                    )
                    persist_partial_snapshot()
                query_timings.append({
                    "source_id": sid,
                    "supplier_id": supplier.supplier_id,
                    "status": "not_started_budget_exhausted",
                    "elapsed_seconds": 0.0,
                })
            elif (
                sid != "government_procurement"
                and supplier.confirmation == "unconfirmed"
                and not (supplier.uscc or supplier.declared_name)
            ):
                queries[key] = _placeholder_query(
                    sid, subject, "needs_manual_review",
                    "缺少企业名称和统一社会信用代码，无法发起 SRM 查询；需人工确认主体", now)
                persist_partial_snapshot()
            else:
                refreshed_keys.add(key)
                if key in prior_queries:
                    replaced_evidence_ids.update(prior_queries[key].evidence_ids)
                adapter = adapters[sid]
                one_started = time.perf_counter()
                result = adapter.query(subject, now)
                elapsed = round(time.perf_counter() - one_started, 3)
                q = _adapter_query(sid, subject, result, now, cfg, builder,
                                   version=getattr(adapter, "version", ADAPTER_VERSION_TAG))
                queries[key] = q
                adapter_records.extend(_record_from_adapter(q, result, subject, entities, sid))
                record_checkpoint(supplier.supplier_id, result.status) if sid == "srm" else None
                persist_partial_snapshot()
                query_timings.append({
                    "source_id": sid,
                    "supplier_id": supplier.supplier_id,
                    "status": result.status,
                    "elapsed_seconds": elapsed,
                })
                typer.secho(f"  [查询] {supplier.display_name} × {sid}: {result.status}（{elapsed:.2f}s）")

    # SRM 浏览器适配器在批量查询期间复用同一会话；所有供应商完成后显式销毁
    # 浏览器上下文，避免 Cookie/页面句柄在宿主进程中继续存活。
    for adapter in adapters.values():
        close = getattr(adapter, "close", None)
        if callable(close):
            close()

    # live 查询是本次运行对对应渠道的刷新结果：移除旧运行同渠道记录，
    # 再按 record_id 去重，避免反复重跑把同一 SRM 记录累加成虚假的数量。
    live_source_ids = set(live_sources)
    refreshed_records = [
        r for r in ext.records
        if (r.source_id, r.supplier_id or "") not in refreshed_keys
        and not (set(r.evidence_ids) & replaced_evidence_ids)
    ]
    record_map = {r.record_id: r for r in [*refreshed_records, *adapter_records]}
    all_records = list(record_map.values())
    merged = ExternalEvidenceFile(
        run=inventory.run,
        queries=sorted(queries.values(), key=lambda q: (q.source_id, q.subject_supplier_id or "", q.query_id)),
        records=all_records,
        ownership=ext.ownership,
    )
    write_json(interim / "external.json", merged.model_dump(mode="json"))
    # 外部刷新是独立资料获取任务：保留按渠道/主体/证据内容分层的快照缓存，
    # 后续报告只消费 external.json，不重复访问网络。
    for query in merged.queries:
        if query.source_id in live_source_ids and query.subject_supplier_id not in selected_supplier_ids:
            continue
        related = [r for r in all_records
                   if r.source_id == query.source_id and set(r.evidence_ids) & set(query.evidence_ids)]
        subject_key = content_hash(query.subject_key)[:16]
        cache_file = project_dir / "output/cache/external" / query.source_id / subject_key / f"{query.query_id}.json"
        write_json(cache_file, {
            "query": query.model_dump(mode="json"),
            "records": [r.model_dump(mode="json") for r in related],
        })
    evidence_map = {
        evidence.evidence_id: evidence
        for evidence in prior_query_evidence
        if evidence.evidence_id not in replaced_evidence_ids
    }
    evidence_map.update({evidence.evidence_id: evidence for evidence in builder.items})
    write_json(
        interim / "evidence-external-queries.json",
        EvidenceFile(run=inventory.run, evidence=list(evidence_map.values())).model_dump(mode="json"),
    )
    write_json(interim / "query-timings.json", {
        "schema_version": "tender-clearance.query-timings.v1",
        "total_seconds": round(time.perf_counter() - query_started, 3),
        "max_total_seconds": max_total_seconds or None,
        "selected_supplier_ids": sorted(selected_supplier_ids),
        "partial_refresh": bool(supplier_id),
        "forced_refresh": refresh,
        "reused_count": reused_count,
        "refreshed_keys": len(refreshed_keys),
        "queries": query_timings,
    })
    typer.secho(
        f"[OK] 查询覆盖完成：{len(merged.queries)} 条查询记录（模式={cfg.external_query_mode}，"
        f"live 渠道={live_sources or '无'}） → external.json",
        fg=typer.colors.GREEN,
    )


def _collect_srm_credentials(*, allow_interactive: bool = False) -> tuple[str, str]:
    """取得本次运行的 SRM 凭据；默认不在执行中突然询问。"""
    username = os.environ.get("SRM_USER", "").strip()
    password = os.environ.get("SRM_PASSWORD", "")
    if username and password:
        return username, password
    if not allow_interactive:
        raise ProjectError(
            "SRM 凭据未在运行开始前准备好；请先取得用户授权并设置 SRM_USER/SRM_PASSWORD，"
            "或显式使用 --interactive 在命令启动时输入"
        )
    if not sys.stdin.isatty():
        raise ProjectError("当前无可交互输入，请在运行前设置 SRM_USER/SRM_PASSWORD 后重试")
    while not username or not password:
        if not username:
            username = input("SRM 用户名: ").strip()
        if not password:
            password = getpass.getpass("SRM 密码: ")
        if not username or not password:
            typer.secho("SRM 用户名和密码不能为空，请重新输入。", fg=typer.colors.YELLOW, err=True)
            username = username.strip()
            password = ""
    return username, password


def _placeholder_query(sid: str, subject: QuerySubject, status: str, detail: str, now: datetime) -> ExternalQuery:
    return ExternalQuery(
        query_id=stable_id("Q", {"source": sid, "supplier": subject.supplier_id, "placeholder": detail[:30]}),
        source_id=sid,  # type: ignore[arg-type]
        subject_supplier_id=subject.supplier_id,
        subject_key={"uscc": subject.uscc, "name": subject.name},
        query_mode="none",
        queried_at=None,
        status=status,  # type: ignore[arg-type]
        record_count=0,
        evidence_ids=[],
        detail=detail,
        adapter_version="none",
    )


def _adapter_query(sid: str, subject: QuerySubject, result: AdapterResult, now: datetime, cfg: ProjectConfig, builder,
                   version: str | None = None) -> ExternalQuery:
    qid = stable_id("Q", {"source": sid, "supplier": subject.supplier_id, "uscc": subject.uscc, "at": "live"})
    ev_ids: list[str] = []
    if result.records:
        ev = builder.add(
            source_type="external_evidence",
            document_id=None,
            location={"kind": "url", "url": result.response_ref or sid, "query_mode": result.request_mode},
            field=f"external.{sid}",
            raw_value=f"适配器查询命中 {len(result.records)} 条记录（{subject.name or ''}/{subject.uscc or ''}）",
            method="adapter_query",
            strength="B",
            note=result.detail,
        )
        ev_ids.append(ev.evidence_id)
    # 查询键必须保留“本次实际请求的主体”。公开渠道页面中的招标人
    # 不能覆盖供应商查询键；只有 SRM 成功确认画像后才采用 resolved identity。
    use_resolved_identity = sid == "srm" and result.status == "match"
    resolved_name = (result.resolved_name if use_resolved_identity else None) or subject.name
    resolved_uscc = (result.resolved_uscc if use_resolved_identity else None) or subject.uscc
    return ExternalQuery(
        query_id=qid,
        source_id=sid,  # type: ignore[arg-type]
        subject_supplier_id=subject.supplier_id,
        subject_key={"uscc": resolved_uscc, "name": resolved_name},
        query_mode=result.request_mode if result.request_mode != "none" else "none",  # type: ignore[arg-type]
        # 只要实际发起过登录/查询，就记录时间；needs_manual_review 表示查询已执行
        # 但主体仍有歧义，不能再伪装成未查询。报告门禁会要求该状态明确列为待人工复核。
        queried_at=now if result.status != "not_queried" else None,
        status=result.status,  # type: ignore[arg-type]
        record_count=len(result.records),
        evidence_ids=ev_ids,
        detail=result.detail,
        adapter_version=version or ADAPTER_VERSION_TAG,
        branch_candidates=result.branch_candidates,
    )


def _record_from_adapter(q: ExternalQuery, result: AdapterResult, subject: QuerySubject, entities: EntitiesFile, sid: str):
    from tc.models import ExternalRecord
    from tc.normalize import normalize_company_name

    out = []
    # 名称查询成功后，SRM 详情页可能返回主体名称/信用代码；即使当前仍是
    # candidate，也要把记录挂到该供应商，避免报告把完整 SRM 结果显示成“未归属”。
    # 风险规则仍只对 confirmed 记录作正式主体归属。
    resolved_name = result.resolved_name or subject.name
    resolved_uscc = result.resolved_uscc or subject.uscc
    resolved_confirmation = result.subject_confirmation
    if resolved_confirmation not in {"confirmed", "candidate", "unconfirmed"}:
        resolved_confirmation = None
    def _comparison_name(value: str | None) -> str:
        value = re.sub(r"\s*[（(]\s*公章\s*[）)]\s*$", "", str(value or ""))
        return normalize_company_name(value)

    can_attach = bool(subject.supplier_id and (
        (resolved_uscc and subject.uscc and resolved_uscc.upper() == subject.uscc.upper())
        or (resolved_name and subject.name
            and _comparison_name(resolved_name) == _comparison_name(subject.name))
    ))
    for i, rec in enumerate(result.records):
        out.append(ExternalRecord(
            record_id=stable_id("R", {"q": q.query_id, "index": i, "fields": rec.fields}),
            source_id=sid,  # type: ignore[arg-type]
            supplier_id=subject.supplier_id if can_attach else None,
            subject_confirmation=(resolved_confirmation or rec.subject_confirmation),  # type: ignore[arg-type]
            subject_name=resolved_name,
            subject_uscc=resolved_uscc,
            record_kind=rec.record_kind,  # type: ignore[arg-type]
            fields=rec.fields,
            effective_from=None,
            effective_to=None,
            evidence_ids=list(q.evidence_ids),
        ))
    return out


ADAPTER_VERSION_TAG = "http/0.3.0"


if __name__ == "__main__":
    app()
