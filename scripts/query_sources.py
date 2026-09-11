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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import content_hash, load_json, stable_id, write_json
from tc.models import (
    EntitiesFile,
    ExternalEvidenceFile,
    ExternalQuery,
    InventoryFile,
    ProjectConfig,
    QueryMode,
)
from tc.projio import EvidenceBuilder, ProjectError, ensure_output_dirs, load_project_config
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

app = typer.Typer(help="执行外部查询并生成查询覆盖记录（合并入 external.json）")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    config_path: Path = typer.Option(None, help="外部渠道配置文件（默认 rules/external-sources.yaml）"),
    interactive: bool = typer.Option(
        False, "--interactive", help="允许在命令启动时交互输入 SRM 凭据；流水线默认只读环境变量"
    ),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        inventory = InventoryFile(**load_json(project_dir / "output/interim/inventory.json"))
        entities = EntitiesFile(**load_json(project_dir / "output/interim/entities.json"))
        ext = ExternalEvidenceFile(**load_json(project_dir / "output/interim/external.json"))
    except (ProjectError, FileNotFoundError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    _out, interim = ensure_output_dirs(project_dir)
    builder = EvidenceBuilder(inventory.run, cfg.id_digest_salt, cfg.redaction_mode)

    queries: dict[tuple[str, str], ExternalQuery] = {}
    adapter_records: list = []
    for q in ext.queries:
        key = (q.source_id, q.subject_supplier_id or "")
        if key not in queries or STATUS_PRECEDENCE[q.status] > STATUS_PRECEDENCE[queries[key].status]:
            queries[key] = q

    source_configs = load_source_config(config_path)
    live_sources = list(cfg.external_query_sources) if cfg.external_query_mode == "live" else []

    runtime_credentials: dict[str, tuple[str, str]] = {}
    if "srm" in live_sources:
        try:
            runtime_credentials["srm"] = _collect_srm_credentials(allow_interactive=interactive)
        except ProjectError as exc:
            typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

    adapters = {}
    if live_sources:
        adapters = build_adapters({sid: source_configs.get(sid, SourceConfig(source_id=sid, label=sid))
                                   for sid in live_sources}, runtime_credentials=runtime_credentials)

    now = datetime.now(timezone.utc)
    new_queries: list[ExternalQuery] = []
    from tc.normalize import normalize_company_name

    for supplier in entities.suppliers:
        subject = QuerySubject(supplier.supplier_id, supplier.declared_name, supplier.uscc)
        for sid in ALL_SOURCES:
            key = (sid, supplier.supplier_id)
            # SRM 作为报告前置门禁必须在本次运行中重新登录并查询；不能用历史导入
            # 记录代替本次授权查询。其他渠道保留已有结果，避免重复刷新。
            if key in queries and not (sid == "srm" and sid in live_sources):
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
            elif (supplier.confirmation != "confirmed" or not supplier.uscc) and sid != "government_procurement":
                queries[key] = _placeholder_query(
                    sid, subject, "needs_manual_review",
                    "主体未确认（缺少有效统一社会信用代码）；同名结果不能自动归属，需人工查询/导入", now)
            else:
                adapter = adapters[sid]
                result = adapter.query(subject, now)
                q = _adapter_query(sid, subject, result, now, cfg, builder,
                                   version=getattr(adapter, "version", ADAPTER_VERSION_TAG))
                queries[key] = q
                adapter_records.extend(_record_from_adapter(q, result, subject, entities, sid))
                typer.secho(f"  [查询] {supplier.display_name} × {sid}: {result.status}")

    # SRM 浏览器适配器在批量查询期间复用同一会话；所有供应商完成后显式销毁
    # 浏览器上下文，避免 Cookie/页面句柄在宿主进程中继续存活。
    for adapter in adapters.values():
        close = getattr(adapter, "close", None)
        if callable(close):
            close()

    all_records = list(ext.records) + adapter_records
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
        related = [r for r in adapter_records
                   if r.source_id == query.source_id and set(r.evidence_ids) & set(query.evidence_ids)]
        subject_key = content_hash(query.subject_key)[:16]
        cache_file = project_dir / "output/cache/external" / query.source_id / subject_key / f"{query.query_id}.json"
        write_json(cache_file, {
            "query": query.model_dump(mode="json"),
            "records": [r.model_dump(mode="json") for r in related],
        })
    from tc.models import EvidenceFile

    write_json(
        interim / "evidence-external-queries.json",
        EvidenceFile(run=inventory.run, evidence=builder.items).model_dump(mode="json"),
    )
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
    return ExternalQuery(
        query_id=qid,
        source_id=sid,  # type: ignore[arg-type]
        subject_supplier_id=subject.supplier_id,
        subject_key={"uscc": subject.uscc, "name": subject.name},
        query_mode=result.request_mode if result.request_mode != "none" else "none",  # type: ignore[arg-type]
        queried_at=now if result.status not in ("not_queried", "needs_manual_review") else None,
        status=result.status,  # type: ignore[arg-type]
        record_count=len(result.records),
        evidence_ids=ev_ids,
        detail=result.detail,
        adapter_version=version or ADAPTER_VERSION_TAG,
    )


def _record_from_adapter(q: ExternalQuery, result: AdapterResult, subject: QuerySubject, entities: EntitiesFile, sid: str):
    from tc.models import ExternalRecord

    out = []
    for i, rec in enumerate(result.records):
        out.append(ExternalRecord(
            record_id=stable_id("R", {"q": q.query_id, "index": i, "fields": rec.fields}),
            source_id=sid,  # type: ignore[arg-type]
            supplier_id=subject.supplier_id if subject.uscc else None,
            subject_confirmation=rec.subject_confirmation,  # type: ignore[arg-type]
            subject_name=subject.name,
            subject_uscc=subject.uscc,
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
